# -*- coding: utf-8 -*-
"""二分定位：换掉 _handle 看窗口能不能建出来（临时脚本）。"""
import sys
sys.path.insert(0, ".")
import core.tray as T


def simple(h, m, w, l):
    return T._user32.DefWindowProcW(h, m, w, l)


# 情形 A：原始 _handle
t1 = T.Tray(tip="a")
t1._class_name = "TrayBisectA"
print("A(原始 _handle) =", t1.start(1.0), "|", t1._error or "(无)")
t1.stop()

# 情形 B：把 _handle 换成最朴素的 DefWindowProc
T.Tray._handle = staticmethod(simple)
t2 = T.Tray(tip="b")
t2._class_name = "TrayBisectB"
print("B(朴素 proc) =", t2.start(1.0), "|", t2._error or "(无)")
t2.stop()
