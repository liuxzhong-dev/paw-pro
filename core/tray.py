# -*- coding: utf-8 -*-
"""
core/tray.py —— 系统托盘图标（S1 · v24）
==============================================

【为什么不用 pystray / Pillow】
本项目的立身之本是「一个 exe，双击就用」。pystray 要拖 Pillow（十几 MB），
而托盘本身就是三个 Win32 调用（Shell_NotifyIconW + 一个隐藏窗口 + 弹出菜单），
用 ctypes 写完不到 300 行、零新增依赖、exe 体积不变。ctypes 的坑本项目已经
踩了十几个版本（规则 2/3/33），比引一个新依赖更可控。

【结构】
    Tray.start()   起一个线程：注册窗口类 -> 建隐藏窗口 -> NIM_ADD 图标
                   -> GetMessageW 消息循环
    回调消息（WM_APP+1）：
        WM_LBUTTONDBLCLK  -> on_show()      双击图标 = 显示仪表盘
        WM_RBUTTONUP      -> 弹出菜单        显示 / 暂停·恢复 / 退出
    Tray.stop()    PostMessage(WM_CLOSE) -> NIM_DELETE -> DestroyWindow
                   -> PostQuitMessage，线程退出

【三条实测要点】
1. TrackPopupMenu 前必须 SetForegroundWindow，菜单后必须 PostMessage(WM_NULL)，
   否则菜单点外面不消失（微软 KB135788 的老坑）。
2. 菜单**每次右键现建**，不在启动时建死——「暂停 / 恢复」的文案要反映
   当前状态，建死就得每次改菜单项，反而更绕。
3. 回调函数对象必须存成实例属性（_wndproc），局部变量会被 GC 掉，
   之后消息一来就是「已回收的回调」崩溃。
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading

# 规则 3：每个模块自己的 DLL 实例
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ctypes.wintypes 里**没有** LRESULT（它是 LONG_PTR = 有符号指针宽度整数）。
# 直接写 c_ssize_t：32 位下 4 字节、64 位下 8 字节，和 DefWindowProc 的
# 返回值宽度一致。写死 c_long 在 64 位下会截断高位（规则 2 的同一类坑）。
LRESULT = ctypes.c_ssize_t

# ---- 常量 ----
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_NULL = 0x0000
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_LBUTTONDBLCLK = 0x0203
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512
CS_DBLCLKS = 0x0008
WS_OVERLAPPED = 0x00000000
HWND_MESSAGE = -3

# 菜单项 id
CMD_SHOW = 1001
CMD_TOGGLE = 1002
CMD_EXIT = 1003


class NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIconW 的数据结构（Vista 之后的完整版）。"""
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


# 规则 2：凡是传句柄/指针的 API 一律声明 argtypes + restype，
# 否则 64 位下返回值被截断成 32 位，表现为「调用成功但什么也没发生」。
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wt.ATOM
_user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
_user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_user32.CreateWindowExW.restype = wt.HWND
_user32.DestroyWindow.argtypes = [wt.HWND]
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                wt.UINT, wt.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.CreatePopupMenu.restype = wt.HMENU
_user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t,
                                wt.LPCWSTR]
_user32.DestroyMenu.argtypes = [wt.HMENU]
_user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND,
                                   ctypes.c_void_p]
_user32.TrackPopupMenu.restype = ctypes.c_int
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
_user32.LoadIconW.restype = wt.HICON
_user32.DestroyIcon.argtypes = [wt.HICON]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD,
                                       ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL
_shell32.ExtractIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT]
_shell32.ExtractIconW.restype = wt.HICON
_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wt.HMODULE


def _exe_icon():
    """
    拿 exe 自己的图标；拿不到退回系统默认图标。
    ExtractIconW 返回 1 表示「不是图标文件」，要一并当失败处理。
    """
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else ""
        if exe:
            h = _shell32.ExtractIconW(None, exe, 0)
            if h and int(h) != 1:
                return h, True
    except Exception:
        pass
    return _user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)), False


