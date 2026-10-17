# -*- coding: utf-8 -*-
"""
core/keyboard.py —— 键盘钩子 + 长按锁定

要实现的效果（主人描述的）：
    长按 W 键 2 秒 -> 进入「锁定」，之后一直按着 W（物理松开也不影响）
    再次按W -> 解除锁定，W 恢复正常
    没设置的键（比如 A）-> 不触发任何行为

技术要点（这是能不能做成的关键）：
  **实现方式是「成对注入 down+up」，不是「只注入 down 再吞掉 keyup」。**

  后者（吞 keyup）试过，是错的：
    - 低级钩子只在事件链上拦截，**改不了键盘硬件的真实状态**；
    - Windows 会为注入的 keydown 自动补配对的 keyup 回环；
    - 关键：Win11 新版记事本这类WinUI/WebView2 应用干脆不响应纯 keydown；
  结果就是「状态机显示已锁定，但屏幕一个字符都不动」。

  现在的做法（AutoHotkey / pyautogery 的标准方案）：
    以 hz 频率成对注入 down + up（中间不插延时）。
    应用每收到一对完整的按下/抬起，就认为是一次完整按键动作，
    连续 30 次/秒就会稳定地重复触发。
  所以钩子**不吞任何事件**，只负责识别状态 + 判断阈值。
  「一直按着」的效果来自高频成对注入，而不是拦住什么。

  另一个要点：钩子回调里抛异常等于事件链断裂，
  键盘会出现「打字打不出来」的故障，所以回调必须包 try 兜底。

生命周期：
  监听线程调 SetWindowsHookEx -> 钩子回调在监听线程被调用（低级钩子）->
  回调里判断：是否命中目标键 / 是否已超阈值 / 是否是「再次按下」-> 决定动作
"""

import ctypes
import ctypes.wintypes as wt
import threading
import time

from . import pausectl          # 全局暂停开关（与 dashboard 共用一个标志）

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# --------------------------------------------------------------------------
# 必须声明 Win32 函数的精确签名，否则会出「int too long to convert」崩溃
# --------------------------------------------------------------------------
# 原因：ctypes 对未声明签名的函数，默认用 C int（32 位）传参。
# 而 lparam / LRESULT 在 64 位 Windows 上是 64 位，地址值塞进 32 位 int 必然溢出。
# 溢出发生在 CallNextHookEx 上 -> 钩子回调每次都抛异常 -> 事件无法传递 ->
# 状态卡在 pressing、键盘无法正常使用。这个坑非常隐蔽，必须声明签名。
_WPARAM = ctypes.c_ulonglong
_LPARAM = ctypes.c_longlong
_LRESULT = ctypes.c_longlong
_HHOOK = ctypes.c_void_p

# 钩子回调签名：必须是 LRESULT(有符号 long) + 返回 LRESULT
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

_user32.CallNextHookEx.argtypes = [_HHOOK, ctypes.c_int, _WPARAM, _LPARAM]
_user32.CallNextHookEx.restype = _LRESULT

_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, _HHOOK, wt.DWORD]
_user32.SetWindowsHookExW.restype = _HHOOK

_user32.UnhookWindowsHookEx.argtypes = [_HHOOK]
_user32.UnhookWindowsHookEx.restype = wt.BOOL

_user32.GetMessageW.argtypes = [ctypes.c_void_p, _HHOOK, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = wt.BOOL

_user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, _WPARAM, _LPARAM]
_user32.PostThreadMessageW.restype = wt.BOOL

_user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_user32.MapVirtualKeyW.restype = wt.UINT

_user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
_user32.SendInput.restype = wt.UINT

_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = _HHOOK

# 钩子消息
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# 钩子类型：低级键盘钩子（能全局拦截，且在事件到达目标前触发）
WH_KEYBOARD_LL = 13

# 虚拟键码 -> 可读名称（常用键）
VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift",
    0x11: "Ctrl", 0x12: "Alt", 0x13: "Pause", 0x14: "CapsLock",
    0x1B: "Esc", 0x20: "Space", 0x25: "Left", 0x26: "Up",
    0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen",
    0x2D: "Insert", 0x2E: "Delete", 0x5B: "LWin", 0x5C: "RWin",
    0x5D: "Apps", 0x90: "NumLock", 0x91: "ScrollLock",
}
for _i in range(1, 25):                       # F1~F24
    VK_NAMES[0x6F + _i] = "F%d" % _i
for _c in range(ord("A"), ord("Z") + 1):        # A~Z
    VK_NAMES[_c] = chr(_c)
for _d in range(10):                            # 0~9
    VK_NAMES[0x30 + _d] = str(_d)

