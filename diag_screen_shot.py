# -*- coding: utf-8 -*-
"""
抓整屏（当前桌面真实像素），存成 BMP。
用来核对「最大化后两边到底长什么样」——headless 渲染只是近似，
真实 WebView2 的绘制结果和窗口矩形才是事实。
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import os

u = ctypes.windll.user32
g = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        u.SetProcessDPIAware()
    except Exception:
        pass

u.GetDC.argtypes = [wt.HWND]
u.GetDC.restype = wt.HDC
u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
g.CreateCompatibleDC.argtypes = [wt.HDC]
g.CreateCompatibleDC.restype = wt.HDC
g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
g.CreateCompatibleBitmap.restype = wt.HBITMAP
g.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
g.SelectObject.restype = wt.HANDLE
u.GetSystemMetrics.argtypes = [ctypes.c_int]
u.GetSystemMetrics.restype = ctypes.c_int
g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                        ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
g.GetDIBits.restype = ctypes.c_int
# 硬规则 2：不声明 DWORD 参数会 OverflowError（0x40000000 当成了有符号）
g.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int,
                     wt.DWORD]
g.BitBlt.restype = wt.BOOL
g.DeleteObject.argtypes = [wt.HANDLE]
g.DeleteObject.restype = wt.BOOL
g.DeleteDC.argtypes = [wt.HDC]
g.DeleteDC.restype = wt.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


out = sys.argv[1] if len(sys.argv) > 1 else "_shots/screen.bmp"
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

W = u.GetSystemMetrics(0)
H = u.GetSystemMetrics(1)
src = u.GetDC(None)
mem = g.CreateCompatibleDC(src)
hb = g.CreateCompatibleBitmap(src, W, H)
g.SelectObject(mem, hb)
# CAPTUREBLT 保证把分层窗口（toast 之类）也抓进去
g.BitBlt(mem, 0, 0, W, H, src, 0, 0, 0x00CC0020 | 0x40000000)

bi = BITMAPINFOHEADER()
bi.biSize = ctypes.sizeof(bi)
bi.biWidth = W
bi.biHeight = -H          # 负高度 = 自上而下
bi.biPlanes = 1
bi.biBitCount = 24
buf = ctypes.create_string_buffer(W * H * 3 + 8)
g.GetDIBits(mem, hb, 0, H, buf, ctypes.byref(bi), 0)
data = buf.raw[:W * H * 3]

with open(out, "wb") as f:
    f.write(b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54))
    f.write(struct.pack("<IiiHHIIiiII", 40, W, H, 1, 24, 0,
                        len(data), 2835, 2835, 0, 0))
    f.write(data)

g.DeleteObject(hb)
g.DeleteDC(mem)
u.ReleaseDC(None, src)
print("抓屏 %dx%d -> %s" % (W, H, out))
