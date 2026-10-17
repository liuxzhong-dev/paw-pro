# -*- coding: utf-8 -*-
"""
第四探针：把「状态位被意外打回 False，但窗口其实还全屏」这个**状态污染**
场景直接造出来 —— 也就是主人报的「点还原没反应」的成因。

A) 造污染：窗口铺满工作区 + 人为把 _norm_rect["valid"] 置 False
   -> 点按钮，期望：真的还原成小窗口（修之前是「尺寸不变、图标跳成还原」）
B) 系统最大化（Win+Up）后再点我们的「还原」
   -> 期望：窗口变小，且 IsZoomed 被清掉（修之前会一直 True，
      以后一次系统还原/激活就可能把窗口又弹回全屏）
C) 连点 5 轮，确认状态机不漂移
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
u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u.ShowWindow.restype = wt.BOOL
u.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
u.SendMessageW.restype = ctypes.c_ssize_t
WM_SYSCOMMAND = 0x0112
SC_MAXIMIZE = 0xF030


def st(tag, h):
    print("   %-16s rect=%-24s IsZoomed=%-5s valid=%-5s"
          % (tag, D._get_rect(h), bool(u.IsZoomed(h)),
             D._norm_rect["valid"]))


def main():
    import webview

    try:
        D._dpi_state = _dpi.enable_dpi_awareness()
    except Exception:
        pass
    api = D.Api()
    html_path = os.path.join(D.HERE, "ui", "dashboard.html")
    win = webview.create_window(
        "连点 / 锁定器", url=("file://" + html_path.replace("\\", "/")),
        js_api=api, width=1180, height=820, min_size=(900, 700),
        background_color="#03060D", text_select=False, frameless=True)
    api._window = win
    D._apply_frameless(win)

    def place(h):
        """回到一个干净的小窗口状态。"""
        u.ShowWindow(h, 6)
        time.sleep(0.3)
        u.ShowWindow(h, 9)
        time.sleep(0.4)
        D._set_rect(h, 78, 78, 1164, 781)
        D._norm_rect["valid"] = False
        time.sleep(0.5)

    def script():
        time.sleep(3.0)
        h = D._win_hwnd()
        print("hwnd=%s 工作区=%s\n" % (h, D._work_area(h)))

        # ---------- A. 状态污染 ----------
        print("===== A. 窗口铺满工作区 + valid 被意外打回 False =====")
        place(h)
        D._set_rect(h, 0, 0, 1920, 1032)
        D._norm_rect["valid"] = False          # 造污染
        time.sleep(0.5)
        st("污染态", h)
        print("   前端此时会显示「最大化」图标（valid=False）--> 用户点它")
        r = D.win_toggle_maximize(h)
        time.sleep(0.8)
        print("   返回 =", r)
        st("点完之后", h)
        print("   期望：rect 变成小窗口（修之前这里尺寸完全不变）")
        print("   >>> 判定:", "通过" if (D._get_rect(h)[2] < 1500)
              else "仍然失败（还是全屏）")

        # ---------- B. 系统最大化 ----------
        print("\n===== B. 系统最大化后再点我们的「还原」=====")
        place(h)
        u.SendMessageW(h, WM_SYSCOMMAND, SC_MAXIMIZE, 0)
        time.sleep(1.0)
        st("系统最大化", h)
        print("   返回 =", D.win_toggle_maximize(h))
        time.sleep(0.8)
        st("点还原后", h)
        print("   >>> IsZoomed 必须被清掉:",
              "通过" if not u.IsZoomed(h) else "失败（还带着 zoomed 状态）")

        # ---------- C. 连点稳定性 ----------
        print("\n===== C. 连点 5 轮，状态不许漂移 =====")
        place(h)
        expect_max = True
        bad = 0
        for i in range(5):
            r = D.win_toggle_maximize(h)
            time.sleep(0.5)
            rect = D._get_rect(h)
            full = rect[2] >= 1800
            ok = (r["maximized"] == expect_max) and (full == expect_max)
            st("第%d轮" % (i + 1), h)
            print("        期望 %s 实际 %s -> %s"
                  % ("全屏" if expect_max else "小窗",
                     "全屏" if full else "小窗", "OK" if ok else "!! 漂移"))
            if not ok:
                bad += 1
            expect_max = not expect_max
        print("   >>> 漂移次数 =", bad, "（应为 0）")

        try:
            win.destroy()
        except Exception:
            pass

    webview.start(script)


if __name__ == "__main__":
    main()
