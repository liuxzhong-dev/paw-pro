# -*- coding: utf-8 -*-
"""在子线程里复现托盘创建流程，逐步打印（临时脚本）。"""
import ctypes, ctypes.wintypes as wt, sys, threading
sys.path.insert(0, ".")
import core.tray as T

done = threading.Event()
holder = {}


def work():
    hinst = T._kernel32.GetModuleHandleW(None)
    proc = ctypes.WINFUNCTYPE(T.LRESULT, wt.HWND, wt.UINT, wt.WPARAM,
                              wt.LPARAM)(lambda h, m, w, l:
                                         T._user32.DefWindowProcW(h, m, w, l))
    wc = T.WNDCLASSW()
    wc.style = T.CS_DBLCLKS
    wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "TrayThreadDbg"
    reg = T._user32.RegisterClassW(ctypes.byref(wc))
    holder["reg"] = (reg, ctypes.get_last_error())
    hwnd = T._user32.CreateWindowExW(0, "TrayThreadDbg", "t",
                                     T.WS_OVERLAPPED, 0, 0, 0, 0,
                                     None, None, hinst, None)
    holder["hwnd"] = (hwnd, ctypes.get_last_error())
    holder["proc"] = proc
    done.set()


th = threading.Thread(target=work, daemon=True)
th.start()
done.wait(3)
print("reg =", holder.get("reg"))
print("hwnd =", holder.get("hwnd"))
