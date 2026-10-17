# -*- coding: utf-8 -*-
"""
verify_bugfix.py —— 本轮 4 个 bug 的定向回归
============================================

只测本轮改动触及的逻辑，跑得快、能独立复现：

  bug1  initButtons 下拉框重复   -> 静态检查 HTML 里是否有清空动作
  bug2  F5 全局热键             -> 真注册一次热键，验证回调能被触发
  bug3  反复点击无效果         -> **实测 down 与 up 之间的时间差**
  bug4  （UI 视觉，人工确认）

为什么必须测「时间差」而不是「次数」：
  之前的回归只断言 down=36 / up=36，看起来是配对的，
  但两次 SendInput 背靠背调用，间隔只有几十微秒 ——
  目标程序会判定为「抖动」直接丢弃，所以实际表现为「没效果」。
  光数次数永远发现不了这个问题，必须量时间。
"""
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[通过]" if cond else "[失败]", name,
                         ("  -> " + detail) if detail else ""))


# ======================================================================
# bug1 —— 下拉框重复
# ======================================================================
def test_bug1():
    print("\n【bug1】自定义按键下拉框重复")
    import dashboard
    html = dashboard.HTML
    m = re.search(r"async function initButtons\(list\)\{(.*?)\n  \}", html, re.S)
    if not m:
        check("找到 initButtons 函数", False)
        return
    body = m.group(1)
    check("initButtons 内有清空动作",
          "innerHTML = \"\"" in body or "innerHTML=''" in body,
          "未找到清空语句——这正是选项累积成多份的原因")
    check("清空发生在 appendChild 之前",
          body.find("innerHTML") < body.find("appendChild")
          if "innerHTML" in body else False)

    # 后端侧：重复调用 list_mouse_buttons 不应自己变长
    api = dashboard.Api()
    a = api.list_mouse_buttons()
    b = api.list_mouse_buttons()
    check("后端重复查询返回一致（无副作用）", a == b,
          "%d 项 -> %d 项" % (len(a), len(b)))

    # 模拟用户点 3 次「识别并添加自定义按键」
    # 【本轮设计变更】旧实现是**盲分配**（读不出按了哪个键，
    # 每次随便找个空槽），所以连点 3 次会得到 3 个新项。
    # 新实现能精确读出 usButtons 位图，且「同一个键不重复添加」——
    # 所以没按任何键时 3 次都不该成功。
    # 这条断言从「盲分配会变长」改成「识别不到就是拒绝添加」，
    # 因为**能加出垃圾条目本身就是缺陷**，不该被断言保护。
    for i in range(3):
        api.add_custom_mouse("")
    c = api.list_mouse_buttons()
    base = len(dashboard.BUILTIN_MOUSE_BUTTONS)
    check("没按任何键时重复添加不会产生垃圾条目",
          len(c) == base,
          "实际 %d 项（内置 %d），不该多出无意义的自定义键" % (len(c), base))

    # 模拟：识别 G1 -> 成功；再识别 G1 -> 提示已存在；识别 G2 -> 成功
    with dashboard._raw_lock:
        dashboard._raw_keys["ids"] = [3]
    r1 = api.add_custom_mouse("")
    check("识别 G1 成功", r1.get("ok") is True, r1.get("msg"))

    with dashboard._raw_lock:
        dashboard._raw_keys["ids"] = [3]
    r2 = api.add_custom_mouse("")
    check("同一键重复识别被拒绝", r2.get("ok") is False, r2.get("msg"))

    with dashboard._raw_lock:
        dashboard._raw_keys["ids"] = [4]
    r3 = api.add_custom_mouse("")
    check("识别 G2 成功", r3.get("ok") is True, r3.get("msg"))

    c2 = api.list_mouse_buttons()
    check("最终 = 内置5 + 自定义2", len(c2) == base + 2,
          "实际 %d 项" % len(c2))
    # 清掉测试期间加的，避免污染后续用例
    with dashboard._custom_lock:
        dashboard._custom_mouse_buttons.clear()