# 名称 -> 虚拟键码（与上面互逆）
# 注意：key 一律存小写，因为 key_to_vk 会先 lower() 再查。
# F1~F24 在 VK_NAMES 里是大写 "F5"，若直接进这张表会导致 key_to_vk("f5") 查不到。
NAME_TO_VK = {name.lower(): vk for vk, name in VK_NAMES.items()}
# 小写别名，方便用户填 w / space / enter
NAME_TO_VK.update({
    "space": 0x20, "spacebar": 0x20, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "backspace": 0x08,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "del": 0x2E, "delete": 0x2E, "ins": 0x2D, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "lwin": 0x5B, "rwin": 0x5C, "win": 0x5B, "super": 0x5B,
    "capslock": 0x14, "numlock": 0x90, "printscreen": 0x2C,
})


def key_to_vk(name):
    """
    把用户输入的键名转成虚拟键码。
    支持 'w' 'W' 'space' 'f5' 'A' '1' 以及 'w,a,space' 这种多键写法。
    失败返回 None。
    """
    if name is None:
        return None
    n = str(name).strip()
    if not n:
        return None
    low = n.lower()
    if low in NAME_TO_VK:
        return NAME_TO_VK[low]
    if len(n) == 1:
        if n.isdigit():
            return ord(n)
        if n.isalpha():
            return ord(n.upper())
    return None


def parse_keys(text):
    """
    解析多个键：'w' / 'w,a' / 'w a space' 都行。
    返回虚拟键码列表，无法识别的键会被跳过。
    """
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        items = [str(x) for x in text]
    else:
        items = [x for x in str(text).replace(",", " ").split() if x]
    out = []
    for item in items:
        vk = key_to_vk(item)
        if vk and vk not in out:
            out.append(vk)
    return out


def vk_to_name(vk):
    return VK_NAMES.get(vk, "VK_%02X" % vk)


# --------------------------------------------------------------------------
# 键盘事件注入（keydown/keyup）
# --------------------------------------------------------------------------
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


# ======================================================================
# INPUT / KEYBDINPUT 定义
# ----------------------------------------------------------------------
# 直接复用 inputs 里的定义，不要在本文件另写一份。
# 【为什么必须复用】INPUT 的联合体里挂的是 inputs.KEYBDINPUT 实例，
# 如果keyboard.py 用自己定义的另一个同名结构体，赋值会报
#「incompatible types, KEYBDINPUT instance instead of KEYBDINPUT instance」。
# 而历史上这里正是踩了更隐蔽的坑：
# 早期 inputs.INPUT 的联合体只写了鼠标成员 mi，没有 ki，
# 导致 ev.ki = KEYBDINPUT(...) **不报错**（ctypes 把它当成给 Python
# 对象挂了个临时属性），数据一个字节都没写进结构体内存。
# 于是每次 SendInput 发出去的都是「键盘类型 + 空按键」，
# SendInput 一直返回成功，而目标程序永远收不到按键。
# 这是「注入统计 100% 成功但屏幕一个字都不刷」的真正根因。
# 现在 inputs.INPUT 的联合体已补全 mi/ki/hi，此坑已封。
INPUT_KEYBOARD = 1

try:
    from .inputs import INPUT as _FULL_INPUT
    from .inputs import KEYBDINPUT
except ImportError:            # 允许单文件直接运行
    from inputs import INPUT as _FULL_INPUT
    from inputs import KEYBDINPUT

_CB_SIZE = ctypes.sizeof(_FULL_INPUT)

# MapVirtualKey：虚拟键码 -> 扫描码
_mapvk = _user32.MapVirtualKeyW

#注入事件的专属标记，写在 dwExtraInfo 里。
# 为什么必须标记：注入的 keydown 会被系统派发回来、再经过低级键盘钩子。
# 如果不识别，程序自己注入的 keydown 会被当成「用户又按了一次」，
# 触发「锁定中再次按下 -> 解除锁定」，结果就是刚锁定就自己解开。
INJECT_MARK = 0x4D414331  # "MAC1"

LLKHF_INJECTED = 0x00000010

# 记录最近一次 SendInput 失败的错误码，供排查用
# （不抛异常，免得打断连点循环；否则界面上看不出「什么都没发生」）
_LAST_SEND_ERROR = [0]

# 注入成败统计：total / ok / fail
_INJECT_STAT = {"total": 0, "ok": 0, "fail": 0}


def last_send_error():
    return _LAST_SEND_ERROR[0]


def inject_stat():
    """返回注入统计副本：{'total': 总次数, 'ok': 成功, 'fail': 失败}"""
    return dict(_INJECT_STAT)


def reset_inject_stat():
    _INJECT_STAT["total"] = 0
    _INJECT_STAT["ok"] = 0
    _INJECT_STAT["fail"] = 0


