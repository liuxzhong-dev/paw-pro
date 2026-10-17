# -*- coding: utf-8 -*-
"""
keyhold_gui.py —— 长按锁定 图形界面版（带下拉切换）
==================================================

为什么要单独做一个图形版：
命令行版要敲参数，切换「一直按着 / 反复点击」必须改命令，
容易忘。这版把模式做成**下拉框**，直接选。

两种模式（对应 KeyHoldLock 的 hold_down 参数）：
    一直按着  -> hold_down=True  重复注入 keydown、**永不注入 keyup**
                          程序看到「键一直没松」-> 游戏角色持续移动
    反复点击  -> hold_down=False 成对注入 keydown + keyup
                          程序看到「按下又抬起」-> 连续点击多次

实现说明
--------
本机Python 环境**没有 tkinter**，所以界面用原生 Win32 API 写
（ctypes直接调user32/gdi32）。好处是打包体积不涨、无第三方依赖。

【必须遵守的 Win32 规则，本项目踩过坑】
1. 所有 API 必须声明精确 argtypes，否则 64 位指针会溢出
   （ctypes 默认按32 位 C int 传参）-> argument N: int too long to convert
2. 函数要挂对的 DLL：user32 / gdi32 / kernel32 / advapi32，不能凭印象
3. 结构体参数不能传 NULL（FillRect 会 access violation）
4. **消息循环必须在主线程**：放后台线程会让 Windows 判定「未响应」，
   因为主窗口所属线程不泵消息
5. 回调里的异常绝不能静默吞掉，要打印出来
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as wt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import keyboard as kb  # noqa: E402


def _fix_console_encoding():
    """
    控制台按 UTF-8 输出，否则 exe 双击运行时中文全是乱码。
    reconfigure(line_buffering=True) 也很重要：
    不加的话输出会卡在缓冲区里，界面上一个字都看不到。
    """
    if sys.platform != "win32":
        return
    try:
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except Exception:
            pass


# ======================================================================
# DLL
# ======================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.SetConsoleOutputCP.argtypes = [wt.DWORD]
kernel32.SetConsoleOutputCP.restype = wt.BOOL
kernel32.SetConsoleCP.argtypes = [wt.DWORD]
kernel32.SetConsoleCP.restype = wt.BOOL


ULONG_PTR = ctypes.c_size_t
DWORD = wt.DWORD
UINT = wt.UINT
WPARAM = wt.WPARAM
LPARAM = wt.LPARAM
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


# ======================================================================
# 常量
# ======================================================================
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000

BS_PUSHBUTTON = 0x0000
ES_NUMBER = 0x2000

CBS_DROPDOWNLIST = 0x0003
CB_ADDSTRING = 0x0143
CB_GETCOUNT = 0x0146
CB_GETCURSEL = 0x0147
CB_SETCURSEL = 0x014E

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLORBTN = 0x0135
WM_APP_STATUS = 0x8001     # 自定义：后台线程通知界面刷新状态

SW_SHOW = 5
SW_RESTORE = 9

IDC_ARROW = 32512
IDI_APPLICATION = 32512
COLOR_WINDOW = 5
COLOR_BTNFACE = 15
TRANSPARENT = 1
GB2312_CHARSET = 134

BN_CLICKED = 0
WM_SETFONT = 0x0030

ID_KEYS = 1001
ID_THRESH = 1002
ID_HZ = 1003
ID_MODE = 1004
ID_START = 1005
ID_STOP = 1006
ID_STATUS = 1007
ID_WARN = 1008


# ======================================================================
# API 签名（不声明=32 位传参=溢出）
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

user32.SendMessageW.argtypes = [wt.HWND, UINT, WPARAM, ctypes.c_void_p]
user32.SendMessageW.restype = LRESULT

user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
user32.SetWindowTextW.restype = wt.BOOL

user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClientRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wt.BOOL

user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
user32.FillRect.restype = ctypes.c_int

user32.DrawTextW.argtypes = [
    wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wt.UINT]
user32.DrawTextW.restype = ctypes.c_int

user32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.BeginPaint.restype = wt.HDC
user32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.EndPaint.restype = wt.BOOL

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
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [wt.LPCWSTR]
gdi32.CreateFontW.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.SetTextColor.argtypes = [wt.HDC, DWORD]
gdi32.SetTextColor.restype = DWORD
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL

kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

# DEFAULT_GUI_FONT=17 / SYSTEM_FONT=16
# 存成整数：SendMessageW 的 wParam 是整型，传 HANDLE 对象会报 TypeError
DEFAULT_FONT = 17
SYSTEM_FONT = 16


# ======================================================================
# 工具
# ======================================================================
def get_text(hwnd, limit=128):
    buf = ctypes.create_unicode_buffer(limit)
    user32.GetWindowTextW(hwnd, buf, limit)
    return buf.value.strip()


def set_text(hwnd, s):
    user32.SetWindowTextW(hwnd, s)


def combo_add(hwnd, text):
    b = ctypes.create_unicode_buffer(text)
    user32.SendMessageW(hwnd, CB_ADDSTRING, 0, ctypes.cast(b, ctypes.c_void_p))


def combo_sel(hwnd):
    return user32.SendMessageW(hwnd, CB_GETCURSEL, 0, None)


def combo_set(hwnd, idx):
    user32.SendMessageW(hwnd, CB_SETCURSEL, idx, None)


# ======================================================================
# 主窗口
# ======================================================================
MODES = [
    ("一直按着（游戏角色持续移动）", True),
    ("反复点击（连续触发多次）", False),
]


class App(object):
    def __init__(self):
        self.hwnd = None
        self._proc = None
        self.lock = None
        self.quit = False
        self.status = "未启动"
        self.warn = ""
        self.lock_ = threading.Lock()

    # ---------- 窗口过程 ----------
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_COMMAND:
                hi = (wp >> 16) & 0xFFFF
                lo = wp & 0xFFFF
                if hi == BN_CLICKED:
                    if lo == ID_START:
                        self.on_start()
                        return 0
                    if lo == ID_STOP:
                        self.on_stop()
                        return 0
                return 0

            if msg == WM_APP_STATUS:
                # 状态文本存在实例上，这里只负责触发重绘。
                # 不通过 lp 传字符串：跨线程的指针生命周期不可靠。
                user32.InvalidateRect(hwnd, None, False)
                return 0

            if msg == WM_PAINT:
                self.on_paint()
                return 0

            if msg in (WM_CLOSE, WM_DESTROY):
                self.on_stop()
                self.quit = True
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            # 回调里吞异常 = 窗口停止响应。必须暴露出来。
            print("!! 窗口回调出错 %s: %s" % (type(e).__name__, e))
            sys.stdout.flush()
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ---------- 绘制 ----------
    def on_paint(self):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            rc = wt.RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(rc))
            w = rc.right - rc.left
            h = rc.bottom - rc.top
            user32.FillRect(hdc, ctypes.byref(rc),
                            gdi32.GetStockObject(COLOR_BTNFACE + 1))
            font = gdi32.CreateFontW(
                -15, 0, 0, 0, 400, 0, 0, 0,
                GB2312_CHARSET, 0, 0, 0, 0, None)
            old = gdi32.SelectObject(hdc, font)
            gdi32.SetBkMode(hdc, TRANSPARENT)

            if self.warn:
                gdi32.SetTextColor(hdc, 0x0000D0)   # 红色（BGR）
                r = wt.RECT(16, h - 56, w - 16, h - 20)
                user32.DrawTextW(hdc, self.warn, len(self.warn),
                                 ctypes.byref(r), 0)
                gdi32.SetTextColor(hdc, 0x000000)

            r = wt.RECT(16, h - 92, w - 16, h - 60)
            with self.lock_:
                t = self.status
            user32.DrawTextW(hdc, t, len(t), ctypes.byref(r), 0)

            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(font)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    # ---------- 控件 ----------
    def make(self, cls, text, style, x, y, cw, ch, cid):
        h = user32.CreateWindowExW(
            0, cls, text, WS_CHILD | WS_VISIBLE | style,
            x, y, cw, ch, self.hwnd, ct_hmenu(cid), hinst, None)
        if cid != -1:
            # cid == -1 的是纯说明文字，不需要设字体和收集
            self.controls.append(h)
        return h

    def build_ui(self):
        self.controls = []
        self.t_keys = self.make("EDIT", "w", ES_NUMBER | WS_BORDER | WS_TABSTOP,
                                120, 24, 160, 26, ID_KEYS)
        self.make("STATIC", "生效按键（用逗号分隔）", 0, 16, 28, 110, 22, -1)

        self.t_thr = self.make("EDIT", "2", ES_NUMBER | WS_BORDER | WS_TABSTOP,
                                120, 60, 90, 26, ID_THRESH)
        self.make("STATIC", "长按多少秒后锁定", 0, 16, 64, 110, 22, -1)

        self.t_hz = self.make("EDIT", "30", ES_NUMBER | WS_BORDER | WS_TABSTOP,
                              120, 96, 90, 26, ID_HZ)
        self.make("STATIC", "重复频率（次/秒）", 0, 16, 100, 110, 22, -1)

        # 【需求】下拉切换：一直按着 / 反复点击
        self.c_mode = self.make(
            "COMBOBOX", "",
            CBS_DROPDOWNLIST | WS_BORDER | WS_VSCROLL | WS_TABSTOP,
            120, 132, 320, 240, ID_MODE)
        for label, _ in MODES:
            combo_add(self.c_mode, label)
        combo_set(self.c_mode, 0)     # 默认「一直按着」

        self.make("STATIC", "锁定行为（下拉切换）", 0, 16, 140, 110, 22, -1)

        self.b_start = self.make("BUTTON", "启动锁定", BS_PUSHBUTTON | WS_TABSTOP,
                                 120, 182, 140, 32, ID_START)
        self.b_stop = self.make("BUTTON", "停止并退出", BS_PUSHBUTTON | WS_TABSTOP,
                                280, 182, 140, 32, ID_STOP)

        self.make("STATIC",
                  "操作：长按设定的键到指定秒数 -> 进入锁定；"
                  "再按一次同一个键 -> 解除；F12 = 紧急释放",
                  0, 16, 226, 440, 22, -1)

    # ---------- 权限提示 ----------
    def check_privilege(self):
        try:
            from diag_privilege import rid_value
            my = rid_value()
            if my is None or my >= 0x3000:
                return
            import diag_target as dt
            higher = []
            for w in dt.list_windows():
                r = rid_value(w.pid)
                if r is not None and r > my:
                    higher.append(w.title[:16])
            if higher:
                self.warn = ("⚠ 权限不足：检测到更高权限的程序"
                             "（%s...）。请右键本exe→以管理员身份运行"
                             % "、".join(higher[:2]))
        except Exception:
            pass

    # ---------- 事件 ----------
    def say(self, text):
        """更新状态栏。后台线程（钩子回调）也会调它，所以必须线程安全。"""
        with self.lock_:
            self.status = text
        if self.hwnd:
            # 用 PostMessage 而不是直接 InvalidateRect：
            # 后台线程直接调 InvalidateRect 在某些情况下会不稳定
            user32.PostMessageW(self.hwnd, WM_APP_STATUS, 0, 0)

    def on_start(self):
        if self.lock is not None:
            self.say("已经在运行了，先点「停止并退出」")
            return
        keys = get_text(self.t_keys, 64) or "w"
        keys = " ".join(k.strip() for k in keys.replace(",", " ").split()
                        if k.strip())
        try:
            thr = float(get_text(self.t_thr, 16) or "2")
        except ValueError:
            thr = 2.0
        try:
            hz = int(float(get_text(self.t_hz, 16) or "30"))
        except ValueError:
            hz = 30
        idx = combo_sel(self.c_mode)
        if idx < 0:
            idx = 0
        label, hold_down = MODES[idx]

        def on_change(active, name):
            if active:
                self.say("[%s] 已锁定：%s 处于持续按下状态"
                         % (time.strftime("%H:%M:%S"), name))
            else:
                self.say("[%s] 已解除：%s 恢复正常"
                         % (time.strftime("%H:%M:%S"), name))

        self.lock = kb.KeyHoldLock(
            keys=keys, threshold_sec=thr, repeat_hz=hz,
            on_state_change=on_change, panic_key="f12",
            hold_down=hold_down)
        ok, err = self.lock.start()
        if not ok:
            self.say("[启动失败] %s" % err)
            self.lock = None
            return
        self.say("已启动 | 键=%s | 阈值=%.1f秒 | %s"
                 % (keys, thr, label))
        print("已启动：键=%s阈值=%.1f秒 模式=%s"
              % (keys, thr, label))
        print("  长按到阈值进入锁定；再按一次同键解除；F12 紧急释放")
        sys.stdout.flush()

    def on_stop(self):
        if self.lock is not None:
            try:
                self.lock.stop()
            except Exception:
                pass
            self.lock = None
        print("已停止。")
        sys.stdout.flush()

    # ---------- 生命周期 ----------
    def create(self):
        global hinst
        hinst = kernel32.GetModuleHandleW(None)
        cls = "KeyHoldGuiV1"
        self._proc = WNDPROC(self._wndproc)

        wc = WNDCLASSW()
        wc.style = 0x0003
        wc.lpfnWndProc = self._proc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.hbrBackground = wt.HBRUSH(COLOR_BTNFACE + 1)
        wc.lpszMenuName = None
        wc.lpszClassName = cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            if err != 1410:
                raise RuntimeError("RegisterClassW 失败 %d" % err)

        self.hwnd = user32.CreateWindowExW(
            0, cls, "长按锁定器", WS_OVERLAPPEDWINDOW,
            200, 140, 480, 380, None, None, hinst, None)
        if not self.hwnd:
            raise RuntimeError("CreateWindowExW 失败")

        self.build_ui()

        # 统一给控件设置 GUI 默认字体（WM_SETFONT=0x0030），
        # 否则控件会用很小的默认宋体，看起来很糊
        for c in self.controls:
            if c:
                user32.SendMessageW(c, WM_SETFONT, DEFAULT_FONT, None)

        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)
        self.check_privilege()
        self.say("就绪 —— 选好模式后点「启动锁定」")
        return self.hwnd

    def pump(self):
        """消息循环必须在主线程（后台线程会触发「未响应」判定）。"""
        msg = wt.MSG()
        while not self.quit:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def ct_hmenu(cid):
    """子控件用 id 当 HMENU，父窗口靠它区分是哪个控件的命令。"""
    return wt.HMENU(cid)


hinst = None


def main():
    _fix_console_encoding()
    print("=" * 56)
    print(" 长按锁定器（图形界面）")
    print("=" * 56)
    print(" 窗口里有下拉框可切换「一直按着 / 反复点击」，默认是「一直按着」。")
    print(" 长按设定键到阈值 -> 锁定；再按一次同键 -> 解除；F12 紧急释放。")
    print()
    sys.stdout.flush()

    app = App()
    app.create()
    app.pump()          # 主线程泵消息
    print("窗口已关闭，程序退出。")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.stdout.flush()