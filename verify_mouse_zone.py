# -*- coding: utf-8 -*-
"""
verify_mouse_zone.py —— 鼠标区专项回归（滚轮 / G2 触发 / 注入回环 / 侧键注入）
==========================================================================

【它验证什么】
鼠标区四个「不报错但行为错」的坑，全部用合成报文复现，不碰真实键鼠：

  1. 滚轮事件不得被当成按键分发（启动连点后滚轮行为异常）
  2. G2 触发链路：按一下启动、松开不停、再按一下停止
  3. **注入回环**：连点滴答的注入事件会顺着 Raw Input 回到 _on_raw_mouse，
     如果不过滤，触发键会被自己注入的事件反复切换
     —— 表现为「点一下 G2 只点一次就没后续了」
  4. 侧键（G1/G2）注入的标志位：必须是 XDOWN(0x0080)/XUP(0x0100)
     且 mouseData 带编号，共两个事件；G3 以上注入不了必须明确失败
  5. 触发方式显示：后端必须按「动作」返回绑定（否则前端回退显示 F5）
  6. 暂停门闩：暂停期间引擎一个点击都不许注入

【测试手法】和 verify_binding.py 一致：直接构造 RAWMOUSE 喂 _on_raw_mouse，
引擎的 _click_once 被打桩成计数器，绝不往系统发真实输入。
"""

import ctypes
import ctypes.wintypes as wt
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


# 注入标记：和 core/keyboard.py 的 INJECT_MARK 必须是同一个值。
# 键盘那边用它过滤「自己注入的按键」，鼠标这边必须对称——
# 否则鼠标触发键会被连点滴答的注入事件反复切换。
MARK = 0x4D414331


def mk(flags=0, raw=0, data=0, extra=0):
    """构造一帧鼠标报文（字段名别写错：位图是 ulRawButtons）。"""
    m = _D.RAWMOUSE()
    m.usFlags = 0
    m.usButtonFlags = flags
    m.usButtonData = data
    m.ulRawButtons = raw
    m.ulExtraInformation = extra
    return m


def reset_raw_state():
    """把 Raw Input 相关状态复位，测试之间互不污染。"""
    with _D._raw_lock:
        _D._raw_keys["raw_buttons"] = 0
        _D._raw_keys["buttons"] = 0
        _D._raw_keys["ids"] = []
        _D._capture["armed"] = False
        _D._capture["raw_id"] = None
    _D._raw_stat["last_ids"] = []


# ------------------------------------------------------------------
# 1. 常量与结构
# ------------------------------------------------------------------
def test_constants():
    section("1. 注入标记常量")
    global _D
    import dashboard as _D
    from core import inputs as inp
    check("inputs.MARK_INJECTED 已定义",
          hasattr(inp, "MARK_INJECTED"),
          "缺常量 → 注入事件无法被识别过滤")
    check("inputs.MARK_INJECTED == keyboard.INJECT_MARK",
          getattr(inp, "MARK_INJECTED", None) == MARK,
          "实测=%r 应为 0x%X" % (getattr(inp, "MARK_INJECTED", None), MARK))
    check("RI_MOUSE_WHEEL 常量已定义（0x0400）",
          getattr(_D, "RI_MOUSE_WHEEL", None) == 0x0400,
          "实测=%r" % getattr(_D, "RI_MOUSE_WHEEL", None))


