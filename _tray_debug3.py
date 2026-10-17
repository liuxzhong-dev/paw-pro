# -*- coding: utf-8 -*-
"""直接试 NIM_ADD 并打印错误码 + 结构体尺寸（临时脚本）。"""
import ctypes, ctypes.wintypes as wt, sys
sys.path.insert(0, ".")
import core.tray as T

print("sizeof NOTIFYICONDATAW =", ctypes.sizeof(T.NOTIFYICONDATAW))

hinst = T._kernel32.GetModuleHandleW(None)
wndproc = ctypes.WINFUNCTYPE(T.LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(
    lambda h, m, w, l: T._user32.DefWindowProcW(h, m, w, l))
wc = T.WNDCLASSW()
wc.style = T.CS_DBLCLKS
wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
wc.hInstance = hinst
wc.lpszClassName = "MouseAutoClickerTrayDbg3"
print("reg =", T._user32.RegisterClassW(ctypes.byref(wc)))
hwnd = T._user32.CreateWindowExW(0, "MouseAutoClickerTrayDbg3", "t", 0,
                                0, 0, 0, 0, None, None, hinst, None)
print("hwnd =", hwnd)

nid = T.NOTIFYICONDATAW()
nid.cbSize = ctypes.sizeof(T.NOTIFYICONDATAW)
nid.hWnd = hwnd
nid.uID = 1
nid.uFlags = T.NIF_MESSAGE | T.NIF_ICON | T.NIF_TIP
nid.uCallbackMessage = T.WM_TRAYICON
nid.hIcon = T._user32.LoadIconW(None, ctypes.c_void_p(T.IDI_APPLICATION))
nid.szTip = "dbg"
r = T._shell32.Shell_NotifyIconW(T.NIM_ADD, ctypes.byref(nid))
print("NIM_ADD =", r, "err =", ctypes.get_last_error(), "hIcon =", nid.hIcon)

r2 = T._shell32.Shell_NotifyIconW(T.NIM_DELETE, ctypes.byref(nid))
print("NIM_DELETE =", r2, "err =", ctypes.get_last_error())
