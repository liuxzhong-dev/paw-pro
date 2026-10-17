# -*- coding: utf-8 -*-
"""
cli.py —— 命令行版入口

用途：临时任务、计划任务、批量调用。不想开界面时用它。
打包出的 cli 版 exe 和界面版 exe 行为一致，只是入口不同。

示例：
  鼠标连点器.exe --cps 20 --duration 30
  鼠标连点器.exe --mode fixed --pos 100,200 --count 50
  鼠标连点器.exe --mode macro --steps "click;wait@300;click"
  鼠标连点器.exe --hotkey F8              # 带界面启动，热键控制
  鼠标连点器.exe --selftest                # 跑自检
  鼠标连点器.exe --list-plugins            # 列出插件（自检命令，验证打包是否带全插件）

跑完不退出（重点功能）：
  双击 exe 走交互引导时**默认**就是这个行为：一轮跑完停在菜单上，
  输入 1（或直接回车）再来一轮，输入 2 重配参数，输入 q 退出。
  带参数跑时加 --repeat 也能停在菜单上；--once 是「跑完就退出」的老行为。
  鼠标连点器.exe --cps 20 --duration 30 --repeat
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfg
from core import actions
from core.engine import ClickerEngine, Registry


def _fix_console_encoding():
    """Windows 控制台默认 GBK，中文会乱码。切成 UTF-8。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            # line_buffering: exe 双击运行时输出会卡在缓冲区，
            # 不加这个实时进度一个都看不到
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except Exception:
            pass


def build_parser():
    p = argparse.ArgumentParser(
        prog="鼠标连点器",
        description="通用鼠标连点工具：支持普通连点、循环拖拽、动作宏三种模式",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="不传参数则读取同目录 config.json；命令行参数会覆盖配置文件。",
    )
    p.add_argument("--cps", type=int, help="每秒点击次数 1~100（默认读配置 10）")
    p.add_argument("--duration", type=int, help="持续秒数，0 = 不限")
    p.add_argument("--count", type=int, help="动作次数上限，0 = 不限")
    p.add_argument("--jitter", type=int, help="随机抖动毫秒，让节奏不那么机械")
    p.add_argument("--button", choices=["left", "right", "middle"], help="点击按键")
    p.add_argument("--mode", choices=["normal", "drag_loop", "macro"], help="运行模式")
    p.add_argument("--pos", help="固定坐标，格式 x,y（例：--pos 100,200）")
    p.add_argument("--steps", help="宏动作队列，用分号分隔（例：\"click;wait@300;click\"）")
    p.add_argument("--points", help="拖拽坐标序列（例：\"10,10 -> 200,200\"）")
    p.add_argument("--hotkey", help="带界面启动时的全局热键，默认 F8")
    p.add_argument("--profile", help="按名字加载图形版存好的方案（profiles/目录），"
                                    "无头启动；方案不存在会直接报错退出")
    p.add_argument("--list-profiles", action="store_true",
                   help="列出全部已存方案后退出")
    p.add_argument("--list-plugins", action="store_true", help="列出已加载插件后退出")
    p.add_argument("--selftest", action="store_true", help="运行自检后退出")
    p.add_argument("--gui", action="store_true", help="强制以图形界面启动")
    p.add_argument("--repeat", action="store_true",
                   help="跑完一轮不退出，停在菜单上等指令（1 再来一次 / 2 重配 / q 退出）")
    p.add_argument("--once", action="store_true",
                   help="跑完一轮直接退出（覆盖交互模式默认的「跑完等待」）")
    p.add_argument("--prepare", type=int, default=3,
                   help="每轮开始前的准备秒数，回车可提前开始（默认 3，0 = 不等）")
    p.add_argument("--next-key", default="f9",
                   help="等待菜单时「直接开始下一轮」的全局热键，默认 f9；填 none 关闭")
    return p


def cmd_list_plugins():
    import plugins as loader
    loader.load_all()
    print("已加载插件：")
    for name in Registry.names():
        pl = Registry.get(name)
        print("  - %-10s %s" % (name, pl.title))
        print("    %s" % pl.description)
    return 0


def cmd_list_plugins():
    import plugins as loader
    loader.load_all()
    print("已加载插件：")
    for name in Registry.names():
        pl = Registry.get(name)
        print("  - %-10s %s" % (name, pl.title))
        print("    %s" % pl.description)
    return 0


