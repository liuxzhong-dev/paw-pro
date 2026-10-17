# -*- coding: utf-8 -*-
"""
最小复现：PostThreadMessageW -> GetMessageW 到底通不通。
排除 toast 管理器全部逻辑，只留线程 + 窗口 + 消息循环。
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)
WM_APP_SHOW = 0x8001


def wnd_proc(hwnd, msg, wp, lp):
    print("  [wnd_proc] 收到消息 msg=0x%X" % msg, flush=True)
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


got = []


def worker():
    wndproc = WNDPROC(wnd_proc)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint),
                    ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbClsExtra2", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p),
                    ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR)]

    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "MinReproClass"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        print("RegisterClassW 失败 err=%r" % kernel32.GetLastError(), flush=True)
        return
    hwnd = user32.CreateWindowExW(0, "MinReproClass", "t", 0x80000000,
                                  0, 0, 100, 100, None, None,
                                  kernel32.GetModuleHandleW(None), None)
    print("worker: CreateWindowExW -> 0x%X tid=%r"
          % (hwnd or 0, kernel32.GetCurrentThreadId()), flush=True)
    msg = wt.MSG()
    print("worker: 进入 GetMessageW 循环", flush=True)
    while True:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        print("worker: GetMessageW r=%r msg=0x%X hwnd=0x%X"
              % (r, msg.message, msg.hwnd or 0), flush=True)
        if r <= 0:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


t = threading.Thread(target=worker, daemon=True)
t.start()
time.sleep(1.0)

tid = kernel32.GetCurrentThreadId()  # 先看主线程 tid
print("主线程 tid=%r" % tid, flush=True)

# 找 worker 的 tid：通过 EnumWindows 反查太麻烦，直接再发到主线程自己
print("主线程: PostThreadMessageW 到自己 tid=%r" % tid, flush=True)
ok = user32.PostThreadMessageW(tid, WM_APP_SHOW, 0, 0)
print("主线程: ret=%r" % ok, flush=True)
msg = wt.MSG()
# 主线程自己 GetMessage 一次（带超时用 MsgWaitForMultipleObjects 简化：直接 PeekMessage）
time.sleep(0.3)
r = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # PM_REMOVE=1
print("主线程: PeekMessageW ret=%r msg=0x%X" % (r, msg.message), flush=True)

print("[done]", flush=True)
