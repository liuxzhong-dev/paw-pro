# -*- coding: utf-8 -*-
"""
diag_v27_macro_probe.py —— v27 新功能专项探针（S2 宏录制回放 GUI 化）
=====================================================================

【v27 做什么】把「录制 → 保存 → 按原时序回放」做进图形界面，成为独立分区。

  后端三块：
    core/recorder.py        低级钩子（WH_KEYBOARD_LL + WH_MOUSE_LL）录制
    plugins/macro_replay.py 回放插件（走 ctx.wait，暂停/停止都灵）
    core/config.py          macros/ 目录的读写（与 profiles/ 同一套 base 语义）
  前端一块：
    ui/dashboard.html 的「宏录制区」四格（录制 / 已录 / 回放 / 保存载入）

【本探针最要紧的一件事】验证「不会录到自己的注入」。
  这条不成立的话，回放一次就会把自己注入的事件录进去，
  下次回放变成两倍动作，再录再放滚成指数级 —— 是能把机器搞死的那种 bug。
  所以第 4 节是**真装真卸钩子 + 真发注入事件**，不靠静态断言。

【测试纪律】
  · 除第 4 节那一次 F24 按键（无功能键，用来证明钩子真的在听），
    其余一律不往系统发真实输入（硬规则 6）。
  · 回放测试把 core.inputs / core.keyboard 全打桩成记录器。
  · 每个场景开头清零计数器（v22 六个假红的教训）。
"""

import ctypes
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = []
FAIL = []
SKIP = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def skip(name, why):
    """
    环境不具备、无法验证的项。

    【为什么单独一类】「注入事件到不了钩子」和「过滤写错了」在断言上
    长得一模一样（都是 count==0）。混在一起要么假绿（把 bug 当环境），
    要么假红（把环境当 bug）。所以宁可显式跳过，并在汇总里单独列出来
    提醒去真机验，也不能糊过去。
    """
    SKIP.append("%s（%s）" % (name, why))
    print("  [SKIP] %s —— %s" % (name, why))


def section(t):
    print("\n=== %s ===" % t)


def summary():
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 56)
    print("v27 探针汇总：%d 项通过 / %d 项失败（共 %d 项判定）"
          % (len(PASS), len(FAIL), total))
    if SKIP:
        print("\n环境不可用、需真机复验的 %d 项：" % len(SKIP))
        for s in SKIP:
            print("  ~ %s" % s)
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 56)
    return 0 if not FAIL else 1


def code_only(src):
    """剥掉注释（硬规则 38）：注释里写「原来错在哪」不算残留代码。"""
    import re
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.split("\n"))


# ======================================================================
# 1. 录制器 —— 键盘事件与过滤链
# ======================================================================
def test_recorder_keyboard():
    section("1. 录制器 · 键盘事件与过滤")
    from core import recorder as R

    rec = R.Recorder()

    def kb(vk, flags=0, extra=0):
        return R.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags,
                                 time=0, dwExtraInfo=extra)

    # --- 正常按下/抬起 ---
    rec.feed_key(R.WM_KEYDOWN, kb(0x41))        # 'A'
    rec.feed_key(R.WM_KEYUP, kb(0x41))
    evs = rec.events()
    check("普通按键录成 kd + ku 两条", len(evs) == 2, evs)
    check("kd 事件带对键码", evs and evs[0].get("a") == "kd"
          and evs[0].get("vk") == 0x41, evs[:1])
    check("ku 事件带对键码", len(evs) > 1 and evs[1].get("a") == "ku"
          and evs[1].get("vk") == 0x41, evs[1:])
    check("时间戳单调不减", all(evs[i]["t"] <= evs[i + 1]["t"]
                              for i in range(len(evs) - 1)), evs)

    # --- 系统键也算按键（Alt+F4 这类走 WM_SYSKEYDOWN）---
    rec2 = R.Recorder()
    rec2.feed_key(R.WM_SYSKEYDOWN, kb(0x73))    # F4
    check("WM_SYSKEYDOWN 也被录（Alt+组合键不丢）",
          len(rec2.events()) == 1, rec2.events())

    # --- 本程序注入的事件必须丢（自己打的标记）---
    rec3 = R.Recorder()
    rec3.feed_key(R.WM_KEYDOWN, kb(0x42, extra=R.MARK_INJECTED))
    rec3.feed_key(R.WM_KEYUP, kb(0x42, extra=R.MARK_INJECTED))
    check("带 MARK_INJECTED 的按键一条都不录（防回放回流）",
          len(rec3.events()) == 0, rec3.events())

    # --- 外部注入：默认也丢（ignore_injected=True）---
    rec4 = R.Recorder(ignore_injected=True)
    rec4.feed_key(R.WM_KEYDOWN, kb(0x43, flags=R.LLKHF_INJECTED))
    check("默认忽略所有注入事件（flags & LLKHF_INJECTED）",
          len(rec4.events()) == 0, rec4.events())

    # --- 关掉总闸后外部注入会录，但自己的标记仍然丢 ---
    # 【这条是负向对照的反面】如果实现把「认标记」写在「看总闸」之后，
    # 一关总闸自己的注入就混进来了，回放一次录一次，指数爆炸。
    rec5 = R.Recorder(ignore_injected=False)
    rec5.feed_key(R.WM_KEYDOWN, kb(0x44, flags=R.LLKHF_INJECTED))
    check("关掉注入过滤后，别人的注入事件会被录",
          len(rec5.events()) == 1, rec5.events())
    rec5.feed_key(R.WM_KEYDOWN, kb(0x45, flags=R.LLKHF_INJECTED,
                                   extra=R.MARK_INJECTED))
    check("…但本程序自己的注入仍然一条不录（标记优先于总闸）",
          len(rec5.events()) == 1, rec5.events())

    # --- 停止键：不录 + 请求停录 ---
    rec6 = R.Recorder()
    rec6.feed_key(R.WM_KEYDOWN, kb(R.VK_PAUSE))
    check("Pause 键不被录进宏", len(rec6.events()) == 0, rec6.events())
    check("Pause 键会请求停止录制", rec6.stop_requested is True)
    check("ScrollLock 也不录（暂停热键的降级候选）",
          (rec6.feed_key(R.WM_KEYDOWN, kb(R.VK_SCROLL)),
           len(rec6.events()) == 0)[1])

    # --- 按下状态跟踪 ---
    rec7 = R.Recorder()
    rec7.feed_key(R.WM_KEYDOWN, kb(0x10))       # Shift
    check("按下后记进 held 集合", 0x10 in rec7._held_keys)
    rec7.feed_key(R.WM_KEYUP, kb(0x10))
    check("抬起后从 held 移除", 0x10 not in rec7._held_keys)


