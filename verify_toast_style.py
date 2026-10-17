# -*- coding: utf-8 -*-
"""
verify_toast_style.py —— 通知弹窗「紫霓虹科技风 + 与主界面统一」回归测试。

## 为什么单独一个套件

UI 改动的验证以前是空白：verify_toast_delivery 只验「消息送得到」，
验不了「画出来对不对」。2026-10-16 本轮实测就撞上两个**打桩测试
永远发现不了**的坑：

  1. GradientFill 顶点写成 (0,0)-(0,H) → 矩形宽 0 → **返回成功却
     一个像素不画**，整张卡片底色全空（文字和圆点还在，特别像
     「只是风格不对」）；
  2. Ellipse 用的刷子/画笔没 SelectObject 进 DC → 状态圆点画成
     默认白刷 + 黑描边，跟状态色无关。

这两个都是「静默失效」，只有**看像素**才抓得到。所以本套件：
  · 离屏渲染真实绘制代码（_toast_render.py），直接断言像素；
  · 从 ui/dashboard.html 的 :root 解析设计 token 做**跨源比对**——
    主界面改色而 toast 没跟上，这里必须红。

跑法：python verify_toast_style.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import toast                                    # noqa: E402
from _toast_render import render                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("OK" if ok else "!!", name,
                           ("  <- " + detail) if detail and not ok else ""))


def near(a, b, tol):
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


# ----------------------------------------------------------------------
print("\n[1] 跨源配色一致性（toast 常量 vs ui/dashboard.html 的 :root）")
# ----------------------------------------------------------------------
html = open(os.path.join(HERE, "ui", "dashboard.html"),
            encoding="utf-8").read()


def token(name):
    m = re.search(r"--%s:\s*#([0-9A-Fa-f]{6})" % re.escape(name), html)
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None


def hexof(t):
    return "#%02X%02X%02X" % (t[0], t[1], t[2])


for var, const, label in (
        ("accent", toast.TOAST_STATE["start"], "运行态=accent-violet"),
        ("accent-mint", toast.TOAST_STATE["ready"], "就绪态=holo-mint"),
        ("spark", toast.TOAST_STATE["pause"], "暂停/降级=spark-amber"),
        ("alert", toast.TOAST_STATE["error"], "错误态=alert-red"),
        ("tx-dim", toast.TOAST_STATE["stop"], "停止态=tx-dim"),
        ("tx-hi", toast.TOAST_TEXT_HI, "标题字色=tx-hi"),
        ("tx-mid", toast.TOAST_TEXT_MID, "说明字色=tx-mid"),
        ("panel", toast.TOAST_BG_BOTTOM, "卡片底色=panel")):
    v = token(var)
    check("--%s %s 与 toast 常量一致" % (var, label),
          v is not None and v == const,
          "dashboard=%s toast=%s" % (v and hexof(v), hexof(const)))

m = re.search(r"--chamfer:\s*(\d+)px", html)
check("--chamfer 与 TOAST_CHAMFER 一致",
      bool(m) and int(m.group(1)) == toast.TOAST_CHAMFER,
      "dashboard=%s toast=%s" % (m and m.group(1), toast.TOAST_CHAMFER))
_flat = re.sub(r"\s+", "", html)
check("主界面用 clip-path polygon 切角（同构形状语言）",
      "clip-path:polygon(var(--chamfer)0," in _flat)

# ----------------------------------------------------------------------
print("\n[2] 离屏渲染像素断言（真实绘制代码的输出）")
# ----------------------------------------------------------------------
SENTINEL = (0xFF, 0x00, 0xFF)          # 洋红：任何残留都说明没画到
CASES = [
    ("start", "鼠标连点 已开始\n每秒 10 次 · 再按 中键 暂停"),
    ("ready", "鼠标连点 已就绪\n按 中键 开始连点"),
    ("pause", "鼠标连点 已暂停\n再按 中键 继续"),
    ("stop",  "鼠标连点 已停止\n回到未就绪，需重新点「就绪」"),
]

for kind, text in CASES:
    c = render(kind, text, sentinel=SENTINEL)
    check("%s: 绘制无异常" % kind, not c.error, c.error)

    # (a) 整块卡片必须被画满 —— 直接钉死 GradientFill 零宽度那个坑
    left = sum(1 for y in range(c.h) for x in range(c.w)
               if c.px(x, y) == SENTINEL)
    check("%s: 底图铺满整卡（无未绘制像素）" % kind, left == 0,
          "残留 %d 个哨兵像素" % left)

    # (b) 底色是深色系（不能是空白/白/亮色）
    body = c.px(6, 60)
    check("%s: 卡片底色为深色（R<70 且 B>R）" % kind,
          body[0] < 70 and body[2] > body[0], "body=%s" % (body,))

    # (c) 垂直渐变确实存在（顶部比底部亮）
    top, bot = c.px(200, 2), c.px(200, 74)
    check("%s: 存在垂直渐变（顶比底亮）" % kind, top[2] > bot[2],
          "top=%s bottom=%s" % (top, bot))

    # (d) 状态圆点用状态色（不是默认白刷/黑边）
    st = toast.TOAST_STATE[kind]
    dot = c.px(24, 26)
    check("%s: 状态圆点颜色 = %s" % (kind, hexof(st)),
          near(dot, st, 40), "dot=%s expect=%s" % (dot, hexof(st)))

    # (e) 标题行画了浅色文字（tx-hi 系）
    found = any(c.px(x, y)[0] > 150 and c.px(x, y)[2] > 150
                for y in range(12, 30) for x in range(40, 330))
    check("%s: 标题行有浅色文字" % kind, found)

    # (f) 说明行画了中间色文字（tx-mid 系，比标题暗）
    found2 = any(110 < c.px(x, y)[0] < 210 and 130 < c.px(x, y)[2] < 230
                 for y in range(42, 56) for x in range(40, 330))
    check("%s: 说明行有次级文字" % kind, found2)

    # (g) 顶边高光线存在（比卡片底色亮一档）
    topline = c.px(200, 0)
    check("%s: 顶部高光线存在" % kind, topline[2] > bot[2] + 30,
          "line=%s bottom=%s" % (topline, bot))

    c.close()

# ----------------------------------------------------------------------
print("\n[3] 切角形状（12px 对角切，主界面同款）")
# ----------------------------------------------------------------------
rgn = toast.make_chamfer_region()
check("能创建切角 region", bool(rgn))


def inside(r, x, y):
    import ctypes
    import ctypes.wintypes as wt
    ctypes.windll.gdi32.PtInRegion.argtypes = [wt.HRGN, ctypes.c_int,
                                               ctypes.c_int]
    ctypes.windll.gdi32.PtInRegion.restype = wt.BOOL
    return bool(ctypes.windll.gdi32.PtInRegion(r, x, y))


if rgn:
    check("左上切角内 (3,3) 不在卡片内", not inside(rgn, 3, 3))
    check("左上切角边 (11,1) 在卡片内", inside(rgn, 11, 1))
    check("右下切角内 (359,75) 不在卡片内", not inside(rgn, 359, 75))
    check("右下切角边 (349,76) 在卡片内", inside(rgn, 349, 76))
    check("卡片中心 (180,39) 在卡片内", inside(rgn, 180, 39))

pts = toast.chamfer_points()
got = [(p.x, p.y) for p in pts]
want = [(12, 0), (360, 0), (360, 66), (348, 78), (0, 78), (0, 12)]
check("切角顶点与主界面 polygon 同序同构", got == want, "%s" % (got,))

# ----------------------------------------------------------------------
print("\n[4] 静态断言（防退化）")
# ----------------------------------------------------------------------
src = open(os.path.join(HERE, "core", "toast.py"), encoding="utf-8").read()

check("窗口裁剪用 CreatePolygonRgn（不是圆角）",
      "CreatePolygonRgn" in src)
check("没有退化成圆角 createRoundRectRgn 调用",
      "CreateRoundRectRgn(" not in src)
check("旧版纯色卡片常量 TOAST_COLORS 已清除",
      "TOAST_COLORS" not in src)
check("绘制逻辑抽成可离屏渲染的 _paint_card",
      "def _paint_card" in src and "_paint_card(hdc)" in src)
check("渐变顶点给足宽度（防零宽矩形再次发生）",
      "v[1].x, v[1].y = TOAST_W, TOAST_H" in src)
check("圆点刷子选进 DC（防默认白刷）",
      "ob = _gdi32.SelectObject(hdc, br)" in src)
check("渐变前先铺纯色底（双保险）",
      "先铺纯色兜底" in src)

print("\n" + "=" * 62)
print("toast 风格回归：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   失败: %s" % f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
