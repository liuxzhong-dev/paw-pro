# -*- coding: utf-8 -*-
"""
注入诊断 v3 —— 全自动，消息循环在主线程
=======================================

v2 为什么还是「未响应」（踩坑记录，勿回退）
-------------------------------------------
1. 【致命】消息循环跑在**后台线程**，而主线程阻塞在 `input()`。
   Windows 的「未响应」判定看的是**拥有主窗口的那个线程**。
   窗口建在后台线程上，主线程被 input 卡住 -> 进程被判死 ->
   弹「未响应」，WM_KEYDOWN=0 / WM_INPUT=0 因为消息根本没被泵。
   v3 起：消息循环必须在**主线程**，且全程零 input()。

2. `FillRect(hdc, None, brush)` 会 access violation。
   本机 ctypes 绑定下结构体参数不能传 NULL。
   -> 先 GetClientRect 拿客户区，再 FillRect(hdc, byref(client), brush)。

3. Win32 API 必须声明精确 argtypes，且 DLL 不能挂错（本机实测）：
   user32:  RegisterClassW CreateWindowExW DefWindowProcW DestroyWindow
            ShowWindow UpdateWindow InvalidateRect PostMessageW
            GetForegroundWindow SetForegroundWindow GetWindowThreadProcessId
            AttachThreadInput GetWindowTextW GetClientRect BeginPaint
            EndPaint FillRect DrawTextW LoadIconW LoadCursorW
   gdi32:   SelectObject SetBkMode DeleteObject CreateFontW GetStockObject
   kernel32: GetCurrentThreadId GetModuleHandleW GetLastError
   不声明 argtypes 时 ctypes 默认按32 位传参 -> 64 位指针溢出
   -> ctypes.ArgumentError: argument 4: int too long to convert

v3 行为
-------
双击即跑，**不需要任何回车**：
弹出窗口 -> 强制抢前台 -> 依次跑 4 组注入 -> 结果直接画在窗口里。
关窗口退出。
"""
import ctypes
import sys
import os
import threading
import time
from ctypes import wintypes as wt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.keyboard as kb  # noqa: E402


# ======================================================================
# 1) DLL
# ======================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ======================================================================
# 2) ctypes.wintypes 里缺失的类型，自己补
# ======================================================================
LONG = ctypes.c_long
DWORD = wt.DWORD
UINT = wt.UINT
WPARAM = wt.WPARAM
LPARAM = wt.LPARAM

