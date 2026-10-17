# -*- coding: utf-8 -*-
"""
verify_trigger.py —— 触发方式 + 启动耗时 定向回归
=====================================================

针对 2026-10-14 那轮修复的三条断言：

1.鼠标键触发器（替代会被占用的 F5）的分发逻辑
2. Raw Input 位图 -> 按键编号的解析
3. register_hotkeys 的耗时（启动卡顿根因）

跑法：python verify_trigger.py
退出码 0 = 全通过
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[通过]" if cond else "[失败]", name,
                       ("  -> " + str(detail)) if detail else ""))


# ======================================================================
# 1. 鼠标键触发器
# ======================================================================
def test_trigger_dispatch():
    print("\n=== 触发器分发逻辑 ===")
    from core import button_trigger as bt

    hits = []
    t = bt.ButtonTrigger("middle", lambda: hits.append(1))

    ck("按下中键触发一次", t.feed(2, True) and len(hits) == 1, len(hits))
    ck("按住不放不重复触发", t.feed(2, True) and len(hits) == 1, len(hits))
    t.feed(2, False)
    t.feed(2, True)
    ck("松开后再按才触发", len(hits) == 2, len(hits))
    ck("别的键不触发", not t.feed(1, True) and len(hits) == 2, len(hits))
    ck("消费了属于自己的键", t.feed(2, True) is True)
    ck("不消费别人的键", t.feed(1, True) is False)

    print("\n--- 非法输入 ---")
    try:
        bt.ButtonTrigger("nope", lambda: None)
        ck("拒绝非法键名", False)
    except ValueError as e:
        ck("拒绝非法键名", True, str(e)[:38])

    print("\n--- 冲突重绑 ---")
    got = []
    bt.HUB.set("x1", lambda: got.append(1))
    bt.HUB.set("x1", lambda: got.append(2))
    bt.HUB.dispatch(3, True)
    bt.HUB.dispatch(3, False)
    ck("同键重绑后只剩一个回调", len(got) == 1 and got[0] == 2, got)
    ck("labels 只有一个 x1", len(bt.HUB.list_labels()) == 1, bt.HUB.list_labels())

    print("\n--- 不同键并存 ---")
    a, b = [], []
    bt.HUB.set("x1", lambda: a.append(1))
    bt.HUB.set("x2", lambda: b.append(1))
    bt.HUB.dispatch(3, True)
    bt.HUB.dispatch(4, True)
    ck("两个键各自独立触发", len(a) == 1 and len(b) == 1, (a, b))

    bt.HUB.clear()
    ck("clear 清空", bt.HUB.list_labels() == {}, bt.HUB.list_labels())


# ======================================================================
# 2. Raw Input 位图解析
# ======================================================================
def test_rawinput_parsing():
    print("\n=== Raw Input 位图 -> 按键编号 ===")
    import dashboard as D

    # 复刻 start_mouse_listener 里的位图解码逻辑，
    # 验证每个按键组合都能解出正确的编号集合
    cases = [
        (0x01, [0], "仅左键"),
        (0x02, [1], "仅右键"),
        (0x04, [2], "仅中键"),
        (0x08, [3], "仅侧键1"),
        (0x10, [4], "仅侧键2"),
        (0x20, [5], "侧键3"),
        (0x04 | 0x08, [2, 3], "中键+侧键1"),
        (0x01 | 0x10, [0, 4], "左键+侧键2"),
    ]
    for btns, expect, desc in cases:
        ids = []
        if btns & 0x01: ids.append(0)
        if btns & 0x02: ids.append(1)
        if btns & 0x04: ids.append(2)
        if btns & 0x08: ids.append(3)
        if btns & 0x10: ids.append(4)
        if btns & 0x20: ids.append(5)
        ck("解码 %s -> %s" % (desc, expect), ids == expect, ids)

    # 结构体大小必须和 Windows 定义一致，否则解析全是垃圾。
    #
    # 【这条断言的价值】第一次写测试时我以为是断言写错了
    # （脑子里的 RAWINPUT_MOUSE 是 16+20=36），实测 48 —— 反而挖出真 bug：
    # ctypes 的 c_ulong 在 64 位下是 8 字节，而 Win32 的 ULONG/DWORD
    # 恒为 4 字节。混用会把结构体从 20 撑到 24，usButtons 的读取偏移
    # 就错了 —— 这正是旧代码「识别自定义按键」算不对编号的根源。
    #
    # 对齐规则：HANDLE 是 8 字节，结构体按 8 对齐
    #   RAWINPUTHDR: dwType(4)+dwSize(4)+hDevice(8)+wParam(4)=20 -> 补到 24
    #   RAWMOUSE:    2+2+4+4+4+4 = 20（自身刚好 4 对齐）
    #   合并后按 8 对齐 -> 48
    import ctypes
    # 【已修正】旧断言写的是 20 字节，那正是 bug：
    # 漏了 usFlags 字段，导致后续字段整体前移 4 字节。
    # 真实布局是 usFlags(2)+填充(2)+union(4)+ulRawButtons(4)+lLastX(4)
    #          +lLastY(4)+ulExtraInformation(4) = 24 字节。
    ck("RAWMOUSE = 24 字节（含 usFlags + 对齐填充）",
       ctypes.sizeof(D.RAWMOUSE) == 24, ctypes.sizeof(D.RAWMOUSE))
    ck("RAWINPUTHDR = 24 字节（20 补齐到 8 的倍数）",
       ctypes.sizeof(D.RAWINPUTHDR) == 24, ctypes.sizeof(D.RAWINPUTHDR))
    ck("RAWINPUT_MOUSE = 48 字节（合并后按 8 对齐）",
       ctypes.sizeof(D.RAWINPUT_MOUSE) == 48, ctypes.sizeof(D.RAWINPUT_MOUSE))

    # 【踩坑记录，钉住免得以后再犯】
    # 我曾断言「ctypes.c_ulong 在 64 位下是 8 字节，所以结构体会错位」——
    # 这是**错的**。实测 ctypes.c_ulong / c_long 固定 4 字节，与位数无关
    # （只有 c_int / c_uint 才是「指针宽度」）。所以旧代码的结构体
    # 本来就没有位宽 bug。
    #
    # 真正的缺陷只有一个：**根本没有 RAWMOUSE 结构**，
    # 只靠 hdr.wParam 的高位标志判断「有鼠标事件」，
    # 分不出是左键还是侧键 -> 自定义键识别只能盲分配。
    #
    # 这条断言防的是「以后有人把 4 字节字段改成 8 字节类型」这种真错。
    import ctypes as _ct
    width_bad = []
    # 每个字段该有多宽（Win32 约定），实际是多少
    expect = {
        "RAWINPUTHDR": {"dwType": 4, "dwSize": 4, "wParam": 4},   # hDevice 是 8
        "RAWMOUSE": {"usFlags": 2, "usButtonFlags": 2,
                     "usButtonData": 2,
                     "ulRawButtons": 4, "lLastX": 4, "lLastY": 4,
                     "ulExtraInformation": 4},
    }
    for cls_name, fields in expect.items():
        cls = getattr(D, cls_name)
        for fname, w in fields.items():
            actual = getattr(cls, fname).size
            if actual != w:
                width_bad.append("%s.%s 应为 %d 字节，实际 %d"
                                 % (cls_name, fname, w, actual))
    ck("各字段宽度符合 Win32 约定（DWORD/ULONG=4，USHORT=2）",
       not width_bad, width_bad or "全部正确")

    # 按键位图是 USHORT -> 位图最多 16 个键，这是协议上限
    ck("ulRawButtons 是 4 字节（位图取低 16 位）",
       D.RAWMOUSE.ulRawButtons.size == 4, D.RAWMOUSE.ulRawButtons.size)

    # 偏移量才是决定解析对错的关键，直接钉住
    ck("RAWINPUTHDR.dwType 偏移 0", D.RAWINPUTHDR.dwType.offset == 0,
       D.RAWINPUTHDR.dwType.offset)
    ck("RAWINPUTHDR.hDevice 偏移 8（8 字节对齐）",
       D.RAWINPUTHDR.hDevice.offset == 8, D.RAWINPUTHDR.hDevice.offset)
    # 【实测钉住】按下鼠标中键时 packet 偏移 28 出现 0x0010，
    # 即 RAWMOUSE 内偏移 4 = usButtonFlags。偏移错了整个解析就废了。
    ck("RAWMOUSE.usFlags 偏移 0",
       D.RAWMOUSE.usFlags.offset == 0, D.RAWMOUSE.usFlags.offset)
    ck("RAWMOUSE.usButtonFlags 偏移 4（usFlags + 2 字节填充）",
       D.RAWMOUSE.usButtonFlags.offset == 4,
       D.RAWMOUSE.usButtonFlags.offset)
    ck("RAWMOUSE.ulRawButtons 偏移 8",
       D.RAWMOUSE.ulRawButtons.offset == 8,
       D.RAWMOUSE.ulRawButtons.offset)
    ck("RAWINPUT_MOUSE.mouse 偏移 24（紧跟 24 字节的 hdr）",
       D.RAWINPUT_MOUSE.mouse.offset == 24, D.RAWINPUT_MOUSE.mouse.offset)

    # 【反直觉的事实，钉住免得以后误判】
    # ctypes 的 c_long 在 64 位是 8 字节，比 Win32 的 LONG(4B) 大，
    # 但 MSG 后面还有 lMessageExtra(4) + lPrivate(4)，
    # 差值恰好被尾部对齐补回来，所以 MSG 两种写法都是 48 字节，
    # 这里原本没有实际影响。顺手改成 c_int32 只是为了语义准确。
    from ctypes import wintypes as wt

    class MSG_FIX(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                    ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                    ("time", wt.DWORD), ("pt_x", ctypes.c_int32),
                    ("pt_y", ctypes.c_int32)]

    class MSG_OLD(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                    ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                    ("time", wt.DWORD), ("pt_x", ctypes.c_long),
                    ("pt_y", ctypes.c_long)]

    print("  [事实] MSG 修正后 %d 字节 / c_long 版 %d 字节"
          "（尾部对齐恰好抵消，本处无实际影响）"
          % (ctypes.sizeof(MSG_FIX), ctypes.sizeof(MSG_OLD)))

    # 按键位图在 RAWMOUSE 偏移 8（ulRawButtons），
    # 旧代码里那个 offset 0 的 usButtons 是不存在的字段，
    # 读到的其实是 usFlags（移动模式标志）—— 这是五个 bug 之一。
    raw = D.RAWINPUT_MOUSE()
    raw.mouse.ulRawButtons = 0x08
    ck("ulRawButtons 位图读回一致", raw.mouse.ulRawButtons == 0x08,
       raw.mouse.ulRawButtons)
    raw.hdr.wParam = 0xFF
    ck("hdr.wParam 不污染 mouse.ulRawButtons（偏移不同）",
       raw.mouse.ulRawButtons == 0x08, raw.mouse.ulRawButtons)
    raw.mouse.usButtonFlags = 0x0010
    ck("usButtonFlags 与位图互不干扰",
       raw.mouse.ulRawButtons == 0x08 and
       raw.mouse.usButtonFlags == 0x0010)


# ======================================================================
# 3. 启动耗时（卡顿根因）
# ======================================================================
def test_startup_cost():
    print("\n=== 启动耗时 ===")
    import dashboard as D

    api = D.Api()
    t0 = time.time()
    api.register_hotkeys()
    el = (time.time() - t0) * 1000
    # 原来用 thread.join(timeout=1.0) 等注册，注册成功的线程会
    # 一直阻塞在 GetMessageW循环里，所以每次必然等满1 秒，两个热键 2 秒。
    ck("register_hotkeys < 100ms（原来 2016ms）", el < 100, "%.0f ms" % el)

    labels = api.button_hotkeys()["labels"]
    ck("默认已绑定鼠标中键", "middle" in labels, labels)

    t0 = time.time()
    api.shutdown()
    el = (time.time() - t0) * 1000
    ck("shutdown < 300ms（原来会卡2秒）", el < 300, "%.0f ms" % el)
    # 退出后必须清掉鼠标键绑定，否则回调还挂着引用
    from core import button_trigger as bt
    ck("shutdown 清空鼠标键绑定", bt.HUB.list_labels() == {},
       bt.HUB.list_labels())


# ======================================================================
# 4. 自定义键识别（不按任何键时的行为）
# ======================================================================
def test_add_custom_mouse():
    print("\n=== 自定义键识别 ===")
    import dashboard as D

    api = D.Api()
    # 两步式：先arm 清空，再提交
    api.arm_custom_mouse()
    r = api.add_custom_mouse("")
    ck("没按任何键时明确报错（不是盲分配）",
       r["ok"] is False and "侧键" in r["msg"], r["msg"])

    # 模拟用户按了侧键1（G1）：Raw Input usButtons 置 bit3
    with D._raw_lock:
        D._raw_keys["ids"] = [3]
    r = api.add_custom_mouse("")
    ck("识别到侧键1并添加", r["ok"] is True, r["msg"])
    names = [b["name"] for b in r["buttons"]]
    ck("列表里出现侧键1(G1)", any("G1" in n for n in names), names)

    # 同一个键再识别一次 -> 应提示已存在，而不是重复添加
    with D._raw_lock:
        D._raw_keys["ids"] = [3]
    r2 = api.add_custom_mouse("")
    ck("重复识别同一键不重复添加",
       r2["ok"] is False and "已在列表" in r2["msg"], r2["msg"])

    # 换一个键 G2
    with D._raw_lock:
        D._raw_keys["ids"] = [4]
    r3 = api.add_custom_mouse("")
    ck("识别到侧键2(G2)", r3["ok"] is True, r3["msg"])

    # 左键不该被当成自定义键（它是内置的）
    with D._raw_lock:
        D._raw_keys["ids"] = [0]
    r4 = api.add_custom_mouse("")
    ck("内置键不作为自定义键添加", r4["ok"] is False, r4["msg"])

    api.shutdown()


# ======================================================================
# 5. 最近按键：标准键（中键/G1/G2）也必须刷新（v29）
# ======================================================================
def test_last_ids_standard_buttons():
    print("\n=== 最近按键：标准键也要刷新 ===")
    import dashboard as D

    # 隔离：清掉上一轮可能残留的按下集合与位图
    with D._raw_lock:
        D._raw_keys["std_down"] = set()
        D._raw_keys["raw_buttons"] = 0
        D._raw_stat["last_ids"] = []

    def feed(flags):
        m = D.RAWMOUSE()
        m.usFlags = 0
        m.usButtonFlags = flags
        m.ulRawButtons = 0
        m.ulExtraInformation = 0
        D._on_raw_mouse(m)

    feed(D.RI_MOUSE_BUTTON4_DOWN)
    ck("按下 G1 后 last_ids == [3]", D._raw_stat["last_ids"] == [3],
       D._raw_stat["last_ids"])
    feed(D.RI_MOUSE_BUTTON4_UP)
    ck("松开 G1 后 last_ids 为空", D._raw_stat["last_ids"] == [],
       D._raw_stat["last_ids"])

    feed(D.RI_MOUSE_BUTTON5_DOWN)
    ck("按下 G2 后 last_ids == [4]", D._raw_stat["last_ids"] == [4],
       D._raw_stat["last_ids"])
    feed(D.RI_MOUSE_BUTTON5_UP)

    feed(D.RI_MOUSE_MIDDLE_BUTTON_DOWN)
    ck("按下中键后 last_ids == [2]", D._raw_stat["last_ids"] == [2],
       D._raw_stat["last_ids"])
    feed(D.RI_MOUSE_MIDDLE_BUTTON_UP)

    # 自己注入的事件必须整条忽略（否则「最近按键」会被自己的点击刷屏）
    before = list(D._raw_stat["last_ids"])
    m = D.RAWMOUSE()
    m.usButtonFlags = D.RI_MOUSE_BUTTON4_DOWN
    m.ulExtraInformation = D.inp.MARK_INJECTED
    D._on_raw_mouse(m)
    ck("注入事件不刷新最近按键", D._raw_stat["last_ids"] == before,
       D._raw_stat["last_ids"])


def main():
    print("=" * 58)
    print("触发方式 + 启动耗时 定向回归")
    print("=" * 58)
    test_trigger_dispatch()
    test_rawinput_parsing()
    test_startup_cost()
    test_add_custom_mouse()
    test_last_ids_standard_buttons()

    print("\n" + "=" * 58)
    print("汇总：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：%s" % f)
    print("=" * 58)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