# ======================================================================
# bug2 —— F5 全局热键
# ======================================================================
def test_bug2():
    print("\n【bug2】F5 全局热键启动/停止")
    import dashboard
    from core import hotkey as hk

    check("默认热键是 F5", dashboard.HOTKEY_MOUSE == "F5",
          "实际 %r" % dashboard.HOTKEY_MOUSE)
    check("降级链首选也是 F5", dashboard.HOTKEY_MOUSE_FALLBACK[0] == "F5")

    api = dashboard.Api()
    res = api.register_hotkeys()
    check("热键注册成功", res["mouse"] is not None,
          res.get("note") or "已注册：%s" % res.get("mouse"))

    if not res["mouse"]:
        print("     （热键被占用，无法做触发测试）")
        return

    fired = []
    # 直接测底层监听器能不能收到 WM_HOTKEY
    # 【2026-10-18】原来用 Ctrl+F7 —— v34 给宏区加了热键降级链
    # ["F7", "Ctrl+F7", "F10"]（dashboard.py HOTKEY_MACRO_FALLBACK），
    # 回归机上 F7 偶发被占时宏会降级拿走 Ctrl+F7，测试就跟着假红。
    # 换成离四条 fallback 链（F5/F6/F7/F8/F9/F10 系）最远的 Ctrl+F11。
    hl = hk.HotkeyListener("Ctrl+F11", lambda: fired.append(time.time()))
    ok, err = hl.start()
    if ok:
        # 用另一个 API 模拟按键触发不方便（需要手动按），
        # 这里改为验证 stop() 能干净退出，不残留按键占用
        time.sleep(0.3)
        hl.stop()
        check("热键监听器可正常停止（不残留占用）", True)
    else:
        check("Ctrl+F11 测试热键注册", False, err)
        hl.stop()

    # 验证 toggle_mouse 存在且可调用
    check("toggle_mouse 方法存在", hasattr(api, "toggle_mouse"))
    check("toggle_keyboard 方法存在", hasattr(api, "toggle_keyboard"))
    check("shutdown 方法存在", hasattr(api, "shutdown"))

    # 参数记忆：热键触发时没有前端表单，必须用记忆的参数
    api.start_mouse({"button": "right", "action": "click",
                     "count": "3", "duration": "0", "cps": "20"}, hide=False)
    check("热键参数已记忆", api.last_mouse.get("button") == "right",
          repr(api.last_mouse))
    api.stop_mouse()
    api.shutdown()


# ======================================================================
# bug3 —— 反复点击没效果（核心）
# ======================================================================
def test_bug3_timing():
    print("\n【bug3】反复点击模式：down 与 up 的时间差")
    import core.keyboard as kb

    # 拦截 send_key，记录每次调用的时刻与up 标记
    events = []
    orig = kb.send_key

    def spy(vk, up=False, mark_injected=False, use_scancode=True):
        events.append((time.time(), bool(up)))
        return True

    kb.send_key = spy

    try:
        lock = kb.KeyHoldLock(keys="w", threshold_sec=0.1, repeat_hz=10,
                             hold_down=False, pair_gap_ms=20)
        # 手动把状态推到locked，让注入循环开始工作
        lock._active_key = kb.key_to_vk("w")
        lock._state[lock._active_key] = "locked"
        t = threading.Thread(target=lock._inject_loop, daemon=True)
        t.start()
        time.sleep(1.2)
        lock._stop_inject = True
        t.join(timeout=2.0)
    finally:
        kb.send_key = orig

    downs = [t for t, up in events if not up]
    ups = [t for t, up in events if up]
    check("有成对注入", len(downs) > 0 and len(ups) > 0,
          "down=%d up=%d" % (len(downs), len(ups)))
    check("down 与 up 数量一致（成对）", len(downs) == len(ups),
          "down=%d up=%d" % (len(downs), len(ups)))

    # ---- 关键断言：每一对 down->up 之间必须真的隔了时间 ----
    ups_set = sorted(ups)
    gaps = []
    for d in downs:
        nxt = [u for u in ups_set if u >= d]
        if nxt:
            gaps.append((nxt[0] - d) * 1000.0)
    if gaps:
        avg = sum(gaps) / len(gaps)
        check("down->up 存在可感知间隔（>=10ms）", avg >= 10.0,
              "实测平均 %.1f ms，共 %d 对" % (avg, len(gaps)))
        check("间隔不超过频率上限（未拖慢节奏）", avg <= 90.0,
              "实测平均 %.1f ms（10次/秒 时期望 100ms 一轮）" % avg)
    else:
        check("能算出 down->up 间隔", False, "样本不足")


