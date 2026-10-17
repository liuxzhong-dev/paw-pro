# -*- coding: utf-8 -*-
"""
plugins/macro_replay.py —— 插件：宏回放（v27 / S2）

把 core/recorder.py 录下来的事件表按原时序重放一遍。

为什么做成插件而不是在 dashboard 里写个线程：
  插件天然拿到 ctx.wait()，而 ctx.wait 里已经接好了全局暂停、分区暂停、
  停止信号三件事（见 core/engine.py 的说明）。自己在别处 sleep 的话，
  暂停键按下去宏还在跑，用户会以为程序卡了。
  另外引擎的次数上限/时长上限/热键控制也一并复用。

两条容易忽略的规矩：

1. **时间轴用「有效时长」而不是墙钟。**
   中途暂停 10 秒，恢复后不该一口气快进 10 秒的动作——那正是暂停最
   不想看到的效果。所以这里自己攒一个 clock，只把 ctx.wait 真正睡掉的
   时间算进去：暂停期间 wait 阻塞、clock 不涨，恢复后从原处接着走。

2. **结束/中断时必须把还按着的东西释放掉。**
   录到「按下左键拖到一半」就停止录制的话，事件表里只有 down 没有 up。
   回放跑到一半用户按了停止，鼠标就永远处于按下状态——松手都不管用，
   得重启资源管理器。所以 finally 里统一抬起，和 core/keyboard.py 的
   panic_release 是同一个道理。
"""

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.engine import Plugin, Registry

# 单步最长等待上限（秒）。事件表里如果有 30 分钟的「等刷新」，
# 整段睡眠会卡住；更长的时间用分片循环走，停止响应才及时。
_MAX_STEP = 0.5
# 分片粒度：越小停止越灵敏，越大 CPU 越省。20ms 是人眼无感的量级。
_SLICE = 0.02


