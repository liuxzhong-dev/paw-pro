# -*- coding: utf-8 -*-
"""
plugins/normal_click.py —— 插件：普通连点

最基础的模式：在鼠标当前位置持续单击。可调 CPS、按键、双击、随机抖动。
"""

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.engine import Plugin, Registry


class NormalClick(Plugin):
    name = "normal"
    title = "普通连点"
    description = "在鼠标当前位置持续单击，支持 CPS 调节与随机抖动"

    FIELD = [
        {
            "key": "button",
            "label": "点击按键",
            "type": "choice",
            "options": [("left", "左键"), ("right", "右键"), ("middle", "中键")],
            "default": "left",
        },
        {
            "key": "click_type",
            "label": "点击方式",
            "type": "choice",
            "options": [("single", "单击"), ("double", "双击")],
            "default": "single",
        },
        {
            "key": "mode",
            "label": "点击位置",
            "type": "choice",
            "options": [("current", "跟随鼠标"), ("fixed", "固定坐标")],
            "default": "current",
        },
        {
            "key": "move_back",
            "label": "每次点完回到起始位置",
            "type": "bool",
            "default": False,
            "hint": "配合「固定坐标」用，可模拟人手往返点击",
        },
    ]

    def on_start(self, ctx):
        # 记住起始位置，供「回到起始位置」用
        ctx.state["origin"] = ctx.cursor_position()
        return True


Registry.register(NormalClick())