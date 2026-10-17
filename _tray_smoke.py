# -*- coding: utf-8 -*-
"""托盘自检（读错误字段）。"""
import sys, time
sys.path.insert(0, ".")
from core import tray

t = tray.Tray(tip="爪Pro · 托盘自检")
ok = t.start()
print("start ok =", ok, "| running =", t.running, "| error =", t._error or "(无)")
time.sleep(0.6)
t.stop()
print("after stop running =", t.running)
