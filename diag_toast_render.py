# -*- coding: utf-8 -*-
"""
diag_toast_render.py —— 把 toast 卡片离屏渲染到内存 DC，导出 BMP 供肉眼验收。

为什么不用截屏：窗口是 WS_EX_LAYERED，内容在 DWM 合成器里，
PrintWindow / 屏幕回读都拿不到干净结果（实测抓到的是「文字在、
底色空」的假象）。离屏渲染喂内存 DC 才是**绘制代码本身**的真实输出，
也顺便让 UI 变成可断言的对象。

用法：python diag_toast_render.py            导出全部状态
     python diag_toast_render.py start      只导一个
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import toast  # noqa: E402
from _bmp2png import bgr_to_rgb, read_bmp, scale_rows, scale_rows_v, write_png  # noqa: E402

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
gdi32.DeleteDC.argtypes = [wt.HDC]


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def render(kind, text, path):
    w, h = toast.TOAST_W, toast.TOAST_H
    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    hdr = BMIH()
    hdr.biSize = ctypes.sizeof(BMIH)
    hdr.biWidth, hdr.biHeight = w, -h     # 负数 = 自上而下，省得翻转
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 24, 0
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(hdr), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, hbm)

    mgr = toast._manager()
    mgr._cur_kind = kind
    mgr._cur_text = text
    mgr._paint_card(hdc)
    if mgr.error:
        print("   !! mgr.error =", repr(mgr.error))

    stride = ((w * 3 + 3) // 4) * 4
    buf = ctypes.string_at(bits, stride * h)
    with open(path, "wb") as f:
        # DIB 是自上而下（biHeight 负数）→ BMP 头也必须写负高度，
        # 否则读图方按自下而上解释，画面会上下颠倒（踩过一次）。
        f.write(b"BM" + struct.pack("<IHHI", 54 + len(buf), 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 24, 0,
                            len(buf), 2835, 2835, 0, 0))
        f.write(buf)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, hdc_screen)
    return (w, h, buf, stride)


CASES = [
    ("start", "鼠标连点 已开始\n每秒 10 次 · 再按 中键 暂停"),
    ("ready", "鼠标连点 已就绪\n按 中键 开始连点"),
    ("pause", "鼠标连点 已暂停\n再按 中键 继续"),
    ("stop",  "鼠标连点 已停止\n回到未就绪，需重新点「就绪」"),
]

want = sys.argv[1] if len(sys.argv) > 1 else None
here = os.path.dirname(os.path.abspath(__file__))
outdir = os.path.join(here, "_shots")
os.makedirs(outdir, exist_ok=True)

made = []
for kind, text in CASES:
    if want and kind != want:
        continue
    p = os.path.join(outdir, "render_%s.bmp" % kind)
    w, h, buf, stride = render(kind, text, p)

    def px(x, y):
        o = y * stride + x * 3
        return (buf[o + 2], buf[o + 1], buf[o])     # BMP 是 BGR

    print("%-6s -> %s" % (kind, os.path.basename(p)))
    print("        卡内(6,60)=%s  顶(200,3)=%s  底(200,74)=%s  角(3,3)=%s"
          % (px(6, 60), px(200, 3), px(200, 74), px(3, 3)))
    made.append(p)

if len(made) > 1:
    rows_all = []
    W = 0
    for p in made:
        w, h, r = read_bmp(p)
        r = scale_rows_v(scale_rows(bgr_to_rgb(r), 2), 2)
        W = max(W, w * 2)
        rows_all.append(r)
        rows_all.append([bytes([0x3C] * (W * 3))] * 10)
    flat = [r for grp in rows_all for r in grp]
    combined = os.path.join(outdir, "render_combined.png")
    write_png(combined, W, len(flat), flat)
    print("combined ->", combined)
