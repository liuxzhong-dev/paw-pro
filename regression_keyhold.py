# -*- coding: utf-8 -*-
"""
长按锁定功能 —— 多场景回归测试
=================================
沙箱环境收不到真实键盘输入（GetAsyncKeyState 都读不到），
所以这里直接手工构造 KBDLLHOOKSTRUCT，调用钩子回调来模拟事件。
不依赖 start()（真实钩子装不上），只跑 _hook_callback + _inject_loop。

用法：python regression_keyhold.py
"""
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.keyboard import (  # noqa: E402
    KeyHoldLock, key_to_vk, INJECT_MARK, KBDLLHOOKSTRUCT,
    WM_KEYDOWN, WM_KEYUP,
)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("   %s %s%s" % ("OK  " if cond else "FAIL", name,
                         ("  -> " + detail) if detail else ""))


def fire(lock, vk, down, flags=0):
    """模拟一次物理按键事件，返回钩子返回值（1 = 被吞掉）。"""
    info = KBDLLHOOKSTRUCT()
    info.vkCode = vk
    info.scanCode = 0
    info.flags = flags
    info.time = 0
    info.dwExtraInfo = 0
    wparam = WM_KEYDOWN if down else WM_KEYUP
    return lock._hook_callback(0, wparam, ctypes.addressof(info))


def make_lock(keys=("w",), threshold=0.5, hz=30, hold_down=True):
    events = []
    lock = KeyHoldLock(
        keys=list(keys), threshold_sec=threshold, repeat_hz=hz,
        hold_down=hold_down,
        on_state_change=lambda active, vk: events.append(
            ("LOCK", vk) if active else ("UNLOCK", vk)),
    )
    t = threading.Thread(target=lock._inject_loop, daemon=True)
    t.start()
    lock._test_thread = t
    return lock, events


def hold(lock, vk, seconds, auto_repeat=True, interval=0.05):
    """模拟「按住」：按下，然后按 Windows 节奏补自动重复 keydown。"""
    fire(lock, vk, True)
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(interval)
        if auto_repeat:
            fire(lock, vk, True)
    return time.time() - t0


def stop(lock):
    lock._stop_inject = True
    lock._test_thread.join(timeout=2.0)


# ==================================================================
print("=" * 62)
print("场景 1：未设置的按键完全不受影响")
print("=" * 62)
lock, events = make_lock(keys=("w",), threshold=0.5)
A = key_to_vk("a")
fire(lock, A, True)
t0 = time.time()
while time.time() - t0 < 3.0:
    time.sleep(0.05)
    fire(lock, A, True)
fire(lock, A, False)
time.sleep(0.3)
check("A 键未触发锁定", lock.active == (False, ""), "active=%r" % (lock.active,))
check("A 键状态保持 idle", lock.state_of(A) == "idle",
      "state=%r" % (lock.state_of(A),))
check("A 键无事件产生", events == [], "events=%r" % (events,))
check("W 键未被误锁", lock.state_of(key_to_vk("w")) != "locked")
stop(lock)

# ==================================================================
print()
print("=" * 62)
print("场景 2：多键 w+a，各自独立")
print("=" * 62)
lock, events = make_lock(keys=("w", "a"), threshold=0.5)
W = key_to_vk("w")
hold(lock, W, 1.0)
time.sleep(0.2)
check("W 已锁定", lock.active == (True, "W"), "active=%r" % (lock.active,))
check("只产生一条 W 的 LOCK 事件", events == [("LOCK", "W")],
      "events=%r" % (events,))

# W 锁着时短按 A，不应影响 W
fire(lock, A, True)
time.sleep(0.1)
fire(lock, A, False)
time.sleep(0.2)
check("按 A 不解除 W 的锁定", lock.active == (True, "W"),
      "active=%r" % (lock.active,))
check("期间无额外事件", events == [("LOCK", "W")], "events=%r" % (events,))