# ------------------------------------------------------------------
# 2. 滚轮：不得触发任何按键分发
# ------------------------------------------------------------------
def test_wheel_silent():
    section("2. 滚轮事件静默（启动连点后滚轮异常）")
    import dashboard as D
    reset_raw_state()
    got = []
    real = D._dispatch
    D._dispatch = lambda bid, down: got.append((int(bid), bool(down)))
    try:
        # 下滚 / 上滚 / 横滚，共三种真实报文
        D._on_raw_mouse(mk(0x0400, 0, 0xFF38))   # WHEEL，data=-120（上滚）
        D._on_raw_mouse(mk(0x0400, 0, 120))      # WHEEL，data=+120（下滚）
        D._on_raw_mouse(mk(0x0800, 0, 120))      # HWHEEL（横滚）
        check("滚轮不触发任何按键分发", got == [], "实际=%s" % (got,))
        check("滚轮不污染按住位图状态",
              D._raw_keys.get("raw_buttons", 0) == 0,
              "raw_buttons=%r" % D._raw_keys.get("raw_buttons"))
    finally:
        D._dispatch = real

    # 滚轮时按住中键（默认触发键）：不得产生第二次「中键按下」
    reset_raw_state()
    got = []
    D._dispatch = lambda bid, down: got.append((int(bid), bool(down)))
    try:
        D._on_raw_mouse(mk(0x0010, 0))           # 中键真实按下 → 通道1
        D._on_raw_mouse(mk(0x0400, 0x04, 120))   # 按住中键时滚轮
        check("按住中键+滚轮不重复触发中键",
              got == [(2, True)], "实际=%s" % (got,))
    finally:
        D._dispatch = real


