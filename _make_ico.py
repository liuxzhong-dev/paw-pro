"""生成 assets/PawPro-violet.ico

为什么要单独写生成器（v33）：
  v32 的 ico 里 16/24/32 三档是「256 母版直接缩」出来的。母版的高光很亮（#ffd8ff），
  细尖角在 16px 上高光占比过半 → 缩略图糊成三条白条，跟 256 的紫色完全不像，
  看起来像「另一个图标」。

做法：
  1. 从母版算出「爪形软掩膜」（按亮度，不二值化，保住抗锯齿与尖角剪影）；
  2. 小尺寸把掩膜里的颜色换成饱和紫竖直渐变（去掉过曝高光），形状不动；
  3. 大尺寸（256/128/64）保留母版原样；
  4. 每一档都写成 PNG 条目（与 v32 一致；本机实测 shell 能正常解码，
     DIB 条目反而要多写一套 AND 掩码且不易自检，没必要）。

用法：python _make_ico.py
"""
import os
import struct
import io
import zlib
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "assets", "PawPro-master.png")
OUT = os.path.join(HERE, "assets", "PawPro-violet.ico")

SMALL_SIZES = (48, 32, 24, 16)   # 这几档要重上色
KEEP_SIZES = (256, 128, 64)      # 母版直接缩

# 小尺寸配色：顶部淡紫 → 底部正紫（不再有接近纯白的高光）
TOP = (222, 176, 255)
BOT = (122, 36, 210)
GAIN = {48: 1.15, 32: 1.30, 24: 1.45, 16: 1.75}


def soft_mask(master):
    w, h = master.size
    px = master.load()
    m = Image.new("L", (w, h), 0)
    mp = m.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 20:
                continue
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            v = int(max(0.0, min(1.0, (lum - 0.10) / 0.50)) * 255)
            mp[x, y] = v
    return m


def plate(master, mask, dark=(20, 6, 36, 255)):
    """把爪形位置抹成暗底色，得到只有圆角方块的底"""
    w, h = master.size
    px = master.load()
    mp = mask.load()
    bg = master.copy()
    bp = bg.load()
    for y in range(h):
        for x in range(w):
            if mp[x, y] > 8:
                bp[x, y] = dark
    return bg.filter(ImageFilter.GaussianBlur(2))


def render(size, mask, bg, gain):
    k = 8
    A = mask.resize((size * k, size * k), Image.LANCZOS)
    if gain != 1.0:
        A = A.point(lambda v: min(255, int(v * gain)))
    grad = Image.new("RGB", (size * k, size * k))
    gd = ImageDraw.Draw(grad)
    for y in range(size * k):
        t = y / (size * k - 1)
        gd.line([(0, y), (size * k, y)],
                fill=tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)))
    base = bg.resize((size * k, size * k), Image.LANCZOS)
    base.paste(grad, (0, 0), A)
    return base.resize((size, size), Image.BOX)


def png_bytes(im):
    bio = io.BytesIO()
    im.save(bio, format="PNG", optimize=True)
    return bio.getvalue()


def dib_bytes(im):
    """32bpp BITMAPINFOHEADER DIB：BXOR 位图（BGRA 自下而上）+ AND 掩码"""
    im = im.convert("RGBA")
    w, h = im.size
    px = im.load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            # 32bpp 条目里 alpha 参与，但保留一个不透明的 AND 掩码兜底
            xor += bytes((b, g, r, a))
    and_row = ((w + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray(and_row)
        for x in range(w):
            if px[x, y][3] == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        and_mask += row
    hdr = struct.pack("<IiiHHIIiiII",
                      40, w, h * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
    return hdr + bytes(xor) + bytes(and_mask)


def main():
    master = Image.open(MASTER).convert("RGBA")
    if master.size != (256, 256):
        master = master.resize((256, 256), Image.LANCZOS)
    mask = soft_mask(master)
    bg = plate(master, mask)

    entries = []  # (size, blob)
    for s in KEEP_SIZES:
        im = master if s == 256 else master.resize((s, s), Image.LANCZOS)
        entries.append((s, png_bytes(im)))
    for s in SMALL_SIZES:
        entries.append((s, png_bytes(render(s, mask, bg, GAIN[s]))))

    entries.sort(key=lambda e: -e[0])

    hdr = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dirs = b""
    blobs = b""
    for s, blob in entries:
        dirs += struct.pack("<BBBBHHII",
                            s if s < 256 else 0, s if s < 256 else 0,
                            0, 0, 1, 32, len(blob), offset)
        blobs += blob
        offset += len(blob)
    with open(OUT, "wb") as f:
        f.write(hdr + dirs + blobs)
    print("wrote", OUT, os.path.getsize(OUT), "bytes,", len(entries), "sizes")

    # 回读自检
    im = Image.open(OUT)
    print("reload ok:", im.size, "sizes:", sorted(im.info.get("sizes", [])))


if __name__ == "__main__":
    main()
