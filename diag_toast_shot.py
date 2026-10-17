# -*- coding: utf-8 -*-
"""
diag_toast_shot.py —— 把 toast 真实渲染结果抓成 BMP，用于肉眼验收 UI。

为什么需要它：所有 verify_*.py 都是 API 级断言（窗口可见、文本正确），
**验不了「画出来长什么样」**。UI 改造必须看像素，否则改了等于没改。

做法：PrintWindow(PW_RENDERFULLCONTENT) 把窗口渲染进内存 DIB，
读出 BGR 像素手写 24 位 BMP（无依赖，不用 PIL）。
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import toast  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = wt.BOOL
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int,
                         wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteDC.argtypes = [wt.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def capture(hwnd, w, h, path):
    """从屏幕 DC 抓窗口所在矩形（真实合成像素），写 24bpp BMP。

    为什么不用 PrintWindow：本窗口是 WS_EX_LAYERED +
    SetLayeredWindowAttributes(TWA_ALPHA)，PrintWindow 抓自身 DC 得到
    全空（实测 True 但像素全黑）——分层窗口的内容只存在于合成器里，
    必须从屏幕 DC 回读。
    """
    rc = (ctypes.c_long * 4)()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    left, top = rc[0], rc[1]
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = h            # 正数 = 自下而上
    hdr.biPlanes = 1
    hdr.biBitCount = 24
    hdr.biCompression = 0       # BI_RGB
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(hdr), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc_mem, hbm)
    # 先铺中灰：切角区域没内容，灰色能直接暴露窗口真实形状
    gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
    gdi32.CreateSolidBrush.restype = wt.HBRUSH
    bg = gdi32.CreateSolidBrush(0x00303030)          # 0x00BBGGRR → 深灰
    rc = (ctypes.c_long * 4)(0, 0, w, h)
    user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
    user32.FillRect(hdc_mem, ctypes.byref(rc), bg)
    gdi32.DeleteObject(bg)
    # CAPTUREBLT | SRCCOPY：CAPTUREBLT 才会把分层窗口一起合成进来
    ok = bool(gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen,
                           left, top, 0x40000000 | 0x00CC0020))

    rows = h
    stride = ((w * 3 + 3) // 4) * 4
    buf = ctypes.string_at(bits, stride * rows)
    with open(path, "wb") as f:
        size = 14 + 40 + len(buf)
        f.write(b"BM" + struct.pack("<IHHI", size, 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0,
                            len(buf), 2835, 2835, 0, 0))
        f.write(buf)

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    return ok


CASES = [
    ("start", "鼠标连点 已开始\n每秒 10 次 · 再按 中键 暂停"),
    ("ready", "鼠标连点 已就绪\n按 中键 开始连点"),
    ("pause", "鼠标连点 已暂停\n再按 中键 继续"),
    ("stop",  "鼠标连点 已停止\n回到未就绪，需重新点「就绪」"),
]

outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_shots")
os.makedirs(outdir, exist_ok=True)

hwnd = None
for kind, text in CASES:
    toast.show(text, kind=kind, seconds=8.0)
    time.sleep(0.7)
    if not hwnd:
        hwnd = user32.FindWindowW("WBMouseClickerToast", None)
    if not hwnd:
        print("!! 找不到 toast 窗口")
        break
    p = os.path.join(outdir, "shot_%s.bmp" % kind)
    real = capture(hwnd, toast.TOAST_W, toast.TOAST_H, p)
    print("shot_%s.bmp  PrintWindow=%s  %d bytes"
          % (kind, real, os.path.getsize(p)))

print("mgr.error =", repr(toast._manager().error))
toast.stop()
time.sleep(0.5)
print("done ->", outdir)