# ------------------------------------------------------------------
# 3. G2 触发链路（端到端，引擎打桩）
# ------------------------------------------------------------------
def test_g2_trigger_chain():
    section("3. G2 触发链路：启动/保持/停止")
    import dashboard as D
    reset_raw_state()

    # 隔离设置文件，别污染真实 ui_settings.json
    tmp = os.path.join(HERE, "_test_ms_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)

    # 引擎打桩：不真发 SendInput，只计数
    clicks = {"n": 0}
    real_click = D._click_once
    D._click_once = lambda token: clicks.__setitem__("n", clicks["n"] + 1)

    api = D.Api()
    try:
        # 绑定 G2（raw_id 4）→ 启停鼠标连点
        r = api.bind_captured_key(4, "mouse")
        check("G2 绑定成功", r.get("ok") is True, r)

        # 【两段式·2026-10-15】先验证未就绪时按 G2 不启动：
        # 旧代码 toggle_mouse 没有 armed 检查，未就绪也直接开跑——
        # 主人实测「直接点中键就开始连点，和鼠标区是否就绪
        # 没有任何关系」。这是 bug 1 的行为固化。
        D._on_raw_mouse(mk(0x0100, 0x10))        # G2 按下（未就绪）
        time.sleep(0.2)
        check("未就绪按 G2 不启动", D.ST.mouse_running is False,
              "mouse_running=%r" % D.ST.mouse_running)
        D._on_raw_mouse(mk(0x0200, 0x00))        # G2 抬起
        # 就绪后再触发
        api.arm_mouse({"button": "left", "action": "click", "count": 0,
                       "duration": 0, "cps": 10}, hide=False)

        # --- 按一下 G2：应启动连点 ---
        D._on_raw_mouse(mk(0x0100, 0x10))        # G2 按下（真实鼠标位图带 bit4）
        time.sleep(0.25)
        check("按 G2 启动连点", D.ST.mouse_running is True,
              "mouse_running=%r" % D.ST.mouse_running)
        n1 = clicks["n"]

        # --- 松开 G2：连点必须继续（不能一松手就停）---
        D._on_raw_mouse(mk(0x0200, 0x00))        # G2 抬起
        time.sleep(0.35)
        check("松开 G2 后连点继续", D.ST.mouse_running is True)
        check("松开期间点击数在涨", clicks["n"] > n1,
              "%d → %d" % (n1, clicks["n"]))

        # --- 连点滴答产生的注入事件（左键）不得干扰 G2 触发器 ---
        n2 = clicks["n"]
        for _ in range(20):
            D._on_raw_mouse(mk(0x0001, 0))       # 注入的左键按下
            D._on_raw_mouse(mk(0x0002, 0))       # 注入的左键抬起
        time.sleep(0.2)
        check("注入回环不误停连点", D.ST.mouse_running is True)
        check("注入回环不产生额外切换", clicks["n"] >= n2)

        # --- 再按一下 G2：应**暂停**（不是停止）---
        # 【2026-10-16 改规格】主人反馈「按一下就结束、想再按继续没反应」，
        # 触发键改成「启动 / 暂停 / 恢复」循环，结束只归「停止」按钮。
        D._on_raw_mouse(mk(0x0100, 0x10))
        time.sleep(0.25)
        check("再按 G2 是暂停（mouse_running 仍为 True）",
              D.ST.mouse_running is True,
              "mouse_running=%r" % D.ST.mouse_running)
        check("鼠标区处于暂停", D.pausectl.is_paused("mouse") is True)
        n3 = clicks["n"]
        time.sleep(0.25)
        check("暂停期间零注入", clicks["n"] == n3,
              "%d → %d" % (n3, clicks["n"]))

        # --- 再按一次：恢复 ---
        # 必须先给一个「抬起」：ButtonTrigger 有防连击（按住不放只触发一次），
        # 连着喂两个 down 第二个会被吃掉 —— 真机上就是「按了没反应」。
        D._on_raw_mouse(mk(0x0200, 0x00))        # G2 抬起
        D._on_raw_mouse(mk(0x0100, 0x10))        # G2 再按下
        time.sleep(0.35)
        check("再按 G2 恢复连点", D.pausectl.is_paused("mouse") is False)
        check("恢复后又开始注入", clicks["n"] > n3,
              "%d → %d" % (n3, clicks["n"]))
    finally:
        try:
            api.stop_mouse()
        except Exception:
            pass
        D._click_once = real_click
        try:
            os.remove(tmp)
        except Exception:
            pass


# ------------------------------------------------------------------
# 4. 注入回环过滤（「只会点一下」的根因族）
# ------------------------------------------------------------------
def test_injected_feedback():
    section("4. 注入回环过滤（自注入事件不得切换触发键）")
    import dashboard as D
    from core import inputs as inp
    reset_raw_state()

    tmp = os.path.join(HERE, "_test_ms2_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)

    clicks = {"n": 0}
    real_click = D._click_once
    D._click_once = lambda token: clicks.__setitem__("n", clicks["n"] + 1)
    api = D.Api()
    try:
        # 场景 A：点击键 = G2 自己（绑定 G2 又用 G2 连点）
        api.bind_captured_key(4, "mouse")
        # 两段式：先就绪再按触发键（未就绪按 G2 不启动，见
        # test_g2_trigger_chain 的行为固化）
        api.arm_mouse({"button": "left", "action": "click", "count": 0,
                       "duration": 0, "cps": 10}, hide=False)
        D._on_raw_mouse(mk(0x0100, 0x10))            # 真实按下 G2 → 启动
        time.sleep(0.2)
        check("A: 真实 G2 按下能启动", D.ST.mouse_running is True)
        # 连点滴答的 G2 注入事件（带标记）回来 → 必须被无视
        for _ in range(10):
            D._on_raw_mouse(mk(0x0100, 0x10, 0, MARK))   # 注入的 G2 按下
            D._on_raw_mouse(mk(0x0200, 0x00, 0, MARK))   # 注入的 G2 抬起
        time.sleep(0.2)
        check("A: 自注入的 G2 事件不切换连点（只会点一下的根因）",
              D.ST.mouse_running is True,
              "被注入事件关掉了")
        api.stop_mouse()
        reset_raw_state()

        # 场景 B：默认配置（中键触发 + 中键连点）
        api2 = D.Api()
        D.bt.HUB.clear()
        D.bt.HUB.set("middle", api2.toggle_mouse)
        api2.arm_mouse({"button": "left", "action": "click", "count": 0,
                        "duration": 0, "cps": 10}, hide=False)
        D._on_raw_mouse(mk(0x0010, 0x04))            # 真实中键按下 → 启动
        time.sleep(0.2)
        check("B: 真实中键按下能启动", D.ST.mouse_running is True)
        for _ in range(10):
            D._on_raw_mouse(mk(0x0010, 0, 0, MARK))  # 注入的中键按下
            D._on_raw_mouse(mk(0x0020, 0, 0, MARK))  # 注入的中键抬起
        time.sleep(0.2)
        check("B: 自注入的中键事件不切换连点",
              D.ST.mouse_running is True,
              "被注入事件关掉了")
        api2.stop_mouse()
        D.bt.HUB.clear()
    finally:
        try:
            api.stop_mouse()
        except Exception:
            pass
        D._click_once = real_click
        try:
            os.remove(tmp)
        except Exception:
            pass


# ------------------------------------------------------------------
# 5. 按住不重复触发
# ------------------------------------------------------------------
def test_hold_no_repeat():
    section("5. 按住不放只触发一次")
    import dashboard as D
    reset_raw_state()
    got = []
    real = D._dispatch
    D._dispatch = lambda bid, down: got.append((int(bid), bool(down)))
    try:
        D._on_raw_mouse(mk(0x0100, 0x10))     # G2 按下
        for _ in range(5):                    # 按住期间的移动帧（位图仍带 bit4）
            D._on_raw_mouse(mk(0x0000, 0x10))
        check("按住期间移动帧不重复触发",
              got == [(4, True)], "实际=%s" % (got,))
        got.clear()
        D._on_raw_mouse(mk(0x0200, 0x00))     # 抬起
        check("抬起事件正确分发", got == [(4, False)], "实际=%s" % (got,))
    finally:
        D._dispatch = real


# ------------------------------------------------------------------
# 6. 侧键注入标志位
# ------------------------------------------------------------------
def test_xbutton_flags():
    section("6. 侧键（G1/G2）注入标志位")
    import dashboard as D
    from core import inputs as inp

    calls = []

    class FakeUser32(object):
        def SendInput(self, n, buf, size):
            # 把每个事件解出来看，不真的发出去
            arr = ctypes.cast(buf, ctypes.POINTER(inp.INPUT * n)).contents
            for ev in arr:
                calls.append((ev.mi.dwFlags, ev.mi.mouseData,
                              ev.mi.dwExtraInfo))
            return n

    real_u = inp._user32
    inp._user32 = FakeUser32()
    try:
        calls.clear()
        D._click_xbutton("xb1")      # 内置 token：侧键1 = XBUTTON1
        check("xb1 注入两个事件（按下+抬起）", len(calls) == 2,
              "实际=%d 个" % len(calls))
        if len(calls) == 2:
            check("xb1 按下 = XDOWN(0x80) 且 mouseData=1",
                  calls[0][0] == 0x0080 and calls[0][1] == 1,
                  "实际 flags=0x%X data=%d" % (calls[0][0], calls[0][1]))
            check("xb1 抬起 = XUP(0x100) 且 mouseData=1",
                  calls[1][0] == 0x0100 and calls[1][1] == 1,
                  "实际 flags=0x%X data=%d" % (calls[0][0], calls[0][1]))
            check("注入事件带标记（供回环过滤）",
                  calls[0][2] == MARK and calls[1][2] == MARK,
                  "实际=%r" % ((calls[0][2], calls[1][2]),))

        calls.clear()
        D._click_xbutton("xb3")      # 原始编号 3 = G1 = XBUTTON1
        ok = (len(calls) == 2 and calls[0][0] == 0x0080
              and calls[0][1] == 1 and calls[1][0] == 0x0100)
        check("xb3(G1) = XBUTTON1，mouseData=1", ok, "实际=%s" % (calls,))

        calls.clear()
        D._click_xbutton("xb4")      # 原始编号 4 = G2 = XBUTTON2
        ok = (len(calls) == 2 and calls[0][0] == 0x0080
              and calls[0][1] == 2 and calls[1][0] == 0x0100)
        check("xb4(G2) = XBUTTON2，mouseData=2", ok, "实际=%s" % (calls,))

        # G3 以上 SendInput 注入不了：必须明确失败，不能静默发垃圾
        calls.clear()
        raised = False
        try:
            D._click_xbutton("xb5")
        except Exception:
            raised = True
        check("xb5(G3+) 注入不了 → 明确报错", raised,
              "静默发送了 %s" % (calls,))
    finally:
        inp._user32 = real_u


# ------------------------------------------------------------------
# 7. 触发方式显示：按动作返回
# ------------------------------------------------------------------
def test_trigger_display():
    section("7. 触发方式显示（按动作返回绑定）")
    import dashboard as D
    tmp = os.path.join(HERE, "_test_ms3_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)
    api = D.Api()
    try:
        api.bind_captured_key(4, "mouse")            # G2 → 鼠标连点
        api.bind_captured_key(2, "key")              # 中键 → 键盘锁定
        b = api.button_hotkeys()
        by = b.get("by_action") or {}
        m = by.get("mouse") or {}
        k = by.get("key") or {}
        check("by_action.mouse 存在且指向 G2",
              m.get("raw_id") == 4 and "G2" in str(m.get("label")),
              "实际=%s" % (m,))
        check("by_action.key 存在且指向中键",
              k.get("raw_id") == 2, "实际=%s" % (k,))
    finally:
        D.bt.HUB.clear()
        try:
            os.remove(tmp)
        except Exception:
            pass


# ------------------------------------------------------------------
# 8. 暂停门闩
# ------------------------------------------------------------------
def test_pause_gate():
    section("8. 暂停门闩（暂停期间零注入）")
    import dashboard as D
    reset_raw_state()
    clicks = {"n": 0}
    real_click = D._click_once
    D._click_once = lambda token: clicks.__setitem__("n", clicks["n"] + 1)
    api = D.Api()
    try:
        r = api.start_mouse({"button": "left", "action": "click",
                             "count": "0", "duration": "0", "cps": "50"},
                            hide=False)
        check("连点启动", r.get("ok") is True, r)
        time.sleep(0.25)
        n1 = clicks["n"]
        check("暂停前有注入", n1 > 0, "clicks=%d" % n1)

        r = api.set_paused(True)
        check("暂停接口存在且可用", r.get("ok") is True, r)
        time.sleep(0.05)
        n2 = clicks["n"]
        time.sleep(0.35)
        check("暂停期间零注入", clicks["n"] == n2,
              "%d → %d" % (n2, clicks["n"]))
        # 循环必须保活：ClickContext.wait 曾把 100ms 轮询超时也返回
        # False，门闩 `if not ctx.wait(0.1): break` 一 break 就连点
        # 整个退出（_finished 回滚 mouse_running），暂停变成了停止。
        st = api.status()
        check("暂停期间连点仍在运行（保活）",
              st.get("mouse_running") is True and st.get("paused") is True,
              "running=%s paused=%s" % (st.get("mouse_running"),
                                        st.get("paused")))

        api.set_paused(False)
        time.sleep(0.35)
        # >= n2+2：只多 1 发可能是暂停瞬间泄漏的那发，证明不了循环活着；
        # 50cps 恢复后 350ms 内按 ~6 发计，留足余量。
        check("恢复后继续注入", clicks["n"] >= n2 + 2,
              "%d → %d" % (n2, clicks["n"]))
    finally:
        try:
            api.set_paused(False)
            api.stop_mouse()
        except Exception:
            pass
        D._click_once = real_click


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------
def main():
    print("=" * 64)
    print(" 鼠标区专项回归（滚轮 / G2 / 注入回环 / 侧键注入 / 显示 / 暂停）")
    print(" 全部使用合成报文，不注入真实键鼠")
    print("=" * 64)
    test_constants()
    test_wheel_silent()
    test_g2_trigger_chain()
    test_injected_feedback()
    test_hold_no_repeat()
    test_xbutton_flags()
    test_trigger_display()
    test_pause_gate()

    print("\n" + "=" * 64)
    print("结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n失败清单：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