# ======================================================================
# 2. 录制器 —— 鼠标事件
# ======================================================================
def test_recorder_mouse():
    section("2. 录制器 · 鼠标事件与移动抽样")
    from core import recorder as R

    def ms(x, y, wparam_data=0, flags=0, extra=0):
        return R.MSLLHOOKSTRUCT(pt=R.POINT(x=x, y=y),
                                mouseData=wparam_data, flags=flags,
                                time=0, dwExtraInfo=extra)

    # --- 按下带坐标、抬起不带坐标 ---
    rec = R.Recorder()
    rec.feed_mouse(R.WM_MOUSEMOVE, ms(100, 200))
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(100, 200))
    rec.feed_mouse(R.WM_LBUTTONUP, ms(100, 200))
    evs = rec.events()
    check("鼠标事件共 3 条（move + down + up）", len(evs) == 3, evs)
    down = [e for e in evs if e["a"] == "down"]
    up = [e for e in evs if e["a"] == "up"]
    check("按下事件带坐标（回放要先搬过去再按）",
          down and (down[0].get("x"), down[0].get("y")) == (100, 200), down)
    check("抬起事件不带坐标（抬的时候不该再移动鼠标）",
          up and "x" not in up[0], up)
    check("左键的键名是 left", down and down[0].get("b") == "left", down)

    # --- 移动抽样：连续 1px 微动只留一条 ---
    # 【注意阈值语义】判定是 dx<4 且 dy<4 且 间隔<12ms 才丢，
    # 所以「1px 微动」最多只能测到 3px 以内；测到第 5 次时累计位移
    # 已经 4px，会正常记录——那不是 bug，是我第一版断言写错了。
    rec2 = R.Recorder()
    for i in range(4):
        rec2.feed_mouse(R.WM_MOUSEMOVE, ms(300 + i, 300))
    check("连续 1px 微动（累计 3px 内）被抽样成 1 条",
          len(rec2.events()) == 1, rec2.events())
    time.sleep(0.02)                    # 超过 12ms 的采样间隔
    rec2.feed_mouse(R.WM_MOUSEMOVE, ms(302, 300))
    check("间隔超过 12ms 后再动会补一条", len(rec2.events()) == 2,
          rec2.events())
    rec2.feed_mouse(R.WM_MOUSEMOVE, ms(500, 300))   # 大跳 198px
    check("大幅移动（>4px）立即记录", len(rec2.events()) == 3,
          rec2.events())

    # --- 滚轮 ---
    rec3 = R.Recorder()
    rec3.feed_mouse(R.WM_MOUSEWHEEL, ms(0, 0, wparam_data=120))
    rec3.feed_mouse(R.WM_MOUSEWHEEL, ms(0, 0, wparam_data=0xFFFFFF88))  # -120
    rec3.feed_mouse(R.WM_MOUSEWHEEL, ms(0, 0, wparam_data=0))          # 抖动
    evs3 = rec3.events()
    check("滚轮向上 = +1 格", evs3 and evs3[0].get("d") == 1, evs3)
    check("滚轮向下 = -1 格（有符号解释，不是 42 亿）",
          len(evs3) > 1 and evs3[1].get("d") == -1, evs3)
    check("0 格抖动被丢弃", len(evs3) == 2, evs3)
    check("滚轮 delta 换算 = 120 一格", R.to_delta_grid(120) == 1
          and R.to_delta_grid(-240) == -2 and R.to_delta_grid(0) == 0)

    # --- 横向滚轮 ---
    rec4 = R.Recorder()
    rec4.feed_mouse(R.WM_MOUSEHWHEEL, ms(0, 0, wparam_data=120))
    evs4 = rec4.events()
    check("横向滚轮记成 hwheel（不能被当成上下滚）",
          evs4 and evs4[0].get("a") == "hwheel", evs4)

    # --- 侧键 ---
    rec5 = R.Recorder()
    rec5.feed_mouse(R.WM_XBUTTONDOWN, ms(10, 10, wparam_data=(1 << 16)))
    rec5.feed_mouse(R.WM_XBUTTONUP, ms(10, 10, wparam_data=(1 << 16)))
    evs5 = rec5.events()
    check("XBUTTON1 记成 x1（编号在高 16 位）",
          evs5 and evs5[0].get("b") == "x1", evs5)
    check("侧键抬起也记（否则回放时一直按着）",
          len(evs5) == 2 and evs5[1].get("a") == "up", evs5)

    # --- 鼠标注入过滤 ---
    rec6 = R.Recorder()
    rec6.feed_mouse(R.WM_LBUTTONDOWN, ms(50, 50, extra=R.MARK_INJECTED))
    check("带标记的鼠标事件不录", len(rec6.events()) == 0, rec6.events())
    rec7 = R.Recorder()
    rec7.feed_mouse(R.WM_LBUTTONDOWN, ms(50, 50, flags=R.LLMHF_INJECTED))
    check("外部注入的鼠标事件默认不录", len(rec7.events()) == 0,
          rec7.events())


