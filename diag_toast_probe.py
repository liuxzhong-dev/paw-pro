# -*- coding: utf-8 -*-
"""
toast 实测探针：验证右下角通知弹窗到底能不能弹出来。

用法：python diag_toast_probe.py
检查项：
  ① toast 管理器内部状态（_hwnd / _tid / _pending / 线程存活）
  ② FindWindowW 能否找到弹窗类名 "WBMouseClickerToast"
  ③ 窗口是否可见、位置是否在工作区右下角
  ④ CreateWindowExW 未声明 restype 时的句柄截断对比（复现硬规则 2）
"""
import ctypes
import ctypes.wintypes as wt
import time
import sys

sys.path.insert(0, ".")

user32 = ctypes.windll.user32

print("=" * 60)
print("④ 先验：CreateWindowExW 返回值截断对比")
print("=" * 60)
# 不声明 restype（toast.py 现状）
w_bad = user32.CreateWindowExW(0, "Static", "probe", 0x80000000,
                               0, 0, 10, 10, None, None,
                               ctypes.windll.kernel32.GetModuleHandleW(None),
                               None)
print("未声明 restype  -> 0x%X (类型 %s, 高位丢失=%s)"
      % (w_bad & 0xFFFFFFFFFFFFFFFF, type(w_bad).__name__,
         w_bad > 0xFFFFFFFF))
# 声明 restype = HWND（修复后应有的样子）
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                                   wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND,
                                   wt.HMENU, wt.HINSTANCE, wt.LPVOID]
w_good = user32.CreateWindowExW(0, "Static", "probe", 0x80000000,
                                0, 0, 10, 10, None, None,
                                ctypes.windll.kernel32.GetModuleHandleW(None),
                                None)
print("声明 restype    -> 0x%X" % (w_good or 0))
if w_bad:
    user32.DestroyWindow(wt.HWND(w_bad))
if w_good:
    user32.DestroyWindow(w_good)

print()
print("=" * 60)
print("①②③ 实测 toast.show_mouse_ready('中键')")
print("=" * 60)
from core import toast

ret = toast.show_mouse_ready("中键")
print("show_mouse_ready 返回: %r" % ret)
time.sleep(1.2)

mgr = toast._MANAGER
print("管理器状态: thread=%r alive=%s hwnd=%r tid=%r pending=%r"
      % (mgr._thread, mgr._thread.is_alive() if mgr._thread else None,
         mgr._hwnd, mgr._tid, mgr._pending))
if mgr._hwnd:
    print("  _hwnd 实际值: 0x%X (超过 32 位=%s)"
          % (mgr._hwnd, mgr._hwnd > 0xFFFFFFFF))

# FindWindowW 找类名
found = user32.FindWindowW(u"WBMouseClickerToast", None)
print("FindWindowW('WBMouseClickerToast') -> 0x%X" % (found or 0))

if found:
    visible = user32.IsWindowVisible(wt.HWND(found))
    print("IsWindowVisible: %s" % bool(visible))
    rc = (wt.RECT * 1)()
    user32.GetWindowRect(wt.HWND(found), rc)
    r = rc[0]
    print("窗口矩形: left=%d top=%d right=%d bottom=%d (w=%d h=%d)"
          % (r.left, r.top, r.right, r.bottom,
             r.right - r.left, r.bottom - r.top))
    # 工作区（排除任务栏）
    wa = (wt.RECT * 1)()
    user32.SystemParametersInfoW(0x0030, 0, wa, 0)
    w0 = wa[0]
    print("工作区: right=%d bottom=%d" % (w0.right, w0.bottom))
    print("距右下角: dx=%d dy=%d"
          % (w0.right - r.right, w0.bottom - r.bottom))
else:
    print("❌ 找不到弹窗窗口 —— toast 静默失败坐实")

print()
print("=" * 60)
print("⑤ 连续 show 四条（模拟完整流程）")
print("=" * 60)
for fn, arg in [("show_mouse_ready", "中键"),
                ("show_mouse_started", 10),
                ("show_mouse_paused", "中键"),
                ("show_mouse_resumed", "中键")]:
    r = getattr(toast, fn)(arg)
    print("%-20s -> %r" % (fn, r))
    time.sleep(0.4)
time.sleep(0.6)
print("当前 _cur_text=%r _cur_kind=%r"
      % (mgr._cur_text, mgr._cur_kind))

toast.stop()
print("\n[done] 探针结束")
