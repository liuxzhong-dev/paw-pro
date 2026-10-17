# -*- coding: utf-8 -*-
"""
toast 探针 3：三条投递路径对比，定位消息在哪一段丢的。
  A. SendMessageW(hwnd, WM_APP_SHOW)      —— 直达窗口过程
  B. PostMessageW(hwnd, WM_APP_SHOW)      —— 带 hwnd 进线程队列
  C. PostThreadMessageW(tid, WM_APP_SHOW) —— 纯线程消息（toast 现状）
每次投递前重置 pending（塞一条新文本），投递后 0.8s 看 _cur_text 是否更新。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, ".")

user32 = ctypes.windll.user32
WM_APP_SHOW = 0x8001

from core import toast

# 先启动 toast 线程并建好窗口
toast.show_mouse_ready("中键")
time.sleep(1.0)
mgr = toast._MANAGER
hwnd, tid = mgr._hwnd, mgr._tid
print("hwnd=0x%X tid=%r alive=%s\n" % (hwnd or 0, tid,
                                        mgr._thread.is_alive()))


def reset_pending(tag):
    with mgr._lock:
        mgr._pending.append(("%s\n第二行" % tag, "start", 3.2))


def check(tag):
    time.sleep(0.8)
    ok = (mgr._cur_text == "%s\n第二行" % tag)
    print("    -> _cur_text=%r pending=%d %s"
          % (mgr._cur_text, len(mgr._pending),
             "✅ 收到" if ok else "❌ 未收到"))
    # 清掉 _cur_text 便于下一轮判断
    mgr._cur_text = ""
    return ok


print("[A] SendMessageW(hwnd) 直达窗口过程")
reset_pending("A-直达窗口")
r = user32.SendMessageW(wt.HWND(hwnd), WM_APP_SHOW, 0, 0)
print("    SendMessageW ret=%r" % r)
check("A-直达窗口")

print("[B] PostMessageW(hwnd) 带 hwnd 进队列")
reset_pending("B-带hwnd")
r = user32.PostMessageW(wt.HWND(hwnd), WM_APP_SHOW, 0, 0)
print("    PostMessageW ret=%r" % r)
check("B-带hwnd")

print("[C] PostThreadMessageW(tid) 纯线程消息（toast 现状）")
reset_pending("C-纯线程")
r = user32.PostThreadMessageW(tid, WM_APP_SHOW, 0, 0)
print("    PostThreadMessageW ret=%r" % r)
check("C-纯线程")

print("\n[D] 对照：直接 show() 走 toast 自己的入口")
mgr._cur_text = ""
toast.show_custom("D-toast入口", "start")
time.sleep(0.8)
print("    -> _cur_text=%r pending=%d"
      % (mgr._cur_text, len(mgr._pending)))

toast.stop()
print("[done]")
