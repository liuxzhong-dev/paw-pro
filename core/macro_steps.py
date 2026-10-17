# -*- coding: utf-8 -*-
"""
core/macro_steps.py —— 宏「步骤表」模型（v30）

为什么不再直接编辑原始事件表
------------------------------
录制产出的是**原始事件流**：鼠标每动一下就是一条，录 10 秒能出几百条
`move`。这种东西给用户看是没法编辑的——没人能在 800 行里找到想改的那一次
点击，而且改错一行就是「在错误坐标上连点」。

成熟宏工具（Pulover's Macro Creator、MacFlow、Macro-Recorder）走的都是
**步骤列表**：一次点击 = 一个节点，节点上挂着坐标和「延时」，用户只跟节点
打交道。本模块就是那层的纯逻辑，界面和回放都吃它：

    原始事件 events  --from_events()-->  步骤 steps  --to_events()-->  原始事件

两条容易搞错的地方
------------------
1. **延时归属**：节点上的 `delay` 表示「这一步做完，等多少毫秒再做下一步」，
   不是「这一步之前等多久」。界面上写「延时」就是前者（和参考截图一致）。
   最后一步的 delay 也保留——它决定「回放结束后停多久才算一步」，回放器
   会照它等完，用户中途停止可以打断。

2. **点击是「按下 + 抬起」两条事件**，压缩时必须成对合并。合并有窗口：
   超过 `merge_ms` 才抬起的，说明用户是**按住拖拽**，那不是一次点击，
   必须拆成 down / up 两个节点保留下来——否则画图、拖拽类操作会静默丢动作。

所有函数都是纯函数（不碰 Win32、不碰全局状态），探针可以直接喂数据。
"""

import random

# 步骤类型
KINDS = ("click", "down", "up", "key", "wheel", "hwheel", "move", "wait")

# 鼠标键名（与 core/recorder.py 的事件表一致）
BUTTONS = ("left", "right", "middle", "x1", "x2")

BUTTON_CN = {"left": "左键", "right": "右键", "middle": "中键",
             "x1": "G1", "x2": "G2"}

KIND_CN = {"click": "点击", "down": "按下", "up": "抬起", "key": "按键",
           "wheel": "滚轮", "hwheel": "横向滚轮", "move": "移动",
           "wait": "等待"}

# 按下到抬起在这个窗口内 = 一次点击；超过 = 按住（拖拽）
MERGE_MS = 180

# 延时上限：10 分钟。再长就该用「等待」节点而不是藏在延时里。
MAX_DELAY = 600000

# 单步注入的按下-抬起时长（毫秒）。太短某些游戏识别不到，太长会漏点。
CLICK_HOLD_MS = 15


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def default_name(kind, b=None, vk=None, d=0):
    if kind == "click":
        return "点击"
    if kind == "down":
        return "按住 %s" % BUTTON_CN.get(b, b)
    if kind == "up":
        return "松开 %s" % BUTTON_CN.get(b, b)
    if kind == "key":
        return "按键 %s" % (vk_name(vk) if vk else "")
    if kind == "wheel":
        return "滚轮 %+d" % int(d or 0)
    if kind == "hwheel":
        return "横向滚轮 %+d" % int(d or 0)
    if kind == "move":
        return "移动"
    if kind == "wait":
        return "等待"
    return "动作"


def vk_name(vk):
    """
    虚拟键码 → 人能看懂的名字。

    只覆盖常用键：字母数字直接转字符，其余用一张小表。
    认不出来就写 VK 十进制——比显示空白强，至少能对着键码表查。
    """
    try:
        vk = int(vk)
    except (TypeError, ValueError):
        return "?"
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    table = {
        0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift",
        0x11: "Ctrl", 0x12: "Alt", 0x13: "Pause", 0x14: "CapsLock",
        0x1B: "Esc", 0x20: "空格", 0x21: "PageUp", 0x22: "PageDown",
        0x23: "End", 0x24: "Home", 0x25: "←", 0x26: "↑", 0x27: "→",
        0x28: "↓", 0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete",
        0x5B: "Win", 0x5C: "Win",
    }
    if vk in table:
        return table[vk]
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    if 0x60 <= vk <= 0x69:
        return "小键盘%d" % (vk - 0x60)
    return "VK%d" % vk