# ======================================================================
# 3. 录制器 —— 收尾补抬起 + 上限
# ======================================================================
def test_recorder_finish():
    section("3. 录制器 · 收尾补抬起与录制上限")
    from core import recorder as R

    # --- 只按下没抬起 → 收尾必须补 up ---
    rec = R.Recorder()

    def kb(vk):
        return R.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0,
                                 dwExtraInfo=0)

    def ms(x, y):
        return R.MSLLHOOKSTRUCT(pt=R.POINT(x=x, y=y), mouseData=0,
                                flags=0, time=0, dwExtraInfo=0)

    rec.feed_key(R.WM_KEYDOWN, kb(0x10))          # Shift 按着
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(20, 20))  # 左键按着
    before = rec.count()
    rec._flush_held()
    after = rec.events()
    check("收尾给按着的键补了 ku",
          any(e["a"] == "ku" and e["vk"] == 0x10 for e in after[len(after)-3:]),
          after)
    check("收尾给按着的鼠标键补了 up",
          any(e["a"] == "up" and e["b"] == "left" for e in after[len(after)-3:]),
          after)
    check("补完之后 held 清空", not rec._held_keys and not rec._held_buttons)
    check("补了两条（一个键 + 一个鼠标键）", len(after) == before + 2,
          (before, len(after)))

    # --- 上限：事件太多自动停 ---
    old_max = R.MAX_EVENTS
    try:
        R.MAX_EVENTS = 5
        rec2 = R.Recorder()
        for i in range(10):
            rec2.feed_key(R.WM_KEYDOWN, kb(0x30 + i))
        evs = rec2.events()
        check("超过 MAX_EVENTS 后停止写入", len(evs) <= 6, len(evs))
        check("overflow 标成 events", rec2.overflow == "events",
              rec2.overflow)
        check("overflow 后请求停录", rec2.stop_requested is True)
    finally:
        R.MAX_EVENTS = old_max

    # --- 时长上限 ---
    old_sec = R.MAX_SECONDS
    try:
        R.MAX_SECONDS = 0.01
        rec3 = R.Recorder()
        rec3.feed_key(R.WM_KEYDOWN, kb(0x41))
        time.sleep(0.05)
        rec3.feed_key(R.WM_KEYDOWN, kb(0x42))
        check("超过 MAX_SECONDS 后自动停", rec3.overflow == "seconds",
              rec3.overflow)
    finally:
        R.MAX_SECONDS = old_sec

    # --- stats 快照 ---
    # 【v30 按新规格改写】起点模式上线后 stats 多了 started / armed /
    # waited / mode 四个字段（界面靠它们显示「等起点…」）。
    # 断言从「恰好等于五个键」改成「至少包含原有五个键」——
    # 新增字段是规格扩展，不该让老断言变红，但原有五个必须还在
    # （否则前端 / 探针对 recording/count 的依赖就断了）。
    rec4 = R.Recorder()
    rec4.feed_key(R.WM_KEYDOWN, kb(0x41))
    st = rec4.stats()
    check("stats 仍包含 recording/count/secs/overflow/error 五个键",
          {"recording", "count", "secs", "overflow", "error"} <= set(st), st)
    check("stats 新增起点字段（started/armed/waited/mode）",
          {"started", "armed", "waited", "mode"} <= set(st), st)
    check("未启动时 stats.recording 为 False", st["recording"] is False)


# ======================================================================
# 4. 真机钩子（唯一会碰系统的一节）
# ======================================================================
def probe_injection_reachable():
    """
    探测「本进程 SendInput 出去的事件，能不能到达本进程装的低级钩子」。

    为什么要先探这一下（2026-10-17 实测记录）：
      WorkBuddy 会话里的 python 进程是**低完整性**的，SendInput 会
      返回成功（ok 计数 +1、GetLastError=0），但事件根本到不了钩子——
      UIPI 把低权限进程的注入挡掉了，连「移到当前光标位置」这种
      零副作用事件都收不到。而同一个钩子**能收到真实的用户点击**
      （实测录到一条 a=down b=left x=1449 y=741）。
    所以「钩子没录到注入」有两种完全不同的原因，必须先用这一探分开：
      · 能到 → 后面可以断言完整的过滤链（真验证）
      • 到不了 → 后面只能跳过，并提示去真机（管理员/正常桌面）验
    """
    from core import recorder as R
    from core import inputs, keyboard

    rec = R.Recorder(ignore_injected=False)
    ok, err = rec.start()
    if not ok:
        rec.stop()
        return False, "钩子装不上：%s" % err
    try:
        pos = inputs.get_cursor_pos() or (0, 0)
        before = rec.count()
        # 移到「当前所在位置」：系统照样派发 move，屏幕上看不出变化
        inputs.move_to(pos[0], pos[1])
        time.sleep(0.12)
        mouse_ok = rec.count() > before

        before2 = rec.count()
        keyboard.send_key(0x7C, up=False, mark_injected=False,
                          use_scancode=False)     # F13，无功能键
        time.sleep(0.12)
        key_ok = rec.count() > before2
        return (mouse_ok or key_ok), ("鼠标注入可达=%s / 键盘注入可达=%s"
                                      % (mouse_ok, key_ok))
    finally:
        try:
            rec.stop()
        except Exception:
            pass


def test_recorder_real_hook():
    section("4. 真机钩子 · 装/卸 + 注入可达性")
    from core import recorder as R
    from core import keyboard

    rec = R.Recorder()

    def f24_count():
        """只数「F24 按下」。原因见下面「认知 2」：总数会被真人操作污染。"""
        return sum(1 for e in rec.events()
                   if e["a"] == "kd" and e["vk"] == 0x87)

    ok, err = rec.start()
    check("低级键鼠钩子安装成功（真机）", ok, err)
    if not ok:
        check("钩子装不上时后续真机断言跳过（记为失败）", False, err)
        return

    check("键盘钩子句柄非空", bool(rec._hook_kb))
    check("鼠标钩子句柄非空", bool(rec._hook_ms))
    check("钩子线程活着", rec._thread is not None and rec._thread.is_alive())
    # hMod 必须是 NULL。传 exe 模块句柄在本机会「装上了但永远收不到」，
    # 这条断言把结论钉在代码里（core/keyboard.py 也是同一个结论）。
    check("SetWindowsHookEx 的 hMod 传 NULL（传模块句柄会静默失效）",
          R._module_handle() is None, R._module_handle())

    reachable, why = probe_injection_reachable()
    if not reachable:
        skip("注入事件的端到端过滤链", "本会话环境拦掉了注入事件；" + why)
    else:
        # 注入可达：可以做真验证
        # 【认知 1】自己 SendInput 出去的事件即使不打 MARK_INJECTED，
        # 系统也会加 LLKHF_INJECTED，所以要先关掉注入总闸才能录到。
        # 【认知 2】这里**不能比总数**：探针跑的时候人是活的，
        # 用户随手动一下鼠标就多几十条事件（实测 158→194，多出的全是
        rec.ignore_injected = False
        base = f24_count()
        keyboard.send_key(0x87, up=False, mark_injected=False,
                          use_scancode=False)
        time.sleep(0.08)
        keyboard.send_key(0x87, up=True, mark_injected=False,
                          use_scancode=False)
        time.sleep(0.12)
        check("关掉注入过滤后，不带标记的按键被录到（钩子确实在工作）",
              f24_count() > base, (base, f24_count()))

        mid = f24_count()
        keyboard.send_key(0x87, up=False, mark_injected=True,
                          use_scancode=False)
        keyboard.send_key(0x87, up=True, mark_injected=True,
                          use_scancode=False)
        time.sleep(0.12)
        check("带标记的注入事件被真钩子过滤掉（标记优先于总闸）",
              f24_count() == mid, (mid, f24_count()))

    # 停止：钩子卸载、线程退出（这条不依赖注入是否能到，永远可验）
    t0 = time.time()
    events = rec.stop()
    cost = time.time() - t0
    check("stop() 在 2 秒内收工", cost < 2.0, "%.2fs" % cost)
    check("停止后 running 为 False", rec.running is False)
    check("停止后钩子句柄已清空",
          rec._hook_kb is None and rec._hook_ms is None)
    check("停止后线程已退出", rec._thread is None, rec._thread)
    check("stop() 返回的是事件表", isinstance(events, list), type(events))

    # 卸钩子之后再注入：不能再录到
    n_after = f24_count()
    keyboard.send_key(0x87, up=False, mark_injected=False, use_scancode=False)
    time.sleep(0.1)
    check("卸钩子之后再按键不会增加事件", f24_count() == n_after,
          (n_after, f24_count()))


