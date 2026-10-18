# -*- coding: utf-8 -*-
"""
core/recorder.py —— 宏录制器（v27 / S2）

用 Windows 低级钩子（WH_KEYBOARD_LL / WH_MOUSE_LL）把真实的键鼠操作
录成一条时间轴事件表，供 plugins/macro_replay.py 按原时序重放。

设计上三条硬要求，每条都对应一个曾经想当然会踩的坑：

1. **必须过滤掉自己注入的事件**
   本程序自己的连点/锁定/回放都走 SendInput，注入的事件会被低级钩子
   原样收回来。不过滤的话：录制时后台的连点任务会被录成几百条假动作，
   「先录→回放→再录」更会滚成指数级死循环。
   两道过滤：
     a. `dwExtraInfo == MARK_INJECTED` —— 精确认出「这是本程序发的」，
        任何情况下都必须丢掉（关掉开关也不许录）；
     b. `flags & LLKHF_INJECTED / LLMHF_INJECTED` —— 所有程序发的注入
        一律丢掉（默认开，可用 ignore_injected=False 关掉）。
   为什么两道都要：a 保证自己的注入绝不会回流（安全性），b 保证别的
   宏工具/远程桌面注入不会污染录制（准确性）。但 b 有副作用——
   某些远程桌面、屏幕键盘、部分触屏驱动发的事件也带 INJECTED 标志，
   那种场景要允许用户关掉 b。

2. **钩子回调里绝对不能抛异常**
   回调抛出去会被系统判定为超时并**悄悄摘钩**（还可能让键盘局部失灵），
   而且只有 stderr 一行 "Exception ignored"，界面上完全看不出来。
   所以 _on_key / _on_mouse 整个包 try，兜底 CallNextHookEx 原样透传。

3. **停止时要把「还按着的东西」补一个抬起**
   用户按下左键拖到一半、或者按住 Shift 时按了停止键，
   事件表里就只剩 down 没有 up。回放出来鼠标永远按着 → 拖拽失控。
   所以 stop() 会给所有仍处于按下状态的键和鼠标键补一条 up。

时间轴用的是「有效时长」而不是墙钟：回放时如果中途暂停，
暂停的那几秒不算进时间轴（暂停 = 冻结时间轴，不是快进）。
录制侧没有这个问题，用 perf_counter 差值即可。
"""

import ctypes
import threading
import time
from ctypes import wintypes as wt

# --------------------------------------------------------------------------
# Win32 常量
# --------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_QUIT = 0x0012
HC_ACTION = 0                      # 钩子回调的 ncode：正常动作

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_INJECTED = 0x00000010        # 键盘：事件由 SendInput/keybd_event 注入
LLMHF_INJECTED = 0x00000001        # 鼠标：同上
LLMHF_LOWER_IL_INJECTED = 0x00000002

# 本程序所有注入事件在 dwExtraInfo 里打的标记（与 core/inputs.py、
# core/keyboard.py 用的是同一个值 "MAC1"）。这里不 import 那两个模块，
# 是为了让 recorder 能独立被测试，不必拉起整个输入层。
MARK_INJECTED = 0x4D414331

# 录制上限：超过就自动停，并置 overflow 说明原因。
# 不设上限的后果是鼠标狂甩时事件表无限膨胀，回放会卡到没法用。
MAX_EVENTS = 20000
MAX_SECONDS = 600

# 录制停止键：Pause，外加 ScrollLock 当退路。
# 为什么连 ScrollLock 一起收：程序自己的暂停热键是 Pause，被别的软件
# 占用时会自动降级成 ScrollLock（见 dashboard 的 HOTKEY_PAUSE_FALLBACK）。
# 那种机器上只认 Pause 的话，用户按「暂停键」（其实是 ScrollLock）
# 什么都不会发生 —— 只能切回窗口点按钮。
VK_PAUSE = 0x13
VK_SCROLL = 0x91

