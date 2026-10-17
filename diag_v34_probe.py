# -*- coding: utf-8 -*-
"""
diag_v34_probe.py —— v34 两项修复的专项探针
=============================================

① **回放跑到左上角**（主人 2026-10-18 实测）：
   `MOUSEINPUT(x=..., y=...)` 字段名写错（真名 dx / dy），而 ctypes 的
   Structure 对**不存在的字段关键字不报错** —— 构造「成功」、SendInput
   也照样返回成功，但坐标永远是 0，于是每一步都把鼠标送到 (0,0)。
   点击类事件不带坐标看不出来，只有移动类事件暴露，所以它能一路活到用户手上。

② **宏区没有启停键**（主人反馈）：宏归自己的暂停分区，F7 启停。

【为什么要用假 SendInput 而不是真的移动鼠标】
本会话进程注入不生效（`move_to` 返回 True 但光标纹丝不动），
真机验证不了。但**我们送出去的字节对不对**是可以验的：
把 `_user32` 换成假对象，SendInput 被调用时把 INPUT 结构原样抄下来，
直接断言 dx/dy —— 这比「看光标有没有动」更硬，且不依赖系统权限。
"""

import ctypes
import ctypes.wintypes as wt
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

PASS = 0
FAIL = 0
SKIP = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [!!] %s %s" % (name, detail))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("  [--] %s（SKIP：%s）" % (name, why))


def read(path):
    return io.open(path, encoding="utf-8").read()


def strip_docstrings(src):
    """
    按三引号分段保留「代码段」：docstring 是偶数块（0、2、4…）。

    必须剥：inputs.py 的注释里特意留了一行 `MOUSEINPUT(x=100, y=200)`
    当**反例教材**，不剥的话静态断言永远红，且会掩盖真正的回归。
    """
    parts = src.split('"""')
    return "\n".join(parts[i] for i in range(0, len(parts), 2))


def strip_comments(src):
    """剥掉 # 注释，避免「注释里的反例」被静态断言当成真代码（规则 38）。"""
    out = []
    for line in src.splitlines():
        s = line.split("#", 1)[0]
        out.append(s)
    return "\n".join(out)


def code_only(path):
    """真正参与执行的代码：剥 docstring 再剥 # 注释。"""
    return strip_comments(strip_docstrings(read(path)))


# ======================================================================
print("=" * 66)
print("① 回放坐标：送出去的字节里到底有没有坐标")
print("=" * 66)

from core import inputs as inp


class FakeUser32(object):
    """假 user32：抄下 SendInput 收到的每一个 INPUT，不做任何真实注入。"""

    def __init__(self):
        self.sent = []
        self.metrics = {0: 1920, 1: 1080, 76: 0, 77: 0, 78: 1920, 79: 1080}

    def SendInput(self, count, ptr, size):
        arr = ctypes.cast(ptr, ctypes.POINTER(inp.INPUT))
        for i in range(count):
            ev = arr[i]
            self.sent.append({"type": ev.type, "dx": ev.mi.dx, "dy": ev.mi.dy,
                              "data": ev.mi.mouseData, "flags": ev.mi.dwFlags,
                              "extra": ev.mi.dwExtraInfo, "size": size})
        return count

    def GetSystemMetrics(self, idx):
        return self.metrics.get(idx, 0)


real_user32 = inp._user32
fake = FakeUser32()
inp._user32 = fake
try:
    ok = inp.move_to(1762, 1017)
    check("move_to 返回成功（假 SendInput 收到 1 条）", ok and len(fake.sent) == 1,
          "实收 %d 条" % len(fake.sent))
    if fake.sent:
        ev = fake.sent[-1]
        # 期望：1762*65535/1919 = 60173，1017*65535/1079 = 61769
        check("绝对移动的 dx 是真实坐标（不是 0）",
              ev["dx"] == 60173, "dx=%s（期望 60173）" % ev["dx"])
        check("绝对移动的 dy 是真实坐标（不是 0）",
              ev["dy"] == 61769, "dy=%s（期望 61769）" % ev["dy"])
        check("flags 带 MOVE|ABSOLUTE|VIRTUALDESK",
              ev["flags"] == (inp.MOUSEEVENTF_MOVE | inp.MOUSEEVENTF_ABSOLUTE
                              | inp.MOUSEEVENTF_VIRTUALDESK),
              "flags=0x%x" % ev["flags"])
        check("dwExtraInfo 带注入标记（不然回放会被自己触发键打断）",
              ev["extra"] == inp.MARK_INJECTED, "extra=0x%x" % ev["extra"])

    # 回放插件真实走一遍：4 个 click 步骤，看每一步送出的坐标
    from core import macro_steps as ms
    steps = [{"k": "click", "x": 1762, "y": 1017, "b": "left", "delay": 100},
             {"k": "click", "x": 300, "y": 200, "b": "left", "delay": 100}]
    events = ms.to_events(steps)
    fake.sent = []
    from plugins.macro_replay import MacroReplayPlugin
    p = MacroReplayPlugin()
    for ev in events:
        p._apply(type("C", (), {"clicked": 0})(), ev)
    moved = [s for s in fake.sent if s["flags"] & inp.MOUSEEVENTF_MOVE]
    check("回放每一步都真的送出了坐标（不是全 0）",
          len(moved) == 4 and all(s["dx"] > 0 and s["dy"] > 0 for s in moved),
          "实收 %s" % [(s["dx"], s["dy"]) for s in moved])
    check("回放没有把任何一步送到 (0,0)（本次 bug 的现象）",
          not any(s["dx"] == 0 and s["dy"] == 0 for s in moved))