def send_key(vk, up=False, mark_injected=False, use_scancode=True):
    """
    注入一次键盘事件。up=True 表示抬起，否则按下。

    mark_injected=True 时打上专属标记，让钩子能认出「这是程序自己注入的」并忽略。

    use_scancode=True 时按扫描码注入（更底层，游戏/部分程序认这个）；
    传 False 则只用虚拟键码（wVk 生效、wScan 置 0）。

    【关键坑，踩了很久】KEYEVENTF_SCANCODE 模式下 **wVk 必须置 0**。
    之前写成 wVk=vk 且 wScan=scan 同时填，属于非法用法：
    wVk 是被忽略的残留字段，会让 TranslateMessage / ToUnicode
    拿扫描码转换时算出错误的键，最终表现是
    「WM_KEYDOWN 能收到（注入确实到了）但永远出不来 WM_CHAR」
    —— 记事本不刷字就是这个原因。
    两种模式只能二选一，不能同时填。
    """
    if use_scancode:
        scan = _mapvk(vk, 0)
        flags = KEYEVENTF_SCANCODE
        vk_out = 0        # 扫描码模式下 wVk 必须为 0
    else:
        # 不带 SCANCODE 时 wVk 生效，wScan 必须为 0
        scan = 0
        flags = 0
        vk_out = vk
    if up:
        flags |= KEYEVENTF_KEYUP
    extra = INJECT_MARK if mark_injected else 0
    ev = _FULL_INPUT(type=INPUT_KEYBOARD)
    ev.ki = KEYBDINPUT(wVk=vk_out, wScan=scan, dwFlags=flags, time=0,
                       dwExtraInfo=ULONG_PTR(extra))
    sent = _user32.SendInput(1, ctypes.byref(ev), _CB_SIZE)
    _INJECT_STAT["total"] += 1
    if sent == 0:
        _LAST_SEND_ERROR[0] = _kernel32.GetLastError()
        _INJECT_STAT["fail"] += 1
    else:
        _INJECT_STAT["ok"] += 1
    return sent > 0


# --------------------------------------------------------------------------
# 低级键盘钩子
# --------------------------------------------------------------------------
# HOOKPROC 类型已在文件开头定义（SetWindowsHookExW 的 argtypes 需要它），
# 这里不再重复定义。


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


def _hook_module_handle():
    """
    取 SetWindowsHookEx 的 hMod 参数。

    实测结论（这台机器的精简版Python 3.13）：
      - hMod = NULL                 -> 安装成功，可正常收到按键
      - hMod = exe 模块句柄          -> 失败，错误 126「模块无效」
      - hMod = python313.dll 句柄-> 失败，错误 126（该精简版没有独立 python DLL）
    所以固定用 NULL，这是低级键盘钩子在托管解释器里的正确写法。
    """
    return None


