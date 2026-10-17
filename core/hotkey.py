# -*- coding: utf-8 -*-
"""
core/hotkey.py —— 全局热键监听

用 RegisterHotKey + 独立线程的 PeekMessage 循环实现。
线程里必须用 PeekMessage 而不是 GetMessage，因为需要能随时退出。
"""

import ctypes
import ctypes.wintypes as wt

_user32 = ctypes.windll.user32

# 修饰键组合
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # 关键：按住不放时不要反复触发

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# ---- 具名按键 -> 虚拟键码 ----
# 【为什么要有这张表】parse_key 原来只认 F1~F24 和单个字母，
# 于是空格、Tab、回车、小键盘、方向键区扩展键全都解析不了，
# 表现为「按了那个键但注册失败」。用户要绑的键是不固定的，
# 只能把常见的都列出来。
#
# 命名统一小写，调用方parse_key 里已经 lower() 过。
VK_NAMES = {
    # 编辑类
    "space": 0x20, "spacebar": 0x20,
    "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    # 导航类
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "pgdown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    # 锁定 / 系统类
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "break": 0x13,
    # 小键盘
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63,
    "num4": 0x64, "num5": 0x65, "num6": 0x66, "num7": 0x67,
    "num8": 0x68, "num9": 0x69,
    "add": 0x6B, "subtract": 0x6D, "sub": 0x6D,
    "multiply": 0x6A, "mul": 0x6A, "divide": 0x6F, "div": 0x6F,
    "decimal": 0x6E,
}

# 界面/日志里显示用的名字（大写，和按键帽一致）
VK_DISPLAY = {
    0x20: "Space", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc",
    0x08: "Backspace", 0x2E: "Delete", 0x2D: "Insert",
    0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown",
    0x26: "Up", 0x28: "Down", 0x25: "Left", 0x27: "Right",
    0x14: "CapsLock", 0x90: "NumLock", 0x91: "ScrollLock",
    0x2C: "PrintScreen", 0x13: "Pause",
    0x6B: "Numpad +", 0x6D: "Numpad -", 0x6A: "Numpad *",
    0x6F: "Numpad /", 0x6E: "Numpad .",
}
for _i in range(10):
    VK_DISPLAY[0x60 + _i] = "Numpad %d" % _i


def pretty_key(text):
    """把 'ctrl+shift+f8' 显示成 'Ctrl + Shift + F8'，用于界面按键帽。"""
    if not text:
        return ""
    out = []
    for part in str(text).split("+"):
        p = part.strip()
        if not p:
            continue
        low = p.lower()
        if low in ("ctrl", "control"):
            out.append("Ctrl")
        elif low == "shift":
            out.append("Shift")
        elif low == "alt":
            out.append("Alt")
        elif low in ("win", "meta", "super"):
            out.append("Win")
        elif low.startswith("f") and low[1:].isdigit():
            out.append("F" + low[1:])
        elif low in VK_NAMES:
            vk = VK_NAMES[low]
            out.append(VK_DISPLAY.get(vk, p.capitalize()))
        else:
            out.append(p.upper() if len(p) == 1 else p.capitalize())
    return " + ".join(out)


def is_panic_key(text):
    """是不是紧急释放键（F12）。绑成触发键会和紧急释放抢同一个键。"""
    if not text:
        return False
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    return "f12" in parts


class HotkeyError(Exception):
    """热键注册失败（一般是被别的软件占用了）。"""


def parse_key(text):
    """
    把 'Ctrl+Shift+F8' 这样的字符串解析成 (修饰键掩码, 虚拟键码)。

    支持：
      · F1~F24
      · A~Z、0~9
      · 具名键（Space / Tab / Enter / Esc / Home / PageUp / Numpad0 ...）
      · ctrl / shift / alt / win 修饰键（顺序任意）
    """
    if not text:
        return None
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    mods = 0
    key = None
    for part in parts:
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("alt",):
            mods |= MOD_ALT
        elif part in ("win", "meta", "super"):
            mods |= MOD_WIN
        elif part.startswith("f") and part[1:].isdigit():
            n = int(part[1:])
            if 1 <= n <= 24:
                # F1~F24 的虚拟键码就是 0x70 + (n-1)
                key = 0x6F + n
            else:
                return None
        elif part in VK_NAMES:
            key = VK_NAMES[part]
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            key = ord(part.upper())
        else:
            return None
    if key is None:
        return None
    mods |= MOD_NOREPEAT
    return mods, key