# --------------------------------------------------------------------------
# 录制起点模式（v30）
# --------------------------------------------------------------------------
# 【为什么需要「起点」】
# 旧实现点「开始录制」立刻记账，可是窗口是这时候才最小化的：用户从窗口
# 切到目标程序的那几下点击、路上的鼠标移动全被录进去，回放时先乱点两下
# 再进入正题。参考成熟宏工具（Pulover's Macro Creator / MacFlow）的做法，
# 起点应该是「用户主动给的第一个动作」或者「倒数结束」。
#
#   now          点按钮就开始（旧行为，兼容脚本调用）
#   first_click  第一次鼠标按下才作为第 0 步开始
#   countdown    倒数 N 秒后开始（给了切窗口的时间）
#   wait_key     装好钩子但一条都不记，等启停键（F7）才开录 —— v41 默认
#
# 【wait_key 为什么成为默认（2026-10-18 主人实测反馈）】
# 「点开始录制 → 直接就在录」和「按 F7 才录」两种心智模型混在一起时，
# 用户按下 F7 只是把会话收尾了，看到的却是「窗口一闪什么都没发生」。
# 新流程把两件事分开：
#   ① 点「开始录制」= 进入待命（钩子装好、不记账、窗口不最小化）
#   ② 按 F7 = 真的开始录（此刻窗口才让位）
#   ③ 再按 F7 = 暂停记账（窗口回来，步骤可见）
#   ④ 点「停止录制」= 收尾定稿
# 待命期间钩子只做一件轻活（判 hotkey 的 vk 就丢），不会污染事件表。
START_NOW = "now"
START_FIRST_CLICK = "first_click"
START_COUNTDOWN = "countdown"
START_WAIT_KEY = "wait_key"
START_MODES = (START_NOW, START_FIRST_CLICK, START_COUNTDOWN,
               START_WAIT_KEY)

# 等首次点击的上限：超过就自动收场。
# 否则用户点完「开始录制」就走开了，两个低级钩子会一直挂在系统里。
ARM_TIMEOUT = 30.0


