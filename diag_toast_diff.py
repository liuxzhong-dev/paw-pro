# -*- coding: utf-8 -*-
"""
diag_toast_diff.py —— 隐藏/显示差分，判定卡片是否被别的东西盖着。

抓屏（BitBlt + CAPTUREBLT）会把该屏幕位置上**所有**窗口一起抓进来，
包括恰好压在同位置的其它置顶窗口。只看一张图分不清「那是我们的」
还是「那是别人的」。抓两张相减，差值为 0 的区域 = toast 没画到的地方。
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import toast  # noqa: E402

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int,
                         wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def grab(left, top, w, h):
    sd = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(sd)
    hdr = BMIH()
    hdr.biSize = ctypes.sizeof(BMIH)
    hdr.biWidth, hdr.biHeight = w, -h
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 24, 0
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(hdr), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, hbm)
    gdi32.BitBlt(hdc, 0, 0, w, h, sd, left, top, 0x40000000 | 0x00CC0020)
    buf = ctypes.string_at(bits, w * 3 * h)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, sd)
    return buf


W, H = toast.TOAST_W, toast.TOAST_H
toast.show("鼠标连点 已开始\n每秒 10 次 · 再按 中键 暂停", kind="start", seconds=20)
time.sleep(0.8)
hwnd = user32.FindWindowW("WBMouseClickerToast", None)
rc = (ctypes.c_long * 4)()
user32.GetWindowRect(hwnd, ctypes.byref(rc))
left, top = rc[0], rc[1]
print("toast rect =", left, top, rc[2] - rc[0], rc[3] - rc[1])

shown = grab(left, top, W, H)
user32.ShowWindow(hwnd, 0)          # SW_HIDE
time.sleep(0.5)
hidden = grab(left, top, W, H)
user32.ShowWindow(hwnd, 4)          # SW_SHOWNOACTIVATE
time.sleep(0.4)

# 差分：统计「toast 有贡献」的像素（无贡献 = 抠掉 toast 后一模一样）
diff_rows = []
zero = 0
total = 0
same_as_bg = 0
for y in range(H):
    row = bytearray()
    for x in range(W):
        o = (y * W + x) * 3
        d = max(abs(shown[o + i] - hidden[o + i]) for i in range(3))
        total += 1
        if d == 0:
            zero += 1
        v = 255 if d > 8 else 0
        row += bytes([v, v, v])
    diff_rows.append(bytes(row))

print("同一点两次抓屏完全相同的像素: %d / %d (%.1f%%)"
      % (zero, total, 100.0 * zero / total))
# 没贡献的像素分布（按行统计，看是不是集中在某处）
for y in (2, 20, 39, 60, 74):
    n = sum(1 for x in range(W)
            if max(abs(shown[(y * W + x) * 3 + i]
                        - hidden[(y * W + x) * 3 + i]) for i in range(3)) <= 8)
    print("   y=%2d 无变化像素 %3d / %d" % (y, n, W))

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_shots", "diff_mask.bmp"), "wb") as f:
    hdr = struct.pack("<IiiHHIIiiII", 40, W, -H, 1, 24, 0,
                      len(b"".join(diff_rows)), 2835, 2835, 0, 0)
    f.write(b"BM" + struct.pack("<IHHI", 54 + len(b"".join(diff_rows)),
                                0, 0, 54) + hdr + b"".join(diff_rows))
print("diff mask -> _shots/diff_mask.bmp （白=toast 画到了，黑=没画到）")
toast.stop()
