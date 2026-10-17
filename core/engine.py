# -*- coding: utf-8 -*-
"""
core/engine.py —— 连点引擎 + 插件注册表

设计目标：加新功能只写一个插件文件，界面和引擎都不用改。

一个插件分两部分：
1. FIELD  —— 声明自己的界面控件（类型/标签/默认值/范围），GUI 读它自动画表单
2. run()  —— 拿到上下文对象 ctx，循环里干活

ctx 提供：
    ctx.should_stop()   -> bool     是否该停（用户点了停止 / 到次数上限）
    ctx.elapsed()       -> float    已跑秒数
    ctx.clicked         -> int      已点击次数
    ctx.tick_count      -> int      循环轮次
    ctx.inputs          -> module  core.inputs 模块
    ctx.options         -> dict    用户在本插件界面上的填写值
    ctx.state           -> dict    插件自己的持久化状态
    ctx.wait(sec)       -> None    可被中断的 sleep
"""

import threading
import time


# --------------------------------------------------------------------------
# 插件注册表
# --------------------------------------------------------------------------
class Registry(object):
    """全局插件表。用名字做 key，方便配置里存状态。"""

    _plugins = {}
    _order = []

    @classmethod
    def register(cls, plugin):
        """注册一个插件。plugin 需有 name 字段。"""
        name = getattr(plugin, "name", None)
        if not name:
            raise ValueError("插件必须有 name 字段")
        if name not in cls._plugins:
            cls._order.append(name)
        cls._plugins[name] = plugin
        return plugin

    @classmethod
    def get(cls, name):
        return cls._plugins.get(name)

    @classmethod
    def all(cls):
        """按注册顺序返回全部插件列表。"""
        return [cls._plugins[n] for n in cls._order]

    @classmethod
    def names(cls):
        return list(cls._order)

    @classmethod
    def clear(cls):
        cls._plugins = {}
        cls._order = []


class Plugin(object):
    """
    插件基类。子类覆盖需要的方法即可。

    子类可覆盖的属性：
        name        插件唯一标识（英文，用于配置存储）
        title       界面显示名（中文）
        description 一句话说明
        FIELD       界面控件声明列表
    """

    name = "base"
    title = "基础插件"
    description = "抽象基类，不该被直接使用"
    FIELD = []

    def build(self):
        """钩子：把 FIELD 翻译成控件定义列表，GUI 直接吃这个结构。"""
        out = []
        for f in self.FIELD:
            item = dict(f)
            item["key"] = "f_%s_%s" % (self.name, f["key"])
            out.append(item)
        return out

    def on_start(self, ctx):
        """钩子：开始前调用一次。返回 False 可阻止启动。"""

    def run(self, ctx):
        """钩子：主循环。默认实现是普通连点。"""
        from . import inputs

        while not ctx.should_stop():
            pos = ctx.cursor_position()
            if pos is None:
                break
            # 【2026-10-17 v26 修】固定坐标必须**先把光标搬过去**再点。
            # 旧代码只算了 pos 就丢掉了：命令行 --mode fixed --pos 100,200
            # 实际点在当前光标位置 —— 典型的「收了参数不用」，还查不到，
            # 因为界面上的固定坐标走的是 dashboard 自己的插件（它自己
            # 搬鼠标），只有 CLI 走这条默认主循环才暴露。
            if ctx.options.get("mode") == "fixed" and ctx.fixed_pos:
                inputs.move_to(*ctx.fixed_pos)
            elif ctx.options.get("move_back") and ctx.state.get("origin"):
                inputs.move_to(*ctx.state["origin"])

            inputs.click(ctx.options.get("button", "left"))

            ctx.clicked += 1
            if ctx.options.get("click_type") == "double":
                inputs.click(ctx.options.get("button", "left"))

            ctx.tick_count += 1
            ctx.wait(ctx.base_interval())
        return ctx.clicked

    def on_stop(self, ctx):
        """钩子：停止后调用一次。"""