# 物理松开 W（keyup 正常透传，不吞——靠注入实现持续触发）
r = fire(lock, W, False)
time.sleep(0.2)
check("W 的 keyup 正常透传（不吞）", r == 0, "ret=%r（1=被吞）" % (r,))
check("松开后仍保持锁定", lock.active == (True, "W"), "active=%r" % (lock.active,))

# 再次按 W -> 解除
r = fire(lock, W, True)
time.sleep(0.2)
check("再次按 W 解除锁定", lock.active == (False, ""), "active=%r" % (lock.active,))
check("产生 UNLOCK 事件", events == [("LOCK", "W"), ("UNLOCK", "W")],
      "events=%r" % (events,))
stop(lock)

# ==================================================================
print()
print("=" * 62)
print("场景 3：反复快速点按不误触发")
print("=" * 62)
lock, events = make_lock(keys=("w",), threshold=0.5)
W = key_to_vk("w")
for _ in range(30):
    fire(lock, W, True)
    time.sleep(0.03)
    fire(lock, W, False)
    time.sleep(0.03)
time.sleep(0.3)
check("快速点按 30 次未误锁定", lock.active == (False, ""),
      "active=%r" % (lock.active,))
check("无 LOCK 事件", not any(e[0] == "LOCK" for e in events),
      "events=%r" % (events,))
stop(lock)

# ==================================================================
print()
print("=" * 62)
print("场景 4：解除后能再次锁定（注入线程存活）")
print("=" * 62)
lock, events = make_lock(keys=("w",), threshold=0.5)
W = key_to_vk("w")
for rnd in (1, 2):
    hold(lock, W, 1.0)
    time.sleep(0.2)
    check("第 %d 轮锁定成功" % rnd, lock.active == (True, "W"),
          "active=%r" % (lock.active,))
    fire(lock, W, False)          # 物理松开
    time.sleep(0.1)
    fire(lock, W, True)           # 再次按下 -> 解除
    time.sleep(0.2)
    check("第 %d 轮解除成功" % rnd, lock.active == (False, ""),
          "active=%r" % (lock.active,))
    fire(lock, W, False)
    time.sleep(0.2)
check("两轮 LOCK/UNLOCK 都记录到",
      events.count(("LOCK", "W")) == 2 and events.count(("UNLOCK", "W")) == 2,
      "events=%r" % (events,))
stop(lock)

# ==================================================================
print()
print("=" * 62)
print("场景 5：panic_release 幂等")
print("=" * 62)
lock, events = make_lock(keys=("w",), threshold=0.5)
W = key_to_vk("w")
hold(lock, W, 1.0)
time.sleep(0.2)
check("先锁定成功", lock.active == (True, "W"))
r1 = lock.panic_release()
time.sleep(0.2)
check("首次 panic 返回 True", r1 is True, "ret=%r" % (r1,))
check("panic 后 active 为空", lock.active == (False, ""), "active=%r" % (lock.active,))
r2 = lock.panic_release()
check("二次 panic 返回 False 且无副作用", r2 is False, "ret=%r" % (r2,))
check("panic_name = %s" % lock.panic_name, lock.panic_name == "F12")
stop(lock)

# 紧急键与目标键冲突时应自动改用 F12
lock2 = KeyHoldLock(keys=["f12"], threshold_sec=1.0, panic_key="f12")
check("紧急键与目标键冲突时改用 F12 兜底",
      lock2.panic_name in ("F12",), "panic=%r" % (lock2.panic_name,))

print("   INJECT_MARK = 0x%08X" % INJECT_MARK)
check("INJECT_MARK 已定义", isinstance(INJECT_MARK, int) and INJECT_MARK != 0)