class KeyHoldLock(object):
    """
    长按锁定器。

    用法：
        lock = KeyHoldLock(keys=['w'], threshold_sec=2.0)
        lock.start()
        ...
        lock.stop()

    参数
    ----
    keys          : 目标键列表，如 ['w'] 或 'w,a'（只有这些键有锁定效果）
    threshold_sec : 长按多少秒后进入锁定，默认 2.0 秒
    repeat_hz     : 锁定后注入的频率（次/秒），默认 30
                    这个值决定「刷字有多快」：
                    每秒成对注入 30 次= 每秒触发 30 次按键动作。
                    调到 50~60 更快但更吃 CPU。

    行为
    ----
    - 未设置的键：完全不影响，正常透传
    - 目标键按住达到阈值：进入锁定，以 hz 频率成对注入 down+up
    - 锁定中物理松开/再按下：keyup 正常透传；再按下视为解除指令
    - 解除时补一个 keyup 收尾，键盘完全恢复正常
    """

    def __init__(self, keys=None, threshold_sec=2.0, repeat_hz=30,
                 on_state_change=None, panic_key="f12", use_scancode=True,
                 hold_down=True, pair_gap_ms=20):
        """
        参数
        ----
        keys          : 目标键列表，如['w'] 或 'w,a'（只有这些键有锁定效果）
        threshold_sec : 长按多少秒后进入锁定，默认 2.0 秒
        repeat_hz     : 成对模式下注入的频率（次/秒），默认 30。
                        只在 hold_down=False 时有意义。
        panic_key     : 紧急释放键，默认 F12。任何时候按它都会强制解除全部锁定
                        并释放所有按键，是防「键盘卡住」的安全阀。
        hold_down     : **核心行为开关**，默认 True。
                        True  = 锁定时重复注入 keydown 且永不注入 keyup，
                                 目标程序看到「一直按住」，游戏角色会持续移动。
                        False = 反复注入「按下+抬起」配对，
                                 目标程序看到「连续点击多次」，
                                 表现是反复点击而不是一直按着。
        pair_gap_ms   : 成对模式下「按下」与「抬起」之间的间隔（毫秒），默认 20。
                        **这是「反复点击」能否生效的决定性参数**，见_inject_loop 注释。
        """
        self.keys = parse_keys(keys) if keys else []
        self.threshold_sec = float(threshold_sec)
        self.use_scancode = bool(use_scancode)
        self.hold_down = bool(hold_down)
        self.repeat_hz = max(1, int(repeat_hz))
        self.pair_gap_ms = max(0, int(pair_gap_ms))
        self.on_state_change = on_state_change
        # 紧急释放键：必须是独立按键，不能和目标键相同，否则按它反而触发锁定
        self.panic_vk = key_to_vk(panic_key) if panic_key else None
        if self.panic_vk in self.keys:
            # 用户把紧急键设成了目标键，追加一个 F12 兜底
            self.panic_vk = key_to_vk("f12")

        self._thread = None
        self._hook_id = None
        self._thread_id = None
        self._running = False
        self._lock = threading.Lock()

        # 每个目标键的状态：'idle' / 'pressing'（按下但未到阈值）/ 'locked'
        self._state = {vk: "idle" for vk in self.keys}
        self._press_start = {}
        # 记录用户是否「物理松开过」该键。
        # 只有松开过之后的再次按下，才算「解除锁定」的指令；
        # 长按产生的自动重复 keydown 不算，否则刚锁定就被自己解除。
        self._physically_up = {vk: True for vk in self.keys}
        # 「刚被解除、且用户还没松开」的保护标志。
        # 见 _dispatch 里down 分支的说明：用来挡住解除后自动重复 keydown
        # 造成的「刚解除又自动锁回去」。
        self._just_unlocked = {vk: False for vk in self.keys}
        self._active_key = None      # 当前处于 locked 的键（同一时刻只锁一个）
        self._stop_inject = False
        self._panic_count = 0        # 紧急释放触发次数

        self._hook_proc = HOOKPROC(self._hook_callback)

    # -- 紧急释放（安全阀）------------------------------------------------
    def panic_release(self):
        """
        强制解除所有锁定，并释放所有目标键。

        这是防「键盘卡住」的最后一道保险：
        - 不看状态机当前是什么，直接把状态清干净
        - 对每个目标键补发 keyup，让系统层面不留「按着」的残留状态
        - 紧急键本身绝不会被吞，按下就一定生效
        """
        with self._lock:
            was_locked = self._active_key
            self._clear_lock_state_locked()
            self._panic_count += 1
            targets = list(self._state.keys())

        # 锁外注入：send_key 会触发 SendInput，注入的按键又回调钩子拿锁
        for vk in targets:
            try:
                send_key(vk, up=True, mark_injected=True,
                         use_scancode=self.use_scancode)   # 强制补一次抬起
            except Exception:
                pass

        if was_locked is not None:
            self._notify(False, was_locked)
        return was_locked is not None

    def _state_frozen(self):
        """
        状态机是否冻结 —— 键盘区处于暂停时返回 True。

        【为什么单独抽一个方法，而不是在 _dispatch 里直接写
          pausectl.is_paused("key")】
        回归测试要能**精确地只关掉这一层**来复现原始 bug：
        「防退化检查必须能被证伪」——如果打掉所有保护它还全绿，
        说明那条断言根本没在测东西（verify_reported_20261016 的负向对照
        就是靠打桩这个方法 + _clear_lock_state_locked 两层来实现的）。
        判定逻辑仍然只有一处：读 core/pausectl。
        """
        return pausectl.is_paused("key")

    def _clear_lock_state_locked(self):
        """
        把锁定状态清干净，回到「什么都没锁」的初始态。

        **调用方必须已经持有 self._lock**（刻意不做加锁，避免与本方法
        内部的重复加锁造成自锁；也避免 panic_release / 注入循环各自
        复制一份清场逻辑后慢慢跑偏）。

        清四项：
          _state          -> 全部 idle（不再是 locked/pressing）
          _press_start    -> 清空（否则残留的起始时刻会在恢复后立刻凑够阈值）
          _physically_up  -> 全部 True（当作「手已经松开了」）
          _just_unlocked  -> 全部 False
          _active_key     -> None
        """
        for vk in list(self._state.keys()):
            self._state[vk] = "idle"
        self._press_start.clear()
        for vk in self._physically_up:
            self._physically_up[vk] = True
        for vk in self._just_unlocked:
            self._just_unlocked[vk] = False
        self._active_key = None

    @property
    def panic_name(self):
        return vk_to_name(self.panic_vk) if self.panic_vk else "未设置"

    # -- 状态查询（给界面显示用）-----------------------------------------
    @property
    def active(self):
        """当前是否有键处于锁定状态，返回 (是否锁定, 键名)。"""
        with self._lock:
            if self._active_key is not None:
                return True, vk_to_name(self._active_key)
        return False, ""

    def state_of(self, vk):
        with self._lock:
            return self._state.get(vk, "idle")

    # -- 启动 / 停止 -------------------------------------------------------
    def start(self):
        """安装钩子并启动注入线程。返回 (成功, 错误信息)。"""
        if not self.keys:
            return False, "没有设置目标按键"
        if self._running:
            return True, ""

        ready = {}
        lock_ready = threading.Event()

        def _hook_thread():
            #低级钩子必须在有消息循环的线程里，且回调在此线程执行
            self._thread_id = _kernel32.GetCurrentThreadId()
            self._hook_id = _user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._hook_proc, _hook_module_handle(), 0
            )
            if not self._hook_id:
                ready["err"] = ("SetWindowsHookEx 失败，错误码 %d"
                                % _kernel32.GetLastError())
                lock_ready.set()
                return
            ready["ok"] = True
            lock_ready.set()

            msg = wt.MSG()
            # GetMessageW 阻塞等待消息；钩子回调由系统在派发消息时触发
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0012:  # WM_QUIT
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            if self._hook_id:
                _user32.UnhookWindowsHookEx(self._hook_id)
                self._hook_id = None

        self._running = True
        self._stop_inject = False
        self._thread = threading.Thread(target=_hook_thread, name="kbdhook", daemon=True)
        self._thread.start()
        lock_ready.wait(timeout=3.0)

        if not ready.get("ok"):
            self._running = False
            return False, ready.get("err", "钩子安装失败")

        # 启动注入线程
        threading.Thread(target=self._inject_loop, name="kbdinject", daemon=True).start()
        return True, ""

    def stop(self):
        """
        停止钩子，并把锁定的键释放掉。

        一定要释放：进程被杀 / 窗口被关时，系统里可能还残留「某键按下」的状态，
        结果就是键盘局部失灵（打字没反应、某个键一直生效）。
        所以这里对所有目标键都补发一次 keyup 强制复位。
        """
        self._stop_inject = True
        with self._lock:
            targets = list(self._state.keys())
            if self._active_key is not None:
                targets.append(self._active_key)
            for vk in targets:
                self._state[vk] = "idle"
            self._press_start.clear()
            for vk in self._physically_up:
                self._physically_up[vk] = True
            for vk in self._just_unlocked:
                self._just_unlocked[vk] = False
            self._active_key = None

        # 对所有目标键补一次抬起，确保系统层面不留「按着」的残留状态
        for vk in targets:
            try:
                send_key(vk, up=True, mark_injected=True,
                         use_scancode=self.use_scancode)
            except Exception:
                pass

        if self._hook_id and self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._running = False

    # -- 钩子回调（关键：拦keyup）------------------------------------------
    def _hook_callback(self, ncode, wparam, lparam):
        """
        低级键盘钩子回调。

        铁律：任何情况下都必须把事件交给下一个钩子（CallNextHookEx）。
        一旦回调抛异常，事件就传不到目标程序，键盘会出现「打字打不出来」
        「某个键卡住」这类故障。所以整个逻辑包在 try 里，
        任何意外都降级成「透传事件」，绝不吞掉。
        """
        try:
            return self._dispatch(ncode, wparam, lparam)
        except Exception:
            # 兜底：出任何错都放行，宁可功能失效也不能弄坏键盘
            try:
                return _user32.CallNextHookEx(None, ncode, wparam, lparam)
            except Exception:
                return 0

    def _dispatch(self, ncode, wparam, lparam):
        if ncode != 0:      # HC_ACTION 才处理
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        msg = wparam & 0xFFFF
        if msg not in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        try:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = info.vkCode
            extra = info.dwExtraInfo
            injected_flag = bool(info.flags & LLKHF_INJECTED)
        except Exception:
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        # ---- 忽略「程序自己注入的按键」 ----
        # 注入的 keydown 会回到钩子这里，若不识别就会被当成用户又按了一次，
        # 触发「锁定中再次按下」而把锁定自己解开 —— 表现为刚锁定就失效。
        if extra == INJECT_MARK:
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        up = msg in (WM_KEYUP, WM_SYSKEYUP)

        # ---- 紧急释放键：最高优先级，按下立即解除，绝不吞掉 ----
        # 放在所有状态判断之前，保证「卡住了也能救回来」。
        if self.panic_vk is not None and vk == self.panic_vk and down:
            self.panic_release()
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        # ---- 暂停期间：状态机完全冻结（事件照常透传，只是不改状态）----
        # 【2026-10-16 · 与「暂停=解除锁定」配套】
        # 不冻结的话会出现这个反直觉场景：暂停后用户按住 W（想手动走两步），
        # 再按触发键恢复 —— 那一刻 press_start 早就超过阈值，
        # 于是「刚恢复就自己锁上，角色直接跑起来」。用户什么都没做，键就锁了。
        #
        # 冻结之后：暂停期间按目标键等于没按；恢复后必须**重新长按**才会锁定。
        #
        # 位置讲究：必须在拿 self._lock **之前**调用。pausectl 内部有自己一把锁，
        # 两把锁的获取顺序必须固定成「先 pausectl 后 self._lock」，
        # 否则会和 _inject_loop 形成交叉等待。
        if vk in self._state and self._state_frozen():
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        # send_key 会在锁外执行：它触发 SendInput，注入的按键又回调本钩子，
        # 而本钩子要拿 self._lock。锁内调用 = 自己等自己 = 死锁。
        release_keys = []
        notify_unlock = None

        with self._lock:
            # 正在停止中：绝对不吞任何事件，否则用户会「打不出字」
            if self._stop_inject:
                return _user32.CallNextHookEx(None, ncode, wparam, lparam)

            # 不在目标列表里的键 -> 原样透传，绝不影响
            if vk not in self._state:
                return _user32.CallNextHookEx(None, ncode, wparam, lparam)

            now = time.time()

            if down:
                # ---- 「锁定中再次按下」-> 视为解除指令 ----
                # 必须同时满足两个条件才算「再次按下」：
                #   1) 该键当前是 locked
                #   2) 用户「物理松开过」这个键（self._physically_up 为真）
                # 条件 2 是必需的：按住不放时，Windows 会持续发自动重复 keydown，
                # 而锁定状态下我们又会持续注入 keydown。若只看 keydown，
                # 这些重复事件会被误判成「用户又按了一次」，
                # 结果就是刚锁定就自己解开（实测踩过）。
                if (self._state.get(vk) == "locked" and self._active_key == vk
                        and self._physically_up.get(vk)):
                    self._state[vk] = "idle"
                    self._active_key = None
                    # 注意这里置 False 而不是 True：
                    # 用户是用「再次按下」解除的，此刻手指还压着键，
                    # 后面 Windows 还会自动重复发keydown。
                    # 若把physically_up 置True，那些自动重复 keydown
                    # 会被当成又一次「按下」，状态回到pressing 并重新计时，
                    # 表现为「刚解除又自动锁回去」。
                    # 置False 表示：必须等用户真正物理松开、再按下，才算新的长按。
                    self._physically_up[vk] = False
                    self._just_unlocked[vk] = True
                    release_keys.append(vk)      # 锁外补一个抬起，确保应用侧收尾完整
                    notify_unlock = vk
                    # 不吞这个 keydown：用户主动按下就该让应用看到，
                    # 否则「解锁」这个动作本身在应用里是凭空消失的。
                    # 应用会收到这一个真实 keydown + 我们补的 keyup，
                    # 效果就是「这一下也生效了，然后恢复常态」，符合直觉。

                # 注意：这里刻意没有「按了另一个目标键就自动切换锁定」的逻辑。
                # 那会让「锁着 W 时手滑按到旁边的键」直接解除锁定，
                # 而这些键可能压根不在目标列表里（没设置任何锁定效果），
                # 违背「没设置的键完全不影响」这条铁律。
                # 解除只有两条路：再按一次同一个键、或按紧急释放键。

                # 已锁定时收到 keydown（多半是系统自动重复）-> 直接忽略状态变更。
                # 否则会把 locked 打回pressing，破坏锁定。
                # 注意：这里只忽略「状态变更」，事件本身照常透传给应用。
                #
                # 另一种情况：刚被解除、用户手指还压着键。
                # 此时 Windows 会继续发自动重复 keydown，若不加判断，
                # 状态会回到 pressing 并重新计时，过一会儿又自动锁回去
                # （实测踩过，表现为「解除后马上就又锁住了」）。
                # 所以：刚解除（_just_unlocked 为真）且还没物理松开过之前，
                # 一律忽略这些 keydown，不进入新的计时。
                if self._state.get(vk) != "locked" and not (
                        self._just_unlocked.get(vk)
                        and not self._physically_up.get(vk)):
                    # 关键：按住一个键时，Windows 会持续重复发送 keydown（长按加速）。
                    # 若每次都重置 press_start，now-start 永远小于阈值，
                    # 状态就会在 pressing/idle 之间反复跳、永远进不了 locked。
                    # 所以只在「从非按下状态首次进入按下」时记录起始时间。
                    if self._state.get(vk) != "pressing":
                        self._press_start[vk] = now
                        self._state[vk] = "pressing"
                # 按下事件本身透传，让应用知道键按下了

            elif up:
                # 任何一次物理抬起都清掉「刚解除」标志，
                # 之后用户再按下才算一次全新的长按。
                self._just_unlocked[vk] = False
                # 【重要】锁定期的物理 keyup **不吞**，正常透传给应用。
                # 现在的实现靠「成对注入 down+up」来维持持续触发
                # （AutoHotkey 的标准做法），应用每收到一对就当作一次完整按键。
                # 如果这里再吞掉物理 keyup，应用侧就只看到 keydown 序列、
                # 看不到抬起，行为会变得不可预测。
                # 锁定期「一直按着」的效果由注入循环持续发 down+up 实现，
                # 不依赖吞键。
                if self._state.get(vk) == "locked":
                    # 记下「用户确实松开过」，这样下一次按下才算解除指令。
                    # 但不吞这个 keyup —— 正常透传。
                    self._physically_up[vk] = True
                elif self._state.get(vk) == "pressing":
                    # 未达阈值的普通抬起 -> 清回 idle，正常透传
                    self._state[vk] = "idle"
                    self._press_start.pop(vk, None)
                    self._physically_up[vk] = True

        # ---- 锁外执行注入，避免重入死锁 ----
        if release_keys:
            for k in release_keys:
                try:
                    # 补发的 keyup 同样要打标记，否则会回环进钩子
                    send_key(k, up=True, mark_injected=True,
                             use_scancode=self.use_scancode)
                except Exception:
                    pass
        if notify_unlock is not None:
            self._notify(False, notify_unlock)

        # 注意：本钩子**从不吞掉任何事件**，全部透传给应用。
        # 「一直按着」的效果完全靠注入循环持续发 down+up 实现（见 _inject_loop），
        # 而不是因为钩子拦住了什么。
        # 这与 AutoHotkey 的做法一致：注入负责产生动作，钩子只负责识别状态。
        return _user32.CallNextHookEx(None, ncode, wparam, lparam)

    # -- 注入线程（判定阈值 + 锁定期补keydown）--------------------------
    def _inject_loop(self):
        """
        一个线程干两件事：

        1) 判定「按住够久了吗」-> 达标就进入锁定
        2) 锁定期间以 repeat_hz 频率持续注入 keydown  <- 屏幕能刷字的关键

        为什么必须注入（曾踩过大坑）：
          曾以为「物理按键还按着、keyup 已被吞掉，应用那边应该已经是按下状态」
          所以完全不注入。结果状态机正常（显示 locked），但屏幕上一个字都不出来。
          原因：**低级键盘钩子只在事件传递链上拦截，改不了键盘硬件的真实状态。**
          物理松手时驱动确实产生了 keyup（被我们吞了），
          但应用判断「按住」靠的是持续不断的 keydown 流；
          记事本这类程序还会用 GetKeyState 读驱动真实状态，钩子对它完全无效。
          所以必须由我们自己补keydown。

        之前担心的「注入回环导致自我解除」，已由 INJECT_MARK 标记机制解决：
        注入的按键带 dwExtraInfo = INJECT_MARK，钩子识别到就原样透传、不碰状态机。
        所以「注入」和「状态机」可以共存，不必二选一。

        【重要】为什么要「按下-抬起」成对注入，而不是只发 keydown：
          只发 keydown 不发 keyup 的方案（靠吞掉物理 keyup 维持按住）
          在实践中被证明不可靠——Windows 会为注入的 keydown 自动补一个配对的
          keyup 回环，应用看到抬起就停；而部分程序（Win11 新版记事本这类
          WinUI/WebView2 应用）干脆不响应注入的纯 keydown。
          **「按下-抬起」成对注入是 AutoHotkey / pyautogui 的标准做法**：
          应用每收到一对就认为是一次完整的按键动作，稳定地重复生效。
          所以这里每轮都发完整的一对 down + up（up 用裸调用不延迟），
          这是兼容性最好的方案，不依赖钩子吞键。
        """
        inject_interval = 1.0 / max(1, self.repeat_hz)
        interval = max(0.005, inject_interval / 4.0)
        next_inject = 0.0        # 下次注入的时间戳；0 表示还没锁过，可立即注入

        # ------------------------------------------------------------------
        # 【本项目最核心的行为开关】hold_down
        # ------------------------------------------------------------------
        # True  =「按住」模式：进入锁定时注入**一次 keydown**，之后永不注入 keyup。
        #         目标程序看到的是「按键一直处于按下状态」，
        #         游戏里角色会持续向前跑，和真人按住 W 完全一样。
        #
        # False =「成对」模式：按下 + 抬起 配对注入，重复 hz 次/秒。
        #         目标程序每次都看到完整的按下+抬起，
        #         于是判定为「连续点击多次」，而不是「一直按住」。
        #         这就是之前「表现成反复点击 W 而不是一直按着」的原因。
        #
        # 差别不在频率，而在**有没有抬起**。
        # ------------------------------------------------------------------
        holding_sent = False    # 已发出「按下」且尚未发出「抬起」

        # 暂停状态跟踪（只在「刚进入暂停」的那一轮做一次收尾动作）。
        #
        # 【2026-10-16 语义修正 · 主人实测报回】
        # 暂停键盘区 = **松手 + 解除锁定**，两件事必须一起做：
        #   ① 若正「已注入按下、未注入抬起」，补一个 keyup
        #      —— 否则目标程序一直认为键按着，暂停就失去意义（角色还在跑）；
        #   ② 把锁定状态清干净（_clear_lock_state_locked）。
        # 旧实现只做了 ①，② 没做：_state 仍是 locked、_active_key 仍指向该键。
        # 于是用户「暂停 → 再按目标键」时，钩子命中「锁定中再次按下 = 解除指令」
        # 分支，弹出一句莫名其妙的「已解除锁定」——锁定在暂停那一刻就该没了。
        was_paused = False

        while not self._stop_inject:
            time.sleep(interval)
            if self._stop_inject:
                break
            now = time.time()

            just_locked = None
            send_down = None        # 需要注入一次 keydown（按住模式）
            send_pair = None        # 需要注入一组 keydown + keyup（成对模式）

            # ---- 暂停门闩（core/pausectl，鼠标/键盘共用一套开关）----
            # 放在「读状态」之后、「注入」之前：暂停期间一个事件都不发。
            # 【分区暂停·2026-10-16】键盘锁定归「键盘区」管：
            # 鼠标连点暂停时前进不能跟着停。全局 Pause 键仍一次停两区。
            #
            # 【语义】暂停 = 解除锁定（不是「冻结住按住状态」）：
            # 进入暂停的那一轮，把按键松开、状态清空，之后按目标键
            # 才是「重新长按」而不是「解除」。
            if pausectl.is_paused("key"):
                if not was_paused:
                    # ---- 第 ① 步：把还按着的键松开（仅在「按住」模式下才有）----
                    release = None
                    with self._lock:
                        if holding_sent and self._active_key is not None:
                            release = self._active_key
                        # ---- 第 ② 步：锁定状态一并清空 ----
                        self._clear_lock_state_locked()
                    holding_sent = False
                    if release is not None:
                        try:
                            send_key(release, up=True, mark_injected=True,
                                     use_scancode=self.use_scancode)
                        except Exception:
                            pass
                was_paused = True
                continue        # 暂停期间一个事件都不注入
            if was_paused:
                was_paused = False
                next_inject = 0.0   # 刚恢复：下一轮立即注入，不空等一个周期

            with self._lock:
                if self._stop_inject:
                    break        # 只有「停止中」才退出线程

                # 绝不能因为「已经锁定」就退出线程。
                # 一旦退出，用户解除后再长按就没人判定阈值 -> 第二次永远锁不上。
                if self._active_key is None:
                    for vk, st in list(self._state.items()):
                        if st != "pressing":
                            continue
                        start = self._press_start.get(vk)
                        if start is not None and (now - start) >= self.threshold_sec:
                            self._state[vk] = "locked"
                            self._active_key = vk
                            # 进入锁定时清掉「已松开」标记：
                            # 此刻物理手指还压着这个键，
                            # 下一个 keydown 只是系统自动重复，不是「再次按下」。
                            self._physically_up[vk] = False
                            just_locked = vk
                            holding_sent = False   # 新一轮锁定，重置按下标记
                            break

                if self._active_key is not None:
                    if self.hold_down:
                        # 按住模式：**只注入 keydown、绝不注入 keyup**，
                        # 但要按频率**重复注入 keydown**。
                        #
                        # 【为什么不只发一次】
                        # 实测：只发一次 keydown，游戏不会持续响应
                        #（表现：状态显示已锁定，但角色不动）。
                        # 原因：真人按住时硬件会持续产生 keydown 重复（约30Hz），
                        # 游戏是靠这个「持续重复」判定为「按住」的。
                        # 单次 keydown 被当成「点了一下」。
                        #
                        # 【为什么不能发 keyup】
                        # 一旦发出 keyup，游戏就认为你松手了 ->
                        # 表现成「反复点击」而不是「一直按着」。
                        #
                        # 所以：重复发 down + 永远不发 up
                        # = 游戏看到「键一直没松」
                        # = 同时还有持续重复事件驱动它响应。
                        if now >= next_inject:
                            send_down = self._active_key
                            next_inject = now + inject_interval
                    else:
                        # 成对模式：按下 + 抬起 配对注入
                        if now >= next_inject:
                            send_pair = self._active_key
                            next_inject = now + inject_interval

            # ---- 锁外执行（send_key 会触发钩子回调拿锁，锁内调用必死锁）----
            if just_locked is not None:
                self._notify(True, just_locked)
            if send_down is not None:
                try:
                    # 注意：只发 keydown，**不发 keyup**
                    send_key(send_down, up=False, mark_injected=True,
                             use_scancode=self.use_scancode)
                    holding_sent = True    # 记下「屏幕上还按着」，暂停时要补 keyup
                except Exception:
                    pass
            elif send_pair is not None:
                try:
                    # ---- 反复点击模式：down 与 up 之间必须有间隔 ----
                    #
                    # 【这是「选了反复点击却没效果」的真正根因】
                    # 之前这里是两个 send_key 背靠背调用，中间零间隔，
                    # 两条注入事件几乎同时到达目标程序。
                    # 目标程序处理按键的典型模型是：
                    #     keydown -> 记录「这个键按下了」
                    #     keyup-> **在这个状态下**执行一次动作
                    # 零间隔时，keyup 到达时按键状态已经被 keydown 改过，
                    # 但很多程序要求「按下持续 N 毫秒」才算一次有效点击，
                    # 于是直接被丢弃 —— 表现就是「一直按着有效，反复点击完全没反应」。
                    #
                    # 参考值：AutoHotkey 的 ClickDelay 默认 10ms，
                    # pyautogui 的 CLICK_INTERVAL 也是 0.1s 量级。
                    # 取 20ms 是「足够被识别为一次点击」与「不至于太迟钝」的折中。
                    send_key(send_pair, up=False, mark_injected=True,
                             use_scancode=self.use_scancode)
                    # 下压时长：夹在「注入间隔」之内，保证频率不被拖慢
                    gap = min(self.pair_gap_ms / 1000.0, inject_interval * 0.8)
                    if gap > 0:
                        time.sleep(gap)
                    send_key(send_pair, up=True, mark_injected=True,
                             use_scancode=self.use_scancode)
                except Exception:
                    pass


    def _notify(self, locked, vk):
        if self.on_state_change:
            try:
                self.on_state_change(locked, vk_to_name(vk))
            except Exception:
                pass