# ======================================================================
# 5. 事件表工具
# ======================================================================
def test_event_tools():
    section("5. 事件表工具 · 校验与摘要")
    from core import recorder as R

    good = [
        {"t": 0, "a": "move", "x": 10, "y": 20},
        {"t": 100, "a": "down", "b": "left", "x": 10, "y": 20},
        {"t": 180, "a": "up", "b": "left"},
        {"t": 300, "a": "wheel", "d": -2},
        {"t": 400, "a": "kd", "vk": 65},
        {"t": 480, "a": "ku", "vk": 65},
    ]
    clean, err = R.validate_events(good)
    check("合法事件表通过校验", err == "" and len(clean) == 6, err)

    check("duration_ms 取最后一条时间戳", R.duration_ms(good) == 480)
    check("空表 duration 为 0", R.duration_ms([]) == 0)

    info = R.summarize(good)
    check("摘要统计左键点击次数", info["clicks"] == 1, info)
    check("摘要统计键盘按下次数", info["keys"] == 1, info)
    check("摘要时长按秒给", info["secs"] == 0.5, info)
    check("摘要预览里有「按下left」", "按下left" in info["preview"], info)

    # --- 脏数据一律大声拒绝 ---
    bad_cases = [
        ({"a": "move"}, "不是列表"),
        ([{"a": "hack"}], "动作未知"),
        ([{"a": "move", "x": 1}], "缺坐标"),
        ([{"a": "down", "b": "wheel3", "x": 1, "y": 1}], "键名不认识"),
        ([{"a": "kd"}], "缺键码"),
        ([{"a": "kd", "vk": 999}], "键码越界"),
        ([{"t": "abc", "a": "up", "b": "left"}], "时间戳不是整数"),
    ]
    for bad, why in bad_cases:
        c, e = R.validate_events(bad)
        check("坏数据必须报错（%s）" % why, bool(e), e)

    # --- 时间戳倒序会被修正成单调 ---
    messy = [{"t": 500, "a": "up", "b": "left"},
             {"t": 100, "a": "up", "b": "left"}]
    fixed, err2 = R.validate_events(messy)
    check("倒序时间戳被修正为单调不减（否则回放出现负等待）",
          err2 == "" and fixed[1]["t"] >= fixed[0]["t"], fixed)


# ======================================================================
# 6. 宏存储（core/config.py）
# ======================================================================
def test_macro_storage(tmpdir):
    section("6. 宏存储 · 目录/命名/读写/删除")
    from core import config as cfg

    check("macros_dir 落到指定 base 下",
          cfg.macros_dir(tmpdir) == os.path.join(tmpdir, "macros"))

    # --- 名字安全化：路径穿越 ---
    check("safe_name 滤掉路径分隔符",
          cfg.safe_name("../../etc/passwd") == "....etcpasswd",
          cfg.safe_name("../../etc/passwd"))
    check("safe_name 拒绝纯点号", cfg.safe_name("..") == "")
    check("safe_name 拒绝空名", cfg.safe_name("   ") == "")
    check("safe_name 保留中文", cfg.safe_name("每日签到") == "每日签到")

    evs = [{"t": 0, "a": "down", "b": "left", "x": 5, "y": 6},
           {"t": 60, "a": "up", "b": "left"}]

    ok, res = cfg.save_macro("每日签到", evs, speed=2.0, repeat=3, base=tmpdir)
    check("保存宏成功", ok, res)
    check("宏文件落在 macros/ 下",
          ok and os.path.isfile(os.path.join(tmpdir, "macros", "每日签到.json")),
          res)
    ok2, data, msg = cfg.load_macro("每日签到", base=tmpdir)
    check("读回来的事件数一致", ok2 and len(data["events"]) == 2, msg)
    check("读回来保留倍速", ok2 and data.get("speed") == 2.0, data)
    check("读回来保留次数", ok2 and data.get("repeat") == 3, data)
    check("读回来带 schema_version",
          ok2 and data.get("schema_version") == cfg.MACRO_SCHEMA_VERSION, data)
    check("list_macros 能看到它", "每日签到" in cfg.list_macros(base=tmpdir),
          cfg.list_macros(base=tmpdir))

    # --- 拒绝空/坏数据 ---
    ok3, msg3 = cfg.save_macro("空宏", [], base=tmpdir)
    check("空事件表不许保存", not ok3, msg3)
    ok4, msg4 = cfg.save_macro("坏宏", [{"a": "hack"}], base=tmpdir)
    check("脏事件不许写盘", not ok4, msg4)

    # --- 不存在时报错要说清楚 ---
    ok5, _, msg5 = cfg.load_macro("不存在的宏", base=tmpdir)
    check("读不存在的宏要失败", not ok5, msg5)
    check("失败信息里点出目录（用户知道去哪找）",
          ok5 or "macros" in msg5 or "宏不存在" in msg5, msg5)

    # --- 删除 ---
    ok6, msg6 = cfg.delete_macro("每日签到", base=tmpdir)
    check("删除宏成功", ok6, msg6)
    check("删完之后列表里没有它",
          "每日签到" not in cfg.list_macros(base=tmpdir))
    ok7, msg7 = cfg.delete_macro("每日签到", base=tmpdir)
    check("重复删除要报错而不是静默", not ok7, msg7)