def cmd_list_profiles():
    """列出全部方案（S5：让用户确认 --profile 该填什么）。"""
    names = cfg.list_profiles()
    if not names:
        print("还没有保存过方案。在图形版界面调好参数 -> 齿轮面板 -> "
              "「存为方案」，或直接把配置 json 放进 profiles/ 目录。")
        print("方案目录：%s" % cfg.profiles_dir())
        return 0
    print("已保存的方案（--profile 用这些名字）：")
    for n in names:
        print("  - %s" % n)
    print("方案目录：%s" % cfg.profiles_dir())
    return 0


# 鼠标区表单动作 -> 命令行点击类型的映射（S5）
_ACTION_TO_CLICK_TYPE = {"click": "single", "double": "double",
                         "triple": "triple"}


def data_from_profile(name):
    """
    把图形版存的方案翻译成命令行的 data 字典（S5 核心）。

    方案文件是界面表单的形态（mouse/key/hide 三段，字段是
    button/action/cps/mode/pos……），命令行的 data 是另一套键
    （cps/button/mode/fixed_pos/random_jitter……），这里做映射。

    返回 (data, mode, msg)。任何不匹配都**大声**返回错误消息：
    调用方必须退出，绝不能静默用默认值开点——挂错参数刷错东西
    比打不开严重得多（S5 验收要点）。
    """
    ok, cleaned, msg = cfg.load_profile(name)
    if not ok:
        return None, None, msg
    mouse = cleaned.get("mouse")
    if not isinstance(mouse, dict) or not mouse:
        return None, None, "方案里没有鼠标区参数（mouse 段为空）"

    action = str(mouse.get("action", "click") or "click")
    if action == "hold":
        return None, None, ("方案动作是「长按」，命令行版不支持"
                            "（请用「鼠标连点器-长按锁定.exe」）")

    data = cfg.load()                      # 默认值打底，方案覆盖
    try:
        data["cps"] = int(float(mouse.get("cps", 10) or 10))
        data["random_jitter"] = int(mouse.get("jitter", 0) or 0)
        data["duration"] = int(float(mouse.get("duration", 0) or 0))
        data["count_limit"] = int(mouse.get("count", 0) or 0)
        data["done_action"] = str(mouse.get("done", "none") or "none")
    except Exception as e:
        return None, None, "方案参数无法识别：%s" % e
    if data["done_action"] not in ("none", "sound", "lock", "shutdown"):
        data["done_action"] = "none"
    data["button"] = str(mouse.get("button", "left") or "left")

    if action == "jiggle":
        # 抖器走插件注册表里的 jiggler（v23 S6）
        mode = "jiggler"
        ps = dict(data.get("plugin_state") or {})
        try:
            interval = max(0.5, float(mouse.get("jig", 30) or 30))
        except Exception:
            interval = 30.0
        ps["jiggler"] = {"interval": interval, "distance": 1}
        data["plugin_state"] = ps
    else:
        mode = "normal"
        data["click_type"] = _ACTION_TO_CLICK_TYPE.get(action, "single")

    # 固定坐标：方案的 pos 可能是 "x,y" 字符串或 [x, y] 列表
    pos = mouse.get("pos")
    pos_mode = str(mouse.get("mode", "cursor") or "cursor")
    # 【v26】多点轮询 / 框内随机只有图形版实现（引擎的默认主循环只认
    # 单点）。这里必须**大声**拒绝：静默退化成「跟随光标」会在一堆
    # 错误的坐标上狂点，比打不开危险得多（S5 的同一条验收要点）。
    if pos_mode in ("multi", "box"):
        return None, None, (
            "方案的位置模式是「%s」，命令行版不支持（引擎只认单个坐标）；"
            "请用图形版「爪Pro.exe」运行，或把方案改成「固定坐标」"
            % ("多点轮询" if pos_mode == "multi" else "框内随机"))
    if pos_mode == "fixed" and pos:
        try:
            if isinstance(pos, str):
                x, y = pos.replace("，", ",").split(",")
            else:
                x, y = pos[0], pos[1]
            data["fixed_pos"] = [int(float(x)), int(float(y))]
            data["mode"] = "fixed"
        except Exception:
            return None, None, "方案里的固定坐标无法识别：%r" % (pos,)
    else:
        data["mode"] = "current"

    note = ""
    if isinstance(cleaned.get("key"), dict) and cleaned["key"]:
        note = "（方案含键盘区参数，命令行版只取鼠标区参数）"
    return data, mode, note


