# -*- coding: utf-8 -*-
"""
core/inputs.py —— 底层鼠标事件注入（Windows SendInput API）

为什么不用 pyautogui：
1. pyautogui 依赖 pymsgbox/pyscreeze/pillow 一堆包，打包体积大且容易漏收集；
2. SendInput 是 Windows 官方 API，事件能被游戏/程序当成真实输入识别；
3. ctypes 是标准库，零依赖，打出来的 exe 干净。

按键位掩码见 SendInput 文档：
    MOUSEEVENTF_MOVE      = 0x0001  移动
    MOUSEEVENTF_LEFTDOWN  = 0x0002  左键按下
    MOUSEEVENTF_LEFTUP    = 0x0004  左键抬起
    MOUSEEVENTF_RIGHTDOWN = 0x0008  右键按下
    MOUSEEVENTF_RIGHTUP   = 0x0010  右键抬起
    MOUSEEVENTF_MIDDLEDOWN= 0x0020  中键按下
    MOUSEEVENTF_MIDDLEUP  = 0x0040  中键抬起
    MOUSEEVENTF_ABSOLUTE  = 0x8000  坐标按屏幕绝对位置计算
    MOUSEEVENTF_WHEEL     = 0x0800  滚轮

屏幕绝对坐标要用 0~65535 的归一化值，不是像素：
    归一化X = 像素X * 65535 / (屏幕宽 - 1)
这是 SendInput 的老规矩，写错会导致鼠标飞到屏幕外。
"""

import ctypes
import ctypes.wintypes as wt

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
# 【v25 副屏坐标】绝对坐标默认只覆盖**主屏**。加上这个标志后，
# 0~65535 的归一化值覆盖**整个虚拟桌面**（所有显示器的外接矩形）——
# 副屏坐标、负坐标（副屏在主屏左侧时 x 为负）才能点得准。
# 必须和 virtual_screen() 配套用：只加标志不换分母，坐标会整体偏移。
MOUSEEVENTF_VIRTUALDESK = 0x4000

# GetSystemMetrics 的索引（虚拟桌面外接矩形）
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# 【注入标记】和 core/keyboard.py 的 INJECT_MARK 必须是同一个值。
# 键盘钩子靠它认出「自己注入的按键」并放行、不碰状态机；
# 鼠标这边必须对称：连点滴答的注入事件会顺着 Raw Input
# （RIDEV_INPUTSINK 什么都收）回到 _on_raw_mouse，
# 不过滤就会被触发键当成「用户又按了一次」——
# 表现为「点一下触发键只连点一次就停了」。
# 改这个值必须同步 keyboard.py，两边不一致等于没标记。
MARK_INJECTED = 0x4D414331          # "MAC1"

# Windows 只支持两个扩展鼠标键（XBUTTON1 / XBUTTON2），
# 编号通过 MOUSEINPUT.mouseData 传（1 / 2），不是 dwFlags。
# 游戏鼠标的 G3~G8 在这个 API 里**根本不存在**，注入不了——
# 所以调用方传别的编号进来必须直接报错，不能静默发一堆垃圾事件。
XBUTTON_MIN = 1
XBUTTON_MAX = 2

# 输入事件类型：0=鼠标 1=键盘 2=硬件
INPUT_MOUSE = 0

BUTTON_EVENTS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

BUTTON_LABELS = {
    "left": "左键",
    "right": "右键",
    "middle": "中键",
}


# --------------------------------------------------------------------------
# ctypes 结构体定义
# --------------------------------------------------------------------------
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    """鼠标输入事件体。字段顺序和类型不能改，对应 Win32 的 MOUSEINPUT。"""

    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    """
    键盘输入事件体，对应 Win32 的 KEYBDINPUT。

    【为什么这个类必须存在】
    INPUT 的联合体必须同时装下 MOUSEINPUT / KEYBDINPUT / HARDWAREINPUT，
    否则 sizeof(INPUT) 算不对（发送端 cbSize 会被 Windows 判非法，
    SendInput 直接返回 0，一个事件都发不出去）。
    """

    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """硬件输入事件体，对应 Win32 的 HARDWAREINPUT。"""

    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class INPUT(ctypes.Structure):
    """
    通用输入事件体：type 说明后面挂的是鼠标/键盘/硬件事件。

    【极其隐蔽的坑，务必保留ki 和 hi 两个字段】
    这个联合体**必须同时包含 mi / ki / hi 全部三个成员**。
    之前只写了 mi，导致：
        ev = INPUT(type=1)
        ev.ki = KEYBDINPUT(...)      # 不报错！
    ctypes 对不存在的联合体成员**不抛异常**，而是当成给 Python 对象
    挂了个临时属性 —— 赋值「看起来成功」，但数据一个字节都没写进内存。
    后果：每次 SendInput 发出去的都是「键盘类型 + 空按键」，
    SendInput 照样返回成功，而目标程序永远收不到按键。
    这就是「注入统计 100% 成功但屏幕一个字都没有」的真正原因。
    """

    class _U(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


class POINT(ctypes.Structure):
    """屏幕坐标点，用于 GetCursorPos。"""

    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


_user32 = ctypes.windll.user32

# --------------------------------------------------------------------------
# 必须声明精确签名，否则指针参数会被当成 32 位 int 传而溢出
# （ctypes 对未声明签名的函数默认用 C int 传参，64 位地址塞进去必然报
#  "int too long to convert"）
# --------------------------------------------------------------------------
_user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
_user32.SendInput.restype = wt.UINT

_user32.GetCursorPos.argtypes = [ctypes.c_void_p]
_user32.GetCursorPos.restype = wt.BOOL

_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int

_user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_user32.MapVirtualKeyW.restype = wt.UINT


# --------------------------------------------------------------------------
# 坐标与屏幕
# --------------------------------------------------------------------------
def get_cursor_pos():
    """取当前鼠标位置，返回 (x, y) 像素坐标。失败返回 None。"""
    pt = POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        return (pt.x, pt.y)
    return None


def screen_size():
    """取屏幕分辨率，返回 (宽, 高)。多显示器时取主屏。"""
    return (_user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))


