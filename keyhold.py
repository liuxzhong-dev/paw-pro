# -*- coding: utf-8 -*-
"""
keyhold.py —— 长按锁定 独立入口（命令行版）

不依赖 tkinter，双击 exe 即可运行，用于快速验证「长按 X 键 N 秒后一直按着，
再次按 X 键解除」这个功能。

用法示例：
  鼠标连点器-长按锁定.exe --keys w --threshold 2
  鼠标连点器-长按锁定.exe --keys w,a --threshold 1.5
  鼠标连点器-长按锁定.exe --keys space --threshold 3 --hz 10

说明：
  只有 --keys 里列出的键才有长按锁定效果，没列出的键（如 A）完全不受影响。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import keyboard as kb


def _fix_console_encoding():
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
            # line_buffering: 打包成 exe 双击运行时，
            # 不加这个输出会卡在缓冲区里，界面上一个字都看不到
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except Exception:
            pass


def main():
    _fix_console_encoding()
    # DPI 感知：固定坐标类功能不走偏；失败不影响运行
    try:
        from core import dpi
        dpi.enable_dpi_awareness()
    except Exception:
        pass

    p = argparse.ArgumentParser(
        prog="长按锁定",
        description="长按指定键超过阈值秒数后进入锁定（一直按着），再次按该键解除",
    )
    p.add_argument("--keys", default="w",
                   help="要生效的键，多个用逗号分隔，如 w 或 w,a,space（默认 w）")
    p.add_argument("--threshold", type=float, default=2.0,
                   help="长按多少秒后进入锁定，默认 2 秒")
    p.add_argument("--hz", type=int, default=30,
                   help="锁定后重复注入的频率（次/秒），默认 30。"
                        "这就是「刷字有多快」：调到 8 会一顿一顿的，"
                        "50~60 更快但更吃 CPU")
    p.add_argument("--panic", default="f12",
                   help="紧急释放键，默认 F12。任何时候按下都会强制解除所有锁定")
    p.add_argument("--vk", action="store_true",
                   help="改用虚拟键码注入（默认是扫描码注入）。"
                        "少数程序只认其中一种，如果屏幕没反应就换另一种试："
                        "扫描码没反应加 --vk，虚拟键码没反应就去掉 --vk")
    p.add_argument("--click-mode", action="store_true",
                   help="改成「反复点击」模式：锁定期间不断注入"
                        "「按下+抬起」配对（默认是「按住」模式）。"
                        "默认模式只注入一次按下、不注入抬起，"
                        "游戏里角色会持续移动；"
                        "加这个参数会变成连续点击多次，"
                        "适合需要每次都产生完整按键动作的场景")
    p.add_argument("--list-keys", action="store_true",
                   help="列出所有可用的键名后退出")
    args = p.parse_args()

    if args.list_keys:
        print("可用的键名（大小写均可，多个用逗号分隔）：")
        print("  字母 : %s" % " ".join(chr(c) for c in range(ord("A"), ord("Z") + 1)))
        print("  数字 : %s" % " ".join(str(i) for i in range(10)))
        print("  功能 : %s" % " ".join("F%d" % i for i in range(1, 25)))
        print("  特殊 : Space Enter Esc Tab Backspace Shift Ctrl Alt Up Down Left Right"
              " Delete Insert CapsLock NumLock")
        return 0

    vks = kb.parse_keys(args.keys)
    if not vks:
        print("[错误] 没有识别到有效按键：%r" % args.keys)
        print("提示：用 --list-keys 查看所有支持的键名")
        return 1

    print("=" * 56)
    print(" 长按锁定已启动")
    print("=" * 56)
    print(" 生效的键   : %s" % ", ".join(kb.vk_to_name(v) for v in vks))
    print(" 锁定阈值   : 长按 %.1f 秒" % args.threshold)
    print(" 注入频率   : %d 次/秒" % args.hz)
    print(" 注入模式   : %s" % ("虚拟键码（--vk）" if args.vk else "扫描码（默认）"))
    print(" 锁定行为   : %s" % (
        "反复点击（--click-mode：按下+抬起 配对注入）" if args.click_mode
        else "按住（默认：只注入一次按下，不注入抬起）"))
    print(" 不生效的键 : 其他所有按键均不受影响")
    print("-" * 56)

    events = []
    last_stat = None      # 供 on_change 闭包重置用（避免重复刷同一行统计）

    def on_change(locked, name):
        events.append((time.time(), locked, name))
        mark = time.strftime("%H:%M:%S")
        if locked:
            kb.reset_inject_stat()   # 每次锁定单独统计，方便看这一次的效果
            last_stat = None
            print("[%s] 已锁定：%s 现在处于持续按下状态（物理松开无效）" % (mark, name))
        else:
            st = kb.inject_stat()
            print("[%s] 已解除：%s 恢复正常（本次锁定共注入 %d 次，成功 %d，失败 %d）"
                  % (mark, name, st["total"], st["ok"], st["fail"]))
            last_stat = None

    lock = kb.KeyHoldLock(keys=args.keys, threshold_sec=args.threshold,
                          repeat_hz=args.hz, on_state_change=on_change,
                          panic_key=args.panic, use_scancode=not args.vk,
                          hold_down=not args.click_mode)
    ok, err = lock.start()
    if not ok:
        print("[启动失败] %s" % err)
        return 2

    print(" 紧急释放 : %s 键（任何时候按下都强制解除全部锁定）" % lock.panic_name)
    print("-" * 56)

    # ---- 权限预检：直接指出「注入为什么没反应」的最常见原因----
    # 实测教训：SendInput 返回成功 ≠ 事件被系统接收。
    # 低权限进程向高权限窗口注入输入会被 UIPI **静默丢弃**，
    # 而 API 照样返回成功 —— 表现为「状态显示已锁定，但目标程序毫无反应」。
    # 在这里主动检查并提示，比让用户反复试错快得多。
    try:
        from diag_privilege import integrity, rid_value
        my_rid = rid_value()
        if my_rid is not None and my_rid < 0x3000:
            higher = []
            try:
                import diag_target as _dt
                for w in _dt.list_windows():
                    r = rid_value(w.pid)
                    if r is not None and r > my_rid:
                        higher.append((w.title[:24], w.pid, r))
            except Exception:
                pass
            if higher:
                print(" ⚠ 权限提示：本工具是「普通用户」权限运行，但检测到这些")
                print("   程序是「管理员」权限，向它们注入输入会被 Windows 静默丢弃：")
                for t, pid, r in higher[:5]:
                    print("     - %s (pid=%d, RID=0x%04X)" % (t, pid, r))
                print("   解决：右键本 exe →「以管理员身份运行」后再试。")
                print("   （注入统计仍会显示成功，但目标程序不会收到——这是权限问题，")
                print("     不是程序故障。）")
                print("-" * 56)
    except Exception:
        pass

    print(" 操作方式   :")
    print("   1. 按住 %s 键 %.1f 秒以上 -> 进入锁定，一直按着"
          % (kb.vk_to_name(vks[0]), args.threshold))
    print("   2. 物理松开不影响，键依然处于按下状态")
    print("   3. 再次按 %s 键 -> 解除锁定，恢复正常" % kb.vk_to_name(vks[0]))
    print("   4. 按 %s 键 -> 紧急释放全部锁定（键盘卡住时用它）" % lock.panic_name)
    print("   5. 按 Ctrl+C 或关闭窗口退出")
    print()
    print(" 当前行为：%s" % (
        "反复点击模式（--click-mode）" if args.click_mode
        else "按住模式 —— 游戏里角色会持续移动，不是连续点击"))
    print(" 若发现表现成「反复点击」而不是「一直按着」，"
          "说明拿到的是旧版本，请到交付目录取最新exe。")
    print("-" * 56)
    print("[就绪] 等待按键…\n")

    last = None
    last_stat = None
    try:
        while True:
            is_locked, name = lock.active
            # 只在状态变化时刷新首行提示
            states = [lock.state_of(v) for v in vks]
            cur = (is_locked, name, tuple(states))
            if cur != last:
                parts = ["%s=%s" % (kb.vk_to_name(v), s) for v, s in zip(vks, states)]
                print("当前状态：%s" % "  ".join(parts))
                last = cur
            # 锁定期间每秒报一次注入统计：这能直接区分
            # 「API 调了但没发出去」和「发出去了但应用没响应」
            if is_locked:
                st = kb.inject_stat()
                if st != last_stat:
                    print("  注入统计：共 %d 次，成功 %d，失败 %d"
                          % (st["total"], st["ok"], st["fail"]))
                    last_stat = st
            else:
                last_stat = None
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        lock.stop()
        st = kb.inject_stat()
        print("本次运行共注入 %d 次（成功 %d / 失败 %d）。"
              % (st["total"], st["ok"], st["fail"]))
        if st["total"] > 0 and st["ok"] == 0:
            print("警告：所有注入都失败了。若目标程序（记事本/游戏）是")
            print("      以管理员身份运行，本程序也需要以管理员身份运行，")
            print("      否则 Windows 会静默丢弃输入（UIPI 机制）。")
        print("已退出，键盘状态已恢复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())