# ======================================================================
# 7. 回放（打桩注入）
# ======================================================================
class PlayStub(object):
    """把 core.inputs / core.keyboard 的注入换成记录器。"""

    def __init__(self):
        self.calls = []

    def install(self):
        from core import inputs, keyboard
        self._saved = {}
        for name in ("move_to", "press_down", "release", "scroll",
                     "scroll_horizontal", "press_down_xbutton",
                     "release_xbutton"):
            self._saved[("inputs", name)] = getattr(inputs, name)
            setattr(inputs, name, self._mk("inputs." + name))
        self._saved[("keyboard", "send_key")] = keyboard.send_key
        keyboard.send_key = self._mk("keyboard.send_key")

    def _mk(self, label):
        def f(*a, **kw):
            self.calls.append((label, a, kw))
            return True
        return f

    def clear(self):
        self.calls = []

    def restore(self):
        from core import inputs, keyboard
        for (mod, name), fn in self._saved.items():
            if mod == "inputs":
                setattr(inputs, name, fn)
            else:
                setattr(keyboard, name, fn)


def make_ctx(events, speed=1.0, repeat=1):
    """真 ClickContext + 真插件，只是注入被打了桩。"""
    from core.engine import ClickContext
    from plugins.macro_replay import MacroReplayPlugin
    plugin = MacroReplayPlugin()
    ctx = ClickContext(options={"events": events, "speed": speed,
                                "repeat": repeat}, base_cps=1)
    ok = plugin.on_start(ctx)
    return plugin, ctx, ok


def test_replay():
    section("7. 回放 · 时序/倍速/可中断/收尾释放")
    stub = PlayStub()
    stub.install()
    try:
        events = [
            {"t": 0, "a": "move", "x": 10, "y": 20},
            {"t": 100, "a": "down", "b": "left", "x": 10, "y": 20},
            {"t": 180, "a": "up", "b": "left"},
            {"t": 220, "a": "wheel", "d": -2},
            {"t": 260, "a": "hwheel", "d": 1},
            {"t": 300, "a": "kd", "vk": 65},
            {"t": 360, "a": "ku", "vk": 65},
        ]

        # --- 1x：调用序列与耗时 ---
        stub.clear()
        plugin, ctx, ok = make_ctx(events, 1.0, 1)
        check("on_start 通过（事件表合法）", ok)
        t0 = time.time()
        plugin.run(ctx)
        cost = time.time() - t0
        labels = [c[0] for c in stub.calls]
        # 正式序列 8 条：move, move+down, up, wheel, hwheel, kd, ku
        # （按下前要多搬一次坐标，所以 move_to 是 2 次）
        # 后面挂着 _release_all 的收尾调用，所以只比前 8 项。
        expect = ["inputs.move_to", "inputs.move_to", "inputs.press_down",
                  "inputs.release", "inputs.scroll",
                  "inputs.scroll_horizontal", "keyboard.send_key",
                  "keyboard.send_key"]
        check("回放按顺序调用了 8 步注入", labels[:8] == expect, labels)
        check("第一条是 move_to", labels[0] == "inputs.move_to", labels)
        check("按下前先搬坐标（否则点在当前光标处）", labels[1] == "inputs.move_to",
              labels)
        check("鼠标按下用 press_down(left)",
              stub.calls[1][1] == (10, 20) and stub.calls[2][0] == "inputs.press_down",
              stub.calls[1:3])
        check("抬起不搬坐标（只调 release）",
              stub.calls[3][0] == "inputs.release", stub.calls[3])
        check("纵向滚轮走 scroll(-2)",
              ("inputs.scroll", (-2,), {}) in stub.calls, stub.calls)
        check("横向滚轮走 scroll_horizontal（不能当上下滚）",
              ("inputs.scroll_horizontal", (1,), {}) in stub.calls, stub.calls)
        check("键盘按下走 send_key(up=False, 带注入标记)",
              any(c[0] == "keyboard.send_key" and c[1][0] == 65
                  and c[2].get("up") is False
                  and c[2].get("mark_injected") is True for c in stub.calls),
              stub.calls)
        check("左键点击计入 clicked", ctx.clicked == 1, ctx.clicked)
        check("1x 回放耗时约 0.36 秒（±0.25）", 0.11 <= cost <= 0.61,
              "%.3fs" % cost)

        # --- 倍速：4x 必须明显更快 ---
        stub.clear()
        plugin2, ctx2, _ = make_ctx(events, 4.0, 1)
        t1 = time.time()
        plugin2.run(ctx2)
        cost4 = time.time() - t1
        check("4x 回放耗时约为 1x 的 1/3 以下",
              cost4 < cost * 0.45, "1x=%.3fs 4x=%.3fs" % (cost, cost4))
        # 用 press_down 计数而不是总数：末端的收尾释放会额外加调用，
        # 总数会因为「按过什么」而浮动（v22 的教训：拿累加和当断言）。
        check("4x 下动作数不变（只改快慢不改内容）",
              sum(1 for c in stub.calls if c[0] == "inputs.press_down") == 1,
              stub.calls)

        # --- 重复次数 ---
        stub.clear()
        plugin3, ctx3, _ = make_ctx(events, 8.0, 3)
        plugin3.run(ctx3)
        check("重复 3 次 = 按下 3 次",
              sum(1 for c in stub.calls if c[0] == "inputs.press_down") == 3,
              stub.calls)
        check("重复 3 次 = clicked 3", ctx3.clicked == 3, ctx3.clicked)

        # --- 非法倍速兜底 ---
        check("倍速 0 被夹成 0.1（否则时间轴为负、无限循环）",
              plugin3._speed(0) >= 0.1, plugin3._speed(0))
        check("倍速写成字符串也能解析", plugin3._speed("2") == 2.0)
        check("倍速上限夹在 8", plugin3._speed(999) == 8.0)

        # --- 可中断：最后一步等 3 秒，150ms 后停止必须立刻退出 ---
        long_events = [
            {"t": 0, "a": "move", "x": 1, "y": 1},
            {"t": 3000, "a": "move", "x": 2, "y": 2},
        ]
        stub.clear()
        plugin4, ctx4, _ = make_ctx(long_events, 1.0, 1)
        th = threading.Thread(target=plugin4.run, args=(ctx4,), daemon=True)
        t2 = time.time()
        th.start()
        time.sleep(0.15)
        ctx4._stop_event.set()
        th.join(timeout=1.5)
        cost_stop = time.time() - t2
        check("停止信号 300ms 内生效（不是等 3 秒的间隔睡完）",
              not th.is_alive() and cost_stop < 0.6, "%.3fs alive=%s"
              % (cost_stop, th.is_alive()))
        check("被打断时只注入了第 1 条 move",
              len(stub.calls) == 1, len(stub.calls))

        # --- 收尾释放：只 down 没 up 的宏，跑完必须补 release ---
        half = [{"t": 0, "a": "down", "b": "left", "x": 7, "y": 7},
                {"t": 30, "a": "kd", "vk": 16}]
        stub.clear()
        plugin5, ctx5, _ = make_ctx(half, 8.0, 1)
        plugin5.run(ctx5)
        check("宏里只有按下时，收尾补发鼠标 release",
              any(c[0] == "inputs.release" for c in stub.calls), stub.calls)
        check("宏里只有按下时，收尾补发键盘 ku",
              any(c[0] == "keyboard.send_key" and c[2].get("up") is True
                  for c in stub.calls), stub.calls)

        # --- 空事件表 ---
        plugin6, _, ok6 = make_ctx([], 1.0, 1)
        check("空事件表 on_start 返回 False（不许空转）", ok6 is False)

        # --- 脏事件表 ---
        plugin7, ctx7, ok7 = make_ctx([{"a": "hack"}], 1.0, 1)
        check("脏事件表 on_start 返回 False", ok7 is False)
        check("脏事件表把原因写进 ctx.error（界面要能显示出来）",
              ok7 is False and "第 1 步" in str(getattr(ctx7, "error", "")),
              getattr(ctx7, "error", None))
    finally:
        stub.restore()


