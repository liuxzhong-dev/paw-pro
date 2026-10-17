# -*- coding: utf-8 -*-
"""
diag_keypause_probe.py —— 复现/验证「暂停键盘区后再按 W 显示已解除锁定」

主人原话（2026-10-16）：
    「键盘就绪以后，长按 w 开始，再点暂停，再按 w 还会显示已解除锁定，
      正常应该是暂停以后就默认解除锁定了」

真因（探针第一步就打印出来）：
    暂停时旧代码**只补了一个 keyup（松手）**，锁定的内部状态原样保留
    （_state[vk] 仍是 "locked"、_active_key 仍指向该键）。
    于是用户再按 W 时，钩子命中「锁定中再次按下 = 解除指令」分支
    -> 走 _notify(False) -> 弹「已解除锁定」。

修法：暂停 = 松手 + 清空锁定状态（core/keyboard.py::_clear_lock_state_locked），
      进入暂停的那一轮一次做完。

本探针用真实 KeyHoldLock 对象驱动真实状态机（只跑 _hook_callback + _inject_loop，
不装全局钩子），**send_key 换成记录器，不注入任何真实按键**。

用法：python diag_keypause_probe.py
"""
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.keyboard as kb                                    # noqa: E402
from core import pausectl                                     # noqa: E402

W = kb.key_to_vk("w")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("   %s %s%s" % ("OK  " if cond else "FAIL", name,
                         ("  -> " + detail) if detail else ""))


# ---- 把注入换成记录器：探针绝不碰真实键盘 -------------------------------
SENT = []


def _fake_send_key(vk, up=False, mark_injected=False, use_scancode=True):
    SENT.append((vk, bool(up)))
    return True


kb.send_key = _fake_send_key


def fire(lock, vk, down):
    """构造一次物理按键事件，直接喂给钩子回调。"""
    info = kb.KBDLLHOOKSTRUCT()
    info.vkCode = vk
    info.scanCode = 0
    info.flags = 0
    info.time = 0
    info.dwExtraInfo = 0
    return lock._hook_callback(
        0, kb.WM_KEYDOWN if down else kb.WM_KEYUP, ctypes.addressof(info))


def make_lock(threshold=0.4):
    events = []
    lock = kb.KeyHoldLock(
        keys=["w"], threshold_sec=threshold, repeat_hz=30, hold_down=True,
        on_state_change=lambda active, vk: events.append(
            ("LOCK", vk) if active else ("UNLOCK", vk)),
    )
    t = threading.Thread(target=lock._inject_loop, daemon=True)
    t.start()
    lock._test_thread = t
    return lock, events


def hold(lock, vk, seconds, interval=0.05):
    fire(lock, vk, True)
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(interval)
        fire(lock, vk, True)      # 模拟 Windows 长按自动重复


def stop(lock):
    lock._stop_inject = True
    lock._test_thread.join(timeout=2.0)


def reset_pause():
    pausectl.set_paused(False)
    pausectl.set_paused(False, "mouse")
    pausectl.set_paused(False, "key")


print("=" * 66)
print("场景 A：锁定中进入暂停 -> 锁定状态必须被清掉")
print("=" * 66)
reset_pause()
SENT[:] = []
lock, events = make_lock()
hold(lock, W, 1.0)
time.sleep(0.2)
check("长按 W 后进入锁定", lock.active == (True, "W"), "active=%r" % (lock.active,))
check("产生 LOCK 事件", events == [("LOCK", "W")], "events=%r" % (events,))
pausectl.set_paused(True, "key")
time.sleep(0.4)
check("暂停后 active 已是 (False,'')", lock.active == (False, ""),
      "active=%r" % (lock.active,))
check("暂停后 W 的状态回到 idle", lock.state_of(W) == "idle",
      "state=%r" % (lock.state_of(W),))
check("暂停时补发了 W 的 keyup（松手）",
      (W, True) in SENT, "SENT=%r" % (SENT[-6:],))
check("暂停本身不产生 UNLOCK 事件（通知由上层发）",
      events == [("LOCK", "W")], "events=%r" % (events,))

print()
print("=" * 66)
print("场景 B：★原始 bug★ 暂停后再按 W —— 不能再出现「已解除锁定」")
print("=" * 66)
before = list(events)
fire(lock, W, False)          # 用户松手
time.sleep(0.15)
fire(lock, W, True)           # 用户再按一次
time.sleep(0.25)
check("再按 W 没有产生任何 UNLOCK 事件",
      [e for e in events if e[0] == "UNLOCK"] == [],
      "events=%r" % (events,))
check("再按 W 之后仍不是锁定态（暂停期间不注入）",
      lock.active == (False, ""), "active=%r" % (lock.active,))
check("事件序列与暂停前一致", events == before,
      "before=%r now=%r" % (before, events))

