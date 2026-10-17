# -*- coding: utf-8 -*-
"""
目标窗口注入诊断 —— 定位「记事本能进、游戏进不去」卡在哪一层
=========================================================

背景
----
记事本测试成功、原神测试失败，但两者都是前台窗口。
这说明 SendInput 机制本身没问题，是**目标程序对注入事件的处理方式不同**。

本工具回答一个关键问题：
    注入的按键，**系统层到底收到了没有**？

做法
----
注册一个全局 Raw Input 监听（Raw Input 是系统级分发，
会看到发往任何窗口的键盘事件），然后：
  1. 把目标窗口抢到前台
  2. 注入一组按键
  3. 同时看「系统层收到了几个」和「目标窗口是不是前台」

判读
----
| 现象 | 含义 |
|---|---|
| 系统层收到 0| 注入根本没发出去（权限/安全桌面/前台锁定） |
| 系统层收到 N，目标窗口是前台 | **注入成功，是目标程序主动丢弃了注入事件** |
| 系统层收到 N，但抢前台失败 | 前台切换被拦，注入进了别的窗口 |

**重要前提**：如果确认是目标程序主动丢弃注入事件（多数带反作弊的游戏如此），
那模拟键盘这条路走不通，这不是bug。游戏用键盘钩子里的
`LLKHF_INJECTED` 标志就能区分「真人按键」和「程序模拟按键」，
并只接受前者——这是反作弊的常规做法，属于设计如此。

注意：绕过反作弊通常违反游戏用户协议，本工具只做**定位**，
不做绕过。
"""
import ctypes
import sys
import os
import time
from ctypes import wintypes as wt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.keyboard as kb  # noqa: E402


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


ULONG_PTR = ctypes.c_size_t
LONG = ctypes.c_long
DWORD = wt.DWORD
UINT = wt.UINT
WPARAM = wt.WPARAM
LPARAM = wt.LPARAM
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, UINT, WPARAM, LPARAM)


# ======================================================================
# 枚举窗口用的结构体
# ======================================================================
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


WNDENUMPROC = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HWND, ctypes.c_void_p)

WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW = 5
SW_RESTORE = 9
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_INPUT = 0x00FF
WM_APP_REFRESH = 0x8001
IDC_ARROW = 32512
IDI_APPLICATION = 32512
COLOR_WINDOW = 5
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
TRANSPARENT = 1
GB2312_CHARSET = 134

RID_INPUT = 0x10000003
RIM_INPUT = 0x00000003
RIM_TYPEKEYBOARD = 1


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.WORD),
        ("usUsage", wt.WORD),
        ("dwFlags", DWORD),
        ("hwndTarget", wt.HWND),
    ]


class RAWINPUTHDR(ctypes.Structure):
    _fields_ = [
        ("dwType", DWORD),
        ("dwSize", DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", WPARAM),
    ]


# ======================================================================
# API 签名
# ======================================================================
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    DWORD, wt.LPCWSTR, wt.LPCWSTR, DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
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
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetClientRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
user32.EnumWindows.restype = wt.BOOL
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
    wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wt.UINT]
user32.DrawTextW.restype = ctypes.c_int
user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, UINT, UINT]
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.GetRawInputData.argtypes = [
    LPARAM, UINT, ctypes.c_void_p, UINT, UINT]
user32.GetRawInputData.restype = UINT

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = DWORD
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [wt.LPCWSTR]
gdi32.CreateFontW.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL


def log(s=""):
    print(s)
    sys.stdout.flush()


# ======================================================================
# 窗口枚举
# ======================================================================
class WindowInfo(object):
    __slots__ = ("hwnd", "title", "cls", "pid", "visible", "w", "h")

    def __init__(self, hwnd, title, cls, pid, visible, w, h):
        self.hwnd = hwnd
        self.title = title
        self.cls = cls
        self.pid = pid
        self.visible = visible
        self.w = w
        self.h = h

    def __repr__(self):
        return "<hwnd=%s title=%r cls=%r pid=%s>" % (
            hex(self.hwnd or 0), self.title[:28], self.cls[:20], self.pid)


_enum_buf = []


def _enum_cb(hwnd, lparam):
    if not user32.IsWindowVisible(hwnd):
        return True
    ln = user32.GetWindowTextLengthW(hwnd)
    if ln <= 0:
        return True
    buf = ctypes.create_unicode_buffer(ln + 2)
    user32.GetWindowTextW(hwnd, buf, ln + 2)
    title = buf.value.strip()
    if not title:
        return True
    pid = DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    _enum_buf.append(WindowInfo(
        hwnd, title, "", pid.value, True,
        rc.right - rc.left, rc.bottom - rc.top))
    return True