def test_replay_engine():
    section("7b. 回放 · 真引擎驱动一次")
    from core import engine as eng
    from plugins.macro_replay import MacroReplayPlugin

    stub = PlayStub()
    stub.install()
    try:
        events = [{"t": 0, "a": "down", "b": "left", "x": 3, "y": 4},
                  {"t": 40, "a": "up", "b": "left"}]
        e = eng.ClickerEngine(MacroReplayPlugin(),
                              options={"events": events, "speed": 4.0,
                                       "repeat": 1},
                              base_cps=1)
        done = {}
        e.on_finish = lambda ctx: done.setdefault("err", getattr(ctx, "error", None))
        e.start()
        t0 = time.time()
        while e.running and time.time() - t0 < 3.0:
            time.sleep(0.02)
        check("引擎线程跑完并退出", not e.running)
        check("引擎里回放没有异常", done.get("err") is None, done)
        check("引擎里确实注入了点击",
              any(c[0] == "inputs.press_down" for c in stub.calls),
              stub.calls)

        # 停止：引擎刚起来就 stop，必须能停下（回放可中断）
        stub.clear()
        long_ev = [{"t": 0, "a": "move", "x": 1, "y": 1},
                   {"t": 5000, "a": "move", "x": 2, "y": 2}]
        e2 = eng.ClickerEngine(MacroReplayPlugin(),
                               options={"events": long_ev, "speed": 1.0,
                                        "repeat": 1}, base_cps=1)
        e2.start()
        time.sleep(0.15)
        t1 = time.time()
        e2.stop(wait=True)
        check("引擎 stop 在 1 秒内收工（5 秒间隔的回放也能立刻停）",
              time.time() - t1 < 1.0, "%.3fs" % (time.time() - t1))
        check("stop 后引擎不再 running", not e2.running)
    finally:
        stub.restore()


# ======================================================================
# 8. dashboard 接线
# ======================================================================
def test_dashboard(tmpdir):
    section("8. dashboard 接线 · Api 层")
    import dashboard as D

    # 设置文件落临时目录，避免往项目根写 macros/
    D._settings_path = lambda: os.path.join(tmpdir, "ui_settings.json")
    api = D.Api()

    # --- 初始状态 ---
    D._MACRO["events"] = []
    D._MACRO["name"] = ""
    D._MACRO["playing"] = False
    st = api.macro_status()
    check("macro_status 有全部键",
          {"ok", "recording", "playing", "name", "count", "secs",
           "preview", "clicks", "keys", "overflow"} <= set(st), st)
    check("初始 count 为 0", st["count"] == 0, st)
    check("初始不在录制", st["recording"] is False)

    # --- 有任务在跑时不许开始录制 ---
    with D.ST.lock:
        D.ST.mouse_running = True
    r = api.macro_start_record()
    check("鼠标任务在跑时拒绝录制（录到的会是程序自己在动）",
          not r["ok"], r)
    with D.ST.lock:
        D.ST.mouse_running = False
        D.ST.key_running = True
    r2 = api.macro_start_record()
    check("键盘锁定在跑时也拒绝录制", not r2["ok"], r2)
    with D.ST.lock:
        D.ST.key_running = False

    # --- 空事件表：回放/保存都要拒绝 ---
    check("没有动作时回放被拒", not api.macro_play(1.0, 1)["ok"])
    check("没有动作时保存被拒", not api.macro_save("空", 1.0, 1)["ok"])

    # --- 载入不存在的宏：大声报错 ---
    r3 = api.macro_load("根本没有这个宏")
    check("载入不存在的宏返回失败", not r3["ok"], r3)
    check("失败信息说清是哪个宏不存在",
          "不存在" in r3["msg"] or "没有" in r3["msg"], r3)

    # --- 有了事件之后：保存 → 列表 → 载入 → 删除 ---
    evs = [{"t": 0, "a": "down", "b": "left", "x": 11, "y": 22},
           {"t": 50, "a": "up", "b": "left"}]
    D._MACRO["events"] = evs
    r4 = api.macro_save("探针宏", 1.0, 2)
    check("有事件时保存成功", r4["ok"], r4)
    check("保存后返回最新列表", "探针宏" in (r4.get("names") or []), r4)
    check("保存后 _MACRO.name 被记住", D._MACRO["name"] == "探针宏",
          D._MACRO["name"])

    D._MACRO["events"] = []
    r5 = api.macro_load("探针宏")
    check("载入已保存的宏成功", r5["ok"], r5)
    check("载入后事件表回到内存", len(D._MACRO["events"]) == 2,
          D._MACRO["events"])
    check("载入回传预览文案", bool(r5.get("preview")), r5)

    r6 = api.macro_delete("探针宏")
    check("删除宏成功", r6["ok"], r6)
    check("删除后列表里没有它", "探针宏" not in (r6.get("names") or []), r6)

    # --- 清空 ---
    D._MACRO["events"] = evs
    r7 = api.macro_discard()
    check("清空后事件表为空", r7["ok"] and D._MACRO["events"] == [], r7)

    # --- 回放：真的能起，且能被停止 ---
    D._MACRO["events"] = [
        {"t": 0, "a": "move", "x": 1, "y": 1},
        {"t": 4000, "a": "move", "x": 2, "y": 2},
    ]
    stub = PlayStub()
    stub.install()
    try:
        r8 = api.macro_play(1.0, 1)
        check("有事件时回放能启动", r8["ok"], r8)
        check("回放中 _MACRO.playing 为 True", D._MACRO["playing"] is True)
        check("回放期间 macro_status.playing 为 True",
              api.macro_status()["playing"] is True)
        check("回放期间再点回放被拒", not api.macro_play(1.0, 1)["ok"])
        time.sleep(0.1)
        r9 = api.macro_stop_play()
        check("停止回放成功", r9["ok"], r9)
        check("停止后 playing 为 False", D._MACRO["playing"] is False)
        check("停止后引擎引用被清空", D._MACRO["engine"] is None)
    finally:
        stub.restore()
        D._MACRO["events"] = []
        D._MACRO["playing"] = False
        D._MACRO["engine"] = None


