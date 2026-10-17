# -*- coding: utf-8 -*-
"""
_toast_render.py —— toast 卡片离屏渲染 helper（诊断 + 回归测试共用）。

为什么需要：卡片画在 WS_EX_LAYERED 窗口上，内容归 DWM 合成器，
PrintWindow / 屏幕回读都拿不到干净像素（实测：文字在、底色是抓屏背景）。
把 _paint_card(hdc) 喂一个内存 DIB 才是绘制代码的真实输出，
UI 这才变成「可断言」而不是「看着差不多」。
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys

if __package__ is None:
    sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

from core import toast  # noqa: E402

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32
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
gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
user32.FillRect.restype = ctypes.c_int
# 必须声明 restype：不声明时按 c_int 读返回值，64 位下 DC 句柄可能被截断
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class Card(object):
    """一次离屏渲染的结果，带像素读取与 BMP 导出。"""

    def __init__(self, kind, text, sentinel=None):
        w, h = toast.TOAST_W, toast.TOAST_H
        sd = user32.GetDC(None)
        self.hdc = gdi32.CreateCompatibleDC(sd)
        hdr = BMIH()
        hdr.biSize = ctypes.sizeof(BMIH)
        hdr.biWidth, hdr.biHeight = w, -h     # 负 = 自上而下
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 24, 0
        self.bits = ctypes.c_void_p()
        self.hbm = gdi32.CreateDIBSection(self.hdc, ctypes.byref(hdr), 0,
                                          ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.hdc, self.hbm)
        self.w, self.h = w, h
        self.stride = ((w * 3 + 3) // 4) * 4
        self._sd = sd
        self.sentinel = sentinel
        if sentinel is not None:
            br = gdi32.CreateSolidBrush(toast._ToastManager._rgb(sentinel))
            rc = (ctypes.c_long * 4)(0, 0, w, h)
            user32.FillRect(self.hdc, ctypes.byref(rc), br)
            gdi32.DeleteObject(br)

        mgr = toast._manager()
        mgr._cur_kind = kind
        mgr._cur_text = text
        mgr.error = ""
        mgr._paint_card(self.hdc)
        self.error = mgr.error

    def px(self, x, y):
        """返回 (r, g, b)。"""
        b = ctypes.string_at(self.bits.value + y * self.stride + x * 3, 3)
        return (b[2], b[1], b[0])

    def rows(self):
        return [ctypes.string_at(self.bits.value + y * self.stride, self.w * 3)
                for y in range(self.h)]

    def save_bmp(self, path):
        buf = b"".join(self.rows())
        with open(path, "wb") as f:
            # DIB 自上而下 → BMP 头也写负高度，否则读图方会上下颠倒
            f.write(b"BM" + struct.pack("<IHHI", 54 + len(buf), 0, 0, 54))
            f.write(struct.pack("<IiiHHIIiiII", 40, self.w, -self.h, 1, 24, 0,
                                len(buf), 2835, 2835, 0, 0))
            f.write(buf)

    def close(self):
        gdi32.SelectObject(self.hdc, self.old)
        gdi32.DeleteObject(self.hbm)
        gdi32.DeleteDC(self.hdc)
        user32.ReleaseDC(None, self._sd)


def render(kind, text, sentinel=None):
    return Card(kind, text, sentinel=sentinel)