# 按下状态到鼠标键名的映射
_MOUSE_DOWN = {
    WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right", WM_MBUTTONDOWN: "middle",
    WM_XBUTTONDOWN: "x",
}
_MOUSE_UP = {
    WM_LBUTTONUP: "left", WM_RBUTTONUP: "right", WM_MBUTTONUP: "middle",
    WM_XBUTTONUP: "x",
}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# x64 下 LONG_PTR 是 8 字节；ctypes.wintypes 里没有 LRESULT，
# 用它代替（v24 托盘模块踩过同一个坑）。
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 \
    else ctypes.c_ulong


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """低级键盘钩子交给回调的结构体。字段顺序不能改。"""

    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    """
    低级鼠标钩子交给回调的结构体。

    【注意 mouseData 的类型是 DWORD 而不是 WORD】
    滚轮事件的 delta 是有符号的（向下滚是负数，存成 0xFFFFFF88 这种），
    取的时候必须按有符号 32 位解释，否则向下滚会变成 +4294967176 格。
    """

    _fields_ = [
        ("pt", POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


# 回调类型：LRESULT CALLBACK (int nCode, WPARAM wParam, LPARAM lParam)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# 传句柄的 API 必须声明 argtypes（见项目硬规则 2：64 位下不声明会被截断）
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE,
                                      wt.DWORD]
_user32.SetWindowsHookExW.restype = wt.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
_user32.UnhookWindowsHookEx.restype = wt.BOOL
_user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
_user32.CallNextHookEx.restype = LRESULT
_user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.TranslateMessage.argtypes = [ctypes.c_void_p]
_user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
_user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.PostThreadMessageW.restype = wt.BOOL
_kernel32.GetCurrentThreadId.restype = wt.DWORD
_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wt.HMODULE


def _module_handle():
    """
    SetWindowsHookEx 的 hMod 参数：**必须是 NULL**。

    【这是本轮最隐蔽的坑，实测踩了】
    低级钩子（WH_KEYBOARD_LL / WH_MOUSE_LL）在托管解释器里必须传 NULL：
      · hMod = NULL            -> 安装成功，正常收到事件
      · hMod = exe 模块句柄     -> 报错 126「模块无效」，或者更糟：
                                  **返回一个非空句柄、看起来装上了，
                                  但回调永远不被调用**（本次就是后者：
                                  诊断脚本里 send_key 全部返回 True，
                                  钩子的 spy 却一条都没收到）
      · hMod = python313.dll   -> 同样 126（精简版解释器没有独立 DLL）
    和 core/keyboard.py 的 _hook_module_handle 保持同一结论。
    """
    return None


def _signed32(v):
    """把 DWORD 按有符号 32 位解释（滚轮 delta / XBUTTON 编号都靠它）。"""
    v = int(v) & 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def to_delta_grid(mouse_data):
    """
    滚轮 mouseData → 格数（WHEEL_DELTA = 120 一格）。

    向下滚一条 120 是 -120，转成 -1；高精度滚轮可能一次 60，
    这时保留小数没意义，四舍五入到最近的整数格（0 就丢掉不记）。
    """
    return int(round(_signed32(mouse_data) / 120.0))


# --------------------------------------------------------------------------
# 录制器
# --------------------------------------------------------------------------
class Recorder(object):
    """
    键鼠录制器。

    用法：
        rec = Recorder()
        ok, err = rec.start()
        ...  用户操作  ...
        events = rec.stop()      # [{"t": 毫秒, "a": "move", ...}, ...]

    事件格式（字段短名是为了让几千条事件的 JSON 别太肥）：
        {"t":12,  "a":"move", "x":100, "y":200}          移动
        {"t":40,  "a":"down", "b":"left", "x":100, "y":200}  按下
        {"t":118, "a":"up",   "b":"left"}                抬起
        {"t":300, "a":"wheel","d":-1}                    滚轮（格，正=上）
        {"t":420, "a":"kd",   "vk":65}                   键盘按下（65='A'）
        {"t":480, "a":"ku",   "vk":65}                   键盘抬起

    down 事件带坐标，是因为回放必须「先搬到那个点再按」——
    只有 up 带坐标没意义（抬起时不该再移动鼠标，会画出一道折线）。
    """

    def __init__(self, ignore_injected=True,
                 stop_vks=(VK_PAUSE, VK_SCROLL),
                 skip_vks=(VK_PAUSE, VK_SCROLL),
                 start_mode=START_NOW, start_delay=3.0):
        self.ignore_injected = bool(ignore_injected)
        self.stop_vks = frozenset(int(v) for v in stop_vks)
        self.skip_vks = frozenset(int(v) for v in skip_vks)
        self.start_mode = start_mode if start_mode in START_MODES \
            else START_NOW
        self.start_delay = float(start_delay or 0)

        self._lock = threading.Lock()
        self._events = []
        self._held_keys = set()        # 当前按下的 vk
        self._held_buttons = set()     # 当前按下的鼠标键
        self._t0 = None
        self._started = False          # 钩子装好了但还没到起点
        self._armed_at = None          # 装上钩子（等起点）的时刻
        self._arm_cancel = threading.Event()
        self._last_move = None         # (x, y)
        self._last_move_t = 0.0

        self._paused = False       # v41：暂停记账（钩子不卸，F7 再按继续）
        self._pause_at = None      # 暂停起点（恢复时把这段时长从时间轴上扣掉）
        self._running = False
        self._thread = None
        self._tid = 0
        self._hook_kb = None
        self._hook_ms = None
        self._error = None
        self.overflow = None           # None / "events" / "seconds"
        self._stop_event = threading.Event()

        # 回调对象必须留引用：ctypes 回调被 GC 掉的话，
        # 系统再调过来就是访问已释放内存（进程直接崩）。
        self._kb_proc_ref = HOOKPROC(self._on_key)
        self._ms_proc_ref = HOOKPROC(self._on_mouse)

    # -- 状态 ---------------------------------------------------------
    @property
    def running(self):
        return self._running

    @property
    def stop_requested(self):
        """
        是否已经收到「停止录制」的请求（用户按了 Pause，或超了上限）。

        钩子线程只负责置位，真正的收尾（补抬起、卸钩子、把事件表交出去）
        必须由界面线程做——在钩子回调里 join 自己会直接死锁。
        """
        return self._stop_event.is_set()

    @property
    def error(self):
        return self._error

    def events(self):
        with self._lock:
            return list(self._events)

    def count(self):
        with self._lock:
            return len(self._events)

    def stats(self):
        """给界面轮询用的快照（不返回事件本体，避免每 500ms 搬几千条）。"""
        with self._lock:
            n = len(self._events)
        secs = 0.0
        if self._started and self._t0 is not None:
            secs = time.perf_counter() - self._t0
        armed = bool(self._running and not self._started)
        waited = 0.0
        if armed and self._armed_at is not None:
            waited = time.perf_counter() - self._armed_at
        return {"recording": bool(self._running), "count": n,
                "secs": round(secs, 1),
                "started": bool(self._started), "armed": armed,
                "waited": round(waited, 1),
                "paused": bool(self._paused),
                "mode": self.start_mode,
                "overflow": self.overflow, "error": self._error}

    # -- 开关 ---------------------------------------------------------
    def start(self, ignore_injected=None, mode=None, delay=None):
        """
        安装钩子开始录制。返回 (成功, 错误信息)。

        mode: now / first_click / countdown。
        first_click 与 countdown 下钩子会立刻装上（否则会漏掉起点那一下），
        但在起点到来之前**一条都不记**——这是「录到的第 0 步就是起点」的前提。
        """
        if self._running:
            return False, "录制已在进行中"
        if ignore_injected is not None:
            self.ignore_injected = bool(ignore_injected)
        if mode in START_MODES:
            self.start_mode = mode
        if delay is not None:
            try:
                self.start_delay = max(0.0, float(delay))
            except (TypeError, ValueError):
                self.start_delay = 0.0

        with self._lock:
            self._events = []
        self._held_keys.clear()
        self._held_buttons.clear()
        self._last_move = None
        self._last_move_t = 0.0
        self._t0 = None
        self._started = (self.start_mode == START_NOW)
        self._armed_at = None
        self._arm_cancel.clear()
        self._error = None
        self.overflow = None
        self._paused = False
        self._pause_at = None
        self._stop_event.clear()

        ready = {}
        gate = threading.Event()

        def _hook_thread():
            # 低级钩子必须在「有消息循环的线程」里装，回调也在该线程执行。
            # 两个钩子（键盘+鼠标）可以装在同一个线程、共用一条消息循环。
            self._tid = _kernel32.GetCurrentThreadId()
            self._hook_kb = _user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._kb_proc_ref, _module_handle(), 0)
            if not self._hook_kb:
                ready["err"] = ("键盘钩子安装失败，错误码 %d"
                                % ctypes.get_last_error())
                gate.set()
                return
            self._hook_ms = _user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._ms_proc_ref, _module_handle(), 0)
            if not self._hook_ms:
                # 半个钩子装上了必须摘掉，否则键盘事件一直被我们截着
                _user32.UnhookWindowsHookEx(self._hook_kb)
                self._hook_kb = None
                ready["err"] = ("鼠标钩子安装失败，错误码 %d"
                                % ctypes.get_last_error())
                gate.set()
                return
            ready["ok"] = True
            gate.set()

            msg = wt.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_QUIT:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            for h in (self._hook_kb, self._hook_ms):
                if h:
                    _user32.UnhookWindowsHookEx(h)
            self._hook_kb = None
            self._hook_ms = None

        self._running = True
        self._thread = threading.Thread(target=_hook_thread, name="macrorec",
                                        daemon=True)
        self._thread.start()
        gate.wait(timeout=3.0)

        if not ready.get("ok"):
            self._running = False
            self._error = ready.get("err", "钩子安装失败（超时）")
            return False, self._error

        if self._started:
            self._t0 = time.perf_counter()
        else:
            self._armed_at = time.perf_counter()
            if self.start_mode == START_COUNTDOWN:
                self._start_countdown(self.start_delay)
            # first_click：什么都不用做，等 feed_mouse 里第一次按下
        return True, ""

    def _start_countdown(self, delay):
        """倒计时起点：到点自动 _begin()。单独线程，不挡界面。"""
        def _later():
            # wait 返回 True 表示被取消（停止/取消录制），此时不能再 _begin
            if not self._arm_cancel.wait(max(0.0, float(delay))):
                self._begin()
        threading.Thread(target=_later, name="macroarm", daemon=True).start()

    def _begin(self):
        """
        把「钩子已装、还没记账」切成「开始记账」。

        第一次鼠标按下 / 倒计时结束 / 立即开始 三条路径最后都走这里。
        加锁是因为倒计时线程和钩子线程可能同时到。
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            self._t0 = time.perf_counter()
        self._armed_at = None
        self._arm_cancel.set()

    # -- 外部控制（v41：F7 三态） --------------------------------------
    def begin(self):
        """
        待命 → 开录。F7 按下时由 dashboard 调。

        返回 True 表示这次调用真的切了状态。已经开录/没在录都返回 False
        ——调用方据此决定要不要提示，避免「按了没反应也不知道为什么」。
        """
        if not self._running or self._started:
            return False
        self._begin()
        return True

    def pause(self):
        """
        暂停记账（F7 第二下）。钩子不卸：卸了再装会漏事件，
        而且暂停期间用户可能还在按着键，卸钩后抬起事件就丢了。

        返回 True 表示这次真的从「录制中」切到了「暂停」。
        """
        if not self._running or not self._started or self._paused:
            return False
        self._flush_held()          # 按着的键先补抬起，避免回放时「一直按住」
        with self._lock:
            self._paused = True
            self._pause_at = time.perf_counter()
        return True

    def resume(self):
        """
        暂停 → 继续录（F7 第三下）。

        时间轴要「扣掉暂停那段」：否则用户暂停去泡杯茶回来，
        第一步和第二步之间会出现几分钟的空等（回放时真等那么久）。
        """
        if not self._running or not self._paused:
            return False
        with self._lock:
            if self._t0 is not None and self._pause_at is not None:
                self._t0 += time.perf_counter() - self._pause_at
            self._paused = False
            self._pause_at = None
        return True

    # -- 录制逻辑（纯函数，探针可以直接喂结构体调） -------------------
    def _now_ms(self):
        """
        相对起点的时间戳。

        起点没到之前返回 0 —— 但那些事件本来也不会被记录（见 feed_*）。
        """
        if not self._started or self._t0 is None:
            return 0
        return int((time.perf_counter() - self._t0) * 1000)

    def stop(self):
        """
        停止录制，返回事件表。

        停下前会检查「有没有还按着的东西」，有就补一条 up（见模块说明第 3 条）。
        """
        if not self._running:
            return self.events()

        # 先掐掉还在等的倒计时/首次点击（否则它会在收尾后又把起点打开）
        self._arm_cancel.set()

        # 再补抬起：此刻用户可能还按着左键/Shift，
        # 不补的话回放出来就是「一直按着」。
        self._flush_held()

        self._stop_event.set()
        if self._tid:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._running = False
        self._thread = None
        return self.events()

    def cancel(self):
        """放弃本次录制（事件表清空）。"""
        self.stop()
        with self._lock:
            self._events = []
        self._held_keys.clear()
        self._held_buttons.clear()

    def _flush_held(self):
        """给所有处于按下状态的键/鼠标键补一条抬起事件。"""
        ts = self._now_ms()
        with self._lock:
            for b in list(self._held_buttons):
                self._events.append({"t": ts, "a": "up", "b": b})
            for vk in list(self._held_keys):
                self._events.append({"t": ts, "a": "ku", "vk": vk})
        self._held_buttons.clear()
        self._held_keys.clear()

    def _feed_gate(self, mouse_down=False):
        """
        起点门闩：返回 True 表示「这一条可以记账」。

        ⚠ 只在 `_running=True`（真的装了钩子）时才拦。
        探针直接调 feed_* 喂结构体时 `_running` 是 False，
        那种场景视作「已经到起点」——否则所有基于喂数据的回归
        都会变成「一条都不记」，看起来像录制器坏了（v30 踩过）。
        """
        if self._started:
            return True
        if not self._running:
            self._begin()
            return True
        if self.start_mode == START_FIRST_CLICK and mouse_down:
            self._begin()
            return True
        return False

    # -- 录制逻辑（纯函数，探针可以直接喂结构体调） -------------------
    def _should_drop_injected(self, extra, flags, flag_bit):
        """
        判断这条事件要不要丢掉。返回 (丢掉?, 该不该自动停录)。

        ⚠ 顺序很重要：**先认自己的标记，再看 INJECTED 总闸**。
        反过来的话，一旦用户关掉 ignore_injected，自己的注入就会
        被录进去，「回放→录制」立刻滚成死循环。
        """
        if int(extra) == MARK_INJECTED:
            return True, False
        if self.ignore_injected and (int(flags) & flag_bit):
            return True, False
        return False, False

    def _overflowed(self, n_events):
        """超限检查。返回 True 表示该停录了。"""
        if n_events >= MAX_EVENTS:
            if self.overflow is None:
                self.overflow = "events"
            self._stop_event.set()
            return True
        if self._t0 is not None and \
                (time.perf_counter() - self._t0) >= MAX_SECONDS:
            if self.overflow is None:
                self.overflow = "seconds"
            self._stop_event.set()
            return True
        return False

    def _push(self, ev):
        with self._lock:
            if self._overflowed(len(self._events)):
                return
            self._events.append(ev)

    def feed_key(self, wparam, info):
        """处理一条键盘事件（钩子回调调它，探针也直接调）。"""
        down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        up = wparam in (WM_KEYUP, WM_SYSKEYUP)
        if not (down or up):
            return

        vk = int(info.vkCode)

        # 停止键：不记录，直接结束录制（用户按 Pause 收工）
        if vk in self.stop_vks:
            if down:
                self._stop_event.set()
            return
        if vk in self.skip_vks:
            return
        # v41 暂停：一条都不记（stop_vks 的判定在上一步，所以暂停中
        # 按 Pause 仍然能收尾——这是「暂停后想直接结束」的退路）。
        if self._paused:
            return

        drop, _ = self._should_drop_injected(info.dwExtraInfo, info.flags,
                                             LLKHF_INJECTED)
        if drop:
            return

        # 起点没到：键盘事件一律不记。
        # 起点只认「鼠标按下」或「倒计时」——键盘按下不算起点，否则
        # 用户切窗口时随手按的键会把起点提前，起点就不再是「第一次点击」。
        if not self._feed_gate(False):
            return

        if down:
            self._held_keys.add(vk)
        else:
            self._held_keys.discard(vk)
        self._push({"t": self._now_ms(), "a": "kd" if down else "ku",
                    "vk": vk})

    def feed_mouse(self, wparam, info):
        """处理一条鼠标事件。"""
        if self._paused:
            return
        x, y = int(info.pt.x), int(info.pt.y)

        drop, _ = self._should_drop_injected(info.dwExtraInfo, info.flags,
                                             LLMHF_INJECTED)
        if drop:
            return

        # ---- 起点门闩（v30）----
        # 起点之前：移动/滚轮/抬起全部丢掉，只有「鼠标按下」能开启录制。
        # 这样录到的第 1 条事件就是用户主动按下的那一下，时间轴从 0 起算。
        if not self._feed_gate(wparam in _MOUSE_DOWN):
            return

        if wparam == WM_MOUSEMOVE:
            # 移动抽样：不抽的话鼠标狂甩一秒能出上千条，
            # 回放时 move_to 会把 CPU 打满还跟不上。
            # 12ms / 4px 双阈值：慢速移动按时间采样，快速移动按距离采样。
            ts = self._now_ms()
            if self._last_move is not None:
                dx = abs(x - self._last_move[0])
                dy = abs(y - self._last_move[1])
                if dx < 4 and dy < 4 and (ts - self._last_move_t) < 12:
                    return
            self._last_move = (x, y)
            self._last_move_t = ts
            self._push({"t": ts, "a": "move", "x": x, "y": y})
            return

        if wparam in _MOUSE_DOWN or wparam in _MOUSE_UP:
            button = _MOUSE_DOWN.get(wparam) or _MOUSE_UP.get(wparam)
            if button == "x":
                # XBUTTON1/2：编号在高 16 位（1 = G1，2 = G2）
                idx = (_signed32(info.mouseData) >> 16) & 0xFFFF
                button = "x%d" % idx if idx in (1, 2) else "x1"
            is_down = wparam in _MOUSE_DOWN
            ev = {"t": self._now_ms(), "a": "down" if is_down else "up",
                  "b": button}
            if is_down:
                # 只有按下带坐标：回放要先搬到那个点再按
                ev["x"], ev["y"] = x, y
                self._held_buttons.add(button)
            else:
                self._held_buttons.discard(button)
            self._push(ev)
            return

        if wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            d = to_delta_grid(info.mouseData)
            if d == 0:
                return          # 高精度滚轮的 0 格抖动，不值得记
            self._push({"t": self._now_ms(),
                        "a": "wheel" if wparam == WM_MOUSEWHEEL else "hwheel",
                        "d": d})

    # -- 钩子回调 -----------------------------------------------------
    def _on_key(self, ncode, wparam, lparam):
        """
        键盘钩子回调。

        【必须返回 CallNextHookEx】返回非 0 等于吞掉这个按键，
        录制期间用户就没法正常打字了。录制只「旁听」，不拦截。
        """
        try:
            if ncode == HC_ACTION and lparam:
                info = ctypes.cast(lparam,
                                   ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                self.feed_key(wparam, info)
        except Exception:
            # 回调里绝不能抛：抛出去会被系统摘钩，而且只有 stderr 一行提示
            pass
        return _user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _on_mouse(self, ncode, wparam, lparam):
        try:
            if ncode == HC_ACTION and lparam:
                info = ctypes.cast(lparam,
                                   ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                self.feed_mouse(wparam, info)
        except Exception:
            pass
        return _user32.CallNextHookEx(None, ncode, wparam, lparam)


# --------------------------------------------------------------------------
# 事件表工具（录制与回放共用；纯函数，便于单测）
# --------------------------------------------------------------------------
def duration_ms(events):
    """事件表总时长（最后一条事件的时间戳）。空表返回 0。"""
    if not events:
        return 0
    return max(0, int(events[-1].get("t", 0)))


def summarize(events, limit=6):
    """
    给界面看的一句话摘要：总共几步、多长、前几步是什么。

    不返回事件全文——几千条事件塞进界面会卡死 webview。
    """
    n = len(events or [])
    if not n:
        return {"count": 0, "secs": 0.0, "clicks": 0, "keys": 0,
                "preview": ""}

    clicks = sum(1 for e in events if e.get("a") == "down"
                 and e.get("b") == "left")
    keys = sum(1 for e in events if e.get("a") == "kd")
    labels = []
    for e in events:
        a = e.get("a")
        if a == "move":
            continue                       # 移动太多，摘要里不列
        if a in ("down", "up"):
            labels.append("%s%s" % ("按下" if a == "down" else "抬起",
                                    e.get("b", "")))
        elif a == "wheel":
            labels.append("滚轮%+d" % int(e.get("d", 0)))
        elif a in ("kd", "ku"):
            labels.append("%sVK%d" % ("按" if a == "kd" else "松",
                                      int(e.get("vk", 0))))
        if len(labels) >= limit:
            break
    more = "" if len(labels) <= limit else " …"
    return {"count": n,
            "secs": round(duration_ms(events) / 1000.0, 1),
            "clicks": clicks, "keys": keys,
            "preview": " → ".join(labels[:limit]) + more}


def validate_events(events):
    """
    读外部文件（用户手改过/跨版本）时的防御性校验。

    返回 (清洗后的事件表, 错误信息)。宁可整条拒绝也不要把脏数据
    喂给回放器——在错误坐标上乱点比加载失败危险得多。
    """
    if not isinstance(events, list):
        return [], "宏数据格式不对（events 必须是列表）"
    out = []
    last_t = 0
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            return [], "第 %d 步不是对象" % (i + 1)
        a = e.get("a")
        if a not in ("move", "down", "up", "wheel", "hwheel", "kd", "ku"):
            return [], "第 %d 步动作未知：%r" % (i + 1, a)
        try:
            t = int(e.get("t", 0))
        except (TypeError, ValueError):
            return [], "第 %d 步时间戳不是整数" % (i + 1)
        # 时间戳必须单调不减，否则回放会出现「负等待」
        t = max(t, last_t)
        last_t = t
        item = {"t": t, "a": a}
        if a in ("move", "down"):
            try:
                item["x"] = int(e["x"])
                item["y"] = int(e["y"])
            except (KeyError, TypeError, ValueError):
                return [], "第 %d 步缺少坐标" % (i + 1)
        if a in ("down", "up"):
            b = str(e.get("b", ""))
            if b not in ("left", "right", "middle", "x1", "x2"):
                return [], "第 %d 步鼠标键名不认识：%r" % (i + 1, b)
            item["b"] = b
        if a in ("wheel", "hwheel"):
            try:
                item["d"] = int(e["d"])
            except (KeyError, TypeError, ValueError):
                return [], "第 %d 步滚轮格数不是整数" % (i + 1)
        if a in ("kd", "ku"):
            try:
                vk = int(e["vk"])
            except (KeyError, TypeError, ValueError):
                return [], "第 %d 步缺少键码" % (i + 1)
            if not (0 < vk < 256):
                return [], "第 %d 步键码越界：%d" % (i + 1, vk)
            item["vk"] = vk
        out.append(item)
    return out, ""
