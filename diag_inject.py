# -*- coding: utf-8 -*-
"""
注入诊断工具 —— 用来定位「注入成功但应用没反应」到底卡在哪
=================================================================

用法（在记事本窗口保持焦点时运行）：
    python diag_inject.py

它会做 4 组测试，每组结果都会打印，并问你「记事本里看到效果了吗」：

  测试 1  裸SendInput（不装钩子）—— 验证注入本身能不能到应用
  测试 2  只注入 keydown 5 次 —— 验证「只发按下不发抬起」应用是否会重复响应
  测试 3  30Hz 持续注入 3 秒 —— 验证频率是否够
  测试 4  不带 KEYEVENTF_SCANCODE 注入 —— 验证扫描码模式是否被目标程序忽略

每组都要你看记事本，不能只看屏幕输出。
因为「API 返回成功」不等于「应用收到了」——
这正是我们前面踩的坑。
"""
import ctypes
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctypes import wintypes as wt  # noqa: E402
import core.keyboard as kb           # noqa: E402


def ask(question):
    try:
        ans = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return ""
    return ans


def inject(vk, up=False, use_scancode=True, mark=True):
    """底层注入，可选是否用扫描码模式。返回 (是否成功, GetLastError)。"""
    if use_scancode:
        flags = kb.KEYEVENTF_SCANCODE
        scan = kb._mapvk(vk, 0)
    else:
        # 不带 SCANCODE 时wVk 生效、wScan 必须为 0
        flags = 0
        scan = 0
    if up:
        flags |= kb.KEYEVENTF_KEYUP

    extra = kb.INJECT_MARK if mark else 0
    ev = kb._FULL_INPUT(type=kb.INPUT_KEYBOARD)
    ev.ki = kb.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                          dwExtraInfo=kb.ULONG_PTR(extra))
    ctypes.set_last_error(0)
    n = kb._user32.SendInput(1, ctypes.byref(ev), kb._CB_SIZE)
    return n > 0, ctypes.get_last_error()


def main():
    vk = kb.key_to_vk("w")
    print("=" * 66)
    print("键盘注入诊断工具")
    print("=" * 66)
    print("vk=0x%Xscan=0x%X  cbSize=%d" % (
        vk, kb._mapvk(vk, 0), kb._CB_SIZE))
    print()
    print("请先打开记事本，把光标放在空白处，然后按回车继续...")
    ask("")

    # ---------- 测试 1：裸注入 ----------
    print()
    print("-" * 66)
    print("测试 1：注入一次完整的「按下+抬起」")
    print("  （不装钩子，纯SendInput）")
    print("-" * 66)
    ok1, e1 = inject(vk, up=False)
    time.sleep(0.12)
    ok2, e2 = inject(vk, up=True)
    print("  keydown:成功=%s err=%d" % (ok1, e1))
    print("  keyup  : 成功=%s err=%d" % (ok2, e2))
    print()
    print("  >>> 现在看记事本：屏幕上应该出现了 1 个 w 字符")
    r = ask("  看到了吗？(y=有 / n=没有 /回车跳过) ")
    if r == "n":
        print()
        print("  [诊断结论] 连单次注入都收不到，问题不在锁定逻辑，而在更底层。")
        print()
        print("  请依次排查下面三项，然后重试：")
        print("   a) 记事本是不是以【管理员身份】运行的？")
        print("      Windows 的 UIPI 会静默丢弃低权限进程注入的输入，")
        print("      而 SendInput 仍然返回 1，看不出任何错误。")
        print("      -> 请务必用【普通权限】的记事本，并把本工具也以管理员运行试对比")
        print()
        print("   b) 这个窗口是不是【安全桌面】？")
        print("      按 Win+L 锁屏、UAC 弹窗、Ctrl+Alt+Del 界面都不接收注入。")
        print("      ->请回到普通桌面再试")
        print()
        print("   c) 有没有装安全软件/输入法在拦键盘钩子？")
        print("      某些国产安全软件会 hook 键盘事件并丢弃注入。")
        print("      -> 先暂时退出第三方安全软件再试")
        print()
        print("  如果 a/b/c 都不是，请把本程序的完整输出发给我。")
        return

    # ---------- 测试 2：只发 keydown ----------
    print()
    print("-" * 66)
    print("测试 2：只注入 5 次 keydown，不发 keyup")
    print("  （模拟「一直按着」，应用应重复响应 5 次）")
    print("-" * 66)
    for i in range(5):
        ok, err = inject(vk, up=False)
        print("  第%d 次 keydown: 成功=%s err=%d" % (i + 1, ok, err))
        time.sleep(0.08)
    time.sleep(0.2)
    # 收尾：把 keyup 补上，别让记事本一直卡在按下
    inject(vk, up=True)
    print()
    print("  >>> 记事本里应该出现一串 w（约 5 个）")
    r = ask("  看到了吗？(y=有 / n=没有) ")
    t2 = r == "y"

    # ---------- 测试 3：30Hz 持续 3 秒 ----------
    print()
    print("-" * 66)
    print("测试 3：30Hz 持续注入 3 秒")
    print("-" * 66)
    cnt = 0
    fail = 0
    t0 = time.time()
    while time.time() - t0 < 3.0:
        ok, _ = inject(vk, up=False)
        cnt += 1
        if not ok:
            fail += 1
        time.sleep(1.0 / 30.0)
    inject(vk, up=True)
    print("  共注入 %d 次，失败 %d 次" % (cnt, fail))
    print()
    print("  >>> 记事本里应该是一大片 w 持续刷出来")
    r = ask("  看到了吗？(y=有 / n=没有) ")
    t3 = r == "y"

    # ---------- 测试 4：不带扫描码 ----------
    print()
    print("-" * 66)
    print("测试 4：不带 KEYEVENTF_SCANCODE 注入（只用虚拟键码）")
    print("-" * 66)
    ok, err = inject(vk, up=False, use_scancode=False)
    time.sleep(0.12)
    inject(vk, up=True, use_scancode=False)
    print("  keydown: 成功=%s err=%d" % (ok, err))
    print()
    print("  >>> 记事本里应该出现 1 个 w")
    r = ask("  看到了吗？(y=有 / n=没有) ")
    t4 = r == "y"

    # ---------- 汇总 ----------
    print()
    print("=" * 66)
    print("诊断汇总")
    print("=" * 66)
    print("  测试 1  单次按下+抬起      : %s" % ("通过" if r != "n" else "失败"))
    print("  测试 2  只发 keydown×5     : %s" % ("通过" if t2 else "失败"))
    print("  测试 3  30Hz 持续 3 秒    : %s" % ("通过" if t3 else "失败"))
    print("  测试 4  不用扫描码        : %s" % ("通过" if t4 else "失败"))
    print()
    if r == "n":
        print("  结论：注入完全没到达应用 -> 查上面 a/b/c 三项")
    elif t2:
        print("  结论：注入链路完全正常。")
        print("        如果长按锁定 exe 还是不刷，问题在锁定程序自身，")
        print("        请把长按锁定 exe 的完整输出发给我。")
    elif t3:
        print("  结论：测试 2 失败但测试 3 成功 -> 应用需要更高频率才响应。")
        print("        请把长按锁定 exe 的 --hz 调到 50 再试。")
    elif t4:
        print("  结论：扫描码模式被目标程序忽略 -> 需要改用虚拟键码模式。")
        print("        请把这个结论告诉我，我改 send_key。")
    else:
        print("  结论：全部失败 -> 环境层面拦截注入，请查 UIPI / 安全软件。")


if __name__ == "__main__":
    main()