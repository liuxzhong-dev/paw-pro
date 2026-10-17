# -*- coding: utf-8 -*-
"""逐步复现托盘创建，打印每步的错误码（临时脚本）。"""
import ctypes, ctypes.wintypes as wt, sys
sys.path.insert(0, ".")
import core.tray as T

hinst = T._kernel32.GetModuleHandleW(None)
print("hinst =", hinst)
wndproc = ctypes.WINFUNCTYPE(T.LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(
    lambda h, m, w, l: T._user32.DefWindowProcW(h, m, w, l))
wc = T.WNDCLASSW()
wc.style = T.CS_DBLCLKS
wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
wc.hInstance = hinst
wc.lpszClassName = "MouseAutoClickerTrayDbg"
r = T._user32.RegisterClassW(ctypes.byref(wc))
print("RegisterClassW =", r, "err =", ctypes.get_last_error())
hwnd = T._user32.CreateWindowExW(0, "MouseAutoClickerTrayDbg", "tray",
                                T.WS_OVERLAPPED, 0, 0, 0, 0,
                                None, None, hinst, None)
print("CreateWindowExW =", hwnd, "err =", ctypes.get_last_error())

# 直接用真实 Tray 对象走一遍，但先重置类名避免冲突
t = T.Tray(tip="dbg2")
t._class_name = "MouseAutoClickerTrayDbg2"
ok = t.start()
print("Tray.start =", ok, "hwnd =", t._hwnd, "ok flag =", t._ok)
t.stop()