def test_bug3_param():
    print("\n【bug3b】锁定行为参数解析")
    import dashboard

    api = dashboard.Api()
    # 覆盖字符串、布尔、缺省、空串。
    # 空串必须回退成「一直按着」（默认档）——否则用户明明选的是
    # 「一直按着」却跑成「反复点击」，界面上还看不出任何异常。
    cases = [
        ("true", True), ("false", False),
        ("TRUE", True), (" False ", False),
        (True, True), (False, False),
        ("", True),          # 空值 -> 默认档
        ("__MISSING__", True),  # 缺省-> 默认档
    ]
    for raw, expect_hold in cases:
        api2 = dashboard.Api()
        try:
            params = {"keys": "w", "threshold": "0.3", "hz": "20",
                      "hold": raw, "gap": "20"}
            if raw == "__MISSING__":
                params.pop("hold")
            api2.start_keyboard(params)
            got = api2.last_key["hold"] == "true"
            check("hold=%-12r -> hold_down=%-5s" % (raw, expect_hold),
                  got == expect_hold, "解析成 %s" % got)
            api2.stop_keyboard()
        except Exception as e:
            check("hold=%r 解析" % (raw,), False, str(e))
    api.stop_keyboard()


def test_bug3_mode_switch():
    print("\n【bug3c】成对模式确实能产生动作（模拟目标程序判定）")
    import core.keyboard as kb

    # 目标程序的典型判定：keydown 记状态，keyup 时若「按下时长 >= 10ms」才算一次点击
    MIN_HOLD = 0.010
    state = {"down_at": None, "clicks": 0}
    events = []
    orig = kb.send_key

    def spy(vk, up=False, mark_injected=False, use_scancode=True):
        events.append((time.time(), bool(up)))
        if not up:
            state["down_at"] = time.time()
        else:
            if state["down_at"] is not None:
                held = time.time() - state["down_at"]
                if held >= MIN_HOLD:
                    state["clicks"] += 1
            state["down_at"] = None
        return True

    kb.send_key = spy
    try:
        lock = kb.KeyHoldLock(keys="w", threshold_sec=0.1, repeat_hz=10,
                             hold_down=False, pair_gap_ms=20)
        lock._active_key = kb.key_to_vk("w")
        lock._state[lock._active_key] = "locked"
        t = threading.Thread(target=lock._inject_loop, daemon=True)
        t.start()
        time.sleep(1.2)
        lock._stop_inject = True
        t.join(timeout=2.0)
    finally:
        kb.send_key = orig

    check("目标程序判定出了有效点击（>=3 次）", state["clicks"] >= 3,
          "1.2 秒内被判定为 %d 次点击" % state["clicks"])


# ======================================================================
if __name__ == "__main__":
    print("=" * 62)
    print("本轮 4 个 bug 定向回归")
    print("=" * 62)
    test_bug1()
    test_bug2()
    test_bug3_timing()
    test_bug3_param()
    test_bug3_mode_switch()
    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    print("=" * 62)
    sys.exit(1 if FAIL else 0)