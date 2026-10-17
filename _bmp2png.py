# -*- coding: utf-8 -*-
"""
_bmp2png.py —— 纯标准库把若干 24 位 BMP 拼成一张竖排 PNG。

为什么不用 Pillow：两个 Python 环境都没装（也不想为一张截图污染环境）。
PNG 本身只需要 zlib + crc32，手写四十行就够，还能顺便做 2x 放大
（最近邻，像素块看得清）。

用法：python _bmp2png.py 输出.png 2 输入1.bmp 输入2.bmp ...
"""
import os
import struct
import sys
import zlib


def read_bmp(path):
    """读 24bpp BI_RGB 的 BMP，返回 (w, h, 自下而上 BGR 行列表)。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"BM":
        raise ValueError("not bmp: %s" % path)
    off = struct.unpack_from("<I", data, 10)[0]
    hdr = struct.unpack_from("<IiiHH", data, 14)
    _, w, h, planes, bpp = hdr
    if bpp != 24:
        raise ValueError("only 24bpp supported, got %d" % bpp)
    flip = h > 0                  # 正高度 = 自下而上存储
    h = abs(h)
    stride = ((w * 3 + 3) // 4) * 4
    rows = []
    for y in range(h):
        s = off + y * stride
        rows.append(data[s:s + w * 3])
    if flip:
        rows.reverse()
    return w, h, rows


def png_chunk(tag, payload):
    body = tag + payload
    return (struct.pack(">I", len(payload)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def write_png(path, w, h, rows):
    """rows: 自上而下的 RGB bytes 列表（每条 w*3）。"""
    raw = bytearray()
    for r in rows:
        raw.append(0)             # filter type 0 (None)
        raw += r
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out += png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    out += png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(out)


def scale_rows(rows, k):
    """最近邻放大 k 倍（横向）。"""
    if k == 1:
        return rows
    out = []
    for r in rows:
        px = bytearray()
        for i in range(0, len(r), 3):
            px += r[i:i + 3] * k
        out.append(bytes(px))
    return out


def scale_rows_v(rows, k):
    out = []
    for r in rows:
        for _ in range(k):
            out.append(r)
    return out


def bgr_to_rgb(rows):
    out = []
    for r in rows:
        b = bytearray(r)
        b[0::3], b[2::3] = r[2::3], r[0::3]   # BGR → RGB
        out.append(bytes(b))
    return out


def main():
    out_path = sys.argv[1]
    k = int(sys.argv[2])
    srcs = sys.argv[3:]
    gap = 12
    parts = []
    W = 0
    for p in srcs:
        w, h, rows = read_bmp(p)
        rows = bgr_to_rgb(rows)
        rows = scale_rows_v(scale_rows(rows, k), k)
        W = max(W, w * k)
        parts.append(rows)
    gaprows = [bytes([0x3C] * (W * 3)) for _ in range(gap)]
    merged = []
    for i, rows in enumerate(parts):
        if i:
            merged += gaprows
        for r in rows:
            pad = W * 3 - len(r)
            merged.append(r + bytes([0x3C] * pad) if pad else r)
    write_png(out_path, W, len(merged), merged)
    print("wrote %s  %dx%d  (srcs=%d)"
          % (out_path, W, len(merged), len(srcs)))


if __name__ == "__main__":
    main()