# --------------------------------------------------------------------------
# 事件 → 步骤
# --------------------------------------------------------------------------
def _t(ev, default=0):
    try:
        return int(ev.get("t", default))
    except (TypeError, ValueError):
        return default


def from_events(events, merge_ms=MERGE_MS, keep_move=False):
    """
    把原始事件表压缩成步骤列表。

    返回的每个步骤：
        {"k": 类型, "name": 名称, "delay": 到下一步的毫秒,
         "delay2": 随机延时上限（0=固定）, "after": "next"/"end",
         以及类型相关字段 b/x/y/vk/d}

    `keep_move=False` 时丢掉移动事件：绝大多数自动化（点点点、签到、连招）
    只需要「到点再点」，回放时一步 move_to 就到，中间轨迹毫无意义，
    留着只会让步骤表从 20 条变 2000 条。
    需要还原鼠标轨迹（比如画图）时传 True。
    """
    evs = [e for e in (events or []) if isinstance(e, dict)]
    steps = []
    i = 0
    n = len(evs)

    def _add(step, start, end):
        step["_s"] = start
        step["_e"] = end
        steps.append(step)

    while i < n:
        e = evs[i]
        a = e.get("a")
        t = _t(e)

        if a == "move":
            if keep_move:
                _add({"k": "move", "x": int(e.get("x", 0)),
                      "y": int(e.get("y", 0)),
                      "name": default_name("move")}, t, t)
            i += 1
            continue

        if a in ("down", "up"):
            b = str(e.get("b", "left"))
            if b not in BUTTONS:
                b = "left"
            if a == "down":
                j, mate = i + 1, None
                while j < n and _t(evs[j]) - t <= merge_ms:
                    if evs[j].get("a") == "up" and \
                            str(evs[j].get("b", "")) == b:
                        mate = j
                        break
                    j += 1
                if mate is not None:
                    _add({"k": "click", "b": b,
                          "x": int(e.get("x", 0)), "y": int(e.get("y", 0)),
                          "name": default_name("click")},
                         t, _t(evs[mate]))
                    i = mate + 1
                    continue
                # 没有及时抬起 = 按住（拖拽起手）
                _add({"k": "down", "b": b,
                      "x": int(e.get("x", 0)), "y": int(e.get("y", 0)),
                      "name": default_name("down", b)}, t, t)
                i += 1
                continue
            _add({"k": "up", "b": b, "name": default_name("up", b)}, t, t)
            i += 1
            continue

        if a in ("kd", "ku"):
            try:
                vk = int(e.get("vk"))
            except (TypeError, ValueError):
                i += 1
                continue
            if a == "kd":
                j, mate = i + 1, None
                while j < n and _t(evs[j]) - t <= merge_ms:
                    if evs[j].get("a") == "ku" and \
                            int(evs[j].get("vk", 0)) == vk:
                        mate = j
                        break
                    j += 1
                if mate is not None:
                    _add({"k": "key", "vk": vk,
                          "name": default_name("key", vk=vk)},
                         t, _t(evs[mate]))
                    i = mate + 1
                    continue
                # 长按不放：保留成「按下 + 抬起」两个节点
                _add({"k": "down", "b": "key", "vk": vk,
                      "name": "按住 %s" % vk_name(vk)}, t, t)
                i += 1
                continue
            _add({"k": "up", "b": "key", "vk": vk,
                  "name": "松开 %s" % vk_name(vk)}, t, t)
            i += 1
            continue

        if a in ("wheel", "hwheel"):
            try:
                d = int(e.get("d", 0))
            except (TypeError, ValueError):
                d = 0
            if d:
                _add({"k": a, "d": d, "name": default_name(a, d=d)}, t, t)
            i += 1
            continue

        i += 1                        # 不认识的类型直接跳过

    # ---- 补延时：下一步开始 - 本步结束 ----
    for k in range(len(steps)):
        cur = steps[k]
        if k + 1 < len(steps):
            gap = steps[k + 1]["_s"] - cur["_e"]
        else:
            gap = 0
        cur["delay"] = _clamp(gap, 0, MAX_DELAY)
        cur["delay2"] = 0
        cur["after"] = "next"
        cur.pop("_s", None)
        cur.pop("_e", None)
    return steps


