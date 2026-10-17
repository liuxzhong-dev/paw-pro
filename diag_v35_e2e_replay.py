# -*- coding: utf-8 -*-
"""
diag_v35_e2e_replay.py —— v34 复核：宏回放端到端字节级对账
===========================================================

【为什么要有这个探针】
主人真机报的 bug 是「回放跑到左上角去点」。v34 修的是
core/inputs.py 里 MOUSEINPUT(x=...) 假字段（真名 dx/dy），diag_v34_probe
只验证了 move_to **单点**。这个探针补上真正的用户场景：

    真实宏文件 → steps → to_events → MacroReplayPlugin 完整回放
    → 假 SendInput 从内存抄下每一条 INPUT 的 dx/dy/dwFlags → 逐条对账

抄的是**结构体数组内存字节**（ctypes cast），不是 Python 层的调用参数——
就算中间哪层再犯「字段名写错」的错，字节也不会说谎。

跑法：.venv/Scripts/python.exe diag_v35_e2e_replay.py
"""
import ctypes
import io
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from core import inputs as I            # noqa: E402
from core.engine import Registry        # noqa: E402

_PASS = 0
_FAIL = 0
_FAILS = []


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK]   %s %s" % (name, ("-> " + detail) if detail else ""))
    else:
        _FAIL += 1
        _FAILS.append(name)
        print("  [FAIL] %s %s" % (name, ("-> " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 1. 假 SendInput：从内存抄字节
# ---------------------------------------------------------------------------
class FakeSendInput:
    """替换 core.inputs._user32.SendInput，抄下每条 INPUT 的真实字节。

    SendInput(count, pInputs, cbSize) 的 pInputs 是 INPUT 数组首地址。
    直接 cast 回 (INPUT * count) 读 mi.dx / mi.dy / mi.dwFlags——
    这些值就是系统「本该收到」的坐标。
    """

    def __init__(self):
        self.calls = []          # [(kind, dx, dy, flags), ...]
        self.mouse = 0
        self.keyboard = 0

    def __call__(self, count, p_inputs, cb_size):
        arr = ctypes.cast(p_inputs,
                          ctypes.POINTER(I.INPUT * count)).contents
        for inp in arr:
            if inp.type == I.INPUT_MOUSE:
                self.calls.append(("m", inp.mi.dx, inp.mi.dy,
                                   inp.mi.dwFlags))
                self.mouse += 1
            else:
                self.calls.append(("k", inp.ki.wVk, inp.ki.dwFlags, 0))
                self.keyboard += 1
        return count


def install_fake():
    """同时拦鼠标（core.inputs）和键盘（core.keyboard）两条注入通道。"""
    fake = FakeSendInput()
    saved = []
    import core.keyboard as K
    for mod in (I, K):
        old = mod._user32.SendInput
        mod._user32.SendInput = fake
        saved.append((mod, old))
    return fake, saved


def restore(saved):
    for mod, old in saved:
        mod._user32.SendInput = old


# ---------------------------------------------------------------------------
# 2. 造一个最小 ctx（暂停/停止逻辑 v27/v34 探针已覆盖，这里只管坐标）
# ---------------------------------------------------------------------------
class FakeCtx:
    def __init__(self, options):
        self.options = options
        self.state = {}
        self.error = None
        self.clicked = 0
        self.tick_count = 0

    def should_stop(self):
        return False

    @staticmethod
    def wait(seconds):
        time.sleep(seconds)
        return True


MACRO = os.path.join(_HERE, "dist_v33", "macros", "001.json")


def load_events():
    data = json.load(io.open(MACRO, encoding="utf-8"))
    steps = data.get("steps") or []
    if steps:
        from core import macro_steps as ms
        return ms.to_events(steps)
    return data.get("events") or []


def main():
    print("=" * 62)
    print("v34 复核：宏回放端到端字节级对账（素材：dist_v33/macros/001.json）")
    print("=" * 62)

    check("真实宏文件存在（主人真机录的那份，只读）", os.path.isfile(MACRO),
          MACRO)
    events = load_events()
    check("宏能展开成事件表", len(events) > 0, "共 %d 条" % len(events))
    has_move = any(e.get("a") == "move" for e in events)
    has_down = any(e.get("a") == "down" and "x" in e for e in events)
    check("宏里有移动类事件（左上角 bug 的暴露面）", has_move and has_down)

    fake, saved = install_fake()
    try:
        import plugins.macro_replay  # noqa: F401  导入即注册
        plugin = Registry.get("macro_replay")
        check("回放插件已注册", plugin is not None)
        ctx = FakeCtx({"events": [dict(e) for e in events],
                       "speed": 8.0,       # 8 倍速跑，回归更快
                       "repeat": 1})
        ok = plugin.on_start(ctx)
        check("on_start 校验通过", ok, ctx.error or "")
        t0 = time.time()
        plugin.run(ctx)
        cost = time.time() - t0
        check("完整回放跑完（无异常）", True,
              "%.2f 秒，注入鼠标 %d 条 / 键盘 %d 条"
              % (cost, fake.mouse, fake.keyboard))
    finally:
        restore(saved)

    # ---- 逐条对账：每条 move 注入的 (dx,dy) 必须等于 _to_absolute(期望) ----
    # 期望表：按事件顺序，move/down 各产生一条带坐标的注入。
    expect = []
    for e in events:
        a = e.get("a")
        if a == "move" or a == "down":
            expect.append((a, I._to_absolute(int(e["x"]), int(e["y"]))))
    got = [(f[0], (f[1], f[2])) for f in fake.calls
           if f[0] == "m" and (f[3] & I.MOUSEEVENTF_MOVE)]
    check("带坐标的注入条数 == move+down 条数",
          len(got) == len(expect),
          "注入 %d / 期望 %d" % (len(got), len(expect)))

    mismatch = []
    for i, ((ea, (ex, ey)), (ga, (gx, gy))) in enumerate(
            zip(expect, got)):
        if (ex, ey) != (gx, gy):
            mismatch.append((i, ea, (ex, ey), (gx, gy)))
    check("每一条坐标逐字节一致（0 偏差）", not mismatch,
          ("首例：#%d %s 期望%s 实到%s" % mismatch[0]) if mismatch else "")

    # ---- 左上角特征：不允许出现「坐标注入 dx=dy=0」----
    zero = [f for f in fake.calls
            if f[0] == "m" and f[1] == 0 and f[2] == 0
            and (f[3] & I.MOUSEEVENTF_MOVE)]
    check("没有任何 dx=dy=0 的移动注入（左上角特征归零）", not zero,
          "%d 条" % len(zero))

    # ---- down 前必有 move：点哪儿之前先到哪儿 ----
    seq = [(f[0], f[1], f[2], f[3]) for f in fake.calls if f[0] == "m"]
    bad_down = []
    for i, f in enumerate(seq):
        if f[3] & (I.MOUSEEVENTF_LEFTDOWN | I.MOUSEEVENTF_RIGHTDOWN
                   | I.MOUSEEVENTF_MIDDLEDOWN):
            if i == 0 or not (seq[i - 1][3] & I.MOUSEEVENTF_MOVE):
                bad_down.append(i)
    check("每次按下前一条都是 MOVE（先到位再点）", not bad_down,
          "%d 处悬空按下" % len(bad_down))

    # ---- 负向对照：老写法（x=/y=）在本探针下必红 ----
    bad = I.MOUSEINPUT(x=60173, y=61769)
    check("负向对照：老写法 x=/y= 构造出的 dx/dy 仍是 0"
          "（证明上面的断言真能抓住原 bug）",
          bad.dx == 0 and bad.dy == 0,
          "dx=%s dy=%s" % (bad.dx, bad.dy))

    print()
    print("=" * 62)
    print("端到端对账：通过 %d / 失败 %d" % (_PASS, _FAIL))
    if _FAILS:
        print("失败项：")
        for n in _FAILS:
            print("  - " + n)
    print("=" * 62)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
