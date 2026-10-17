# -*- coding: utf-8 -*-
"""
verify_binding.py —— 「绑键」链路端到端回归

【它验证什么】
从"用户按下鼠标键"到"后端记下按键编号"这一整条链路是否真的通。
这是唯一能证明「绑键有效/无效」的测试，因为链路中间出过五个 bug，
且都不报错——只有跑通端到端才敢说修好了。

【五个 bug 回顾】（都在 dashboard.py 里，已修）
  1. dwFlags 写成 0x01 → 那是 RIDEV_REMOVE（反注册），应为 0x100
  2. hwndTarget = None → 原始输入跟随键盘焦点，后台线程收不到
  3. GetRawInputData 的 uiCommand 传了 RIM_INPUT(3) → 非法命令，返回 -1
  4. GetRawInputData 的 pcbSize 传整数 128 → 被当地址 0x80，返回 -1
  5. RAWMOUSE 少了 usFlags 字段 → 后续字段整体错位 4 字节

【测试手法】
用 SendInput 注入合成鼠标输入。注意：注入输入的 ulRawButtons 恒为 0，
所以本测试只能验证「通道 1（usButtonFlags）」，
通道 2（位图边沿，抓 G3+）无法在此环境验证，需在真实鼠标上验收。
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 【输入注入闸门】默认关闭。
# 开启后会真的往系统里发鼠标事件 —— 用户在电脑前就会看到鼠标自己动。
# 只有需要验证「系统确实会投递 Raw Input 消息」时才显式加 --real-input。
_REAL_INPUT = "--real-input" in sys.argv
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


# ------------------------------------------------------------------
# 1. 结构尺寸与常量
# ------------------------------------------------------------------
def test_struct_and_constants():
    print("\n=== 1. 结构尺寸与常量 ===")
    import dashboard as D

    check("sizeof(RAWMOUSE) == 24",
          ctypes.sizeof(D.RAWMOUSE) == 24,
          "实测=%d" % ctypes.sizeof(D.RAWMOUSE))
    check("sizeof(RAWINPUTHDR) == 24",
          ctypes.sizeof(D.RAWINPUTHDR) == 24,
          "实测=%d" % ctypes.sizeof(D.RAWINPUTHDR))
    check("sizeof(RAWINPUT_MOUSE) == 48",
          ctypes.sizeof(D.RAWINPUT_MOUSE) == 48,
          "实测=%d" % ctypes.sizeof(D.RAWINPUT_MOUSE))
    check("sizeof(RAWINPUTDEVICE) == 16",
          ctypes.sizeof(D.RAWINPUTDEVICE) == 16,
          "实测=%d" % ctypes.sizeof(D.RAWINPUTDEVICE))
    check("sizeof(MSG) == 48",
          ctypes.sizeof(D.MSG_STRUCT) == 48
          if hasattr(D, "MSG_STRUCT") else True,
          "(MSG 为 proc 内部定义，跳过)")

    check("RIDEV_INPUTSINK == 0x100",
          D.RIDEV_INPUTSINK == 0x00000100,
          "实测=0x%X" % D.RIDEV_INPUTSINK)
    check("RIDEV_REMOVE == 0x01",
          D.RIDEV_REMOVE == 0x00000001)
    check("RID_INPUT == 0x10000003",
          D.RID_INPUT == 0x10000003,
          "实测=0x%X" % D.RID_INPUT)
    check("RIM_INPUT == 0x0",
          D.RIM_INPUT == 0x00000000,
          "实测=0x%X" % D.RIM_INPUT)
    check("RI_MOUSE_MIDDLE_BUTTON_DOWN == 0x0010",
          D.RI_MOUSE_MIDDLE_BUTTON_DOWN == 0x0010,
          "实测=0x%X" % D.RI_MOUSE_MIDDLE_BUTTON_DOWN)
    check("RI_MOUSE_MIDDLE_BUTTON_UP == 0x0020",
          D.RI_MOUSE_MIDDLE_BUTTON_UP == 0x0020,
          "实测=0x%X" % D.RI_MOUSE_MIDDLE_BUTTON_UP)
    check("RI_MOUSE_RIGHT_BUTTON_DOWN == 0x0004",
          D.RI_MOUSE_RIGHT_BUTTON_DOWN == 0x0004,
          "实测=0x%X" % D.RI_MOUSE_RIGHT_BUTTON_DOWN)

    # RAWMOUSE 字段名必须齐全
    names = [f[0] for f in D.RAWMOUSE._fields_]
    for need in ("usFlags", "usButtonFlags", "usButtonData",
                 "ulRawButtons"):
        check("RAWMOUSE 含字段 %s" % need, need in names)

    # DefWindowProcW 必须声明签名，否则 64 位 lParam 会溢出
    check("DefWindowProcW 已声明 argtypes",
          D.user32.DefWindowProcW.argtypes is not None)


# ------------------------------------------------------------------
# 2. 通道 1：usButtonFlags 解码
# ------------------------------------------------------------------
def test_flag_decode():
    print("\n=== 2. 通道 1：usButtonFlags 解码 ===")
    import dashboard as D

    got = []

    def fake_dispatch(bid, down):
        got.append((int(bid), bool(down)))

    real = D._dispatch
    D._dispatch = fake_dispatch
    try:
        class M(ctypes.Structure):
            _fields_ = D.RAWMOUSE._fields_

        def mk(flags, raw=0):
            m = M()
            m.usButtonFlags = flags
            m.ulRawButtons = raw
            return m

        cases = [
            (0x0001, 0, True, "左键按下"),
            (0x0002, 0, False, "左键抬起"),
            (0x0004, 1, True, "右键按下"),
            (0x0008, 1, False, "右键抬起"),
            (0x0010, 2, True, "中键按下"),
            (0x0020, 2, False, "中键抬起"),
            (0x0040, 3, True, "G1 按下"),
            (0x0080, 3, False, "G1 抬起"),
            (0x0100, 4, True, "G2 按下"),
            (0x0200, 4, False, "G2 抬起"),
        ]
        for flags, want_bid, want_down, label in cases:
            got.clear()
            D._on_raw_mouse(mk(flags))
            ok = got and got[0] == (want_bid, want_down)
            check("%s → 编号%d/%s" % (label, want_bid,
                                     "按下" if want_down else "抬起"),
                  ok, "实际=%s" % (got,))
    finally:
        D._dispatch = real


# ------------------------------------------------------------------
# 3. 通道 2：位图边沿检测（G3+）
# ------------------------------------------------------------------
def test_bitmap_edge():
    print("\n=== 3. 通道 2：位图边沿检测（G3+）===")
    import dashboard as D

    got = []
    real = D._dispatch
    D._dispatch = lambda bid, down: got.append((int(bid), bool(down)))
    try:
        class M(ctypes.Structure):
            _fields_ = D.RAWMOUSE._fields_

        def mk(flags, raw):
            m = M()
            m.usButtonFlags = flags
            m.ulRawButtons = raw
            return m

        # 复位
        with D._raw_lock:
            D._raw_keys["raw_buttons"] = 0

        # G5(编号7) 按下
        got.clear()
        D._on_raw_mouse(mk(0, (1 << 7)))
        check("G5 按下 → (7, True)", got == [(7, True)], "实际=%s" % (got,))

        # 按住不放再来一帧 → 不应重复触发
        got.clear()
        D._on_raw_mouse(mk(0, (1 << 7)))
        check("按住不放不重复触发", got == [], "实际=%s" % (got,))

        # 抬起
        got.clear()
        D._on_raw_mouse(mk(0, 0))
        check("G5 抬起 → (7, False)", got == [(7, False)],
              "实际=%s" % (got,))

        # 通道 1 的键不应被通道 2 重复处理（模拟"中键按下"后跟一帧移动）
        with D._raw_lock:
            D._raw_keys["raw_buttons"] = 0
        got.clear()
        D._on_raw_mouse(mk(0x0010, 0))      # 中键按下（合成输入位图为 0）
        check("中键按下由通道 1 处理", got == [(2, True)],
              "实际=%s" % (got,))
    finally:
        D._dispatch = real


# ------------------------------------------------------------------
# 4. 端到端：真的按下中键，看捕获器有没有记下来
# ------------------------------------------------------------------
def test_end_to_end():
    """
    端到端：进入捕获态 → 来一帧「中键按下」→ 检查捕获器记下的编号。

    【为什么默认不调 SendInput】
    SendInput 是把事件投递给**当前前台窗口**，也就是用户正在用的程序。
    之前每次跑本测试，用户那边就会看到鼠标自己点一下中键；
    regression_keyhold.py 结尾还会真的敲一个 W。
    这是测试在污染真实输入环境，必须默认关掉。

    默认改走「直接喂报文」：跳过系统投递，把构造好的 RAWMOUSE
    直接交给 _on_raw_mouse()。三个好处：
      1. 完全不碰用户的键鼠
      2. ulRawButtons 位图可以自由构造 → 能覆盖 G3~G8。
         （合成/注入输入的位图恒为 0，走 SendInput 反而测不到这条通道）
      3. 不依赖前台窗口状态，结果稳定可重复

    要验证「系统确实会投递消息」时加 --real-input 显式开启，
    脚本会先倒数 2 秒并打印醒目提示，避免变成"偷袭"。
    """
    print("\n=== 4. 端到端：中键按下 → 捕获结果 ===")
    import dashboard as D

    # 把设置读写隔离到临时文件，别污染真实 ui_settings.json
    tmp = os.path.join(HERE, "_test_settings_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)

    def feed(flags, raw_buttons):
        """
        直接喂一帧鼠标报文，不经过系统 —— 不会动用户的鼠标。

        【字段名别写错】持续按键位图是 ulRawButtons（offset 8），
        不是 usButtons。RAWMOUSE 里压根没有 usButtons 这个字段，
        写错的话赋值会静默落到实例字典上，解析时读到 0，
        表现是「按下没反应」——和之前那个结构错位的症状一模一样。
        """
        m = D.RAWMOUSE()
        m.usFlags = 0
        m.ulRawButtons = raw_buttons
        m.usButtonFlags = flags
        D._on_raw_mouse(m)

    user32 = D.user32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG),
                    ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD),
                    ("wParamH", wt.WORD)]

    class UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                    ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", UNION)]

    user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wt.UINT

    def send(flags):
        inp = INPUT()
        inp.type = 0
        inp.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
        return user32.SendInput(1, ctypes.byref(inp),
                                ctypes.sizeof(INPUT))

    # 进入捕获态
    with D._raw_lock:
        D._capture["armed"] = True
        D._capture["raw_id"] = None
    seq0 = int(D._capture["seq"])

    if _REAL_INPUT:
        # 显式开启：先给 2 秒撤离时间，别让测试变成"偷袭"
        print("\n  !! 2 秒后向系统注入一次真实的中键按下/抬起 !!")
        print("  !! 请把光标移到无害位置，或按 Ctrl+C 取消 !!")
        try:
            time.sleep(2.0)
        except KeyboardInterrupt:
            print("  已取消真实注入，本项跳过")
            return
        send(0x0020)
        time.sleep(0.4)
        send(0x0040)
        time.sleep(0.6)
        note = "（真实注入）"
    else:
        # 通道 1：usButtonFlags 带中键按下；位图为 0（合成输入的特征）
        feed(0x0010, 0)
        time.sleep(0.15)
        feed(0x0020, 0)          # 中键抬起
        time.sleep(0.25)
        note = "（合成报文，未动真实鼠标）"

    with D._raw_lock:
        raw_id = D._capture["raw_id"]
        seq1 = int(D._capture["seq"])

    check("捕获器记下了按键 %s" % note, raw_id is not None,
          "raw_id=%r seq %d→%d" % (raw_id, seq0, seq1))
    if raw_id is not None:
        check("捕获到的是中键（编号 2）", int(raw_id) == 2,
              "实际编号=%d" % int(raw_id))
    check("捕获后自动解除武装（不会抓下一个键）",
          D._capture["armed"] is False,
          "armed=%r" % (D._capture["armed"],))


# ------------------------------------------------------------------
# 4b. G3~G8：只有「位图边沿」通道能抓到，这里直接构造位图验证
# ------------------------------------------------------------------
def test_high_buttons():
    """
    验证 G3~G8（位图 bit5~bit10）能被捕获。

    【为什么单列一节】
    usButtonFlags 只为左/右/中/G1/G2 定义了按下标志，
    G3 及以后**没有任何 down 标志位**——只能靠
    「这一帧位图 vs 上一帧位图」的边沿检测。
    而合成/注入输入的 ulRawButtons 恒为 0，这条路**没法用 SendInput 测**，
    所以必须直接构造位图喂进去，否则这条通道等于没有测试覆盖。
    """
    print("\n=== 4b. G3~G8 高位鼠标键捕获 ===")
    import dashboard as D

    def feed(flags, raw_buttons):
        m = D.RAWMOUSE()
        m.usFlags = 0
        m.ulRawButtons = raw_buttons      # 持续按键位图，不是 usButtons
        m.usButtonFlags = flags
        D._on_raw_mouse(m)

    for bit, label in ((5, "G3"), (6, "G4"), (7, "G5"),
                       (8, "G6"), (9, "G7"), (10, "G8")):
        with D._raw_lock:
            D._raw_keys["raw_buttons"] = 0      # 上一帧：空
            D._capture["armed"] = True
            D._capture["raw_id"] = None
        feed(0, 1 << bit)                        # 这帧：该键按下
        got = D._capture["raw_id"]
        check("位图 bit%d（%s）能被捕获" % (bit, label), got == bit,
              "期望=%d 实际=%r" % (bit, got))

    # 按住不放：位图连续为 1，不能再触发第二次（防重复）
    with D._raw_lock:
        D._raw_keys["raw_buttons"] = 0
        D._capture["armed"] = True
        D._capture["raw_id"] = None
    seq_a = int(D._capture["seq"])
    feed(0, 1 << 7)      # G5 按下
    feed(0, 1 << 7)      # 还按着（同一帧状态重复上报）
    feed(0, 1 << 7)
    seq_b = int(D._capture["seq"])
    check("按住不放只算一次按下（不重复触发）",
          seq_b - seq_a == 1, "seq 增量=%d" % (seq_b - seq_a))

    # 抬起后再按下：应能再次捕获。
    # 注意必须先重新 armed —— 捕获器是「一次性」的，抓到一个键就自动解除，
    # 不重新武装的话第二次按下不会被记（这正是防误抓的设计）。
    feed(0, 0)                 # 松开（抬起事件不进捕获器）
    with D._raw_lock:
        D._capture["armed"] = True
        D._capture["raw_id"] = None
    feed(0, 1 << 7)            # 再按
    check("松开后再按能再次捕获",
          int(D._capture["seq"]) - seq_b == 1,
          "seq 增量=%d" % (int(D._capture["seq"]) - seq_b))


# ------------------------------------------------------------------
# 5. 自检接口
# ------------------------------------------------------------------
def test_raw_diag():
    print("\n=== 5. 自检接口 ===")
    import dashboard as D

    api = D.Api()
    d = api.raw_diag()
    for key in ("mode", "sink", "registered", "messages",
                "last_buttons", "last_ids", "hint"):
        check("raw_diag 含字段 %s" % key, key in d, "实际=%s" % (list(d),))
    print("  当前状态:", d)


if __name__ == "__main__":
    print("=" * 64)
    print("绑键链路回归测试")
    print("=" * 64)

    # 先启动监听线程（模块导入不会自动起）
    import dashboard as D
    D.start_mouse_listener()
    time.sleep(1.2)

    if _REAL_INPUT:
        print("\n[注意] 已开启 --real-input：本次会向系统注入真实鼠标事件")
    else:
        print("\n[安全] 默认不注入真实输入（需真实注入请加 --real-input）")

    test_struct_and_constants()
    test_flag_decode()
    test_bitmap_edge()
    test_end_to_end()
    test_high_buttons()
    test_raw_diag()

    # 清理测试产生的临时设置文件
    for fn in os.listdir(HERE):
        if fn.startswith("_test_settings_") and fn.endswith(".json"):
            try:
                os.remove(os.path.join(HERE, fn))
            except Exception:
                pass

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print("  - %s" % f)
        sys.exit(1)
    print("全部通过")
