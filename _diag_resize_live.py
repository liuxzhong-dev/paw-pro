# -*- coding: utf-8 -*-
"""
实测「窗口能不能拉伸」：只看三件事，不猜。

  1. 主窗口的 GWL_STYLE 里有没有 WS_THICKFRAME（没有 = 系统根本不认缩放）
  2. 鼠标移到窗口边缘时，光标有没有变成缩放光标（没变 = 前端热区没吃到）
  3. 窗口当前尺寸 / 是否被锁死

用法：python _diag_resize_live.py
不注入键鼠，只 SetCursorPos + GetCursorInfo，结束后还原鼠标位置。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u32.FindWindowW.restype = wt.HWND
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsWindowVisible.restype = wt.BOOL
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR, ctypes.c_int]
u32.GetWindowTextLengthW.argtypes = [wt.HWND]
u32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.LoadCursorW.argtypes = [wt.HWND, ctypes.c_void_p]
u32.LoadCursorW.restype = ctypes.c_void_p
u32.GetCursorInfo.argtypes = [ctypes.c_void_p]
u32.GetCursorInfo.restype = wt.BOOL

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_CAPTION = 0x00C00000
WS_POPUP = 0x80000000
WS_MAXIMIZEBOX = 0x00010000


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", wt.POINT)]


CURSOR_NAMES = [(32642, "SIZENWSE 斜角缩放"), (32643, "SIZENESW 斜角缩放"),
                (32644, "SIZEWE 横向"), (32645, "SIZENS 纵向"),
                (32646, "SIZEALL 移动"), (32649, "HAND 手型"),
                (32512, "ARROW 普通箭头"), (32513, "IBEAM 文本")]


def cursor_name():
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not u32.GetCursorInfo(ctypes.byref(ci)):
        return "?"
    for cid, name in CURSOR_NAMES:
        h = u32.LoadCursorW(None, ctypes.c_void_p(cid))
        if h and ci.hCursor == h:
            return name
    return "其它(0x%x)" % int(ci.hCursor or 0)


def all_windows(keyword="连点"):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _enum(h, lp):
        n = u32.GetWindowTextLengthW(h)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, buf, n + 1)
            title = buf.value
            if keyword in title or "锁定" in title:
                r = wt.RECT()
                u32.GetWindowRect(h, ctypes.byref(r))
                pid = wt.DWORD()
                u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                out.append({
                    "hwnd": h, "title": title, "pid": pid.value,
                    "visible": bool(u32.IsWindowVisible(h)),
                    "w": r.right - r.left, "h": r.bottom - r.top,
                    "rect": (r.left, r.top, r.right, r.bottom),
                })
        return True

    u32.EnumWindows(_enum, 0)
    return out


def pick_main(wins):
    """
    主窗口 = 可见 + 面积最大。

    【为什么不能 FindWindowW 按标题找】同进程里还有 toast 卡片、
    隐藏消息窗，标题可能同名；实测 FindWindowW 先撞上一个
    136x39 的小窗（0x000A1448，带 WS_CAPTION，特别像主窗）。
    """
    vis = [w for w in wins if w["visible"] and w["w"] > 200 and w["h"] > 200]
    if not vis:
        vis = [w for w in wins if w["visible"]]
    vis.sort(key=lambda w: w["w"] * w["h"], reverse=True)
    return vis[0] if vis else None


def main():
    wins = all_windows()
    print("=== 标题含「连点/锁定」的顶层窗口 %d 个 ===" % len(wins))
    for w in wins:
        print("  0x%08X  pid=%-6d vis=%-5s %4dx%-4d  %s"
              % (w["hwnd"], w["pid"], w["visible"], w["w"], w["h"],
                 w["title"][:40]))
    m = pick_main(wins)
    if m is None:
        print("\n没有可用窗口，先启动程序")
        return 1
    hwnd = m["hwnd"]
    print("\n=== 认定主窗口 0x%08X（%dx%d）===" % (hwnd, m["w"], m["h"]))

    style = u32.GetWindowLongW(hwnd, GWL_STYLE)
    print("style = 0x%08X" % (style & 0xFFFFFFFF))
    print("  WS_THICKFRAME  : %s   <- 没有这一位，系统根本不认缩放"
          % ("有" if style & WS_THICKFRAME else "*** 缺 ***"))
    print("  WS_CAPTION     : %s" % ("有" if style & WS_CAPTION else "无"))
    print("  WS_MAXIMIZEBOX : %s" % ("有" if style & WS_MAXIMIZEBOX else "无"))
    print("  WS_POPUP       : %s" % ("有" if style & WS_POPUP else "无"))

    l, t, r, b = m["rect"]
    old = wt.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    print("原鼠标位置 = (%d,%d)（结束时还原）" % (old.x, old.y))

    u32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    try:
        spots = [
            ("右下角内 3px", r - 3, b - 3, "SIZENWSE"),
            ("右边缘中点",   r - 3, (t + b) // 2, "SIZEWE"),
            ("下边缘中点",   (l + r) // 2, b - 3, "SIZENS"),
            ("左边缘中点",   l + 3, (t + b) // 2, "SIZEWE"),
            ("上边缘中点",   (l + r) // 2, t + 2, "SIZENS"),
            ("标题栏中间",   (l + r) // 2, t + 20, "ARROW"),
        ]
        print("\n--- 光标实测（= 前端热区吃不吃到）---")
        for label, x, y, want in spots:
            u32.SetCursorPos(x, y)
            time.sleep(0.25)
            print("  %-12s (%5d,%5d)  光标=%-20s 期望≈%s"
                  % (label, x, y, cursor_name(), want))
    finally:
        u32.SetCursorPos(old.x, old.y)
        print("\n鼠标已还原到 (%d,%d)" % (old.x, old.y))
    return 0


if __name__ == "__main__":
    sys.exit(main())
