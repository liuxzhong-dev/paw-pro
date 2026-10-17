# -*- coding: utf-8 -*-
"""
core/dpi.py —— DPI 感知（多显示器缩放适配）

【为什么需要】
Windows 会在「进程不是 DPI 感知」时，把所有拿到的坐标按 96dpi
虚拟化后再给进程。于是：
  · 显示器缩放 150% 时，GetCursorPos 拿到的 (100,100)
    实际落在物理像素 (150,150)
  · 「固定坐标点击」「宏 move@x,y」「鼠标录制回放」
    全部按虚拟坐标走位 —— 录制时记的是虚拟坐标，
    换一台缩放比例不同的机器回放，落点整体偏移

【为什么以前没炸】
连点是「点当前鼠标位置」，自己按自己，偏移被抵消；
键盘锁定不涉及坐标。所以纯连点场景感知不到。
**鼠标录制 / 固定坐标 / 跨机器回放 一律会中招。**

【怎么做】
启动时调用一次 enable_dpi_awareness()，告诉系统
「本进程自己处理缩放，别虚拟化」。之后 GetCursorPos /
GetWindowRect / SendInput 的绝对坐标全部是物理像素，一致。

【调用时机】
必须在创建任何窗口之前（dashboard 起 webview 前、
app.py 起 tkinter 前）。晚调用无效，系统只认第一次。

【兼容】
按 Windows 版本递进尝试三条路，全部失败也不抛
（老系统上继续跑，只是回到旧行为）：
  Win10 1703+ : SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2 = -4)
  Win8.1+     : SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE = 2)
  Vista+      : SetProcessDPIAware()
"""

import ctypes
from ctypes import wintypes as wt

_done = False
_result = "未调用"


def enable_dpi_awareness():
    """
    把本进程设为「每显示器感知」。可重复调用，只生效一次。
    返回当前状态字符串，供启动日志记录（排障时看得见）。
    """
    global _done, _result
    if _done:
        return _result

    try:
        user32 = ctypes.WinDLL("user32")
    except Exception:
        _done = True
        _result = "无 user32（非 Windows）"
        return _result

    # 路 1：Win10 1703+，最现代，跨显示器缩放变化时收 WM_DPICHANGED
    try:
        ctx = user32.SetProcessDpiAwarenessContext
        ctx.argtypes = [ctypes.c_void_p]
        ctx.restype = wt.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctx(wt.HANDLE(-4).value):
            _done = True
            _result = "PER_MONITOR_AWARE_V2"
            return _result
    except Exception:
        pass

    # 路 2：Win8.1+
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.HRESULT
        # PROCESS_PER_MONITOR_DPI_AWARE = 2（S_OK = 0）
        if shcore.SetProcessDpiAwareness(2) == 0:
            _done = True
            _result = "PROCESS_PER_MONITOR_DPI_AWARE"
            return _result
    except Exception:
        pass

    # 路 3：Vista+，系统级（所有显示器同一缩放）
    try:
        if user32.SetProcessDPIAware():
            _done = True
            _result = "SYSTEM_DPI_AWARE"
            return _result
    except Exception:
        pass

    _done = True
    _result = "设置失败（保持旧行为）"
    return _result


def status():
    """当前 DPI 感知状态（未调用过时先不真设，只返回文字）。"""
    return _result
