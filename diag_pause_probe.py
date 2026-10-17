# -*- coding: utf-8 -*-
"""
探针：右上角「暂停 / 恢复运行」按钮的后端链路到底通不通。

主人反馈：按钮显示「恢复运行」时点它没反应、也不弹通知。
直接调 Api.toggle_pause() 逐次打印返回值和 pausectl 内部状态，
把「后端返回值」与「前端显示」对齐看差异。
"""
import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dashboard as d                                          # noqa: E402
from core import pausectl                                      # noqa: E402


def dump(tag):
    print("   %-14s _paused=%-5s zones=%s any=%s  is_paused()=%s"
          % (tag, pausectl._paused, pausectl._zones,
             pausectl.any_paused(), pausectl.is_paused()))


print("=== 初始 ===")
dump("init")

api = d.Api()
print("=== Api 已构造 ===")
dump("after ctor")

print("\n=== 第 1 次点按钮（暂停）===")
try:
    r1 = api.toggle_pause()
    print("   返回:", r1)
except Exception:
    print("   !! 抛异常:"); traceback.print_exc()
dump("after 1st")

print("\n=== 第 2 次点按钮（恢复）===")
try:
    r2 = api.toggle_pause()
    print("   返回:", r2)
except Exception:
    print("   !! 抛异常:"); traceback.print_exc()
dump("after 2nd")

print("\n=== 第 3 次（暂停）===")
try:
    print("   返回:", api.toggle_pause())
except Exception:
    print("   !! 抛异常:"); traceback.print_exc()
dump("after 3rd")

print("\n=== 前端 status() 里 paused / mouse_paused / key_paused ===")
s = api.status()
print("   paused=%s mouse_paused=%s key_paused=%s message=%r"
      % (s["paused"], s["mouse_paused"], s["key_paused"], s["message"]))

print("\n=== 场景 B：只用中键暂停了鼠标区（分区暂停），再点按钮 ===")
pausectl.set_paused(True, "mouse")
dump("mouse-zone on")
print("   前端 paused(any)=%s → 按钮文案会是「恢复运行」"
      % pausectl.any_paused())
print("   后端 is_paused()=%s  →  toggle_pause 会算出 not(它)=%s"
      % (pausectl.is_paused(), not pausectl.is_paused()))
try:
    r = api.toggle_pause()
    print("   返回:", r)
    print("   → 用户点「恢复运行」得到的是：%s"
          % ("仍在暂停（应显示已恢复）" if r.get("paused") else "已恢复"))
except Exception:
    print("   !! 抛异常:"); traceback.print_exc()
dump("after B")

print("\n=== 场景 C：用中键暂停键盘区，再点按钮 ===")
pausectl.set_paused(False)
pausectl.set_paused(True, "key")
dump("key-zone on")
try:
    r = api.toggle_pause()
    print("   返回:", r)
except Exception:
    print("   !! 抛异常:"); traceback.print_exc()
dump("after C")

try:
    from core import toast
    toast.stop()
except Exception:
    pass