# --------------------------------------------------------------------------
# 步骤 → 事件
# --------------------------------------------------------------------------
def effective_delay(step, rnd=None):
    """
    这一步的真实延时（毫秒）。随机延时取 [delay, delay2] 闭区间。

    rnd 可以传 random.Random(种子) 让探针结果可复现。
    """
    try:
        lo = int(step.get("delay", 0) or 0)
    except (TypeError, ValueError):
        lo = 0
    lo = _clamp(lo, 0, MAX_DELAY)
    try:
        hi = int(step.get("delay2", 0) or 0)
    except (TypeError, ValueError):
        hi = 0
    if hi <= lo:
        return lo
    hi = _clamp(hi, lo, MAX_DELAY)
    return (rnd or random).randint(lo, hi)


def to_events(steps, rnd=None):
    """
    步骤表 → 回放器吃的原始事件表。

    【为什么要重新展开而不是录制时留一份】
    用户编辑的是步骤（改坐标、改延时、删节点）。回放必须吃**编辑后**的
    数据，否则「改了没生效」会是最难查的那类 bug。展开成本可以忽略：
    一万步也就几万条 dict。
    """
    out = []
    clock = 0

    def emit(ev):
        ev["t"] = clock
        out.append(ev)

    for s in steps or []:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        d = effective_delay(s, rnd)

        if k == "click":
            x, y = int(s.get("x", 0)), int(s.get("y", 0))
            b = s.get("b", "left")
            emit({"a": "move", "x": x, "y": y})
            emit({"a": "down", "b": b, "x": x, "y": y})
            clock += CLICK_HOLD_MS
            emit({"a": "up", "b": b})
        elif k == "down":
            if s.get("b") == "key":
                emit({"a": "kd", "vk": int(s.get("vk", 0))})
            else:
                x, y = int(s.get("x", 0)), int(s.get("y", 0))
                emit({"a": "move", "x": x, "y": y})
                emit({"a": "down", "b": s.get("b", "left"), "x": x, "y": y})
        elif k == "up":
            if s.get("b") == "key":
                emit({"a": "ku", "vk": int(s.get("vk", 0))})
            else:
                emit({"a": "up", "b": s.get("b", "left")})
        elif k == "key":
            vk = int(s.get("vk", 0))
            emit({"a": "kd", "vk": vk})
            clock += CLICK_HOLD_MS
            emit({"a": "ku", "vk": vk})
        elif k in ("wheel", "hwheel"):
            emit({"a": k, "d": int(s.get("d", 0))})
        elif k == "move":
            emit({"a": "move", "x": int(s.get("x", 0)),
                  "y": int(s.get("y", 0))})
        elif k == "wait":
            pass                       # 等待只是延时，本身不产生事件
        clock += d
        if s.get("after") == "end":
            break

    return out


def total_ms(steps):
    """
    整段宏的时长（毫秒）= 各步延时 + 点击/按键的按下时长。

    随机延时按**下限**算：这是用户能保证的最短时长，用它做「这段宏多长」
    的说法不会吹牛。
    """
    total = 0
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        try:
            d = int(s.get("delay", 0) or 0)
        except (TypeError, ValueError):
            d = 0
        total += _clamp(d, 0, MAX_DELAY)
        if s.get("k") in ("click", "key"):
            total += CLICK_HOLD_MS
    return total


def stats(steps):
    """给界面用的摘要：步数 / 时长 / 点击次数 / 按键次数。"""
    steps = steps or []
    clicks = sum(1 for s in steps if isinstance(s, dict) and
                 s.get("k") == "click")
    keys = sum(1 for s in steps if isinstance(s, dict) and
               s.get("k") == "key")
    return {"count": len(steps), "secs": round(total_ms(steps) / 1000.0, 1),
            "clicks": clicks, "keys": keys}


