# -*- coding: utf-8 -*-
"""
core/pausectl.py —— 全局暂停开关
================================

## 为什么单独一个模块

「暂停」要同时管住两条互不认识的链路：

  · dashboard.py 的鼠标连点循环（DynClick.run）
  · core/keyboard.py 的键盘锁定注入循环（KeyHoldLock._inject_loop）

而 dashboard  imports keyboard（键盘钩子归它管），
keyboard 绝不能反向 import dashboard —— 循环导入。
所以暂停状态放第三个模块里，两边都 import 它，
**全程序只有一个暂停标志**，不会出现「鼠标暂停了键盘还在跑」。

## 语义

paused = True 时：
  · 两个区都**保持「运行中」状态**（不是停止）
  · 但一个输入事件都不注入
  · 前端状态显示「已暂停」，再按一次暂停键恢复

暂停 vs 停止的区别（用户明确要求的功能）：
  · 停止 = 连点/锁定结束，参数可能要重设
  · 暂停 = 整个动作定住，参数原样保留，恢复后接着跑
  游戏里按一下暂停键定住画面，再按一下继续，
  不用切回仪表盘重来 —— 这正是「玩原神时 Ctrl+C 无效」的解法。

## 【分区暂停 · 2026-10-16】

主人原话：「就绪以后，点中键可以随时触发/暂停，点停止才会结束」。
于是触发键从「启停」变成了「启动 / 暂停 / 恢复」，
而原神里常常是**鼠标连点（普攻）+ 键盘锁定（前进）同时开着**，
按中键只想停普攻、不想连前进一起冻住。

所以暂停做成两层：

  · **全局开关** `_global`：Pause / ScrollLock 热键用，一次停两个区
  · **分区开关** `_zones["mouse"] / _zones["key"]`：各自的触发键用

判定 = 全局 OR 本区。老调用 `set_paused(flag)` / `is_paused()`
（不带 zone）行为完全不变，键盘回归测试和鼠标区测试照旧能过。
"""

import threading

_lock = threading.Lock()
_paused = False                       # 全局开关（Pause 热键）
_zones = {"mouse": False, "key": False, "macro": False}   # 分区开关（各自的触发键）
#  ↑ "macro" 是 v34 加的：宏回放跑在自己的引擎里，以前只能靠**全局**暂停
#     （Pause 键）冻住 —— 一按 Pause 连鼠标区、键盘区一起停。
#     宏有了自己的启停键（默认 F7）后必须有个只管它的分区，
#     否则「按 F7 暂停宏」会连带把连点也定住。
#     判定仍是「全局 OR 本区」，老调用行为不变。


def set_paused(flag, zone=None):
    """
    设置暂停状态，返回设置后的值。

    zone=None  → 全局（同时写所有区，老行为）
    zone="mouse"/"key"/"macro" → 只管那一个区
    """
    global _paused
    with _lock:
        if zone is None:
            _paused = bool(flag)
            for k in _zones:
                _zones[k] = bool(flag)
            return _paused
        if zone not in _zones:
            raise ValueError("未知分区：%r" % (zone,))
        _zones[zone] = bool(flag)
        return _zones[zone]


def is_paused(zone=None):
    """
    当前是否暂停。

    zone=None  → 只看全局开关
    zone="mouse"/"key"/"macro" → 全局 OR 本区
    """
    with _lock:
        if _paused:
            return True
        if zone is None:
            return False
        return bool(_zones.get(zone, False))


def any_paused():
    """只要有任何一个区处于暂停就返回 True（前端徽标用）。"""
    with _lock:
        return _paused or any(_zones.values())


def clear_global():
    """
    只清全局暂停位，**不动分区标志**（分区恢复用）。

    【为什么需要它 · 2026-10-17 主人实测】
    分区触发键恢复时，如果全局暂停（Pause 键/全局按钮）还开着，
    判定是「全局 OR 本区」，只清本区是无效的，必须把全局位也解除。
    旧代码直接调 set_paused(False)（zone=None）—— 它会**把两个区的
    标志一起清掉**，主人实测现象：「按 G1 恢复键盘锁定，
    暂停中的鼠标区也跟着恢复了」。
    本函数只动 _paused，其他区暂停与否原样保留。
    """
    global _paused
    with _lock:
        _paused = False
        return _paused


def toggle_paused(zone=None):
    """切换暂停状态，返回切换后的值。zone=None 为全局。"""
    return set_paused(not is_paused(zone), zone)