# ==================================================================
print()
print("=" * 62)
print("场景 6：两种注入模式的按键形态")
print("=" * 62)
# 【核心区别】hold_down=True  **重复注入 down、绝不注入 up**
#              -> 程序看到「键一直没松，且有持续重复」-> 角色持续移动
# hold_down=False 成对注入 down+up
#              -> 程序看到「按下又抬起」-> 判定为多次点击
# 两个断言缺一不可：
#   1) 锁定期间 up数量必须为 0（否则变成反复点击）
#   2) keydown 必须重复多次（只发一次游戏不响应，角色不动）
import core.keyboard as kb  # noqa: E402

sent = []
_REAL_SENDINPUT = kb._user32.SendInput
_real_send_key = kb.send_key


def spy_send_key(vk, up=False, mark_injected=False, use_scancode=True):
    # 【重要】第0 位是时间戳：判断「反复点击」有没有效果，
    # 量的就是 down 与 up 之间的**时间差**，没有时间戳量不出来。
    sent.append((time.time(), vk, up, mark_injected, use_scancode))
    return True


kb.send_key = spy_send_key
try:
    W = key_to_vk("w")

    # ---- 按住模式（默认）：只发一次 down，锁定期间绝不发 up ----
    lock, events = make_lock(keys=("w",), threshold=0.5, hz=30,
                             hold_down=True)
    sent.clear()
    hold(lock, W, 1.2)
    time.sleep(0.5)
    downs = [s for s in sent if s[2] is False]
    ups = [s for s in sent if s[2] is True]
    check("按住模式：锁定期间有注入 keydown", len(downs) > 0,
          "共%d 次" % len(downs))
    check("按住模式：注入的键就是 W", all(s[1] == W for s in downs))
    check("按住模式：注入的 keydown 都带标记",
          all(s[3] is True for s in downs),
          "未标记 %d 次" % sum(1 for s in downs if not s[3]))
    # 【核心断言1】按住模式下锁定期间**绝不能有任何 keyup**，
    # 否则目标程序看到的是「按了又松」= 反复点击，而不是一直按着。
    check("【核心】按住模式：锁定期间 keyup 数量为 0", len(ups) == 0,
          "down=%d up=%d" % (len(downs), len(ups)))
    # 【核心断言2】必须**重复注入 keydown**。
    # 实测只发一次 keydown 时，游戏不会持续响应（角色不动）——
    # 真人按住时硬件会持续产生 keydown 重复，游戏靠这个判定为「按住」。
    check("【核心】按住模式：keydown 重复注入", len(downs) > 5,
          "实际 %d 次" % len(downs))
    stop(lock)
    time.sleep(0.2)

    # ---- 成对模式：down 与 up 数量相等，频率接近 hz ----
    lock, events = make_lock(keys=("w",), threshold=0.5, hz=30,
                             hold_down=False)
    sent.clear()
    hold(lock, W, 1.2)
    time.sleep(0.5)
    downs = [s for s in sent if s[2] is False]
    ups = [s for s in sent if s[2] is True]
    # 【修订】原来这条断言写的是 `len(downs)==len(ups) and downs`——
    # 末尾的 `and downs` 返回的是 list 而非 bool，且计数相等并非本模式的关键，
    # 真正决定「反复点击有没有效果」的是 **down->up 之间的时间差**。
    # 所以改为：数量成对 + 每对之间确实隔了可感知的时间。
    check("成对模式：down 与 up 成对注入",
          len(downs) > 0 and abs(len(downs) - len(ups)) <= 1,
          "down=%d up=%d（允许末位差1：停止瞬间可能只发了 down）"
          % (len(downs), len(ups)))
    # 【核心断言】down->up 必须有真实间隔。
    # 之前这里是两次 SendInput 背靠背调用，间隔仅几十微秒，
    # 目标程序会判定为「抖动」直接丢弃 —— 表现就是「选了反复点击却完全没反应」。
    # 参考：AutoHotkey ClickDelay 默认 10ms，这里取 20ms。
    if len(downs) and len(ups):
        ups_ts = sorted(s[0] for s in ups)
        gaps = []
        for d in downs:
            nxt = [u for u in ups_ts if u >= d[0]]
            if nxt:
                gaps.append((nxt[0] - d[0]) * 1000.0)
        avg_gap = (sum(gaps) / len(gaps)) if gaps else 0.0
        check("【核心】成对模式：down->up 间隔 >= 10ms（否则目标程序忽略）",
              avg_gap >= 10.0, "实测平均 %.1f ms，共 %d 对" % (avg_gap, len(gaps)))
    check("成对模式：注入频率接近 30Hz", 15 <= len(downs) <= 55,
          "锁定约1.2秒内 %d 组（成对模式每组多花20ms下压，频率略降属正常）"
          % len(downs))
    stop(lock)
