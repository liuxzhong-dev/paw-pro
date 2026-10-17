# -*- coding: utf-8 -*-
"""
plugins/jiggler.py —— 插件：鼠标抖器（jiggler · S6 / v23）
==============================================

【是什么】每隔一段时间把鼠标轻移 1 像素再移回，**只挪不点**。
系统会认为「有人在动鼠标」：防息屏、防挂机检测
（会议软件的不活跃状态、远程桌面断连都靠这个）。

【和连点器的分工】连点是「持续产生点击」，抖器是「持续产生微量移动」。
两者互斥没有意义，但参数体系完全一样（间隔/次数/时长/倒计时/暂停都通用），
所以做成引擎插件而不是外挂：仪表盘鼠标区选「抖器」动作即用，
命令行 `--mode jiggler` 无头跑（挂机保活）。

【负向对照锚点】本插件**绝不调用 click**：ctx.clicked 必须始终是 0。
探针里有断言——哪次改漏了把点击发出来，测试直接红。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.engine import Plugin, Registry


class Jiggler(Plugin):
    name = "jiggler"
    title = "鼠标抖器"
    description = ("每隔一段时间轻移鼠标 1 像素再移回，只挪不点，"
                   "防息屏 / 防挂机检测")

    FIELD = [
        {
            "key": "interval",
            "label": "挪动间隔(秒)",
            "type": "number",
            "default": 30,
            "hint": "每隔多少秒挪一次",
        },
        {
            "key": "distance",
            "label": "挪动距离(像素)",
            "type": "number",
            "default": 1,
            "hint": "1 像素足够被系统感知，又不影响正常使用",
        },
    ]

    def run(self, ctx):
        from core import inputs

        interval = float(ctx.options.get("interval", 30) or 30)
        interval = max(0.5, interval)
        distance = int(ctx.options.get("distance", 1) or 1)
        distance = max(1, distance)
        jitter = getattr(ctx, "jitter_ms", 0) or 0

        # 【v23】固定坐标模式：启动时先把鼠标搬到设定点，之后绕着
        # **当时所在位置** 挪。不每轮硬搬——用户中途把鼠标移走是
        # 故意的，每轮拽回来等于和用户抢鼠标。
        anchor = getattr(ctx, "fixed_pos", None)
        if anchor:
            inputs.move_to(int(anchor[0]), int(anchor[1]))
            if not ctx.wait(0.05):
                return ctx.clicked

        while not ctx.should_stop():
            pos = inputs.get_cursor_pos()
            if pos is None:
                break                   # 拿不到光标位置：没法绕着他挪
            x, y = pos
            inputs.move_to(x + distance, y)
            if not ctx.wait(0.05):
                break                   # 停止信号：立刻收手
            inputs.move_to(x, y)        # 挪回原位，用户无感
            # 只挪不点：tick_count 记「挪了几次」（次数上限按它算），
            # clicked 保持 0 —— 所有 KPI 显示/统计都读 clicked，
            # 抖器显示 0 次点击才是对的。
            ctx.tick_count += 1
            gap = interval
            if jitter > 0:
                gap += random_uniform(-jitter, jitter) / 1000.0
            if not ctx.wait(max(0.1, gap)):
                break
        return ctx.clicked


def random_uniform(a, b):
    import random
    return random.uniform(a, b)


Registry.register(Jiggler())
