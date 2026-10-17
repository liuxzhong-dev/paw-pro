# -*- coding: utf-8 -*-
"""托盘调试：把 _run 里的异常打出来（临时脚本）。"""
import sys, traceback
sys.path.insert(0, ".")
from core import tray

t = tray.Tray(tip="debug")
try:
    t._run()
except Exception:
    traceback.print_exc()
print("hwnd =", t._hwnd, "ok =", t._ok, "ready =", t._ready.is_set())
