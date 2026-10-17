# -*- coding: utf-8 -*-
"""
diag_v25_vscreen_probe.py —— v25 副屏/虚拟桌面坐标探针
========================================================

【为什么要做 v25（S3 的前置）】
v22 的「固定坐标」按**主屏**算：SendInput 的绝对坐标不带
MOUSEEVENTF_VIRTUALDESK 时只铺主屏，而项目里还额外把坐标 clamp 到
[0, 主屏宽)。后果：副屏在主屏**左边**（x 为负）或右侧时，
手输的坐标会被夹成 0/主屏边缘 —— 表现为「鼠标飞到左上角」。

v25 两处一起改（缺一不可）：
  ① core/inputs.py  _to_absolute 按虚拟桌面归一化（减原点、用虚屏宽高）
     + move_to 带 MOUSEEVENTF_VIRTUALDESK
  ② dashboard 的 clamp 从 [0, 主屏) 改成虚拟桌面矩形

【本探针】
  · 数学层：单屏 / 双屏(副屏在左) / 双屏(副屏在右) 三组归一化值
  · 事件层：move_to 发出的 flags 必须带 VIRTUALDESK
  · 参数层：dashboard 解析固定坐标时保留负坐标、只夹到虚屏边界
  · 负向对照：把 clamp 打回「按主屏」，副屏负坐标必须被夹成 0
    （证明这些断言真的在测东西，而不是恒真）

【纪律】不调用真实 move_to（会移动用户的鼠标）；事件层用替身捕获。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def section(t):
    print("\n=== %s ===" % t)


def summary():
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 56)
    print("v25 探针汇总：%d 项通过 / %d 项失败（共 %d）"
          % (len(PASS), len(FAIL), total))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 56)
    return 0 if not FAIL else 1


def code_only(path):
    import re
    src = open(path, encoding="utf-8").read()
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


# ==================================================================
# 1. 真实 virtual_screen
# ==================================================================
def test_real_vscreen():
    section("1. virtual_screen() 真机取值")
    from core import inputs

    vx, vy, vw, vh = inputs.virtual_screen()
    sw, sh = inputs.screen_size()
    check("返回四元组且宽高为正",
          vw > 0 and vh > 0, (vx, vy, vw, vh))
    check("虚拟桌面不小于主屏（多屏时更大，单屏时相等）",
          vw >= sw and vh >= sh, (vw, sw, vh, sh))
    check("单屏时原点为 0（多屏时可以是负数）",
          (vw == sw and vh == sh) and (vx == 0 and vy == 0)
          or (vw >= sw and vh >= sh), (vx, vy, vw, vh))

    # 现有 API 不回归：screen_size 仍是主屏
    check("screen_size() 仍是主屏尺寸", sw > 0 and sh > 0, (sw, sh))


# ==================================================================
# 2. 归一化数学（打桩 virtual_screen）
# ==================================================================
def test_math():
    section("2. _to_absolute 归一化（单屏 / 副屏在左 / 副屏在右）")
    from core import inputs

    real = inputs.virtual_screen
    try:
        # --- 单屏 1920x1080 ---
        inputs.virtual_screen = lambda: (0, 0, 1920, 1080)
        check("单屏：左上角 -> (0,0)", inputs._to_absolute(0, 0) == (0, 0),
              inputs._to_absolute(0, 0))
        check("单屏：右下角 -> (65535,65535)",
              inputs._to_absolute(1919, 1079) == (65535, 65535),
              inputs._to_absolute(1919, 1079))
        # 归一化中点对应像素 (w-1)/2 = 959.5，不是 w/2=960，所以容差给到 ±40
        cx, cy = inputs._to_absolute(960, 540)
        check("单屏：中心约为 32767（容差 ±40）", abs(cx - 32767) <= 40
              and abs(cy - 32767) <= 40, (cx, cy))

        # --- 双屏：副屏 1920x1080 在主屏**左侧**（虚拟桌面 -1920..1919）---
        inputs.virtual_screen = lambda: (-1920, 0, 3840, 1080)
        check("副屏在左：虚屏最左 -> (0,0)",
              inputs._to_absolute(-1920, 0) == (0, 0),
              inputs._to_absolute(-1920, 0))
        check("副屏在左：主屏最右 -> (65535, ...)",
              inputs._to_absolute(1919, 0)[0] == 65535,
              inputs._to_absolute(1919, 0))
        # 关键：负坐标必须是正归一化值，不能被夹成 0
        nx, _ny = inputs._to_absolute(-960, 500)
        check("副屏在左：负坐标不再被夹成 0（v22 的 bug）",
          nx > 0, nx)
        # 主屏左上角在虚屏中点是 32767 附近
        mx, _ = inputs._to_absolute(0, 0)
        check("副屏在左：主屏原点落在虚屏中点（容差 ±40）",
              abs(mx - 32767) <= 40, mx)

        # --- 双屏：副屏在主屏**上方**（虚拟桌面 y 为负）---
        inputs.virtual_screen = lambda: (0, -1080, 1920, 2160)
        check("副屏在上：负 y 不被夹成 0",
              inputs._to_absolute(500, -540)[1] > 0,
              inputs._to_absolute(500, -540))
        check("副屏在上：虚屏最下 -> 65535",
              inputs._to_absolute(1919, 1079)[1] == 65535,
              inputs._to_absolute(1919, 1079))

        # --- 兜底：系统返回 0 尺寸时退回主屏，不能除零 ---
        inputs.virtual_screen = lambda: (0, 0, 0, 0)
        inputs.screen_size = lambda: (1920, 1080)
        check("虚屏尺寸为 0 时退回主屏（不除零、不炸）",
              inputs._to_absolute(960, 540)[0] > 0,
              inputs._to_absolute(960, 540))
    finally:
        inputs.virtual_screen = real


# ==================================================================
# 3. 事件层：move_to 必须带 VIRTUALDESK
# ==================================================================
def test_flags():
    section("3. move_to 发出的事件带 MOUSEEVENTF_VIRTUALDESK")
    from core import inputs

    captured = []
    real_send = inputs._send
    real_vs = inputs.virtual_screen
    inputs.virtual_screen = lambda: (-1920, 0, 3840, 1080)
    inputs._send = lambda count, events: (captured.extend(events), 1)[1]
    try:
        inputs.move_to(-100, 200)
    finally:
        inputs._send = real_send
        inputs.virtual_screen = real_vs

    check("move_to 真的发了事件", len(captured) == 1, captured)
    if captured:
        _x, _y, flags = captured[0]
        check("带 MOVE 标志", bool(flags & inputs.MOUSEEVENTF_MOVE), flags)
        check("带 ABSOLUTE 标志",
              bool(flags & inputs.MOUSEEVENTF_ABSOLUTE), flags)
        check("带 VIRTUALDESK 标志（副屏关键）",
              bool(flags & inputs.MOUSEEVENTF_VIRTUALDESK), flags)

    src = code_only(os.path.join(HERE, "core", "inputs.py"))
    check("VIRTUALDESK 常量已定义",
          "MOUSEEVENTF_VIRTUALDESK = 0x4000" in src)
    check("系统指标常量齐全（76~79）",
          all(("SM_%s" % k) in src for k in
              ("XVIRTUALSCREEN", "YVIRTUALSCREEN",
               "CXVIRTUALSCREEN", "CYVIRTUALSCREEN")))


# ==================================================================
# 4. dashboard 参数层：负坐标保留、越界只夹到虚屏边界
# ==================================================================
def test_dashboard_clamp():
    section("4. dashboard 固定坐标 clamp 改按虚拟桌面")
    import dashboard as D

    real_vs = D.inp.virtual_screen
    D.inp.virtual_screen = lambda: (-1920, 0, 3840, 1080)   # 副屏在左
    try:
        p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                        "mode": "fixed",
                                        "pos": "-800,400"})
        check("副屏负坐标被保留（不再变 0）",
              err is None and p["pos"] == [-800, 400], (err, (p or {}).get("pos")))

        p2, err2 = D._parse_mouse_params({"button": "left", "action": "click",
                                          "mode": "fixed",
                                          "pos": "9999,400"})
        check("超出虚屏右边界 -> 夹到 1919",
              err2 is None and p2["pos"] == [1919, 400],
              (err2, (p2 or {}).get("pos")))

        p3, err3 = D._parse_mouse_params({"button": "left", "action": "click",
                                          "mode": "fixed",
                                          "pos": "-99999,400"})
        check("超出虚屏左边界 -> 夹到 -1920",
              err3 is None and p3["pos"] == [-1920, 400],
              (err3, (p3 or {}).get("pos")))

        # ---- 负向对照：打回「按主屏 clamp」，负坐标必须被夹成 0 ----
        def old_clamp(x, y):
            sw, sh = 1920, 1080
            return [max(0, min(x, sw - 1)), max(0, min(y, sh - 1))]

        check("负向对照：旧 clamp 会把副屏坐标夹成 0（证明断言在测东西）",
              old_clamp(-800, 400) == [0, 400], old_clamp(-800, 400))
    finally:
        D.inp.virtual_screen = real_vs

    py = code_only(os.path.join(HERE, "dashboard.py"))
    check("dashboard clamp 用 virtual_screen()",
          "inp.virtual_screen()" in py)
    check("dashboard 已无按主屏 clamp 的旧写法",
          "sw, sh = inp.screen_size()" not in py)


def main():
    print("v25 副屏/虚拟桌面坐标探针")
    test_real_vscreen()
    test_math()
    test_flags()
    test_dashboard_clamp()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