finally:
    kb.send_key = _real_send_key

# ==================================================================
print()
print("=" * 62)
print("场景 7：解除锁定会补发 keyup（否则应用停在「按下」）")
print("=" * 62)
sent.clear()
kb.send_key = spy_send_key
try:
    lock, events = make_lock(keys=("w",), threshold=0.5, hz=30)
    W = key_to_vk("w")
    hold(lock, W, 1.0)
    time.sleep(0.3)
    check("先锁定成功", lock.active == (True, "W"))
    sent.clear()
    fire(lock, W, False)          # 物理松开（keyup 正常透传）
    time.sleep(0.1)
    fire(lock, W, True)           # 再次按下 -> 解除
    time.sleep(0.3)
    # 关键：解除用的那次「按下」必须再补一次物理抬起，
    # 否则按键仍处于按下状态，Windows 自动重复 keydown 会让状态
    # 重新回到pressing -> 又开始计时锁定 -> 看起来像「解除后又注入」。
    fire(lock, W, False)
    time.sleep(0.2)
    ups = [s for s in sent if s[2] is True]
    check("解除时补发了 keyup", len(ups) >= 1, "共 %d 次" % len(ups))
    check("补发的 keyup 带标记", all(s[3] is True for s in ups))
    check("解除后 active 为空", lock.active == (False, ""), "active=%r" % (lock.active,))
    check("解除且物理松开后状态回到 idle",
          lock.state_of(W) == "idle", "state=%r" % (lock.state_of(W),))
    # 解除后不应再持续注入。
    # 注意不是绝对为0：注入线程每轮只在锁内读一次 active_key，
    # 解除瞬间可能还有几组已经排好的补发（属正常收尾尾巴）。
    # 关键是「不再持续」——0.4 秒 @30Hz 若还在注入就会有约 12 组。
    before = len([s for s in sent if s[2] is False])
    time.sleep(0.4)
    after = [s for s in sent if s[2] is False]
    check("解除后停止注入（允许收尾尾巴）", len(after) - before <= 3,
          "解除前%d 次 -> 0.4秒内新增 %d 次" % (before, len(after) - before))
    stop(lock)
finally:
    kb.send_key = _real_send_key

# ==================================================================
print()
print("=" * 62)
print("场景 8：SendInput 的 cbSize 必须是 sizeof(INPUT)【关键】")
print("=" * 62)
# 这个坑最阴险：传了精简结构的大小，Windows 直接拒绝、返回 0，
# 一个事件都发不出去。状态机一切正常、日志显示「已锁定」，
# 但屏幕上什么都不动——因为事件压根没进系统。
import core.inputs as inp  # noqa: E402

need_size = ctypes.sizeof(inp.INPUT)
W = key_to_vk("w")
check("SendInput 用的 sizeof(INPUT) 必须为 40",
      need_size == 40, "实际 %d" % need_size)

captured = []
kb._user32.SendInput = lambda n, p, cb: (captured.append(cb), 1)[1]
try:
    sent.clear()
    kb.send_key(W, up=False, mark_injected=True)
    kb.send_key(W, up=True, mark_injected=True)
finally:
    kb._user32.SendInput = _REAL_SENDINPUT
