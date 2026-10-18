"""极简 PNG 取色：不依赖 PIL（本项目 venv 没装）。

用法：
    python _px.py 图.png x,y x,y ...        # 打印这些点的 RGB
    python _px.py 图.png --area x0,y0,x1,y1 # 打印该矩形平均色

为什么要它：浅色主题的对比度问题靠肉眼判断会来回打转，取到真实
像素值（比如「面板实测 #E6E0F0、文字实测 #C9BCE0」）才能定性。
只支持 Edge 截图产出：8 位、非隔行、RGB/RGBA。
"""
import sys
import zlib
import struct


def load(path):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and inter == 0, "只支持 8 位非隔行"
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln
    raw = zlib.decompress(idat)
    nch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = w * nch
    out, prev = [], bytearray(stride)
    p = 0
    for _ in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        for i in range(stride):
            a = line[i - nch] if i >= nch else 0
            b = prev[i]
            c = prev[i - nch] if i >= nch else 0
            x = line[i]
            if ft == 1:
                x += a
            elif ft == 2:
                x += b
            elif ft == 3:
                x += (a + b) // 2
            elif ft == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                x += a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = x & 0xFF
        out.append(bytes(line))
        prev = line
    return w, h, nch, out


def _write_png(path, w, h, rows):
    """写回 8 位 RGB PNG（每行 filter 0，够用且实现最短）。"""
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d +
                struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    open(path, "wb").write(png)


def crop(path, box, out, scale=2):
    """裁一块出来放大看：肉眼判断颜色/对齐时比整图缩略可靠。"""
    w, h, nch, rows = load(path)
    x0, y0, x1, y1 = box
    x1, y1 = min(x1, w), min(y1, h)
    cw, chh = x1 - x0, y1 - y0
    out_rows = []
    for y in range(y0, y1):
        src = rows[y]
        line = bytearray()
        for x in range(x0, x1):
            px = src[x * nch:x * nch + 3]
            line += px * scale
        for _ in range(scale):
            out_rows.append(bytes(line))
    _write_png(out, cw * scale, chh * scale, out_rows)
    print("裁剪 %s -> %s（%dx%d，x%d）" % (path, out, cw * scale, chh * scale, scale))


def main():
    path = sys.argv[1]
    if "--crop" in sys.argv:
        i = sys.argv.index("--crop")
        box = [int(v) for v in sys.argv[i + 1].split(",")]
        out = sys.argv[i + 2]
        scale = int(sys.argv[i + 3]) if len(sys.argv) > i + 3 else 2
        crop(path, box, out, scale)
        return
    w, h, nch, rows = load(path)
    print("%s  %dx%d  ch=%d" % (path, w, h, nch))
    skip = set()
    for idx, arg in enumerate(sys.argv[2:], start=2):
        if idx in skip:
            continue
        if arg == "--area":
            skip.add(idx + 1)
            x0, y0, x1, y1 = [int(v) for v in sys.argv[idx + 1].split(",")]
            n = r = g = b = 0
            for y in range(y0, min(y1, h)):
                row = rows[y]
                for x in range(x0, min(x1, w)):
                    px = row[x * nch:x * nch + 3]
                    r += px[0]; g += px[1]; b += px[2]; n += 1
            print("  区域均值 rgb(%d,%d,%d)" % (r // n, g // n, b // n))
            continue
        if arg == "--dark":
            # 一块区域里最暗的像素 = 文字颜色（近似）
            skip.add(idx + 1)
            x0, y0, x1, y1 = [int(v) for v in sys.argv[idx + 1].split(",")]
            best, bp = 999, None
            for y in range(y0, min(y1, h)):
                row = rows[y]
                for x in range(x0, min(x1, w)):
                    px = row[x * nch:x * nch + 3]
                    lum = 0.2126 * px[0] + 0.7152 * px[1] + 0.0722 * px[2]
                    if lum < best:
                        best, bp = lum, px
            if bp:
                print("  最深像素 rgb(%d,%d,%d) 亮度%.0f" % (bp[0], bp[1], bp[2], best))
            continue
        if "," in arg:
            x, y = [int(v) for v in arg.split(",")]
            if y >= h or x >= w:
                print("  (%d,%d) 越界" % (x, y))
                continue
            px = rows[y][x * nch:x * nch + 3]
            print("  (%4d,%4d) = rgb(%3d,%3d,%3d)  #%02X%02X%02X"
                  % (x, y, px[0], px[1], px[2], px[0], px[1], px[2]))


main()
