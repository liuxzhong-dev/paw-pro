# -*- coding: utf-8 -*-
"""
selftest.py —— 自检脚本

用途：打包后 / 改动后快速验证核心能力是否正常，不需要人肉点界面。
运行：python selftest.py

检查项：
1. 插件加载是否齐全
2. 引擎停止条件（限次 / 限时 / 手动中断）是否可靠
3. SendInput 是否真能注入
4. 坐标归一化边界是否精确
5. 界面能否构建（用 Tcl 解释器做无窗口检查）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = "通过", "失败"
results = []


def check(name, ok, detail=""):
    results.append((name, PASS if ok else FAIL, detail))
    print("  [%s] %s%s" % (PASS if ok else FAIL, name,
                (" -> " + detail) if detail else ""))
    return ok


def main():
    print("=" * 56)
    print(" 鼠标连点器 自检")
    print("=" * 56)

    # ---- 1. 插件加载 ----
    print("\n[1] 插件加载")
    from core.engine import Registry
    import plugins as loader

    ok, bad = loader.load_all()
    names = Registry.names()
    check("内置插件全部加载", len(names) >= 3, "已注册: %s" % ", ".join(names))
    if bad:
        check("无插件加载失败", False, "; ".join(bad))
    else:
        check("无插件加载失败", True)
    for n in names:
        p = Registry.get(n)
        check("插件 %s 表单字段可生成" % n, len(p.build()) > 0,
              "%d 个字段" % len(p.build()))

    # ---- 2. 引擎停止条件 ----
    print("\n[2] 引擎停止条件（用 wait 动作测，不产生真实点击）")
    import time
    from core.engine import ClickerEngine, ClickContext

    macro = Registry.get("macro")

    d = {}
    e = ClickerEngine(macro, {"steps": "wait@50", "loop": True, "round_pause": 0},
                      base_cps=20, count_limit=5)
    e.on_finish = lambda c: d.update(ticks=c.tick_count)
    e.start()
    time.sleep(1.8)
    check("次数上限精确生效", d.get("ticks") == 5, "设定5 实际 %s" % d.get("ticks"))

    d2 = {}
    e2 = ClickerEngine(macro, {"steps": "wait@30", "loop": True, "round_pause": 0},
                       base_cps=10, duration=1)
    e2.on_finish = lambda c: d2.update(t=c.elapsed())
    e2.start()
    time.sleep(2.2)
    check("限时自动停止", 0.9 <= d2.get("t", 0) <= 1.6, "用时 %.2f 秒" % d2.get("t", 0))

    e3 = ClickerEngine(macro, {"steps": "wait@20", "loop": True, "round_pause": 0},
                       base_cps=10, count_limit=0, duration=0)
    e3.start()
    time.sleep(0.8)
    alive = e3.running
    e3.stop()
    check("无上限时不被误停", alive)

    e4 = ClickerEngine(macro, {"steps": "wait@500", "loop": True, "round_pause": 0},
                       base_cps=2)
    e4.start()
    time.sleep(0.3)
    t0 = time.time()
    e4.stop(wait=True)
    cost = time.time() - t0
    check("手动停止响应迅速", cost < 1.5, "耗时 %.2f 秒" % cost)

    ctx = ClickContext({"x": 1}, base_cps=1000, jitter_ms=500)
    intervals = [ctx.base_interval() for _ in range(200)]
    check("随机抖动不产生非正间隔", min(intervals) > 0,
          "最小 %.5f 秒" % min(intervals))

    # ---- 3. SendInput 注入 ----
    print("\n[3] 鼠标事件注入（会真的点一下当前鼠标位置）")
    from core import inputs

    pos = inputs.get_cursor_pos()
    check("可读取鼠标位置", pos is not None, str(pos))
    if pos:
        check("单击注入成功", bool(inputs.click("left")))
        check("滚轮注入成功", bool(inputs.scroll(1)))

    sw, sh = inputs.screen_size()
    ax, ay = inputs._to_absolute(sw - 1, sh - 1)
    check("坐标归一化边界精确", (ax, ay) == (65535, 65535),
          "右下角 -> (%d,%d)" % (ax, ay))

    # ---- 4. 配置读写 ----
    print("\n[4] 配置读写")
    from core import config as cfg

    data = cfg.load()
    check("配置加载正常", isinstance(data, dict) and "cps" in data,
          "%d 个字段" % len(data))
    ok, err = cfg.save(data)
    check("配置写入正常", ok, err or cfg.config_path())

    # ---- 5. 界面构建 ----
    #
    # 【这里曾经测 tkinter，已改测 pywebview】
    # 主界面从 v4 起就换成 pywebview（内嵌 Edge/WebView2）了，
    # app.py 那套 Tk 界面是历史遗留。继续测 tkinter 只会得到
    # 「No module named 'tkinter'」——本机三个 Python 环境全都没装，
    # 也就是说这条断言在报告**环境缺失**，而不是**代码有问题**。
    # 真正该验的是「主界面依赖的库在不在」。
    print("\n[5] 界面构建（无窗口检查）")
    try:
        import webview as _wv      # noqa: F401
        check("pywebview 可用（主界面依赖）", True)
    except Exception as exc:
        check("pywebview 可用（主界面依赖）", False, str(exc))

    try:
        import dashboard as _dash
        check("dashboard 模块可导入", hasattr(_dash, "HTML")
              and hasattr(_dash, "Api"))
    except Exception as exc:
        check("dashboard 模块可导入", False, str(exc))

    # ---- 6. JS 桥接安全 ----
    #
    # 【2026-10-15 v9 实测事故】pywebview 6.x 的 util.get_functions 在生成
    # JS 桥时会把 js_api 上每个**公开**的非函数对象属性递归展开
    # （跳过 _ 开头），且没有环检测。当时 Api.window 挂着 pywebview 的
    # Window 对象 → native → AccessibilityObject.Bounds.Empty.Empty...
    # 无限自引用，直接爆 RecursionError：桥接线程挂掉、loaded 事件不
    # 触发、主线程死等 → 窗口「无响应」30 秒被系统强杀（AppHangB1）。
    # 修法：窗口引用一律下划线开头（api._window），句柄走 api_ref。
    # 本断言守住这条线：Api 上不得有会被递归展开的公开对象属性。
    # （get_functions 是 inject_pywebview 里的闭包，import 不到，
    #   所以这里复刻它的判据做静态扫描 + 负向样本验证。）
    print("\n[6] JS 桥接安全（Api 公开属性不含对象图）")
    try:
        def _scan(obj):
            """复刻 pywebview get_functions 的递归判据：公开（不 _ 开头）、
            非可调用、有 __module__ 的对象属性——这类属性会被递归展开。"""
            bad = []
            for name in dir(obj):
                if name.startswith("_"):
                    continue
                try:
                    attr = getattr(obj, name)
                except Exception:
                    continue
                if callable(attr):
                    continue
                if hasattr(attr, "__module__"):
                    bad.append("%s=%s" % (name, type(attr).__name__))
            return bad

        api = _dash.Api()
        bad = _scan(api)
        check("Api 无公开对象属性（不爆 JS 桥递归）", not bad, "，".join(bad))

        # 负向验证：喂一个「公开属性挂自引用对象」的样本，
        # 确认判据真的能报警——检查失效时显示的也是全绿
        class _Bomb(object):
            def __init__(self):
                self.native = self      # 模拟 Window.native 的自引用

        probe = _dash.Api()
        probe.window = _Bomb()          # 就是当年出事的那一手
        caught = any(x.startswith("window=") for x in _scan(probe))
        check("负向样本能被判据抓住（检查本身有效）", caught)
    except Exception as exc:
        check("Api 桥接安全", False, str(exc))

    # ---- 汇总 ----
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = len(results) - passed
    print("\n" + "=" * 56)
    print(" 合计 %d 项：通过 %d，失败 %d" % (len(results), passed, failed))
    if failed:
        print(" 失败项：")
        for name, status, detail in results:
            if status == FAIL:
                print("   - %s %s" % (name, detail))
    print("=" * 56)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())