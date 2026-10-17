# -*- coding: utf-8 -*-
"""
第三次复现：这次尽量照 dashboard.main() 的真实环境起（DPI 感知、原始输入
监听、热键注册、监控线程），并且把窗口过程收到的**每一条**消息打点。

前两个探针里「我们的最大化 -> 还原」是好的，说明真机上还有别的消息
在事后把 _norm_rect["valid"] 改掉 —— 只要日志里有这条，就能抓到它。
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes as wt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dashboard as D                                          # noqa: E402
from core import dpi as _dpi                                   # noqa: E402

u = ctypes.windll.user32
u.IsZoomed.argtypes = [wt.HWND]
u.IsZoomed.restype = wt.BOOL

# ---- 我们关心的消息名 ----
MSG_NAME = {
    0x0005: "WM_SIZE", 0x0046: "WM_WINDOWPOSCHANGING",
    0x0047: "WM_WINDOWPOSCHANGED", 0x0083: "WM_NCCALCSIZE",
    0x0006: "WM_ACTIVATE", 0x0007: "WM_SETFOCUS", 0x0086: "WM_NCACTIVATE",
    0x0112: "WM_SYSCOMMAND", 0x0113: "WM_TIMER", 0x001A: "WM_SETTINGCHANGE",
    0x007E: "WM_DISPLAYCHANGE", 0x02E0: "WM_DPICHANGED",
    0x0018: "WM_SHOWWINDOW", 0x0024: "WM_GETMINMAXINFO",
}

TRACE = []
_orig_msg = D._frameless_msg


def _msg(hwnd, msg, wparam, lparam, old_proc):
    if msg in MSG_NAME:
        r = D._get_rect(hwnd)
        TRACE.append("    msg %-20s wparam=%-6s rect=%-22s valid=%s"
                     % (MSG_NAME[msg], wparam, r, D._norm_rect["valid"]))
    return _orig_msg(hwnd, msg, wparam, lparam, old_proc)


D._frameless_msg = _msg


def dump(tag, h):
    print("[%s] rect=%s IsZoomed=%s valid=%s free=%s"
          % (tag, D._get_rect(h), bool(u.IsZoomed(h)),
             D._norm_rect["valid"],
             (D._last_free_rect["x"], D._last_free_rect["y"],
              D._last_free_rect["w"], D._last_free_rect["h"],
              D._last_free_rect["ok"])))
    for line in TRACE:
        print(line)
    del TRACE[:]


def main():
    import webview

    # ---- 和 main() 一样：建窗口之前先设 DPI 感知 ----
    try:
        D._dpi_state = _dpi.enable_dpi_awareness()
    except Exception as e:
        print("DPI 设置失败", e)
    print("DPI 感知 =", getattr(D, "_dpi_state", "?"))

    api = D.Api()
    try:
        D.start_mouse_listener()
        print("原始输入监听已启动")
    except Exception as e:
        print("原始输入监听失败", e)
    try:
        hkmod = D.hk
        hkmod.set_error_logger(D._log_exc)
        api.register_hotkeys()
        print("热键已注册")
    except Exception as e:
        print("热键注册失败", e)

    html_path = os.path.join(D.HERE, "ui", "dashboard.html")
    win = webview.create_window(
        "连点 / 锁定器", url=("file://" + html_path.replace("\\", "/")),
        js_api=api, width=1180, height=820, min_size=(900, 700),
        background_color="#03060D", text_select=False, frameless=True)
    api._window = win
    D._apply_frameless(win)

    def script():
        time.sleep(3.5)
        h = D._win_hwnd()
        print("\n===== 环境就绪 hwnd=%s =====" % h)
        dump("初始", h)

        print("\n===== 点「最大化」（前端 winMax 同路径）=====")
        win.evaluate_js("winMax()")
        time.sleep(3.0)                 # 给「事后扰动」留足时间
        dump("等 3 秒后", h)
        print("  后端 is_maximized =", D._win_is_maximized())

        print("\n===== 点「还原」=====")
        win.evaluate_js("winMax()")
        time.sleep(2.5)
        dump("点还原后", h)
        print("  后端 is_maximized =", D._win_is_maximized())

        try:
            win.destroy()
        except Exception:
            pass

    webview.start(script)


if __name__ == "__main__":
    main()