# ---------------------------------------------------------------- 错误上报
# 回调异常**绝不能**穿出监听线程：_boot 里是
#     while GetMessageW(...):  self.callback()   finally: UnregisterHotKey(...)
# 异常会直接从 while 里炸出去，finally 顺手把热键注销掉、线程结束 ——
# 表现为「按一次热键之后它就永久失效了」，而且界面上没有任何提示。
# 2026-10-16 实测：dashboard.toggle_pause 里一个模块名拼错（pausctl）
# 就让全局暂停热键从上线起就没工作过，还按一次死一次。
# dashboard.py 在 main() 里把 _log_exc 挂进来（core 不能反向 import）。
_ERROR_HOOK = None


def set_error_logger(fn):
    """挂一个「记录异常」的回调（一般是 dashboard 的 _log_exc）。"""
    global _ERROR_HOOK
    _ERROR_HOOK = fn


def _invoke(callback):
    """跑一次热键回调，异常一律兜住并上报。"""
    try:
        callback()
    except Exception:
        import traceback
        traceback.print_exc()
        if _ERROR_HOOK is not None:
            try:
                _ERROR_HOOK("热键回调")
            except Exception:
                pass


class HotkeyListener(object):
    """
    全局热键监听器。

    用法：
        hl = HotkeyListener("F8", callback)
        hl.start()
        ...
        hl.stop()
    """

    HOTKEY_ID = 0xBEEF

    def __init__(self, key_text, callback):
        self.callback = callback
        self._thread = None
        self._tid = None
        self._ok = False
        parsed = parse_key(key_text)
        if not parsed:
            raise HotkeyError("无法识别的按键: %r" % (key_text,))
        self._mods, self._vk = parsed
        self._key_text = key_text

    def start(self):
        """注册并启动监听线程。返回 (成功, 错误信息)。"""
        import threading

        if self._thread and self._thread.is_alive():
            return True, ""
        result = {}
        # 【性能坑】原来这里用 self._thread.join(timeout=1.0) 等注册完成，
        # 但注册成功后线程会进入**阻塞的** GetMessageW 循环、永远不返回，
        # 所以 join 每次都必然等满整个 timeout —— 注册两个热键就白等 2 秒，
        # 表现为「双击 exe 后卡很久才出窗口，标题栏还是『未响应』」。
        # 正确做法：用 Event 让线程在 RegisterHotKey 完成后立刻 signal，
        # 只等「注册」这一瞬间，不等它进消息循环。
        ready = threading.Event()

        def _boot():
            # 关键：hWnd 传NULL 时， WM_HOTKEY 会被投递给「调用 RegisterHotKey 的
            # 那个线程」，所以 tid 必须记这个线程的，不能记主线程的。
            self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
            if not _user32.RegisterHotKey(None, self.HOTKEY_ID, self._mods, self._vk):
                result["err"] = (
                    "热键 %s 注册失败，可能被其他软件占用（常见于游戏或远程控制软件）"
                    % self._key_text
                )
                ready.set()
                return
            result["ok"] = True
            ready.set()   # 只通知「注册好了」，不关心后面循环何时结束
            msg = wt.MSG()
            try:
                # 必须用会阻塞的 GetMessageW —— PeekMessage 抓不到这类线程消息。
                # 退出靠别的线程 PostThreadMessage 发 WM_QUIT。
                while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY:
                        if self.callback:
                            # 走 _invoke：回调抛异常不能带走监听线程
                            _invoke(self.callback)
                    elif msg.message == WM_QUIT:
                        break
            finally:
                _user32.UnregisterHotKey(None, self.HOTKEY_ID)

        self._thread = threading.Thread(target=_boot, name="hotkey")
        self._thread.daemon = True
        self._thread.start()
        # 等「注册完成」这个瞬间。0.5s 足够覆盖线程调度，
        # 超时也只是当没注册上，不会像原来那样白等 1 秒。
        ready.wait(timeout=0.5)
        if result.get("ok"):
            self._ok = True
            return True, ""
        self._ok = False
        return False, result.get("err", "热键注册失败")

    def stop(self):
        if self._thread and self._thread.is_alive():
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            # 这里 join 是合理的：WM_QUIT 已发出，线程马上就会退出循环。
            # 超时从 1.0s 收到 0.3s——退出本该是毫秒级，等 1 秒只是保险；
            # 而 shutdown 里会串行调stop()，两个热键就是 2 秒卡在退出上。
            self._thread.join(timeout=0.3)
        self._ok = False