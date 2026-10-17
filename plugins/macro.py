# -*- coding: utf-8 -*-
"""
plugins/macro.py —— 插件：动作宏

把「点击、等待、移动、拖拽、滚轮」排成一条动作队列循环执行。
这是「可拓展」的最好示范：加新动作类型只需在 run() 的分派表里加一行。

动作语法（每步之间用 ; 分隔）：
    click          点击当前鼠标位置
    click@100,200  先移动到 100,200 再点击
    dclick         双击
    rclick         右键
    move@300,400   只移动
    drag@100,100->400,400   从 100,100 拖到 400,400
    wheel@3        向上滚 3 格（wheel@-2 向下）
    wait@500       等待 500 毫秒
"""

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.engine import Plugin, Registry


class MacroPlugin(Plugin):
    name = "macro"
    title = "动作宏"
    description = "把多步鼠标动作排成队列循环执行（点击/移动/拖拽/滚轮/等待）"

    FIELD = [
        {
            "key": "steps",
            "label": "动作队列",
            "type": "text",
            "default": "click ; wait@300 ; move@600,400 ; click",
            "hint": "用分号分隔。支持 click / dclick / rclick / move@x,y / drag@x,y->x,y / wheel@n / wait@ms",
        },
        {
            "key": "loop",
            "label": "循环执行",
            "type": "bool",
            "default": True,
            "hint": "关闭则只执行一遍动作队列",
        },
        {
            "key": "round_pause",
            "label": "每轮结束停顿(ms)",
            "type": "int",
            "default": 500,
            "min": 0,
            "max": 10000,
        },
    ]

    def on_start(self, ctx):
        steps = self._parse(ctx.options.get("steps", ""))
        if not steps:
            ctx.error = "动作队列为空，请先填写至少一步动作"
            return False
        ctx.state["queue"] = steps
        return True

    @staticmethod
    def _parse(raw):
        """把动作字符串解析成 [(动作名, 参数), ...] 列表。"""
        out = []
        for chunk in str(raw).split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "@" in chunk:
                name, arg = chunk.split("@", 1)
                out.append((name.strip().lower(), arg.strip()))
            else:
                out.append((chunk.lower(), ""))
        return out

    def run(self, ctx):
        from core import inputs

        queue = ctx.state.get("queue") or []
        loop = bool(ctx.options.get("loop", True))
        round_pause = max(0, int(ctx.options.get("round_pause", 500))) / 1000.0

        # 动作分派表：加新动作类型，往这里加一行即可
        def do_click(arg):
            if arg:
                inputs.move_to(*parse_xy(arg))
            inputs.click("left")
            ctx.clicked += 1

        def do_dclick(arg):
            if arg:
                inputs.move_to(*parse_xy(arg))
            inputs.click("left", count=2)
            ctx.clicked += 1

        def do_rclick(arg):
            if arg:
                inputs.move_to(*parse_xy(arg))
            inputs.click("right")
            ctx.clicked += 1

        def do_move(arg):
            if arg:
                inputs.move_to(*parse_xy(arg))

        def do_drag(arg):
            pts = [p for p in str(arg).split("->") if p.strip()]
            if len(pts) >= 2:
                inputs.drag(parse_xy(pts[0]), parse_xy(pts[1]))
                ctx.clicked += 1

        def do_wheel(arg):
            try:
                inputs.scroll(int(float(arg)))
            except (TypeError, ValueError):
                pass

        def do_wait(arg):
            try:
                ctx.wait(min(60.0, max(0.0, float(arg) / 1000.0)))
            except (TypeError, ValueError):
                pass

        dispatch = {
            "click": do_click,
            "dclick": do_dclick,
            "rclick": do_rclick,
            "move": do_move,
            "drag": do_drag,
            "wheel": do_wheel,
            "wait": do_wait,
        }

        while not ctx.should_stop():
            for name, arg in queue:
                if ctx.should_stop():
                    break
                fn = dispatch.get(name)
                if fn is None:
                    continue          # 未知动作跳过，不中断整个宏
                fn(arg)
                ctx.tick_count += 1
            if not loop:
                break
            if round_pause:
                ctx.wait(round_pause)
        return ctx.clicked


def parse_xy(text):
    """把 '100,200' 解析成 (100, 200)。"""
    x, y = str(text).split(",", 1)
    return (int(float(x)), int(float(y)))


Registry.register(MacroPlugin())