# wintypes 没有 LRESULT（窗口过程返回值）
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wt.HDC),
        ("fErase", wt.BOOL),
        ("rcPaint", wt.RECT),
        ("fRestore", wt.BOOL),
        ("fIncUpdate", wt.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


# ---------- Raw Input（判断注入的最低层证据）----------
# WM_INPUT 是「系统收到了按键」的权威证据。
# 浏览器(Chrome/Edge)、现代游戏、部分 Electron 应用都只认 Raw Input。
class RAWINPUTHDR(ctypes.Structure):
    _fields_ = [
        ("dwType", DWORD),
        ("dwSize", DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", WPARAM),
    ]


ULONG_PTR = ctypes.c_size_t


class RAWINPUTDATA(ctypes.Structure):
    """RAWKEYBOARD —— 字段名必须是大写 Vkey/MakeCode，与Win32 结构一致。"""
    _fields_ = [
        ("Vkey", wt.SHORT),
        ("MakeCode", wt.SHORT),
        ("Flags", DWORD),
        ("Reserved", DWORD),
        ("ExtraInformation", ULONG_PTR),
    ]


# ======================================================================
# 3) 常量
# ======================================================================
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
SW_SHOW = 5
SW_RESTORE = 9

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_INPUT = 0x00FF

RID_INPUT = 0x10000003
RIM_INPUT = 0x00000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHARDWARE = 2

# 自定义消息：后台注入线程用它通知窗口重绘
WM_APP_REFRESH = 0x8001

IDC_ARROW = 32512
IDI_APPLICATION = 32512
COLOR_WINDOW = 5
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
TRANSPARENT = 1
GB2312_CHARSET = 134


# ======================================================================
# 4) API 签名（不声明=32 位传参=溢出）
# ======================================================================
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM

user32.CreateWindowExW.argtypes = [
    DWORD, wt.LPCWSTR, wt.LPCWSTR, DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wt.HWND

user32.DefWindowProcW.argtypes = [wt.HWND, UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL

user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL

user32.UpdateWindow.argtypes = [wt.HWND]
user32.UpdateWindow.restype = wt.BOOL

user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
user32.InvalidateRect.restype = wt.BOOL

user32.PostMessageW.argtypes = [wt.HWND, UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wt.BOOL

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND

user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL

user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetWindowThreadProcessId.restype = DWORD

user32.AttachThreadInput.argtypes = [DWORD, DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL

user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClientRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wt.BOOL

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None

user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, UINT, UINT]
user32.GetMessageW.restype = ctypes.c_int

user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.TranslateMessage.restype = wt.BOOL

user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = LRESULT

user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wt.HANDLE

user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wt.HANDLE

user32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.BeginPaint.restype = wt.HDC

user32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.EndPaint.restype = wt.BOOL

user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
user32.FillRect.restype = ctypes.c_int

user32.DrawTextW.argtypes = [
    wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wt.UINT
]
user32.DrawTextW.restype = ctypes.c_int

# --- Raw Input ---
user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, UINT, UINT]
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.GetRawInputData.argtypes = [
    LPARAM, UINT, ctypes.c_void_p, UINT, UINT
]
user32.GetRawInputData.restype = UINT

user32.ToUnicode.argtypes = [
    UINT, UINT, ctypes.c_void_p, wt.LPWSTR, ctypes.c_int, wt.LPWSTR
]
user32.ToUnicode.restype = ctypes.c_int
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short

gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [wt.LPCWSTR]
gdi32.CreateFontW.restype = wt.HGDIOBJ

gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ

gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int

gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL

gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HGDIOBJ

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = DWORD
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = DWORD


# ======================================================================
# 5) 前台窗口工具
# ======================================================================
def force_foreground(hwnd):
    """
    把窗口抢到前台。

    关键认知：**SendInput 只把事件投递给前台窗口**，不是广播。
    前台不对，注入了目标程序也看不见。

    绕过后台进程限制的标准做法：
    AttachThreadInput 把当前线程和前台线程「粘」起来，
    再调 SetForegroundWindow 就不会被系统拒绝。
    """
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    cur = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    attached = False
    if fg_thread and fg_thread != cur:
        attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_thread, False)
    return user32.GetForegroundWindow() == hwnd


def foreground_title():
    h = user32.GetForegroundWindow()
    if not h:
        return "(无)"
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(h, buf, 512)
    return buf.value or "(无标题)"


def vk_to_char(vk, scan):
    """
    虚拟键码 -> 字符，绕开 TranslateMessage。

    为什么不用 TranslateMessage 生成的 WM_CHAR：
    实测注入的按键经常只出 WM_KEYDOWN 不出 WM_CHAR，
    所以必须自己 ToUnicode 转，否则会误判成「注入失败」。
    """
    if vk in (0x10, 0x11, 0x12, 0xA0, 0xA1, 0x5B, 0x5C, 0x5D):
        return ""              # 修饰键不成字符
    ks = (ctypes.c_ubyte * 256)()
    user32.GetKeyboardState(ks)
    buf = ctypes.create_unicode_buffer(8)
    n = user32.ToUnicode(vk, scan, ks, buf, 8, None)
    if n > 0:
        return buf.value[0]
    return ""


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.WORD),
        ("usUsage", wt.WORD),
        ("dwFlags", DWORD),
        ("hwndTarget", wt.HWND),
    ]


def register_raw_input(hwnd):
    """注册 Raw Input：这是「系统层确实收到了按键」的权威证据。"""
    devs = (RAWINPUTDEVICE * 1)()
    devs[0].usUsagePage = 0x01      # HID_USAGE_PAGE_GENERIC
    devs[0].usUsage = 0x06           # HID_USAGE_GENERIC_KEYBOARD
    devs[0].dwFlags = 0
    devs[0].hwndTarget = hwnd
    n = user32.RegisterRawInputDevices(
        ctypes.byref(devs), 1, ctypes.sizeof(RAWINPUTDEVICE))
    return n > 0


# ======================================================================
# 6) 测试窗口
# ======================================================================
_CB_ERR_SEEN = set()


class TestWindow(object):
    def __init__(self):
        self.hwnd = None
        self.chars = []
        self.keydowns = 0
        self.raw_inputs = 0
        self.raw_vks = []
        self.raw_types = []
        self.paints = 0
        self.beat = 0              # 心跳：证明消息循环活着
        self.log = []              # 诊断过程文字
        self.done = False
        self._proc = None
        self.raw_ok = False
        self.lock = threading.Lock()
        self.quit = False

    # ---------- 窗口过程 ----------
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_APP_REFRESH:
                self.beat += 1
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_CHAR:
                with self.lock:
                    self.chars.append(chr(wp & 0xFFFF))
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_KEYDOWN:
                # 【重要】不依赖 TranslateMessage 产生 WM_CHAR。
                # 实测：注入的按键 TranslateMessage 经常不给 WM_CHAR，
                # 但 WM_KEYDOWN 一定到。自己用 ToUnicode 转字符才靠谱。
                vk = wp & 0xFFFF
                with self.lock:
                    self.keydowns += 1
                    ch = vk_to_char(vk, (lp >> 16) & 0xFF)
                    if ch:
                        self.chars.append(ch)
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_KEYUP:
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_INPUT:
                # 解包 Raw Input：现代程序/浏览器/游戏读键盘走的就是这条路。
                # WM_INPUT=0 只说明没注册 Raw Input，不说明注入失败。
                try:
                    n = ctypes.sizeof(RAWINPUTHDR)
                    buf = (ctypes.c_byte * 48)()
                    cnt = user32.GetRawInputData(lp, RIM_INPUT, buf, 48, n)
                    if cnt > 0:
                        hdr = RAWINPUTHDR.from_buffer_copy(bytes(buf)[:cnt])
                        with self.lock:
                            self.raw_inputs += 1
                            self.raw_types.append(hdr.dwType)
                            # RAWINPUT 布局 = RAWINPUTHEADER 紧跟设备数据，
                            # 所以设备数据从「头部大小」这个偏移读，不是从 0。
                            # 从 0 读会拿到 dwType/dwSize/hDevice 这些头部字段。
                            off = ctypes.sizeof(RAWINPUTHDR)
                            sz = ctypes.sizeof(RAWINPUTDATA)
                            if cnt >= off + sz:
                                d = RAWINPUTDATA.from_buffer_copy(
                                    bytes(buf)[off:off + sz])
                                if hdr.dwType == RIM_TYPEKEYBOARD:
                                    self.raw_vks.append(d.Vkey)
                except Exception as e:
                    key = "rawinput:%s" % str(e)[:60]
                    if key not in _CB_ERR_SEEN:
                        _CB_ERR_SEEN.add(key)
                        self.say("!! RawInput 解析失败: %s" % e)
                return 0
            if msg == WM_PAINT:
                self._on_paint()
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                self.quit = True
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            # 绝不能静默吞：回调里抛异常 = 事件链断裂 = 窗口停止响应。
            key = "%s:%s" % (type(e).__name__, str(e)[:120])
            if key not in _CB_ERR_SEEN:
                _CB_ERR_SEEN.add(key)
                self.say("!! 回调出错 %s: %s" % (type(e).__name__, e))
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ---------- 画 ----------
    def _on_paint(self):
        with self.lock:
            chars = list(self.chars[-600:])
            kd = self.keydowns
            ri = self.raw_inputs
            rv = list(self.raw_vks[-8:])
            log = list(self.log[-12:])
        self.paints += 1

        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            client = wt.RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(client))
            cw = client.right - client.left
            ch = client.bottom - client.top

            brush = gdi32.GetStockObject(COLOR_WINDOW + 1)
            user32.FillRect(hdc, ctypes.byref(client), brush)

            font = gdi32.CreateFontW(
                -18, 0, 0, 0, 400, 0, 0, 0,
                GB2312_CHARSET, 0, 0, 0, 0, None)
            old = gdi32.SelectObject(hdc, font)
            gdi32.SetBkMode(hdc, TRANSPARENT)

            def line(text, top, height=26):
                rc = wt.RECT(16, top, cw - 16, top + height)
                user32.DrawTextW(hdc, text, len(text),
                                 ctypes.byref(rc), 0)

            y = 12
            line("注入诊断 v4（全自动，无需回车）", y); y += 30
            line("消息心跳=%d   WM_PAINT=%d   前台=%s"
                 % (self.beat, self.paints, foreground_title()[:28]), y)
            y += 30
            # WM_KEYDOWN>0 即证明注入到达窗口；WM_INPUT 是更底层的证据
            line("WM_KEYDOWN=%d   RawInput=%d(注册%s)   字符数=%d"
                 % (kd, ri, "OK" if self.raw_ok else "失败", len(chars)), y)
            y += 30
            line("RawInput 收到的 VK 码：%s"
                 % (" ".join(hex(v) for v in rv) if rv else "（无）"), y)
            y += 34

            line("—— 收到的字符 ——", y); y += 28
            body = "".join(chars)[-600:]
            line(body if body else "（还没有收到字符）", y, 90)
            y += 110

            line("—— 诊断过程 ——", y); y += 28
            for t in log:
                line(t, y, 24)
                y += 24

            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(font)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    # ---------- 对外 ----------
    def say(self, text):
        with self.lock:
            self.log.append(text)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)

    def add_chars(self, n):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)

    def reset(self):
        with self.lock:
            self.chars = []
            self.keydowns = 0
            self.raw_inputs = 0
            self.raw_vks = []

    def snapshot(self):
        with self.lock:
            return ("".join(self.chars), self.keydowns,
                    self.raw_inputs, list(self.raw_vks))

    def create(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "AutoClickDiagWinV3"

        # 关键：WNDPROC 对象必须持有引用，否则被GC 后回调指向野指针
        self._proc = WNDPROC(self._wndproc)

        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._proc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.hbrBackground = wt.HBRUSH(COLOR_WINDOW + 1)
        wc.lpszMenuName = None
        wc.lpszClassName = cls_name

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = kernel32.GetLastError()
            if err != 1410:        # 1410 = 类已存在（重复运行）
                raise RuntimeError("RegisterClassW 失败，错误码 %d" % err)

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            cls_name,
            "注入诊断 v3",
            WS_OVERLAPPEDWINDOW,
            160, 120, 820, 560,
            None, None, hinst, None)
        if not self.hwnd:
            raise RuntimeError("CreateWindowExW 失败，错误码 %d"
                               % kernel32.GetLastError())
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)
        self.raw_ok = register_raw_input(self.hwnd)
        return self.hwnd

    def pump(self):
        """
        消息循环 —— **必须在主线程**。
        放后台线程会导致 Windows 判「未响应」（窗口所属线程不泵消息）。
        """
        msg = wt.MSG()
        while not self.quit:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ======================================================================