# --------------------------------------------------------------------------
# 连点上下文
# --------------------------------------------------------------------------
class ClickContext(object):
    """传给插件 run() 的上下文。字段变化集中在 stop() / tick() 里。"""

    def __init__(self, options, state=None, stop_event=None, fixed_pos=None,
                 base_cps=10, jitter_ms=0, duration=0, count_limit=0,
                 zone="mouse"):
        self.options = dict(options or {})
        self.state = dict(state or {})
        self._stop_event = stop_event or threading.Event()
        self._start = time.time()

        # 【分区 · v34】这个引擎归哪个暂停区管。默认 "mouse" 与老行为一致；
        # 宏回放传 "macro"，这样它的启停键只影响宏自己，
        # 不会一按就把鼠标连点也定住。判定见 pausectl.is_paused。
        self.zone = zone or "mouse"
        self.fixed_pos = fixed_pos
        self.base_cps = max(1, int(base_cps))
        self.jitter_ms = max(0, int(jitter_ms))
        self.duration = max(0, int(duration))
        self.count_limit = max(0, int(count_limit))

        self.clicked = 0
        self.tick_count = 0
        self.error = None

    # -- 供插件调用 --------------------------------------------------
    def should_stop(self):
        if self._stop_event.is_set():
            return True
        if self.duration and self.elapsed() >= self.duration:
            return True
        # 次数上限按「动作轮次」算，不能按 clicked 算：
        # 像 wait / move 这类动作不会增加 clicked，只看 clicked 会导致
        # 上限永远触发不了，程序变成无限循环失控。
        if self.count_limit and self.tick_count >= self.count_limit:
            return True
        return False

    def elapsed(self):
        return time.time() - self._start

    def base_interval(self):
        """本次循环要等待的秒数（已含随机抖动）。"""
        base = 1.0 / self.base_cps
        if self.jitter_ms > 0:
            # 在 ±抖动 范围内随机，模拟人手不匀，防止被识别成机械固定频率
            base += random_uniform(-self.jitter_ms, self.jitter_ms) / 1000.0
        return max(0.001, base)

    def cursor_position(self):
        """
        按 mode 返回要点的位置。
        mode='fixed' -> 固定坐标；否则返回当前鼠标位置（None 表示拿不到）。
        """
        from . import inputs

        if self.options.get("mode") == "fixed" and self.fixed_pos:
            return tuple(self.fixed_pos)
        return inputs.get_cursor_pos()

    def wait(self, seconds):
        """
        可被中断的等待。返回 False 表示是被停止信号打断的（唯一含义）。

        【全局暂停门闩 —— 一处覆盖所有插件】
        每个插件的主循环都是「动作 → ctx.wait(间隔)」的结构，
        所以把暂停检查放在 wait 开头，normal / drag_loop / macro
        三个插件就都自动获得暂停能力，不用逐个改。
        代价：按下暂停后最多多执行一个动作才定住
        （CPS 20 时约 50ms，人眼不可感知）；
        仪表盘的鼠标连点另有循环顶部的严格门闩（零额外点击）。

        【坑】暂停期间本方法会一直阻塞（100ms 轮询）直到恢复或停止，
        **绝不**能因为轮询超时就返回 False：调用方清一色是
        `if not ctx.wait(...): break`，把 False 当「已停止」处理——
        一 break 连点就整个退出，暂停变成停止，恢复后再也不动。
        """
        from . import pausectl
        # 【分区暂停·2026-10-16】连点引擎归「鼠标区」管：
        # 键盘锁定区单独暂停时不能把连点一起冻住。
        # 全局 Pause 键会同时写所有区，所以这里照样生效。
        # 【v34】改成看 self.zone：宏回放引擎传的是 "macro"，
        # 于是宏的启停键只定宏；老调用（默认 mouse）行为一字不变。
        while pausectl.is_paused(self.zone):
            if self._stop_event.is_set():
                return False        # 暂停中按了停止：立刻放行，让循环退出
            # 超时只是轮询间隔到了，不是停止信号：继续等，别返回
            self._stop_event.wait(0.1)
        return not self._stop_event.wait(seconds)


def random_uniform(a, b):
    import random

    return random.uniform(a, b)


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
class ClickerEngine(object):
    """
    连点执行器。跑在后台线程，GUI 只管下命令。

    用法：
        engine = ClickerEngine(plugin, options, base_cps=10)
        engine.start()
        ...
        engine.stop()
    """

    def __init__(self, plugin, options, state=None, base_cps=10,
                 jitter_ms=0, duration=0, count_limit=0, fixed_pos=None,
                 zone="mouse"):
        self.plugin = plugin
        self._stop_event = threading.Event()
        self._thread = None
        self.ctx = ClickContext(
            options=options,
            state=state,
            stop_event=self._stop_event,
            fixed_pos=fixed_pos,
            base_cps=base_cps,
            jitter_ms=jitter_ms,
            duration=duration,
            count_limit=count_limit,
            zone=zone,
        )
        self.on_finish = None  # 结束回调：(ctx) -> None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="clicker")
        self._thread.daemon = True
        self._thread.start()
        return True

    def stop(self, wait=True):
        self._stop_event.set()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self):
        ctx = self.ctx
        try:
            if self.plugin.on_start(ctx) is False:
                return
            self.plugin.run(ctx)
        except Exception as exc:   # 插件抛异常不能让主进程崩
            ctx.error = exc
        finally:
            try:
                self.plugin.on_stop(ctx)
            except Exception:
                pass
            if self.on_finish:
                try:
                    self.on_finish(ctx)
                except Exception:
                    pass