check("send_key 传出的 cbSize == sizeof(INPUT)",
      captured and all(c == need_size for c in captured),
      "实测 %s，应为 %d" % (captured, need_size))

# 真实调一次 SendInput，确认真的发得出去。
#
# 【输入注入闸门】这一行会**真的敲一个 W**，投给当前前台窗口 ——
# 也就是用户正在用的程序。之前不加闸门跑测试时，
# 用户那边就会看到键盘自己动。默认跳过；
# 本条用例真正要验的东西（cbSize 是否为 sizeof(INPUT)）上面已经验过了，
# 这里只是额外确认"真的发得出去"，加 --real-input 才执行。
if "--real-input" in sys.argv:
    kb.send_key(W, up=False, mark_injected=True)
    down_ok = kb.last_send_error() == 0
    time.sleep(0.05)
    kb.send_key(W, up=True, mark_injected=True)
    up_ok = kb.last_send_error() == 0
    check("真实 SendInput 调用无错误", down_ok and up_ok,
          "last_error=%d" % kb.last_send_error())
else:
    print("  跳过真实注入（会敲一个 W）；加 --real-input 可开启")

# ==================================================================
print()
print("=" * 62)
print("场景 9：解除后手指不松手，不应自动重新锁定")
print("=" * 62)
# 真实场景：用户再按一次W 解除，但手指还压着没抬起。
# Windows 会持续发自动重复 keydown，
# 若状态机处理不当，会立刻回到 pressing 并重新计时锁定（表现为「解除无效」）。
lock, events = make_lock(keys=("w",), threshold=0.5)
W = key_to_vk("w")
hold(lock, W, 1.0)
time.sleep(0.2)
check("先锁定成功", lock.active == (True, "W"))
fire(lock, W, False)            # 物理松开
time.sleep(0.1)
fire(lock, W, True)             # 再按下 -> 解除（手指不抬起）
time.sleep(0.2)
check("已解除", lock.active == (False, ""), "active=%r" % (lock.active,))
# 模拟按住不放：持续来自动重复 keydown，持续 1.5 秒（远超阈值 0.5）
for _ in range(30):
    fire(lock, W, True)
    time.sleep(0.05)
check("手指不松手也不会自动重新锁定", lock.active == (False, ""),
      "active=%r  <- 若为 True 说明又锁回去了" % (lock.active,))
check("期间没有新的 LOCK 事件", events.count(("LOCK", "W")) == 1,
      "events=%r" % (events,))
fire(lock, W, False)            # 终于抬起
time.sleep(0.2)
check("抬起后状态为 idle", lock.state_of(W) == "idle",
      "state=%r" % (lock.state_of(W),))
# 抬起后重新长按，应当能再次锁定
hold(lock, W, 1.0)
time.sleep(0.2)
check("重新长按可以再次锁定", lock.active == (True, "W"),
      "active=%r" % (lock.active,))
stop(lock)

# ==================================================================
print()
print("=" * 62)
print("场景 10：扫描码注入时 wVk 必须为 0")
print("=" * 62)
# 【本项目最隐蔽的一个坑】
# KEYEVENTF_SCANCODE 模式下wVk 是被忽略的残留字段，
# 同时填 wVk 和 wScan 属于非法用法：
#   -> WM_KEYDOWN 能收到（注入确实到了目标窗口）
#   -> 但TranslateMessage/ToUnicode 拿扫描码换算出错误的键
#   -> 永远出不来 WM_CHAR，表现为「记事本不刷字」
# 这个 bug 只看 SendInput 返回值是发现不了的（它照样返回成功）。
import core.inputs as _inp2  # noqa: E402

_grabbed = []