def resolve(args, base=None):
    """合并配置与命令行参数：命令行优先。base 可传方案字典（S5）。"""
    data = base if base is not None else cfg.load()
    if args.cps is not None:
        data["cps"] = args.cps
    if args.duration is not None:
        data["duration"] = args.duration
    if args.count is not None:
        data["count_limit"] = args.count
    if args.jitter is not None:
        data["random_jitter"] = args.jitter
    if args.button:
        data["button"] = args.button
    if args.pos:
        try:
            x, y = args.pos.split(",", 1)
            data["fixed_pos"] = [int(float(x)), int(float(y))]
            data["mode"] = "fixed"
        except ValueError:
            raise SystemExit("[参数错误] --pos 格式应为 x,y，例：--pos 100,200")

    mode = args.mode
    plugin_state = data.get("plugin_state") or {}

    if mode == "macro":
        if not args.steps:
            raise SystemExit("[参数错误] --mode macro 需要搭配 --steps")
        plugin_state["macro"] = {"steps": args.steps, "loop": True, "round_pause": 500}
    elif mode == "drag_loop":
        if not args.points:
            raise SystemExit("[参数错误] --mode drag_loop 需要搭配 --points")
        plugin_state["drag_loop"] = {
            "points": args.points, "steps": 20, "step_delay": 10, "pause": 300,
        }
    elif mode == "normal":
        plugin_state.setdefault("normal", {})
        plugin_state["normal"]["button"] = args.button or data.get("button", "left")
        plugin_state["normal"]["mode"] = "fixed" if args.pos else "current"

    data["plugin_state"] = plugin_state
    return data, (mode or "normal")


# --------------------------------------------------------------------------
# 轮次间的指令菜单
# --------------------------------------------------------------------------
# 【为什么要有这一块】
# 主人反馈：「执行完一次任务以后暂停不退出程序，并且可以比如输入 1 再执行一次」。
# 以前 run_headless 是「跑完 -> 打印一行 -> return 0 -> 进程结束」，
# 想再跑一次只能重新双击 exe、把参数问题再答一遍。
# 现在一轮跑完停在菜单上，参数原样保留，1 + 回车就再来一轮。
CMD_AGAIN = "again"      # 再来一次
CMD_RECONF = "reconf"    # 回到参数配置，重新问一遍
CMD_QUIT = "quit"        # 退出程序

RC_OK = 0
RC_ERROR = 2
RC_RECONFIG = 3          # 用户要重新配置，交给 cmd_interactive 接着问


def _kbhit():
    """控制台里有没有按键等着读。非 Windows / 非 tty 恒 False。"""
    if sys.platform != "win32":
        return False
    try:
        import msvcrt
        return bool(msvcrt.kbhit())
    except Exception:
        return False


def _get_key():
    """读一个按键（不自动回显，需要自己 print）。失败返回空串。"""
    try:
        import msvcrt
        return msvcrt.getwch()
    except Exception:
        return ""


def _parse_command(raw):
    """
    把用户输入翻译成指令。空输入（直接回车）= 再来一次。

    认不出来的输入一律按「再来一次」处理：菜单上卡住不动
    比多跑一轮难受得多，而多跑一轮按暂停键就能定住。
    """
    text = (raw or "").strip().lower()
    if text in ("", "1", "y", "yes", "again", "go", "run", "start"):
        return CMD_AGAIN
    if text in ("2", "r", "c", "reconf", "config", "reset", "set"):
        return CMD_RECONF
    if text in ("q", "0", "n", "no", "quit", "exit", "end", "stop"):
        return CMD_QUIT
    return CMD_AGAIN


