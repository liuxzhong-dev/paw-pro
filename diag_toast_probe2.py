# -*- coding: utf-8 -*-
"""
toast 探针 2：定位 WM_APP_SHOW 为什么没到窗口过程。

手法（硬规则 8：faulthandler 到点泄线程栈）：
  ① show 一次 → 2 秒后 dump 所有线程栈，看 toast 线程卡在哪
  ② GetQueueStatus 看 toast 线程队列里有没有消息
  ③ 手工 PostThreadMessageW 补投一条，看 pending 是否被消费
  ④ 直接调 _on_show()（绕过消息循环）验证显示链路本身好不好使
  ⑤ 检查 PostThreadMessageW 返回值
"""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import sys
import time

sys.path.insert(0, ".")

user32 = ctypes.windll.user32
user32.PostThreadMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]

from core import toast

faulthandler.dump_traceback_later(3, exit=False)

print("[1] show_mouse_ready ...")
r1 = toast.show_mouse_ready("中键")
print("    ret=%r" % r1)
time.sleep(2.5)

mgr = toast._MANAGER
print("[2] 队列状态: GetQueueStatus(QS_ALLPOSTMESSAGE=0xFF00) = 0x%X"
      % (user32.GetQueueStatus(0xFF00) or 0))
print("    toast线程 alive=%s hwnd=0x%X tid=%r pending=%r"
      % (mgr._thread.is_alive(), mgr._hwnd or 0, mgr._tid, mgr._pending))
print("    _cur_text=%r _cur_kind=%r" % (mgr._cur_text, mgr._cur_kind))

print("[3] 手工补投 WM_APP_SHOW(0x%X) 到 tid=%r ..."
      % (0x8000 + 1, mgr._tid))
ok = user32.PostThreadMessageW(mgr._tid, 0x8000 + 1, 0, 0)
print("    PostThreadMessageW ret=%r GetLastError=%r"
      % (ok, ctypes.get_last_error()))
time.sleep(1.0)
print("    补投后: pending=%r _cur_text=%r"
      % (mgr._pending, mgr._cur_text))

print("[4] 直接调 _on_show() 绕过消息循环 ...")
try:
    mgr._on_show()
    print("    _on_show 返回（无异常）")
except Exception as e:
    print("    _on_show 抛异常: %r" % e)
time.sleep(0.5)
print("    pending=%r _cur_text=%r" % (mgr._pending, mgr._cur_text))
vis = user32.IsWindowVisible(wt.HWND(mgr._hwnd)) if mgr._hwnd else None
print("    IsWindowVisible=%r" % vis)
if mgr._hwnd:
    rc = (wt.RECT * 1)()
    user32.GetWindowRect(wt.HWND(mgr._hwnd), rc)
    print("    矩形: %r" % (rc[0],))

print("[5] _wndproc 引用: %r" % mgr._wndproc)
print("    RegisterClass 类是否存在: FindAtomW/GetClassInfo 跳过")
print("[done]")