def _grab(n, p, cb):
    # p 是 byref() 产生的 CArgObject，
    # 必须 cast 成 POINTER(INPUT) 才能取出内容（addressof 会报 invalid type）
    ev = ctypes.cast(p, ctypes.POINTER(_inp2.INPUT)).contents
    # 直接读原始内存：只看 ev.ki.xxx 会漏掉「属性是假的」这种坑，
    # 必须确认字节真的写进了结构体。
    raw = ctypes.string_at(ctypes.byref(ev), ctypes.sizeof(ev))
    _grabbed.append((ev.ki.wVk, ev.ki.wScan, ev.ki.dwFlags, raw))
    return 1


kb._user32.SendInput = _grab
try:
    # 两组分别收集，别中途 clear，否则后面的断言读到的是被清空后又被
    # 虚拟键码模式重新填上的数据，索引会错位。
    _grabbed.clear()
    kb.send_key(W, up=False, use_scancode=True)
    kb.send_key(W, up=True, use_scancode=True)
    _scan = list(_grabbed)
    _grabbed.clear()
    kb.send_key(W, up=False, use_scancode=False)
    kb.send_key(W, up=True, use_scancode=False)
    _vk = list(_grabbed)
finally:
    kb._user32.SendInput = _REAL_SENDINPUT
_grabbed = _scan + _vk

check("扫描码模式 wVk==0 且 wScan!=0",
      _grabbed[0][0] == 0 and _grabbed[0][1] != 0,
      "抓到 %r" % (_grabbed[0],))
check("扫描码模式带 KEYEVENTF_SCANCODE 标志",
      bool(_grabbed[0][2] & kb.KEYEVENTF_SCANCODE),
      "flags=%r" % (_grabbed[0][2],))
check("抬起事件带 KEYEVENTF_KEYUP 标志",
      bool(_grabbed[1][2] & kb.KEYEVENTF_KEYUP),
      "flags=%r" % (_grabbed[1][2],))
check("虚拟键码模式 wVk!=0 且 wScan==0",
      _grabbed[2][0] != 0 and _grabbed[2][1] == 0,
      "抓到 %r" % (_grabbed[2][:3],))

# ---- 最隐蔽的一个坑：INPUT 联合体缺ki 成员 ----
# ctypes 给不存在的联合体成员赋值**不报错**，
# 而是当成给 Python 对象挂临时属性，数据一个字节都没写进内存。
# 后果：SendInput 发的是「键盘类型 + 空按键」，永远收不到按键，
# 而 API 返回值一直是成功。这是「注入统计全成功但屏幕不刷字」的根因。
_union_members = [f[0] for f in _inp2.INPUT._U._fields_]
check("INPUT 联合体同时含 mi/ki/hi（缺一不可）",
      all(k in _union_members for k in ("mi", "ki", "hi")),
      "成员=%r" % (_union_members,))
check("sizeof(INPUT) == 40（SendInput 要求完整联合体大小）",
      ctypes.sizeof(_inp2.INPUT) == 40,
      "实际 %d" % ctypes.sizeof(_inp2.INPUT))

# 直接验证「字节真的进了内存」：扫描码模式的 wScan=0x11 必须出现在原始字节里
_ev = _inp2.INPUT(type=1)
_ev.ki = _inp2.KEYBDINPUT(wVk=0, wScan=0x11, dwFlags=8, time=0, dwExtraInfo=0)
_raw = ctypes.string_at(ctypes.byref(_ev), ctypes.sizeof(_ev))
check("键盘事件字节确实写进内存（不是假属性）",
      b"\x11\x00\x08\x00" in _raw,
      "原始字节=%s" % _raw.hex(" "))

# send_key 实际发出的结构体，wScan 也必须落到内存里
check("send_key 发出的扫描码真实存在于内存",
      b"\x11\x00" in _grabbed[0][3],
      "原始字节=%s" % _grabbed[0][3].hex(" "))