def _can_poll_keys():
    """能不能用 msvcrt 轮询读键。管道/重定向输入时只能退回 input()。"""
    if sys.platform != "win32":
        return False
    try:
        import msvcrt         # noqa: F401
    except Exception:
        return False
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _read_command(next_event=None):
    """
    在轮次之间读一条指令。

    【为什么不用一句 input()】
    input() 是阻塞的，等待期间主线程什么都做不了 —— 「下一轮」热键
    在别的线程被按下也没人处理。所以 Windows 控制台下改成 msvcrt 轮询
    （50ms 一轮）：既能收键盘，也能同时看到热键事件。
    输入被重定向（脚本调用）时没有控制台可轮询，退回 input()。
    """
    sys.stdout.write("  请选择 [1]：")
    sys.stdout.flush()

    if not _can_poll_keys():
        try:
            return _parse_command(input())
        except EOFError:
            return CMD_QUIT

    buf = ""
    while True:
        if next_event is not None and next_event.is_set():
            next_event.clear()
            sys.stdout.write("\n[热键触发] 开始下一轮\n")
            sys.stdout.flush()
            return CMD_AGAIN
        if _kbhit():
            ch = _get_key()
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return _parse_command(buf)
            if ch == "\x03":            # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x1b":            # Esc
                sys.stdout.write("\n")
                sys.stdout.flush()
                return CMD_QUIT
            if ch == "\x08":            # 退格
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch and ch not in ("\x00", "\xe0"):
                buf += ch
                sys.stdout.write(ch)
                sys.stdout.flush()
        time.sleep(0.05)


def _countdown(seconds, next_event=None, tip=""):
    """轮次开始前的准备时间：让人把鼠标移到目标位置。回车可提前开始。"""
    if seconds <= 0:
        return
    print(tip or "把鼠标移到目标位置，%d 秒后自动开始（回车立即开始）……" % seconds)
    end = time.time() + seconds
    left = -1
    while time.time() < end:
        if next_event is not None and next_event.is_set():
            next_event.clear()
            break
        if _kbhit():
            ch = _get_key()
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
        now = max(0, int(end - time.time() + 0.999))
        if now != left:
            left = now
            sys.stdout.write("\r  准备：%d 秒…（回车立即开始）   " % left)
            sys.stdout.flush()
        time.sleep(0.05)
    sys.stdout.write("\r" + " " * 46 + "\r")
    sys.stdout.flush()


def _register_pause_hotkey():
    """注册全局暂停热键，返回 (按键名, listener)，失败返回 (None, None)。"""
    from core import hotkey as hk
    from core import pausectl
    from core import toast

    def _toggle_pause():
        now = pausectl.toggle_paused()
        if now:
            print("\n[已暂停] 所有动作定住，再按一次暂停键恢复")
            toast.show_paused()
        else:
            print("\n[已恢复] 继续运行")
            toast.show_resumed()

    for cand in ("pause", "scrolllock", "ctrl+pause"):
        try:
            hl = hk.HotkeyListener(cand, _toggle_pause)
            good, _err = hl.start()
            if good:
                return cand, hl
        except Exception:
            continue
    return None, None


def _register_next_hotkey(key_text, event):
    """
    注册「再来一轮」热键，返回 (按键名, listener)，失败返回 (None, None)。

    【为什么要热键】主人玩原神时控制台在后面，切回来敲 1 很麻烦；
    有个全局键就不用切窗口。注册失败（被游戏占用）不影响正常使用，
    照样能在控制台里输入 1。
    """
    if not key_text or str(key_text).strip().lower() in ("none", "off", "0", ""):
        return None, None
    from core import hotkey as hk

    def _fire():
        event.set()

    try:
        hl = hk.HotkeyListener(key_text, _fire)
        good, _err = hl.start()
        if good:
            return key_text, hl
    except Exception:
        pass
    return None, None


def _cli_done_tick(left):
    """命令行版收尾动作倒计时的每秒回调（S4）。"""
    sys.stdout.write("\r  收尾动作倒计时：%d 秒（Ctrl+C 取消）   "
                     % max(0, int(left)))
    sys.stdout.flush()


def _arm_done_action(data):
    """自然完成后按方案/配置排定收尾动作（S4）。返回是否排上了。"""
    action = str(data.get("done_action", "none") or "none")
    if action in ("", "none"):
        return False
    if actions.arm(action, on_tick=_cli_done_tick):
        print("\n  已排定收尾动作「%s」，倒计时中……按 Ctrl+C 取消"
              % action)
        return True
    return False


def _run_one_round(plugin, options, data, round_no):
    """跑一轮，返回 ctx。进度用 \\r 覆盖式输出。"""
    finished = {"ctx": None}

    def _on_finish(ctx):
        finished["ctx"] = ctx

    engine = ClickerEngine(
        plugin,
        options=options,
        state=options,
        base_cps=data["cps"],
        jitter_ms=data["random_jitter"],
        duration=data["duration"],
        count_limit=data["count_limit"],
        fixed_pos=data.get("fixed_pos"),
    )
    engine.on_finish = _on_finish
    engine.start()

    try:
        last = -1
        while engine.running:
            time.sleep(0.5)
            ctx = engine.ctx
            if ctx.clicked != last:
                last = ctx.clicked
                sys.stdout.write("\r  第 %d 轮：已执行 %d 次 / %.1f 秒"
                                 % (round_no, ctx.clicked, ctx.elapsed()))
                sys.stdout.flush()   # \r 覆盖式输出必须手动flush，否则看不到刷新
        print()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止……")
        actions.cancel()          # S4：中断 = 收尾动作一并取消
        engine.stop()
    return finished.get("ctx") or engine.ctx


