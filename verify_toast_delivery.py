# -*- coding: utf-8 -*-
"""
verify_toast_delivery.py —— 通知弹窗投递链路回归测试
=====================================================

【背景 · 2026-10-16 v13~v14 实测「没有通知」】
toast.py 用 PostThreadMessageW 投递 WM_APP_SHOW。这种 hwnd=NULL 的
**线程消息**，DispatchMessageW 不会送到任何窗口过程——消息进了队列、
GetMessageW 也取得到，但分发目标为空，_wnd_proc 永远收不到。
表现为：show() 返回 True、线程活着、窗口建好了，但弹窗就是不显示，
且全链路无日志，极难排查。

修复：改用 PostMessageW(hwnd, ...)——带 hwnd 的窗口消息，
DispatchMessageW 才能正确送到窗口过程。

【本测试防什么】
① 有人把投递方式改回 PostThreadMessageW（源码静态断言，立刻红）
② 消息投递后 _on_show 真的执行了（pending 被消费、文本就位）
③ 窗口真的可见且落在工作区右下角（TOAST_MARGIN 边距）

注意：本测试会**真的弹出** 3 秒通知（右下角、不抢焦点、点不穿），
这是有意的——离线打桩测试曾经把 toast 整个 patch 掉，
导致 v13 的静默失效一路绿灯到交付（硬规则 12/17 的同款坑）。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def main():
    print("=" * 62)
    print("verify_toast_delivery：通知弹窗投递链路回归")
    print("=" * 62)

    # ---------- ① 静态断言：源码不得再用 PostThreadMessageW ----------
    print("\n[1] 静态断言：投递方式")
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "core", "toast.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    check("core/toast.py 无 PostThreadMessageW 调用",
          "PostThreadMessageW(" not in src,
          "发现残留调用：线程消息送不到窗口过程，弹窗会静默失效")
    check("core/toast.py 使用 PostMessageW(hwnd)",
          "PostMessageW(" in src)

    from core import toast

    # ---------- ② 动态：show 后消息必须被消费 ----------
    print("\n[2] 动态：show_mouse_ready -> 消息消费 + 可见性")
    toast.stop()
    time.sleep(0.3)
    r = toast.show_mouse_ready("中键")
    check("show_mouse_ready 返回 True", r is True, "ret=%r" % r)
    time.sleep(1.0)

    mgr = toast._MANAGER
    check("pending 已被消费（_on_show 执行过）",
          not mgr._pending, "pending=%r" % (mgr._pending,))
    check("_cur_text 已更新为就绪文案",
          "已就绪" in (mgr._cur_text or ""),
          "_cur_text=%r" % mgr._cur_text)
    check("弹窗窗口已建成", bool(mgr._hwnd), "hwnd=%r" % mgr._hwnd)

    user32 = ctypes.windll.user32
    if mgr._hwnd:
        vis = user32.IsWindowVisible(wt.HWND(mgr._hwnd))
        check("窗口可见（IsWindowVisible）", bool(vis))

        rc = (wt.RECT * 1)()
        user32.GetWindowRect(wt.HWND(mgr._hwnd), rc)
        wa = (wt.RECT * 1)()
        user32.SystemParametersInfoW(0x0030, 0, wa, 0)
        dx = wa[0].right - rc[0].right
        dy = wa[0].bottom - rc[0].bottom
        check("窗口贴工作区右下角（dx/dy = TOAST_MARGIN=24）",
              abs(dx - 24) <= 2 and abs(dy - 24) <= 2,
              "dx=%d dy=%d" % (dx, dy))

    # ---------- ③ 动态：完整状态链（文案与行为匹配） ----------
    print("\n[3] 动态：就绪->开始->暂停->继续 状态链")
    cases = [
        ("show_mouse_started", (10, "中键"), "已开始"),
        ("show_mouse_paused", ("中键",), "已暂停"),
        ("show_mouse_resumed", ("中键",), "已继续"),
        ("show_mouse_stopped", (), "已停止"),
    ]
    for fn, args, expect in cases:
        getattr(toast, fn)(*args)
        time.sleep(0.5)
        got = mgr._cur_text or ""
        check("%s 文案含「%s」" % (fn, expect), expect in got,
              "_cur_text=%r" % got)

    print("\n[4] 启动通知带真实触发键名（不是写死的「触发键」）")
    toast.show_mouse_started(12, "中键")
    time.sleep(0.5)
    check("show_mouse_started(12,'中键') 文案含「中键」",
          "中键" in (mgr._cur_text or ""),
          "_cur_text=%r" % mgr._cur_text)

    toast.stop()
    time.sleep(0.3)

    print("\n" + "=" * 62)
    print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
        return 1
    print("[done] 通知弹窗投递链路全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
