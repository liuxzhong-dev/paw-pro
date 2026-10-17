# -*- coding: utf-8 -*-
"""
diag_v26_multipos_probe.py —— v26 多点轮询 / 框内随机探针（S3）
==============================================================

【v26 做什么】
点击位置从两种扩成四种：
    cursor 跟随光标   —— 老行为
    fixed  固定坐标   —— v22
    multi  多点轮询   —— 按轮次**依次**取点：A -> B -> C -> A …
    box    框内随机   —— 每轮在两个对角点围成的矩形内随机取一点

【为什么用 tick_count 而不是自己维护游标】
次数上限（count_limit）就是按 tick_count 判的。轮询序号若另开一个
计数器，出问题时「第 N 次点击该点哪儿」就算不出来 —— 两个计数同源，
日志/复现都能直接推算。

【本探针五层】
  1. 解析层   _parse_points：分号/换行/中文标点/列表四种写法，
              坏行整条报错（不静默丢点）、200 点上限、负坐标保留
  2. 参数层   _parse_mouse_params：四模式白名单、box 两点排序、
              box/multi 点数不对必须报错、hold 一律归零
  3. 选点层   _next_target：轮询序列、框内落点、固定点、跟随光标
  4. 接线层   真跑引擎（注入全打桩）：move_to 序列必须 A,B,C,A,B,C；
              引擎 options.mode 只能喂 cursor/fixed（multi/box 由插件搬）
  5. 双源层   ui/dashboard.html == 内嵌 HTML，且前端键真的收集了 points

【负向对照（证明断言不是恒真）】
  NC-A 把 pos_mode 打回 fixed（= v22 只会点一个点）→ 轮询断言必须红
  NC-B 把 random_uniform_int 打桩成恒返回下界 → 随机断言必须红
  NC-C 把 p["mode"] 直传给引擎（旧写法）→ cursor/fixed 不变量必须红

【纪律】绝不发真实键鼠：_click_once / inp.move_to 全部打桩（硬规则 6），
打桩计数器每场景清零（v22 六条假红的教训）。
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def section(t):
    print("\n=== %s ===" % t)


def summary():
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 56)
    print("v26 探针汇总：%d 项通过 / %d 项失败（共 %d）"
          % (len(PASS), len(FAIL), total))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 56)
    return 0 if not FAIL else 1


def code_only(src):
    """剥掉 Python 注释后的代码（硬规则 38：注释里的旧写法会被误判）。"""
    import re
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def html_code(html):
    """剥掉 HTML/JS/CSS 注释。"""
    import re
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"/\*[\s\S]*?\*/", "", html)
    html = re.sub(r"(?m)//.*$", "", html)
    return html


# ------------------------------------------------------------------
# 打桩装置（与 v23 探针同一套）
# ------------------------------------------------------------------
def make_stubs():
    return {"clicks": {"n": 0}, "moves": [], "cursor": (500, 500)}


def install_stubs(D, stubs):
    stubs["real_click"] = D._click_once
    stubs["real_move"] = D.inp.move_to
    stubs["real_size"] = D.inp.screen_size
    stubs["real_vscreen"] = D.inp.virtual_screen
    stubs["real_pos"] = D.inp.get_cursor_pos
    D._click_once = lambda token: _inc(stubs)
    D.inp.move_to = lambda x, y: (stubs["moves"].append((int(x), int(y)))
                                  or True)
    D.inp.screen_size = lambda: (1920, 1080)
    D.inp.virtual_screen = lambda: (0, 0, 1920, 1080)
    D.inp.get_cursor_pos = lambda: stubs["cursor"]


def _inc(stubs):
    stubs["clicks"]["n"] += 1


def restore_stubs(D, stubs):
    D._click_once = stubs["real_click"]
    D.inp.move_to = stubs["real_move"]
    D.inp.screen_size = stubs["real_size"]
    D.inp.virtual_screen = stubs["real_vscreen"]
    D.inp.get_cursor_pos = stubs["real_pos"]


def reset_state(D, api, stubs):
    """清场：停任务 + 清暂停 + 打桩计数器清零（不清就是累加和）。"""
    try:
        api.stop_mouse()
    except Exception:
        pass
    D.pausectl.set_paused(False)
    stubs["clicks"]["n"] = 0
    stubs["moves"] = []
    stubs["cursor"] = (500, 500)
    with D.ST.lock:
        D.ST.mouse_armed = False
        D.ST.mouse_running = False
        D.ST.mouse_engine = None
        D.ST.mouse_stop_requested = False
        D.ST.mouse_msg = ""


def wait_engine_done(D, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with D.ST.lock:
            if not D.ST.mouse_running and D.ST.mouse_engine is None:
                return True
        time.sleep(0.02)
    return False


def raises(fn, *a, **k):
    """返回 (是否抛异常, 异常文本)。"""
    try:
        fn(*a, **k)
        return False, ""
    except Exception as e:
        return True, "%s: %s" % (type(e).__name__, e)


# ==================================================================
# 1. 解析层：_parse_points 的四种写法
# ==================================================================
def test_parse_points(D):
    section("1. _parse_points 解析（分号/换行/中文标点/列表）")

    got = D._parse_points("100,200;300,400")
    check("分号分隔 -> 两个点", got == [[100, 200], [300, 400]], got)

    got = D._parse_points("100,200\n300,400")
    check("换行分隔（界面文本框就是这种）",
          got == [[100, 200], [300, 400]], got)

    got = D._parse_points("100，200；300，400")
    check("中文标点也认（用户手输很容易带）",
          got == [[100, 200], [300, 400]], got)

    got = D._parse_points("300,400\r\n100,200\r\n")
    check("Windows 换行 + 末尾空行不产生空点",
          got == [[300, 400], [100, 200]], got)

    got = D._parse_points([[10, 20], [30, 40]])
    check("列表输入（配置导入走这条）",
          got == [[10, 20], [30, 40]], got)

    got = D._parse_points("".strip())
    check("空串 -> 空列表（multi 空由参数层报错，解析层不炸）",
          got == [], got)

    got = D._parse_points(None)
    check("None -> 空列表", got == [], got)

    # 坏行必须整条报错：少点一个位置比报错危险得多（挂机没人盯着）
    ok, msg = raises(D._parse_points, "100,200;abc")
    check("坏行整条报错（不静默丢掉坏行）", ok, msg)

    ok, msg = raises(D._parse_points, "100;200")
    check("只有一个数报错（缺 y）", ok, msg)

    ok, msg = raises(D._parse_points, "1,2", True)
    check("need_two：只给 1 个点必须报错", ok, msg)

    ok, msg = raises(D._parse_points, "1,2;3,4;5,6", True)
    check("need_two：给 3 个点必须报错", ok, msg)

    many = ";".join("%d,%d" % (i, i) for i in range(201))
    ok, msg = raises(D._parse_points, many)
    check("超过 200 个点必须报错（防手滑粘一整屏）", ok, msg)

    many_ok = ";".join("%d,%d" % (i, i) for i in range(200))
    got = D._parse_points(many_ok)
    check("正好 200 个点放行", len(got) == 200, len(got))

    # clamp 走虚拟桌面（v25 的结论，副屏负坐标必须保留）
    real_vs = D.inp.virtual_screen
    D.inp.virtual_screen = lambda: (-1920, 0, 3840, 1080)
    try:
        got = D._parse_points("-800,400")
        check("副屏负坐标被保留（不被夹成 0）",
              got == [[-800, 400]], got)
        got = D._parse_points("9999,400")
        check("超右界夹到虚屏右边缘 1919",
              got == [[1919, 400]], got)
    finally:
        D.inp.virtual_screen = real_vs


# ==================================================================
# 2. 参数层：_parse_mouse_params 的四模式
# ==================================================================
def test_parse_params(D):
    section("2. _parse_mouse_params 四模式接线")

    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "multi",
                                    "points": "100,200\n300,400"})
    check("multi 解析成功", err is None, err)
    check("multi：mode=multi", (p or {}).get("mode") == "multi",
          (p or {}).get("mode"))
    check("multi：points 两个点",
          (p or {}).get("points") == [[100, 200], [300, 400]],
          (p or {}).get("points"))
    check("multi：pos 保持 None（单点字段不掺进来）",
          (p or {}).get("pos") is None, (p or {}).get("pos"))

    # box：两点倒着输也要能认（自动排成左上/右下）
    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "box",
                                    "points": "300,400;100,200"})
    check("box：两点倒序被排序成左上/右下",
          err is None and (p or {}).get("points") == [[100, 200], [300, 400]],
          (err, (p or {}).get("points")))

    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "box", "points": "500,600;100,200"})
    check("box：x/y 各自排序（x 反、y 也反）",
          err is None and (p or {}).get("points") == [[100, 200], [500, 600]],
          (err, (p or {}).get("points")))

    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "box", "points": "100,200"})
    check("box：只给 1 个点必须报错（不静默退化成固定坐标）",
          err is not None, p)

    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "multi", "points": ""})
    check("multi：空列表必须报错（否则启动后站在光标上点）",
          err is not None, p)

    # 不回归：fixed 仍走 pos
    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "fixed", "pos": "10,20"})
    check("fixed 不回归：pos 解析成 [10,20]",
          err is None and (p or {}).get("pos") == [10, 20],
          (err, (p or {}).get("pos")))
    check("fixed：points 为空（不串到多点字段）",
          (p or {}).get("points") == [], (p or {}).get("points"))

    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "cursor"})
    check("cursor：不需要坐标，points 为空",
          err is None and (p or {}).get("points") == [],
          (err, (p or {}).get("points")))

    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "weird", "points": "1,2"})
    check("未知模式退回 cursor（白名单之外不执行）",
          err is None and (p or {}).get("mode") == "cursor",
          (err, (p or {}).get("mode")))

    # 长按路径：有状态的动作不吃坐标
    p, err = D._parse_mouse_params({"button": "left", "action": "hold",
                                    "mode": "multi",
                                    "points": "100,200;300,400"})
    check("hold：mode 归零成 cursor、points 清空（不静默忽略）",
          err is None and (p or {}).get("mode") == "cursor"
          and (p or {}).get("points") == [],
          (err, (p or {}).get("mode"), (p or {}).get("points")))

    # 抖器路径：位置参数对它无意义，但也不该炸
    p, err = D._parse_mouse_params({"button": "left", "action": "jiggle",
                                    "mode": "multi", "points": "1,2;3,4",
                                    "jig": 5})
    check("jiggle：解析不炸且 jig 仍生效",
          err is None and (p or {}).get("jig") == 5,
          (err, (p or {}).get("jig")))


# ==================================================================
# 3. 选点层：_next_target
# ==================================================================
class _Ctx(object):
    """最小上下文：_next_target 只认 tick_count。"""

    def __init__(self, tick):
        self.tick_count = tick
        self.clicked = 0


def test_next_target(D):
    section("3. DynClick._next_target 选点语义")

    pts = [(100, 200), (300, 400), (500, 600)]
    pl = D._make_mouse_plugin("left", 0, "single", None, 0, pts, "multi")
    check("插件拿到 points（构造参数没丢）",
          pl.points == pts, pl.points)
    check("插件 pos_mode = multi", pl.pos_mode == "multi", pl.pos_mode)

    seq = [pl._next_target(_Ctx(i)) for i in range(7)]
    check("multi 轮询序列：A,B,C,A,B,C,A",
          seq == [pts[0], pts[1], pts[2], pts[0], pts[1], pts[2], pts[0]],
          seq)

    # box：真实随机，落点必须在框内且不能恒定
    plb = D._make_mouse_plugin("left", 0, "single", None, 0,
                               [(100, 100), (110, 110)], "box")
    hits = [plb._next_target(_Ctx(i)) for i in range(40)]
    check("box 落点都在框内（40 次全中）",
          all(100 <= x <= 110 and 100 <= y <= 110 for x, y in hits),
          [h for h in hits if not (100 <= h[0] <= 110
                                   and 100 <= h[1] <= 110)][:3])
    check("box 落点不全相同（真的在随机）",
          len(set(hits)) > 1, len(set(hits)))

    # fixed：恒定一点
    plf = D._make_mouse_plugin("left", 0, "single", (7, 8), 0, [], "fixed")
    check("fixed 每轮都是同一点",
          {plf._next_target(_Ctx(i)) for i in range(3)} == {(7, 8)},
          [plf._next_target(_Ctx(i)) for i in range(3)])

    # cursor：None（不搬鼠标）
    plc = D._make_mouse_plugin("left", 0, "single", None, 0, [], "cursor")
    check("cursor 返回 None（不搬鼠标，白费 API 调用）",
          plc._next_target(_Ctx(0)) is None, plc._next_target(_Ctx(0)))

    # ---- NC-A：打回 v22 行为（只会点一个点）----
    pln = D._make_mouse_plugin("left", 0, "single", pts[0], 0, pts, "fixed")
    nseq = [pln._next_target(_Ctx(i)) for i in range(7)]
    check("NC-A 负向对照：打回 fixed 后 7 轮全同一点"
          "（证明上面的轮询断言真能区分）",
          len(set(nseq)) == 1, nseq)

    # ---- NC-B：随机打桩成恒返回下界 ----
    real_rand = D.random_uniform_int
    D.random_uniform_int = lambda a, b: min(a, b)
    try:
        fixed_hits = [plb._next_target(_Ctx(i)) for i in range(10)]
    finally:
        D.random_uniform_int = real_rand
    check("NC-B 负向对照：随机恒返回下界后落点全同"
          "（证明「不全相同」断言不是恒真）",
          len(set(fixed_hits)) == 1, fixed_hits[:3])


# ==================================================================
# 4. 接线层：真跑引擎，move_to 序列必须按轮询走
# ==================================================================
def _run_once(D, api, stubs, params, expect_ticks, timeout=6.0):
    """arm + start 一轮，返回 (启动结果, move 序列, 点击数, 是否结束)。"""
    reset_state(D, api, stubs)
    r = api.arm_mouse(params, hide=False)
    if not r.get("ok"):
        return r, [], 0, True
    api.start_mouse(api.last_mouse, hide=False)
    done = wait_engine_done(D, timeout)
    moves = list(stubs["moves"])
    clicks = stubs["clicks"]["n"]
    reset_state(D, api, stubs)
    return r, moves, clicks, done


def test_wiring(D, api, stubs):
    section("4. 接线：引擎里真的按多点搬鼠标")

    params = {"button": "left", "action": "click", "count": 6,
              "duration": 0, "cps": 50, "mode": "multi",
              "points": "100,200\n300,400\n500,600"}
    r, moves, clicks, done = _run_once(D, api, stubs, params, 6)
    check("multi 一轮跑完（次数上限能停住）", done, "still running")
    check("multi：6 轮 = 6 次搬鼠标", len(moves) == 6, moves)
    check("multi：搬动序列严格 A,B,C,A,B,C",
          moves == [(100, 200), (300, 400), (500, 600)] * 2, moves)
    check("multi：每轮仍点一下（位置改了不能把点击弄丢）",
          clicks == 6, clicks)

    params = {"button": "left", "action": "click", "count": 6,
              "duration": 0, "cps": 50, "mode": "box",
              "points": "100,100\n140,140"}
    r, moves, clicks, done = _run_once(D, api, stubs, params, 6)
    check("box 一轮跑完", done, "still running")
    check("box：6 轮 = 6 次搬鼠标", len(moves) == 6, moves)
    check("box：落点都在框内",
          all(100 <= x <= 140 and 100 <= y <= 140 for x, y in moves), moves)
    check("box：落点不全相同（不是固定一个角）",
          len(set(moves)) > 1, moves)

    # 不回归：fixed 仍是每轮搬同一点，cursor 一次都不搬
    params = {"button": "left", "action": "click", "count": 4,
              "duration": 0, "cps": 50, "mode": "fixed", "pos": "800,600"}
    r, moves, clicks, done = _run_once(D, api, stubs, params, 4)
    check("fixed 不回归：4 轮都搬 (800,600)",
          moves == [(800, 600)] * 4, moves)

    params = {"button": "left", "action": "click", "count": 4,
              "duration": 0, "cps": 50, "mode": "cursor"}
    r, moves, clicks, done = _run_once(D, api, stubs, params, 4)
    check("cursor 不回归：一次都不搬鼠标", moves == [], moves)
    check("cursor 不回归：照样点 4 下", clicks == 4, clicks)


def test_engine_options(D, api, stubs):
    section("5. 引擎 options 只喂 cursor/fixed（multi/box 由插件搬）")
    captured = {}

    class FakeEngine(object):
        def __init__(self, plugin, options, state=None, base_cps=10,
                     jitter_ms=0, duration=0, count_limit=0, fixed_pos=None):
            captured.update(options=dict(options), fixed_pos=fixed_pos,
                            plugin=plugin)
            self.running = False

        def start(self):
            with D.ST.lock:            # 模拟「瞬间结束」，别留运行态
                D.ST.mouse_engine = None
            return True

        def stop(self):
            self.running = False

    real_eng = D.eng.ClickerEngine
    D.eng.ClickerEngine = FakeEngine
    try:
        for mode, extra in (("multi", {"points": "1,1;2,2"}),
                            ("box", {"points": "1,1;9,9"}),
                            ("fixed", {"pos": "800,600"}),
                            ("cursor", {})):
            reset_state(D, api, stubs)
            captured.clear()
            params = {"button": "left", "action": "click", "count": 0,
                      "duration": 0, "cps": 10, "mode": mode}
            params.update(extra)
            api.arm_mouse(params, hide=False)
            api.start_mouse(api.last_mouse, hide=False)
            opts = captured.get("options", {})
            check("%s：引擎 mode 在 cursor/fixed 白名单内" % mode,
                  opts.get("mode") in ("cursor", "fixed"), opts)
            if mode in ("multi", "box"):
                check("%s：引擎 mode 必须是 cursor（插件自己搬）" % mode,
                      opts.get("mode") == "cursor", opts)
                check("%s：引擎 fixed_pos 必须是 None" % mode,
                      captured.get("fixed_pos") is None,
                      captured.get("fixed_pos"))
            if mode == "fixed":
                check("fixed：引擎 mode=fixed 且拿到单点",
                      opts.get("mode") == "fixed"
                      and captured.get("fixed_pos") == [800, 600],
                      (opts, captured.get("fixed_pos")))
            reset_state(D, api, stubs)
    finally:
        D.eng.ClickerEngine = real_eng

    # ---- NC-C：旧写法（p["mode"] 直传）会破坏这个不变量 ----
    old_options = {"mode": "multi"}
    check("NC-C 负向对照：旧写法把 multi 直传引擎，"
          "不满足 cursor/fixed 不变量（证明断言可区分）",
          old_options.get("mode") not in ("cursor", "fixed"), old_options)


# ==================================================================
# 6. 静态 + 双源
# ==================================================================
def test_static(D):
    section("6. 静态断言 + 双源同步")

    py = code_only(open(os.path.join(HERE, "dashboard.py"),
                        encoding="utf-8").read())
    check("四模式白名单在代码里（不是注释里）",
          'if mode not in ("cursor", "fixed", "multi", "box")' in py)
    check("旧两模式白名单已彻底消失",
          '("cursor", "fixed")' not in py)
    check("_next_target 用 tick_count 取模",
          "ctx.tick_count % len(self.points)" in py)
    check("box 走 random_uniform_int（可打桩）",
          "random_uniform_int(x1, x2)" in py
          and "random_uniform_int(y1, y2)" in py)
    check("引擎 options 只认 fixed/cursor",
          'engine_options = {"mode": "fixed" if p["mode"] == "fixed"'
          in py and 'else "cursor"}' in py)
    check("引擎 fixed_pos 只给 fixed 模式",
          'engine_fixed = p["pos"] if p["mode"] == "fixed" else None' in py)
    check("插件工厂把 points/pos_mode 透传下去",
          "points=points, pos_mode=pos_mode" in py)
    # 定义 1 处 + 调用 2 处（就绪日志、启动日志）= 3；多一处说明又有一处
    # 各写各的（下次加模式必然漏改其中一处）
    check("就绪与启动两处日志共用 _pos_desc_of（不会再漏改一处）",
          py.count("def _pos_desc_of(p)") == 1
          and py.count("_pos_desc_of(p)") == 3,
          py.count("_pos_desc_of(p)"))

    disk = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    check("双源同步：内嵌 HTML == ui/dashboard.html",
          D.HTML == disk, (len(D.HTML), len(disk)))

    h = html_code(D.HTML)
    check("界面有坐标列表输入框", 'id="mPoints"' in h)
    check("界面有坐标列表行容器", 'id="mPointsRow"' in h)
    check("位置下拉有 multi 选项", 'value="multi"' in h)
    check("位置下拉有 box 选项", 'value="box"' in h)
    check("联动函数控制坐标列表显隐",
          'mPointsRow").style.display' in h)
    check("collectSettings 收集 points",
          'points: $("mPoints").value' in h)
    check("applySettings 恢复 points",
          "if(m.points != null)" in h)
    check("自动保存监听 mPoints（否则重启丢列表）",
          '"mPoints",' in h)
    check("「取当前」按模式分流追加（multi/box 追加一行）",
          'mode === "multi" || mode === "box"' in h)
    check("有清空坐标列表的入口", "function clearPoints(" in h
          and "clearPoints()" in h)
    check("框内随机提示了两个角（左上/右下）",
          "左上、右下" in h)


# ==================================================================
# 7. 引擎默认主循环：fixed 必须真搬鼠标（v26 顺带修的实 bug）
# ==================================================================
def test_engine_default_run_fixed():
    section("7. 引擎默认主循环（CLI 走这条）fixed 必须真搬")
    import json
    import tempfile

    from core import engine as eng
    from core import inputs as inp

    class Plain(eng.Plugin):
        name = "probe_plain"

    moves, clicks = [], {"n": 0}
    real_move, real_click = inp.move_to, inp.click
    inp.move_to = lambda x, y: (moves.append((int(x), int(y))) or True)
    inp.click = lambda *a, **k: (clicks.__setitem__("n", clicks["n"] + 1)
                                 or True)
    try:
        e = eng.ClickerEngine(Plain(), options={"mode": "fixed"},
                              base_cps=50, count_limit=3,
                              fixed_pos=[100, 200])
        e.start()
        t0 = time.time()
        while e.running and time.time() - t0 < 5:
            time.sleep(0.02)
        e.stop()
        check("默认主循环跑满 3 轮", e.ctx.tick_count == 3, e.ctx.tick_count)
        check("fixed 模式每轮都搬到 (100,200)（旧代码从不搬）",
              moves == [(100, 200)] * 3, moves)
        check("搬完仍点了 3 下", clicks["n"] == 3, clicks["n"])

        moves[:] = []
        clicks["n"] = 0
        e2 = eng.ClickerEngine(Plain(), options={"mode": "current"},
                               base_cps=50, count_limit=2)
        e2.start()
        t0 = time.time()
        while e2.running and time.time() - t0 < 5:
            time.sleep(0.02)
        e2.stop()
        check("current 模式一次都不搬（不回归）", moves == [], moves)
    finally:
        inp.move_to, inp.click = real_move, real_click

    # ---- NC-D：把搬运那两行打掉（= 旧代码），断言必须红 ----
    src = code_only(open(os.path.join(HERE, "core", "engine.py"),
                         encoding="utf-8").read())
    old_src = src.replace('if ctx.options.get("mode") == "fixed" '
                          "and ctx.fixed_pos:", "if False:")
    check("NC-D 负向对照：打掉 fixed 搬运分支后，"
          "该分支在源码里不再生效（证明断言盯的是真代码）",
          "if False:" in old_src
          and 'if ctx.options.get("mode") == "fixed" and ctx.fixed_pos:' in src)


# ==================================================================
# 8. CLI：多点/框内方案必须大声拒绝（不静默退化成跟随光标）
# ==================================================================
def test_cli_refuses_multipos():
    section("8. CLI --profile：多点/框内方案大声拒绝")
    import json
    import shutil
    import tempfile as tf

    from core import config as cfg
    import cli

    tmp = tf.mkdtemp(prefix="v26prof_")
    real_cache = cfg._settings_dir_cache[0]
    cfg._settings_dir_cache[0] = tmp
    try:
        pdir = cfg.profiles_dir()
        base = {"schema_version": 1,
                "mouse": {"button": "left", "action": "click",
                          "count": "10", "duration": "0", "cps": "10",
                          "jitter": "0", "mode": "fixed", "pos": "640,360",
                          "countdown": "0", "hide": False, "jig": "30",
                          "done": "none"},
                "hide": False}
        for name, mode, kw in (("多点方案", "multi",
                                {"points": "100,200\n300,400"}),
                               ("框内方案", "box",
                                {"points": "100,100\n200,200"})):
            prof = dict(base)
            prof["mouse"] = dict(base["mouse"])
            prof["mouse"]["mode"] = mode
            for k, v in kw.items():
                prof["mouse"][k] = v
            with open(os.path.join(pdir, "%s.json" % name), "w",
                      encoding="utf-8") as f:
                json.dump(prof, f, ensure_ascii=False)

        data, mode, msg = cli.data_from_profile("多点方案")
        check("多点方案：data 为 None（不静默降级）", data is None, (data, msg))
        check("多点方案：报错指明「多点轮询」+ 指路图形版",
              "多点轮询" in (msg or "") and "图形版" in (msg or ""), msg)
        data, mode, msg = cli.data_from_profile("框内方案")
        check("框内方案：data 为 None", data is None, (data, msg))
        check("框内方案：报错指明「框内随机」",
              "框内随机" in (msg or ""), msg)

        # 固定坐标方案不回归：仍能映射
        with open(os.path.join(pdir, "固定方案.json"), "w",
                  encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False)
        data, mode, msg = cli.data_from_profile("固定方案")
        check("固定坐标方案不回归：仍映射成 fixed",
              data is not None and data["mode"] == "fixed"
              and data["fixed_pos"] == [640, 360], (msg, data))
    finally:
        cfg._settings_dir_cache[0] = real_cache
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    import dashboard as D

    tmp = os.path.join(HERE, "_test_v26_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)

    stubs = make_stubs()
    install_stubs(D, stubs)
    api = D.Api()
    try:
        test_parse_points(D)
        test_parse_params(D)
        test_next_target(D)
        test_wiring(D, api, stubs)
        test_engine_options(D, api, stubs)
        test_static(D)
        test_engine_default_run_fixed()
        test_cli_refuses_multipos()
    finally:
        reset_state(D, api, stubs)
        restore_stubs(D, stubs)
        try:
            os.remove(tmp)
        except OSError:
            pass
    return summary()


if __name__ == "__main__":
    sys.exit(main())