class MacroReplayPlugin(Plugin):
    name = "macro_replay"
    title = "宏回放"
    description = "按原时序重放录制好的键鼠操作"

    FIELD = [
        {
            "key": "speed",
            "label": "倍速",
            "type": "float",
            "default": 1.0,
            "hint": "1.0 = 原速；2.0 = 两倍速；0.5 = 放慢一半",
        },
        {
            "key": "repeat",
            "label": "重复次数",
            "type": "int",
            "default": 1,
            "min": 1,
            "max": 9999,
        },
    ]

    def on_start(self, ctx):
        events = ctx.options.get("events") or []
        if not events:
            ctx.error = "宏里没有动作，请先录制"
            return False
        # 录制器已经校验过一遍，但事件也可能是从文件里读回来的
        from core.recorder import validate_events
        clean, err = validate_events(events)
        if err:
            ctx.error = err
            return False
        ctx.state["events"] = clean
        ctx.state["speed"] = self._speed(ctx.options.get("speed", 1.0))
        ctx.state["repeat"] = max(1, int(ctx.options.get("repeat", 1) or 1))
        return True

    @staticmethod
    def _speed(raw):
        """倍速兜底：0 或负数会让时间轴变成负数（无限循环），必须夹住。"""
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return 1.0
        return min(8.0, max(0.1, v))

    def run(self, ctx):
        events = ctx.state.get("events") or []
        speed = ctx.state.get("speed", 1.0)
        repeat = ctx.state.get("repeat", 1)

        try:
            for _ in range(repeat):
                if ctx.should_stop():
                    break
                if not self._play_once(ctx, events, speed):
                    break            # 被打断（停止），不是错误
                ctx.tick_count += 1
        finally:
            # 无论怎么退出，先把「还按着的东西」松开
            self._release_all(ctx, events)
        return ctx.clicked

    # -- 回放本体 -----------------------------------------------------
    def _play_once(self, ctx, events, speed):
        """跑一遍事件表。返回 False 表示被停止打断了。"""
        # clock 用单元素列表：Python 的 float 是按值传的，
        # 想在 _wait_until 里就地推进时间轴，只能塞进可变容器。
        clock = [0.0]
        t0 = int(events[0].get("t", 0)) if events else 0

        for ev in events:
            if ctx.should_stop():
                return False
            # 目标时刻：源时间轴相对第一条的偏移 / 倍速
            target = (int(ev.get("t", 0)) - t0) / 1000.0 / speed
            if not self._wait_until(ctx, clock, target):
                return False
            clock[0] = max(clock[0], target)
            self._apply(ctx, ev)
        return True

    def _wait_until(self, ctx, clock, target):
        """
        等到时间轴推进到 target 秒。clock 是单元素列表 [秒]，就地推进。

        【为什么要切 20ms 片，而不是一次 ctx.wait(remain) 睡完】
        停止信号是能打断整段睡眠的（ClickContext.wait 内部用的是
        Event.wait(seconds)，stop_event 一 set 就立刻返回 False）。
        **切片的理由是暂停**：暂停检查在 ctx.wait 的**开头**
        （`while pausectl.is_paused(...)` 循环），睡下去之后就不再看了。
        所以单个长间隔（比如录到「等 5 秒刷新」）一次性睡的话，
        用户中途按 Pause，要等这 5 秒睡完才停住——感觉像没反应。
        切片把暂停响应压到 20ms 以内，人也感知不到。

        暂停期间 clock 不涨：ctx.wait 在暂停时会一直阻塞，返回时只算
        睡掉了 step。这样恢复后从原处接着走，不会一口气快进。
        """
        while True:
            remain = target - clock[0]
            if remain <= 0:
                return True
            step = min(_SLICE, remain)
            if not ctx.wait(step):
                return False          # 只有「已停止」会让 wait 返回 False
            clock[0] += step

    def _apply(self, ctx, ev):
        """把一条事件变成真实的注入。"""
        from core import inputs, keyboard

        a = ev.get("a")
        if a == "move":
            inputs.move_to(int(ev["x"]), int(ev["y"]))
        elif a == "down":
            inputs.move_to(int(ev["x"]), int(ev["y"]))
            self._button_down(ev.get("b", "left"))
            if ev.get("b") == "left":
                ctx.clicked += 1
        elif a == "up":
            # 抬起不搬鼠标：搬到 down 的坐标再抬，会在屏幕上画一道折线
            self._button_up(ev.get("b", "left"))
        elif a in ("wheel", "hwheel"):
            d = int(ev.get("d", 0))
            if a == "wheel":
                inputs.scroll(d)
            else:
                inputs.scroll_horizontal(d)
        elif a == "kd":
            keyboard.send_key(int(ev["vk"]), up=False, mark_injected=True)
        elif a == "ku":
            keyboard.send_key(int(ev["vk"]), up=True, mark_injected=True)

    # -- 鼠标键分流 ---------------------------------------------------
    @staticmethod
    def _button_down(b):
        from core import inputs
        if b in ("x1", "x2"):
            inputs.press_down_xbutton(int(b[1:]))
        else:
            inputs.press_down(b)

    @staticmethod
    def _button_up(b):
        from core import inputs
        if b in ("x1", "x2"):
            inputs.release_xbutton(int(b[1:]))
        else:
            inputs.release(b)

    def _release_all(self, ctx, events):
        """
        收尾释放。

        不用管「哪些真的按着」——按事件表里出现过的键全部补一次抬起，
        成本是几条无效 SendInput，换来的是绝不会把用户的鼠标/键盘留在
        按下状态。多按一次空抬起没有副作用（系统里本来就是松的）。
        """
        buttons = set()
        keys = set()
        for ev in events or []:
            if ev.get("a") == "down":
                buttons.add(ev.get("b"))
            elif ev.get("a") == "kd":
                try:
                    keys.add(int(ev["vk"]))
                except (KeyError, TypeError, ValueError):
                    pass
        for b in buttons:
            try:
                self._button_up(b or "left")
            except Exception:
                pass
        for vk in keys:
            try:
                from core import keyboard
                keyboard.send_key(vk, up=True, mark_injected=True)
            except Exception:
                pass


Registry.register(MacroReplayPlugin())