def virtual_screen():
    """
    整个虚拟桌面的外接矩形，返回 (左, 上, 宽, 高)。

    【为什么需要它 · v25】
    `screen_size()` 只给主屏尺寸，而 SendInput 的绝对坐标默认也只铺主屏：
    副屏在主屏**左边**时它的 x 是负数，按主屏公式算出来是负归一化值，
    被 clamp 成 0 —— 表现为「固定坐标一设副屏，鼠标就飞到左上角」。
    要覆盖副屏必须同时做两件事：
        ① 归一化用虚拟桌面的宽高、减去虚拟桌面原点 (vx, vy)
        ② 发送时带 MOUSEEVENTF_VIRTUALDESK
    单做一样都会偏（这是 v22 遗留的已知限制，v25 补齐）。

    拿不到（驱动异常返回 0）时退回主屏，保证仍是「能用」而不是「乱飞」。
    """
    vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if vw <= 0 or vh <= 0:
        sw, sh = screen_size()
        return 0, 0, sw, sh
    return vx, vy, vw, vh


def _to_absolute(x, y):
    """把像素坐标换成 SendInput 要求的 0~65535 归一化值（按虚拟桌面）。"""
    vx, vy, vw, vh = virtual_screen()
    nx = int((x - vx) * 65535 / max(1, vw - 1))
    ny = int((y - vy) * 65535 / max(1, vh - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


# --------------------------------------------------------------------------
# 事件发送
# --------------------------------------------------------------------------
def _send(count, events):
    """把一组 (x, y, flags) 发给系统，返回成功注入的事件数。

    【dwExtraInfo 必须打标记】见 MARK_INJECTED 的说明：
    注入事件会被 Raw Input 原样收回，不带标记就会被触发键
    误判成用户操作，造成「连点自己把自己停掉」。

    【⚠ 字段名必须写 dx / dy，不能写 x / y（2026-10-18 实测踩过）】
    ctypes 的 Structure 对**不存在的字段关键字不报错**——写
    `MOUSEINPUT(x=100, y=200)` 构造「成功」、SendInput 也返回成功，
    但 dx/dy 始终是 0，表现为「鼠标每次都飞到屏幕左上角 (0,0) 去点」。
    点击类事件（不带坐标）看不出问题，只有移动类事件暴露，
    所以这个 bug 能一路活到用户手上。改字段名时务必同步 4 处构造点。
    """
    structures = []
    for x, y, flags in events:
        structures.append(
            INPUT(
                type=INPUT_MOUSE,
                mi=MOUSEINPUT(dx=x, dy=y, mouseData=0, dwFlags=flags,
                              time=0, dwExtraInfo=MARK_INJECTED),
            )
        )
    size = ctypes.sizeof(INPUT)
    array = (INPUT * len(structures))(*structures)
    sent = _user32.SendInput(count, ctypes.byref(array), size)
    # SendInput 返回 0 且 GetLastError 是 UIPI(0x5) 时，说明目标窗口权限更高
    return sent


def move_to(x, y):
    """把鼠标瞬移到指定像素坐标（虚拟桌面坐标，支持副屏/负坐标）。"""
    ax, ay = _to_absolute(x, y)
    return _send(1, [(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                      | MOUSEEVENTF_VIRTUALDESK)]) > 0


def click(button="left", count=1, interval_ms=40):
    """
    在当前鼠标位置点击指定次数。

    参数
    ----
    button      : 'left' / 'right' / 'middle'
    count       : 点击次数
    interval_ms : 多次点击之间的间隔（毫秒）
    """
    if button not in BUTTON_EVENTS:
        raise ValueError("不支持的按键: %r（可选 left/right/middle）" % (button,))
    down, up = BUTTON_EVENTS[button]
    ok = True
    for _ in range(count):
        # 按下和抬起必须成对、连续发，中间不要插别的鼠标事件
        ok = _send(2, [(0, 0, down), (0, 0, up)]) > 0 and ok
        if interval_ms > 0 and count > 1:
            import time

            time.sleep(interval_ms / 1000.0)
    return ok


def press_down(button="left"):
    """只按下不抬起。用于需要按住不放的场景（配合 release）。"""
    down, _ = BUTTON_EVENTS.get(button, BUTTON_EVENTS["left"])
    return _send(1, [(0, 0, down)]) > 0


def release(button="left"):
    """释放按键。"""
    _, up = BUTTON_EVENTS.get(button, BUTTON_EVENTS["left"])
    return _send(1, [(0, 0, up)]) > 0


# --------------------------------------------------------------------------
# 侧键（XBUTTON1 / XBUTTON2，即常见鼠标的 G1 / G2）
# --------------------------------------------------------------------------
def _xbutton_events(xbutton, down):
    """
    构造侧键事件。

    【两个曾经踩过的坑，都在这里修】
    1. 编号在 mouseData 里，不在 dwFlags 里。
       光设 dwFlags 不发编号，系统不知道该发哪个侧键。
    2. 按下/抬起的标志是 0x0080 / 0x0100。
       旧代码写成 0x0001 / 0x0002（那是左键标志），
       再或上 0x0004（左键抬起）——发出去的是一堆左键垃圾事件，
       游戏里看到的就是「拿侧键连点，实际在狂点左键」。
    """
    xbutton = int(xbutton)
    if not (XBUTTON_MIN <= xbutton <= XBUTTON_MAX):
        raise ValueError(
            "Windows 只支持 XBUTTON1/2（编号 1/2，即 G1/G2），"
            "收到编号 %d。G3 以上的按键 SendInput 注入不了。" % xbutton)
    flags = MOUSEEVENTF_XDOWN if down else MOUSEEVENTF_XUP
    return [(0, 0, flags, xbutton)]


def click_xbutton(xbutton):
    """侧键点击一次（按下+抬起成对，带注入标记）。"""
    events = _xbutton_events(xbutton, True) + _xbutton_events(xbutton, False)
    structures = []
    for x, y, flags, data in events:
        structures.append(INPUT(
            type=INPUT_MOUSE,
            mi=MOUSEINPUT(dx=x, dy=y, mouseData=data, dwFlags=flags,
                          time=0, dwExtraInfo=MARK_INJECTED)))
    array = (INPUT * len(structures))(*structures)
    return _user32.SendInput(len(structures), ctypes.byref(array),
                             ctypes.sizeof(INPUT)) > 0


def press_down_xbutton(xbutton):
    """侧键只按下不抬起（长按用）。"""
    events = _xbutton_events(xbutton, True)
    x, y, flags, data = events[0]
    ev = INPUT(type=INPUT_MOUSE,
               mi=MOUSEINPUT(dx=x, dy=y, mouseData=data, dwFlags=flags,
                             time=0, dwExtraInfo=MARK_INJECTED))
    return _user32.SendInput(1, ctypes.byref(ev),
                             ctypes.sizeof(INPUT)) > 0


def release_xbutton(xbutton):
    """侧键抬起。"""
    events = _xbutton_events(xbutton, False)
    x, y, flags, data = events[0]
    ev = INPUT(type=INPUT_MOUSE,
               mi=MOUSEINPUT(dx=x, dy=y, mouseData=data, dwFlags=flags,
                             time=0, dwExtraInfo=MARK_INJECTED))
    return _user32.SendInput(1, ctypes.byref(ev),
                             ctypes.sizeof(INPUT)) > 0


def drag(from_xy, to_xy, steps=20, step_delay_ms=10):
    """
    从 from_xy 拖到 to_xy（模拟按住左键拖动）。

    步骤：移到起点 → 按下 → 分步移动 → 抬起。
    """
    import time

    move_to(*from_xy)
    time.sleep(0.03)
    press_down("left")
    time.sleep(0.03)
    fx, fy = from_xy
    tx, ty = to_xy
    for i in range(1, max(1, steps) + 1):
        x = fx + (tx - fx) * i / steps
        y = fy + (ty - fy) * i / steps
        move_to(int(x), int(y))
        time.sleep(step_delay_ms / 1000.0)
    time.sleep(0.03)
    return release("left")


def scroll(amount=1):
    """
    滚轮滚动，正数向上、负数向下，每次一格。

    注意：滚轮事件靠 mouseData 字段传滚轮格数（不是坐标），
    所以这里不能复用 _send，要单独构造。
    """
    data = int(amount) * 120  # WHEEL_DELTA = 120
    event = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=data, dwFlags=MOUSEEVENTF_WHEEL,
                      time=0, dwExtraInfo=MARK_INJECTED),
    )
    return _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) > 0


def scroll_horizontal(amount=1):
    """
    横向滚轮（倾斜滚轮/触控板左右滑），正数向右、负数向左。

    【为什么单独一个函数】MOUSEEVENTF_HWHEEL 和 MOUSEEVENTF_WHEEL 是两个
    不同的标志位，同一个 delta 走错标志就变成"上下滚"——在 Excel 里
    想左右翻列，结果整页上下跑。宏回放要忠实还原，所以必须有这一条。
    """
    data = int(amount) * 120
    event = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=data,
                      dwFlags=MOUSEEVENTF_HWHEEL,
                      time=0, dwExtraInfo=MARK_INJECTED),
    )
    return _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) > 0