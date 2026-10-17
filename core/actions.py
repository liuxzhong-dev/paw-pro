# -*- coding: utf-8 -*-
"""
core/actions.py —— 任务自然完成后的收尾动作（S4 · v23）
==============================================

【是什么】连点/抖器跑到设定的次数或时长上限（**自然完成**，不是用户按停止）后，
自动执行一个收尾动作：提示音 / 锁屏 / 关机。参考项目 WinAutoClick 的
「完成后动作」（锁屏/注销/关机/待机），挂机刷材料的场景很实用：
刷完自动关机省电，刷完提示音叫人回来。

【三条安全设计】
1. **只在自然完成时触发**。用户点「停止鼠标」/ Ctrl+C 绝不触发——
   判断逻辑在调用方（dashboard 的 _finished / cli 的 _on_finish），
   本模块只管执行，不判断该不该执行。
2. **关机有倒计时且可取消**。默认 30 秒，倒计时每秒更新一次状态消息；
   期间用户回仪表盘点「取消」、或命令行按 Ctrl+C，都能中止。
   取消时除了停掉本模块的倒计时，还会发 `shutdown /a` 撤销系统层指令。
3. **系统层也留窗口**。真正关机走 `shutdown /s /t 60`（延迟 60 秒），
   不是立刻断电压——即使本程序崩了/被杀了，用户也有 60 秒窗口
   在别的控制台敲 `shutdown /a` 救回来。

【测试】所有 OS 调用（声音/锁屏/关机/撤销）集中在 _do_* 四个函数里，
探针打桩这四个函数即可全覆盖，不会真的锁屏关机。
"""

import ctypes
import subprocess
import threading
import time

# 合法动作（none = 不动作）
ACTIONS = ("none", "sound", "lock", "shutdown")

# 倒计时默认秒数（关机前给用户的反应时间）
DEFAULT_SECONDS = 30

# 系统层关机延迟：shutdown /s /t 的这个值。留得比我们的倒计时长，
# 保证「我们的倒计时结束 -> 发指令 -> 用户还有得取消」。
SHUTDOWN_OS_DELAY = 60

# 中文标签（界面/提示用）
LABELS = {"none": "不动作", "sound": "提示音",
          "lock": "锁屏", "shutdown": "关机"}

# subprocess 不弹黑窗（CREATE_NO_WINDOW）
_CREATE_NO_WINDOW = 0x08000000

_lock = threading.Lock()
_state = {"pending": False, "action": "none", "left": 0, "fired": False}
_cancel_evt = threading.Event()


def _label(action):
    return LABELS.get(str(action), str(action))


# --------------------------------------------------------------------------
# OS 调用层（测试只打桩这四个，其他逻辑照真跑）
# --------------------------------------------------------------------------
def _do_sound():
    """响两声系统提示音。失败返回 False（比如没有声音设备）。"""
    try:
        import winsound
        for _ in range(2):
            winsound.MessageBeep(0x00000040)      # MB_ICONASTERISK
            time.sleep(0.35)
        return True
    except Exception:
        return False


def _do_lock():
    """锁屏。普通用户权限就能调，不需要管理员。"""
    try:
        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:
        return False


def _do_shutdown():
    """发系统关机指令（延迟 SHUTDOWN_OS_DELAY 秒，可被 shutdown /a 撤销）。"""
    try:
        subprocess.Popen(
            ["shutdown", "/s", "/t", str(SHUTDOWN_OS_DELAY)],
            creationflags=_CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def _do_abort():
    """撤销系统层关机指令（shutdown /a）。"""
    try:
        subprocess.Popen(["shutdown", "/a"], creationflags=_CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 状态查询
# --------------------------------------------------------------------------
def pending_info():
    """当前待执行状态快照（前端轮询用）。"""
    with _lock:
        return dict(_state)


def is_pending():
    with _lock:
        return _state["pending"]


# --------------------------------------------------------------------------
# 倒计时与取消
# --------------------------------------------------------------------------
def arm(action, seconds=DEFAULT_SECONDS, on_tick=None):
    """
    启动一个收尾动作倒计时。返回 True 表示已排队。

    action="none"/不认识的动作直接返回 False（什么都不做）。
    已有一个在排队时返回 False，不叠加。
    on_tick(left) 每秒回调一次，供调用方更新界面消息。
    """
    action = str(action or "none")
    if action not in ACTIONS or action == "none":
        return False
    seconds = max(1, int(seconds or DEFAULT_SECONDS))
    with _lock:
        if _state["pending"]:
            return False
        _state.update(pending=True, action=action, left=seconds,
                      fired=False)
    _cancel_evt.clear()
    t = threading.Thread(target=_countdown,
                         args=(action, seconds, on_tick),
                         name="done-action", daemon=True)
    t.start()
    return True


def _countdown(action, seconds, on_tick):
    """倒计时线程：每秒一步，可被取消事件随时打断。"""
    left = seconds
    while left > 0:
        if _cancel_evt.wait(1.0):
            # 取消：倒计时作废，什么都不执行
            with _lock:
                _state.update(pending=False, left=0, fired=False)
            return
        left -= 1
        with _lock:
            _state["left"] = left
        if on_tick is not None:
            try:
                on_tick(left)
            except Exception:
                pass          # 界面更新失败不该影响倒计时本身
    # 数完了：执行
    with _lock:
        if not _state["pending"]:
            return           # 最后一秒被取消了
    _execute(action)


def _execute(action):
    """执行动作。关机特殊：执行后继续保持可取消状态（系统层 60 秒窗口）。"""
    if action == "sound":
        _do_sound()
        with _lock:
            _state.update(pending=False, left=0, fired=False)
        return
    if action == "lock":
        _do_lock()
        with _lock:
            _state.update(pending=False, left=0, fired=False)
        return
    if action == "shutdown":
        _do_shutdown()
        # 指令已发：继续保持 pending，让 UI 的「取消」按钮还能救回来
        with _lock:
            _state.update(pending=True, left=SHUTDOWN_OS_DELAY, fired=True)
        return


def cancel():
    """
    取消待执行的收尾动作。返回 (取消了什么, 消息)。

    · 倒计时中 -> 停表，不执行
    · 关机指令已发 -> 发 shutdown /a 撤销
    · 什么都没排 -> 原样返回
    """
    with _lock:
        was_pending = _state["pending"]
        action = _state["action"]
        fired = _state["fired"]
    _cancel_evt.set()          # 打断倒计时线程（如果还在跑）
    if fired:
        ok = _do_abort()
        with _lock:
            _state.update(pending=False, left=0, fired=False)
        if ok:
            return True, "已取消关机（系统关机指令已撤销）"
        return False, "取消失败：请手动在命令行运行 shutdown /a"
    if was_pending:
        with _lock:
            _state.update(pending=False, left=0, fired=False)
        return True, "已取消待执行的「%s」" % _label(action)
    return False, "当前没有待执行的动作"


def reset():
    """测试用：清空全部状态（不碰系统）。"""
    _cancel_evt.set()
    with _lock:
        _state.update(pending=False, action="none", left=0, fired=False)
    _cancel_evt.clear()