class Tray(object):
    """
    托盘图标。回调都在消息线程里执行，调用方自己保证线程安全。

    on_show()    双击图标 / 菜单「显示仪表盘」
    on_toggle()  菜单「暂停 / 恢复」；paused 属性决定菜单文案
    on_exit()    菜单「退出」
    """

    def __init__(self, tip="爪Pro", on_show=None, on_toggle=None,
                 on_exit=None, is_paused=None):
        self._tip = (tip or "爪Pro")[:120]
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_exit = on_exit
        # 【为什么用 getter 而不是缓存 bool】菜单文案必须反映**点开菜单
        # 那一刻**的真实状态。缓存一份就得保证每条暂停路径都记得同步
        # （热键 / 界面按钮 / 分区按钮 / 配置导入……漏一条菜单就撒谎）。
        # 现问现答最省心：多一次函数调用的代价。
        self._is_paused = is_paused
        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._ok = False
        self._paused = False
        self._icon = None
        self._icon_owned = False
        # 失败原因：托盘是「静默功能」，失败了用户只会觉得「图标没出来」。
        # 把原因留下来，dashboard 往 launch.log 写一行，排障时不用猜。
        self._error = ""
        self._wndproc = None          # 要点 3：别让回调被 GC
        self._class_name = "MouseAutoClickerTrayWnd"

    # ---- 状态 ----
    @property
    def running(self):
        return bool(self._hwnd) and self._ok

    def set_paused(self, paused):
        """菜单文案用；不重建菜单（每次右键现建）。"""
        self._paused = bool(paused)

    def toggle_label(self):
        return "恢复运行" if self.paused else "暂停运行"

    @property
    def paused(self):
        """当前是否暂停。有 getter 就问 getter（永远最新），否则用缓存值。"""
        if self._is_paused is not None:
            try:
                return bool(self._is_paused())
            except Exception:
                pass
        return self._paused

    def set_tip(self, text):
        """更新悬浮提示（限 120 字，超了截断）。"""
        self._tip = (text or "爪Pro")[:120]
        if self.running:
            self._notify(NIM_MODIFY)

    # ---- 图标本身 ----
    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._icon
        nid.szTip = self._tip
        return nid

    def _notify(self, action):
        try:
            nid = self._nid()
            ok = bool(_shell32.Shell_NotifyIconW(action, ctypes.byref(nid)))
            if not ok:
                self._error = ("Shell_NotifyIcon(action=%d) 失败，"
                               "GetLastError=%d"
                               % (action, ctypes.get_last_error()))
            return ok
        except Exception as e:
            self._error = "Shell_NotifyIcon 异常：%r" % (e,)
            return False

    # ---- 菜单 ----
    def _popup_menu(self):
        """右键弹菜单。返回被点中的命令 id（0 = 点外面取消）。"""
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return 0
        try:
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_SHOW,
                                "显示仪表盘")
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_TOGGLE,
                                self.toggle_label())
            _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_EXIT, "退出程序")
            pt = wt.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            hwnd = self._hwnd
            # 要点 1：不先抢前台，菜单会「点别处不消失」
            _user32.SetForegroundWindow(hwnd)
            cmd = _user32.TrackPopupMenu(
                hmenu,
                TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                pt.x, pt.y, 0, hwnd, None)
            _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            return int(cmd)
        finally:
            _user32.DestroyMenu(hmenu)

    # ---- 消息处理 ----
    def _handle(self, hwnd, msg, wparam, lparam):
        # 签名必须和 WNDPROC 一致：**4 个参数**（hwnd, msg, wparam, lparam）。
        # 少写 hwnd 会 TypeError（ctypes 回调不报错，只在 stderr 留一行
        # "Exception ignored"）——图标看似正常，实则所有消息都没处理，
        # 表现就是「双击没反应、右键没菜单」。2026-10-17 实测踩过一次。
        if msg == WM_TRAYICON:
            low = lparam & 0xFFFF
            if low == WM_LBUTTONDBLCLK:
                self._safe(self._on_show)
            elif low == WM_RBUTTONUP:
                self._on_menu_cmd(self._popup_menu())
            return 0
        if msg == WM_COMMAND:
            return 0
        if msg == WM_CLOSE:
            self._notify(NIM_DELETE)
            self._ok = False
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        # 【必须用回调给的 hwnd，不能用 self._hwnd】窗口创建期间
        # （WM_NCCREATE / WM_NCCALCSIZE）self._hwnd 还没赋值（要等
        # CreateWindowExW 返回），传进去就是 NULL；WM_NCCALCSIZE 拿到
        # DefWindowProc(NULL,...) 的 0 当作「拒绝」→ CreateWindowExW
        # 直接返回 NULL、GetLastError 却是 0（看起来毫无线索）。
        # 2026-10-17 实测卡在这里，靠打印每条消息才定位到。
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_menu_cmd(self, cmd):
        """
        菜单命令分派。**抽成独立方法是为了可测**：TrackPopupMenu 会一直
        阻塞到用户选完，自动化测试点不到；但「选中某一项该做什么」是
        最容易写错的部分（比如把暂停接成退出），必须能直接打一枪。
        """
        if cmd == CMD_SHOW:
            self._safe(self._on_show)
        elif cmd == CMD_TOGGLE:
            if self._is_paused is None:
                self._paused = not self._paused
            self._safe(self._on_toggle)
        elif cmd == CMD_EXIT:
            self._safe(self._on_exit)

    @staticmethod
    def _safe(fn):
        if fn is None:
            return
        try:
            fn()
        except Exception:
            # 托盘回调抛异常绝不能穿出去：消息循环一死，图标就变成
            # 「点了永远没反应」的僵尸（规则 37 的同一类问题）。
            pass

    def _run(self):
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            self._wndproc = ctypes.WINFUNCTYPE(
                LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(
                    self._handle)
            wc = WNDCLASSW()
            wc.style = CS_DBLCLKS          # 双击才有 WM_LBUTTONDBLCLK
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != 1410:            # 1410 = 类已注册（重复实例，可接受）
                    self._error = "RegisterClassW 失败，GetLastError=%d" % err
                    return
            self._hwnd = _user32.CreateWindowExW(
                0, self._class_name, "tray", WS_OVERLAPPED,
                0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                self._error = ("CreateWindowExW 失败，GetLastError=%d"
                               % ctypes.get_last_error())
                _user32.UnregisterClassW(self._class_name, hinst)
                return
            self._icon, self._icon_owned = _exe_icon()
            self._ok = self._notify(NIM_ADD)
            self._ready.set()
            if not self._ok:
                # 图标加不上（explorer 没起来等）就别空转消息循环
                _user32.DestroyWindow(self._hwnd)
                return
            msg = wt.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._ok = False
            try:
                import traceback
                self._error = traceback.format_exc().strip().splitlines()[-1]
            except Exception:
                self._error = "托盘线程异常"
        finally:
            try:
                if self._icon and self._icon_owned:
                    _user32.DestroyIcon(self._icon)
            except Exception:
                pass
            self._icon = None
            self._hwnd = None
            self._ready.set()

    def start(self, timeout=3.0):
        """起托盘线程。返回 True 表示图标已挂上（explorer 正常时）。"""
        if self.running:
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="tray",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.running

    def stop(self, timeout=3.0):
        """移除图标并退出消息线程。"""
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None
        self._ok = False
        self._hwnd = None
