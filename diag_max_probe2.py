# -*- coding: utf-8 -*-
"""
续查 diag_max_probe：「我们的按钮最大化 -> 还原」在干净环境下是好的，
说明真机走的是**另外的路径**。这里测两种真实用户会做的操作：

  A) 系统最大化（Win+Up / 拖到屏幕顶部 / 标题栏双击）
     再点我们的「还原」按钮 —— 窗口带 WS_MAXIMIZE，我们的
     SetWindowPos 可能压根不生效
  B) 标题栏双击（HTCAPTION 双击 -> DefWindowProc 默认 SC_MAXIMIZE）

每个场景都打印：窗口矩形、IsZoomed、_norm_rect.valid、我们的按钮返回值。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes
import ctypes.wintypes as wt
import dashboard as D                                          # noqa: E402

u = ctypes.windll.user32
u.IsZoomed.argtypes = [wt.HWND]
u.IsZoomed.restype = wt.BOOL
u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u.ShowWindow.restype = wt.BOOL
u.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
u.SendMessageW.restype = ctypes.c_ssize_t
WM_SYSCOMMAND = 0x0112
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120


def state(tag, h):
    print("%-22s rect=%-22s IsZoomed=%-5s valid=%-5s"
          % (tag, D._get_rect(h), bool(u.IsZoomed(h)),
             D._norm_rect["valid"]))


def main():
    import webview

    api = D.Api()
    html_path = os.path.join(D.HERE, "ui", "dashboard.html")
    win = webview.create_window(
        "连点 / 锁定器", url=("file://" + html_path.replace("\\", "/")),
        js_api=api, width=1180, height=820, min_size=(900, 700),
        background_color="#03060D", text_select=False, frameless=True)
    api._window = win
    D._apply_frameless(win)

    def script():
        time.sleep(3.0)
        h = D._win_hwnd()
        print("hwnd =", h, " 工作区 =", D._work_area(h))
        state("初始", h)

        # ---------------- 场景 A：系统最大化 ----------------
        print("\n===== A. 系统最大化（等价 Win+Up）=====")
        u.SendMessageW(h, WM_SYSCOMMAND, SC_MAXIMIZE, 0)
        time.sleep(1.0)
        state("系统最大化后", h)
        print("  后端 is_maximized =", D._win_is_maximized())
        print("--> 现在点我们的「还原」按钮")
        r = D.win_toggle_maximize(h)
        time.sleep(1.0)
        print("  返回值 =", r)
        state("点还原后", h)
        print("  后端 is_maximized =", D._win_is_maximized())

        # 回干净状态
        u.ShowWindow(h, 6)   # SW_MINIMIZE
        time.sleep(0.4)
        u.ShowWindow(h, 9)   # SW_RESTORE
        time.sleep(0.6)
        D._norm_rect["valid"] = False
        D._set_rect(h, 78, 78, 1164, 781)
        time.sleep(0.6)
        state("复位后", h)

        # ---------------- 场景 B：我们的按钮 -> 系统还原 ----------------
        print("\n===== B. 我们的按钮最大化 -> 系统还原 -> 再点我们的按钮 =====")
        print("--> 点我们的「最大化」")
        print("  返回值 =", D.win_toggle_maximize(h))
        time.sleep(0.8)
        state("我们的最大化后", h)
        print("--> 用 Win+Down 等价物（SC_RESTORE）还原")
        u.SendMessageW(h, WM_SYSCOMMAND, SC_RESTORE, 0)
        time.sleep(0.8)
        state("系统还原后", h)
        print("  后端 is_maximized =", D._win_is_maximized())
        print("--> 再点我们的按钮")
        print("  返回值 =", D.win_toggle_maximize(h))
        time.sleep(0.8)
        state("再点后", h)

        # ---------------- 场景 C：标题栏双击 ----------------
        print("\n===== C. 标题栏双击（DefWindowProc HTCAPTION 双击）=====")
        u.ShowWindow(h, 6)
        time.sleep(0.3)
        u.ShowWindow(h, 9)
        time.sleep(0.5)
        D._norm_rect["valid"] = False
        D._set_rect(h, 78, 78, 1164, 781)
        time.sleep(0.6)
        u.SendMessageW(h, 0x00A3, 2, 0)     # WM_NCLBUTTONDBLCLK, HTCAPTION=2
        time.sleep(1.0)
        state("标题栏双击后", h)
        print("  后端 is_maximized =", D._win_is_maximized())
        print("--> 点我们的「还原」")
        print("  返回值 =", D.win_toggle_maximize(h))
        time.sleep(0.8)
        state("点还原后", h)

        try:
            win.destroy()
        except Exception:
            pass

    webview.start(script)


if __name__ == "__main__":
    main()
