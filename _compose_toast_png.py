# -*- coding: utf-8 -*-
"""_compose_toast_png.py —— 把四张 toast BMP 竖拼成一张交付 PNG（无 PIL）。

BMP 是 diag_toast_shot.py 自己写的 24 位 BGR 底到顶格式；PNG 手写
（zlib + 三类 chunk），避免引第三方依赖。
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "_shots")
OUT = os.path.join(HERE, "交付", "图片", "通知弹窗-四种状态.png")
GAP = 12
BG = (6, 3, 13)          # 与界面底色 #06030D 一致


def read_bmp(path):
    data = open(path, "rb").read()
    assert data[:2] == b"BM", "不是 BMP"
    pix_off = struct.unpack("<I", data[10:14])[0]
    w, h = struct.unpack("<ii", data[18:26])
    bpp = struct.unpack("<H", data[28:30])[0]
    assert bpp == 24, "只处理 24 位 BMP，实际 %d" % bpp
    stride = (w * 3 + 3) // 4 * 4
    rows = []
    for y in range(h):
        base = pix_off + (h - 1 - y) * stride     # 底到顶 → 顶到底
        row = []
        for x in range(w):
            b, g, r = data[base + x * 3: base + x * 3 + 3]
            row.append((r, g, b))
        rows.append(row)
    return w, h, rows


def write_png(path, w, h, rows):
    raw = bytearray()
    for row in rows:
        raw.append(0)                               # filter: None
        for (r, g, b) in row:
            raw += bytes((r, g, b))

    def chunk(typ, payload):
        c = struct.pack(">I", len(payload)) + typ + payload
        return c + struct.pack(">I", zlib.crc32(typ + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit RGB
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    open(path, "wb").write(png)


imgs = []
for name in ("shot_start.bmp", "shot_ready.bmp",
             "shot_pause.bmp", "shot_stop.bmp"):
    w, h, rows = read_bmp(os.path.join(SHOTS, name))
    print("%s  %dx%d" % (name, w, h))
    imgs.append((w, h, rows))

W = max(i[0] for i in imgs)
H = sum(i[1] for i in imgs) + GAP * (len(imgs) - 1)
canvas = [[BG] * W for _ in range(H)]
y = 0
for w, h, rows in imgs:
    for ry in range(h):
        canvas[y + ry][:w] = rows[ry]
    y += h + GAP

write_png(OUT, W, H, canvas)
print("saved %s  %d bytes  %dx%d" % (OUT, os.path.getsize(OUT), W, H))