finally:
    inp._user32 = real_user32

# 负向对照：错误写法必须仍然产出 0，否则这条断言没有意义（规则 42）
bad = inp.MOUSEINPUT(x=60173, y=61769)
check("负向对照：错误写法 x=/y= 依然被静默忽略（dx=dy=0）",
      bad.dx == 0 and bad.dy == 0, "dx=%s dy=%s" % (bad.dx, bad.dy))

# 静态：源码里不许再出现这种写法（剥掉 docstring 与注释，只看真代码）
src = code_only(os.path.join(_HERE, "core", "inputs.py"))
check("inputs.py 真代码里没有 MOUSEINPUT(x= 这种写法",
      "MOUSEINPUT(x=" not in src and "MOUSEINPUT(y=" not in src)
# 6 处：_send 通用路径 / 侧键点击 / 侧键按下 / 侧键抬起 / 滚轮 / 横滚
check("所有 6 处构造点都写的是 dx=/dy=",
      src.count("MOUSEINPUT(dx=") == 6, "实到 %d 处" % src.count("MOUSEINPUT(dx="))

# ======================================================================
print("=" * 66)
print("② 宏区启停键")
print("=" * 66)

dash = read(os.path.join(_HERE, "dashboard.py"))
html = read(os.path.join(_HERE, "ui", "dashboard.html"))
flat = re.sub(r"\s+", "", dash)

check("宏有自己的热键候选（默认 F7）",
      'HOTKEY_MACRO_FALLBACK = ["F7"' in dash)
check("启动时按『已保存优先、默认候选兜底』注册宏启停键",
      '_saved_first("macro",HOTKEY_MACRO_FALLBACK),self.toggle_macro,"macro"'
      in flat)
check("toggle_macro 存在且在类里（不是裸函数）",
      "def toggle_macro(self)" in dash)
check("宏引擎跑在 macro 分区（zone=\"macro\"）",
      'zone="macro"' in flat)
check("停止回放会清掉本区暂停（否则下次按 F7 变成「先恢复一次」）",
      'pausectl.set_paused(False,"macro")' in flat)

from core import pausectl
pausectl.set_paused(False)
pausectl.set_paused(False, "macro")
pausectl.set_paused(True, "macro")
check("macro 分区暂停只影响 macro，不牵连鼠标区",
      pausectl.is_paused("macro") and not pausectl.is_paused("mouse"))
pausectl.set_paused(True)          # 全局
check("全局暂停仍然能冻住宏（判定是全局 OR 本区）",
      pausectl.is_paused("macro"))
pausectl.set_paused(False)

# 引擎的 wait 看的是自己的分区
from core.engine import ClickContext
ctx_m = ClickContext(options={}, zone="mouse")
ctx_c = ClickContext(options={}, zone="macro")
check("默认引擎仍归 mouse 区（老行为不变）", ctx_m.zone == "mouse")
check("宏引擎归 macro 区", ctx_c.zone == "macro")

# 前端
check("触发方式区有第 4 格「宏启停」", 'id="hkMacro"' in html)
check("宏区有启停键提示行", 'id="macTrigKey"' in html)
check("改键下拉里有「宏启停热键」", 'value="macro"' in html)
check("绑鼠标键下拉里有「启停宏回放」", "启停宏回放" in html)
check("回放状态会区分「回放中」和「已暂停」",
      "已暂停 · 再按 " in html.replace("\n", " ") or "已暂停 · 再按 " in html)

# 动作白名单：macro 必须被三处接受
check("set_system_hotkey 接受 macro",
      'if act not in ("mouse", "key", "macro")' in dash)
check("bind_captured_key 接受 macro",
      dash.count('not in ("mouse", "key", "macro")') >= 2,
      "实到 %d 处" % dash.count('not in ("mouse", "key", "macro")'))

# ======================================================================
print("=" * 66)
print("探针汇总：通过 %d / 失败 %d / 跳过 %d" % (PASS, FAIL, SKIP))
print("=" * 66)
sys.exit(1 if FAIL else 0)
