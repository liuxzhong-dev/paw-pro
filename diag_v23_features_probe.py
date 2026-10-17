# -*- coding: utf-8 -*-
"""
diag_v23_features_probe.py —— v23 新功能专项探针（S4/S5/S6）
============================================================

【v23 三个功能】主人 2026-10-17 拍板「开工」，S1~S6 排队，本版做小的三个：

  S4 完成后动作   —— 次数/时长自然用完才触发：提示音/锁屏/关机。
                    安全三件套：用户停止绝不触发 + 关机 30 秒可取消 +
                    系统层 shutdown /a 兜底。参考 WinAutoClick。
  S5 CLI --profile —— 命令行按名字加载图形版存的方案（profiles/），
                    无头启动。加载失败大声报错退出，不静默用默认值。
                    参考 EC-Autoclicker 的 /profile 无头模式。
  S6 鼠标抖器     —— 每隔 N 秒把光标挪 1 像素再挪回，只挪不点，
                    防息屏/防挂机检测。参考 FluAutoClicker jiggler。

【测试纪律】和 v22 探针一致：注入类调用全部打桩成计数器，
绝不往系统发真实输入（硬规则 6）；打桩计数器场景间必须清零。

【负向对照】每个功能都配「打回旧行为/打掉一层」：
  · 抖器必须 clicked == 0（把 click 加回去必须红）
  · 用户停止后收尾动作必须不触发（把标志去掉必须红）
  · --profile 方案不存在必须报错（静默默认值必须红）
"""

import json
import os
import shutil
import sys
import tempfile
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
    print("v23 探针汇总：%d 项通过 / %d 项失败（共 %d）"
          % (len(PASS), len(FAIL), total))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 56)
    return 0 if not FAIL else 1


# ------------------------------------------------------------------
# 静态断言剥注释（硬规则 38）：三种语法都踩过，注释里的同文本
# 会被当成残留代码放过去。
# ------------------------------------------------------------------
def code_only(src):
    """剥掉 Python 三风格注释后的代码（朴素但够用：本项目不用
    三引号字符串跨行包裹可执行代码）。"""
    import re
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    out = []
    for line in src.split("\n"):
        line = re.sub(r"#.*$", "", line)
        out.append(line)
    return "\n".join(out)


def html_code(html):
    """剥掉 HTML 注释与 JS/CSS 注释。"""
    import re
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"/\*[\s\S]*?\*/", "", html)
    html = re.sub(r"(?m)//.*$", "", html)
    return html


# ------------------------------------------------------------------
# 测试装置
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
    # v25：坐标 clamp 改用虚拟桌面，打桩成「单屏 1920x1080」
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
    """清场：停任务 + 清暂停 + 清零打桩计数器（不清就是累加和，
    v22 六个假红的教训）。"""
    try:
        api.stop_mouse()
    except Exception:
        pass
    D.pausectl.set_paused(False)
    stubs["clicks"]["n"] = 0
    stubs["moves"] = []
    stubs["cursor"] = (500, 500)
    with D.ST.lock:
        D.ST.stats["click"] = 0
        D.ST.stats["inject"] = 0
        D.ST.mouse_armed = False
        D.ST.mouse_stop_requested = False
        D.ST.mouse_msg = ""