# ======================================================================
# 9. 静态与双源
# ======================================================================
def test_static(tmpdir):
    section("9. 静态断言与双源同步")
    import dashboard as D

    py = code_only(open(os.path.join(HERE, "dashboard.py"),
                        encoding="utf-8").read())
    html = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()

    check("内嵌 HTML == ui/dashboard.html（双源一致）", D.HTML == html,
          (len(D.HTML), len(html)))

    # 【v30 按新规格改写】② 格从「已录动作摘要」换成了「步骤列表」，
    # macPreview 被 macSteps / macStepsMore 取代；v41 再把「起点」下拉
    # 换成启停键流程提示（macFlowHint / macFlowKey）。
    ids = ["cardMacro", "macRecBtn", "macIgnoreInj", "macRecStatus",
           "macFlowHint", "macFlowKey", "macCount", "macSecs", "macClicks",
           "macKeys",
           "macSteps", "macStepsMore", "macModal", "mcName", "mcX", "mcY",
           "mcDelayType", "mcDelay", "mcAfter",
           "macSpeed", "macRepeat", "macPlayBtn", "macStopPlayBtn",
           "macPlayStatus", "macName", "macList", "macSaveStatus"]
    missing = [i for i in ids if ('id="%s"' % i) not in html]
    check("宏区 26 个控件 id 全部存在（v30 步骤编辑器）", not missing, missing)

    for fn in ("macPoll", "macToggleRecord", "macPlay", "macStopPlay",
               "macSave", "macLoad", "macDelete", "macRefreshList"):
        check("前端函数 %s 已定义" % fn,
              ("async function %s(" % fn) in html or
              ("function %s(" % fn) in html)

    check("启动时拉一次宏列表并起轮询",
          "macRefreshList();" in html and "setInterval(macPoll" in html)

    # --- 后端接线点 ---
    check("start_mouse 录制期间拦截",
          "if _macro_recording():" in py, "缺拦截")
    check("start_keyboard 录制期间拦截",
          py.count("if _macro_recording():") >= 2,
          py.count("if _macro_recording():"))
    check("shutdown 里取消录制（否则退出时键盘卡在按下）",
          "_macro_recorder().cancel()" in py)
    check("shutdown 里停回放", 'e.ctx._user_stopped = True' in py)
    check("录制收尾会把窗口叫回来（Pause 停止时窗口是最小化的）",
          "self.show_from_tray()" in py)

    # --- 插件注册 ---
    check("宏回放插件已注册到 Registry",
          "macro_replay" in __import__("plugins.macro_replay",
                                       fromlist=["x"]).MacroReplayPlugin.name)

    # --- 打包清单（打包前先看代码里有没有加） ---
    for spec in ("spec_dashboard.spec", "spec_cli.spec"):
        p = os.path.join(HERE, spec)
        src = open(p, encoding="utf-8").read()
        check("%s 的 HIDDEN 收了 plugins.macro_replay" % spec,
              "plugins.macro_replay" in src)
        check("%s 的 HIDDEN 收了 core.recorder" % spec,
              "core.recorder" in src)

    vm = os.path.join(HERE, "_verify_exe_modules.py")
    vsrc = open(vm, encoding="utf-8").read()
    check("_verify_exe_modules 的 EXPECT_PYZ 收了 core.recorder",
          "core.recorder" in vsrc)
    check("_verify_exe_modules 的 EXPECT_PYZ 收了 plugins.macro_replay",
          "plugins.macro_replay" in vsrc)


# ======================================================================
# 10. 负向对照（打掉一层，断言必须变红）
# ======================================================================
def test_negative(tmpdir):
    section("10. 负向对照 · 打掉防线必须红")
    from core import recorder as R
    from plugins import macro_replay as MR

    # A. 把「认自己的标记」删掉 → 自己的注入会混进事件表
    rec = R.Recorder(ignore_injected=False)

    def kb(extra):
        return R.KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0, flags=0, time=0,
                                 dwExtraInfo=extra)

    real = rec._should_drop_injected
    rec._should_drop_injected = lambda extra, flags, bit: (
        True, False) if (int(flags) & bit) else (False, False)
    rec.feed_key(R.WM_KEYDOWN, kb(R.MARK_INJECTED))
    polluted = len(rec.events())
    rec._should_drop_injected = real
    check("【负向】去掉标记优先级后，自己的注入确实会被录进来（说明防线在起作用）",
          polluted == 1, polluted)

    # B. 切片 vs 一次性睡完 —— 暂停响应（这条真的能分辨）
    #
    # 【我第一版把这条写错了】原来断言「一次性睡完 → 停止信号卡住」，
    # 实测永远不卡：ClickContext.wait 内部是 Event.wait(seconds)，
    # stop_event 一 set 就立刻返回 False。切片的真正理由是**暂停**——
    # 暂停检查在 ctx.wait 的**开头**（while pausectl.is_paused），
    # 睡下去之后就不再看了。所以单个长间隔中途按 Pause：
    #   切片 20ms  → 最多 20ms 后停住，第 2 条动作永远不执行
    #   一次睡 0.8 秒 → 睡完照常执行第 2 条（暂停期间动作还在跑）
    from core import pausectl
    old_slice = MR._SLICE
    events = [{"t": 0, "a": "move", "x": 1, "y": 1},
              {"t": 800, "a": "move", "x": 2, "y": 2}]
    try:
        for slice_sec, label, expect_ran in ((0.02, "切片 20ms", False),
                                             (10.0, "一次睡完", True)):
            MR._SLICE = slice_sec
            stub = PlayStub()
            stub.install()
            pausectl.set_paused(False, "mouse")
            try:
                plugin, ctx, _ = make_ctx(events, 1.0, 1)
                th = threading.Thread(target=plugin.run, args=(ctx,),
                                      daemon=True)
                th.start()
                time.sleep(0.15)
                pausectl.set_paused(True, "mouse")     # 中途按 Pause
                time.sleep(1.0)                        # 已越过第 2 步的目标时刻
                ran = any(c[0] == "inputs.move_to" and tuple(c[1]) == (2, 2)
                          for c in stub.calls)
                pausectl.set_paused(False, "mouse")    # 恢复，让线程收尾
                th.join(timeout=2.0)
                if expect_ran:
                    check("【负向】%s时，暂停期间动作照样执行（说明切片是必要的）"
                          % label, ran, stub.calls)
                else:
                    check("%s时，暂停 1 秒后第 2 步仍未执行（暂停冻结时间轴）"
                          % label, not ran, stub.calls)
            finally:
                pausectl.set_paused(False, "mouse")
                stub.restore()
    finally:
        MR._SLICE = old_slice
        pausectl.set_paused(False, "mouse")

    # C. 回放不释放按下的键 → 只 down 的宏跑完鼠标一直按着
    events = [{"t": 0, "a": "down", "b": "left", "x": 5, "y": 5}]
    stub2 = PlayStub()
    stub2.install()
    try:
        plugin2, ctx2, _ = make_ctx(events, 8.0, 1)
        real_release = plugin2._release_all
        plugin2._release_all = lambda ctx, evs: None      # 打掉收尾
        plugin2.run(ctx2)
        released = any(c[0] == "inputs.release" for c in stub2.calls)
        check("【负向】去掉收尾释放后，鼠标确实一直按着（说明 finally 是必要的）",
              not released, stub2.calls)
        plugin2._release_all = real_release
    finally:
        stub2.restore()

    # D. 校验器放水 → 未知动作会被当成合法
    fake = [{"a": "hack"}]
    ok, err = R.validate_events(fake)
    check("【负向】未知动作确实进不来（校验器没放水）", not ok and bool(err),
          (ok, err))


