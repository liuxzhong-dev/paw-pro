# -*- coding: utf-8 -*-
"""
外观探针（v39 自定义背景 + v40 内置背景/透明度/按钮样式/裁剪保存）。

复刻的时序就是主人报 bug 的时序——「关了再重启又恢复默认」：
  ① 初始：无外观设置 → bootstrap.bg = builtin twilight，glass=1/1，btnstyle=gem
  ② save_background(dataURL) → 文件落盘 backgrounds/ + 设置写 custom
  ③ 模拟前端自动保存 save_settings({mouse,key,hide}) → bg/glass/btnstyle 不能被擦
  ④ 重新构造 Api（等价重启）→ 全部仍是用户设定值
  ⑤ set_background 切内置（纸白/墨黑）→ 落盘 + 重启保持
  ⑥ save_appearance 存透明度/按钮样式 → 落盘 + 重启保持
  ⑦ 负向对照：坏 dataURL / 未知背景 id / 越界透明度 / 未知按钮样式
  ⑧ 文件被用户删掉 → 降级默认暮光（绝不给坏链）
  ⑨ 路径穿越防护（file 只取 basename）
  ⑩ v39 → v40 迁移：旧值 {"type":"default"} 必须读成内置暮光

运行：python diag_bg_probe.py
"""
import base64
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
    print("=== v40 外观探针（背景 / 透明度 / 按钮样式 / 裁剪保存）===")
    reset_state()

    # ① 初始态：默认内置暮光 + 原质感 + 宝石按钮
    api = D.Api()
    bs = api.bootstrap()
    check("初始 bootstrap.bg = builtin twilight",
          bs.get("bg", {}).get("type") == "builtin"
          and bs["bg"].get("id") == "twilight", bs.get("bg"))
    check("初始 glass = 1/1",
          bs.get("glass") == {"panel": 1.0, "slot": 1.0}, bs.get("glass"))
    check("初始 btnstyle = gem", bs.get("btnstyle") == "gem", bs.get("btnstyle"))

    # ② 保存自定义背景（裁剪后的 dataURL）
    r = api.save_background(png_data_url())
    check("save_background ok", r.get("ok") is True, r)
    check("返回带 dataURL",
          str(r.get("bg", {}).get("url", "")).startswith("data:image/png;base64,"),
          r.get("bg"))
    p = os.path.join(D._backgrounds_dir(), "bg_custom.png")
    check("文件落盘 backgrounds/bg_custom.png", os.path.isfile(p))
    saved = api._settings.get("bg")
    check("设置写 {type:custom, file:bg_custom.png}",
          saved == {"type": "custom", "file": "bg_custom.png"}, saved)

    # ③ 外观键 + 自动保存互不干扰（v35「trigger 被擦」同款回归）
    r2 = api.save_settings({"mouse": {"cps": "7"}, "key": {"hz": "25"}, "hide": True})
    check("自动保存 ok", r2.get("ok") is True, r2)
    check("自动保存后 bg 保留 custom",
          api._settings.get("bg") == {"type": "custom", "file": "bg_custom.png"},
          api._settings.get("bg"))

    ra = api.save_appearance({"glass": {"panel": 0.7, "slot": 0.6}})
    check("save_appearance(glass) ok", ra.get("ok") is True, ra)
    check("glass 落盘为 0.7/0.6",
          api._settings.get("glass") == {"panel": 0.7, "slot": 0.6},
          api._settings.get("glass"))
    rb = api.save_appearance({"btnstyle": "pill"})
    check("save_appearance(btnstyle) ok", rb.get("ok") is True, rb)
    check("btnstyle 落盘为 pill", api._settings.get("btnstyle") == "pill",
          api._settings.get("btnstyle"))
    r2b = api.save_settings({"mouse": {"cps": "9"}, "key": {"hz": "30"}, "hide": False})
    check("再自动保存 ok", r2b.get("ok") is True, r2b)
    check("自动保存没擦掉 glass",
          api._settings.get("glass") == {"panel": 0.7, "slot": 0.6},
          api._settings.get("glass"))
    check("自动保存没擦掉 btnstyle", api._settings.get("btnstyle") == "pill",
          api._settings.get("btnstyle"))
    check("自动保存没擦掉 bg",
          api._settings.get("bg", {}).get("type") == "custom", api._settings.get("bg"))

    # ④ 模拟重启：三件套全部回读
    api2 = D.Api()
    bg2 = D._bg_entry(api2._settings.get("bg"))
    check("重启后 bg 仍 custom", bg2["type"] == "custom", bg2)
    check("重启后回读出 dataURL",
          bg2["url"].startswith("data:image/png;base64,"), bg2["url"][:40])
    bs2 = api2.bootstrap()
    check("重启 bootstrap.bg = custom", bs2["bg"]["type"] == "custom", bs2["bg"])
    check("重启 bootstrap.glass 保持 0.7/0.6",
          bs2["glass"] == {"panel": 0.7, "slot": 0.6}, bs2["glass"])
    check("重启 bootstrap.btnstyle 保持 pill",
          bs2["btnstyle"] == "pill", bs2["btnstyle"])

    # 换格式覆盖：png → jpg，旧 png 残留必须清掉
    r3 = api.save_background(jpeg_tiny())
    check("换 jpg 保存 ok", r3.get("ok") is True, r3)
    left = sorted(n for n in os.listdir(D._backgrounds_dir()) if n.startswith("bg_custom."))
    check("换格式后只剩 bg_custom.jpg", left == ["bg_custom.jpg"], left)

    # ⑤ 内置背景切换 + 重启保持
    r4 = api.set_background({"type": "builtin", "id": "paper"})
    check("切纸白 ok", r4.get("ok") is True and r4["bg"]["id"] == "paper", r4)
    api3 = D.Api()
    check("重启后仍是纸白",
          api3.bootstrap()["bg"] == {"type": "builtin", "id": "paper",
                                     "file": "", "url": ""},
          api3.bootstrap()["bg"])
    r5 = api3.set_background({"type": "builtin", "id": "ink"})
    check("切墨黑 ok", r5.get("ok") is True and r5["bg"]["id"] == "ink", r5)
    # 自定义图在切走之后仍然可换回（文件不删）
    r6 = api3.set_background({"type": "custom", "file": "bg_custom.jpg"})
    check("切回自定义图 ok", r6.get("ok") is True and r6["bg"]["type"] == "custom", r6)

    # ⑥ 恢复默认
    r7 = api3.reset_background()
    check("reset ok", r7.get("ok") is True, r7)
    check("reset 后 = 内置暮光",
          api3._settings.get("bg") == {"type": "builtin", "id": "twilight"},
          api3._settings.get("bg"))
    check("reset 后旧文件保留（可换回）",
          os.path.isfile(os.path.join(D._backgrounds_dir(), "bg_custom.jpg")))
    api4 = D.Api()
    check("重启后 = 内置暮光",
          D._bg_entry(api4._settings.get("bg"))["id"] == "twilight")

    # ⑦ 负向对照：背景
    r8 = api.save_background("https://example.com/a.png")
    check("拒绝非 dataURL", r8.get("ok") is False, r8)
    r9 = api.save_background("data:image/bmp;base64," + base64.b64encode(b"x" * 2048).decode())
    check("拒绝不支持的格式", r9.get("ok") is False, r9)
    r10 = api.save_background("data:image/png;base64,")
    check("拒绝空内容", r10.get("ok") is False, r10)
    r11 = api.save_background("data:image/png;base64,not-base64!!")
    check("拒绝坏 base64", r11.get("ok") is False, r11)
    big = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG" + b"x" * (16 * 1024 * 1024 + 1024)).decode()
    r12 = api.save_background(big)
    check("拒绝 >16MB", r12.get("ok") is False, r12)

    # ⑧ 负向对照：内置 id / 透明度越界 / 未知按钮样式
    r13 = api.set_background({"type": "builtin", "id": "rainbow"})
    check("未知背景 id 回落暮光",
          r13.get("ok") is True and r13["bg"]["id"] == "twilight", r13)
    r14 = api.save_appearance({"glass": {"panel": 9, "slot": 0.01}})
    check("透明度越界被夹到上限/下限",
          r14["glass"] == {"panel": 1.0, "slot": 0.4}, r14)
    r15 = api.save_appearance({"glass": {"panel": "abc", "slot": None}})
    check("透明度坏值回落 1/1", r15["glass"] == {"panel": 1.0, "slot": 1.0}, r15)
    r16 = api.save_appearance({"btnstyle": "neon"})
    check("未知按钮样式回落 gem", r16["btnstyle"] == "gem", r16)
    r17 = api.save_appearance("not-a-dict")
    check("非字典参数被拒", r17.get("ok") is False, r17)

    # ⑨ 文件被删 → 降级默认（不给坏链）
    os.remove(os.path.join(D._backgrounds_dir(), "bg_custom.jpg"))
    api5 = D.Api()
    api5._settings["bg"] = {"type": "custom", "file": "bg_custom.jpg"}
    bg7 = D._bg_entry(api5._settings["bg"])
    check("图片文件丢失 → 降级内置暮光",
          bg7["type"] == "builtin" and bg7["id"] == "twilight" and not bg7["url"], bg7)

    # ⑩ 路径穿越防护：file 名只允许 basename
    api5._settings["bg"] = {"type": "custom", "file": "../../ui_settings.json"}
    bg8 = D._bg_entry(api5._settings["bg"])
    check("路径穿越被 basename 挡住", bg8["type"] == "builtin", bg8)

    # ⑪ v39 → v40 迁移：旧值没有 id，只有 type=default
    check("v39 旧值 {type:default} 读成内置暮光",
          D._normalize_bg({"type": "default"}) == {"type": "builtin", "id": "twilight"},
          D._normalize_bg({"type": "default"}))
    check("缺失 bg 键读成内置暮光",
          D._normalize_bg(None) == {"type": "builtin", "id": "twilight"},
          D._normalize_bg(None))

    reset_state()
    print("\n=== 结果：%d 通过 / %d 失败 ===" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
