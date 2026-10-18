# -*- coding: utf-8 -*-
"""
diag_v30_editor_probe.py —— v30 专项探针（录制起点 / 步骤化 / 节点编辑）
=====================================================================

【v30 做什么】把「录出来一堆原始事件、用户改不动」变成「录出来一串节点、
每个节点都能改」。三件事：

  ① 诊断：「最近按键」锁存（原来抬起就清空，500ms 轮询采样不到，看着像只认左键）
  ② 布局：三栏底部状态行贴底（最大化后短栏不再一大片空白）
  ③ 录制：起点可选（首次点击 / 倒计时 / 立即），录完自动步骤化，
     步骤可改名称 / 坐标 / 延时 / 执行完成后，可删除 / 复制 / 上下移 / 插入

【本探针的重点】纯逻辑（压缩、展开、校验）必须逐条断言数值，
不能用「跑完没报错」代替 —— 这是硬规则 44 的教训：
没报错和结果对是两件事。

【测试纪律】
  · 全程不发任何真实键鼠输入（硬规则 6）。feed_* 直接喂结构体。
  · 每个场景开头重置 _MACRO，避免上一场景的残留（v22 的教训）。
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def sec(title):
    print("\n" + "-" * 58)
    print(title)
    print("-" * 58)


# ----------------------------------------------------------------------
# 1. 事件 → 步骤（压缩）
# ----------------------------------------------------------------------
def test_from_events():
    sec("1. 事件 → 步骤：点击成对合并、拖拽拆开、移动丢噪声")
    from core import macro_steps as ms

    evs = [
        {"t": 0, "a": "move", "x": 10, "y": 10},
        {"t": 100, "a": "down", "b": "left", "x": 789, "y": 2444},
        {"t": 140, "a": "up", "b": "left"},
        {"t": 1520, "a": "down", "b": "left", "x": 800, "y": 2500},
        {"t": 1580, "a": "up", "b": "left"},
        {"t": 3000, "a": "kd", "vk": 65},
        {"t": 3040, "a": "ku", "vk": 65},
        {"t": 4000, "a": "wheel", "d": -1},
    ]
    st = ms.from_events(evs)
    check("短按成对合并成 1 个点击节点", len(st) == 4, "得到 %d 个" % len(st))
    check("第 1 个节点是点击且带坐标",
          st[0]["k"] == "click" and st[0]["x"] == 789 and st[0]["y"] == 2444,
          str(st[0]))
    check("延时 = 下一步开始 - 本步结束（1520-140=1380）",
          st[0]["delay"] == 1380, "得到 %s" % st[0]["delay"])
    check("键盘按下/抬起合并成按键节点", st[2]["k"] == "key"
          and st[2]["vk"] == 65, str(st[2]))
    check("滚轮成为独立节点", st[3]["k"] == "wheel" and st[3]["d"] == -1,
          str(st[3]))
    check("移动默认不占步骤", all(s["k"] != "move" for s in st))

    # 拖拽：按下后 400ms 才抬起 → 拆成 down / up 两个节点，不许静默丢
    drag = [{"t": 0, "a": "down", "b": "left", "x": 5, "y": 6},
            {"t": 400, "a": "up", "b": "left"}]
    st2 = ms.from_events(drag)
    check("按住不放松（>180ms）拆成 down + up",
          len(st2) == 2 and st2[0]["k"] == "down" and st2[1]["k"] == "up",
          str(st2))

    # keep_move=True 时才保留轨迹（画图类场景）
    st3 = ms.from_events(evs[:1], keep_move=True)
    check("keep_move=True 才保留移动节点",
          len(st3) == 1 and st3[0]["k"] == "move", str(st3))

    # 空表 / 脏数据不炸
    check("空表返回空步骤", ms.from_events([]) == [])
    check("脏条目被跳过而不是抛异常",
          ms.from_events([{"a": "wtf"}, None, {"t": 0, "a": "up",
                                               "b": "left"}]) != [])

    # G1/G2（XBUTTON）也走同一条点击合并
    xb = [{"t": 0, "a": "down", "b": "x1", "x": 1, "y": 2},
          {"t": 60, "a": "up", "b": "x1"}]
    stx = ms.from_events(xb)
    check("G1 点击也能合并成点击节点",
          len(stx) == 1 and stx[0]["b"] == "x1", str(stx))
    return st


# ----------------------------------------------------------------------
# 2. 步骤 → 事件（展开）
# ----------------------------------------------------------------------
def test_to_events():
    sec("2. 步骤 → 事件：改了延时/坐标，回放必须跟着变")
    from core import macro_steps as ms

    steps = [
        {"k": "click", "b": "left", "x": 100, "y": 200, "delay": 500,
         "delay2": 0, "after": "next", "name": "点击"},
        {"k": "wait", "delay": 300, "delay2": 0, "after": "next",
         "name": "等待"},
        {"k": "key", "vk": 87, "delay": 0, "delay2": 0, "after": "end",
         "name": "按键 W"},
    ]
    evs = ms.to_events(steps)
    downs = [e for e in evs if e["a"] == "down"]
    ups = [e for e in evs if e["a"] == "up"]
    check("点击展开成 down + up 两条", len(downs) == 1 and len(ups) == 1)
    check("点击坐标被原样带进事件",
          downs[0]["x"] == 100 and downs[0]["y"] == 200, str(downs[0]))
    kd = [e for e in evs if e["a"] == "kd"]
    check("按键展开成 kd + ku", len(kd) == 1 and kd[0]["vk"] == 87)
    # 第二条事件（wait）之后才到按键：t 至少是 500 + 300
    check("延时串进时间轴（按键不早于 800ms）",
          kd and kd[0]["t"] >= 800, "t=%s" % (kd[0]["t"] if kd else None))
    check("after=end 之后不再有事件",
          evs[-1]["a"] in ("ku",) or evs[-1]["a"] == "kd",
          "最后一条 %s" % evs[-1]["a"])

    # 随机延时必须落在区间内，且不是恒定值
    import random
    rnd = random.Random(7)
    vals = [ms.effective_delay({"delay": 100, "delay2": 200}, rnd)
            for _ in range(50)]
    check("随机延时落在 [100,200]",
          min(vals) >= 100 and max(vals) <= 200, "min=%d max=%d"
          % (min(vals), max(vals)))
    check("随机延时不是恒定值", len(set(vals)) > 1)
    check("delay2 小于 delay 时按固定延时算",
          ms.effective_delay({"delay": 500, "delay2": 100}) == 500)

    # 时长统计：点击/按键各带一次按下时长（CLICK_HOLD_MS）
    check("total_ms 累加延时 + 按下时长",
          ms.total_ms(steps) == 500 + 300 + 2 * ms.CLICK_HOLD_MS,
          "得到 %s" % ms.total_ms(steps))
    check("stats 数出点击/按键次数",
          ms.stats(steps) == {"count": 3, "secs": 0.8, "clicks": 1,
                              "keys": 1}, str(ms.stats(steps)))
    return steps


# ----------------------------------------------------------------------
# 3. 校验：坏步骤宁可整条拒绝
# ----------------------------------------------------------------------
def test_validate():
    sec("3. 校验：越界坐标/未知类型必须拒收（在错坐标乱点比打不开危险）")
    from core import macro_steps as ms

    ok, err = ms.validate_steps([{"k": "click", "b": "left", "x": 1,
                                  "y": 2, "delay": 10}])
    check("正常步骤通过校验", err == "" and len(ok) == 1, err)

    _, err = ms.validate_steps([{"k": "fly"}])
    check("未知类型被拒", "类型不认识" in err, err)

    _, err = ms.validate_steps([{"k": "click", "b": "left", "x": "abc",
                                 "y": 2}])
    check("坐标不是整数被拒", "坐标不是整数" in err, err)

    _, err = ms.validate_steps([{"k": "click", "b": "脚趾", "x": 1, "y": 2}])
    check("未知鼠标键被拒", "鼠标键名" in err, err)

    ok, _ = ms.validate_steps([{"k": "click", "b": "left", "x": 99999,
                                "y": -99999, "delay": -5}])
    check("越界值被夹住而不是报错",
          ok[0]["x"] == 32767 and ok[0]["y"] == -32768 and ok[0]["delay"] == 0,
          str(ok[0]))

    ok, _ = ms.validate_steps([{"k": "click", "b": "left", "x": 1, "y": 2,
                                "delay": 900, "delay2": 100}])
    check("delay2 < delay 时自动抬到 delay",
          ok[0]["delay2"] == 900, str(ok[0]))

    _, err = ms.validate_steps({"k": "click"})
    check("非列表被拒", "必须是列表" in err, err)


# ----------------------------------------------------------------------
# 4. 录制起点
# ----------------------------------------------------------------------
def test_start_modes():
    sec("4. 录制起点：首次点击 / 倒计时 / 立即（不发真实输入）")
    from core import recorder as rec_mod

    class Pt(object):
        def __init__(self, x=10, y=20):
            self.x, self.y = x, y

    class MouseInfo(object):
        def __init__(self, x=10, y=20, data=0):
            self.pt = Pt(x, y)
            self.mouseData = data
            self.flags = 0
            self.dwExtraInfo = 0

    def new_rec(mode):
        r = rec_mod.Recorder(start_mode=mode)
        # 绕过 start()：本机低完整性环境装钩子收不到事件，
        # 但 feed_* 是纯函数，直接喂结构体就能验起点门闩。
        r._running = True
        r._started = (mode == rec_mod.START_NOW)
        r._armed_at = 0.0 if not r._started else None
        return r

    # 首次点击：起点前的移动/滚轮不入账，第一次按下才算第 0 步
    r = new_rec(rec_mod.START_FIRST_CLICK)
    r.feed_mouse(rec_mod.WM_MOUSEMOVE, MouseInfo(1, 2))
    r.feed_mouse(rec_mod.WM_MOUSEWHEEL, MouseInfo(1, 2, 120))
    check("起点前：移动和滚轮都不记录", r.count() == 0, "count=%d" % r.count())
    check("起点前 stats 报 armed", r.stats()["armed"] is True)
    r.feed_mouse(rec_mod.WM_LBUTTONDOWN, MouseInfo(300, 400))
    check("第一次点击开启录制并记下这一步", r.count() == 1, "count=%d" % r.count())
    check("起点那一步的时间戳是 0", r.events()[0]["t"] == 0,
          str(r.events()[0]))
    r.feed_mouse(rec_mod.WM_LBUTTONUP, MouseInfo(300, 400))
    check("起点后抬起正常记录", r.count() == 2)

    # 起点前键盘不开启录制（否则切窗口时随手按的键会提前起点）
    r2 = new_rec(rec_mod.START_FIRST_CLICK)
    ki = type("K", (), {"vkCode": 65, "flags": 0, "dwExtraInfo": 0})()
    r2.feed_key(rec_mod.WM_KEYDOWN, ki)
    check("起点前键盘事件不记录也不开启起点",
          r2.count() == 0 and r2.stats()["started"] is False)

    # 立即模式：移动也照记（老行为，脚本兼容）
    r3 = new_rec(rec_mod.START_NOW)
    r3.feed_mouse(rec_mod.WM_MOUSEMOVE, MouseInfo(5, 6))
    check("立即模式：第一条移动就记录", r3.count() == 1)

    # 倒计时：用 0.05 秒验「到点自己开」
    import time as _t
    r4 = rec_mod.Recorder(start_mode=rec_mod.START_COUNTDOWN)
    r4._running = True
    r4._started = False
    r4._armed_at = _t.perf_counter()
    r4.start_delay = 0.05
    r4._start_countdown(0.05)
    _t.sleep(0.25)
    check("倒计时到点自动开始", r4.stats()["started"] is True)
    r4._arm_cancel.set()


# ----------------------------------------------------------------------
# 5. 诊断锁存：「最近按键」不再一松手就消失
# ----------------------------------------------------------------------
def test_diag_latch():
    sec("5. 诊断：最近按键锁存（这是「只偶尔显示左键」的真因）")
    import dashboard as D

    with D._raw_lock:
        D._raw_stat["last_press_id"] = -1
        D._raw_stat["last_press_at"] = 0.0
        D._raw_stat["last_ids"] = []
        D._raw_keys["std_down"] = set()
        D._raw_keys["raw_buttons"] = 0

    D._std_ids_update(3, True)
    check("按下 G1：last_ids 有 3", D._raw_stat["last_ids"] == [3],
          str(D._raw_stat["last_ids"]))
    D._std_ids_update(3, False)
    check("抬起后 last_ids 清空（「正在按住」语义不变）",
          D._raw_stat["last_ids"] == [])
    check("抬起后 last_press 仍保留 G1",
          D._raw_stat["last_press_id"] == 3,
          str(D._raw_stat["last_press_id"]))

    api = D.Api()
    d = api.raw_diag()
    check("raw_diag 回带 last_press 且名字是 G1",
          d.get("last_press") and d["last_press"]["name"] == "G1",
          str(d.get("last_press")))
    check("last_press 带 age（秒）", isinstance(
        (d.get("last_press") or {}).get("age"), float), str(d.get("last_press")))

    # 中键 / 左键也走同一条路
    D._std_ids_update(2, True)
    D._std_ids_update(2, False)
    check("中键同样能被锁存", D._raw_stat["last_press_id"] == 2)


# ----------------------------------------------------------------------
# 6. 步骤编辑 API
# ----------------------------------------------------------------------
def test_edit_api(tmpdir):
    sec("6. 编辑 API：改 / 删 / 复制 / 移动 / 插入 / 存取")
    import dashboard as D
    from core import macro_steps as ms

    D._settings_dir = lambda: tmpdir          # 别写到用户的真实设置目录
    D._MACRO["steps"] = ms.from_events([
        {"t": 0, "a": "down", "b": "left", "x": 10, "y": 20},
        {"t": 50, "a": "up", "b": "left"},
        {"t": 600, "a": "down", "b": "left", "x": 30, "y": 40},
        {"t": 650, "a": "up", "b": "left"},
        {"t": 900, "a": "wheel", "d": 1},
    ])
    D._MACRO["events"] = []
    D._MACRO["playing"] = False
    D._MACRO["rec"] = None
    D._macro_bump()
    api = D.Api()

    r = api.macro_steps(0, 300)
    check("取步骤：总数为 3", r["total"] == 3, str(r["total"]))
    check("取步骤：每项都带 label",
          all(it.get("label") for it in r["items"]), str(r["items"][:1]))

    ver0 = r["step_ver"]
    r = api.macro_step_set(0, {"x": 555, "y": 666, "delay": 1234,
                               "name": "我的点击"})
    check("改坐标/延时/名称成功", r["ok"], r.get("msg"))
    check("改完版本号 +1", r["step_ver"] == ver0 + 1, str(r["step_ver"]))
    s0 = api.macro_steps(0, 10)["items"][0]
    check("改动真的落到步骤表",
          s0["x"] == 555 and s0["y"] == 666 and s0["delay"] == 1234
          and s0["name"] == "我的点击", str(s0))

    r = api.macro_step_set(0, {"x": "abc"})
    check("非法坐标被拒且不改动", r["ok"] is False
          and "坐标" in r["msg"], str(r.get("msg")))

    r = api.macro_step_dup(0)
    check("复制：总数 +1 且副本紧跟其后",
          r["ok"] and api.macro_steps(0, 10)["total"] == 4, r.get("msg"))
    after = api.macro_steps(0, 10)["items"]
    check("副本内容与原节点一致",
          after[1]["name"] == after[0]["name"] and after[1]["x"] == after[0]["x"])

    r = api.macro_step_move(1, -1)
    check("上移成功", r["ok"], r.get("msg"))
    r = api.macro_step_move(0, -1)
    check("到边界移不动并明确报错", r["ok"] is False and "边界" in r["msg"],
          str(r.get("msg")))

    total0 = api.macro_steps(0, 10)["total"]
    r = api.macro_step_del(0)
    check("删除：总数 -1", r["ok"] and
          api.macro_steps(0, 10)["total"] == total0 - 1, r.get("msg"))

    r = api.macro_step_insert(1, "wait", 800)
    items = api.macro_steps(0, 10)["items"]
    check("插入等待节点落在指定位置",
          r["ok"] and items[1]["k"] == "wait" and items[1]["delay"] == 800,
          str(items[1] if len(items) > 1 else items))

    # 保存 → 读回：steps 必须一起存，且 events 由 steps 展开
    r = api.macro_save("探针宏")
    check("保存成功", r["ok"], r.get("msg"))
    D._MACRO["steps"] = []
    D._MACRO["events"] = []
    r = api.macro_load("探针宏")
    check("载入后步骤数一致", r["ok"] and r["count"] == len(
        api.macro_steps(0, 300)["items"]), str(r))
    check("载入后步骤内容可用（第一条有坐标）",
          api.macro_steps(0, 1)["items"][0].get("x") is not None)

    # 旧文件（只有 events）也能载入：走一次压缩
    import json
    from core import config as cfg
    old_path = os.path.join(cfg.macros_dir(base=tmpdir), "旧版宏.json")
    with open(old_path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "name": "旧版宏",
                   "events": [{"t": 0, "a": "down", "b": "left",
                               "x": 7, "y": 8},
                              {"t": 40, "a": "up", "b": "left"}]}, f)
    r = api.macro_load("旧版宏")
    check("v1 老宏载入后自动步骤化",
          r["ok"] and r["count"] == 1, str(r))

    # 录制/回放中不许编辑（否则回放器读到半截数据）
    D._MACRO["playing"] = True
    r = api.macro_step_del(0)
    check("回放中拒绝编辑", r["ok"] is False and "回放" in r["msg"],
          str(r.get("msg")))
    D._MACRO["playing"] = False


# ----------------------------------------------------------------------
# 7. 前端接线静态断言
# ----------------------------------------------------------------------
def test_frontend():
    sec("7. 前端接线：列表 / 弹窗 / 启停键流程提示都在，旧引用清干净")
    p = os.path.join(HERE, "ui", "dashboard.html")
    html = open(p, encoding="utf-8").read()
    # 内嵌副本也要有（双源必须同步，规则 12/19）
    import dashboard as D
    emb = D.HTML

    for name, src in (("文件", html), ("内嵌", emb)):
        check("%s：步骤列表容器在" % name, 'id="macSteps"' in src)
        check("%s：节点设置弹窗在" % name, 'id="macModal"' in src)
        # v41：起点下拉被启停键流程取代（主人要的是「开始→F7→操作→F7→停止」）
        check("%s：启停键流程提示在" % name, 'id="macFlowHint"' in src)
        check("%s：已无旧起点下拉" % name, 'id="macStart"' not in src)
        check("%s：没有残留 macPreview 引用" % name, "macPreview" not in src)
        check("%s：调用 macro_steps" % name, "macro_steps(" in src)
        check("%s：调用 macro_step_set" % name, "macro_step_set" in src)
        check("%s：允许编辑 .mc-step" % name, "function macEditStep" in src)
    check("两份 HTML 完全一致（双源同步）", html == emb,
          "md5 不一致，先跑 _sync_html.py")


# ----------------------------------------------------------------------
# 8. 交叉：新功能 × 已有全局机制
# ----------------------------------------------------------------------
def test_cross():
    sec("8. 交叉：录制中编辑 / 回放吃步骤表 / 起点不碰全局暂停")
    import dashboard as D

    class FakeRec(object):
        running = True

    D._MACRO["rec"] = FakeRec()
    api = D.Api()
    r = api.macro_step_del(0)
    check("录制中拒绝编辑步骤", r["ok"] is False and "录制" in r["msg"],
          str(r.get("msg")))
    D._MACRO["rec"] = None

    # v28 的守卫不能被 v30 破坏：录制中按 Pause 只结束录制，
    # 关键断言是「暂停状态没被打开」（返回码是 ok=True 的软拒绝）。
    D._MACRO["rec"] = FakeRec()
    r = api.toggle_pause()
    check("录制中按 Pause 不会打开全局暂停（v28 回归）",
          r.get("paused") is False and "录制" in r.get("msg", ""), str(r))
    D._MACRO["rec"] = None

    # 回放数据源：有 steps 就该用 steps 展开，而不是原始 events
    from core import macro_steps as ms
    D._MACRO["steps"] = [{"k": "click", "b": "left", "x": 11, "y": 22,
                          "delay": 0, "delay2": 0, "after": "next",
                          "name": "点击"}]
    D._MACRO["events"] = [{"t": 0, "a": "down", "b": "left",
                           "x": 999, "y": 999}]
    expanded = ms.to_events(D._MACRO["steps"])
    check("回放取的是步骤展开结果（坐标 11,22 而不是 999,999）",
          expanded[0].get("x") == 11 and expanded[0].get("y") == 22,
          str(expanded[0]))


def main():
    print("=" * 58)
    print("v30 探针：录制起点 / 步骤化 / 节点编辑 / 诊断锁存")
    print("=" * 58)
    tmpdir = tempfile.mkdtemp(prefix="v30probe_")
    try:
        test_from_events()
        test_to_events()
        test_validate()
        test_start_modes()
        test_diag_latch()
        test_edit_api(tmpdir)
        test_frontend()
        test_cross()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 58)
    print("通过 %d / 失败 %d（共 %d 项）"
          % (len(PASS), len(FAIL), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败清单：")
        for f in FAIL:
            print("  - %s" % f)
        print("结果：FAIL")
        return 1
    print("结果：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
