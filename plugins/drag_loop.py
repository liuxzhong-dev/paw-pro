# -*- coding: utf-8 -*-
"""
plugins/drag_loop.py —— 插件：循环拖拽

按记录的多个坐标点循环拖拽，用于反复画线、反复拉框这类操作。
坐标点存在配置里，可以持久化。
"""

import sys
import os

# 插件统一用绝对导入。根目录由启动器（app.py）塞进 sys.path，
# 这样源码运行和打包后 exe 旁边放 plugins/ 目录都能找到 core 包。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.engine import Plugin, Registry


class DragLoop(Plugin):
    name = "drag_loop"
    title = "循环拖拽"
    description = "在一组坐标点之间反复拖拽，用于重复画线 / 拉框"

    FIELD = [
        {
            "key": "points",
            "label": "坐标点序列",
            "type": "text",
            "default": "100,100 -> 400,300 -> 100,300",
            "hint": "格式：x,y -> x,y -> x,y，按顺序循环拖拽",
        },
        {
            "key": "steps",
            "label": "单段拖拽步数",
            "type": "int",
            "default": 20,
            "min": 2,
            "max": 200,
            "hint": "越大越平滑，越小越快",
        },
        {
            "key": "step_delay",
            "label": "每步间隔(ms)",
            "type": "int",
            "default": 10,
            "min": 0,
            "max": 200,
        },
        {
            "key": "pause",
            "label": "一轮结束后停顿(ms)",
            "type": "int",
            "default": 300,
            "min": 0,
            "max": 10000,
        },
    ]

    def on_start(self, ctx):
        raw = ctx.options.get("points", "")
        pts = []
        for chunk in str(raw).split("->"):
            chunk = chunk.strip()
            if not chunk or "," not in chunk:
                continue
            try:
                x, y = chunk.split(",", 1)
                pts.append((int(float(x)), int(float(y))))
            except ValueError:
                continue
        if len(pts) < 2:
            ctx.error = "坐标点至少要 2 个，格式如：10,10 -> 200,200"
            return False
        ctx.state["points"] = pts
        return True

    def run(self, ctx):
        from core import inputs

        pts = ctx.state.get("points") or []
        if len(pts) < 2:
            return 0

        steps = max(2, int(ctx.options.get("steps", 20)))
        step_delay_ms = max(0, int(ctx.options.get("step_delay", 10)))
        pause = max(0, int(ctx.options.get("pause", 300))) / 1000.0

        while not ctx.should_stop():
            for i in range(len(pts) - 1):
                if ctx.should_stop():
                    break
                inputs.drag(pts[i], pts[i + 1], steps=steps, step_delay_ms=step_delay_ms)
                ctx.clicked += 1   # 记为一次「动作」
                ctx.tick_count += 1
                # 拖拽本身已耗时，这里补一小段间隔，避免动作连成一片
                if not ctx.wait(ctx.base_interval()):
                    break
            if pause:
                ctx.wait(pause)
        return ctx.clicked


Registry.register(DragLoop())