# ==================================================================
print()
print("=" * 62)
print("场景 11：暂停门闩（暂停期间零注入；暂停 = 松手 + 解除锁定）")
print("=" * 62)
# 【对应主人反馈】「没有设置全局暂停，玩原神之后 ctrl+c 就无效了」。
# 暂停期间注入循环必须一个事件都不发；而「按住」模式还要额外补一个
# keyup —— 不补的话目标程序一直认为键按着，暂停就失去意义（角色还在跑）。
from core import pausectl  # noqa: E402

lock, events = make_lock(keys=("w",), threshold=0.5, hz=30, hold_down=True)
W = key_to_vk("w")
_hold_downs = []
_real_send_key = kb.send_key


def _counting_send_key(vk, up=False, mark_injected=False, use_scancode=True):
    if vk == W:
        _hold_downs.append(bool(up))
    return _real_send_key(vk, up=up, mark_injected=mark_injected,
                          use_scancode=use_scancode)


kb.send_key = _counting_send_key
try:
    hold(lock, W, 1.0)
    time.sleep(0.3)
    check("先锁定成功", lock.active == (True, "W"),
          "active=%r" % (lock.active,))
    check("暂停前有注入", len(_hold_downs) > 0, "注入 %d 次" % len(_hold_downs))

    # ---- 暂停 ----
    #
    # 【2026-10-16 规格变更 · 主人实测报回】
    # 原话：「键盘就绪以后，长按 w 开始，再点暂停，再按 w 还会显示已解除锁定，
    #        正常应该是暂停以后就默认解除锁定了」
    # 于是键盘区的暂停从「冻结住按住状态」改成「松手 + 解除锁定」：
    # 进入暂停时既补 keyup，也把状态机清干净。
    # 因此「暂停期间锁定状态不丢」这条旧断言已经不成立，改为「必须已清空」。
    pausectl.set_paused(True)
    _n_before = len(_hold_downs)
    _ups_before = sum(1 for u in _hold_downs if u)
    time.sleep(0.4)
    _downs_during = len([u for u in _hold_downs[_n_before:] if not u])
    _ups_during = len([u for u in _hold_downs[_n_before:] if u])
    check("暂停期间零注入（无新 keydown）", _downs_during == 0,
          "暂停后仍注入 %d 次 down" % _downs_during)
    check("按住模式进入暂停时补了一个 keyup", _ups_during == 1,
          "补了 %d 个 up" % _ups_during)
    check("暂停即解除锁定（状态已清空）", lock.active == (False, ""),
          "active=%r" % (lock.active,))
    check("暂停后目标键状态回到 idle", lock.state_of(W) == "idle",
          "state=%r" % (lock.state_of(W),))

    # ---- 恢复 ----
    # 暂停已经把锁定解除了，所以恢复**不会**「接着跑」：
    # 目标是「该区重新待命」，用户必须重新长按目标键。
    pausectl.set_paused(False)
    time.sleep(0.4)
    _new_downs = len([u for u in _hold_downs[_n_before:] if not u])
    check("恢复后不会自动接着注入（锁定已在暂停时解除）", _new_downs == 0,
          "恢复后新增 %d 次 down" % _new_downs)
    check("恢复后不是锁定态（需重新长按）", lock.active == (False, ""),
          "active=%r" % (lock.active,))

    # 重新长按 -> 应该能再次锁定并恢复注入
    hold(lock, W, 1.1)
    time.sleep(0.3)
    _again_downs = len([u for u in _hold_downs[_n_before:] if not u])
    check("重新长按后恢复注入", _again_downs > 0,
          "重新长按后新增 %d 次 down" % _again_downs)
    check("重新长按后重新进入锁定", lock.active == (True, "W"),
          "active=%r" % (lock.active,))
    fire(lock, W, False)
    time.sleep(0.1)
finally:
    kb.send_key = _real_send_key
    pausectl.set_paused(False)
    stop(lock)

# ==================================================================
print()
print("=" * 62)
print("汇总：通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    print("结论：FAIL")
    sys.exit(1)
print("结论：PASS 全部场景正常")