def label(s):
    """
    一行给界面看的描述。和界面的列一一对应：
        点击 · 左键 (789, 2444) · 延时 1427ms
    """
    k = s.get("k")
    bits = [s.get("name") or default_name(k)]
    if k in ("click", "down", "up") and s.get("b") not in ("key", None):
        bits.append(BUTTON_CN.get(s.get("b"), ""))
    if k in ("click", "down", "move"):
        bits.append("(%d, %d)" % (int(s.get("x", 0)), int(s.get("y", 0))))
    return " · ".join([b for b in bits if b])


# --------------------------------------------------------------------------
# 校验与编辑
# --------------------------------------------------------------------------
def validate_steps(steps, max_steps=20000):
    """
    清洗外部/手改过的步骤表。返回 (干净步骤, 错误信息)。

    宁可整条拒绝也不要把脏数据喂给回放——在错误坐标上乱点比打不开危险。
    """
    if not isinstance(steps, list):
        return [], "步骤格式不对（steps 必须是列表）"
    if len(steps) > max_steps:
        return [], "步骤太多（%d 条，上限 %d）" % (len(steps), max_steps)
    out = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return [], "第 %d 步不是对象" % (i + 1)
        k = s.get("k")
        if k not in KINDS:
            return [], "第 %d 步类型不认识：%r" % (i + 1, k)
        item = {"k": k}
        name = str(s.get("name") or default_name(k))
        item["name"] = name[:32]
        if k in ("click", "down", "up", "move") and s.get("b") != "key":
            try:
                item["x"] = _clamp(int(s.get("x", 0)), -32768, 32767)
                item["y"] = _clamp(int(s.get("y", 0)), -32768, 32767)
            except (TypeError, ValueError):
                return [], "第 %d 步坐标不是整数" % (i + 1)
        if k in ("click", "down", "up"):
            b = str(s.get("b", "left"))
            if b != "key" and b not in BUTTONS:
                return [], "第 %d 步鼠标键名不认识：%r" % (i + 1, b)
            item["b"] = b
            if b == "key":
                try:
                    vk = int(s.get("vk", 0))
                except (TypeError, ValueError):
                    return [], "第 %d 步缺少键码" % (i + 1)
                if not (0 < vk < 256):
                    return [], "第 %d 步键码越界：%d" % (i + 1, vk)
                item["vk"] = vk
        if k == "key":
            try:
                vk = int(s.get("vk", 0))
            except (TypeError, ValueError):
                return [], "第 %d 步缺少键码" % (i + 1)
            if not (0 < vk < 256):
                return [], "第 %d 步键码越界：%d" % (i + 1, vk)
            item["vk"] = vk
        if k in ("wheel", "hwheel"):
            try:
                item["d"] = _clamp(int(s.get("d", 0)), -100, 100)
            except (TypeError, ValueError):
                return [], "第 %d 步滚轮格数不是整数" % (i + 1)
            if item["d"] == 0:
                return [], "第 %d 步滚轮格数是 0" % (i + 1)
        try:
            delay = int(s.get("delay", 0) or 0)
            delay2 = int(s.get("delay2", 0) or 0)
        except (TypeError, ValueError):
            return [], "第 %d 步延时不是整数" % (i + 1)
        item["delay"] = _clamp(delay, 0, MAX_DELAY)
        item["delay2"] = _clamp(max(delay2, 0), 0, MAX_DELAY)
        if item["delay2"] and item["delay2"] < item["delay"]:
            item["delay2"] = item["delay"]
        item["after"] = "end" if s.get("after") == "end" else "next"
        out.append(item)
    return out, ""


def new_step(kind="wait", delay=500, x=0, y=0):
    """造一个新步骤（界面「插入等待」用）。"""
    if kind not in KINDS:
        kind = "wait"
    s = {"k": kind, "name": default_name(kind), "delay": _clamp(delay, 0,
                                                               MAX_DELAY),
         "delay2": 0, "after": "next"}
    if kind in ("click", "down"):
        s.update(x=int(x), y=int(y), b="left")
    elif kind == "move":
        s.update(x=int(x), y=int(y))
    return s
