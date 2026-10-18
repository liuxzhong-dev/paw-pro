# -*- coding: utf-8 -*-
"""
v39 自定义背景探针：save_background / reset_background / 重启恢复。

复刻的时序（也是主人报 bug 的时序）：
  ① 初始：没有 bg 设置 → bootstrap.bg = default
  ② save_background(dataURL) → 文件落盘 backgrounds/ + 设置写 custom
  ③ 模拟前端自动保存 save_settings({mouse,key,hide}) → bg 不能被擦
  ④ 重新构造 Api（等价重启）→ bg 仍是 custom，且回读出 dataURL
  ⑤ reset_background() → 设置回 default；文件保留可换回
  ⑥ 坏输入负向对照：非 dataURL / 不支持的格式 / 空内容 / 超限
  ⑦ 文件被用户删掉 → _bg_entry 必须降级 default（绝不给坏链）

运行：python diag_bg_probe.py
"""
import base64
import importlib
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dashboard as D  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK  %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (name, detail))


def make_png(w, h, rgb):
    """纯 Python 现造一张合法 PNG（测试环境没有 PIL，也不能依赖外部图）。"""
    raw = b""
    for _y in range(h):
        raw += b"\x00" + bytes(rgb) * w

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d +
                struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    return png


def png_data_url():
    return "data:image/png;base64," + base64.b64encode(
        make_png(64, 64, (123, 90, 200))).decode("ascii")


def jpeg_tiny():
    return "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"x" * 2048).decode()


def reset_state():
    p = D._settings_path()
    if os.path.isfile(p):
        os.remove(p)
    d = D._backgrounds_dir()
    for n in os.listdir(d):
        if n.startswith("bg_custom."):
            os.remove(os.path.join(d, n))


def main():
    print("=== v39 自定义背景探针 ===")
    reset_state()

    # ① 初始态
    api = D.Api()
    bg0 = D._bg_entry(api._settings.get("bg"))
    check("初始 bg = default", bg0["type"] == "default" and not bg0["url"], bg0)
    bs = api.bootstrap()
    check("bootstrap 带 bg 字段且为 default",
          isinstance(bs.get("bg"), dict) and bs["bg"]["type"] == "default", bs.get("bg"))

    # ② 保存自定义背景
    r = api.save_background(png_data_url())
    check("save_background ok", r.get("ok") is True, r)
    check("返回带 dataURL", str(r.get("bg", {}).get("url", "")).startswith("data:image/png;base64,"), r.get("bg"))
    p = os.path.join(D._backgrounds_dir(), "bg_custom.png")
    check("文件落盘 backgrounds/bg_custom.png", os.path.isfile(p))
    saved = api._settings.get("bg")
    check("设置写 {type:custom, file:bg_custom.png}",
          saved == {"type": "custom", "file": "bg_custom.png"}, saved)

    # ③ 前端自动保存不能擦掉 bg（v35 同款回归：局部表单覆盖全量）
    r2 = api.save_settings({"mouse": {"cps": "7"}, "key": {"hz": "25"}, "hide": True})
    check("自动保存 ok", r2.get("ok") is True, r2)
    check("自动保存后 bg 保留 custom",
          api._settings.get("bg") == {"type": "custom", "file": "bg_custom.png"},
          api._settings.get("bg"))

    # ④ 模拟重启（新 Api 读盘）
    api2 = D.Api()
    bg2 = D._bg_entry(api2._settings.get("bg"))
    check("重启后 bg 仍 custom", bg2["type"] == "custom", bg2)
    check("重启后回读出 dataURL", bg2["url"].startswith("data:image/png;base64,"), bg2["url"][:40])
    bs2 = api2.bootstrap()
    check("重启 bootstrap.bg = custom", bs2["bg"]["type"] == "custom", bs2["bg"])

    # 换格式覆盖：png → jpg，旧 png 残留必须清掉
    r3 = api.save_background(jpeg_tiny())
    check("换 jpg 保存 ok", r3.get("ok") is True, r3)
    left = sorted(n for n in os.listdir(D._backgrounds_dir()) if n.startswith("bg_custom."))
    check("换格式后只剩 bg_custom.jpg", left == ["bg_custom.jpg"], left)

    # ⑤ 恢复默认
    r4 = api2.reset_background()
    check("reset ok", r4.get("ok") is True, r4)
    check("reset 后设置 = default", api2._settings.get("bg") == {"type": "default"}, api2._settings.get("bg"))
    check("reset 后旧文件保留（可换回）", os.path.isfile(os.path.join(D._backgrounds_dir(), "bg_custom.jpg")))
    api3 = D.Api()
    check("重启后 bg = default", D._bg_entry(api3._settings.get("bg"))["type"] == "default")

    # ⑥ 负向对照
    r5 = api.save_background("https://example.com/a.png")
    check("拒绝非 dataURL", r5.get("ok") is False, r5)
    r6 = api.save_background("data:image/bmp;base64," + base64.b64encode(b"x" * 2048).decode())
    check("拒绝不支持的格式", r6.get("ok") is False, r6)
    r7 = api.save_background("data:image/png;base64,")
    check("拒绝空内容", r7.get("ok") is False, r7)
    r8 = api.save_background("data:image/png;base64,not-base64!!")
    check("拒绝坏 base64", r8.get("ok") is False, r8)
    big = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"x" * (16 * 1024 * 1024 + 1024)).decode()
    r9 = api.save_background(big)
    check("拒绝 >16MB", r9.get("ok") is False, r9)

    # ⑦ 文件被删 → 降级 default（不给坏链）
    os.remove(os.path.join(D._backgrounds_dir(), "bg_custom.jpg"))
    api4 = D.Api()
    api4._settings["bg"] = {"type": "custom", "file": "bg_custom.jpg"}
    bg7 = D._bg_entry(api4._settings.get("bg"))
    check("图片文件丢失 → 降级 default", bg7["type"] == "default" and not bg7["url"], bg7)

    # ⑧ 路径穿越防护：file 名只允许 basename
    api4._settings["bg"] = {"type": "custom", "file": "../../ui_settings.json"}
    bg8 = D._bg_entry(api4._settings.get("bg"))
    check("路径穿越被 basename 挡住", bg8["type"] == "default", bg8)

    reset_state()
    print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