def list_windows():
    """列出所有有标题的可见顶层窗口。"""
    del _enum_buf[:]
    user32.EnumWindows(WNDENUMPROC(_enum_cb), None)
    return list(_enum_buf)


def window_title(h):
    if not h:
        return "(无)"
    ln = user32.GetWindowTextLengthW(h)
    if ln <= 0:
        return "(无标题)"
    buf = ctypes.create_unicode_buffer(ln + 2)
    user32.GetWindowTextW(h, buf, ln + 2)
    return buf.value or "(无标题)"


def foreground():
    return user32.GetForegroundWindow()


def force_foreground(hwnd):
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


# ======================================================================
# 监听窗口：只看 Raw Input，统计系统层收到的键盘事件数
# ======================================================================
class SpyWindow(object):
    def __init__(self):
        self.hwnd = None
        self.kb_events = 0
        self.vks = []
        self.beat = 0
        self.raw_ok = False
        self.log = []
        self._proc = None
        self.quit = False
        self.in_loop = False
        self.baseline = 0

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_APP_REFRESH:
                self.beat += 1
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_INPUT:
                buf = (ctypes.c_byte * 64)()
                n = ctypes.sizeof(RAWINPUTHDR)
                cnt = user32.GetRawInputData(lp, RIM_INPUT, buf, 64, n)
                if cnt > 0:
                    hdr = RAWINPUTHDR.from_buffer_copy(bytes(buf)[:cnt])
                    if hdr.dwType == RIM_TYPEKEYBOARD:
                        self.kb_events += 1
                        # 记录 wParam 的 make code（低位），够定位用了
                        self.vks.append(wp & 0xFFFF)
                return 0
            if msg == WM_PAINT:
                self._on_paint()
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                self.quit = True
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            self.say("!! 回调出错 %s: %s" % (type(e).__name__, e))
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _on_paint(self):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            client = wt.RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(client))
            cw = client.right - client.left
            ch = client.bottom - client.top
            brush = wt.HBRUSH(COLOR_WINDOW + 1)
            user32.FillRect(hdc, ctypes.byref(client), brush)

            g = gdi32
            f = g.CreateFontW(-18, 0, 0, 0, 400, 0, 0, 0,
                               GB2312_CHARSET, 0, 0, 0, 0, None)
            old = g.SelectObject(hdc, f)
            g.SetBkMode(hdc, TRANSPARENT)

            def line(t, top, h=26):
                rc = wt.RECT(16, top, cw - 16, top + h)
                user32.DrawTextW(hdc, t, len(t), ctypes.byref(rc), 0)

            y = 14
            line("目标窗口注入诊断（系统层是否收到注入事件）", y); y += 30
            line("RawInput 注册=%s   刷新心跳=%d"
                 % ("成功" if self.raw_ok else "失败", self.beat), y)
            y += 30
            line("【系统层收到的键盘事件总数】%d" % self.kb_events, y)
            y += 30
            line("本次测试新增：%d 个"
                 % (self.kb_events - self.baseline), y)
            y += 34
            line("当前前台窗口：%s" % window_title(foreground())[:34], y)
            y += 30
            line("—— 判定 ——", y); y += 28
            for t in self.log[-8:]:
                line(t, y, 24); y += 24
            g.SelectObject(hdc, old)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    def say(self, t):
        self.log.append(t)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)

    def create(self):
        hinst = kernel32.GetModuleHandleW(None)
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
        wc.lpszClassName = "TargetInjectDiag"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom and kernel32.GetLastError() != 1410:
            raise RuntimeError("RegisterClassW失败")
        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "TargetInjectDiag",
            "目标窗口注入诊断", WS_OVERLAPPEDWINDOW,
            160, 120, 720, 380, None, None, hinst, None)
        if not self.hwnd:
            raise RuntimeError("CreateWindowExW 失败")
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)
        devs = (RAWINPUTDEVICE * 1)()
        devs[0].usUsagePage = 0x01
        devs[0].usUsage = 0x06
        devs[0].dwFlags = 0
        devs[0].hwndTarget = self.hwnd
        self.raw_ok = user32.RegisterRawInputDevices(
            ctypes.byref(devs), 1, ctypes.sizeof(RAWINPUTDEVICE)) > 0
        return self.hwnd

    def pump(self):
        msg = wt.MSG()
        while not self.quit:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def main():
    log("=" * 70)
    log("目标窗口注入诊断")
    log("=" * 70)
    log()
    log("这个工具回答一个问题：注入的按键，系统层到底收到了没有？")
    log()
    log("步骤：")
    log("  1. 先把你���要测的程序开起来（比如原神）")
    log("  2. 回到这里，程序会列出所有可见窗口")
    log("  3. 输入序号选目标窗口，工具会把它抢到前台并注入一组 W")
    log("  4. 看窗口里【系统层收到的键盘事件总数】有没有增加")
    log()

    wins = list_windows()
    if not wins:
        log("没找到任何有标题的窗口。先打开目标程序再运行本工具。")
        return

    log("当前可见窗口列表：")
    for i, w in enumerate(wins):
        mark = " <= 当前前台" if w.hwnd == foreground() else ""
        log("  [%2d] pid=%-6s %sx%-5s %r%s"
            % (i, w.pid, w.w, w.h, w.title[:36], mark))
    log()

    try:
        idx = int(input("选哪个窗口？输入序号（直接回车用当前前台窗口）：").strip()
                  or "-1")
    except Exception:
        idx = -1

    if idx == -1:
        fg = foreground()
        target = WindowInfo(fg, window_title(fg), "", 0, True, 0, 0)
    elif 0 <= idx < len(wins):
        target = wins[idx]
    else:
        log("序号越界，退出。")
        return

    log()
    log("目标窗口：%r  hwnd=%s" % (target.title[:40], hex(target.hwnd or 0)))
    log()

    spy = SpyWindow()
    spy.create()
    spy.say("已选择目标：%s" % target.title[:26])

    log("现在会弹出一个诊断窗口，然后自动切到目标窗口注入。")
    log("注入期间请不要碰键盘鼠标，否则计数会混入你的真实按键。")
    log()

    import threading

    def worker():
        time.sleep(2.0)          # 给用户时间看到窗口
        spy.baseline = spy.kb_events
        ok = force_foreground(target.hwnd)
        time.sleep(0.6)
        spy.say("抢前台：%s" % ("成功" if ok else "失败"))
        spy.say("注入时前台=%s"
                % ("目标窗口" if foreground() == target.hwnd
                   else window_title(foreground())[:20]))
        vk = kb.key_to_vk("w")
        s0 = kb.inject_stat()
        for _ in range(6):
            kb.send_key(vk, up=False, use_scancode=True)
            time.sleep(0.06)
            kb.send_key(vk, up=True, use_scancode=True)
            time.sleep(0.06)
        time.sleep(0.8)
        s1 = kb.inject_stat()
        got = spy.kb_events - spy.baseline
        api_ok = s1["ok"] - s0["ok"]
        fg_ok = foreground() == target.hwnd

        log("=" * 70)
        log("结果")
        log("=" * 70)
        log("  SendInput 调用成功 : %d/%d" % (api_ok, s1["total"] - s0["total"]))
        log("  系统层收到键盘事件  : %d" % got)
        log("  注入时目标是否前台  : %s" % ("是" if fg_ok else "否"))
        log()
        if got == 0:
            log(">>> 判定：注入没进系统。检查：")
            log("    - 是否被 UIPI 权限拦截（把本工具以管理员运行）")
            log("    - 是否处于锁屏/UAC 安全桌面")
            log("    - 是否有安全软件拦截键盘钩子")
        elif not fg_ok:
            log(">>> 判定：注入进了系统，但没进目标窗口（前台切换失败）。")
            log("    目标程序没被真正切到前台，事件进了别的窗口。")
        else:
            log(">>> 判定：注入确实进了系统，而且目标窗口当时在前台。")
            log(">>>")
            log(">>> 如果目标程序仍然没反应，那就是它**主动丢弃了注入事件**。")
            log(">>> 带反作弊的游戏会检查键盘钩子里的 LLMHF_INJECTED 标志，")
            log(">>> 只接受真实硬件按键。这是设计如此，不是 bug。")
            log(">>>")
            log(">>> 模拟键盘这条路对这类游戏走不通。绕过的做法通常违反")
            log(">>> 游戏用户协议，本工具不做，也不建议。")
        log()
        log("关掉诊断窗口即可退出。")
        spy.say("测试结束：系统层收到 %d 个" % got)

    threading.Thread(target=worker, daemon=True).start()
    spy.pump()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.stdout.flush()