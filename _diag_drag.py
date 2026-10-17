# -*- coding: utf-8 -*-
"""
在「非管理员」的诊断实例上真拖一次边缘，看窗口尺寸变不变。

用法：python _diag_drag.py [left|top|right|bottom|br]
默认 left。注入真实鼠标事件（mouse_event），结束后还原鼠标位置。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsWindowVisible.restype = wt.BOOL
u32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
u32.mouse_event.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                            ctypes.c_uint, ctypes.c_size_t]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

LDOWN, LUP = 0x0002, 0x0004


def pick_window():
    """挑面积最大的可见「连点」窗口（诊断实例 1164x781）。"""
    cands = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, lp):
        n = u32.GetWindowTextLengthW(h)
        if n > 0 and u32.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, b, n + 1)
            if "连点" in b.value:
                r = wt.RECT()
                u32.GetWindowRect(h, ctypes.byref(r))
                cands.append((h, r))
        return True

    u32.EnumWindows(cb, 0)
    cands.sort(key=lambda c: (c[1].right - c[1].left) *
               (c[1].bottom - c[1].top), reverse=True)
    return cands[0] if cands else (None, None)


def main():
    edge = (sys.argv[1] if len(sys.argv) > 1 else "left").lower()
    h, r = pick_window()
    if not h:
        print("没找到窗口")
        return 1
    print("窗口 0x%08X  %dx%d @ (%d,%d)"
          % (h, r.right - r.left, r.bottom - r.top, r.left, r.top))
    u32.SetForegroundWindow(h)
    time.sleep(0.5)
    old = wt.POINT()
    u32.GetCursorPos(ctypes.byref(old))

    l, t, rr, b = r.left, r.top, r.right, r.bottom
    spots = {
        "left":   (l + 3, (t + b) // 2, -8, 0),
        "right":  (rr - 3, (t + b) // 2, 8, 0),
        "top":    ((l + rr) // 2, t + 2, 0, -6),
        "bottom": ((l + rr) // 2, b - 3, 0, 6),
        "br":     (rr - 3, b - 3, 8, 6),
    }
    x, y, dx, dy = spots.get(edge, spots["left"])
    try:
        u32.SetCursorPos(x, y)
        time.sleep(0.4)
        u32.mouse_event(LDOWN, 0, 0, 0, 0)
        time.sleep(0.35)
        for i in range(1, 11):
            u32.SetCursorPos(x + dx * i, y + dy * i)
            time.sleep(0.06)
        time.sleep(0.25)
        u32.mouse_event(LUP, 0, 0, 0, 0)
        time.sleep(0.6)
    finally:
        u32.SetCursorPos(old.x, old.y)

    r2 = wt.RECT()
    u32.GetWindowRect(h, ctypes.byref(r2))
    print("拖后 %dx%d @ (%d,%d)"
          % (r2.right - r2.left, r2.bottom - r2.top, r2.left, r2.top))
    print("尺寸变化 dx=%d dy=%d  %s"
          % ((r2.right - r2.left) - (r.right - r.left),
             (r2.bottom - r2.top) - (r.bottom - r.top),
             "← 真的缩放了" if (r2.right - r2.left) != (r.right - r.left)
             or (r2.bottom - r2.top) != (r.bottom - r.top) else "← 完全没动"))
    print("鼠标已还原到 (%d,%d)" % (old.x, old.y))
    return 0


if __name__ == "__main__":
    sys.exit(main())