# 7) 诊断流程（跑在后台线程，只管注入 + 通知重绘）
# ======================================================================
def run_tests(win):
    vk = kb.key_to_vk("w")

    def bg():
        # 稍等一下让窗口先绘制出来
        for _ in range(20):
            time.sleep(0.05)
            if win.hwnd:
                break

        ok = force_foreground(win.hwnd)
        time.sleep(0.4)
        win.say("抢前台：%s" % ("成功" if ok else "失败"))
        win.say("当前前台：%s" % foreground_title()[:30])

        results = []

        # --- 测试 1：单次 完整按下+抬起（扫描码）---
        with win.lock:
            win.chars = []
            win.keydowns = 0
        win.say("[测试1] 单次按下+抬起（扫描码）")
        force_foreground(win.hwnd)
        kb.send_key(vk, up=False, use_scancode=True)
        time.sleep(0.08)
        kb.send_key(vk, up=True, use_scancode=True)
        time.sleep(0.5)
        c, k, r, rv = win.snapshot()
        results.append(("单次按下+抬起", c, k))
        win.say("     ->字符%r keydown=%d" % (c, k))
        win.add_chars(len(c))

        # --- 测试 2：成对 x6（扫描码）---
        with win.lock:
            win.chars = []
            win.keydowns = 0
        win.say("[测试2] 成对按下抬起 x6（扫描码）")
        force_foreground(win.hwnd)
        for _ in range(6):
            kb.send_key(vk, up=False, use_scancode=True)
            time.sleep(0.05)
            kb.send_key(vk, up=True, use_scancode=True)
            time.sleep(0.05)
        time.sleep(0.5)
        c, k, r, rv = win.snapshot()
        results.append(("成对x6", c, k))
        win.say("     ->字符%r keydown=%d" % (c, k))
        win.add_chars(len(c))

        # --- 测试 3：30Hz 持续 3 秒（扫描码，只按下不抬起）---
        with win.lock:
            win.chars = []
            win.keydowns = 0
        win.say("[测试3] 30Hz持续3秒（只按下不抬起）")
        force_foreground(win.hwnd)
        s0 = kb.inject_stat()
        t0 = time.time()
        n = 0
        while time.time() - t0 < 3.0:
            kb.send_key(vk, up=False, use_scancode=True)
            n += 1
            time.sleep(1.0 / 30.0)
        kb.send_key(vk, up=True, use_scancode=True)
        time.sleep(0.5)
        s1 = kb.inject_stat()
        c, k, r, rv = win.snapshot()
        results.append(("30Hz持续", c, k))
        win.say("     -> 注入%d次 成功%d 字符%d个"
                % (n, s1["ok"] - s0["ok"], len(c)))
        win.add_chars(len(c))

        # --- 测试 4：虚拟键码模式 x6 ---
        with win.lock:
            win.chars = []
            win.keydowns = 0
        win.say("[测试4] 虚拟键码模式 x6")
        force_foreground(win.hwnd)
        for _ in range(6):
            kb.send_key(vk, up=False, use_scancode=False)
            time.sleep(0.05)
            kb.send_key(vk, up=True, use_scancode=False)
            time.sleep(0.05)
        time.sleep(0.5)
        c, k, r, rv = win.snapshot()
        results.append(("虚拟键码x6", c, k))
        win.say("     ->字符%r keydown=%d" % (c, k))
        win.add_chars(len(c))

        # ---- 结论 ----
        # 【判据优先级】以 WM_KEYDOWN 为准，不是 WM_CHAR。
        # 实测：注入的按键经常只有 WM_KEYDOWN、没有 WM_CHAR，
        # 但只要 KEYDOWN 到达，注入就是成功的（程序读的是按键状态不是字符）。
        k1 = results[0][2]
        k2 = results[1][2]
        k4 = results[3][2]
        c1 = len(results[0][1])
        win.say("")
        if k1 >= 1 or k4 >= 1 or c1 >= 1:
            win.say(">>> 注入链路正常！WM_KEYDOWN=%d已到达窗口。" % max(k1, k4))
            win.say(">>> 所以长按锁定 exe 的注入机制本身没问题，")
            win.say(">>> 之前不刷字的原因是目标程序不在前台 / 不认注入。")
            if k1 >= 1 and k4 < 1:
                win.say(">>> 扫描码模式有效、虚拟键码无效 -> 保持默认扫描码")
            elif k4 >= 1 and k1 < 1:
                win.say(">>> 扫描码无效、虚拟键码有效 -> 加 --vk 参数运行")
            else:
                win.say(">>> 两种模式都有效 -> 用默认扫描码即可")
        else:
            win.say(">>> 连 WM_KEYDOWN 都没收到，问题在最底层：")
            win.say("    a) 窗口是否真在前台（看上面「前台=」那一行）")
            win.say("    b) 是否有安全软件拦截键盘钩子")
            win.say("    c) 是否处于锁屏/UAC 安全桌面（此时注入一律无效）")
        win.say("测试结束，关窗口退出。")

    threading.Thread(target=bg, daemon=True).start()


def main():
    print("注入诊断 v4 已启动，窗口会自动弹出并自动测试。")
    print("全程不需要按回车。关掉窗口即可退出。")
    sys.stdout.flush()

    win = TestWindow()
    win.say("窗口已创建，正在启动诊断…")
    win.create()

    # 先起注入线程（它只做 SendInput + PostMessage，不泵消息）
    run_tests(win)

    # 消息循环在主线程 —— 这是 v2 判「未响应」的根本原因
    win.pump()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.stdout.flush()