def run_headless(data, mode, loop=False, prepare=3, next_key="f9"):
    """
    不显示界面，直接开跑。

    loop=True 时跑完一轮**不退出**，停在菜单上等指令：
        1 / 回车 = 再来一轮（参数不变）
        2        = 重新配置参数
        q        = 退出
    轮次之间暂停热键一直有效，「下一轮」热键也能直接开跑。
    """
    import plugins as loader
    loader.load_all()

    plugin = Registry.get(mode)
    if not plugin:
        raise SystemExit("[错误] 未知模式: %s" % mode)

    options = dict((data.get("plugin_state") or {}).get(mode) or {})
    if mode == "normal":
        options.setdefault("button", data.get("button", "left"))
        options.setdefault("click_type", "single")
        options.setdefault("mode", "fixed" if data.get("fixed_pos") else "current")

    print("模式：%s（%s）" % (mode, plugin.title))
    print("CPS=%s  时长=%s秒  次数上限=%s  抖动=%sms"
          % (data["cps"], data["duration"], data["count_limit"],
             data["random_jitter"]))
    if options.get("mode") == "fixed" or mode == "drag_loop":
        print("位置：%s" % (data.get("fixed_pos") or options.get("points")))
    if mode == "jiggler":
        print("挪动间隔：%s 秒（只挪不点，防息屏/挂机检测）"
              % options.get("interval", 30))
    if data.get("done_action", "none") not in ("none", "", None):
        print("完成后动作：%s（自然完成才触发，Ctrl+C 可取消）"
              % data["done_action"])

    from core import toast

    pause_key, pause_listener = _register_pause_hotkey()
    if pause_key:
        print("按 %s 全局暂停/恢复（暂停≠停止，参数保留），Ctrl+C 退出。"
              % pause_key)
    else:
        print("暂停键注册失败（可能被占用），只能用 Ctrl+C 退出。")

    next_event = threading.Event()
    next_key_name, next_listener = (None, None)
    if loop:
        next_key_name, next_listener = _register_next_hotkey(next_key, next_event)
    if next_key_name:
        print("按 %s 直接开始下一轮（不用切回本窗口）。" % next_key_name)
    print()

    result = RC_OK
    round_no = 0
    try:
        while True:
            if round_no > 0:
                _countdown(prepare, next_event)
            round_no += 1

            # 状态弹窗：命令行版也能在游戏最前方看到「启动/暂停/停止」
            toast.show_custom("第 %d 轮 已启动（CPS=%s）"
                              % (round_no, data["cps"]), "start")
            ctx = _run_one_round(plugin, options, data, round_no)

            if ctx.error:
                print("[运行出错] %s" % ctx.error)
                result = RC_ERROR
                break
            print("  完成：执行 %d 次，用时 %.1f 秒" % (ctx.clicked, ctx.elapsed()))

            # 【v23 S4】自然完成（次数/时长上限到了，不是 Ctrl+C 中断）
            # 才排定收尾动作。判断式与 dashboard._finished 保持一致。
            ticks = getattr(ctx, "tick_count", 0)
            natural = ((data.get("count_limit") and ticks >= data["count_limit"])
                       or (data.get("duration")
                           and ctx.elapsed() >= data["duration"] - 1.0))
            if natural:
                _arm_done_action(data)

            if not loop:
                break

            print("-" * 56)
            print("  本轮结束，等你发话（参数原样保留）：")
            print("    1 + 回车 = 再来一次")
            print("    2 + 回车 = 重新配置参数")
            print("    q + 回车 = 退出")
            if next_key_name:
                print("    （或按 %s 直接再来一轮）" % next_key_name)
            # 用 show 而不是 show_custom：show_custom 是两参数的老封装，
            # 传不进「停留秒数」。菜单可能等很久，弹窗给 30 秒。
            toast.show("本轮结束 —— 1 再来一次 / q 退出", "info", 30)
            cmd = _read_command(next_event)
            if cmd == CMD_QUIT:
                break
            if cmd == CMD_RECONF:
                result = RC_RECONFIG
                break
            print()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出……")
        actions.cancel()          # S4：退出前撤掉未执行的收尾动作
    finally:
        for listener in (pause_listener, next_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        toast.show_custom("%s 已停止（共 %d 轮）" % (plugin.title, round_no),
                          "stop")
        toast.stop()
    return result


def cmd_interactive(loop=True, prepare=3, next_key="f9"):
    """
    无参数双击时进入的交互引导。

    以前这里是「尝试开图形界面 -> 没有 tkinter -> 报错退出」，
    表现就是双击一下窗口闪就没了，用户完全不知道发生了什么。
    现在改成一步步提问，用户不接触命令行也能跑。

    loop 默认 True：一轮跑完**停住不退出**，等用户输 1 再来一轮。
    这是主人要的行为——「执行完一次任务以后暂停不退出程序」。
    """
    print("=" * 56)
    print(" 鼠标连点器  （直接按提示回答即可，回车用默认值）")
    print("=" * 56)

    # 权限预检：低级权限向高级权限窗口注入输入会被 UIPI 静默丢弃，
    # 而 SendInput 依然返回成功——表现为「点了没反应」但没任何报错。
    # 提前提示比让用户反复试错快得多。
    try:
        from diag_privilege import rid_value
        _my_rid = rid_value()
        if _my_rid is not None and _my_rid < 0x3000:
            _higher = []
            try:
                import diag_target as _dt
                for _w in _dt.list_windows():
                    _r = rid_value(_w.pid)
                    if _r is not None and _r > _my_rid:
                        _higher.append((_w.title[:22], _w.pid))
            except Exception:
                pass
            if _higher:
                print("\n ⚠ 权限提示：本工具是「普通用户」权限运行，"
                      "但检测到这些程序是「管理员」权限：")
                for _t, _pid in _higher[:5]:
                    print("     - %s (pid=%d)" % (_t, _pid))
                print("   向它们注入输入会被 Windows 静默丢弃，"
                      "而本程序仍会报「成功」。")
                print("   如果目标程序是上面这些，请右键本 exe "
                      "→「以管理员身份运行」后再试。\n")
    except Exception:
        pass

    def ask(prompt, default, cast=int):
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return None
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            print("      输入无效，改用默认值 %s" % default)
            return default

    # run_headless 跑完一轮会停在菜单上；选「重新配置」时返回 RC_RECONFIG，
    # 这里就回到下面重新问一遍参数，不用关掉程序重开。
    while True:
        print("\n选择模式：")
        print("  1 = 普通连点（默认，在鼠标当前位置点）")
        print("  2 = 固定坐标连点（永远点同一个位置）")
        print("  3 = 循环拖拽（在一串坐标点间反复拖）")
        print("  4 = 动作宏（多步动作排成队列）")
        print("  5 = 鼠标抖器（只挪不点，防息屏/挂机检测，v23 新增）")
        # 【坑】默认值原来写的是整数 1，用户直接回车拿到的就是 int，
        # 再 .strip() 立刻 AttributeError —— 表现为「回答完问题程序就没了」。
        # 一路回车是最常见的操作，所以默认值必须是字符串。
        choice = str(ask("请输入序号 [1]: ", "1", str)).strip() or "1"

        cps = ask("每秒点击次数 CPS [10]: ", 10, int)
        if cps is None:
            return 0
        cps = max(1, min(100, cps))

        duration = ask("持续多少秒（0=一直点）[10]: ", 10, int)
        count_limit = ask("点击总次数（0=不限）[0]: ", 0, int)
        jitter = ask("随机抖动毫秒（0=不抖动）[0]: ", 0, int)

        args = argparse.Namespace()
        args.cps, args.duration, args.count, args.jitter = cps, duration, count_limit, jitter
        args.button = None
        args.jitter = jitter

        if choice == "2":
            raw = ask("目标坐标 x,y [100,100]: ", "100,100", str)
            try:
                x, y = str(raw).split(",", 1)
                args.pos = "%d,%d" % (int(float(x)), int(float(y)))
            except ValueError:
                args.pos = "100,100"
            args.mode, args.steps, args.points = None, None, None

        elif choice == "3":
            args.points = ask("坐标序列，如 100,100 -> 400,300 [100,100 -> 400,300]: ",
                              "100,100 -> 400,300", str)
            args.mode, args.pos, args.steps = "drag_loop", None, None

        elif choice == "4":
            args.steps = ask("动作队列，如 click;wait@300;click [click;wait@300;click]: ",
                             "click;wait@300;click", str)
            args.mode, args.pos, args.points = "macro", None, None

        elif choice == "5":
            # v23 S6 抖器：问个挪动间隔，其余参数（时长/次数/倒计时）通用
            args.jig_interval = max(0.5, float(
                ask("每隔几秒挪一次鼠标 [30]: ", 30, int) or 30))
            args.mode, args.pos, args.steps, args.points = "jiggler", None, None, None

        else:
            args.mode, args.pos, args.steps, args.points = None, None, None, None

        print("\n" + "-" * 56)
        print("配置完成，准备开始。")
        print("鼠标移到要点击的位置，停止按 Ctrl+C")
        print("-" * 56)
        _countdown(prepare, None)

        data, mode = resolve(args)
        if mode == "jiggler":
            ps = dict(data.get("plugin_state") or {})
            ps["jiggler"] = {"interval": getattr(args, "jig_interval", 30),
                             "distance": 1}
            data["plugin_state"] = ps
        cfg.save(data)
        rc = run_headless(data, mode, loop=loop, prepare=prepare,
                          next_key=next_key)
        if rc == RC_RECONFIG:
            print("\n（回到参数配置，重新问一遍）")
            continue
        return rc


def main():
    _fix_console_encoding()
    # DPI 感知：让坐标拿的是物理像素（固定坐标/宏/录制回放不走偏）。
    # 必须在建窗口前调用；失败不影响运行，只是回到旧行为。
    try:
        from core import dpi
        dpi.enable_dpi_awareness()
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args()

    if args.selftest:
        import selftest
        return selftest.main()

    if args.list_plugins:
        return cmd_list_plugins()

    if args.list_profiles:
        return cmd_list_profiles()

    # 【v23 S5】--profile：把图形版存的方案作为底稿，命令行参数覆盖它。
    # 方案加载失败直接退出——静默用默认值开点比打不开严重得多。
    base = None
    profile_mode = None
    if args.profile:
        if args.gui or bool(args.hotkey):
            print("[提示] --profile 只作用于命令行模式；本次指定了界面"
                  "启动，方案未加载。\n")
        else:
            base, profile_mode, pmsg = data_from_profile(args.profile)
            if base is None:
                raise SystemExit(
                    "[错误] --profile 加载失败：%s\n"
                    "（用 --list-profiles 看看有哪些方案）" % pmsg)
            if pmsg:
                print("[提示] %s" % pmsg)
            print("已加载方案「%s」" % args.profile)

    has_run_args = any(
        v is not None and v is not False
        for v in (args.cps, args.duration, args.count, args.jitter,
                  args.button, args.mode, args.pos, args.steps,
                  args.points, args.profile)
    )

    # 跑完要不要停在菜单上：
    #   交互引导（双击）默认「停住等指令」——这是主人要的行为；
    #   带参数跑默认「跑完就退出」，保持脚本调用的老语义，加 --repeat 才停。
    #   --once 优先级最高，两种模式都强制跑完就退出。
    prepare = max(0, int(args.prepare or 0))
    loop = (not args.once) and (args.repeat or not has_run_args)

    # --gui / --hotkey 明确要界面时才尝试开界面
    if args.gui or bool(args.hotkey):
        try:
            import app as app_mod
        except ImportError:
            print("[提示] 当前构建不含图形界面，已切换到命令行模式。\n")
            return cmd_interactive(loop=loop, prepare=prepare,
                                   next_key=args.next_key)
        if args.hotkey:
            cfg_data = cfg.load()
            cfg_data["hotkey_start"] = args.hotkey
            cfg.save(cfg_data)
        app_mod.main()
        return 0

    # 什么都没给 -> 交互引导（双击就是这个分支）
    if not has_run_args:
        return cmd_interactive(loop=loop, prepare=prepare,
                               next_key=args.next_key)

    data, mode = resolve(args, base=base)
    if profile_mode and not args.mode:
        mode = profile_mode        # 方案的抖器模式优先于默认的 normal
    cfg.save(data)   # 固化本次参数，下次直接跑
    return run_headless(data, mode, loop=loop, prepare=prepare,
                        next_key=args.next_key)


if __name__ == "__main__":
    sys.exit(main())