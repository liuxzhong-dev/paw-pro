# -*- coding: utf-8 -*-
"""
复现「最大化以后再点还原，窗口没变化」的探针。

做法：开一个和 exe 一样的 pywebview frameless 窗口，用 evaluate_js 调
前端真正的 winMax()（= 点右上角按钮），跑「最大化 -> 还原」两轮，
每轮打印窗口矩形 + _norm_rect["valid"]。

同时给 WM_WINDOWPOSCHANGED / WM_SIZE / _set_rect 打点：
状态机是被这些消息喂出来的，谁把 valid 改掉了必须看得见。

用 envs/vsum 跑（唯一有 pywebview 的解释器）：
    python envs/vsum/Scripts/python.exe diag_max_probe.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dashboard as D                                          # noqa: E402

LOG = []


def _fmt(h):
    r = D._get_rect(h) if h else None
    return "rect=%s valid=%s free=%s setting=%s" % (
        r, D._norm_rect["valid"],
        (D._last_free_rect["x"], D._last_free_rect["y"],
         D._last_free_rect["w"], D._last_free_rect["h"],
         D._last_free_rect["ok"]),
        D._setting_rect["flag"])


# ---- 给两个消息处理打点（_frameless_msg 是运行时查模块全局，能被替换） ----
_orig_pos = D._on_window_pos_changed
_orig_size = D._on_window_size


def _pos(hwnd):
    before = D._norm_rect["valid"]
    _orig_pos(hwnd)
    after = D._norm_rect["valid"]
    LOG.append("  WM_WINDOWPOSCHANGED  %s   valid %s->%s"
               % (_fmt(hwnd), before, after))


def _size(hwnd, wparam):
    before = D._norm_rect["valid"]
    _orig_size(hwnd, wparam)
    after = D._norm_rect["valid"]
    LOG.append("  WM_SIZE(wparam=%s)   %s   valid %s->%s"
               % (wparam, _fmt(hwnd), before, after))


D._on_window_pos_changed = _pos
D._on_window_size = _size


def dump(tag):
    h = D._win_hwnd()
    print("[%s] %s" % (tag, _fmt(h)))
    wa = D._work_area(h) if h else None
    print("        工作区=%s" % (wa,))
    for line in LOG:
        print(line)
    del LOG[:]


def main():
    import webview

    api = D.Api()
    html_path = os.path.join(D.HERE, "ui", "dashboard.html")
    win = webview.create_window(
        "连点 / 锁定器",
        url=("file://" + html_path.replace("\\", "/")),
        js_api=api,
        width=1180, height=820,
        min_size=(900, 700),
        background_color="#03060D",
        text_select=False,
        frameless=True,
    )
    api._window = win
    D._apply_frameless(win)

    def script():
        time.sleep(3.0)
        h = D._win_hwnd()
        print("=== 窗口就绪 hwnd=%s ===" % h)
        dump("初始")

        print("\n=== 第 1 次点右上角按钮（期望：最大化）===")
        win.evaluate_js("winMax()")
        time.sleep(1.2)
        dump("点1后")
        print("  后端说 is_maximized =", D._win_is_maximized())

        print("\n=== 第 2 次点同一个按钮（期望：还原）===")
        win.evaluate_js("winMax()")
        time.sleep(1.2)
        dump("点2后")
        print("  后端说 is_maximized =", D._win_is_maximized())

        print("\n=== 第 3 次（期望：再最大化）===")
        win.evaluate_js("winMax()")
        time.sleep(1.2)
        dump("点3后")

        print("\n=== 第 4 次（期望：再还原）===")
        win.evaluate_js("winMax()")
        time.sleep(1.2)
        dump("点4后")

        try:
            win.destroy()
        except Exception:
            pass

    try:
        webview.start(script)
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