print()
print("=" * 66)
print("场景 C：暂停期间短按 W 不会「排队」，恢复后需重新长按")
print("=" * 66)
fire(lock, W, False)
time.sleep(0.1)
pausectl.set_paused(False, "key")
time.sleep(0.3)
check("恢复后仍未被锁定（暂停期间的按键不生效）",
      lock.active == (False, ""), "active=%r" % (lock.active,))

print()
print("=" * 66)
print("场景 D：恢复后长按 W -> 应该能重新锁定（出现第二条 LOCK）")
print("=" * 66)
hold(lock, W, 1.0)
time.sleep(0.2)
check("恢复后长按可重新锁定", lock.active == (True, "W"),
      "active=%r" % (lock.active,))
check("产生了第二条 LOCK 事件",
      events == [("LOCK", "W"), ("LOCK", "W")], "events=%r" % (events,))
check("全程零 UNLOCK（旧代码这里会多一条 UNLOCK）",
      [e for e in events if e[0] == "UNLOCK"] == [], "events=%r" % (events,))
stop(lock)

print()
print("=" * 66)
print("场景 E：全局暂停（Pause 键）走同一条路径，行为一致")
print("=" * 66)
reset_pause()
lock, events = make_lock()
hold(lock, W, 1.0)
time.sleep(0.2)
check("先锁定", lock.active == (True, "W"), "active=%r" % (lock.active,))
pausectl.set_paused(True)          # 全局
time.sleep(0.4)
check("全局暂停同样解除锁定", lock.active == (False, ""),
      "active=%r" % (lock.active,))
fire(lock, W, False)
time.sleep(0.1)
fire(lock, W, True)
time.sleep(0.25)
check("全局暂停后再按 W 也不出现 UNLOCK",
      [e for e in events if e[0] == "UNLOCK"] == [], "events=%r" % (events,))
stop(lock)
reset_pause()

print()
print("=" * 66)
print("场景 F：负向对照 —— 两层防护都打掉，原 bug 必须复现")
print("=" * 66)
# 【为什么必须有这一段】全绿和「检查失效」长得一模一样。
# 要复现原始 bug 必须同时打掉两层：
#   ① 暂停期间的状态机冻结 _state_frozen
#   ② 暂停时的锁定状态清场 _clear_lock_state_locked
# 只打一层也不会出 UNLOCK —— 那正是「检查失效却全绿」的样子。
_orig_clear = kb.KeyHoldLock._clear_lock_state_locked
_orig_frozen = kb.KeyHoldLock._state_frozen
kb.KeyHoldLock._clear_lock_state_locked = lambda self: None
kb.KeyHoldLock._state_frozen = lambda self: False
try:
    lock, events = make_lock()
    hold(lock, W, 1.0)
    time.sleep(0.2)
    pausectl.set_paused(True, "key")
    time.sleep(0.4)
    check("负向对照：旧行为下暂停后 active 仍是锁定的（状态没清）",
          lock.active == (True, "W"), "active=%r" % (lock.active,))
    fire(lock, W, False)
    time.sleep(0.15)
    fire(lock, W, True)
    time.sleep(0.25)
    check("负向对照：少了防护，UNLOCK 果然出现（断言确实有效）",
          [e for e in events if e[0] == "UNLOCK"] != [],
          "events=%r" % (events,))
    stop(lock)
finally:
    kb.KeyHoldLock._clear_lock_state_locked = _orig_clear
    kb.KeyHoldLock._state_frozen = _orig_frozen
    reset_pause()

print()
print("=" * 66)
print("场景 G：暂停期间一直按住 W，恢复时不许「瞬间锁定」")
print("=" * 66)
# 不冻结状态机的话：暂停中按住 W -> press_start 早超阈值 ->
# 一恢复就自己锁上，角色直接跑起来，用户什么都没做。
reset_pause()
lock, events = make_lock()
pausectl.set_paused(True, "key")
hold(lock, W, 1.0)          # 暂停期间按住 W（自动重复也照发）
time.sleep(0.2)
check("暂停期间按住 W 不会锁定", lock.active == (False, ""),
      "active=%r" % (lock.active,))
pausectl.set_paused(False, "key")
time.sleep(0.15)
check("刚恢复的一瞬间没有被瞬间锁定", lock.active == (False, ""),
      "active=%r" % (lock.active,))
hold(lock, W, 1.0)          # 恢复后继续按着（手指没松）
time.sleep(0.2)
check("恢复后继续按着，越过阈值才正常锁定（这是应有的行为）",
      lock.active == (True, "W"), "active=%r" % (lock.active,))
fire(lock, W, False)
stop(lock)
reset_pause()

print()
print("=" * 66)
print("汇总：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
print("=" * 66)
for n in FAIL:
    print("   失败 -> %s" % n)
sys.exit(1 if FAIL else 0)