# ======================================================================
def test_pause_conflict(tmpdir):
    section("11. v28 · 录制中按 Pause 不能顺带把全局暂停打开")
    import dashboard as D
    from core import pausectl
    from core import recorder as R

    D._settings_path = lambda: os.path.join(tmpdir, "ui_settings.json")
    api = D.Api()
    pausectl.set_paused(False)
    D._MACRO["rec"] = None
    D._MACRO["playing"] = False

    # --- 录制中：Pause 只该结束录制，不该改暂停位 ---
    fake = type("FakeRec", (), {"running": True,
                                "stop_requested": True,
                                "cancel": lambda self: None})()
    D._MACRO["rec"] = fake
    r = api.toggle_pause()
    check("录制中按 Pause 不打开全局暂停",
          pausectl.any_paused() is False, pausectl.any_paused())
    check("录制中按 Pause 的返回里带说明（不是静默吞掉）",
          isinstance(r, dict) and "录制" in (r.get("msg") or ""), r)

    # --- 负向对照：把守卫打掉，同一个调用就会把暂停打开 ---
    real = D._macro_recording
    D._macro_recording = lambda: False
    try:
        api.toggle_pause()
        opened = pausectl.any_paused()
    finally:
        D._macro_recording = real
        pausectl.set_paused(False)
    check("【负向】去掉守卫后确实会打开暂停（证明这条测试有辨别力）",
          opened is True, opened)

    # --- 暂停中不许起回放（否则卡在 ctx.wait 的门闩里，看着像没反应）---
    D._MACRO["rec"] = None
    D._MACRO["events"] = [{"t": 0, "a": "move", "x": 1, "y": 1}]
    pausectl.set_paused(True)
    rp = api.macro_play(1.0, 1)
    check("暂停状态下回放被拒绝", not rp["ok"], rp)
    check("拒绝理由里说清「先按 Pause 恢复」",
          "Pause" in (rp.get("msg") or "") or "恢复" in (rp.get("msg") or ""),
          rp)
    check("被拒绝时没留下「回放中」的假状态",
          D._MACRO["playing"] is False, D._MACRO["playing"])
    pausectl.set_paused(False)
    D._MACRO["events"] = []

    # --- ScrollLock 必须也能结束录制（暂停热键降级到它时）---
    rec = R.Recorder()
    check("停止键含 Pause", R.VK_PAUSE in rec.stop_vks, rec.stop_vks)
    check("停止键含 ScrollLock（暂停热键降级后仍能结束录制）",
          R.VK_SCROLL in rec.stop_vks, rec.stop_vks)
    rec.feed_key(R.WM_KEYDOWN,
                 R.KBDLLHOOKSTRUCT(vkCode=R.VK_SCROLL, scanCode=0, flags=0,
                                   time=0, dwExtraInfo=0))
    check("ScrollLock 触发停止请求", rec.stop_requested is True)
    check("ScrollLock 本身不进事件表（它只是结束键）",
          rec.count() == 0, rec.events())

    # --- 静态：两处修复都还在源码里（防止以后被改回去）---
    py = io.open("dashboard.py", encoding="utf-8").read()
    check("toggle_pause 里有录制守卫",
          "if _macro_recording():" in py.split("def toggle_pause")[1][:1200])
    # 【v34 规格变更】宏搬到了自己的暂停分区（以前跟着 "mouse" 走，
    # 一按宏的启停键会把连点也定住）。前置检查自然也要看 macro 区。
    # 全局 Pause 仍能拦住回放：pausectl 的判定是「全局 OR 本区」。
    check("macro_play 里有暂停前置检查（v34 起看 macro 分区）",
          'pausectl.is_paused("macro")' in py.split("def macro_play")[1][:900])
    html = io.open("ui/dashboard.html", encoding="utf-8").read()
    check("applySettings 给顶层 hide 补了读者（不再是死键）",
          "s.hide != null" in html)


def main():
    print("=" * 56)
    print("v27 探针：S2 宏录制回放（录制器 / 回放 / 存储 / 接线）")
    print("=" * 56)

    tmpdir = tempfile.mkdtemp(prefix="v27probe_")
    try:
        test_recorder_keyboard()
        test_recorder_mouse()
        test_recorder_finish()
        test_recorder_real_hook()
        test_event_tools()
        test_macro_storage(tmpdir)
        test_replay()
        test_replay_engine()
        test_dashboard(tmpdir)
        test_static(tmpdir)
        test_negative(tmpdir)
        test_pause_conflict(tmpdir)
    finally:
        try:
            import dashboard as D
            if D._MACRO.get("rec") is not None and D._MACRO["rec"].running:
                D._MACRO["rec"].cancel()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