def wait_engine_done(D, timeout=4.0):
    """等引擎线程真正结束（on_finish 是在引擎线程里调的）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with D.ST.lock:
            if not D.ST.mouse_running and D.ST.mouse_engine is None:
                return True
        time.sleep(0.05)
    return False


# ==================================================================
# 1. _parse_mouse_params 的 v23 新键（jig / done）
# ==================================================================
def test_parse_params_v23(D):
    section("1. _parse_mouse_params · v23 新键规范化")
    f = D._parse_mouse_params

    p, err = f({"action": "jiggle", "jig": "15", "done": "sound"})
    check("jiggle 是合法动作", err is None, err)
    check("jig 转 int", p and p["jig"] == 15, p)
    check("done 白名单内保留", p and p["done"] == "sound", p)

    p, err = f({"action": "jiggle"})
    check("jig 缺省 30", err is None and p["jig"] == 30, (err, p))
    check("done 缺省 none", p and p["done"] == "none", p)

    p, err = f({"jig": "0"})
    check("jig=0 太吵 -> 兜底 30", err is None and p["jig"] == 30,
          (err, p))
    p, err = f({"jig": "99999"})
    check("jig 超大 -> 封顶 3600", err is None and p["jig"] == 3600,
          (err, p))

    p, err = f({"done": "boom"})
    check("陌生收尾动作 -> none（不静默执行陌生动作）",
          err is None and p["done"] == "none", (err, p))
    check("v22 键没被带坏（回归）",
          err is None and p["action"] == "click" and p["jitter"] == 0,
          (err, p))

    # 长按：收尾动作归零（press/release 路径没有自然完成事件）
    p, err = f({"action": "hold", "done": "shutdown", "countdown": "5",
                "mode": "fixed", "pos": "10,20"})
    check("长按：done 归零为 none", err is None and p["done"] == "none",
          (err, p))
    check("长按：v22 的归零规则仍在（倒计时/坐标清掉）",
          p["countdown"] == 0 and p["pos"] is None, p)


# ==================================================================
# 2. core/actions 单元（OS 调用全打桩）
# ==================================================================
def test_actions_unit():
    section("2. core/actions · 收尾动作模块")
    from core import actions as A

    calls = {"sound": 0, "lock": 0, "shutdown": 0, "abort": 0}
    real = (A._do_sound, A._do_lock, A._do_shutdown, A._do_abort)
    A._do_sound = lambda: (calls.__setitem__("sound", calls["sound"] + 1)
                           or True)
    A._do_lock = lambda: (calls.__setitem__("lock", calls["lock"] + 1)
                          or True)
    A._do_shutdown = lambda: (calls.__setitem__("shutdown",
                                                calls["shutdown"] + 1)
                              or True)
    A._do_abort = lambda: (calls.__setitem__("abort", calls["abort"] + 1)
                           or True)
    try:
        A.reset()
        check("arm(none) 直接不排", A.arm("none") is False)
        check("arm(陌生动作) 不排", A.arm("format_c") is False)
        check("没排时 pending=False", A.is_pending() is False)

        # 正常倒计时到点执行（1 秒，快测）
        ticks = []
        ok = A.arm("sound", seconds=1, on_tick=lambda l: ticks.append(l))
        check("arm(sound) 排队成功", ok is True)
        check("排队中 pending=True", A.is_pending() is True)
        info = A.pending_info()
        check("pending_info 带动作与剩余秒数",
              info.get("action") == "sound" and info.get("left") == 1,
              info)
        time.sleep(1.6)
        check("到点执行了提示音", calls["sound"] == 1, calls)
        check("执行完 pending 回落", A.is_pending() is False)
        check("on_tick 每秒回调过", len(ticks) >= 1, ticks)

        # 倒计时中取消：绝不执行
        A.reset()
        ok = A.arm("shutdown", seconds=5)
        check("arm(shutdown) 排队成功", ok is True)
        time.sleep(0.4)
        cancelled, msg = A.cancel()
        check("取消返回 True + 说明", cancelled is True and "取消" in msg,
              (cancelled, msg))
        time.sleep(0.3)
        check("取消后不执行关机", calls["shutdown"] == 0, calls)
        check("取消后 pending 回落", A.is_pending() is False)

        # 关机指令已发：fired 状态可取消，且发 shutdown /a
        A.reset()
        A.arm("shutdown", seconds=1)
        time.sleep(1.5)
        check("到点发了关机指令", calls["shutdown"] == 1, calls)
        check("指令已发后仍可取消（pending 保持）",
              A.is_pending() is True)
        cancelled, msg = A.cancel()
        check("取消已发指令 -> 调 shutdown /a",
              cancelled is True and calls["abort"] == 1, (cancelled, calls))
        check("撤销后 pending 回落", A.is_pending() is False)

        # 锁屏
        A.reset()
        A.arm("lock", seconds=1)
        time.sleep(1.5)
        check("锁屏到点执行", calls["lock"] == 1, calls)

        # 不叠加：排队中再 arm 返回 False
        A.reset()
        check("第一次 arm 成功", A.arm("lock", seconds=5) is True)
        check("排队中第二次 arm 被拒（不叠加）",
              A.arm("sound", seconds=1) is False)
        A.cancel()

        # 什么都没排时取消：原样返回不炸
        A.reset()
        cancelled, msg = A.cancel()
        check("空取消：返回 False + 友好消息",
              cancelled is False and "没有" in msg, (cancelled, msg))
    finally:
        A._do_sound, A._do_lock, A._do_shutdown, A._do_abort = real
        A.reset()


# ==================================================================
# 3. S6 抖器插件（打桩 inputs）
# ==================================================================
def test_jiggler_plugin():
    section("3. plugins/jiggler · 鼠标抖器")
    from core import engine as eng
    from core import inputs as inp
    import plugins.jiggler as JG

    pl = eng.Registry.get("jiggler")
    check("jiggler 已注册", pl is not None)
    check("注册表里拿到的是 Jiggler 实例",
          isinstance(pl, JG.Jiggler), type(pl))

    # 打桩 inputs：记录 move_to，get_cursor_pos 返回固定点
    moves = []
    real_move, real_pos = inp.move_to, inp.get_cursor_pos
    inp.move_to = lambda x, y: (moves.append((int(x), int(y))) or True)
    inp.get_cursor_pos = lambda: (500, 500)
    try:
        e = eng.ClickerEngine(pl, options={"interval": 0.2, "distance": 1},
                              base_cps=1, count_limit=3)
        check("引擎启动成功", e.start() is True)
        t0 = time.time()
        while e.running and time.time() - t0 < 5:
            time.sleep(0.05)
        e.stop()
        check("次数上限能停住抖器（tick_count 语义通用）",
              not e.running, "still running")
        check("clicked 始终为 0（只挪不点 · 负向对照锚点）",
              e.ctx.clicked == 0, "clicked=%d" % e.ctx.clicked)
        check("tick_count 记了挪动轮次",
              e.ctx.tick_count == 3, "tick=%d" % e.ctx.tick_count)
        check("挪动成对：+1 像素再挪回",
              len(moves) >= 6
              and moves[0] == (501, 500) and moves[1] == (500, 500),
              "moves=%r" % (moves[:4],))
        check("每轮都是先挪走再挪回（成对）",
              all(moves[i] == (501, 500) and moves[i + 1] == (500, 500)
                  for i in range(0, min(6, len(moves) - 1), 2)),
              "moves=%r" % (moves[:6],))

        # distance=2 也生效（参数真的传进去了）
        moves2 = []
        inp.move_to = lambda x, y: (moves2.append((int(x), int(y))) or True)
        e2 = eng.ClickerEngine(pl, options={"interval": 0.2, "distance": 2},
                               base_cps=1, count_limit=1)
        e2.start()
        t0 = time.time()
        while e2.running and time.time() - t0 < 5:
            time.sleep(0.05)
        e2.stop()
        check("distance=2：挪 2 像素",
              moves2 and moves2[0] == (502, 500), "moves=%r" % (moves2[:2],))
    finally:
        inp.move_to, inp.get_cursor_pos = real_move, real_pos

    # 负向对照：把 click 加回 run（模拟「改漏了」），clicked 必须爆红
    real_run = JG.Jiggler.run

    def bad_run(self, ctx):
        from core import inputs
        while not ctx.should_stop():
            inputs.click("left")
            ctx.clicked += 1
            ctx.tick_count += 1
            if not ctx.wait(0.05):
                break
        return ctx.clicked

    JG.Jiggler.run = bad_run
    try:
        inp.move_to = lambda x, y: True
        inp.get_cursor_pos = lambda: (500, 500)
        e3 = eng.ClickerEngine(pl, options={"interval": 0.2, "distance": 1},
                               base_cps=1, count_limit=2)
        e3.start()
        t0 = time.time()
        while e3.running and time.time() - t0 < 5:
            time.sleep(0.05)
        e3.stop()
        check("NC-S6 负向对照：把点击加回 run，clicked 必须 >0"
              "（证明「clicked==0」断言真能区分）",
              e3.ctx.clicked > 0, "clicked=%d" % e3.ctx.clicked)
    finally:
        JG.Jiggler.run = real_run
        inp.move_to, inp.get_cursor_pos = real_move, real_pos


# ==================================================================
# 4. S4 dashboard 接线（自然完成才触发 / 用户停止绝不触发）
# ==================================================================
def test_s4_wiring(D, api, stubs):
    section("4. S4 · dashboard 收尾动作接线")
    from core import actions as A

    armed = []
    real_arm = A.arm
    real_cancel = A.cancel

    def rec_arm(action, seconds=30, on_tick=None):
        armed.append(action)
        return False            # 不真起倒计时线程，只记录

    def rec_cancel():
        armed.append("__cancel__")
        return False, "测试取消"

    A.arm = rec_arm
    A.cancel = rec_cancel
    try:
        # --- 自然完成（次数用完）：触发收尾动作 ---
        reset_state(D, api, stubs)
        armed[:] = []
        r = api.arm_mouse({"button": "left", "action": "click", "count": 3,
                           "duration": 0, "cps": 50, "done": "sound"},
                          hide=False)
        check("S4 就绪成功（前置）", r.get("ok") is True, r)
        check("done 进了 last_mouse（热键路径同参）",
              api.last_mouse.get("done") == "sound", api.last_mouse)
        api.start_mouse(api.last_mouse, hide=False)
        check("引擎真的起来了（前置）", D.ST.mouse_running is True)
        done = wait_engine_done(D)
        check("次数用完引擎自然结束（前置）", done is True)
        time.sleep(0.3)          # 给 on_finish 一点余量
        check("自然完成：收尾动作被排定", armed == ["sound"], armed)
        check("自然完成：状态行说已完成",
              "已完成" in (D.ST.mouse_msg or ""), D.ST.mouse_msg)

        # --- done=none：什么都不排 ---
        reset_state(D, api, stubs)
        armed[:] = []
        api.arm_mouse({"button": "left", "action": "click", "count": 2,
                       "duration": 0, "cps": 50, "done": "none"}, hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        wait_engine_done(D)
        time.sleep(0.3)
        check("done=none：不排任何动作", armed == [], armed)

        # --- 用户停止：绝不触发 + 消息不被覆盖 ---
        reset_state(D, api, stubs)
        armed[:] = []
        api.arm_mouse({"button": "left", "action": "click", "count": 9999,
                       "duration": 0, "cps": 50, "done": "shutdown"},
                      hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        time.sleep(0.4)
        rs = api.stop_mouse()
        check("用户停止成功（前置）", rs.get("ok") is True, rs)
        time.sleep(0.8)          # 引擎线程这时才该结束
        check("用户停止：收尾动作绝不触发（关机事故防线）",
              armed == ["__cancel__"], armed)
        check("用户停止：状态行保持「已停止」不被覆盖成「已完成」",
              "已停止" in (D.ST.mouse_msg or "")
              and "已完成" not in (D.ST.mouse_msg or ""), D.ST.mouse_msg)

        # --- 抖器自然完成也触发（同一套 on_finish） ---
        reset_state(D, api, stubs)
        armed[:] = []
        api.arm_mouse({"button": "left", "action": "jiggle", "jig": 1,
                       "count": 1, "duration": 0, "cps": 1,
                       "done": "lock"}, hide=False)
        check("抖器就绪成功（前置）",
              api.last_mouse.get("action") == "jiggle", api.last_mouse)
        api.start_mouse(api.last_mouse, hide=False)
        done = wait_engine_done(D, timeout=6)
        check("抖器按次数自然结束（前置）", done is True)
        time.sleep(0.3)
        check("抖器自然完成：同样触发收尾动作",
              armed == ["lock"], armed)

        # --- 插件抛异常：不触发收尾动作 ---
        reset_state(D, api, stubs)
        armed[:] = []
        real_mk = D._make_mouse_plugin

        class BoomPlugin(D.eng.Plugin):
            name = "boom_probe"

            def run(self, ctx):
                raise RuntimeError("探针炸")

        D._make_mouse_plugin = lambda *a, **k: BoomPlugin()
        try:
            r = api.arm_mouse({"button": "left", "action": "click", "count": 3,
                               "duration": 0, "cps": 50, "done": "sound"},
                              hide=False)
            check("出错场景就绪成功（前置）", r.get("ok") is True, r)
            r = api.start_mouse(api.last_mouse, hide=False)
            check("出错场景启动返回（前置）", r.get("ok") is True, r)
            wait_engine_done(D)
            time.sleep(0.3)
            check("插件出错：不触发收尾动作", armed == [], armed)
            check("插件出错：状态行报错（竞态修复：启动消息不许盖掉出错）",
                  "出错" in (D.ST.mouse_msg or ""), D.ST.mouse_msg)
        finally:
            D._make_mouse_plugin = real_mk

        # --- 取消 API：没排时友好返回 ---
        reset_state(D, api, stubs)
        A.reset()
        r = api.cancel_done_action()
        check("取消 API 可用且不炸",
              r.get("ok") is True and r.get("cancelled") is False, r)

        # --- 陈旧标志防线：stop 后不启动直接再走一次 _finished 语义 ---
        # （模拟：上次停止留下的 mouse_stop_requested 必须被 start 复位，
        #   否则下次自然完成会被误判成用户停止，收尾动作永远不触发）
        # ⚠ 第一轮必须用「跑不完的次数」：count=2 在 50ms 内就自然完成，
        #   停止根本赶不上——那是场景设计错误，不是产品 bug（v22 教训）。
        reset_state(D, api, stubs)
        armed[:] = []
        api.arm_mouse({"button": "left", "action": "click", "count": 9999,
                       "duration": 0, "cps": 50, "done": "sound"},
                      hide=False)
        r = api.start_mouse(api.last_mouse, hide=False)
        check("第一轮启动成功（前置）", r.get("ok") is True, r)
        time.sleep(0.3)
        check("第一轮确实在跑（前置：停止是用户停止）",
              D.ST.mouse_running is True)
        api.stop_mouse()          # 留下 stop_requested=True
        time.sleep(0.5)
        api.arm_mouse({"button": "left", "action": "click", "count": 2,
                       "duration": 0, "cps": 50, "done": "sound"},
                      hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        wait_engine_done(D)
        time.sleep(0.3)
        check("停止→再启动→自然完成：收尾动作正常触发"
              "（陈旧 stop 标志已复位）",
              armed == ["__cancel__", "sound"], armed)
    finally:
        A.arm = real_arm
        A.cancel = real_cancel


# ==================================================================
# 5. S5 CLI --profile
# ==================================================================
def test_s5_cli():
    section("5. S5 · CLI --profile 与方案加载")
    from core import config as cfg
    import cli

    tmp = tempfile.mkdtemp(prefix="v23prof_")
    real_cache = cfg._settings_dir_cache[0]
    cfg._settings_dir_cache[0] = tmp
    try:
        # 图形版形态的方案文件（mouse/key/hide 三段）
        prof = {"schema_version": 1,
                "mouse": {"button": "right", "action": "double",
                          "count": "50", "duration": "0", "cps": "20",
                          "jitter": "15", "mode": "fixed",
                          "pos": "640,360", "countdown": "3",
                          "hide": True, "jig": "30", "done": "lock"},
                "key": {"keys": "w", "threshold": "2", "hz": "30",
                        "hold": "true", "gap": "20"},
                "hide": True}
        pdir = cfg.profiles_dir()
        with open(os.path.join(pdir, "挂机刷材料.json"), "w",
                  encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)

        check("profiles 目录建在设置目录下",
              pdir == os.path.join(tmp, "profiles"), pdir)
        check("list_profiles 列得出",
              cfg.list_profiles() == ["挂机刷材料"], cfg.list_profiles())

        # cfg.load_profile：校验 + 清洗
        ok, cleaned, msg = cfg.load_profile("挂机刷材料")
        check("load_profile 成功", ok is True, msg)
        check("清洗后只留白名单键",
              set(cleaned.keys()) == {"mouse", "key", "hide"}, cleaned.keys())
        check("不存在的方案大声报错",
              cfg.load_profile("不存在的方案")[0] is False)
        check("报错消息带目录线索（告诉用户去哪看）",
              "profiles" in cfg.load_profile("不存在的方案")[2] or
              tmp in cfg.load_profile("不存在的方案")[2],
              cfg.load_profile("不存在的方案")[2])

        # 高版本 schema 拒绝
        bad = dict(prof)
        bad["schema_version"] = 99
        with open(os.path.join(pdir, "高版本.json"), "w",
                  encoding="utf-8") as f:
            json.dump(bad, f, ensure_ascii=False)
        ok, _c, msg = cfg.load_profile("高版本")
        check("高版本方案被拒绝并说明", ok is False and "v99" in msg, msg)

        # 空方案拒绝（v21 空壳事故的防线）
        with open(os.path.join(pdir, "空壳.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"schema_version": 1}, f, ensure_ascii=False)
        ok, _c, msg = cfg.load_profile("空壳")
        check("空壳方案被拒绝（v21 事故防线）",
              ok is False and "没有可识别" in msg, msg)

        # data_from_profile：映射到命令行 data
        data, mode, note = cli.data_from_profile("挂机刷材料")
        check("映射成功", data is not None, note)
        check("cps/抖动/次数映射",
              data["cps"] == 20 and data["random_jitter"] == 15
              and data["count_limit"] == 50, data)
        check("双击动作 -> click_type=double",
              data["click_type"] == "double", data)
        check("固定坐标映射（字符串 -> 列表）",
              data["mode"] == "fixed" and data["fixed_pos"] == [640, 360],
              data)
        check("done_action 映射", data["done_action"] == "lock", data)
        check("mode=normal", mode == "normal", mode)
        check("带键盘区时有提示（不静默丢参数）",
              "键盘区" in note, note)

        # 长按方案：大声报错（CLI 干不了）
        hold = dict(prof)
        hold["mouse"] = dict(prof["mouse"])
        hold["mouse"]["action"] = "hold"
        with open(os.path.join(pdir, "长按.json"), "w",
                  encoding="utf-8") as f:
            json.dump(hold, f, ensure_ascii=False)
        data, mode, msg = cli.data_from_profile("长按")
        check("长按方案：命令行版明确拒绝并指路",
              data is None and "长按锁定" in msg, msg)

        # 抖器方案 -> mode jiggler + 插件参数
        jig = dict(prof)
        jig["mouse"] = dict(prof["mouse"])
        jig["mouse"]["action"] = "jiggle"
        jig["mouse"]["jig"] = 12
        with open(os.path.join(pdir, "挂机防睡.json"), "w",
                  encoding="utf-8") as f:
            json.dump(jig, f, ensure_ascii=False)
        data, mode, note = cli.data_from_profile("挂机防睡")
        check("抖器方案 -> mode=jiggler", mode == "jiggler", mode)
        check("抖器方案 -> interval 进 plugin_state",
              data["plugin_state"]["jiggler"]["interval"] == 12,
              data.get("plugin_state"))

        # 命令行参数覆盖方案
        import argparse
        args = cli.build_parser().parse_args(
            ["--profile", "挂机刷材料", "--cps", "99"])
        base, _m, _n = cli.data_from_profile("挂机刷材料")
        data2, mode2 = cli.resolve(args, base=base)
        check("--cps 覆盖方案的 cps", data2["cps"] == 99, data2["cps"])
        check("没覆盖的键保留方案值（抖动）",
              data2["random_jitter"] == 15, data2["random_jitter"])
        check("--profile 在参数表里",
              "--profile" in cli.build_parser().format_help())
        check("--list-profiles 在参数表里",
              "--list-profiles" in cli.build_parser().format_help())

        # 路径对账：dashboard 与 core.config 必须解析到同一目录
        # （两边都钉到 tmp 上：比的是「路径规则一致」，不是各自独立挑选
        #   的结果——独立挑选在真机上同逻辑，但这里两份缓存互不相干）
        import dashboard as D
        real_d_cache = D._settings_dir_pick_cache[0]
        D._settings_dir_pick_cache[0] = tmp
        try:
            api = D.Api()
            check("dashboard 与 core.config 的方案目录一致"
                  "（改一边忘另一边会「命令行找不到方案」）",
                  api._profiles_dir() == cfg.profiles_dir(),
                  (api._profiles_dir(), cfg.profiles_dir()))
            check("方案文件名安全化一致（防目录穿越）",
                  api._profile_path("a/b") == cfg.profile_path("a/b"),
                  (api._profile_path("a/b"), cfg.profile_path("a/b")))
        finally:
            D._settings_dir_pick_cache[0] = real_d_cache
    finally:
        cfg._settings_dir_cache[0] = real_cache
        shutil.rmtree(tmp, ignore_errors=True)


# ==================================================================
# 6. S6 dashboard 接线（jiggle 走引擎 + Jiggler 插件）
# ==================================================================
def test_s6_wiring(D, api, stubs):
    section("6. S6 · dashboard 抖器接线")

    reset_state(D, api, stubs)
    r = api.arm_mouse({"button": "left", "action": "jiggle", "jig": 1,
                       "count": 3, "duration": 0, "cps": 1}, hide=False)
    check("抖器就绪成功", r.get("ok") is True, r)
    api.start_mouse(api.last_mouse, hide=False)
    check("抖器启动后进入运行态", D.ST.mouse_running is True)
    time.sleep(1.0)
    with D.ST.lock:
        e = D.ST.mouse_engine
    check("引擎插件是 Jiggler",
          e is not None and type(e.plugin).__name__ == "Jiggler",
          type(e.plugin).__name__ if e else None)
    check("引擎 options 带上挪动间隔",
          e is not None and e.ctx.options.get("interval") == 1,
          e.ctx.options if e else None)
    check("抖器运行中：零点击（只挪不点）",
          stubs["clicks"]["n"] == 0, "clicks=%d" % stubs["clicks"]["n"])
    check("抖器运行中：有挪动记录",
          len(stubs["moves"]) >= 2, "moves=%r" % (stubs["moves"][:4],))
    check("状态行显示「抖器」",
          "抖器" in (D.ST.mouse_info or ""), D.ST.mouse_info)
    api.stop_mouse()
    check("抖器能被停止", D.ST.mouse_running is False)

    # 固定坐标 + 抖器：先搬到固定点再绕着他挪
    reset_state(D, api, stubs)
    api.arm_mouse({"button": "left", "action": "jiggle", "jig": 1,
                   "count": 2, "duration": 0, "cps": 1, "mode": "fixed",
                   "pos": "800,600"}, hide=False)
    check("抖器+固定坐标：就绪成功且坐标解析",
          api.last_mouse.get("pos") == [800, 600], api.last_mouse)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(1.0)
    check("抖器+固定坐标：第一站搬到固定点",
          stubs["moves"] and stubs["moves"][0] == (800, 600),
          "moves=%r" % (stubs["moves"][:4],))
    check("抖器+固定坐标：之后绕着当时位置挪（不每轮硬拽回）",
          len(stubs["moves"]) >= 3 and stubs["moves"][1] == (501, 500)
          and stubs["moves"][2] == (500, 500),
          "moves=%r" % (stubs["moves"][:6],))
    api.stop_mouse()


# ==================================================================
# 7. 静态接线（剥注释，硬规则 38）+ 双源同步（硬规则 12）
# ==================================================================
def test_static_wiring(D):
    section("7. 静态接线 + 双源同步")
    import hashlib
    disk = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    src = open(os.path.join(HERE, "dashboard.py"), encoding="utf-8").read()
    cli_src = code_only(open(os.path.join(HERE, "cli.py"),
                             encoding="utf-8").read())
    html = html_code(disk)
    py = code_only(src)

    m = hashlib.md5(disk.encode("utf-8")).hexdigest()
    import re
    mm = re.search(r'HTML = r"""(.*?)"""', src, re.S)
    check("dashboard.py 找得到内嵌 HTML", mm is not None)
    if mm:
        check("内嵌 HTML == ui/dashboard.html（md5）",
              hashlib.md5(mm.group(1).encode("utf-8")).hexdigest() == m)

    # --- 界面控件与函数 ---
    for fid in ("mJig", "mDone", "mJigRow", "doneCancelRow"):
        check("界面存在 #%s" % fid, ('id="%s"' % fid) in html)
    check("mAct 有 jiggle 选项", 'value="jiggle"' in html)
    check("mDone 三个动作选项齐全",
          'value="sound"' in html and 'value="lock"' in html
          and 'value="shutdown"' in html)
    check("界面有 cancelDone 函数", "async function cancelDone" in html)
    check("startMouse 传 jig/done",
          "jig: $(\"mJig\").value" in html
          and "done: $(\"mDone\").value" in html)
    check("collectSettings 收集 jig/done",
          "jig: $(\"mJig\").value" in html
          and "done: $(\"mDone\").value" in html)
    check("applySettings 恢复 jig/done",
          "$(\"mJig\").value   = m.jig" in html
          and "$(\"mDone\").value  = m.done" in html)
    check("syncMouseRows 处理 jiggle 行显隐",
          "mJigRow" in html)
    check("轮询渲染收尾取消按钮", "doneCancelRow" in html)

    # --- Python 侧（剥注释后） ---
    check("_MOUSE_ACTIONS 含 jiggle", '"jiggle"' in py)
    check("_DONE_ACTIONS 白名单存在", "_DONE_ACTIONS" in py)
    check("_finished 有自然完成判定", "natural" in py)
    check("_finished 排定收尾动作", 'actions.arm(p["done"]' in py)
    check("stop_mouse 取消收尾动作", "actions.cancel()" in py)
    check("stop_mouse 置停止标志", "mouse_stop_requested = True" in py)
    check("Api 有 cancel_done_action", "def cancel_done_action(self)" in py)
    check("snapshot 暴露 done_pending", '"done_pending"' in py)
    check("dashboard 委托方案目录给 core.config"
          "（且显式传本进程设置目录，否则重定向/测试会分叉）",
          "cfg.profiles_dir(_settings_dir())" in py
          and "cfg.profile_path(name, _settings_dir())" in py)
    check("jiggle 分支构建 Jiggler 插件",
          "from plugins.jiggler import Jiggler" in py)
    check("引擎 options 带 interval（抖器）",
          '"interval": p["jig"]' in py)

    # --- cli 侧 ---
    check("cli 有 --profile 参数", '"--profile"' in cli_src)
    check("cli 有 data_from_profile", "def data_from_profile" in cli_src)
    check("cli 自然完成排定收尾动作", "_arm_done_action(data)" in cli_src)
    check("cli Ctrl+C 取消收尾动作", cli_src.count("actions.cancel()") >= 2)
    check("cli 交互菜单有抖器", "鼠标抖器" in cli_src)

    # --- 插件注册 ---
    from core import engine as eng
    check("注册表有 jiggler", "jiggler" in eng.Registry.names())
    init_src = code_only(open(os.path.join(HERE, "plugins", "__init__.py"),
                              encoding="utf-8").read())
    check("BUILTIN_MODULES 含 jiggler（打包收得进）",
          '"jiggler"' in init_src)


# ==================================================================
# main
# ==================================================================
def main():
    import dashboard as D

    tmp = os.path.join(HERE, "_test_v23_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)

    stubs = make_stubs()
    install_stubs(D, stubs)
    api = D.Api()
    try:
        test_parse_params_v23(D)
        test_actions_unit()
        test_jiggler_plugin()
        test_s4_wiring(D, api, stubs)
        test_s5_cli()
        test_s6_wiring(D, api, stubs)
        test_static_wiring(D)
    finally:
        try:
            api.stop_mouse()
        except Exception:
            pass
        D.pausectl.set_paused(False)
        D.actions.reset()
        restore_stubs(D, stubs)
        for p in (tmp, tmp + ".bak"):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    return summary()


if __name__ == "__main__":
    sys.exit(main())
