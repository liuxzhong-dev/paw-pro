# -*- coding: utf-8 -*-
"""打印 _handle 收到的每条消息，定位哪条炸掉（临时脚本）。"""
import sys
sys.path.insert(0, ".")
import core.tray as T

orig = T.Tray._handle


def spy(self, h, m, w, l):
    sys.stderr.write("MSG %#06x hwnd=%r\n" % (m, h))
    sys.stderr.flush()
    return orig(self, h, m, w, l)


T.Tray._handle = spy
t = T.Tray(tip="spy")
t._class_name = "TraySpy"
print("start =", t.start(1.0), "|", t._error or "(无)")
t.stop()
