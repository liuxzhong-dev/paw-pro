# -*- coding: utf-8 -*-
"""
dashboard.py —— 连点/锁定器 单文件仪表盘
========================================

技术选型
--------
界面用 **pywebview（内嵌 Edge/WebView2）+ HTML/CSS**，
不用 tkinter，原因：
  1. 本机 Python 没有 tkinter（实测：托管版和两个 venv 全都没有）
  2. 原生 Win32画不出科幻风（渐变、发光、圆角、响应式网格），
     而这些正是本次需求的核心
  3. 和 VideoSummarizer 同一套技术栈，维护方式一致

架构
----
Python 侧（只有一个线程在做注入）通过 `window.pywebview.api` 暴露给网页：
    start_mouse(params) / stop_mouse()
    start_keyboard(params) / stop_keyboard()
    list_mouse_buttons()      识别本机鼠标按键
    status()状态快照，供前端定时轮询
前端负责所有排版与视觉，Python 只做注入，职责清晰。

**关键约束（踩过坑）**
- 消息/事件循环：pywebview 会自己处理，注入线程一律用 threading，
  不要在窗口线程里跑阻塞循环
- 后台线程回调前端必须通过 `window.evaluate_js`，不能直接改 DOM
- 单文件打包：只写 EXE 不写 COLLECT（见 spec_dashboard.spec）
"""
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes as wt


HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import keyboard as kb            # noqa: E402
from core import engine as eng             # noqa: E402
from core import inputs as inp             # noqa: E402
from core import button_trigger as bt      # noqa: E402
from core import hotkey as hk              # noqa: E402
from core import pausectl                  # noqa: E402
from core import toast                     # noqa: E402
from core import actions                   # noqa: E402  v23 S4 收尾动作
from core import config as cfg             # noqa: E402  v23 S5 方案目录
from core import tray as tray_mod          # noqa: E402  v24 S1 系统托盘


# ======================================================================
# 提权检测（UIPI 权限隔离）
# ======================================================================
# 【为什么需要这个】2026-10-15 实测：原神（YuanShen）以管理员权限运行时，
# 普通权限的本工具 SendInput 注入会被 Windows UIPI **静默拦截**——
# API 照样返回成功、界面照样显示"已启动"，但游戏里一个事件都收不到。
# 两个命令行 exe 用户用了管理员身份所以正常；GUI 双击启动是普通权限
# 所以撞墙——不是 GUI 代码的 bug，是运行方式没配对。
#
# 检测原理：非管理员进程打不开提权进程的 PROCESS_QUERY_INFORMATION
# （OpenProcess 返回 NULL 且错误码 5 = ERROR_ACCESS_DENIED）。
# 注意：本函数只在"我们自己没提权"时有意义（都提权了互相都能打开）。

_PROCESS_QUERY_INFORMATION = 0x0400
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20          # TOKEN_INFORMATION_CLASS.TokenElevation
_ERROR_ACCESS_DENIED = 5

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel.OpenProcess.restype = wt.HANDLE
_kernel.CloseHandle.argtypes = [wt.HANDLE]
_kernel.GetCurrentProcess.restype = wt.HANDLE
_advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                     ctypes.POINTER(wt.HANDLE)]
_advapi.OpenProcessToken.restype = wt.BOOL
_advapi.GetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD,
                                        ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]
_advapi.GetTokenInformation.restype = wt.BOOL


def our_elevated():
    """本进程是否以管理员身份运行。"""
    try:
        tok = wt.HANDLE()
        if not _advapi.OpenProcessToken(_kernel.GetCurrentProcess(),
                                        _TOKEN_QUERY,
                                        ctypes.byref(tok)):
            return False
        try:
            elev = wt.DWORD()
            got = wt.DWORD()
            ok = _advapi.GetTokenInformation(tok, _TOKEN_ELEVATION,
                                             ctypes.byref(elev),
                                             ctypes.sizeof(elev),
                                             ctypes.byref(got))
            return bool(ok and elev.value)
        finally:
            _kernel.CloseHandle(tok)
    except Exception:
        return False


def foreground_pid():
    """当前前台窗口所属进程 PID；取不到返回 None。"""
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = wt.HWND
        user32.GetWindowThreadProcessId.argtypes = [wt.HWND,
                                                    ctypes.POINTER(wt.DWORD)]
        user32.GetWindowThreadProcessId.restype = wt.DWORD
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def process_elevated(pid):
    """
    目标进程是否比普通权限高（提权/保护进程）。

    判据：OpenProcess(PROCESS_QUERY_INFORMATION) 被拒。
    进程刚退出时也会失败（错误码 87），那种不算提权，
    所以必须核对错误码是不是 5（ERROR_ACCESS_DENIED）。
    """
    if not pid:
        return False
    try:
        h = _kernel.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
        if h:
            _kernel.CloseHandle(h)
            return False
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    except Exception:
        return False


def injection_blocked_by_uipi():
    """
    当前前台目标是否"比我们权高"——即注入大概率会被 UIPI 拦。
    我们自己已提权时不存在这个问题（提权可注入任意窗口）。
    """
    if our_elevated():
        return False
    pid = foreground_pid()
    if not pid or pid == os.getpid():
        return False
    return process_elevated(pid)


# ======================================================================
# 鼠标按键表（含侧键，支持自由识别添加）
# ======================================================================
# 标准 Win32 鼠标常量
MOUSE_LEFT = 0
MOUSE_RIGHT = 1
MOUSE_MIDDLE = 2

# XBUTTON：Windows 把侧键统一映射成 4 / 5，游戏鼠标的更多按键靠扩展映射识别
XBUTTON1 = 4
XBUTTON2 = 5

BUILTIN_MOUSE_BUTTONS = [
    (MOUSE_LEFT, "左键", "left"),
    (MOUSE_RIGHT, "右键", "right"),
    (MOUSE_MIDDLE, "中键", "middle"),
    (XBUTTON1, "侧键1（后退）", "xb1"),
    (XBUTTON2, "侧键2（前进）", "xb2"),
]

# 用户自定义识别出来的按键：{label: xbutton_index}
_custom_mouse_buttons = {}
_custom_lock = threading.Lock()

# 界面下拉的 value 是 BUILTIN_MOUSE_BUTTONS 的 id（"0"~"4"），
# 而引擎 _click_once/_mouse_hold 只认 token（left/right/middle/xb1/xb2）。
# 【坑·2026-10-15】历史上没有这层转换：UI 选「左键」传下来是 "0"，
# 一路直达 _click_once → 不是 left/right/middle → 走 _click_xbutton →
# _xbutton_number("0") 抛「Windows 只支持两个侧键」——用户选左键
# 却得到侧键注入失败，界面还照常显示「运行中」，极难排查。
_BUTTON_ID_TO_TOKEN = {"0": "left", "1": "right", "2": "middle",
                       "3": "xb1", "4": "xb2"}


def _normalize_button(value):
    """把界面传来的 button（id 或 token）统一成引擎认识的 token。"""
    token = str(value if value is not None else "").strip().lower()
    if not token:
        return "left"
    if token in _BUTTON_ID_TO_TOKEN:
        return _BUTTON_ID_TO_TOKEN[token]
    if token in ("left", "right", "middle", "xb1", "xb2"):
        return token
    # 自定义键识别出来的是 xb3+（Raw Input 编号），原样放行，
    # 让 _xbutton_number 去抛「G3 以上 SendInput 发不了」的明确错误
    return token

# Raw Input 的 usButtons 位图编号 -> 人类可读名字。
# 编号来自位图：0左 1右 2中 3侧1 4侧2 5侧3 …
# 注意和上面的 BUILTIN_MOUSE_BUTTONS 不同——那边是 token 字符串
# （"xb1"），这边是 Raw Input 的整数编号。识别自定义键时用后者。
_BUTTON_LABEL_BY_ID = {
    0: "左键", 1: "右键", 2: "中键",
    3: "侧键1(G1)", 4: "侧键2(G2)",
    5: "侧键3(G3)", 6: "侧键4(G4)", 7: "侧键5(G5)",
}


# ======================================================================
# 全局热键
# ======================================================================
# 为什么要热键（用户反馈的第二个 bug）：
#   「启动鼠标连点后没法方便地切到目标界面点击」
# SendInput 把事件发给**当前前台窗口**，所以连点时目标程序必须在最前。
# 如果只能靠点仪表盘上的按钮启动，启动完仪表盘还在前面，
# 目标程序被挡住、也拿不到焦点，连点就点在了仪表盘自己身上。
#
# 解决：F5 =鼠标连点 启动/停止 切换（主人指定）。
# 配好参数后按 F5，仪表盘自动最小化让位给目标界面，
# 再按 F5 停止，全程不用碰仪表盘。
HOTKEY_MOUSE = "F5"
HOTKEY_KEY = "F6"
# 热键被占用时的降级方案：主人明确要的 F5 优先，占不上再退这两个
HOTKEY_MOUSE_FALLBACK = ["F5", "Ctrl+F5", "F8"]
HOTKEY_KEY_FALLBACK = ["F6", "Ctrl+F6", "F9"]
# 全局暂停热键。为什么是 Pause/Break：
#   主人反馈「没有设置全局暂停，玩原神之后 ctrl+c 就无效了」——
#   游戏中切不回控制台，必须有一个**系统级**的暂停键。
#   Pause(0x13) 在游戏里几乎没人绑定（它本职是DOS时代的Break），
#   被占了降级 ScrollLock(0x91)，再不行退 Ctrl+Pause。
HOTKEY_PAUSE_FALLBACK = ["pause", "scrolllock", "ctrl+pause"]
# 【宏启停热键 · v34】主人反馈「录制区应该也得和鼠标区、键盘区一样有启停键」。
#   宏回放时窗口是最小化的，想再启停只能靠系统级热键 —— 和 F5/F6 同一个道理。
#   为什么是 F7：紧挨 F5（连点）/ F6（键盘锁定）往下排，三个区一套，
#   且 F7 在绝大多数软件里没有默认含义（浏览器里也不是常用功能键）。
HOTKEY_MACRO_FALLBACK = ["F7", "Ctrl+F7", "F10"]


# ======================================================================
# 原始输入：用来「识别」用户实际按下的鼠标侧键
# ======================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
_raw_keys = {}
_raw_lock = threading.Lock()

# 原始输入自检。
# 【为什么要有它】「绑键无效」已经反馈两轮了，而根因是两个十六进制常量
# 写错——这类 bug 在测试环境里看不出来（没有真实鼠标输入），
# 只有用户上手才发现。所以把链路状态直接暴露给界面：
# 打开就能看到"监听到底注册上没有 / 收到几条消息 / 最后按下的是几号键"，
# 不用再靠猜。
_raw_stat = {
    "mode": "init",       # raw = 原始输入可用；poll = 降级轮询；fail = 两条都失败
    "sink": False,        # 隐藏窗口是否建成功
    "registered": False,  # RegisterRawInputDevices 是否成功
    "messages": 0,        # 收到多少条 WM_INPUT
    "last_buttons": 0,    # 最后一次看到的按键位图
    "last_ids": [],       # 最后一次按下的编号列表
    # 【v30】「最近按下」锁存。原来只有 last_ids（当前按住），抬起即清空；
    # 而界面 500ms 才轮询一次，正常点击（按下→抬起 <100ms）几乎永远
    # 采样不到 —— 主人看到的就是「偶尔闪一下就没了，还以为只认左键」。
    # 所以另存「最后一次按下的键 + 时刻」，由前端按时间差显示「N 秒前」。
    "last_press_id": -1,
    "last_press_at": 0.0,
    "last_wheel": 0,      # 最后一次滚轮滚动量（诊断"滚轮输入奇怪"用）
    "wheel_hits": 0,      # 滚轮事件计数（诊断用）
    "error": "",
}


# ======================================================================
# 触发键「按一下就绑」的捕获状态
# ======================================================================
# 为什么不再用下拉框：
#   鼠标按键的下拉项永远只有 middle / G1 / G2 三项，
#   而游戏鼠标实际有 G1~G8 甚至更多——用户想绑的键根本不在列表里。
#   改成捕获式：前端调arm_trigger_capture() 亮起提示，
#   Raw Input 线程收到「刚按下」事件后调 _capture_key()，
#   前端下一轮轮询 status() 时把结果取走。
#
# 这样任何能出现在 usButtons 位图里的键都能绑，且不用事先登记。
#
# 【v29】加了 8 秒超时。理由：旧实现 armed 之后只能靠 Esc 或「按下某个键」
# 结束，用户按侧键没反应时会一直挂着 —— 之后随便点一下界面，左键就被
# 录成触发键，弹出「左键不能作为触发键」，看起来像「侧键被识别成左键」。
# 超时判定放后端（不是纯前端定时器）：这样探针/日志能直接看到
# `armed=False` + 原因，排查时不用猜是界面没刷新还是链路没通。
_CAPTURE_TIMEOUT = 8.0
_CAPTURE_TIMEOUT_MSG = (
    "没收到任何标准鼠标键，请检查鼠标模式/驱动："
    "① 优先用有线或 2.4G（蓝牙模式常把侧键报成键盘/多媒体事件）；"
    "② 官方驱动里把侧键设为「鼠标按键 4/5」，不要设成键盘按键或宏；"
    "③ 先试中键——中键能绑，就说明是侧键上报的问题。")

_capture = {"armed": False, "raw_id": None, "seq": 0,
            "t0": 0.0,          # 进入捕获态的时刻（time.monotonic）
            "msg0": 0,          # 进入捕获态时的 WM_INPUT 计数
            "timeout": _CAPTURE_TIMEOUT,
            "timed_out": False,  # 本次捕获是否已判超时（待前端取走）
            "timeout_msg": ""}


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.WORD), ("usUsage", wt.WORD),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


# 【关键】RAWINPUT 的鼠标数据结构。
# 之前根本没有 RAWMOUSE 这个结构，只有一个 RAWINPUTHDR，
# 靠 msg.wParam 的高位标志只能知道「有鼠标事件」，
# **分不出是左键还是侧键** —— 所以「识别自定义按键」一直只能盲分配。
# 补上 RAWMOUSE 后可以从 usButtons 的位图读出具体按键编号：
#   bit0=左键 bit1=右键 bit2=中键 bit3=侧键1 bit4=侧键2
#
# 【类型选择】显式写 c_uint32 / c_int32 而不是 c_ulong / c_long。
# 实测二者在这个 Python 里是同一个类（都是 4 字节），
# 所以这纯粹是可读性考虑—— 但显式标注能防止以后有人「优化」成
# wt.ULONG 之类而引入真实偏差，也让Win32 的宽度约定一眼可查：
#   DWORD/ULONG/LONG/USHORT 全部固定 4/2 字节，
#   只有 HANDLE / WPARAM / LPARAM 才随位数变。
class RAWMOUSE(ctypes.Structure):
    """
    RAWMOUSE 的真实布局（winuser.h），已用真实报文实测确认。

        typedef struct tagRAWMOUSE {
          USHORT usFlags;                        // offset 0
          union {                                // offset 4（ULONG 要 4 字节对齐，
            ULONG  ulButtons;                    //          所以 2~3 是填充）
            struct { USHORT usButtonFlags;       // offset 4
                     USHORT usButtonData; };     // offset 6
          };
          ULONG ulRawButtons;                    // offset 8
          LONG  lLastX;                          // offset 12
          LONG  lLastY;                          // offset 16
          ULONG ulExtraInformation;              // offset 20
        } RAWMOUSE;                              // sizeof = 24

    【实证依据】实测收到 sizeof(RAWINPUT)=48 字节的鼠标报文，
    其中 24 字节是表头，所以 RAWMOUSE 必然是 24 字节；
    按下鼠标中键时，packet 偏移 28（= 本结构 offset 4）出现
    0x0010，正是 RI_MOUSE_MIDDLE_BUTTON_DOWN。

    【旧定义错在哪】旧定义把 usButtons 放在 offset 0，整个结构只有
    20 字节，于是每个字段都比真实位置**前移了 4 字节**：

        代码读 usButtons      → 实际读到 usFlags（移动模式，通常 0 或 1）
        代码读 usButtonFlags  → 实际读到 2~3 字节的填充（恒为 0）
        代码读 ulRawButtons   → 实际读到 usButtonFlags

    按下/抬起标志被读成恒 0，按键识别全线失效。

    【另一个实测结论】「当前按住的键」位图（ulRawButtons）在**注入的**
    合成输入下恒为 0，对真实鼠标才会更新。所以按下判定不能只依赖位图，
    必须以 usButtonFlags 为主通道 —— 见下方 _on_raw_mouse()。
    """
    _fields_ = [
        ("usFlags", ctypes.c_uint16),        # 移动模式（相对/绝对）
        ("_pad", ctypes.c_uint16),           # 对齐填充，无意义
        ("usButtonFlags", ctypes.c_uint16),  # 本次事件的按下/抬起/滚轮
        ("usButtonData", ctypes.c_uint16),   # 滚轮增量 / X 键编号
        ("ulRawButtons", ctypes.c_uint32),   # 原始按键状态位图
        ("lLastX", ctypes.c_int32),          # 相对位移 X
        ("lLastY", ctypes.c_int32),          # 相对位移 Y
        ("ulExtraInformation", ctypes.c_uint32),
    ]


class RAWINPUTHDR(ctypes.Structure):
    # 第 4 个字段官方名是 wParam。
    #【对鼠标事件来说，它的低 16 位就是 usButtons】——
    # 也就是说 hdr.wParam 和 mouse.usButtons 读的是同一份数据，
    # 只是偏移不同。之前代码把 hdr 拿到的值当"自定义键编号"用，
    # 而高位又是别的含义，算出来的编号基本是随机的。
    _fields_ = [("dwType", ctypes.c_uint32),   # DWORD
                ("dwSize", ctypes.c_uint32),   # DWORD
                ("hDevice", wt.HANDLE),         # 8 字节（64 位）
                ("wParam", ctypes.c_uint32)]    # WPARAM


class RAWINPUT_MOUSE(ctypes.Structure):
    """hdr + mouse 两段连在一起，这是 GetRawInputData 真正返回的布局。"""
    _fields_ = [("hdr", RAWINPUTHDR), ("mouse", RAWMOUSE)]


user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
user32.RegisterRawInputDevices.restype = wt.BOOL
# 【签名】GetRawInputData(hRawInput, uiCommand, pData, pcbSize, cbSizeHeader)
# 第 4 参 pcbSize 是 **PUINT（指针）**，指向"缓冲区有多大"的变量，
# 调用后还会被写回"实际用了多少"。
# 旧代码把它声明成 UINT 并直接传 128 —— 128 会被当成内存地址 0x80，
# 系统调用直接失败返回 (UINT)-1。
user32.GetRawInputData.argtypes = [wt.LPARAM, wt.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wt.UINT), wt.UINT]
user32.GetRawInputData.restype = wt.UINT

# 【第三处常量错误，同样是致命的】
# 这两个名字很像，含义完全不同，之前被当成同一个东西用了：
#
#   RID_INPUT  = 0x10000003  → GetRawInputData() 的 **uiCommand 参数**
#                              （"我要拿完整的 RAWINPUT 数据"）
#   RIM_INPUT  = 0x00000000  → RAWINPUTHEADER.dwType 里表示
#                              "这个事件来自前台/后台" 的**标志位**
#                              （RIM_INPUTSINK = 1 表示来自后台）
#
# 旧代码两处都错：
#   1. 把 RIM_INPUT 定义成了 0x00000003（真值应为 0）
#   2. 更糟的是，调用 GetRawInputData 时把 **RIM_INPUT 当成了 uiCommand**
#      —— 传进去一个非法命令 3，API 立刻失败，返回 (UINT)-1。
#
# 而 (UINT)-1 = 0xFFFFFFFF，于是后面那句
#     if cnt >= ctypes.sizeof(RAWINPUT_MOUSE):
# 永远成立（没有任何结构能有 42 亿字节），
# 代码便拿一个**全零的、根本没被写入的缓冲区**去解析按键位图，
# 解出来 usButtons = 0 —— 即"当前没有任何键按下"。
#
# 结果：每一条原始输入都被翻译成"什么都没按"，
# 「按下要绑的键」捕获不到任何输入。这才是"绑键无效"的直接原因。
RID_INPUT = 0x10000003
RID_HEADER = 0x10000005
RIM_INPUT = 0x00000000        # ← 真值；旧代码写成 0x00000003
RIM_INPUTSINK = 0x00000001
RIM_TYPEMOUSE = 0
# 【第二处常量写错，一并修正】
# 旧值把「抬起」的位当成了「按下」的位：
#   右键按下 写成 0x0002（实为 LEFT_BUTTON_UP，正确值 0x0004）
#   中键按下 写成 0x0200（实为 BUTTON_5_UP，  正确值 0x0010）
#   G2  按下 写成 0x0080（实为 BUTTON_4_UP，  正确值 0x0100）
# 好在真正的按下/抬起判定不靠这几个标志，而是靠 usButtons 位图比对
# （见下方「边沿检测」注释），所以之前只是 _raw_keys["down"] 这个
# 显示字段偶尔不准，没造成功能失效。但留着会误导后来的人，故改。
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010
RI_MOUSE_MIDDLE_BUTTON_UP = 0x0020
RI_MOUSE_BUTTON4_DOWN = 0x0040          # 侧键1(G1)
RI_MOUSE_BUTTON4_UP = 0x0080
RI_MOUSE_BUTTON5_DOWN = 0x0100          # 侧键2(G2)
RI_MOUSE_BUTTON5_UP = 0x0200
# 滚轮标志：usButtonFlags 置这些位时，本条消息里装的不是按键事件，
# 而是滚轮滚动量（mouseData 是带符号的滚动值）。
# 连点器不关心滚轮，但必须显式接住并**直接返回**——
# 否则滚轮事件会流进下面的按键分发逻辑，表现为
# 「启动鼠标区后滚轮输入有点奇怪」（主人反馈）。
RI_MOUSE_WHEEL = 0x0400                  # 竖直滚轮（usButtonFlags 位）
RI_MOUSE_HWHEEL = 0x0800                 # 水平滚轮（usButtonFlags 位）

# ---- RIDEV_* 标志位 ----
# 【这是本轮最致命的一个 bug，务必记住】
# 旧代码写的是 dwFlags = 0x01，注释却写着 "RIDEV_INPUTSINK"。
# 但 0x01 在 winuser.h 里是 **RIDEV_REMOVE** —— 含义是
#   「把这个设备从监听列表里移除」，也就是**反注册**。
# RIDEV_INPUTSINK 的真实值是 **0x00000100**。
#
# 于是那个 RegisterRawInputDevices() 调用做的事，恰恰是它名字的反面：
# 它把鼠标从原始输入监听里摘掉了。后果是 WM_INPUT 永远不会投递，
# 「按下要绑的键」捕获不到任何输入，鼠标键触发器也全部失效——
# 这就是反馈里「绑键那块还是无效」的根因。
#
# 这类 bug 极难看出来：变量名、注释、函数名都对，
# 只有一个十六进制字面量错了，而 API 还返回 TRUE（移除成功也算成功），
# 所以没有任何报错。
RIDEV_REMOVE = 0x00000001
RIDEV_EXCLUDE = 0x00000010
RIDEV_PAGEONLY = 0x00000020
RIDEV_NOLEGACY = 0x00000030
RIDEV_INPUTSINK = 0x00000100      # ← 正确值；0x01 是 RIDEV_REMOVE
RIDEV_CAPTUREMOUSE = 0x00000200
RIDEV_NOHOTKEYS = 0x00000200
RIDEV_APPKEYS = 0x00000400
RIDEV_EXINPUTSINK = 0x00001000
RIDEV_DEVNOTIFY = 0x00002000

# 只收消息的隐藏窗口：作为 WM_INPUT 的投递目标。
# HWND_MESSAGE 的值是 (HWND)-3。
HWND_MESSAGE = ctypes.c_void_p(-3)

# 【必须显式声明 DefWindowProcW 的签名】
# 不声明的话 ctypes 默认按 C int（32 位）传参，而窗口过程的
# LPARAM/WPARAM 在 64 位下是 64 位，一旦高位非零就会：
#     ctypes.ArgumentError: int too long to convert
# 窗口过程抛异常后消息处理就断了。
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL

# ---- 窗口类 API 的签名声明（按 Win32 文档逐个钉死）----
# 【为什么必须逐个声明，不声明会怎样】
# ctypes 对未声明 argtypes 的函数，会把 Python int 按 **32 位 C int**
# 传递。而 64 位 Windows 的 HWND 是 64 位句柄：pywebview 的
# native.Handle.ToInt64() 给出的是完整 64 位整数，一旦 ≥ 2^31，
# 调用当场抛 ctypes.ArgumentError（"int too long to convert"）。
#
# 实测：向未声明的 GetWindowRect 传 0x180001234 必抛
# （verify_window.py 有对应断言，见 test_frameless_subclass）。
#
# 【为什么这不是理论问题】句柄值由系统分配，平时可能一直很小
# （实测见过 0x1D10DA、0x4510A8 这种），但窗口开多了/重启几次就可能
# 越过 2^31。而症状不是崩溃，是**静默失效**——
# frameless / 最大化 / 还原 / 拖动 / 缩放整条链路的调用点大多包在
# try 里，异常被吞掉后只是返回个 False：
#   · _restore_styles 失败        → 样式位不补，边框拖不动
#   · _subclass_frameless 失败    → 白边修复不生效，外部最大化同步也没了
#   · _set_rect 失败              → 最大化/还原点下去没反应
#   · _work_area 失败             → win_toggle_maximize 直接返回 ok=False
#   · win_is_maximized 没有 try   → 异常抛到前端，界面弹错误
#   · win_drag_move / win_resize_begin 失败 → 标题栏拖不动、边框缩不了
# 用户看到的现象会是「这功能时好时坏」，极难定位。
#
# 声明值和 probe_border3.py 里实测过的一致，那个探针在本机
# 全部跑通过（真窗口 800x600，边框测量/子类化/最大化还原全 OK）。
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wt.LONG
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
user32.SetWindowLongW.restype = wt.LONG
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE
# 第二参故意写 c_void_p：MONITORINFO 类定义在文件后半部分
# （_work_area 旁边），这里还引用不到；调用点本来就传 byref(mi)，
# 指针类型足够，不会丢信息。
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.ReleaseCapture.argtypes = []
user32.ReleaseCapture.restype = wt.BOOL
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t


_WND_CLASS_NAME = "AutoClickerRawInputSink"
# 【防 GC】窗口过程是个 ctypes 回调，Python 侧一旦没有引用就会被回收，
# 系统再调用它时函数指针已失效 → 进程直接崩（没有 Python 栈可看）。
# 所以建一个模块级列表把回调钉住。
_WNDPROC_REFS = []


def _create_message_window():
    """
    建一个「只收消息」（message-only）的隐藏窗口，作为 WM_INPUT 的投递目标。

    【为什么非建不可】
    Raw Input 是按窗口投递的，两条规则决定了必须自己造窗口：

      1. hwndTarget 指向谁，WM_INPUT 就进**创建那个窗口的线程**的消息队列；
      2. hwndTarget 为 NULL 时，MSDN 原文是
         "A handle to the target window. If NULL it follows the keyboard focus."
         —— 也就是消息跟着**键盘焦点窗口**走。

    旧代码正是 hwndTarget=None，于是：
      · 我们的仪表盘在前台 → 消息进 pywebview 主线程队列，后台线程收不到；
      · 别的程序在前台    → 消息进那个程序的线程，更收不到。
    再叠加上面那个 dwFlags=0x01（RIDEV_REMOVE）的 bug，连监听都没注册上。

    两条一叠加：后台那条消息循环永远空转，
    「按下要绑的键」捕获不到任何东西 —— 这就是「绑键还是无效」的根因。

    这里建的窗口和消息循环同属 proc 线程，配上 RIDEV_INPUTSINK，
    即使本程序最小化在后台，按键照样收得到。

    返回窗口句柄；失败返回 None（调用方会降级到轮询）。
    """
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                                 wt.WPARAM, wt.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        # 【注意】wintypes 里**没有** HCURSOR（只有 HICON/HBRUSH/HINSTANCE），
        # 直接写 wt.HCURSOR 会 AttributeError。
        # 这些字段本质都是指针，统一写 c_void_p 最稳。
        _fields_ = [("cbSize", wt.UINT),
                    ("style", wt.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p),
                    ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_void_p),
                    ("lpszClassName", ctypes.c_wchar_p),
                    ("hIconSm", ctypes.c_void_p)]

    def _wndproc(hwnd, umsg, wparam, lparam):
        # 只当消息的"信封"，内容全在消息循环里取，这里无需处理
        return user32.DefWindowProcW(hwnd, umsg, wparam, lparam)

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k32.GetModuleHandleW.restype = wt.HMODULE

    cb = WNDPROC(_wndproc)
    _WNDPROC_REFS.append(cb)

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = cb
    wc.lpszClassName = _WND_CLASS_NAME
    wc.hInstance = k32.GetModuleHandleW(None)

    user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassExW.restype = wt.ATOM
    # 已注册过会返回 0（ERROR_CLASS_ALREADY_EXISTS），忽略即可
    user32.RegisterClassExW(ctypes.byref(wc))

    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wt.HWND

    hwnd = user32.CreateWindowExW(
        0, _WND_CLASS_NAME, "rawsink", 0,
        0, 0, 0, 0,
        HWND_MESSAGE, None, wc.hInstance, None)
    return hwnd if hwnd else None


# GetAsyncKeyState 虚拟键 -> Raw Input 位图编号（降级轮询用）
_VK_TO_RAW = {0x01: 0,        # VK_LBUTTON -> left
              0x02: 1,        # VK_RBUTTON -> right
              0x04: 2,        # VK_MBUTTON -> middle
              0x05: 3,        # VK_XBUTTON1 -> x1
              0x06: 4}        # VK_XBUTTON2 -> x2


def _poll_fallback():
    """
    Raw Input 建窗口失败时的**保底**方案：用 GetAsyncKeyState 轮询。

    【覆盖不到的部分，必须说清楚】
    GetAsyncKeyState 只认得左/右/中/X1/X2 这五个虚拟键。
    G3 及以上的按键走厂商私有协议，标准 API **读不到**。
    所以这条路只是保底——保证「鼠标中键 / G1 / G2 触发」不至于全废，
    想绑 G3~G8 仍然必须走 Raw Input。降级时界面会提示。
    """
    prev = 0
    while True:
        cur = 0
        for vk, rid in _VK_TO_RAW.items():
            try:
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    cur |= (1 << rid)
            except Exception:
                pass
        just_down = [i for i in range(16)
                     if (cur & (1 << i)) and not (prev & (1 << i))]
        just_up = [i for i in range(16)
                   if (prev & (1 << i)) and not (cur & (1 << i))]
        with _raw_lock:
            _raw_keys["buttons"] = cur
            _raw_keys["ids"] = [i for i in range(16) if cur & (1 << i)]
            _raw_stat["last_buttons"] = int(cur)
            _raw_stat["last_ids"] = list(_raw_keys["ids"])
            if just_down:
                _raw_stat["messages"] += 1
        for bid in just_down:
            try:
                bt.HUB.dispatch(bid, True)
            except Exception:
                pass
        for bid in just_up:
            try:
                bt.HUB.dispatch(bid, False)
            except Exception:
                pass
        if just_down and _capture["armed"]:
            _capture_key(just_down[-1])
        prev = cur
        time.sleep(0.01)


def start_mouse_listener():
    """
    后台线程 + 消息循环，监听鼠标按键。
    两件事靠这一个线程完成：
      1. 「自定义拓展按键」：识别用户按了哪个侧键
      2.鼠标键当热键：分发到 button_trigger.HUB

    注意：这个线程必须自己泵消息（GetMessage），
    但它不是主窗口所属线程，所以不会影响主窗口的「未响应」判定。
    """
    rid = RID_INPUT
    thread_id = ctypes.c_ulong()

    class MSG(ctypes.Structure):
        # pt_x/pt_y 对应 POINT 的 LONG（4 字节）。
        # 原来写成 ctypes.c_long —— 在 64 位下那是 8 字节，
        # MSG 会比真实的48 字节大 8 字节，GetMessageW 写进去时
        # 字段全部错位，拿到的 message/lParam 都可能是垃圾值。
        _fields_ = [
            ("hwnd", wt.HWND), ("message", wt.UINT),
            ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
            ("time", wt.DWORD), ("pt_x", ctypes.c_int32),
            ("pt_y", ctypes.c_int32),
        ]

    user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]

    def proc():
        # 【注意】GetCurrentThreadId 在 kernel32，不在 user32。
        # 挂错会直接 AttributeError: function not found。
        # 这里用不到线程 ID，所以干脆不取。
        # 【先建窗口，再注册】顺序不能反：
        # RegisterRawInputDevices 要的是"此刻就存在"的窗口句柄，
        # 先注册后建窗口的话，消息会投递到一个还不存在的目标上，直接丢弃。
        try:
            sink = _create_message_window()
        except Exception as e:
            sink = None
            _raw_stat["error"] = str(e)
        if not sink:
            # 极端环境（无窗口站 / 服务会话）下建不出窗口，退回轮询保底
            _raw_stat["mode"] = "poll"
            ST.set_msg("原始输入不可用，已降级为轮询"
                       "（仅支持中键/G1/G2）")
            _poll_fallback()
            return
        _raw_stat["sink"] = True

        devs = (RAWINPUTDEVICE * 1)()
        # 用 RID_INPUT 监听所有设备（而不是指定某个 HID 用法），
        # 这样任何鼠标（含游戏鼠标的额外按键）都能被捕获
        devs[0].usUsagePage = 0x01
        devs[0].usUsage = 0x02      # HID_USAGE_GENERIC_MOUSE
        # RIDEV_INPUTSINK：本程序在后台也照样收到原始输入。
        # 【必须与 hwndTarget 同时给出】MSDN 明确写了
        # "Note that hwndTarget must be specified."，
        # 只给 INPUTSINK 不给窗口，这个标志等于没写。
        devs[0].dwFlags = RIDEV_INPUTSINK
        devs[0].hwndTarget = sink
        ok = user32.RegisterRawInputDevices(
            ctypes.byref(devs), 1, ctypes.sizeof(RAWINPUTDEVICE))
        if not ok:
            # 注册失败也要保底，不能让触发键彻底死掉
            _raw_stat["mode"] = "poll"
            _raw_stat["error"] = ("RegisterRawInputDevices 失败，"
                                  "GetLastError=%s"
                                  % ctypes.get_last_error())
            ST.set_msg("原始输入注册失败，已降级为轮询"
                       "（仅支持中键/G1/G2）")
            _poll_fallback()
            return
        _raw_stat["registered"] = True
        _raw_stat["mode"] = "raw"

        msg = MSG()
        # 缓冲区给足128 字节：RAWHID 最长可达sizeof(RAWINPUTHEADER)+sizeof(RAWHID)
        # (RAWHID 里还有可变长usage 数组)，64 字节在某些设备上会截断。
        buf = (ctypes.c_byte * 128)()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == 0x00FF:      # WM_INPUT
                _raw_stat["messages"] += 1
                raw = _read_raw_mouse(msg.lParam, buf)
                if raw is not None and raw.hdr.dwType == RIM_TYPEMOUSE:
                    _on_raw_mouse(raw.mouse)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    t = threading.Thread(target=proc, name="rawmouse", daemon=True)
    t.start()
    return t


def _capture_key(raw_id):
    """
    Raw Input 线程调用：记下用户刚按下的那个键，并解除捕获态。

    只取第一个按下的键：同时按两个（比如中键 + G3）时，
    取 ids 顺序里的第一个，符合直觉。
    """
    with _raw_lock:
        _capture["armed"] = False
        _capture["raw_id"] = int(raw_id)
        _capture["timed_out"] = False
        _capture["timeout_msg"] = ""
        _capture["seq"] += 1


def _capture_tick():
    """
    捕获态超时检查（v29）。

    【为什么要轮询式判定，而不是开一个 Timer 线程】
      · 只在「有人在问」时才需要结论：前端 500ms 轮询一次
        take_captured_key，探针读一次 raw_diag，都是同步点；
      · 定时器线程要处理 arm/cancel/成功 三种竞态，容易漏 case；
      · 这里幂等：重复调用只会算一次（armed 已被置 False）。

    超时后 raw_id 置 None，但 seq +1 —— 前端靠 seq 判「有新结果」，
    这样「超时」这个结果只需要送一次，不会每轮轮询重复刷提示。
    """
    with _raw_lock:
        if not _capture["armed"]:
            return
        if time.monotonic() - float(_capture["t0"] or 0) < float(_capture["timeout"]):
            return
        grew = int(_raw_stat["messages"]) - int(_capture["msg0"])
        _capture["armed"] = False
        _capture["raw_id"] = None
        _capture["timed_out"] = True
        if grew <= 0:
            msg = _CAPTURE_TIMEOUT_MSG
        else:
            # 消息在涨但没按下任何键：多半只动了滚轮，或侧键被驱动
            # 报成了键盘/多媒体事件（那些不走 WM_INPUT 鼠标通道）。
            msg = ("没收到任何标准鼠标键，请检查鼠标模式/驱动："
                   "期间收到 %d 条鼠标消息，但没有任何按键。"
                   "① 优先用有线或 2.4G；② 驱动里把侧键设为「鼠标按键 4/5」；"
                   "③ 先试中键。" % grew)
        _capture["timeout_msg"] = msg
        _capture["seq"] += 1


# usButtonFlags 里「按下/抬起」标志 → 按键编号。
# 【为什么只到 5】Windows 只为左/右/中/X1/X2 定义了标志，
# RI_MOUSE_BUTTON_5_UP 是最后一个；G3 及以后没有任何标志位，
# 那些键只能靠 ulRawButtons 位图的边沿检测（见 _on_raw_mouse 通道 2）。
_DOWN_FLAGS = [(RI_MOUSE_LEFT_BUTTON_DOWN, 0),
               (RI_MOUSE_RIGHT_BUTTON_DOWN, 1),
               (RI_MOUSE_MIDDLE_BUTTON_DOWN, 2),
               (RI_MOUSE_BUTTON4_DOWN, 3),
               (RI_MOUSE_BUTTON5_DOWN, 4)]
_UP_FLAGS = [(RI_MOUSE_LEFT_BUTTON_UP, 0),
             (RI_MOUSE_RIGHT_BUTTON_UP, 1),
             (RI_MOUSE_MIDDLE_BUTTON_UP, 2),
             (RI_MOUSE_BUTTON4_UP, 3),
             (RI_MOUSE_BUTTON5_UP, 4)]

# 编号 → 界面名。0~4 是通道 1 的标准键，5 以后是通道 2 的位图键（G3+）。
_RAW_ID_NAMES = {0: "左键", 1: "右键", 2: "中键", 3: "G1", 4: "G2"}
for _i in range(5, 16):
    _RAW_ID_NAMES[_i] = "G%d" % (_i - 2)


def _std_ids_update(bid, down):
    """
    维护「当前按住的键」编号列表（v29）。

    【修的是什么】通道 1（usButtonFlags）覆盖的正是**标准键**：
    左/右/中/G1/G2。旧代码在这里 `_dispatch()` 后直接 return，
    从不写 last_ids —— 于是界面上「最近按键」对标准侧键永远是「—」，
    只有 G3 以上（走通道 2 位图）才会刷新。排障时这条信息误导性极强：
    明明按了 G1/G2，自检条却一动不动。

    只更新诊断数据，不碰分发顺序、不碰 return 短路
    （短路是为了防止同一个事件被分发两次）。
    """
    with _raw_lock:
        down_set = _raw_keys.setdefault("std_down", set())
        if down:
            down_set.add(bid)
            # 锁存「最近按下」：抬起不清它，只清「当前按住」。
            # 界面靠这个 + 时间差显示「G1（0.6 秒前）」，快按快松也看得见。
            _raw_stat["last_press_id"] = int(bid)
            _raw_stat["last_press_at"] = time.monotonic()
        else:
            down_set.discard(bid)
        raw_bits = int(_raw_keys.get("raw_buttons", 0) or 0)
        merged = sorted(set(down_set) |
                        set(i for i in range(5, 16) if raw_bits & (1 << i)))
        _raw_keys["ids"] = merged
        _raw_stat["last_ids"] = list(merged)


def _dispatch(bid, down):
    """分发一个按键事件：喂给触发器，并且（仅按下时）喂给捕获器。"""
    try:
        bt.HUB.dispatch(bid, down)
    except Exception:
        pass
    if down and _capture["armed"]:
        _capture_key(bid)


def _read_raw_mouse(lparam, buf):
    """
    从一条 WM_INPUT 读出完整的 RAWINPUT（表头 + 鼠标段）。

    返回 None = 这条消息拿不到有效数据，调用方直接跳过。

    【三个曾经踩过的坑，全集中在这里处理】
      1. uiCommand 必须是 RID_INPUT(0x10000003)。
         旧代码误传了 RIM_INPUT(3) —— 那是「事件来自前台还是后台」的
         标志位，不是命令字。传 3 进去，API 立刻失败返回 (UINT)-1。
      2. 第 4 参 pcbSize 是 **PUINT 指针**（指向"缓冲区多大"的变量，
         返回时写回"实际用了多少"），不是大小值。
         旧代码传整数 128，被当成内存地址 0x80，同样失败。
      3. 失败返回值是 (UINT)-1 = 0xFFFFFFFF，必须显式拦掉。
         否则后面 `cnt >= sizeof(...)` 恒成立（没有结构有 42 亿字节），
         代码就会拿一个从未被写入的全零缓冲区去解析 ——
         解出来永远是"没有任何键按下"，且不报任何错。
    """
    total = ctypes.sizeof(buf)
    sz = wt.UINT(total)
    cnt = user32.GetRawInputData(
        lparam, RID_INPUT, buf, ctypes.byref(sz),
        ctypes.sizeof(RAWINPUTHDR))
    if cnt <= 0 or cnt == 0xFFFFFFFF or cnt > total:
        return None
    if cnt < ctypes.sizeof(RAWINPUT_MOUSE):
        return None
    try:
        return RAWINPUT_MOUSE.from_buffer_copy(bytes(buf)[:cnt])
    except Exception:
        return None


def _on_raw_mouse(mouse):
    """
    处理一条鼠标原始输入，算出「哪些键刚按下 / 刚抬起」并分发。

    【为什么分两条通道 —— 这是实测结论，不是理论推测】
    实测把中键按下/抬起的合成输入喂进来，发现：
      · usButtonFlags 正常出现 0x0010 / 0x0020（按下 / 抬起）—— 可靠
      · ulRawButtons（"当前按住的键"位图）恒为 0 —— 注入的输入不更新它
    而对真实鼠标，ulRawButtons 才会反映真实按住状态。

    两条通道的能力互补：
      · usButtonFlags：无论输入来源都可靠，但只覆盖左/右/中/G1/G2 五个键
      · ulRawButtons：能覆盖到 G3~G8，但对注入输入无反应

    所以两条都走，通道 1 命中就直接返回（避免重复分发），
    没命中才落到通道 2 去抓 G3+。

    【边沿检测为什么必要】
    位图是"当前按住的键"的快照。不比对上一帧的话，
    按住不放期间每一帧都会算出同一个键"刚按下"，
    触发器会被反复触发。比对之后：
        新出现的一位 = 刚按下
        消失的一位   = 刚抬起

    【滚轮事件（对应主人反馈"滚轮输入有点奇怪"）】
    usButtonFlags 置 RI_MOUSE_WHEEL/HWHEEL 位时，本条消息装的是
    滚轮滚动量而不是按键状态。按键分发逻辑不认识它，
    会当"没有任何按键变化"直接往下走位图比对——
    位图当然也没变，于是什么也不发生。
    但这带来两个隐患：
      1. 滚轮消息挤占分发通道，和真实点击混在同一条路径上；
      2. ulRawButtons 对注入输入无反应（见函数末尾注释），
         若注入的滚轮和真实点击挨着发生，边沿状态可能被抹掉。
    所以这里显式接住滚轮、记一笔诊断、直接返回。

    【注入回环过滤 —— "绑了 G2 点一下就没后续"的根因之一】
    连点滴答发出的点击事件，经 RIDEV_INPUTSINK 会**原样回到
    _on_raw_mouse**。不过滤的话，触发器会把自注入的按下
    当成"用户又按了一次触发键"：
    若点击键恰好就是触发键（如都绑在 G2 上），
    第一次滴答注入就触发 toggle_mouse → 连点刚启动就被自己关掉，
    表现就是「点一下触发键只连点一次」。
    键盘侧早有对称机制（core/keyboard.py 的 INJECT_MARK），
    鼠标侧此前没有——现在用同一个标记值补上
    （core/inputs.py 的 MARK_INJECTED，二者必须保持一致）。
    """
    flags = int(mouse.usButtonFlags)
    btns = int(mouse.ulRawButtons)

    # ---- 自己注入的事件不当成用户操作（先于一切判断）----
    if int(mouse.ulExtraInformation) == inp.MARK_INJECTED:
        return

    # ---- 滚轮：记诊断后直接返回，不进按键分发 ----
    if flags & (RI_MOUSE_WHEEL | RI_MOUSE_HWHEEL):
        with _raw_lock:
            _raw_stat["last_wheel"] = int(mouse.usButtonData)
            _raw_stat["wheel_hits"] = int(_raw_stat.get("wheel_hits", 0)) + 1
        return

    # ---- 通道 1：usButtonFlags（实测可靠，覆盖左/右/中/G1/G2）----
    # v29：先更新 last_ids 再分发（顺序不影响分发语义，两件事互不依赖）
    for mask, bid in _DOWN_FLAGS:
        if flags & mask:
            _std_ids_update(bid, True)
            _dispatch(bid, True)
            return
    for mask, bid in _UP_FLAGS:
        if flags & mask:
            _std_ids_update(bid, False)
            _dispatch(bid, False)
            return

    # ---- 通道 2：ulRawButtons 位图边沿（只为抓 G3~G8）----
    with _raw_lock:
        prev = int(_raw_keys.get("raw_buttons", 0) or 0)
        _raw_keys["raw_buttons"] = btns
        _raw_keys["buttons"] = btns
        _raw_stat["last_buttons"] = int(btns)

    changed = prev ^ btns
    if not changed:
        return
    ids = [i for i in range(16) if btns & (1 << i)]
    with _raw_lock:
        _raw_keys["ids"] = list(ids)
        _raw_stat["last_ids"] = list(ids)
        # 通道 2 抓的是 G3+；新出现的那一位就是「刚按下」，同样要锁存，
        # 否则「最近按键」对 G3~G8 又是同一类盲区。
        fresh = [i for i in range(5, 16) if (changed & btns) & (1 << i)]
        if fresh:
            _raw_stat["last_press_id"] = int(fresh[-1])
            _raw_stat["last_press_at"] = time.monotonic()
    # 0~4 通道 1 已覆盖，这里只处理 5 及以上，防止同一个事件分发两次
    for i in range(5, 16):
        if changed & (1 << i):
            _dispatch(i, bool(btns & (1 << i)))


# ======================================================================
# 全局状态
# ======================================================================
class State(object):
    """线程安全的运行时状态快照，供前端轮询。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.mouse_engine = None
        self.key_lock = None
        self.mouse_running = False
        self.key_running = False
        self.mouse_info = ""
        self.key_info = ""
        self.mouse_msg = ""          # 鼠标区专属消息（不再和键盘区混用）
        self.key_msg = ""            # 键盘区专属消息
        self.message = "就绪"        # 全局/最近一条消息
        self.started_at = 0
        self.stats = {"click": 0, "inject": 0}
        # 【就绪态】按钮只装填参数不启动；按热键才真启动。
        # 没有这个字段时，「点启动=直接触发」和主人的两段式预期
        # （启动=准备就绪，F5=开始）对不上，且第一次按 F5 会被
        # 当成「停止正在跑的任务」，表现为「点第一次 F5 就暂停」。
        self.mouse_armed = False
        self.key_armed = False
        self.elev_warn = False       # 检测到「目标提权+我们没提权」时为 True
        # 【v23 S4】用户按了「停止鼠标」时为 True：_finished 靠它区分
        # 「自然完成」（次数/时长用完，该触发收尾动作）和「用户停止」
        # （绝不做收尾动作）。没有这个标志时，停止后引擎线程稍后结束，
        # _finished 会把状态消息覆盖成「已完成」—— race 之外还会让
        # S4 在用户主动停止时也执行关机，那是事故。
        self.mouse_stop_requested = False

    def set_msg(self, m, zone=None):
        """
        写一条消息。

        zone=None → 全局消息（顶栏一行）；
        zone="mouse"/"key" → 只写对应区的状态行。
        【为什么分区】以前所有消息都写同一个 message，前端两个区都渲染它，
        于是「鼠标暂停了」也会显示在键盘区（主人反馈：
        「几个区的显示状态是全局的而不是单独的」）。
        """
        with self.lock:
            if zone == "mouse":
                self.mouse_msg = m
            elif zone == "key":
                self.key_msg = m
            else:
                self.message = m

    def set_paused(self, flag, zone=None):
        # 真正的事实源在 core/pausectl.py：键盘锁定循环也要读它，
        # 而 keyboard 不能 import dashboard（循环导入）。
        # zone=None → 全局（Pause 热键）；"mouse"/"key" → 只停那一个区。
        pausectl.set_paused(flag, zone)

    def is_paused(self, zone=None):
        return pausectl.is_paused(zone)

    def snapshot(self):
        with self.lock:
            dur = (time.time() - self.started_at) if self.started_at else 0
            return {
                "mouse_running": self.mouse_running,
                "key_running": self.key_running,
                # paused = 任意区暂停（徽标/暂停按钮用，点它就全局恢复）
                "paused": pausectl.any_paused(),
                "mouse_paused": pausectl.is_paused("mouse"),
                "key_paused": pausectl.is_paused("key"),
                "mouse_info": self.mouse_info,
                "key_info": self.key_info,
                "mouse_msg": self.mouse_msg,
                "key_msg": self.key_msg,
                "message": self.message,
                "mouse_armed": self.mouse_armed,
                "key_armed": self.key_armed,
                "elev_warn": self.elev_warn,
                "elapsed": round(dur, 1),
                "click": self.stats["click"],
                "inject": self.stats["inject"],
                "panic": self.key_lock.panic_name if self.key_lock else "F12",
                # v23 S4：收尾动作待执行状态（前端显示「取消」按钮）
                "done_pending": actions.pending_info(),
            }


ST = State()

# ======================================================================
# 宏录制 / 回放状态（v27 / S2）
# ======================================================================
# 放模块级字典而不是散落的全局变量：录制、回放、保存、退出四个入口
# 共享同一份事实，探针也能一次性读取和重置。
#   rec      core.recorder.Recorder（懒创建：不进宏区就不装钩子）
#   events   最近一次录制/载入的原始事件表（回放时若没有 steps 才用它）
#   steps    步骤表（v30）。**编辑与回放都以它为准**：用户改的是节点，
#            回放必须吃改后的数据，否则「改了没生效」是最难查的 bug。
#   engine   回放引擎（跟鼠标连点的 mouse_engine 一样，靠它判断在不在跑）
_MACRO = {
    "rec": None,
    "events": [],
    "steps": [],
    "engine": None,
    "playing": False,
    "name": "",
    "step_ver": 0,
}


def _macro_recorder():
    """懒创建录制器：钩子只在真录制时装，平时一丁点开销都没有。"""
    if _MACRO["rec"] is None:
        from core.recorder import Recorder
        _MACRO["rec"] = Recorder()
    return _MACRO["rec"]


def _macro_bump():
    """
    步骤表版本号 +1。

    界面 700ms 轮询一次状态，如果每次都全量重绘步骤列表，用户正按着
    准备点的那一行会被刷掉（点不中）。所以只在版本变了才重画。
    """
    _MACRO["step_ver"] = int(_MACRO.get("step_ver", 0)) + 1
    return _MACRO["step_ver"]


def _macro_preview(steps, limit=6):
    """前几步的一句话预览（给状态行用，不搬全量数据）。"""
    from core import macro_steps as ms
    if not steps:
        return ""
    head = " → ".join(ms.label(s) for s in steps[:limit])
    return head + (" …" if len(steps) > limit else "")


def _macro_recording():
    rec = _MACRO.get("rec")
    return bool(rec is not None and rec.running)

# DPI 感知状态（main() 里设，_log_environment() 里读；见 main 的坑注释）
_dpi_state = "未设置（main 未跑起来？）"


# ======================================================================
# 鼠标连点
# ======================================================================
def random_uniform_int(a, b):
    """
    在 [a, b] 间随机取一个整数（框内随机用）。

    单独抽出来是为了**可测**：探针要打桩它并断言「每个落点都在框内、
    且不是恒定值」。直接写 random.randint 的话，测试只能靠概率猜。
    a > b 时自动交换（调用方一般已排序，这里是双保险）。
    """
    import random
    lo, hi = (a, b) if a <= b else (b, a)
    return random.randint(int(lo), int(hi))


def _make_mouse_plugin(button_token, duration=0, click_type="single",
                       fixed_pos=None, countdown=0, points=None,
                       pos_mode="cursor"):
    """
    按所选按键生成连点插件。
    复用 plugins.normal_click 的实现思路，但支持 XBUTTON 侧键。

    【v22 新增能力 · 参考 GitHub 同类程序】
    · click_type  : single / double / triple —— 每轮次点 1/2/3 下
      （EC-Autoclicker / WinAutoClick / OP Auto Clicker 都有点击类型选择）
    · fixed_pos   : 固定坐标（不为 None 时每轮次先把鼠标搬过去再点）
      —— 引擎 ClickContext 早就支持，dashboard 以前没接线
    · countdown   : 启动前倒计时秒数，给用户切到目标窗口的时间
      （EC-Autoclicker 的 Pre-start delay / yankivare 的 countdown timers）
    """
    from core.engine import Plugin

    # 每轮次点击次数：单击 1 下，双击 2 下，三击 3 下
    per_tick = {"single": 1, "click": 1, "double": 2, "triple": 3}
    n_per_tick = per_tick.get(str(click_type or "single"), 1)

    class DynClick(Plugin):
        name = "dynamic_click"
        description = "按所选按键连点"

        def __init__(self, btn, duration=0, per_tick=1, fixed_pos=None,
                     countdown=0, points=None, pos_mode="cursor"):
            self.btn = btn
            self.per_tick = max(1, int(per_tick))
            self.fixed_pos = tuple(fixed_pos) if fixed_pos else None
            self.countdown = max(0, int(countdown or 0))
            # 【v26 多点轮询 / 框内随机】
            # cursor = 不搬鼠标；fixed = 每轮搬到一个点；
            # multi  = 按轮次**依次**取点（A->B->C->A…）；
            # box    = 每轮在两个对角点围成的矩形内**随机**取点。
            self.pos_mode = str(pos_mode or "cursor")
            self.points = [tuple(p) for p in (points or [])]
            # 【坑】ClickContext.__init__ 里 duration 是 int(...)，
            # 传 0.5 会被截断成 0（= 不限时）。所以真实时长必须自己存。
            self.duration = float(duration or 0)

        def _next_target(self, ctx):
            """
            算出这一轮该点哪。返回 (x, y) 或 None（跟随光标 = 不搬）。

            【为什么用 tick_count 而不是自己再维护一个游标】
            次数上限（count_limit）就是按 tick_count 判的，两者同源
            才能保证「第 N 次点击落在第 N%len 个点」这种可预测性——
            出问题时对日志能直接算出来该点哪儿。
            """
            if self.pos_mode == "multi" and self.points:
                return self.points[ctx.tick_count % len(self.points)]
            if self.pos_mode == "box" and len(self.points) == 2:
                (x1, y1), (x2, y2) = self.points
                return (random_uniform_int(x1, x2), random_uniform_int(y1, y2))
            if self.pos_mode == "fixed" and self.fixed_pos:
                return self.fixed_pos
            return None

        def on_start(self, ctx):
            # 【启动倒计时 · v22】还没开始注入，先数 N 秒。
            # 只认「停止」信号（ctx.wait 返回 False）—— 次数/时长上限
            # 不该在倒计时里计时，否则「时长 2 秒 + 倒计时 5 秒」会
            # 倒计时还没数完就被判超时。暂停 = 停表（ctx.wait 内部的
            # 暂停门闩会阻塞到恢复，这一秒自然不消耗）。
            if not self.countdown:
                return True
            left = self.countdown
            with ST.lock:
                ST.mouse_info = "倒计时 %ds…" % left
            ST.set_msg("倒计时 %d 秒后开始连点 · 点「停止鼠标」可取消"
                       % left, zone="mouse")
            while left > 0:
                if not ctx.wait(1.0):
                    return False        # 停止：状态已由 stop_mouse 回滚
                left -= 1
                with ST.lock:
                    ST.mouse_info = ("倒计时 %ds…" % left) if left > 0 \
                        else "启动中…"
            return True

        def run(self, ctx):
            # 【坑】ctx.should_stop / base_interval / elapsed 都是**方法**，
            # 不是属性，写成属性会拿到 bound method，while 判断永远为真
            # 且循环体一次都不执行（表现为「启动了但点击数一直是 0」）。
            # 另一个坑：ClickContext.__init__ 里 duration 是 int(...)
            # 非0.6 秒会被截断成 0（= 不限时），所以这里在插件内自己计时。
            t0 = time.time()
            while not ctx.should_stop():
                if self.duration and (time.time() - t0) >= self.duration:
                    break
                # 【暂停门闩】暂停≠停止：连点状态保持「运行中」，
                # 但一个事件都不注入。按一下触发键就能定住画面，
                # 再按一下恢复 —— 而不必把连点整个停掉重设参数。
                # 为什么必须在这里拦：SendInput 是同步调用，
                # 暂停期间若继续滴答，游戏画面仍在动，暂停就失去了意义。
                # 【分区·2026-10-16】只看鼠标区：键盘锁定单独暂停时
                # 连点不能跟着停（原神里「普攻暂停、前进继续」要靠它）。
                # 全局 Pause 键会一路写进 mouse 区，所以照样拦得住。
                while ST.is_paused("mouse") and not ctx.should_stop():
                    if not ctx.wait(0.1):
                        break
                if ctx.should_stop():
                    break
                # 【固定坐标 · v22 / 多点轮询 · v26】每轮次先把鼠标搬到
                # 目标点再点。跟随光标模式 _next_target 返回 None，不搬
                # （搬到自己当前位置是白费一次 API 调用）。
                target = self._next_target(ctx)
                if target:
                    inp.move_to(target[0], target[1])
                # 【点击类型 · v22】双击/三击 = 一轮里连点 N 下。
                # 按下抬起成对由 _click_once 内部保证。
                for _ in range(self.per_tick):
                    _click_once(self.btn)
                # 【坑·2026-10-15】clicked 和 tick_count 必须一起加：
                # 所有「注入了 N 次」的显示（结束日志/心跳/停止统计）
                # 读的都是 ctx.clicked，而旧代码只加 tick_count ——
                # 于是注入实际发生了，界面和日志却永远显示 0 次，
                # 主人据此判断「根本没注入」，排查全被带偏。
                # v22：clicked 按**实际注入次数**加（双击一轮 +2），
                # tick_count 按轮次加（次数上限的语义是轮次，与引擎一致）。
                ctx.clicked += self.per_tick
                ctx.tick_count += 1        # 次数上限靠这个计数，不加会导致停不下来
                with ST.lock:
                    ST.stats["click"] += self.per_tick
                if not ctx.wait(ctx.base_interval()):
                    break

        def on_stop(self, ctx):
            return True

    return DynClick(button_token, duration, per_tick=n_per_tick,
                    fixed_pos=fixed_pos, countdown=countdown,
                    points=points, pos_mode=pos_mode)


# 合法的鼠标动作：单击/双击/三击/长按/抖器
_MOUSE_ACTIONS = ("click", "double", "triple", "hold", "jiggle")

# 合法的收尾动作（v23 S4，与 core/actions.ACTIONS 一致）
_DONE_ACTIONS = ("none", "sound", "lock", "shutdown")


def _clamp_point(raw):
    """
    把 "x,y" 字符串或 [x, y] 列表解析成 clamp 到虚拟桌面的 [x, y]。

    【为什么 clamp 用虚拟桌面 · v25】副屏在主屏左侧时 x 是负数，
    按主屏 [0, sw) 夹会把副屏坐标变成 0（鼠标飞到左上角）。
    """
    if isinstance(raw, str):
        txt = raw.replace("，", ",").strip()
        if not txt:
            raise ValueError("请填 X,Y 或点「取当前」按钮")
        parts = [s.strip() for s in txt.replace("\n", ",").split(",")
                 if s.strip()]
        if len(parts) != 2:
            raise ValueError("坐标应为 x,y 两个数")
        raw = parts
    if raw is None or len(raw) != 2:
        raise ValueError("坐标缺失")
    x, y = int(float(raw[0])), int(float(raw[1]))
    vx, vy, vw, vh = inp.virtual_screen()
    return [max(vx, min(x, vx + vw - 1)), max(vy, min(y, vy + vh - 1))]


def _parse_points(raw, need_two=False):
    """
    解析一组坐标（v26 多点轮询 / 框内随机用）。

    接受的写法（都是用户可能手输出来的）：
        "100,200;300,400"      分号分隔
        "100,200\n300,400"     一行一个（界面文本框就是这种）
        "100，200；300，400"    中文标点
        [[100,200],[300,400]]  列表（配置导入）
    单点格式错就整条报错，不静默丢掉坏行——「少点了一个位置」
    比「报错让我改」危险得多（挂机时不会有人盯着）。
    """
    if raw is None:
        items = []
    elif isinstance(raw, str):
        txt = raw.replace("；", ";").replace("\n", ";").replace("\r", "")
        txt = txt.replace("，", ",")
        items = [seg for seg in txt.split(";") if seg.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError("坐标列表格式不对")

    pts = [_clamp_point(it) for it in items]
    if need_two and len(pts) != 2:
        raise ValueError("框内随机需要正好两个坐标（左上、右下），"
                         "当前 %d 个" % len(pts))
    if len(pts) > 200:
        raise ValueError("坐标太多（上限 200 个）")
    return pts


def _parse_mouse_params(params):
    """
    把前端/热键/配置导入传来的鼠标参数**规范化**成一份字典。

    【为什么抽这个函数 · v22】arm_mouse（按钮路径）和 start_mouse
    （热键路径）以前各写一遍 try/int() 解析。v22 新增抖动、固定坐标、
    倒计时三个参数后，两处漂移的代价变高：一处解析了另一处没有，
    就会「按钮就绪带的参数、热键启动时不带」——这类不一致极难排查。
    收敛成单一入口，两边行为永远一致。

    返回 (规范化字典, None) 或 (None, 错误消息)。
    规范化字典键：button / action / count / duration / cps /
                  jitter / mode / pos / countdown /
                  jig（抖器挪动间隔秒）/ done（收尾动作）
    """
    try:
        token = _normalize_button(params.get("button", "left"))
        action = str(params.get("action", "click") or "click")
        count = int(params.get("count", 0) or 0)
        duration = float(params.get("duration", 0) or 0)
        cps = float(params.get("cps", 10) or 10)
        jitter = int(params.get("jitter", 0) or 0)
        countdown = int(params.get("countdown", 0) or 0)
        jig = int(float(params.get("jig", 30) or 30))
    except Exception as e:
        return None, "参数解析失败：%s" % e

    if action not in _MOUSE_ACTIONS:
        return None, ("未知动作：%r（可选 click/double/triple/hold/jiggle）"
                      % action)
    if jitter < 0:
        jitter = 0                      # 负抖动没有意义，按 0 处理
    if countdown < 0:
        countdown = 0
    elif countdown > 600:
        countdown = 600                 # 上限 10 分钟，防手滑打天文数字
    if jig < 1:
        jig = 30                        # 抖器间隔兜底：小于 1 秒太吵
    elif jig > 3600:
        jig = 3600                      # 上限 1 小时

    # 【v23 S4】收尾动作白名单：不认识的按 none（不静默执行陌生动作）
    done = str(params.get("done", "none") or "none")
    if done not in _DONE_ACTIONS:
        done = "none"

    mode = str(params.get("mode", "cursor") or "cursor")
    # 【v26】点击位置从「跟随/固定」两种扩成四种：
    #   cursor 跟随光标 / fixed 固定一点 / multi 多点轮询 / box 框内随机
    if mode not in ("cursor", "fixed", "multi", "box"):
        mode = "cursor"

    # 固定坐标：接受 "x,y" 字符串（界面输入框）或 [x, y] 列表
    # （配置导入带进来的是列表）。 clamp 到屏幕范围内，越界坐标
    # 会被 _to_absolute 归一化出垃圾值，必须在这里拦掉。
    fixed = None
    points = []
    if mode in ("fixed", "multi", "box"):
        raw = params.get("pos") if mode == "fixed" else params.get("points")
        try:
            if mode == "fixed":
                fixed = _clamp_point(raw)
            else:
                points = _parse_points(raw, need_two=(mode == "box"))
                if mode == "box":
                    # 框内随机：两点排序成 (左上, 右下)，坐标倒着输也不会炸
                    (x1, y1), (x2, y2) = points[0], points[1]
                    points = [[min(x1, x2), min(y1, y2)],
                              [max(x1, x2), max(y1, y2)]]
                if mode == "multi" and not points:
                    raise ValueError("至少要有一个坐标")
        except Exception as e:
            return None, ("%s无法使用：%s"
                          % ("坐标" if mode == "fixed" else "坐标列表", e))

    # 长按路径不走引擎（没有倒计时的位置），不支持倒计时和固定坐标——
    # 不静默忽略：归零/归一成跟随光标，界面也会禁用这两项。
    # （配置导入可能带进 action=hold + mode=fixed 的古老组合，
    #   在这里统一清掉，别让 Hold 路径拿到一个用不上的坐标。）
    # 【v23】长按同样不支持收尾动作（press/release 路径没有「自然完成」
    # 事件），归零成 none，界面也会禁用。
    if action == "hold":
        countdown = 0
        mode = "cursor"
        fixed = None
        points = []
        done = "none"

    # 【v23】抖器不同意倒计时：它不点任何东西、也不需要切前台等窗口
    # （那正是倒计时存在的理由）。归零而不是留着不用——「收了参数却
    # 不生效」是最难查的一类问题，界面也同步禁用该项。
    if action == "jiggle":
        countdown = 0

    return {
        "button": token, "action": action, "count": count,
        "duration": duration, "cps": cps, "jitter": jitter,
        "mode": mode, "pos": fixed, "countdown": countdown,
        "jig": jig, "done": done,
        # v26：多点轮询 / 框内随机用的坐标列表（multi：N 个；box：正好 2 个）
        "points": points,
    }, None


def _done_tick(left):
    """收尾动作倒计时的每秒回调（v23 S4）：把剩余秒数写进鼠标区消息。"""
    ST.set_msg("完成后动作：%d 秒后执行——点「取消待执行动作」或停止可中止"
               % max(0, int(left)), zone="mouse")


def _pos_desc_of(p):
    """
    点击位置的日志文案（排障第一眼看这行，v26 四种模式都必须认得出）。

    【为什么抽成函数】就绪日志和启动日志各写一遍的话，v26 加模式时
    必然漏改一处 —— 启动日志还写着「跟随光标」，实际在多点轮询，
    排查时会被自己的日志骗过去（v22 的 _pos_desc 只在就绪那处）。
    """
    mode = (p or {}).get("mode") or "cursor"
    p = p or {}
    if mode == "fixed" and p.get("pos"):
        return "固定坐标 %s" % (p["pos"],)
    if mode == "multi":
        pts = p.get("points") or []
        return "多点轮询 %d 个点 %s%s" % (len(pts), pts[:3],
                                       "…" if len(pts) > 3 else "")
    if mode == "box":
        pts = p.get("points") or []
        return "框内随机 %s" % (pts,)
    return "跟随光标"


def _click_once(token):
    """按 token 发一次鼠标点击（含左右键的按下抬起）。"""
    if token == "left":
        inp.click("left", count=1)
    elif token == "right":
        inp.click("right", count=1)
    elif token == "middle":
        inp.click("middle", count=1)
    else:
        _click_xbutton(token)


def _xbutton_number(token):
    """
    把按键 token 转成 XBUTTON 编号（1=XBUTTON1/G1，2=XBUTTON2/G2）。

    token 有两种来源，都指向同一个物理键：
      · 内置表 BUILTIN_MOUSE_BUTTONS：  "xb1"/"xb2"
      · Raw Input 识别出的自定义键：    "xb3"/"xb4"（编号=Raw Input 位图编号）
    所以 1/3 都映射到 XBUTTON1，2/4 都映射到 XBUTTON2。

    【Windows 只支持两个扩展键】SendInput 的 XBUTTON 注入只有
    XBUTTON1 / XBUTTON2 两个编号（mouseData=1/2），
    G3~G8 根本注入不了——不是代码问题，是 API 上限。
    遇到 xb5+ 直接抛 ValueError，让上层把"该键无法注入"
    明确告诉用户，而不是静默发一堆垃圾事件。
    """
    idx = None
    if isinstance(token, str) and token.startswith("xb"):
        try:
            idx = int(token[2:])
        except ValueError:
            idx = None
    if idx in (1, 3):
        return 1
    if idx in (2, 4):
        return 2
    raise ValueError(
        "按键 %r 无法注入：Windows 只支持两个侧键（G1/G2），"
        "G3 及以后的按键 SendInput 发不了" % (token,))


def _click_xbutton(token):
    """
    XBUTTON 侧键点击一次（按下+抬起成对）。

    历史坑（已修）：旧代码把 dwFlags 写成 XBUTTON1|XUP=0x0001|0x0004，
    又把编号塞进 mouseData=idx(3/4)——发出去的是
    flags=5/6 的垃圾事件，系统不认，点了没反应。
    正确姿势：MOUSEEVENTF_XDOWN(0x0080)/MOUSEEVENTF_XUP(0x0100)，
    编号 1/2 放 mouseData。这套逻辑现在收在 core/inputs.py 的
    click_xbutton() 里，带注入标记（防回环自触发）。
    """
    inp.click_xbutton(_xbutton_number(token))


def _mouse_hold(token):
    """长按：只发按下，不发抬起。停止时由 stop_mouse 统一松开。"""
    if token == "left":
        inp.press_down("left")
    elif token == "right":
        inp.press_down("right")
    elif token == "middle":
        inp.press_down("middle")
    else:
        inp.press_down_xbutton(_xbutton_number(token))


def _mouse_release_all(timeout=1.5):
    """
    把所有可能按住的鼠标键都松开，防止残留按下状态。

    【为什么必须加超时】
    SendInput 是**同步阻塞**的：它要把事件排进目标窗口的输入队列，
    而系统会等目标线程真正处理完才返回。
    如果前台程序卡死（游戏崩溃后弹「未响应」、远程桌面断连），
    SendInput 就永远返回不了。
    而 _mouse_release_all 出现在 shutdown 和「停止」这两条路上——
    也就是说**目标程序一卡死，本程序就再也关不掉，
    而恰恰此时键还是按住的**，这正是最需要成功的一次释放。

    实测踩到过：残留一个未响应的连点器窗口后，
    任何走SendInput 的调用都会挂死（连 stop_mouse 都卡在第一行）。

    所以放进守护线程执行，主流程最多等 timeout 秒就继续。
    超时意味着前台程序已经无法接收输入了，
    但本进程必须还能退出——不能为了救一个已经卡死的目标把自己也搭进去。
    """
    def _work():
        for b in ("left", "right", "middle"):
            try:
                inp.release(b)
            except Exception:
                pass
        # 侧键：Windows 只有 XBUTTON1/XBUTTON2 两个，编号 1/2。
        # （旧代码循环 4~7 并用 0x0001|0x0004 当标志位，发的是垃圾事件。）
        for xb in (1, 2):
            try:
                inp.release_xbutton(xb)
            except Exception:
                pass

    th = threading.Thread(target=_work, name="release", daemon=True)
    th.start()
    th.join(timeout=timeout)
    return not th.is_alive()      # False = 前台卡死导致没发完


def _arm_exit_watchdog(seconds=8.0):
    """
    退出看门狗：到点直接 ExitProcess，**不等任何线程**。

    【为什么必须有它 —— 对应主人反馈的"进程杀不掉"】
    完整因果链是这样的：

        前台程序卡住（游戏未响应 / 掉进了 UAC 安全桌面）
          → 我们为了安全退出，调 SendInput 把所有按下的键松开
          → SendInput 是**同步阻塞**的：
            事件排进目标线程队列后，要等对方取走才返回；
            对方不取（未响应），它就**永不返回**
          → 调用线程卡在内核态一个不可中断的等待上
          → 这种进程 TerminateProcess 也要等那个等待结束才收得了尸，
            于是任务管理器显示"未响应"、杀不掉
          → Windows 还会让 dwm 接管出一个 ghost window，
            所以在任务管理器里看到的进程名变成了 dwm —— 它杀不掉是应该的
            （系统关键进程），但它只是症状，真正的僵在那儿的是我们自己的进程
          → 只能重启

    看门狗怎么解决：
        正常清理路径走不通时，到点直接 os._exit()。
        它绕开 atexit / finally / 线程 join，直接结束进程，
        所以即使有线程卡在 SendInput 里，进程也能立刻消失。

    代价：os._exit() 跳过所有清理。但看门狗只在"正常退出已超时"时触发，
    且 shutdown() 在此之前已经尽力释放过按键，所以这个代价可以接受。
    """
    def _kill():
        try:
            os._exit(0)
        except Exception:
            pass

    t = threading.Timer(seconds, _kill)
    t.daemon = True
    t.start()
    return t


# ======================================================================
# 系统托盘（S1 · v24）
# ======================================================================
# 【设计取舍】托盘图标**始终存在**（程序在跑就有一个图标，能看到状态、
# 能一键暂停、能退出），但「关闭窗口 = 藏到托盘」是**可选**的
# （界面勾上才生效，默认关）。
#
# 为什么默认关：X 按钮的通用语义是「退出」。默认改成藏起来，用户以为
# 程序关了、实际还在后台往目标窗口注入点击——这种「以为停了其实没停」
# 比「想挂后台却退出了」危险得多。所以让想要的人主动勾一次。
_TRAY = {"obj": None}


def _tray_should_hide(api):
    """关窗时该藏到托盘还是真退出。"""
    if _TRAY.get("obj") is None:
        return False                    # 没有图标 -> 藏起来就再也叫不回来了
    try:
        return bool((api._settings or {}).get("tray", False))
    except Exception:
        return False


def _tray_sync(api=None):
    """把暂停状态同步到托盘（菜单文案 + 悬浮提示）。"""
    t = _TRAY.get("obj")
    if t is None:
        return
    try:
        paused = pausectl.any_paused()
        t.set_paused(paused)
        t.set_tip("爪Pro · " % ("已暂停" if paused else "运行中"))
    except Exception:
        pass


def _tray_start(api):
    """
    挂上托盘图标。失败不算致命：界面照常可用，只是没有托盘入口
    （explorer 没启动、被安全软件拦都会失败），日志留一行。
    """
    if _TRAY.get("obj") is not None:
        return True

    def _on_show():
        api.show_from_tray()

    def _on_toggle():
        # 和右上角按钮、全局热键共用一个入口（toggle_pause 内部会写日志）
        api.toggle_pause()
        _tray_sync(api)

    def _on_exit():
        _log("托盘菜单：退出程序")
        _arm_exit_watchdog(8.0)
        try:
            api.shutdown()
        except Exception:
            pass
        os._exit(0)

    t = tray_mod.Tray(tip="爪Pro · 运行中", on_show=_on_show,
                      on_toggle=_on_toggle, on_exit=_on_exit,
                      is_paused=pausectl.any_paused)
    if not t.start():
        _log("托盘图标未能挂上（explorer 未运行或被拦），界面功能不受影响")
        return False
    _TRAY["obj"] = t
    _log("托盘图标已挂上：双击显示仪表盘 / 右键菜单（显示·暂停·退出）")
    return True


def _tray_stop():
    t = _TRAY.get("obj")
    _TRAY["obj"] = None
    if t is not None:
        try:
            t.stop()
        except Exception:
            pass


def _on_window_closing(api):
    """
    窗口关闭事件的总决策。返回 True = **取消关闭**（藏到托盘）。

    【为什么抽成模块级函数】pywebview 的 closing 回调必须挂在 main()
    里（要 win 对象），但「关还是不关」这个判断是最该被测试覆盖的
    逻辑——留成闭包就只能靠静态断言看源码字符串，负向对照根本做不了。
    抽出来后探针可以直接造一个假 api 打这一枪。
    """
    if _tray_should_hide(api):
        _log("关闭事件：按设置藏到托盘（任务继续）")
        try:
            threading.Thread(target=api.hide_to_tray, daemon=True).start()
        except Exception:
            _log_exc("hide_to_tray")
        return True                    # 阻止真正关闭
    _arm_exit_watchdog(8.0)
    try:
        api.shutdown()
    except Exception:
        pass
    return None


# ======================================================================
# 参数持久化（记住上次设置）
# ======================================================================
# 【为什么单独存一个settings.json 而不是塞进 core/config.py 的 config.json】
# config.json 是 cli.py / app.py 的配置，字段名和语义都跟界面无关
# （cps / button / duration / hotkey_start ...）。
# 把界面的表单值塞进去会互相污染：界面的「动作=点击/长按」和
# config 的 click_type 不是一回事，写同一份文件迟早对不上。
# 分开存最省心，读写互不影响，也不会读到别人的字段。
_SETTINGS_FILE = "ui_settings.json"
# 设置可能被前端自动保存、绑键接口、配置导入同时写；必须把「合并内存 + 落盘」
# 串成一个事务，否则后到的表单快照会把刚写入的 trigger 擦掉。
_SETTINGS_IO_LOCK = threading.RLock()

# 配置导入导出用的 schema 版本与字段白名单（2026-10-16 新增）
_CONFIG_SCHEMA_VERSION = 1
# 允许携带的顶层键（通用参数）。trigger（触发键绑定是设备相关的 raw_id，
# 跨机无意义）、window（历史残留死键，从未被写入）不跨机迁移——
# 别人的鼠标侧键 ID 在你机器上未必是同一个。
_CONFIG_EXPORT_KEYS = ("mouse", "key", "hide")


def _user_data_dir():
    """用户级数据目录（和启动日志同一个地方）。"""
    return os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "MouseAutoClicker")


def _app_dir():
    """
    程序所在目录：打包后 = **exe 所在目录**，源码运行 = 脚本目录。

    【为什么不能用 HERE 当设置文件目录】
    HERE = os.path.dirname(os.path.abspath(__file__))。PyInstaller 单文件
    模式下 __file__ 指向的是**临时解压目录** %TEMP%\\_MEIxxxxx\\，而这个
    目录名每次启动都不一样 —— 上次写进去的设置文件，下次启动根本不在
    那儿，等于没存。表现就是「绑好的 G1/G2、调好的 CPS，关掉重开全没了」。

    2026-10-16 主人实测报回，实测证据：%TEMP% 下全部 _MEI* 历史目录里，
    只有一次留下了 ui_settings.json，其余每次启动都是新目录。
    所以打包态必须从 sys.executable 推目录，不能用 __file__。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return HERE


_settings_dir_pick_cache = [None]


def _settings_dir():
    """
    选设置文件放哪。**优先 exe 同目录**：
      · 便携：拷 exe 给别人时配置跟着走；
      · 看得见：用户能直接找到、直接改，也能拷到别的机器；
      · 和界面上写的承诺一致（见 ui/dashboard.html「参数持久化」那段）。
    只有目录写不进去（Program Files、只读盘）才退回
    %LOCALAPPDATA%\\MouseAutoClicker\\ —— 和启动日志一个地方。
    结果缓存一次，避免每次读写都去试写探针文件。
    """
    if _settings_dir_pick_cache[0]:
        return _settings_dir_pick_cache[0]
    for d in (_app_dir(), _user_data_dir()):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, "_wb_write_probe.tmp")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            _settings_dir_pick_cache[0] = d
            return d
        except Exception:
            continue
    _settings_dir_pick_cache[0] = _user_data_dir()
    return _settings_dir_pick_cache[0]


def _settings_path():
    return os.path.join(_settings_dir(), _SETTINGS_FILE)


def _load_settings():
    """
    读界面参数。任何异常都退回空字典 —— 配置坏了不该让程序起不来。

    命中/未命中都写一行启动日志：以后用户报「设置没记住」，
    第一眼看的就是这行（路径对不对、文件到底在不在）。
    """
    p = ""
    try:
        p = _settings_path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                try:
                    _log("  界面设置     : 已读取 %s（%d 个顶层键）"
                         % (p, len(data)))
                except Exception:
                    pass
                return data
        try:
            _log("  界面设置     : 尚无 %s，本次用默认值" % p)
        except Exception:
            pass
    except Exception as e:
        try:
            _log("  界面设置     : 读取失败 %s -> %r" % (p, e))
        except Exception:
            pass
    return {}


def _save_settings(data):
    """
    原子写界面参数。失败只返回错误信息，不抛。

    成熟桌面应用的常见做法是「同目录临时文件 → flush/fsync → os.replace」：
    进程在 json.dump 中途退出时，旧配置仍是完整文件，不会留下半截 JSON。
    """
    p = ""
    tmp = ""
    try:
        with _SETTINGS_IO_LOCK:
            p = _settings_path()
            # 只保留我们自己认识的顶层键，避免旧版本残留字段堆积。
            # tray 是 v24 的正式设置；旧白名单漏了它，会造成重启后恢复默认。
            keep = ("mouse", "key", "trigger", "window", "hide", "tray")
            out = {k: v for k, v in (data or {}).items() if k in keep}
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(p), prefix=".ui_settings_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(out, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, p)
                tmp = ""
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return True, ""
    except Exception as e:
        try:
            _log("  界面设置     : 写入失败 %s -> %r" % (p, e))
        except Exception:
            pass
        return False, str(e)


# ======================================================================
# 供前端调用的 API（挂在window.pywebview.api 上）
# ======================================================================
class Api(object):
    """前端 JS 通过 window.pywebview.api.xxx() 调用这里的同名方法。"""

    def __init__(self):
        # 【坑】必须是下划线开头。pywebview 6.x 生成 JS 桥时会把 js_api 上
        # 所有**公开**属性递归展开（util.get_functions，无环检测），
        # Window 对象 → native → AccessibilityObject.Bounds.Empty.Empty...
        # 无限自引用直接爆 RecursionError，桥接线程挂掉、主线程死等
        # loaded 事件 → 整个程序「无响应」被系统强杀（AppHang）。
        # 详见 main() 里 api._window = win 处的注释。
        self._window = None
        # 记住上一次启动用的参数：热键触发时没有前端表单可用，
        # 必须用这套「用户已经设好的参数」重新启动，否则热键启动的是默认值。
        self.last_mouse = {"button": "left", "action": "click",
                           "count": "0", "duration": "0", "cps": "10",
                           # v22 新增四项（缺省值 = 关）
                           "jitter": "0", "mode": "cursor", "pos": "",
                           "countdown": "0",
                           # v23 新增两项：抖器挪动间隔 / 收尾动作
                           "jig": "30", "done": "none",
                           # v26 新增：多点轮询/框内随机的坐标列表
                           "points": ""}
        self.last_key = {"keys": "w", "threshold": "2", "hz": "30",
                         "hold": "true", "gap": "20"}
        # 「启动后最小化」勾选：arm_mouse 记住，热键启动时遵守。
        # 默认 True——热键启动时仪表盘若还在前台，连点会点在自己身上。
        self._mouse_hide_on_start = True
        # 运行监控线程（心跳日志 + 提权预警），同时只留一个
        self._monitor_thread = None
        self._monitor_stop = threading.Event()
        self._hotkeys = {}
        # 降级说明按动作分开存（mouse/key）：旧代码共用一个字段，
        # 两个热键都降级时后写的覆盖先写的，用户只看到一条。
        self._hotkey_notes = {}
        self._pause_key = None       # 实际注册上的暂停热键（Pause/ScrollLock）
        self._settings = _load_settings()
        # 上次的参数同时喂给热键回调（前端还没连上时的兜底）
        for k in ("mouse", "key"):
            v = self._settings.get(k)
            if isinstance(v, dict) and v:
                self.last_mouse.update(v) if k == "mouse" \
                    else self.last_key.update(v)
        # 记住上次绑的触发键，关掉重开不用重设
        self._btn_binding = (self._settings.get("trigger")
                             if isinstance(self._settings.get("trigger"),
                                            dict) else {}) or {}

    # ---- 参数持久化 ----
    def save_settings(self, data):
        """
        合并前端表单快照，再把完整内存快照原子落盘。

        前端只提交 mouse/key/hide，不含 trigger。旧实现直接把这份局部快照
        当成整份配置写盘，所以「刚绑好 G2 / Ctrl+Alt+M」会先成功，随后
        被自动保存擦掉；本次运行内存里仍有效，重启才回默认，最容易误判。
        """
        if not isinstance(data, dict):
            return {"ok": False, "msg": "设置必须是字典"}
        with _SETTINGS_IO_LOCK:
            for k in ("mouse", "key", "trigger", "window", "hide", "tray"):
                if k not in data:
                    continue
                v = data[k]
                if isinstance(v, dict):
                    self._settings[k] = dict(v)
                    if k == "trigger":
                        self._btn_binding = self._settings[k]
                elif k in ("hide", "tray") and isinstance(v, bool):
                    self._settings[k] = v
            # 热键回调也读这两份内存，当前会话不必等重启。
            if isinstance(data.get("mouse"), dict):
                self.last_mouse.update(data["mouse"])
            if isinstance(data.get("key"), dict):
                self.last_key.update(data["key"])
            ok, err = _save_settings(self._settings)
        return {"ok": ok, "msg": err}

    def load_settings(self):
        return self._settings

    def _persist_bindings(self):
        """把鼠标触发键 + 系统热键作为一个 trigger 快照原子保存。"""
        with _SETTINGS_IO_LOCK:
            snap = dict(self._btn_binding)
            if isinstance(snap.get("sys"), dict):
                snap["sys"] = dict(snap["sys"])
            self._btn_binding = snap
            self._settings["trigger"] = snap
            return _save_settings(self._settings)

    # ---- 启动时同步给前端 ----
    def bootstrap(self):
        return {
            "buttons": self.list_mouse_buttons(),
            "custom": list(_custom_mouse_buttons.keys()),
            "panic": "F12",
            "hotkey": HOTKEY_MOUSE,
            "pause_key": self._pause_key,
            "hotkey_ok": self._hotkey_ok,
            "hotkey_err": self._hotkey_err,
            "settings": self._settings,
            "trigger": self._btn_binding,
        }

    # ---- 配置导入 / 导出 / 剪贴板 / 多方案 ----
    # 【为什么单独做这套】2026-10-16 主人想「把别人的配置导入进来」分享参数。
    # 朴素做法（存文件+导入按钮）有三个坑：导入会覆盖当前配置没备份、坏文件
    # 没校验会崩、没有便捷分享通道。这里补齐：自动备份 + schema 版本校验 +
    # 字段白名单 + 剪贴板通道 + 多套方案（profiles/ 目录）。

    def _export_dict(self):
        """把当前通用参数收成带版本号的字典。"""
        out = {"schema_version": _CONFIG_SCHEMA_VERSION}
        for k in _CONFIG_EXPORT_KEYS:
            if k in self._settings:
                out[k] = self._settings[k]
        return out

    def export_config(self):
        """导出当前通用参数为带版本号的 JSON 文本（前端拿去下载或复制）。"""
        try:
            return {"ok": True,
                    "text": json.dumps(self._export_dict(), ensure_ascii=False, indent=2)}
        except Exception as e:
            return {"ok": False, "msg": "导出失败：%r" % (e,)}

    def export_config_file(self):
        """导出配置到设置目录，文件名带时间戳（不用文件对话框，最稳）。"""
        try:
            d = _settings_dir()
            fn = "爪Pro配置-%s.json" % time.strftime("%Y%m%d-%H%M%S")
            p = os.path.join(d, fn)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._export_dict(), f, ensure_ascii=False, indent=2)
            return {"ok": True, "path": p}
        except Exception as e:
            return {"ok": False, "msg": "导出失败：%r" % (e,)}

    def _validate_import_dict(self, data):
        """校验导入的配置字典，返回 (ok, cleaned, msg)。
        cleaned 只留白名单键且类型正确；高版本文件直接拒绝。"""
        if not isinstance(data, dict):
            return False, None, "配置不是合法的 JSON 对象"
        v = data.get("schema_version")
        if isinstance(v, int) and v > _CONFIG_SCHEMA_VERSION:
            return False, None, ("配置版本不兼容（文件为 v%d，本程序支持 v%d）"
                                 % (v, _CONFIG_SCHEMA_VERSION))
        cleaned = {}
        for k in _CONFIG_EXPORT_KEYS:
            if k not in data:
                continue
            val = data[k]
            if k == "hide":
                cleaned[k] = bool(val)
            elif isinstance(val, dict):
                cleaned[k] = val
            # 类型不对的键直接丢弃，防坏数据把程序带偏
        if not cleaned:
            return False, None, ("配置里没有可识别的参数（mouse/key/hide）。"
                                 "若是旧版本存出来的空方案，请在界面调好参数后"
                                 "重新「存为方案」")
        return True, cleaned, ""

    def _apply_imported_settings(self, cleaned):
        """写入导入的配置 + 自动备份当前 + 同步热键参数。返回 (ok, msg)。"""
        # 导入前自动备份当前配置，想回去有门
        try:
            p = _settings_path()
            if os.path.isfile(p):
                import shutil
                shutil.copy2(p, p + ".bak")
        except Exception:
            pass  # 备份失败不阻断导入
        # 只更新白名单键，保留 trigger/window 等本机相关项
        for k in _CONFIG_EXPORT_KEYS:
            if k in cleaned:
                self._settings[k] = cleaned[k]
        ok, err = _save_settings(self._settings)
        if not ok:
            return False, "写入失败：" + err
        if isinstance(cleaned.get("mouse"), dict):
            self.last_mouse.update(cleaned["mouse"])
        if isinstance(cleaned.get("key"), dict):
            self.last_key.update(cleaned["key"])
        return True, "已导入（原配置已自动备份）"

    def import_config_text(self, text):
        """从文本导入配置（文件内容或剪贴板内容共用）。"""
        try:
            data = json.loads(text)
        except Exception as e:
            return {"ok": False, "msg": "不是合法的 JSON：%r" % (e,)}
        ok, cleaned, msg = self._validate_import_dict(data)
        if not ok:
            return {"ok": False, "msg": msg}
        ok, msg = self._apply_imported_settings(cleaned)
        return {"ok": ok, "msg": msg}

    # ---- 剪贴板（Win32 直接读写，不依赖 pywebview 的 clipboard）----
    def _set_clipboard_text(self, text):
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            if not u.OpenClipboard(0):
                return False, "无法打开剪贴板（可能被其他程序占用）"
            try:
                u.EmptyClipboard()
                raw = text.encode("utf-16-le") + b"\x00\x00"
                h = k.GlobalAlloc(0x0042, len(raw))  # GMEM_MOVEABLE
                if not h:
                    return False, "分配内存失败"
                p = k.GlobalLock(h)
                if not p:
                    k.GlobalFree(h)
                    return False, "锁定内存失败"
                ctypes.memmove(p, raw, len(raw))
                k.GlobalUnlock(h)
                if not u.SetClipboardData(CF_UNICODETEXT, h):
                    return False, "写入剪贴板失败"
            finally:
                u.CloseClipboard()
            return True, ""
        except Exception as e:
            return False, "剪贴板写入异常：%r" % (e,)

    def _get_clipboard_text(self):
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return False, "剪贴板不是文本"
            if not u.OpenClipboard(0):
                return False, "无法打开剪贴板"
            try:
                h = u.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return False, "剪贴板为空"
                size = k.GlobalSize(h)
                buf = ctypes.create_string_buffer(size)
                p = k.GlobalLock(h)
                if not p:
                    return False, "锁定内存失败"
                ctypes.memmove(buf, p, size)
                k.GlobalUnlock(h)
                return True, buf.raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
            finally:
                u.CloseClipboard()
        except Exception as e:
            return False, "剪贴板读取异常：%r" % (e,)

    def copy_config_clipboard(self):
        """把当前配置复制到剪贴板（分享给别人最顺手，不用传文件）。"""
        r = self.export_config()
        if not r["ok"]:
            return r
        ok, msg = self._set_clipboard_text(r["text"])
        if not ok:
            return {"ok": False, "msg": "复制到剪贴板失败：" + msg}
        return {"ok": True, "msg": "配置已复制到剪贴板"}

    def paste_config_clipboard(self):
        """从剪贴板读取配置并导入。"""
        ok, text = self._get_clipboard_text()
        if not ok:
            return {"ok": False, "msg": text}
        return self.import_config_text(text)

    # ---- 多套方案（profiles/ 目录，文件名即方案名）----
    # 【v23 S5】目录与路径规则统一下沉到 core/config.py：命令行版
    # 的 --profile 要读同一份方案，两边各写一遍必然漂移（一个存
    # exe 旁一个存 LOCALAPPDATA，用户就「命令行找不到方案」）。
    # 但**必须把本进程的设置目录传进去**：图形版的 _settings_dir()
    # 可被重定向（回归测试的临时目录、将来可能的自定义路径），
    # 让 core.config 自己再算一遍就会出现「界面存这里、测试/CLI 找那里」。
    def _profiles_dir(self):
        return cfg.profiles_dir(_settings_dir())

    def _profile_path(self, name):
        return cfg.profile_path(name, _settings_dir())

    def list_profiles(self):
        try:
            d = self._profiles_dir()
            names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
            return {"ok": True, "profiles": names}
        except Exception as e:
            return {"ok": False, "msg": str(e), "profiles": []}

    def save_profile(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "msg": "方案名不能为空"}
        try:
            p = self._profile_path(name)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._export_dict(), f, ensure_ascii=False, indent=2)
            return {"ok": True, "msg": "已保存方案：%s" % name}
        except Exception as e:
            return {"ok": False, "msg": "保存方案失败：%r" % (e,)}

    def load_profile(self, name):
        if not name:
            return {"ok": False, "msg": "方案名不能为空"}
        try:
            p = self._profile_path(name)
            if not os.path.isfile(p):
                return {"ok": False, "msg": "方案不存在：%s" % name}
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            return {"ok": False, "msg": "读方案失败：%r" % (e,)}
        return self.import_config_text(text)

    def delete_profile(self, name):
        try:
            p = self._profile_path(name)
            if not os.path.isfile(p):
                return {"ok": False, "msg": "方案不存在：%s" % name}
            os.remove(p)
            return {"ok": True, "msg": "已删除方案：%s" % name}
        except Exception as e:
            return {"ok": False, "msg": "删除方案失败：%r" % (e,)}

    @property
    def _hotkey_ok(self):
        return bool(self._hotkeys)

    @property
    def _hotkey_err(self):
        return self._hotkey_notes.get("mouse", "") or \
            self._hotkey_notes.get("key", "") or \
            self._hotkey_notes.get("macro", "")

    def list_mouse_buttons(self):
        """返回可用的鼠标按键列表（内置 + 用户识别添加的）。"""
        items = [{"id": str(i), "name": n} for (i, n, _) in
                 BUILTIN_MOUSE_BUTTONS]
        with _custom_lock:
            for name, idx in _custom_mouse_buttons.items():
                items.append({"id": "xb%d" % idx, "name": name})
        return items

    def add_custom_mouse(self, name):
        """
        把「刚刚按下的那个鼠标键」记为自定义按键。

        【修复】旧实现读的是 _raw_keys["wparam"] —— 那是解析错位的字段
        （RAWINPUTHDR 第 4 个 DWORD 其实是 usButtons 的低 16 位，不是 wParam），
        算出来的编号基本是随机的，所以「识别并添加自定义按键」本质上是盲分配：
        点了添加就随便找个空槽，用户按的键和记录的对不上。
        现在 Raw Input 里正确解析出了 usButtons 位图，
        可以准确知道「你刚按的是哪个键」。

        【用法】先调arm_custom_mouse() 清空历史，再让用户按侧键，
        最后调本方法提交——两步式才不会读到上次的旧按键。
        """
        with _raw_lock:
            ids = list(_raw_keys.get("ids", []))
        # 只认非标准键：左/右/中是内置的，3/4 是内置侧键1/2
        extra = [i for i in ids if i >= 3]
        if not extra:
            return {"ok": False,
                    "msg": "没收到任何标准鼠标键，请检查鼠标模式/驱动："
                           "① 优先用有线或 2.4G（蓝牙模式常把侧键报成键盘/多媒体事件）；"
                           "② 官方驱动里把侧键设为「鼠标按键 4/5」；"
                           "③ 先试中键——中键能识别，就说明是侧键上报的问题。"
                           "（点「开始识别」后 8 秒内按键有效）",
                    "buttons": self.list_mouse_buttons()}
        bid = extra[-1]                     # 同时按多个时用最后按下的那个
        with _custom_lock:
            same = [k for k, v in _custom_mouse_buttons.items() if v == bid]
        if same:
            return {"ok": False, "msg": "该按键已在列表里：%s" % same[0],
                    "buttons": self.list_mouse_buttons()}

        label = _BUTTON_LABEL_BY_ID.get(bid, "自定义按键%d" % bid)
        name = name or label
        with _custom_lock:
            _custom_mouse_buttons[name] = bid
        ST.set_msg("已识别并添加：%s" % name)
        return {"ok": True, "msg": "已添加：%s" % name,
                "buttons": self.list_mouse_buttons()}

    def arm_custom_mouse(self):
        """进入「等待你按侧键」状态：清空历史按键记录。"""
        with _raw_lock:
            _raw_keys["ids"] = []
            _raw_keys["buttons"] = 0
        ST.set_msg("请在 8 秒内按一下鼠标侧键，然后点「完成」")
        return {"ok": True, "msg": "请按一下侧键（8 秒内），然后点「完成」"}

    # ---- 鼠标键当热键 ----
    def set_button_hotkey(self, button, action):
        """
        把某个鼠标键绑成热键（兼容旧的「下拉选择 + 绑定」接口）。

        action: "mouse"启停鼠标连点 / "key" 启停键盘锁定 /
                "macro" 启停宏回放 / "" 取消全部鼠标键绑定
        """
        try:
            btn = str(button or "").strip().lower()
            act = str(action or "").strip().lower()
        except Exception:
            return {"ok": False, "msg": "参数错误"}

        if not btn or btn == "none":
            bt.HUB.clear()
            for k in ("mouse", "key", "macro"):
                self._btn_binding.pop(k, None)
            ok, err = self._persist_bindings()
            ST.set_msg("已清除全部鼠标键热键")
            return {"ok": ok,
                    "msg": "已清除" if ok else "本次已清除，但保存失败：" + err,
                    "labels": bt.HUB.list_labels(),
                    "bindings": bt.HUB.list_bindings()}

        # v34：宏区也能绑鼠标键（比如侧键 G1 一键回放），和 F7 并存
        if act not in ("mouse", "key", "macro"):
            return {"ok": False, "msg": "动作只能是 mouse / key / macro"}

        cb = (self.toggle_mouse if act == "mouse"
              else self.toggle_macro if act == "macro"
              else self.toggle_keyboard)
        try:
            # 必须传 action：HUB 靠它实现「一个动作只留一个触发键」，
            # 也是触发方式区按动作反查的依据（不传则界面永远显示 F5）
            bt.HUB.set(btn, cb, action=act)
        except ValueError as e:
            return {"ok": False, "msg": str(e)}

        self._btn_binding[act] = bt.HUB.list_by_action()[act]["raw_id"]
        ok, err = self._persist_bindings()
        lbl = bt.BUTTON_LABELS.get(btn, btn)
        what = ("鼠标连点" if act == "mouse"
                else "宏回放" if act == "macro" else "键盘锁定")
        ST.set_msg("已绑定：按%s%s" % (lbl, what))
        return {"ok": ok,
                "msg": ("已绑定 %s → %s" % (lbl, what)) if ok
                       else ("本次已绑定，但保存失败：" + err),
                "labels": bt.HUB.list_labels(),
                "bindings": bt.HUB.list_bindings()}

    # ---- 触发键捕获：按一下就绑，替掉只能选三项的下拉框 ----
    def arm_trigger_capture(self):
        """
        进入「请按一下你想用的键」状态。

        为什么不留下拉：
          - 鼠标键下拉只有 middle/G1/G2 三项，游戏鼠标的 G3~G8 绑不了
          - 而 Raw Input 已经能报出完整 16 位位图，只是没人用
        所以直接让用户按，捕获到哪个编号就绑哪个。
        """
        with _raw_lock:
            _capture["armed"] = True
            _capture["raw_id"] = None
            _capture["timed_out"] = False
            _capture["timeout_msg"] = ""
            _capture["t0"] = time.monotonic()
            _capture["msg0"] = int(_raw_stat["messages"])
        ST.set_msg("请按一下想用作触发键的鼠标键"
                   "（左键/右键除外，中键与各侧键均可；8 秒内没有按键会自动取消）")
        return {"ok": True,
                "msg": "请按一下鼠标键…（左键/右键除外，8 秒内有效）",
                "timeout": float(_capture["timeout"])}

    def cancel_trigger_capture(self):
        with _raw_lock:
            _capture["armed"] = False
            _capture["raw_id"] = None
            _capture["timed_out"] = False
            _capture["timeout_msg"] = ""
        return {"ok": True}

    def take_captured_key(self, seq):
        """
        前端轮询用：拿捕获结果。**必须带 seq**：
        前端记住上次拿到的 seq，只有 seq 变大才有新结果，
        否则每次轮询都会把同一个键重复绑一遍
        （表现为「绑一次之后鼠标键被反复重绑，触发行为错乱」）。
        """
        want = int(seq or 0)
        _capture_tick()          # v29：超时判定必须在取结果之前跑
        with _raw_lock:
            cur = _capture["seq"]
            if cur <= want:
                return {"ok": True, "got": False, "seq": cur}
            if _capture["timed_out"]:
                # 超时也要变成一个「结果」送出去一次，否则前端
                # 永远等不到事件、提示也刷不出来。
                msg = _capture["timeout_msg"] or _CAPTURE_TIMEOUT_MSG
                _capture["timed_out"] = False
                _capture["timeout_msg"] = ""
                return {"ok": False, "got": True, "seq": cur,
                        "timeout": True, "msg": msg}
            if _capture["raw_id"] is None:
                return {"ok": True, "got": False, "seq": cur}
            raw_id = _capture["raw_id"]
            _capture["raw_id"] = None
        name = bt.RAW_ID_TO_NAME.get(raw_id)
        if name is None:
            return {"ok": False, "got": False, "seq": cur,
                    "msg": "该按键编号 %d 无法识别" % raw_id}
        if raw_id < 2:
            # v29 文案：区分「你自己点界面被录进来」和「设备没上报」。
            # 旧文案只说「不能用作触发键」，用户会以为侧键被识别成了左键。
            return {"ok": False, "got": True, "seq": cur,
                    "name": name, "raw_id": raw_id,
                    "label": bt.RAW_ID_LABELS.get(raw_id, name),
                    "msg": "捕获到的是左键/右键——多半是你自己点界面时被录进来了。"
                           "按完「按下要绑的键」之后不要再碰左键，"
                           "把光标停在窗口上直接按侧键；按 Esc 可取消。"}
        return {"ok": True, "got": True, "seq": cur, "name": name,
                "raw_id": raw_id,
                "label": bt.RAW_ID_LABELS.get(raw_id, name)}

    def bind_captured_key(self, raw_id, action):
        """
        把捕获到的鼠标键绑到某个动作上。

        action: "mouse" 启停鼠标连点 / "key" 启停键盘锁定 /
                "macro" 启停宏回放（v34）
        """
        try:
            rid = int(raw_id)
        except Exception:
            return {"ok": False, "msg": "按键编号无效"}
        if str(action or "").strip().lower() not in ("mouse", "key", "macro"):
            return {"ok": False, "msg": "动作只能是 mouse / key / macro"}
        act = str(action).strip().lower()
        cb = (self.toggle_mouse if act == "mouse"
              else self.toggle_macro if act == "macro"
              else self.toggle_keyboard)
        try:
            bt.HUB.set_by_raw_id(rid, cb,
                                 label=bt.RAW_ID_LABELS.get(rid),
                                 action=act)
        except ValueError as e:
            return {"ok": False, "msg": str(e),
                    "labels": bt.HUB.list_labels()}
        lbl = bt.RAW_ID_LABELS.get(rid, "按键%d" % rid)
        what = ("鼠标连点" if act == "mouse"
                else "宏回放" if act == "macro" else "键盘锁定")
        ST.set_msg("已绑定：按%s%s" % (lbl, what))
        # 存进设置文件：关掉重开不用重设触发键。
        # 必须保存完整内存快照；前端自动保存只提交 mouse/key/hide，
        # 若把局部表单直接覆盖整文件，trigger 会在几百毫秒后被擦掉。
        self._btn_binding[act] = rid
        ok, err = self._persist_bindings()
        return {"ok": ok,
                "msg": ("已绑定 %s → %s" % (lbl, what)) if ok
                       else ("本次已绑定，但保存失败：" + err),
                "labels": bt.HUB.list_labels(),
                "bindings": bt.HUB.list_bindings()}

    def button_hotkeys(self):
        """
        给前端显示当前生效的鼠标键热键。

        by_action 是给「触发方式」区用的：界面问的是
        「鼠标连点现在绑的是哪个键」，按动作查才对。
        只给 labels（按键名索引）的话，前端拿 labels.mouse
        永远取不到，只能回退显示系统热键 F5 ——
        「绑了 G2 触发方式还显示 F5」就是这么来的。
        """
        return {"labels": bt.HUB.list_labels(),
                "bindings": bt.HUB.list_bindings(),
                "by_action": bt.HUB.list_by_action()}

    def raw_diag(self):
        """
        原始输入链路自检，给「绑键没反应」时第一时间看。

        界面上直接给出四件事：
          mode       —— 走的是原始输入还是降级轮询
          sink/reg   —— 隐藏窗口和注册两步各自成功没有
          messages   —— 到底收到几条原始输入消息（按一下键应该 +1）
          last_ids   —— 最后按下的是几号键（2=中键 3=G1 4=G2 …）

        排查口诀：
          messages 一直是 0 → 链路没通，看 error 字段；
          messages 在涨但绑不上 → 链路通了，问题在绑定/持久化那一侧。
        """
        # v29：先跑一次超时判定。探针/日志读到的 armed 必须是
        # 「8 秒后真的会变 False」，否则这个字段没有任何诊断价值。
        _capture_tick()
        with _raw_lock:
            stat = dict(_raw_stat)
            stat["last_ids"] = list(_raw_stat["last_ids"])
            stat["armed"] = bool(_capture["armed"])
            stat["seq"] = int(_capture["seq"])
            stat["timed_out"] = bool(_capture["timed_out"])
            stat["timeout"] = float(_capture["timeout"])
            stat["timeout_msg"] = _capture["timeout_msg"]
        # v30：最近按下（锁存）+ 距今秒数。
        # 前端拿 age 自己决定还显示多久，后端只报事实，不在后端做超时清空——
        # 后端一旦清空，探针和界面看到的又会不一致。
        lp = int(stat.get("last_press_id", -1))
        if lp >= 0:
            stat["last_press"] = {
                "id": lp,
                "name": _RAW_ID_NAMES.get(lp, "#%d" % lp),
                "age": round(max(0.0, time.monotonic() -
                                 float(stat.get("last_press_at") or 0.0)), 2),
            }
        else:
            stat["last_press"] = None
        if stat["mode"] == "raw":
            stat["hint"] = ("原始输入正常：按一下鼠标键，"
                            "「消息数」应当增加。")
        elif stat["mode"] == "poll":
            stat["hint"] = ("当前为降级轮询，只能识别"
                            "鼠标中键 / G1 / G2，G3 以上绑不了。")
        elif stat["mode"] == "fail":
            stat["hint"] = "监听链路完全不可用，请重启程序再试。"
        else:
            stat["hint"] = "监听线程尚未启动，稍等一两秒再看。"
        return stat

    # ---- 鼠标 ----
    def arm_mouse(self, params, hide=True):
        """
        按钮路径：**只装填参数进入就绪态，不启动连点**。

        【为什么按钮不直接启动】主人 2026-10-15 明确的两段式预期：
        「设定好参数以后点启动应该是准备就绪，然后按鼠标连点启停键
        （默认 F5）再开始」。旧行为是点启动立即触发，于是第一次按
        F5 被当成「停止正在跑的任务」——主人看到的就是
        「点第一次 F5 就暂停」。

        就绪态下按 F5（toggle_mouse）才真启动，且启动时仪表盘
        自动最小化让位（hotkey start 的 hide 由本方法记住）。
        """
        # 【v22】参数规范化收进 _parse_mouse_params：arm_mouse 与
        # start_mouse 共用同一份解析规则。以前两处各写一遍 try/int()，
        # 新增抖动/坐标/倒计时后必然漂移（一处解析了一处没有，
        # 热键路径和按钮路径行为就不一致了）。
        p, err = _parse_mouse_params(params or {})
        if err:
            return {"ok": False, "msg": err}
        token, action = p["button"], p["action"]
        count, duration, cps = p["count"], p["duration"], p["cps"]
        jitter, mode, fixed, countdown = (p["jitter"], p["mode"],
                                          p["pos"], p["countdown"])

        # 记住参数：热键启动时要用它（与 start_mouse 存同一份）
        self.last_mouse = dict(p)
        self._mouse_hide_on_start = bool(hide)

        with ST.lock:
            if ST.mouse_running:
                # 触发键现在只切暂停，能真正结束的只有「停止鼠标」按钮
                return {"ok": False, "msg": "鼠标任务正在运行，"
                                           "先点「停止鼠标」才能重新装填"}
            ST.mouse_armed = True
            # 复位「用户停止」标志：上次停止留下的 True 会把下次
            # 自然完成误判成用户中断（收尾动作就永远不触发了）。
            ST.mouse_stop_requested = False
        trig = self._trigger_label("mouse")
        # 就绪 = 全新一轮：把上一轮可能残留的本区暂停清掉，
        # 否则「暂停过一次 → 停止 → 再就绪 → 按中键」会开着一个不动的连点。
        pausectl.set_paused(False, "mouse")
        ST.set_msg("已就绪：按 %s 开始连点（启动后仪表盘自动让位）"
                   % trig, zone="mouse")
        toast.show_mouse_ready(trig)
        # 位置文案：v26 四种模式都要在日志里认得出（排障第一眼看这行）
        _pos_desc = _pos_desc_of(p)
        _log("鼠标区已就绪（未启动）：button=%s action=%s cps=%s count=%s "
             "duration=%s 抖动=±%sms 位置=%s 倒计时=%ss 启动时最小化=%s"
             % (token, action, cps, count, duration, jitter,
                _pos_desc, countdown, bool(hide)))
        return {"ok": True,
                "msg": "已就绪。按 %s 开始连点，再按 %s 暂停；"
                       "点「停止鼠标」才真正结束" % (trig, trig)}

    def cursor_pos(self):
        """
        取当前鼠标像素坐标（固定坐标模式的「取当前」按钮用）。

        【为什么单独开 API 而不是让 JS 算】DPI 缩放下网页里的坐标
        和屏幕像素不是一回事（v14 踩过：96dpi 虚拟化让坐标整体偏移）。
        坐标只有后端用 GetCursorPos 拿才准。
        """
        try:
            x, y = inp.get_cursor_pos()
            return {"ok": True, "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def cancel_done_action(self):
        """
        取消待执行的收尾动作（v23 S4 的「取消待执行动作」按钮）。
        关机指令已发时会顺带发 shutdown /a 撤销系统层指令。
        """
        cancelled, msg = actions.cancel()
        ST.set_msg(msg, zone="mouse")
        _log("收尾动作取消：%s" % msg)
        return {"ok": True, "cancelled": cancelled, "msg": msg}

    def start_mouse(self, params, hide=True):
        """
        启动鼠标连点 / 长按。

        hide=True 时启动成功后**把仪表盘窗口最小化**。
        这是解决「仪表盘抢前台，没法点目标界面」的关键：
        连点/长按必须发生在别的程序上，而 SendInput 注入到哪，
        取决于那一刻哪个窗口在前台。所以必须让仪表盘主动让位。
        """
        # 【v27】录制期间不许启动别的任务：注入事件虽然会被录制器
        # 按标记过滤掉，但用户录到的是「程序自己在动」的画面，
        # 这份宏一步都不可信，不如直接拦住。
        if _macro_recording():
            return {"ok": False,
                    "msg": "宏正在录制，先按 Pause 结束录制再启动"}
        # 【v22】与 arm_mouse 共用同一份规范化（见 _parse_mouse_params）
        p, err = _parse_mouse_params(params or {})
        if err:
            return {"ok": False, "msg": err}
        token, action = p["button"], p["action"]
        count, duration, cps = p["count"], p["duration"], p["cps"]

        # 记住参数：热键触发时要用它
        self.last_mouse = dict(p)

        with ST.lock:
            if ST.mouse_running:
                return {"ok": False, "msg": "鼠标任务已在运行"}
            ST.mouse_armed = False     # 真启动后就不再是就绪态
            ST.mouse_stop_requested = False

        # 长按模式（按住不放）走 press_down / release
        if action == "hold":
            _mouse_hold(token)
            with ST.lock:
                ST.mouse_running = True
                ST.mouse_info = "%s 长按中" % token
                ST.started_at = time.time()
            ST.set_msg("鼠标已开始长按（%s）· 按 %s 停止"
                       % (token, self._hotkey_label("mouse")), zone="mouse")
            toast.show_custom("鼠标长按 已启动（%s）" % token, "start")
            _log("鼠标长按已启动：button=%s hide=%s" % (token, bool(hide)))
            self._hide_if_needed(hide)
            return {"ok": True, "msg": "已长按（%s）· 按 %s 停止"
                                      % (token, self._hotkey_label("mouse"))}

        # 【v22】点击类型/固定坐标/倒计时传给插件；抖动传给引擎
        # （ClickContext.base_interval() 早就实现了 ±抖动，只是以前
        # dashboard 创建引擎时没传 jitter_ms——白埋了一个版本的功能）。
        # 【v23 S6】抖器动作走 plugins/jiggler（只挪不点），
        # 其余参数（间隔/次数/时长/倒计时/暂停）全部通用。
        if action == "jiggle":
            from plugins.jiggler import Jiggler
            plugin = Jiggler()
            engine_options = {"interval": p["jig"], "distance": 1}
        else:
            plugin = _make_mouse_plugin(
                token, duration,
                click_type=action if action != "hold" else "single",
                fixed_pos=p["pos"], countdown=p["countdown"],
                points=p["points"], pos_mode=p["mode"])
            # 【v26】引擎的 options.mode 只认 cursor/fixed（多点/框内由插件
            # 自己搬鼠标）。所以 multi/box 一律告诉引擎「跟随光标」，
            # 否则引擎的 cursor_position() 会返回 None 或旧坐标，
            # 让别的插件（拖拽/宏）也跟着乱。
            engine_options = {"mode": "fixed" if p["mode"] == "fixed"
                              else "cursor"}
        # fixed_pos 只给「固定坐标」用；multi/box 的坐标在插件里，
        # 引擎拿到单点反而会让 cursor_position() 撒谎。
        engine_fixed = p["pos"] if p["mode"] == "fixed" else None
        e = eng.ClickerEngine(plugin, options=engine_options,
                             base_cps=cps, jitter_ms=p["jitter"],
                             duration=duration, count_limit=count,
                             fixed_pos=engine_fixed)

        # 结束回调：次数用完 / 时间到了 / 出错 时自动把状态回滚，
        # 否则前端会一直显示「运行中」（实际线程早已退出）。
        def _finished(ctx):
            # 【v23 S4】先读「是不是用户按了停止」——停止的收尾由
            # stop_mouse 全权负责，这里再写一遍消息会把「已停止」
            # 覆盖成「已完成」，收尾动作更绝不能触发。
            user_stopped = ST.mouse_stop_requested
            with ST.lock:
                if ST.mouse_engine is e:
                    ST.mouse_engine = None
                    ST.mouse_running = False
                    ST.mouse_info = ""
                    ST.started_at = 0
                ST.mouse_stop_requested = False
            if getattr(ctx, "error", None):
                ST.set_msg("鼠标任务出错：%s" % ctx.error, zone="mouse")
                _log("鼠标任务出错：%s（已注入 %s 次）"
                     % (ctx.error, getattr(ctx, "clicked", 0)))
                return
            if user_stopped:
                # 用户主动停止：stop_mouse 已经给过消息和 toast，这里
                # 什么都不做（避免覆盖 + 避免误触发收尾动作）。
                return
            # 自然完成 = 次数或时长上限到了（不是用户中断）。
            # FakeCtx 之类的桩没有 tick_count/elapsed，用 getattr 兜底。
            ticks = getattr(ctx, "tick_count", 0)
            try:
                el = ctx.elapsed()
            except Exception:
                el = 0
            natural = ((count and ticks >= count) or
                       (duration and el >= duration - 1.0))
            if not natural:
                return
            ST.set_msg("鼠标任务已完成（次数或时间已到）", zone="mouse")
            _log("鼠标任务自然结束：共注入 %s 次"
                 % getattr(ctx, "clicked", 0))
            toast.show_mouse_stopped()
            # 【v23 S4】收尾动作：提示音/锁屏/关机（30 秒倒计时可取消）。
            # none 时压根不调 arm——不给「排了个空动作」的错觉。
            if p["done"] not in ("none", "") and \
                    actions.arm(p["done"], on_tick=_done_tick):
                _log("已排定收尾动作「%s」：%d 秒后执行，可取消"
                     % (p["done"], actions.DEFAULT_SECONDS))
                toast.show_custom(
                    "次数/时长已用完：%d 秒后%s，回仪表盘可取消"
                    % (actions.DEFAULT_SECONDS,
                       {"sound": "响提示音", "lock": "锁屏",
                        "shutdown": "关机"}.get(p["done"], p["done"])),
                    "stop", 8)

        e.on_finish = _finished

        # 动作标签：双击/三击/抖器要在状态行写清楚，否则用户看到
        # 「连点」实际每轮点两下，次数对不上会以为程序坏了
        act_label = {"click": "连点", "double": "双击", "triple": "三击",
                     "jiggle": "抖器"}
        act_label = act_label.get(action, "连点")

        with ST.lock:
            ST.mouse_engine = e
            ST.started_at = time.time()
        if not e.start():
            with ST.lock:
                if ST.mouse_engine is e:
                    ST.mouse_engine = None
            return {"ok": False, "msg": "启动失败"}
        # 【v23 修竞态】插件可能在 e.start() 返回后**瞬间就结束**
        # （on_start 返回 False / 首轮就抛异常）：_finished 已经把状态
        # 清干净并写好「出错」。这时若继续无条件置运行态 + 写「已启动」，
        # 会把出错消息盖掉——表现为「显示启动了但什么都没发生」。
        # 所以先确认引擎还归我们管，再写运行态。
        with ST.lock:
            alive = (ST.mouse_engine is e) and not ST.mouse_stop_requested
            if alive:
                ST.mouse_running = True
                # 倒计时期间 on_start 会把 mouse_info 覆盖成「倒计时 Nds…」；
                # 这里给初值，防止线程还没跑就先显示成普通连点
                ST.mouse_info = ("%s %s（倒计时 %ds）"
                                 % (token, act_label, p["countdown"])
                                 if p["countdown"] else
                                 "%s %s" % (token, act_label))
        if not alive:
            # 引擎已自行结束：_finished 写过的消息（出错/已完成）即最终态，
            # 这里再写任何「已启动」都是盖错误消息。
            _log("鼠标引擎启动后即刻结束（首轮异常或 on_start 拒绝），"
                 "不写启动消息；最终消息以状态行为准")
            return {"ok": True, "msg": "引擎已结束，请看状态行"}
        # 分区消息：鼠标区只显示鼠标的事（主人反馈「鼠标暂停也显示在键盘区」）
        ST.set_msg("鼠标连点已启动（%s %s）· 按 %s 或触发键停止"
                   % (token, act_label, self._hotkey_label("mouse")),
                   zone="mouse")
        # 置顶弹窗：启动后仪表盘会最小化，靠它确认状态
        toast.show_mouse_started(cps, self._trigger_label("mouse"),
                                 p["jitter"])
        _log("鼠标连点已启动：button=%s action=%s cps=%s count=%s duration=%s "
             "抖动=±%sms 位置=%s 倒计时=%ss hide=%s 本进程提权=%s"
             % (token, action, cps, count, duration, p["jitter"],
                _pos_desc_of(p),
                p["countdown"], bool(hide), our_elevated()))
        # 提权预警：目标是提权窗口（如管理员身份的原神）而我们没有，
        # 注入会被 UIPI 静默拦掉——界面却显示"已启动"，极难排查。
        self._start_elevation_watchdog()
        self._hide_if_needed(hide)
        return {"ok": True, "msg": "已开始连点（%s）· 按 %s 停止"
                                  % (token, self._hotkey_label("mouse"))}

    def _hide_if_needed(self, hide):
        """最小化仪表盘窗口，把前台让给目标程序。"""
        if not hide or self._window is None:
            return
        try:
            # 延迟一小会儿再最小化：让前端先把提示消息渲染出来，
            # 否则用户什么都看不到，只觉得「点了没反应」。
            def _later():
                time.sleep(0.35)
                try:
                    self._window.minimize()
                except Exception:
                    pass
            threading.Thread(target=_later, daemon=True).start()
        except Exception:
            pass

    def toggle_mouse(self):
        """
        触发键回调：鼠标区「就绪 -> 启动 <-> 暂停 <-> 恢复」。

        【三段式 · 2026-10-16】主人实测反馈：
        「就绪后按中键开始，再按中键就结束变未就绪了，想再按中键继续
        连点没反应 —— 结束键等于没有作用」。

        于是语义改成：**触发键只管启停循环，真正结束只有「停止」按钮**：

        | 当前状态 | 按触发键 | 结果 |
        |---|---|---|
        | 未就绪 | 忽略 + 提示 | 保持未就绪 |
        | 已就绪 | 启动连点 | 运行中（armed 清掉） |
        | 运行中 | 暂停 | 仍显示运行中，但零注入 |
        | 已暂停 | 恢复 | 接着跑 |

        要点：暂停期间 `mouse_running` 保持 True —— 一旦置 False，
        下一次按触发键会走进「未就绪」分支，又要回界面点「① 就绪」，
        正是主人抱怨的那个死循环。

        【坑·2026-10-15】旧代码没有 armed 检查，未就绪也直接
        start_mouse(last_mouse)——中键又是默认触发键，主人实测
        「直接点中键就开始连点，和鼠标区是否就绪没有任何关系」。
        两段式的全部意义就是「点①就绪 = 装参数，触发键 = 真启动」，
        少了这道检查，两段式对触发键路径等于不存在。
        """
        trig = self._trigger_label("mouse")
        with ST.lock:
            running = ST.mouse_running
            armed = ST.mouse_armed
        if running:
            # 运行中 / 暂停中：切换的是**本区**暂停，不是停止
            now = self.set_zone_paused("mouse", None)
            if now:
                ST.set_msg("鼠标连点已暂停 · 再按 %s 继续" % trig, zone="mouse")
                toast.show_mouse_paused(trig)
                _log("触发键 %s：鼠标连点 -> 暂停" % trig)
                return {"ok": True, "msg": "已暂停 · 再按 %s 继续" % trig}
            ST.set_msg("鼠标连点已继续 · 再按 %s 暂停" % trig, zone="mouse")
            toast.show_mouse_resumed(trig)
            _log("触发键 %s：鼠标连点 -> 恢复" % trig)
            return {"ok": True, "msg": "已继续 · 再按 %s 暂停" % trig}
        if not armed:
            msg = ("鼠标区还未就绪：请先在界面点「① 就绪」装好参数，"
                   "再按 %s 开始" % trig)
            ST.set_msg(msg, zone="mouse")
            toast.show_custom("鼠标区 未就绪\n先点界面「① 就绪」再按 %s" % trig,
                              "warn")
            _log("触发键被忽略：鼠标区未就绪（未点「① 就绪」），未启动")
            return {"ok": False, "msg": msg}
        _log("触发键 %s：就绪态 -> 启动鼠标连点" % trig)
        return self.start_mouse(self.last_mouse,
                                hide=self._mouse_hide_on_start)

    def set_zone_paused(self, zone, flag=None):
        """
        只暂停/恢复某一个区（"mouse" / "key"）。

        flag=None → 翻转该区当前状态，返回翻转后的值。
        全局 Pause 热键不走这里（它调 set_paused(None) 一次停两区）。

        【2026-10-17 修复】恢复某个区时，如果全局暂停还开着，
        旧代码调 pausectl.set_paused(False)（zone=None）——它会**把两个区
        的标志一起清掉**。主人实测报回：「按 G1 恢复键盘锁定，
        已经暂停的鼠标区也跟着恢复了」。判定本来就是「全局 OR 本区」，
        想恢复本区只需把**全局位**解除掉，别的区暂停与否与本次操作无关。
        改用 pausectl.clear_global()：只清全局位，分区标志原样保留。
        """
        if flag is None:
            flag = not pausectl.is_paused(zone)
        flag = bool(flag)
        if not flag and pausectl.is_paused():
            # 全局暂停（Pause 键/全局按钮）还开着时，单区恢复是不生效的——
            # 判定是「全局 OR 本区」。用户按触发键说「继续」却纹丝不动
            # 会被当成 bug，所以这里顺手把全局位解除（仅此而已）。
            pausectl.clear_global()
            _log("分区恢复：全局暂停仍在，已解除全局位（不影响其他区）")
        pausectl.set_paused(flag, zone)
        return flag

    def _hotkey_label(self, tag):
        """
        当前实际生效的热键显示名。

        【坑·2026-10-15】文案不能写死 HOTKEY_MOUSE/HOTKEY_KEY 常量：
        F5/F6 在本机被其他软件占用（实测 RegisterHotKey 返回 1409），
        注册成功后 _try_hotkeys 存的是降级后的真键（如 F8/F9），
        而就绪提示仍旧写「按 F6 启动」——用户按 F6 自然没有任何效果，
        界面上还找不到线索。所有给用户看的键名一律走这里取真键。
        """
        h = self._hotkeys.get(tag)
        if h:
            return hk.pretty_key(h[0])
        # v34：宏区也有自己的热键了，回落名不能张冠李戴写成 F5/F6
        if tag == "macro":
            return HOTKEY_MACRO_FALLBACK[0].upper()
        return HOTKEY_MOUSE if tag == "mouse" else HOTKEY_KEY

    def _trigger_label(self, tag):
        """
        给用户看的**真正能按下去的那个键**。

        与 _hotkey_label 的区别：触发键有两条路——
          ① 鼠标键触发器（默认中键，可绑侧键，走 Raw Input）
          ② 系统热键（F5/F6，被占了会降级到 F8/F9…）
        用户实际按的是**鼠标键**，而 _hotkey_label 只会报系统热键名，
        于是通知里写「按 F9 开始」、主人手里按的却是中键——文案和行为对不上。

        所以：**绑了鼠标键就报鼠标键名，没绑才回落热键名**。
        """
        try:
            b = bt.HUB.list_by_action().get(tag)
            if b and b.get("label"):
                return str(b["label"])
        except Exception:
            pass
        return self._hotkey_label(tag)

    def stop_mouse(self):
        """
        **唯一**回到「未就绪」的出口。

        【2026-10-16】主人要的语义：触发键是启停循环（启动/暂停/恢复），
        「停止鼠标」按钮才是真正的结束 + 复位到未就绪。
        这里必须顺手清掉本区暂停标志 —— 否则「暂停中按停止」会把
        paused=True 留到下一轮，再就绪启动后跑起来却一动不动。

        【v23 S4】先置 mouse_stop_requested：引擎线程随后结束时会走
        _finished，靠这个标志认出「用户停止」——不覆盖消息、更不触发
        收尾动作（关机被用户停止还执行就是事故）。
        """
        with ST.lock:
            e = ST.mouse_engine
            ST.mouse_engine = None
            ST.mouse_running = False
            ST.mouse_info = ""
            ST.started_at = 0
            ST.mouse_armed = False      # 停止后连同就绪态一起清掉
            ST.elev_warn = False
            ST.mouse_stop_requested = True
        # 用户停止 = 收尾动作一并取消（防止「点停止前一刻排定的关机」）
        actions.cancel()
        pausectl.set_paused(False, "mouse")
        clicked = getattr(getattr(e, "ctx", None), "clicked", 0) if e else 0
        if e is not None:
            e.stop(wait=False)
        # 保险：把可能按住的键都松开（含侧键）
        released = _mouse_release_all()
        toast.show_mouse_stopped()
        _log("鼠标连点已停止（用户操作）：本次共注入 %s 次，"
             "按键释放=%s" % (clicked, "成功" if released else "失败"))
        if not released:
            # 释放没发完 = 前台程序已无法接收输入。
            # 必须如实告诉用户，否则他看到「已停止」却发现游戏里角色还在走，
            # 只会以为程序坏了。
            ST.set_msg("鼠标已停止，但前台程序无响应，"
                       "按键可能未完全松开（切走前台窗口后会自动恢复）",
                       zone="mouse")
            return {"ok": True, "released": False,
                    "msg": "已停止。但前台程序无响应，"
                           "按键可能没松开：切到别的窗口再切回来即可"}
        ST.set_msg("鼠标已停止", zone="mouse")
        return {"ok": True, "released": True, "msg": "鼠标已停止"}

    # ---- 运行监控：心跳日志 + 提权预警 ----
    def _start_elevation_watchdog(self):
        """
        连点运行期间的看门狗（同时只留一个）：

        1. **心跳日志**：每 5 秒记一条「已注入 N 次」。
           2026-10-15 主人报「连点没起效」时 launch.log 里只有启动记录，
           **零运行期信息**，根本无法判断引擎到底跑没跑、注入了多少。
        2. **提权预警**：发现前台是提权窗口（如管理员身份的原神）
           而自己没提权 → 注入会被 UIPI 静默拦掉 → toast + 状态条
           明确警告，并给一键提权重启入口。
        """
        self._monitor_stop.clear()
        if self._monitor_thread and self._monitor_thread.is_alive():
            # 让旧线程尽快退出再开新的（它每 1 秒醒一次，0.2s 等不到
            # 也无妨——最坏多打一条心跳，不会有功能影响）
            self._monitor_stop.set()
            self._monitor_thread.join(timeout=0.2)
            self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="run-monitor", daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self):
        last_beat = 0.0
        warned = False
        while not self._monitor_stop.wait(1.0):
            with ST.lock:
                m_running = ST.mouse_running
                k_running = ST.key_running
                e = ST.mouse_engine
            # 两个区都停了才退出看门狗——键盘区也注入（SendInput），
            # 同样需要提权预警，不能只盯着鼠标区。
            if not (m_running or k_running):
                break
            now = time.time()
            # 心跳只对鼠标区有意义：连点是定时注入、有计数可报；
            # 键盘锁定是事件驱动（等用户长按），没有"注入了多少次"。
            if m_running and now - last_beat >= 5.0:
                last_beat = now
                clicked = getattr(getattr(e, "ctx", None), "clicked", 0) or 0
                _log("连点滴答：已注入 %s 次（运行 %.0f 秒）"
                     % (clicked, now - (ST.started_at or now)))
            # 提权预警：只警告一次，避免刷屏
            if not warned and injection_blocked_by_uipi():
                warned = True
                with ST.lock:
                    ST.elev_warn = True
                _log("⚠ 检测到前台窗口权限更高（提权/保护进程），"
                     "本进程为普通权限——注入会被 UIPI 拦截，"
                     "游戏/程序里收不到输入。请以管理员身份运行本工具。")
                toast.show_custom(
                    "⚠ 目标窗口权限更高，输入被系统拦截\n"
                    "请以管理员身份运行本工具（界面有一键重启）",
                    "warn")
                ST.set_msg("⚠ 目标窗口权限更高，注入被拦截——"
                           "请以管理员身份运行本工具",
                           zone="mouse" if m_running else "key")
        self._monitor_stop.clear()

    def relaunch_as_admin(self):
        """
        以管理员身份重启自己（弹一次 UAC）。

        用途：目标程序（原神等）以管理员运行时，普通权限的注入会被
        UIPI 拦掉——与其跟用户解释「右键→以管理员身份运行」，
        不如给一个按钮。重启后新进程是提权的，注入不再被拦。
        """
        try:
            if getattr(sys, "frozen", False):
                # PyInstaller exe：直接提权跑自己
                target, args, cwd = sys.executable, "", os.path.dirname(
                    sys.executable)
            else:
                # 源码运行：提权跑 python + 脚本
                target = sys.executable
                script = os.path.abspath(sys.argv[0]) if sys.argv else ""
                args = '"%s"' % script if script else ""
                cwd = os.path.dirname(script) if script else HERE
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.ShellExecuteW.argtypes = [
                wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR,
                wt.LPCWSTR, ctypes.c_int]
            shell32.ShellExecuteW.restype = wt.HANDLE
            SE_ERR_ACCESSDENIED = 5
            r = shell32.ShellExecuteW(None, "runas", target, args or None,
                                      cwd, 1)   # SW_SHOWNORMAL
            # ShellExecuteW 成功返回 >32 的 HINSTANCE；失败返回错误码
            if r <= 32:
                if r == SE_ERR_ACCESSDENIED:
                    return {"ok": False,
                            "msg": "UAC 提权被取消。也可以手动："
                                   "右键 exe → 以管理员身份运行"}
                return {"ok": False, "msg": "提权重启失败（错误码 %s）" % r}
            _log("应用户要求以管理员身份重启（UAC 已确认）")
            # 新实例起来后，老实例退位（延迟一点让新窗口先建好）
            threading.Thread(target=self._exit_later, daemon=True).start()
            return {"ok": True, "msg": "正在以管理员身份重启…"}
        except Exception as exc:
            _log_exc("relaunch_as_admin")
            return {"ok": False, "msg": "提权重启出错：%s" % exc}

    @staticmethod
    def _exit_later(delay=1.5):
        time.sleep(delay)
        os._exit(0)

    # ---- 宏录制 / 回放（v27 / S2） ----
    def macro_status(self):
        """前端每 500ms 轮询一次。只回摘要，不回事件本体（几千条会卡死 webview）。"""
        rec = _MACRO.get("rec")
        steps = _MACRO.get("steps") or []
        events = _MACRO.get("events") or []
        from core import macro_steps as ms
        if steps:
            info = ms.stats(steps)
            preview = _macro_preview(steps)
        else:
            from core.recorder import summarize
            info = summarize(events)
            preview = info["preview"]
        out = {"ok": True, "recording": False, "playing": bool(_MACRO["playing"]),
               "name": _MACRO.get("name", ""),
               "count": info["count"], "secs": info["secs"],
               "preview": preview,
               "clicks": info["clicks"], "keys": info["keys"],
               # step_ver 每改一次步骤就 +1：界面只在版本变了才重画列表。
               # 否则 700ms 一次全量重绘会把用户正按住的那一行刷掉。
               "step_ver": int(_MACRO.get("step_ver", 0)),
               # v34：宏有了自己的启停键，界面必须显示**真键名**（降级后可能是
               # Ctrl+F7），写死 F7 会出现「照提示按了没反应」的老毛病。
               # 暂停态也要回：回放中按 F7 定住后，界面上得看得出来是暂停不是卡死。
               "trig": self._trigger_label("macro"),
               "paused": bool(pausectl.is_paused("macro")),
               "armed": False, "started": True, "waited": 0, "overflow": ""}
        if rec is not None and rec.running:
            st = rec.stats()
            out.update(recording=True, count=st["count"],
                       secs=st["secs"], overflow=st["overflow"] or "",
                       armed=bool(st.get("armed")),
                       started=bool(st.get("started")),
                       waited=st.get("waited", 0),
                       mode=st.get("mode", ""))
            if st["overflow"]:
                out["preview"] = {"events": "动作太多，已自动停止录制",
                                  "seconds": "录制超时，已自动停止录制"} \
                    .get(st["overflow"], "")
            elif st.get("armed"):
                out["preview"] = "等起点…（已等 %.1f 秒）" % st.get("waited", 0)
            else:
                out["preview"] = "录制中……"
        return out

    def macro_start_record(self, ignore_injected=True,
                           start_mode="first_click", start_delay=3):
        """
        开始录制。

        start_mode（v30 新增，见 core/recorder.py 顶部说明）：
          first_click  第一次鼠标按下才算起点（默认）
          now          点按钮立刻开始（脚本/老用法）
          countdown    倒数 start_delay 秒后开始

        【为什么录制前必须确认没有任务在跑】
        连点/键盘锁定都在后台注入，虽然 recorder 会按注入标记过滤掉，
        但用户此刻录到的会是「目标程序自己动」的画面，一步都不可信。
        与其录出一份废宏，不如直接拒绝并说清楚原因。
        """
        with ST.lock:
            if ST.mouse_running or ST.key_running:
                return {"ok": False,
                        "msg": "有任务正在运行，先停止再录制"
                               "（否则录到的是程序自己在动）"}
        if _MACRO["playing"]:
            return {"ok": False, "msg": "宏正在回放，先停止回放再录制"}

        rec = _macro_recorder()
        ok, err = rec.start(ignore_injected=bool(ignore_injected),
                            mode=start_mode, delay=start_delay)
        if not ok:
            _log("宏录制启动失败：%s" % err)
            return {"ok": False, "msg": "录制启动失败：%s" % err}

        _MACRO["events"] = []
        _MACRO["steps"] = []
        _MACRO["name"] = ""
        _macro_bump()
        _log("宏录制开始（起点=%s / 过滤注入事件=%s）"
             % (rec.start_mode, bool(ignore_injected)))
        if rec.start_mode == "first_click":
            tip = ("已就绪：切到目标程序，第一次点击就是起点"
                   "（按 Pause 结束录制）")
        elif rec.start_mode == "countdown":
            tip = "录制将在 %d 秒后开始（按 Pause 可取消）" % int(start_delay)
        else:
            tip = "录制中：去目标程序操作，按 Pause 键结束录制"
        ST.set_msg(tip)
        toast.show_custom("开始录制：按 Pause 结束", "start")
        # 录完要能看见界面，所以录制期间把窗口最小化让位给目标程序，
        # 结束时 _macro_finish 会自动把它叫回来。
        self._hide_if_needed(True)
        threading.Thread(target=self._macro_watch, name="macrowatch",
                         daemon=True).start()
        return {"ok": True, "msg": tip, "mode": rec.start_mode}

    def _macro_watch(self):
        """看门狗：Pause 键/超限只置位，收尾必须回到界面线程做。"""
        rec = _MACRO.get("rec")
        if rec is None:
            return
        from core.recorder import ARM_TIMEOUT
        while rec.running:
            if rec.stop_requested:
                break
            # v30：等起点也会等丢人。钩子装好了却一直不用，它就一直挂着。
            st = rec.stats()
            if not st.get("started") and st.get("waited", 0) >= ARM_TIMEOUT:
                rec.arm_timeout = True
                break
            time.sleep(0.2)
        if rec.running:
            self._macro_finish(auto=True)

    def macro_stop_record(self):
        """用户点按钮结束录制（与按 Pause 走同一条收尾路径）。"""
        if not _macro_recording():
            return {"ok": False, "msg": "当前没有在录制"}
        return self._macro_finish(auto=False)

    def _macro_finish(self, auto=False):
        """
        录制收尾：补抬起 → 卸钩子 → 存事件表 → 把窗口叫回来 → 报摘要。

        auto=True 表示是 Pause 键或超限触发的，这时窗口还是最小化的，
        必须主动 show，否则用户按完 Pause 会以为程序没了。
        """
        rec = _MACRO.get("rec")
        if rec is None or not rec.running:
            return {"ok": False, "msg": "当前没有在录制"}

        events = rec.stop()
        _MACRO["events"] = events
        # v30：录完立刻压缩成步骤表——用户接下来要编辑的是节点，
        # 不是几百条 move。压缩规则见 core/macro_steps.py。
        from core import macro_steps as ms
        _MACRO["steps"] = ms.from_events(events)
        _macro_bump()
        info = ms.stats(_MACRO["steps"])

        if auto:
            self.show_from_tray()      # show + SetForegroundWindow
        overflow = rec.overflow
        armed_timeout = bool(getattr(rec, "arm_timeout", False))
        rec.arm_timeout = False
        if armed_timeout:
            msg = ("等了 30 秒没等到第一次点击，已自动取消录制"
                   "（先点「开始录制」再切到目标程序点一下）")
        elif overflow == "events":
            msg = "动作超过 %d 条，已自动停止录制" % len(events)
        elif overflow == "seconds":
            msg = "录制超过时限，已自动停止"
        elif not events:
            msg = ("录制结束，一步都没录到：起点没触发？"
                   "（起点模式为「首次点击」时需要先在目标程序里点一下）")
        else:
            msg = "录制完成：%d 个步骤，约 %.1f 秒" % (info["count"],
                                                     info["secs"])

        ST.set_msg(msg)
        _log("宏录制结束：%d 条原始事件 → %d 个步骤 / %.1f 秒 / "
             "左键 %d 次 / 键盘 %d 次%s"
             % (len(events), info["count"], info["secs"], info["clicks"],
                info["keys"], "（%s）" % msg if overflow or armed_timeout else ""))
        if info["count"]:
            toast.show_custom("录制完成：%d 个步骤，%.1f 秒"
                              % (info["count"], info["secs"]), "stop")
        else:
            toast.show_custom("录制结束，但一步都没录到", "stop")
        return {"ok": True, "msg": msg, "count": info["count"],
                "secs": info["secs"], "preview": _macro_preview(_MACRO["steps"]),
                "clicks": info["clicks"], "keys": info["keys"],
                "step_ver": _MACRO["step_ver"]}

    def macro_discard(self):
        """丢掉当前录制（不保存）。"""
        rec = _MACRO.get("rec")
        if rec is not None and rec.running:
            rec.cancel()
            self.show_from_tray()
        _MACRO["events"] = []
        _MACRO["steps"] = []
        _MACRO["name"] = ""
        _macro_bump()
        ST.set_msg("已清空录制的动作")
        return {"ok": True, "msg": "已清空", "step_ver": _MACRO["step_ver"]}

    # ---- 步骤编辑（v30） --------------------------------------------
    def _macro_edit_guard(self):
        """录制/回放期间不许改步骤：改的时候回放器正在读同一份数据。"""
        if _macro_recording():
            return "正在录制，先结束录制再编辑"
        if _MACRO["playing"]:
            return "正在回放，先停止回放再编辑"
        if not (_MACRO.get("steps") or []):
            return "还没有可编辑的步骤：先录制，或载入一个宏"
        return ""

    def macro_steps(self, offset=0, limit=300):
        """
        分页取步骤（界面只画前 300 行，再多就该分页了）。

        返回的是「已经算好 label」的展示数据 + 原始字段，
        前端点行就能直接回填弹窗，不用再查一遍。
        """
        from core import macro_steps as ms
        steps = _MACRO.get("steps") or []
        try:
            off = max(0, int(offset or 0))
            lim = min(1000, max(1, int(limit or 300)))
        except (TypeError, ValueError):
            off, lim = 0, 300
        page = steps[off:off + lim]
        items = []
        for i, s in enumerate(page):
            idx = off + i
            items.append(dict(s, i=idx, label=ms.label(s)))
        return {"ok": True, "total": len(steps), "offset": off,
                "items": items, "step_ver": int(_MACRO.get("step_ver", 0))}

    def macro_step_set(self, index, patch=None):
        """改一个步骤（弹窗「确定」走这里）。只认白名单字段。"""
        err = self._macro_edit_guard()
        if err:
            return {"ok": False, "msg": err}
        steps = _MACRO["steps"]
        try:
            i = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "节点编号不是整数"}
        if not (0 <= i < len(steps)):
            return {"ok": False, "msg": "节点编号超出范围（共 %d 步）"
                                        % len(steps)}
        p = dict(patch or {})
        cur = dict(steps[i])
        for k in ("name", "x", "y", "delay", "delay2", "after"):
            if k in p:
                cur[k] = p[k]
        from core import macro_steps as ms
        clean, e = ms.validate_steps([cur])
        if e:
            return {"ok": False, "msg": e}
        steps[i] = clean[0]
        _macro_bump()
        _log("宏步骤 %d 已修改：%s" % (i, ms.label(clean[0])))
        return {"ok": True, "msg": "已保存第 %d 步" % i,
                "step": dict(clean[0], i=i, label=ms.label(clean[0])),
                "step_ver": _MACRO["step_ver"]}

    def macro_step_del(self, index):
        err = self._macro_edit_guard()
        if err:
            return {"ok": False, "msg": err}
        steps = _MACRO["steps"]
        try:
            i = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "节点编号不是整数"}
        if not (0 <= i < len(steps)):
            return {"ok": False, "msg": "节点编号超出范围"}
        steps.pop(i)
        _macro_bump()
        return {"ok": True, "msg": "已删除第 %d 步" % i,
                "step_ver": _MACRO["step_ver"]}

    def macro_step_dup(self, index):
        """复制一步，插在它后面（参考截图里「长按节点复制」的行为）。"""
        err = self._macro_edit_guard()
        if err:
            return {"ok": False, "msg": err}
        steps = _MACRO["steps"]
        try:
            i = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "节点编号不是整数"}
        if not (0 <= i < len(steps)):
            return {"ok": False, "msg": "节点编号超出范围"}
        steps.insert(i + 1, dict(steps[i]))
        _macro_bump()
        return {"ok": True, "msg": "已复制第 %d 步" % i,
                "step_ver": _MACRO["step_ver"]}

    def macro_step_move(self, index, delta=1):
        """上移/下移一步。调换是「顺序」的唯一操作，别做拖拽（拖拽易误触）。"""
        err = self._macro_edit_guard()
        if err:
            return {"ok": False, "msg": err}
        steps = _MACRO["steps"]
        try:
            i = int(index)
            d = int(delta or 0)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "参数不是整数"}
        j = i + (1 if d > 0 else -1)
        if not (0 <= i < len(steps)) or not (0 <= j < len(steps)):
            return {"ok": False, "msg": "到边界了，移不动"}
        steps[i], steps[j] = steps[j], steps[i]
        _macro_bump()
        return {"ok": True, "msg": "第 %d 步已移到 %d" % (i, j),
                "step_ver": _MACRO["step_ver"]}

    def macro_step_insert(self, index=-1, kind="wait", delay=500):
        """插入一步（默认插在末尾的「等待」节点）。"""
        err = self._macro_edit_guard()
        if err:
            return {"ok": False, "msg": err}
        from core import macro_steps as ms
        steps = _MACRO["steps"]
        try:
            i = int(index)
        except (TypeError, ValueError):
            i = -1
        if i < 0 or i > len(steps):
            i = len(steps)
        steps.insert(i, ms.new_step(kind, delay=delay))
        _macro_bump()
        return {"ok": True, "msg": "已在第 %d 步插入" % i,
                "index": i, "step_ver": _MACRO["step_ver"]}

    def macro_play(self, speed=1.0, repeat=1):
        """回放当前录制的动作（或刚载入的宏）。"""
        if _macro_recording():
            return {"ok": False, "msg": "正在录制，先结束录制再回放"}
        # 【v28】暂停中不许起回放：ctx.wait 开头的暂停门闩会让它一步都不走，
        # 用户看到的是「点了开始回放，窗口缩下去了，然后什么都没发生」。
        # 宁可当场说清楚，也不要让人以为是回放坏了。
        # 【v34】看的是 macro 区而不是 mouse 区：宏有自己的分区后，
        # 「鼠标区单独暂停」不该拦住回放（以前会误拦）。全局 Pause 仍然
        # 写得进 macro 区（判定是全局 OR 本区），所以这条拦不住全局暂停。
        if pausectl.is_paused("macro"):
            return {"ok": False,
                    "msg": "现在是暂停状态：先按 %s（或点右上角"
                           "「恢复运行」）再回放，否则回放会卡在第一步"
                           % self._trigger_label("macro")}
        if _MACRO["playing"]:
            return {"ok": False, "msg": "宏已经在回放中"}
        # 【v30】回放吃「步骤表展开后的事件」，不是录制时的原始事件——
        # 用户改过坐标/延时/删过节点，回放必须反映修改。
        steps = _MACRO.get("steps") or []
        if steps:
            from core import macro_steps as ms
            events = ms.to_events(steps)
        else:
            events = _MACRO.get("events") or []
        if not events:
            return {"ok": False, "msg": "还没有录好的动作：先录制，或从下拉里选一个宏"}

        try:
            speed = float(speed or 1.0)
            repeat = int(repeat or 1)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "倍速/次数不是数字"}
        if speed <= 0:
            return {"ok": False, "msg": "倍速必须大于 0"}
        # 记住本轮参数：宏的启停键（F7 / 绑的鼠标键）启动时没有界面传参，
        # 只能用上一次的设置，否则按热键回放一律变成 1 倍速 1 次。
        _MACRO["speed"] = speed
        _MACRO["repeat"] = max(1, repeat)
        # 起跑前先解开本区暂停：上一轮「暂停后直接点停止」会把标志留下，
        # 不清的话新一轮一启动就被自己的门闩冻住（规则 40：暂停≠结束）。
        pausectl.set_paused(False, "macro")

        from plugins.macro_replay import MacroReplayPlugin
        plugin = MacroReplayPlugin()
        opts = {"speed": speed, "repeat": max(1, repeat), "events": events}
        # zone="macro"：宏归自己的暂停区管。以前它跟着 "mouse" 走，
        # 宏的启停键（F7）会一并把鼠标连点定住 —— v34 拆开。
        e = eng.ClickerEngine(plugin, options=opts, base_cps=1,
                              duration=0, count_limit=0, zone="macro")

        def _finished(ctx):
            if _MACRO.get("engine") is e:
                _MACRO["engine"] = None
            _MACRO["playing"] = False
            err = getattr(ctx, "error", None)
            if err:
                ST.set_msg("宏回放出错：%s" % err)
                _log("宏回放出错：%s" % err)
                return
            if getattr(ctx, "_user_stopped", False):
                return                 # 用户主动停止，stop 那边已经说过话
            ST.set_msg("宏回放完成：%d 次" % max(1, repeat))
            _log("宏回放完成：%d 次，注入鼠标点击 %s 次"
                 % (max(1, repeat), getattr(ctx, "clicked", 0)))
            toast.show_custom("宏回放完成", "stop")

        e.on_finish = _finished
        _MACRO["engine"] = e
        _MACRO["playing"] = True
        if not e.start():
            _MACRO["playing"] = False
            _MACRO["engine"] = None
            return {"ok": False, "msg": "回放线程启动失败"}

        _log("宏回放开始：%d 步 / 倍速 %s / 重复 %d 次"
             % (len(events), speed, max(1, repeat)))
        ST.set_msg("宏回放中……按 Pause 暂停，点「停止回放」结束")
        toast.show_custom("宏回放开始（%.1fx）" % speed, "start")
        self._hide_if_needed(True)
        return {"ok": True, "msg": "回放中：%d 步 × %d 次（%.1fx）"
                                   % (len(events), max(1, repeat), speed)}

    def macro_stop_play(self):
        e = _MACRO.get("engine")
        if e is None:
            _MACRO["playing"] = False
            return {"ok": False, "msg": "当前没有在回放"}
        e.ctx._user_stopped = True      # 让 on_finish 别再写「已完成」
        e.stop(wait=True)
        _MACRO["engine"] = None
        _MACRO["playing"] = False
        # 停止 = 打回未启动，本区暂停标志必须一起清（规则 40：暂停≠结束）。
        # 不清的话：回放中按 F7 暂停 → 点停止 → 下次按 F7 会先「恢复」一次，
        # 表现成「按了没反应」。
        pausectl.set_paused(False, "macro")
        ST.set_msg("宏回放已停止")
        _log("宏回放已由用户停止")
        toast.show_custom("宏回放已停止", "stop")
        return {"ok": True, "msg": "已停止"}

    # -- 宏启停键（v34）------------------------------------------------
    def toggle_macro(self):
        """
        触发键回调：宏区「启动 <-> 暂停 <-> 恢复」，和鼠标区/键盘区同构。

        为什么宏也要有（主人 2026-10-18 反馈）：
          回放一开始窗口就最小化让位给目标程序了（_hide_if_needed），
          想暂停/继续只能切回窗口点按钮，等于回放期间没法中途干预。
          F5 管连点、F6 管键盘锁定，那宏就得有自己的一条通道。

        状态分派：
          录制中    → 停止录制（录制时也常常切不回窗口，顺手让它能收尾）
          回放中    → 暂停
          暂停中    → 恢复
          空闲有步骤 → 启动回放
          空闲没步骤 → 明确提示「先录制」，不给静默无反应

        暂停用的是 **macro 分区**（不是全局）：只定宏，不牵连鼠标区/键盘区。
        全局 Pause 键仍然能冻住宏（判定是「全局 OR 本区」）。
        """
        trig = self._hotkey_label("macro")
        if _macro_recording():
            self.macro_stop_record()
            _log("触发键 %s：录制中 -> 停止录制" % trig)
            return {"ok": True, "msg": "已停止录制"}

        if _MACRO.get("playing"):
            now = pausectl.set_paused(not pausectl.is_paused("macro"),
                                      "macro")
            if now:
                ST.set_msg("宏回放已暂停 · 再按 %s 继续" % trig)
                toast.show_custom("宏回放已暂停\n再按 %s 继续" % trig, "pause")
                _log("触发键 %s：宏回放 -> 暂停" % trig)
                return {"ok": True, "msg": "已暂停 · 再按 %s 继续" % trig}
            pausectl.clear_global()      # 全局位还开着的话只恢复宏是不够的
            ST.set_msg("宏回放已继续")
            toast.show_custom("宏回放已继续", "start")
            _log("触发键 %s：宏回放 -> 恢复" % trig)
            return {"ok": True, "msg": "已继续"}

        steps = _MACRO.get("steps") or []
        events = _MACRO.get("events") or []
        if not steps and not events:
            msg = "宏区还没有动作：先录制（或载入一个宏）再按 %s" % trig
            ST.set_msg(msg)
            toast.show_custom("宏区 没有动作\n先录制再按 %s" % trig, "warn")
            _log("触发键 %s 被忽略：宏区既没步骤也没事件" % trig)
            return {"ok": False, "msg": msg}

        _log("触发键 %s：启动宏回放" % trig)
        sp = _MACRO.get("speed", 1.0)
        rp = _MACRO.get("repeat", 1)
        return self.macro_play(speed=sp, repeat=rp)

    def macro_list(self):
        """列出已保存的宏。"""
        return {"ok": True, "names": cfg.list_macros(base=_settings_dir())}

    def macro_save(self, name, speed=1.0, repeat=1):
        steps = _MACRO.get("steps") or []
        events = _MACRO.get("events") or []
        if not steps and not events:
            return {"ok": False, "msg": "还没有录好的动作可保存"}
        ok, res = cfg.save_macro(name, events, speed=speed, repeat=repeat,
                                 base=_settings_dir(), steps=steps)
        if not ok:
            return {"ok": False, "msg": res}
        _MACRO["name"] = cfg.safe_name(name)
        _log("宏已保存：%s（%d 个步骤）→ %s"
             % (_MACRO["name"], len(steps), res))
        return {"ok": True, "msg": "已保存「%s」" % _MACRO["name"],
                "names": cfg.list_macros(base=_settings_dir())}

    def macro_load(self, name):
        ok, data, msg = cfg.load_macro(name, base=_settings_dir())
        if not ok:
            _log("宏加载失败：%s" % msg)
            return {"ok": False, "msg": msg}
        from core import macro_steps as ms
        _MACRO["events"] = data.get("events") or []
        # 老文件只有 events（v27~v29 存的），载入时补一次压缩，
        # 这样「升级前录的宏」也能进新编辑器。
        steps = data.get("steps") or []
        _MACRO["steps"] = steps if steps else ms.from_events(_MACRO["events"])
        _MACRO["name"] = cfg.safe_name(name)
        _macro_bump()
        info = ms.stats(_MACRO["steps"])
        _log("宏已载入：%s（%d 个步骤 / %.1f 秒）"
             % (_MACRO["name"], info["count"], info["secs"]))
        return {"ok": True, "msg": "已载入「%s」：%d 个步骤，约 %.1f 秒"
                                   % (_MACRO["name"], info["count"],
                                      info["secs"]),
                "count": info["count"], "secs": info["secs"],
                "preview": _macro_preview(_MACRO["steps"]),
                "step_ver": _MACRO["step_ver"],
                "speed": data.get("speed", 1.0),
                "repeat": data.get("repeat", 1)}

    def macro_delete(self, name):
        ok, msg = cfg.delete_macro(name, base=_settings_dir())
        if ok and _MACRO.get("name") == cfg.safe_name(name):
            _MACRO["name"] = ""
        _log("删除宏：%s → %s" % (name, msg))
        return {"ok": ok, "msg": msg,
                "names": cfg.list_macros(base=_settings_dir())}

    # ---- 键盘 ----
    def arm_keyboard(self, params):
        """
        按钮路径：**只装填参数进入就绪态，不启动监听**。

        与鼠标区 arm_mouse 完全对称（主人 2026-10-15 的两段式预期）：
        点「启动」按钮 = 装填参数进入就绪态；按热键（默认 F6）才真启动，
        再按一次停止。旧行为点按钮立即起监听，第一次按 F6 被当成
        「停止正在跑的锁定」——与鼠标区同一个 bug，只是场景不同。
        """
        try:
            keys = (params.get("keys") or "w").strip()
            thr = float(params.get("threshold", 2) or 2)
            hz = int(params.get("hz", 30) or 30)
            # 【坑】前端 select 的 value 是字符串 "true"/"false"，
            # 必须显式转 bool 三步走：str() -> strip() -> lower()。
            # 直接用 params.get("hold") 拿到的是字符串，
            # 而**非空字符串在Python 里恒为 True**，
            # 于是「反复点击」会被当成「一直按着」。
            #
            # 空字符串要显式当缺省处理：否则 "" 会被解析成 False（=反复点击），
            # 用户明明选的是「一直按着」却跑成了另一个模式，且界面无任何提示。
            _hold_raw = params.get("hold", "true")
            _hold_txt = str(_hold_raw).strip().lower()
            if not _hold_txt:
                _hold_txt = "true"
            hold = (_hold_txt == "true")
            gap = int(float(params.get("gap", 20) or 20))
        except Exception as e:
            return {"ok": False, "msg": "参数解析失败：%s" % e}

        # 记住参数：热键启动时要用它（与 start_keyboard 存同一份）
        self.last_key = {"keys": keys, "threshold": thr, "hz": hz,
                         "hold": "true" if hold else "false", "gap": gap}

        with ST.lock:
            if ST.key_running:
                # 同上：触发键只切暂停，结束归「停止」按钮
                return {"ok": False, "msg": "键盘锁定正在运行，"
                                           "先点「停止」才能重新装填"}
            ST.key_armed = True
        trig = self._trigger_label("key")
        # 与鼠标区同样处理：就绪 = 新一轮，清掉上一轮残留的本区暂停
        pausectl.set_paused(False, "key")
        ST.set_msg("已就绪：按 %s 启动锁定（长按 %s 超过 %.1f 秒即锁定）"
                   % (trig, keys, thr), zone="key")
        toast.show_key_ready(trig)
        _log("键盘区已就绪（未启动）：keys=%s thr=%s hz=%s hold=%s gap=%s"
             % (keys, thr, hz, "true" if hold else "false", gap))
        return {"ok": True,
                "msg": "已就绪。按 %s 启动锁定，再按 %s 暂停；"
                       "点「停止」才真正结束" % (trig, trig)}

    def start_keyboard(self, params):
        # 【v27】同 start_mouse：录制期间不许起任务
        if _macro_recording():
            return {"ok": False,
                    "msg": "宏正在录制，先按 Pause 结束录制再启动"}
        try:
            keys = (params.get("keys") or "w").strip()
            thr = float(params.get("threshold", 2) or 2)
            hz = int(params.get("hz", 30) or 30)
            # 【坑】前端 select 的 value 是字符串 "true"/"false"，
            # 必须显式转 bool 三步走：str() -> strip() -> lower()。
            # 直接用 params.get("hold") 拿到的是字符串，
            # 而**非空字符串在Python 里恒为 True**，
            # 于是「反复点击」会被当成「一直按着」。
            #
            # 空字符串要显式当缺省处理：否则 "" 会被解析成 False（=反复点击），
            # 用户明明选的是「一直按着」却跑成了另一个模式，且界面无任何提示。
            _hold_raw = params.get("hold", "true")
            _hold_txt = str(_hold_raw).strip().lower()
            if not _hold_txt:
                _hold_txt = "true"
            hold = (_hold_txt == "true")
            gap = int(float(params.get("gap", 20) or 20))
        except Exception as e:
            return {"ok": False, "msg": "参数解析失败：%s" % e}

        self.last_key = {"keys": keys, "threshold": thr, "hz": hz,
                         "hold": "true" if hold else "false", "gap": gap}

        with ST.lock:
            if ST.key_running:
                return {"ok": False, "msg": "键盘锁定已在运行"}

        def on_change(active, name):
            if active:
                ST.set_msg("[%s] 已锁定：%s（%s）"
                           % (time.strftime("%H:%M:%S"), name,
                              "一直按着" if self.last_key["hold"] == "true"
                              else "反复点击"), zone="key")
                toast.show_custom("键盘锁定：%s 已锁定（%s）"
                                  % (name, "一直按着"
                                     if self.last_key["hold"] == "true"
                                     else "反复点击"), "start")
            else:
                ST.set_msg("[%s] 已解除：%s"
                           % (time.strftime("%H:%M:%S"), name), zone="key")
                toast.show_custom("键盘锁定：%s 已解除" % name, "stop")

        lock = kb.KeyHoldLock(keys=keys, threshold_sec=thr, repeat_hz=hz,
                             on_state_change=on_change, panic_key="f12",
                             hold_down=hold, pair_gap_ms=gap)
        ok, err = lock.start()
        if not ok:
            # 【坑·2026-10-15】热键路径下失败必须留痕：旧代码只 return，
            # 用户按触发键后什么都没发生——无日志、无弹窗、无状态变化，
            # 等于「键盘区完全无效果」且无从排查。现在三层都报。
            _log("键盘锁定启动失败：%s（本进程提权=%s）"
                 % (err, our_elevated()))
            toast.show_custom("键盘锁定启动失败：%s" % err, "warn")
            ST.set_msg("键盘锁定启动失败：%s" % err, zone="key")
            return {"ok": False, "msg": str(err)}
        mode = "一直按着" if hold else "反复点击"
        with ST.lock:
            ST.key_lock = lock
            ST.key_running = True
            ST.key_info = "%s %s" % (keys, mode)
            ST.key_armed = False     # 真启动后就不再是就绪态
        ST.set_msg("键盘锁定已启动（%s）· 现在长按 %s 超过 %.1f 秒即锁定"
                   % (mode, keys, thr), zone="key")
        toast.show_key_started("%s %s" % (keys, mode))
        _log("键盘锁定已启动：keys=%s thr=%s hz=%s hold=%s gap=%s "
             "本进程提权=%s"
             % (keys, thr, hz, "true" if hold else "false", gap,
                our_elevated()))
        # 与鼠标区共用看门狗：心跳/提权预警同样保护键盘注入
        # （SendInput 注入键盘事件一样受 UIPI 管辖）
        self._start_elevation_watchdog()
        return {"ok": True, "msg": "已启动（%s）。长按 %s 超过 %.1f 秒即锁定，"
                                  "再按一次解除，F12 紧急释放"
                                  % (mode, keys, thr)}

    def toggle_keyboard(self):
        """
        触发键回调：键盘区「就绪 -> 启动 <-> 暂停 <-> 恢复」。

        与 toggle_mouse 完全对称（2026-10-16 主人拍板两区一起改）：
        - 就绪态（用户点过「① 就绪」）按触发键 = 用刚设的参数真启动；
        - 没就绪按触发键 = **不启动**，提示先去界面就绪；
        - 运行中按触发键 = **暂停**（不是停止）；
        - 暂停中按触发键 = 恢复。

        【2026-10-16 语义修正 · 主人实测报回】
        键盘区的「暂停」与鼠标区不完全一样：鼠标连点是「动作定住、参数保留」，
        而键盘锁定的本体就是「一个键被按住」。所以暂停键盘区 = **松手并解除锁定**
        （core/keyboard.py::_inject_loop 在进入暂停的那一刻补 keyup + 清状态）。
        恢复 = 该区重新待命，用户再长按目标键就能重新锁定。
        旧行为只松手不清状态，导致「暂停后按目标键」被判成解除指令，
        弹出莫名其妙的「已解除锁定」。

        只有「停止」按钮才把它打回未就绪 —— 否则「按一下就结束、
        再按没反应」的死循环在键盘区照样存在。
        """
        trig = self._trigger_label("key")
        with ST.lock:
            running = ST.key_running
            armed = ST.key_armed
        if running:
            now = self.set_zone_paused("key", None)
            # 目标键名（"w" / "w,a"…），恢复提示要点名，否则用户不知道该长按哪个键
            keys = (self.last_key or {}).get("keys") or ""
            if now:
                # 【2026-10-16 语义修正】暂停 = 松开 + 解除锁定，
                # 两件事在 core/keyboard.py::_inject_loop 里同一时刻完成。
                # 文案必须说清「已解除」，否则用户会以为恢复后锁定还在，
                # 再按目标键时对「已解除锁定」的提示莫名其妙。
                ST.set_msg("键盘锁定已暂停（已解除锁定）· 再按 %s 恢复运行" % trig,
                           zone="key")
                toast.show_key_paused(trig)
                _log("触发键 %s：键盘锁定 -> 暂停（已顺带解除锁定）" % trig)
                return {"ok": True,
                        "msg": "已暂停（锁定已解除）· 再按 %s 恢复运行" % trig}
            ST.set_msg("键盘锁定已恢复运行 · 长按 %s 可重新锁定"
                       % (keys or "目标键"), zone="key")
            toast.show_key_resumed(trig, keys)
            _log("触发键 %s：键盘锁定 -> 恢复运行（需重新长按目标键）" % trig)
            return {"ok": True, "msg": "已恢复运行 · 长按目标键可重新锁定"}
        if not armed:
            msg = ("键盘区还未就绪：请先在界面点「① 就绪」装好参数，"
                   "再按 %s 启动" % trig)
            ST.set_msg(msg, zone="key")
            toast.show_custom("键盘区 未就绪\n先点界面「① 就绪」再按 %s" % trig,
                              "warn")
            _log("触发键被忽略：键盘区未就绪（未点「① 就绪」），未启动")
            return {"ok": False, "msg": msg}
        _log("触发键 %s：就绪态 -> 启动键盘锁定" % trig)
        return self.start_keyboard(self.last_key)

    def stop_keyboard(self):
        """
        **唯一**回到「未就绪」的出口（与 stop_mouse 对称）。
        同样必须清掉本区暂停标志，避免残留到下一轮。
        """
        with ST.lock:
            lock = ST.key_lock
            ST.key_lock = None
            ST.key_running = False
            ST.key_info = ""
            ST.key_armed = False      # 停止后连同就绪态一起清掉
        pausectl.set_paused(False, "key")
        if lock is not None:
            lock.stop()
        ST.set_msg("键盘已停止", zone="key")
        toast.show_key_stopped()
        _log("键盘锁定已停止（用户操作）")
        return {"ok": True, "msg": "键盘已停止"}

    # ---- 通用 ----
    def stop_all(self):
        self.stop_mouse()
        self.stop_keyboard()
        ST.set_msg("已全部停止")
        return {"ok": True}

    def set_paused(self, flag):
        """
        全局暂停 / 恢复（两个区共用一个开关，见 core/pausectl）。

        为什么单独做「暂停」而不只是「停止」：
          主人原话——「没有设置全局暂停，虽然可以用 ctrl+c 停止，
          但比如玩原神之后启动的话就 ctrl+c 就无效了」。
          游戏中切不回控制台，Ctrl+C 是按不到的；
          而「停止」会把参数清掉，恢复后要重设一遍。
          暂停 = 动作定住、参数保留、一个事件都不注入，
          再按一次暂停键原地恢复。

        热键默认用 Pause（Break）键：游戏基本不占它，
        被占了自动降级 ScrollLock（见 _try_pause_hotkey）。
        """
        want = bool(flag)
        now = pausectl.set_paused(want)
        if now:
            ST.set_msg("已全局暂停（再按暂停键恢复）")
            toast.show_paused()
            # 【2026-10-17】全局暂停/恢复必须留日志：这条路径
            # （右上角按钮 + Pause 热键）以前一行日志都不写，
            # 主人报「按钮点了没反应」时，launch.log 里什么都看不到，
            # 只能靠猜。现在每次切换都有痕迹，可按时间线还原操作序列。
            _log("全局暂停（右上角按钮或暂停热键）")
        else:
            ST.set_msg("已恢复运行")
            toast.show_resumed()
            _log("全局恢复运行（右上角按钮或暂停热键）")
        return {"ok": True, "paused": now,
                "msg": "已暂停" if now else "已恢复运行"}

    def toggle_pause(self):
        """
        暂停 <-> 恢复。**前端右上角按钮和全局暂停热键共用这一个入口**。

        【2026-10-16 主人反馈「右上角恢复运行按钮点了没反应」的真因】
        这里原来写的是 `pausctl.is_paused()` —— 模块名少了一个 e。
        Python 是「用时才查名字」，语法检查、导入、打桩测试全都发现不了，
        一按就 NameError。而且：
          · 前端 `catch(e){}` 把它吞掉 → 界面毫无反应、也不弹通知；
          · 热键回调在线程里裸调（core/hotkey.py 的 self.callback()），
            异常穿出 while（连 finally）→ UnregisterHotKey → 线程死掉，
            所以**全局暂停热键从来没能用过，按一次还会永久失效**。

        【为什么判定用 any_paused() 而不是 is_paused()】
        按钮文案跟的是 status().paused = pausectl.any_paused()：
        只要任一分区在暂停，按钮就显示「恢复运行」。
        用 is_paused()（只看全局位）时，中键暂停了鼠标区 → 按钮显示
        「恢复运行」→ 点它算出 not False = True → **反而把两个区全冻住**，
        与用户的意图正好相反。用 any_paused() 才和按钮文案对称。
        """
        # 【v28 修复 · 录制期间不许改暂停位】按 Pause 结束录制时，
        # 低级钩子只「旁听」不吞键（见 core/recorder.py 的 _on_key），
        # 所以这颗键照样会走到 RegisterHotKey → 这里。
        # 旧行为：录完 + 全局暂停 = 程序被冻住，之后点「开始回放」
        # 第一步就卡在 ctx.wait 的暂停门闩里 —— 用户看到的是「回放没反应」，
        # 而且这个暂停位是「顺手按 Pause 结束录制」时被动留下的，不是本意。
        # 录制中的 Pause 意图只有一个：结束录制。所以直接忽略暂停切换。
        if _macro_recording():
            _log("录制中按 Pause：只结束录制，不切换全局暂停（v28）")
            return {"ok": True, "paused": pausectl.any_paused(),
                    "msg": "录制中：Pause 只结束录制，暂停状态不变"}
        return self.set_paused(not pausectl.any_paused())

    def status(self):
        return ST.snapshot()

    # ---- 系统托盘（v24 S1）----
    def hide_to_tray(self):
        """
        把窗口藏到托盘（后台继续跑）。**只隐藏，不停止任何任务**。

        由两处调用：① 关窗时（设置里勾了「关闭到托盘」）；
        ② 将来可能的「最小化到托盘」按钮。
        必须走线程：调用点在 pywebview 的 UI 线程消息里，
        而 window.hide() 内部是 Invoke 到同一个 UI 线程——同步等自己
        会卡死窗口。
        """
        w = self._window
        if w is None:
            return {"ok": False, "msg": "窗口尚未就绪"}
        try:
            w.hide()
        except Exception as e:
            return {"ok": False, "msg": "藏到托盘失败：%s" % e}
        # 第一次藏起来必须说清楚「程序还在跑」——否则用户以为关掉了，
        # 实际目标窗口还在被点，这种误会比功能本身危险。
        try:
            toast.show("已最小化到托盘：连点仍在后台运行，"
                       "双击托盘图标可恢复", kind="info", seconds=7)
        except Exception:
            pass
        _log("窗口已隐藏到托盘（任务继续运行）")
        return {"ok": True, "msg": "已最小化到托盘"}

    def show_from_tray(self):
        """从托盘把窗口叫回来，并抢到前台（否则藏在别的窗口后面）。"""
        w = self._window
        if w is None:
            return {"ok": False, "msg": "窗口尚未就绪"}
        try:
            w.show()
        except Exception as e:
            _log("从托盘恢复窗口失败：%s" % e)
        try:
            h = _win_hwnd()
            if h:
                user32.SetForegroundWindow(h)
        except Exception:
            pass
        _log("从托盘恢复窗口")
        return {"ok": True}

    # ---- 自绘标题栏 ----
    # 前端只能调 Api 上的方法，所以模块级函数要在这里包一层
    def title_drag_begin(self, x=0, y=0, scale=1.0):
        return win_drag_begin(x, y, scale)

    def title_drag_move(self, x, y):
        return win_drag_move(x, y)

    def title_drag_end(self):
        return win_drag_end()

    def title_minimize(self):
        """
        最小化。

        同样自己发 SW_MINIMIZE（不经由 WM_SYSCOMMAND），
        理由和 title_maximize 一样：frameless 窗口缺少样式位，
        走 DefWindowProc 那条路会被丢弃。
        """
        h = _win_hwnd()
        if not h:
            return {"ok": False, "minimized": False}
        try:
            user32.ShowWindow(h, 6)      # SW_MINIMIZE
            return {"ok": True, "minimized": True}
        except Exception as e:
            return {"ok": False, "minimized": False, "msg": str(e)}

    def title_maximize(self):
        """
        在「最大化」和「还原」之间切换。

        【为什么不用 SC_MAXIMIZE / SW_MAXIMIZE】
        frameless 走的是 WinForms `FormBorderStyle = None`，
        它建出来的窗口样式里**没有 WS_THICKFRAME**（实测样式位0x16010000，
        只有 WS_VISIBLE|WS_CLIPSIBLING|WS_CLIPCHILDREN|WS_MAXIMIZEBOX）。
        没有 WS_THICKFRAME 的窗口，DefWindowProc 会把
        SC_MAXIMIZE / SW_MAXIMIZE 当成无效命令丢掉——
        这就是「点最大化按钮毫无反应」的根本原因，
        旧代码发 WM_SYSCOMMAND 走的正是这条被丢弃的路径。

        所以这里**自己算矩形**：取显示器工作区（排除任务栏），
        用 SetWindowPos 铺满，���存原矩形供还原。
        这条路完全不依赖任何窗口样式位，也不依赖 pywebview 版本。
        """
        h = _win_hwnd()
        if not h:
            return {"ok": False, "maximized": False}
        return win_toggle_maximize(h)

    def is_maximized(self):
        return {"ok": True, "maximized": _win_is_maximized()}

    def title_resize(self, edge, x, y, scale=1.0):
        """
        边框拖拽缩放 —— 按下时调一次，之后每次移动调 title_resize_move。

        【为什么不交还 Windows】见 win_resize_begin 上方那段：
        本窗口的 WM_NCHITTEST 四边全是 HTCLIENT，且 WebView2 持有鼠标捕获，
        系统的模态缩放循环根本进不去（实测 0 位移）。所以自己算矩形。
        """
        return win_resize_begin(edge, x, y, scale)

    def title_resize_move(self, x, y):
        return win_resize_move(x, y)

    def title_resize_end(self):
        return win_resize_end()

    def title_close(self):
        # 关闭窗口等价于「停掉所有注入 + 释放按键」，走 shutdown 才安全。
        #
        # 【看门狗为什么放在这里】
        # shutdown() 内部要通过 SendInput 松开所有按下的键，
        # 而 SendInput 在前台程序未响应时会**永不返回**，
        # 线程卡在内核态 → 进程僵住 → 任务管理器也杀不掉
        # （上次就是这么被迫重启的）。
        # 所以先挂一个 8 秒的定时炸弹，清理能做完最好，
        # 做不完到点也一定退出，绝不留僵尸进程占着 exe。
        _arm_exit_watchdog(8.0)
        try:
            self.shutdown()
        except Exception:
            pass
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
        os._exit(0)

    # ---- 全局热键 ----
    def register_hotkeys(self):
        """
        注册全局热键。

        注意坑：**F5 在浏览器里是刷新键，在很多软件里也有默认含义**，
        所以这里必须用 RegisterHotKey 而不是网页里的 keydown 监听
        （网页监听只在仪表盘有焦点时生效，正好是「抢前台」解决不了的那个场景）。
        注册失败（被别的软件占用）时按降级列表依次尝试。
        """
        # 用户保存的热键必须**第一优先**注册；只有它无效/被占用时，
        # 才尝试默认候选。旧实现先注册默认键、再停掉、再注册自定义键：
        # 一旦 HotkeyListener.start() 返回 (False, err) 而不是抛异常，
        # 代码既不恢复默认，也不留下自定义键，当前会话直接没有热键。
        saved_sys = self._btn_binding.get("sys") \
            if isinstance(self._btn_binding.get("sys"), dict) else {}

        def _saved_first(action, defaults):
            want = str(saved_sys.get(action) or "").strip()
            if not want:
                return list(defaults)
            return [want] + [x for x in defaults
                             if str(x).lower() != want.lower()]

        ok_mouse = self._try_hotkeys(
            _saved_first("mouse", HOTKEY_MOUSE_FALLBACK),
            self.toggle_mouse, "mouse")
        ok_key = self._try_hotkeys(
            _saved_first("key", HOTKEY_KEY_FALLBACK),
            self.toggle_keyboard, "key")
        ok_macro = self._try_hotkeys(
            _saved_first("macro", HOTKEY_MACRO_FALLBACK),
            self.toggle_macro, "macro")
        # 全局暂停不是用户可改项，继续走固定候选链。
        ok_pause = self._try_hotkeys(
            HOTKEY_PAUSE_FALLBACK, self.toggle_pause, "pause")
        self._pause_key = (self._hotkeys.get("pause") or (None,))[0]

        if not self._hotkeys.get("mouse"):
            self._hotkey_notes["mouse"] = ("鼠标热键注册失败（被其他软件占用），"
                                           "请用界面按钮或鼠标触发键控制")
        if not self._hotkeys.get("key"):
            self._hotkey_notes["key"] = ("键盘热键注册失败（被其他软件占用），"
                                         "请用界面按钮或鼠标触发键控制")
        if not self._hotkeys.get("macro"):
            self._hotkey_notes["macro"] = ("宏热键注册失败（被其他软件占用），"
                                           "请用界面按钮或鼠标触发键控制")
        # 恢复上次的鼠标触发键。mouse 没存档时才装默认中键；key/macro
        # 没有鼠标键默认值，只保留各自的系统热键。v34 曾漏掉 macro 恢复，
        # 结果宏侧键明明已写盘，重启后却只剩 F7。
        trigger_defs = (
            ("mouse", self.toggle_mouse),
            ("key", self.toggle_keyboard),
            ("macro", self.toggle_macro),
        )
        restored_actions = set()
        for action, cb in trigger_defs:
            raw_id = self._btn_binding.get(action)
            if raw_id is None:
                continue
            try:
                raw_id = int(raw_id)
                bt.HUB.set_by_raw_id(
                    raw_id, cb, label=bt.RAW_ID_LABELS.get(raw_id),
                    action=action)
                restored_actions.add(action)
            except Exception as e:
                _log("恢复鼠标触发键失败：%s=%r -> %r" %
                     (action, raw_id, e))
        if "mouse" not in restored_actions:
            try:
                bt.HUB.set("middle", self.toggle_mouse, action="mouse")
            except Exception:
                pass
        return {"mouse": ok_mouse, "key": ok_key, "macro": ok_macro,
                "pause": ok_pause, "note": self._hotkey_err}

    def _try_hotkeys(self, candidates, cb, tag):
        for i, key in enumerate(candidates):
            try:
                hl = hk.HotkeyListener(key, cb)
            except hk.HotkeyError:
                continue
            good, err = hl.start()
            if good:
                self._hotkeys[tag] = (key, hl)
                # 第一个候选（=主人指定的 F5/F6）成功就不写降级说明。
                # 降级说明必须 pretty 化并在界面显示——2026-10-15 实测
                # 本机 F5/F6/Ctrl+F5/Ctrl+F6/Pause 全部被其他软件占用
                # （RegisterHotKey 返回 1409），用户按界面提示的 F6
                # 没有任何反应，只因提示写的是常量而不是真键。
                if i > 0:
                    self._hotkey_notes[tag] = (
                        "%s 被其他软件占用，已改用 %s"
                        % (hk.pretty_key(candidates[0]),
                           hk.pretty_key(key)))
                    _log("热键降级：%s 被占用，%s 已改用 %s"
                         % (hk.pretty_key(candidates[0]), tag,
                            hk.pretty_key(key)))
                else:
                    self._hotkey_notes.pop(tag, None)
                return key
        return None

    def set_system_hotkey(self, action, key_text):
        """
        改绑系统热键（比如把 F5 换成 Ctrl+Alt+M）。

        【为什么要留这条路】鼠标键虽然几乎不会被占用，但不是所有设备都有侧键
        （笔记本触控板、部分鼠标只有三键）。所以系统热键必须仍然可用，
        且允许换成任意「字母/数字/F键 + 修饰键」的组合。

        实现要点：先试新的，成功了再把旧的停掉。
        反过来做（先停再注册）一旦新键也被占用，用户就变成「两个热键都没了」。
        """
        act = str(action or "").strip().lower()
        if act not in ("mouse", "key", "macro"):
            return {"ok": False, "msg": "动作只能是 mouse / key / macro"}
        text = str(key_text or "").strip()
        if not text:
            return {"ok": False, "msg": "请先按一个键"}
        if hk.is_panic_key(text):
            return {"ok": False, "msg": "F12 是紧急释放键，不能同时当触发键"}
        try:
            hl = hk.HotkeyListener(text, None)
        except hk.HotkeyError as e:
            return {"ok": False, "msg": str(e)}
        cb = (self.toggle_mouse if act == "mouse"
              else self.toggle_macro if act == "macro"
              else self.toggle_keyboard)
        hl.callback = cb
        good, err = hl.start()
        if not good:
            return {"ok": False,
                    "msg": "%s 注册失败：%s"
                           % (hk.pretty_key(text), err or "可能被其他软件占用")}
        # 新的注册成功了，现在才停旧的
        old = self._hotkeys.get(act)
        if old:
            try:
                old[1].stop()
            except Exception:
                pass
        self._hotkeys[act] = (text, hl)
        # 用户显式设的键注册成功 = 该动作不再是降级状态
        self._hotkey_notes.pop(act, None)
        # 记进设置文件；保存失败必须明说，不能让用户以为重启仍会保留。
        trg = self._btn_binding.setdefault("sys", {})
        trg[act] = text
        ok, save_err = self._persist_bindings()
        ST.set_msg("热键已改为 %s" % hk.pretty_key(text))
        return {"ok": ok,
                "msg": ("热键已改为 %s" % hk.pretty_key(text)) if ok
                       else ("本次已生效，但保存失败：" + save_err),
                "pretty": hk.pretty_key(text)}

    def clear_system_hotkey(self, action):
        """取消某个系统热键，回到默认候选。"""
        act = str(action or "").strip().lower()
        if act not in ("mouse", "key", "macro"):
            return {"ok": False, "msg": "动作只能是 mouse / key / macro"}
        old = self._hotkeys.pop(act, None)
        if old:
            try:
                old[1].stop()
            except Exception:
                pass
        sys_map = self._btn_binding.get("sys", {})
        if isinstance(sys_map, dict):
            sys_map.pop(act, None)
            if not sys_map:
                self._btn_binding.pop("sys", None)
        ok, save_err = self._persist_bindings()
        cands = (HOTKEY_MOUSE_FALLBACK if act == "mouse"
                 else HOTKEY_MACRO_FALLBACK if act == "macro"
                 else HOTKEY_KEY_FALLBACK)
        cb = (self.toggle_mouse if act == "mouse"
              else self.toggle_macro if act == "macro"
              else self.toggle_keyboard)
        key = self._try_hotkeys(cands, cb, act)
        return {"ok": ok,
                "msg": (("已恢复默认热键：%s" % hk.pretty_key(key))
                        if key else "系统热键注册失败") if ok
                       else ("本次已恢复默认，但保存失败：" + save_err),
                "pretty": hk.pretty_key(key) if key else ""}

    def hotkey_text(self):
        """给前端显示当前生效的热键。"""
        m = self._hotkeys.get("mouse")
        k = self._hotkeys.get("key")
        mc = self._hotkeys.get("macro")
        return {"mouse": m[0] if m else "", "key": k[0] if k else "",
                # pretty 是给按键帽显示的："ctrl+alt+m" -> "Ctrl + Alt + M"
                "mouse_pretty": hk.pretty_key(m[0]) if m else "",
                "key_pretty": hk.pretty_key(k[0]) if k else "",
                # 【分区触发键名·2026-10-16】界面提示要写「用户真正会按的那个键」：
                # 绑了鼠标键（默认中键）就显示「中键」，否则才回落 F8/F9。
                # 以前一律显示系统热键名，主人手里按中键、界面写 F9，
                # 提示和行为对不上，是「按了没反应」误判的源头之一。
                "mouse_trigger": self._trigger_label("mouse"),
                "key_trigger": self._trigger_label("key"),
                # 【v34】宏启停键：回放/录制的启停键，宏区要用它显示真键名
                "macro": mc[0] if mc else "",
                "macro_pretty": hk.pretty_key(mc[0]) if mc else "",
                "macro_trigger": self._trigger_label("macro"),
                "note_macro": self._hotkey_notes.get("macro", ""),
                # 降级说明：界面必须显示，否则用户按提示里的 F5/F6
                # 没反应还找不到原因（2026-10-15 实测这两个键被占用）
                "note_mouse": self._hotkey_notes.get("mouse", ""),
                "note_key": self._hotkey_notes.get("key", ""),
                "note": self._hotkey_err}

    def shutdown(self):
        """退出前清理：停任务 + 注销热键 + 释放所有按键 + 撤托盘图标。"""
        # 先撤托盘：图标留着却点不动（进程正在退）比没有更糟
        try:
            _tray_stop()
        except Exception:
            pass
        # 【v27】宏：录制的钩子必须先卸。低级钩子挂在进程里，
        # 进程没了钩子会连带失效，但录制中的按下状态不会自己复位——
        # 用户按着 Shift 时关窗口，键盘就一直卡在按下状态。
        try:
            if _macro_recording():
                _macro_recorder().cancel()
            e = _MACRO.get("engine")
            if e is not None:
                e.ctx._user_stopped = True
                e.stop(wait=True)
                _MACRO["engine"] = None
                _MACRO["playing"] = False
        except Exception:
            _log_exc("shutdown.macro")
        for tag, (key, hl) in list(self._hotkeys.items()):
            try:
                hl.stop()
            except Exception:
                pass
        self._hotkeys.clear()
        # 鼠标键触发器也要清：否则回调还挂着对已销毁对象的引用，
        # 而且 Raw Input 线程还活着时会往已stop 的引擎里派发任务
        try:
            bt.HUB.clear()
        except Exception:
            pass
        try:
            self.stop_all()
        except Exception:
            pass
        _mouse_release_all()
        with ST.lock:
            if ST.key_lock:
                try:
                    ST.key_lock.stop()
                except Exception:
                    pass
        # 兜底再释放一次鼠标键。
        # 为什么要重复：stop_mouse 里已经释放过，但那时可能键盘锁定还按着键，
        # 而释放走的是 SendInput（依赖前台窗口能收事件）。
        # 两次加起来最坏3秒，换来「关窗时一定把所有键松开」，
        # 这个代价在退出场景完全可以接受。
        _mouse_release_all()
        # 弹窗线程退掉：进程马上 os._exit，留着也没意义
        try:
            toast.stop()
        except Exception:
            pass
        # 窗口过程还回去。进程紧接着就 os._exit，这一步主要是为了干净
        # （探针/测试环境里进程不退出，不还原会一直挂着子类化）
        _unsubclass_frameless()


# ======================================================================
# 启动
# ======================================================================
def _fix_console():
    if sys.platform != "win32":
        return
    try:
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001)
        k.SetConsoleCP(65001)
    except Exception:
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace",
                          line_buffering=True)
        except Exception:
            pass


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>爪Pro · Paw Pro</title>
<style>
  :root{
      /* ---- 底色层（带紫味的近黑，不用纯 #000）---- */
      --bg-void:#06030D;
      --bg-abyss:#0C0618;
      --panel:#120A24;
      --panel-inset:#180E30;
      /* ---- 强调色（紫霓虹：与图标渐变 #DEB0FF→#7A24D2 同源）---- */
      --accent:#8B5CF6;
      --accent-deep:#5B1E9E;
      --accent-mint:#3EF0C4;
      --accent-blue:#A78BFA;
      --accent-ultra:#3B1478;
      --alert:#FF2D4B;
      --spark:#FF9A3C;
      /* ---- 文字 ---- */
      --tx-hi:#F0E8FF;
      --tx-mid:#9C8CC4;
      --tx-dim:#5F4E85;
      /* ---- 线 / 网格 / 辉光 ---- */
      --line:rgba(139,92,246,.22);
      --line-soft:rgba(139,92,246,.10);
      --grid:rgba(139,92,246,.055);
      --glow:rgba(139,92,246,.55);
      --glow-soft:rgba(139,92,246,.18);
      /* ---- 形状：科技风靠切角而不是圆角 ---- */
      --chamfer:12px;
      --t-fast:.18s;
      --ease:cubic-bezier(.4,0,.2,1);
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%}
    body{
      background:var(--bg-void);
      color:var(--tx-hi);
      font-family:"Inter","HarmonyOS Sans SC","Segoe UI","Microsoft YaHei",system-ui,sans-serif;
      font-size:13px;
      min-height:100vh;
      /* 顶部留出标题栏的高度，否则内容会被压在标题栏下面 */
      padding:52px 24px 28px;
      overflow-x:hidden;
    }

    /* ---------- 滚动条：跟全息风统一 ----------
       【原来为什么显眼】完全没有 ::-webkit-scrollbar 样式，
       WebView2 会用默认的亮灰色Win10 滚动条压在深色内容上，
       是整个界面里唯一的亮色元素，一眼就出戏。
       现在做成细窄的青色发光条，滑块和轨道都走切角风格。 */
    /* 【v31 为什么把滚动条隐藏了】
       滚动条占的是**视口最右侧 11px**，而 position:fixed 的包含块
       不含滚动条 —— 也就是说右边那条缩放热区被滚动条盖住，
       鼠标移到最右边缘是滚动条（箭头），拖不动。
       实测过：左/上/下三个方向光标正常，右/右下死掉，就是这个原因。
       现在把滚动条宽度设 0：滚轮照样能滚，右边 8px 让给缩放热区。 */
    ::-webkit-scrollbar{width:0;height:0}
    ::-webkit-scrollbar-track{
      background:rgba(12,6,24,.9);
      /* 左侧一条细网格线，和 body 的背景网格对齐 */
      border-left:1px solid var(--line-soft);
    }
    ::-webkit-scrollbar-thumb{
      background:linear-gradient(180deg,var(--accent-deep),#2E0F5E);
      border:1px solid rgba(139,92,246,.35);
      /* 切角而不是圆角，和面板保持同一套形状语言 */
      clip-path:polygon(0 0,100% 0,100% calc(100% - 4px),
                        calc(100% - 4px) 100%,0 100%);
      min-height:38px;
    }
    ::-webkit-scrollbar-thumb:hover{
      background:linear-gradient(180deg,var(--accent),var(--accent-deep));
      box-shadow:0 0 10px var(--glow);
    }
    /* 两条轨道交汇的方块，默认是亮灰色，必须盖掉 */
    ::-webkit-scrollbar-corner{background:rgba(12,6,24,.9)}
    ::-webkit-scrollbar-button{display:none}

    /* ---------- 边框缩放热区 ----------
       frameless 窗口的边缘没有系统边框，用户不知道能拖。
       在四周埋 8 条热区，鼠标移上去变对应方向的光标。
       【v31】8px（原来 5px 太细，抓不住）；按下后由前端抓指针、
       后端 SetWindowPos，不再交给系统的模态缩放循环（见 win_resize_begin）。
       z-index 必须高于 titlebar，否则标题栏右侧几像素会抢走鼠标。 */
    .rz{position:fixed;z-index:40}
    .rz-t{top:0;left:10px;right:10px;height:8px;cursor:n-resize}
    .rz-b{bottom:0;left:10px;right:10px;height:8px;cursor:s-resize}
    .rz-l{left:0;top:10px;bottom:10px;width:8px;cursor:w-resize}
    .rz-r{right:0;top:10px;bottom:10px;width:8px;cursor:e-resize}
    .rz-tl{top:0;left:0;width:14px;height:14px;cursor:nwse-resize}
    .rz-tr{top:0;right:0;width:14px;height:14px;cursor:nesw-resize}
    .rz-bl{bottom:0;left:0;width:14px;height:14px;cursor:nesw-resize}
    .rz-br{bottom:0;right:0;width:14px;height:14px;cursor:nwse-resize;
           background:linear-gradient(135deg,transparent 45%,
                      rgba(139,92,246,.5) 45%,rgba(139,92,246,.5) 55%,
                      transparent 55%,transparent 62%,
                      rgba(139,92,246,.3) 62%,rgba(139,92,246,.3) 70%,
                      transparent 70%)}
    /* 最大化后不能拖边框（Windows 也是这个行为） */
    body.maxed .rz{display:none}

    /* ---------- 自绘标题栏 ----------
       无边框窗口（frameless=True）没有系统标题栏，
       自己画一条才能和整体的全息深色统一。
       系统那条是亮色 Win10 风格，深色内容配亮色标题条很割裂。 */
    .titlebar{
      position:fixed;top:0;left:0;right:0;height:34px;z-index:30;
      display:flex;align-items:center;
      background:linear-gradient(180deg,#150C28 0%,#0E0718 100%);
      border-bottom:1px solid var(--line);
      /* 上边一条高光线，是全息面板的标志性细节 */
      box-shadow:0 1px 0 rgba(139,92,246,.14), 0 4px 14px rgba(0,0,0,.5);
      user-select:none;-webkit-user-select:none;
    }
    .titlebar .tb-left{display:flex;align-items:center;gap:9px;padding-left:14px;min-width:0}
    /* 标题栏不能选中文本，否则拖动会选中一片 */
    /* 【v34 复核修复】左上角 logo 原来是 CSS 画的紫色切角**方块**，
       和程序图标（三根紫尖爪）完全对不上——主人指出的「不一致」就是它。
       现在换成与 assets/PawPro-violet.ico 同款的爪形内联 SVG：
       渐变 #DEB0FF→#7A24D2 直接取自图标本体，缩放不糊、不依赖外部文件
       （HTML 会被 dashboard.py 整体内嵌进 exe，必须自包含）。 */
    .titlebar .tb-logo{
      width:14px;height:14px;flex:none;display:block;
      filter:drop-shadow(0 0 5px var(--glow));
    }
    .titlebar .tb-logo svg{width:100%;height:100%;display:block}
    .titlebar .tb-title{
      font-size:11.5px;font-weight:600;letter-spacing:.13em;
      color:var(--tx-hi);white-space:nowrap;
    }
    .titlebar .tb-ver{
      font-size:9px;letter-spacing:.2em;text-transform:uppercase;
      color:var(--tx-dim);white-space:nowrap;
      padding-left:10px;border-left:1px solid var(--line-soft);
    }
    .titlebar .tb-right{margin-left:auto;display:flex;height:100%}
    .titlebar .tb-btn{
      width:44px;height:100%;border:0;background:transparent;
      color:var(--tx-mid);cursor:pointer;
      display:flex;align-items:center;justify-content:center;
      transition:background var(--t-fast) var(--ease),color var(--t-fast) var(--ease);
    }
    .titlebar .tb-btn:hover{background:rgba(139,92,246,.13);color:var(--tx-hi)}
    .titlebar .tb-btn:active{background:rgba(139,92,246,.22)}
    /* 关闭按钮 hover 变红——这是用户肌肉记忆，不能改 */
    .titlebar .tb-close:hover{background:#C42B1C;color:#fff}

    /* ---------- 触发方式区 ---------- */
    .sublabel{
      margin:14px 0 8px;font-size:10px;letter-spacing:.2em;
      text-transform:uppercase;color:var(--tx-dim);
      padding-top:12px;border-top:1px dashed var(--line-soft);
    }
    .trig{margin-top:var(--s5,20px)}
    .trig .keycap.mint{
      color:var(--accent-mint);border-color:rgba(62,240,196,.4);
      background:rgba(62,240,196,.08);
    }
    .trig-grid{
      display:grid;grid-template-columns:repeat(3,1fr);gap:var(--s3,12px);
      margin:14px 0 4px;
    }
    .trig-cell{
      border:1px solid var(--line-soft);
      border-left:2px solid var(--accent);
      background:linear-gradient(180deg,rgba(139,92,246,.06),rgba(18,10,36,.4));
      padding:11px 14px;
      clip-path:polygon(0 0,100% 0,100% calc(100% - 7px),
                        calc(100% - 7px) 100%,0 100%);
    }
    .trig-cell .tc-title{
      font-size:10px;letter-spacing:.18em;text-transform:uppercase;
      color:var(--tx-dim);margin-bottom:7px;
    }
    .trig-cell .tc-body{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
    .trig-cell .tc-kbd{
      font-family:"JetBrains Mono","Cascadia Mono",Consolas,monospace;
      font-size:17px;font-weight:700;color:var(--accent);
      text-shadow:0 0 12px var(--glow);
    }
    .trig-cell .tc-kbd.amber{color:var(--spark);text-shadow:0 0 12px rgba(255,154,60,.5)}
    .trig-cell .tc-hint{font-size:11px;color:var(--tx-dim)}

    .trig-bind{
      display:flex;align-items:center;gap:9px;flex-wrap:wrap;
      margin-top:12px;padding-top:12px;border-top:1px dashed var(--line-soft);
    }
    .trig-bind .tb-label{font-size:12px;color:var(--tx-mid);flex:0 0 auto}
    .trig-bind .tb-arrow{color:var(--tx-dim);font-size:13px;flex:0 0 auto}
    /* 注意：不能写 min-width:180px 之外再让 select 自适应，
       flex 项的 flex-basis 默认是 auto，内容短的会被拉伸/撑开。
       固定 basis +允许收缩，两个下拉才并排一行。 */
    .trig-bind select{flex:0 1 220px;width:220px;min-width:0}
    .trig-bind button{flex:0 0 auto}

    /* ---------- 宏录制区（v27 / S2） ----------
       独立整宽分区，不塞进上面的三栏：宏是「录一次、以后反复放」的
       另一套工作流，和连点/锁定的实时控制不同类；而且宽屏下三栏
       已经有鼠标/键盘/触发三张卡，硬塞第四张会把 1440 的三栏等高
       布局挤成 3+1（规则 43 的验收就是量这个）。
       四格复用 .trig-cell 的同一套皮，视觉跟着触发方式区走。 */
    .macro{margin-top:var(--s5,20px)}
    .macro .macro-grid{
      display:grid;gap:12px;margin-top:12px;
      grid-template-columns:repeat(4,minmax(0,1fr));
    }
    /* 四格在 1240 的窄屏会挤成每个 280px，文字全折行 —— 降两列 */
    @media (max-width:1320px){
      .macro .macro-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
    }
    @media (max-width:760px){
      .macro .macro-grid{grid-template-columns:1fr}
    }
    .macro .mc-inline{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .macro .mc-inline + .mc-inline{margin-top:8px}
    .macro .mc-inline label{font-size:11px;color:var(--tx-dim);flex:0 0 auto}
    .macro .mc-inline input[type=text],
    .macro .mc-inline input[type=number]{flex:1 1 90px;min-width:0;width:auto}
    .macro .mc-inline select{flex:1 1 90px;min-width:0;width:auto}
    .macro .mc-inline button{flex:0 0 auto;width:auto;min-width:0;padding:8px 12px}
    .macro .mc-check{
      display:flex;align-items:center;gap:6px;margin-top:8px;
      font-size:11px;color:var(--tx-mid);cursor:pointer;
    }
    .macro .mc-check input{width:auto;flex:0 0 auto}
    .macro .mc-stat{
      font-size:11.5px;color:var(--tx-mid);
      font-family:"JetBrains Mono",Consolas,monospace;
    }
    .macro .mc-stat b{color:var(--accent);font-size:14px}
    .macro .mc-preview{
      margin-top:7px;font-size:11px;color:var(--tx-dim);line-height:1.5;
      min-height:32px;word-break:break-all;
    }

    /* ---------- 步骤列表（v30）----------
       照成熟宏编辑器的形态：一行一个节点，点一行打开设置弹窗。
       不再让用户面对几百条原始事件。 */
    .macro .mc-steps{
      margin-top:8px;max-height:190px;overflow:auto;
      border:1px solid var(--line);background:rgba(255,255,255,.02);
      font-family:"JetBrains Mono",Consolas,monospace;font-size:11.5px;
    }
    .macro .mc-steps::-webkit-scrollbar{width:8px}
    .macro .mc-steps::-webkit-scrollbar-thumb{background:rgba(139,92,246,.35)}
    .macro .mc-step{
      display:grid;grid-template-columns:34px minmax(0,1fr) auto;
      gap:8px;align-items:center;padding:6px 9px;cursor:pointer;
      border-bottom:1px solid rgba(255,255,255,.05);
    }
    .macro .mc-step:hover{background:rgba(139,92,246,.10)}
    .macro .mc-step.sel{background:rgba(139,92,246,.16);
      box-shadow:inset 2px 0 0 var(--accent)}
    .macro .mc-step .ms-i{color:var(--tx-dim);font-size:10.5px;text-align:right}
    .macro .mc-step .ms-l{color:var(--tx-hi);overflow:hidden;
      text-overflow:ellipsis;white-space:nowrap}
    .macro .mc-step .ms-d{color:var(--accent-mint);font-size:10.5px;
      white-space:nowrap}
    .macro .mc-empty{padding:14px 10px;color:var(--tx-dim);
      font-size:11px;text-align:center}
    .macro .mc-steps-more{font-size:10.5px;color:var(--tx-dim);margin-top:4px}

    /* ---------- 节点设置弹窗 ---------- */
    .mc-modal{position:fixed;inset:0;z-index:200;display:none;
      background:rgba(4,9,20,.68);backdrop-filter:blur(3px);
      align-items:center;justify-content:center;padding:24px}
    .mc-modal.show{display:flex}
    .mc-dialog{width:min(520px,100%);max-height:88vh;overflow:auto;
      background:linear-gradient(160deg,rgba(18,32,58,.98),rgba(9,16,32,.98));
      border:1px solid rgba(139,92,246,.35);padding:18px 22px 20px;
      box-shadow:0 24px 60px rgba(0,0,0,.6)}
    .mc-dh{display:flex;align-items:center;gap:10px;margin-bottom:14px}
    .mc-dh b{font-size:15px;color:var(--tx-hi)}
    .mc-dh span{font-size:11px;color:var(--tx-dim)}
    .mc-x{margin-left:auto;width:auto;padding:2px 8px;background:transparent;
      border:1px solid var(--line);color:var(--tx-dim);cursor:pointer}
    .mc-field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}
    .mc-field label{font-size:11px;color:var(--tx-dim)}
    .mc-field input,.mc-field select{width:100%}
    .mc-two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .mc-tip{font-size:10.5px;color:var(--tx-dim);line-height:1.6;
      margin:-2px 0 12px}
    .mc-dfoot{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
    .mc-dfoot button{flex:0 0 auto;width:auto}
    .mc-dfoot .primary{margin-left:auto}
    .mc-danger{color:var(--alert)!important;
      border-color:rgba(255,90,90,.45)!important}

    /* ---------- 按键捕获组件 ----------
       替代「104 个键位的下拉」：点一下按钮进入捕获，
       然后按什么键就录什么。组合键会显示成Ctrl + W。
       下拉只留给真正需要枚举的项（如「点击 / 长按」）。 */
    .capwrap{display:flex;align-items:center;gap:8px;flex-wrap:wrap;width:100%}
    /* 已录下的键：筹码（chip）形式，点 × 单独删一个 */
    .capchip{
      display:inline-flex;align-items:center;gap:6px;
      font-family:"JetBrains Mono",Consolas,monospace;
      font-size:11.5px;font-weight:700;color:var(--accent);
      background:rgba(139,92,246,.12);
      border:1px solid rgba(139,92,246,.45);
      padding:4px 8px 4px 10px;
      clip-path:polygon(5px 0,100% 0,100% calc(100% - 5px),
                        calc(100% - 5px) 100%,0 100%,0 5px);
    }
    .capchip .x{
      cursor:pointer;color:var(--tx-dim);font-weight:400;
      font-size:13px;line-height:1;padding:0 1px;
    }
    .capchip .x:hover{color:var(--alert)}
    /* 正在等待按键时的占位提示 */
    .capwait{
      font-size:11.5px;color:var(--accent-mint);
      border:1px dashed rgba(62,240,196,.5);
      padding:4px 11px;
      clip-path:polygon(5px 0,100% 0,100% calc(100% - 5px),
                        calc(100% - 5px) 100%,0 100%,0 5px);
      animation:pulse 1.1s var(--ease) infinite;
    }

    /* 原始输入自检条：绑键没反应时，一眼看出是"没收到消息"还是"绑不上去" */
    .rawdiag{
      display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;
      margin-top:9px;padding:6px 10px;
      font-size:11px;letter-spacing:.02em;
      color:var(--text-dim);
      background:rgba(255,255,255,.025);
      border-left:2px solid rgba(62,240,196,.4);
    }
    .rawdiag .rd-item b{font-weight:600;color:var(--text);
      font-variant-numeric:tabular-nums}
    .rawdiag .rd-item b.ok{color:var(--accent-mint)}
    .rawdiag .rd-item b.bad{color:var(--spark)}
    .rawdiag .rd-item b.rd-hot{color:var(--accent-mint)}
    .rawdiag .rd-hint{flex:1 1 100%;color:var(--text-dim);opacity:.82;
      line-height:1.5}

    /* 「开始识别」后按钮呼吸，提示用户现在该按侧键了 */
    button.pulse{animation:breathe 1.4s var(--ease) infinite;
      border-color:var(--accent-mint)!important;color:var(--accent-mint)!important}
    button:disabled{opacity:.42;cursor:not-allowed}
    /* 底层：径向渐层 + 网格平铺。
       性能注意：网格必须用 background-size 平铺，不要堆重复 DOM。 */
    body::before{
      content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
      background:
        radial-gradient(120% 90% at 50% 0%, var(--bg-abyss) 0%, var(--bg-void) 70%),
        linear-gradient(var(--grid) 1px,transparent 1px),
        linear-gradient(90deg,var(--grid) 1px,transparent 1px);
      background-size:100% 100%,32px 32px,32px 32px;
    }
/* 扫描线纹理 */
  body::after{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:1;
    background:repeating-linear-gradient(0deg,
      rgba(255,255,255,.022) 0 1px, transparent 1px 3px);
  }
  /* 全局扫描光束：只给1 个元素跑动画，别整屏加。
     刻意压得很淡（.22 -> .12）：这道光束会从 KPI 数字上扫过去，
     太亮会把文字盖住，看起来像「文字变白了/配色错了」。 */
  .scanbeam{
    position:fixed;left:0;right:0;height:2px;pointer-events:none;z-index:5;
    background:linear-gradient(90deg,transparent,rgba(139,92,246,.12),transparent);
    animation:scanline 11s linear infinite;
  }
    @keyframes scanline{ 0%{top:-4%} 100%{top:104%} }
    @keyframes pulse{ 0%,100%{opacity:1} 50%{opacity:.35} }
    @keyframes breathe{
      0%,100%{box-shadow:0 0 18px var(--glow-soft)}
      50%{box-shadow:0 0 34px rgba(139,92,246,.42)}
    }
    /* 【2026-10-16 主人两条反馈定下的宽屏方案】
       ① 「最大化后两边全黑区域很违和」→ 横向空间必须用起来
       ② 「单纯拉伸太蠢，功能区、按钮都被拉长很难看」→ 不许拉宽面板
       调研结论（Tailwind / 大屏设计惯例 / Grid 自动布局）：
       宽屏靠**加列**吃掉多余宽度，容器封顶；面板保持自己的舒适宽度。
       所以窄窗口完全不变（仍是单列竖排），只有拉宽到 1440px 以上才切换：
       （阈值取 1440 而不是 1500：窗口宽度 ≠ 视口宽度，页面内容会出
        纵向滚动条，视口比窗口窄约 17~24px。设 1500 的话，用户把窗口
        拖到 1500 反而进不了三栏 —— 反直觉。）
         row1  [ 头部 ]
         row2  [ 鼠标区 ][ 键盘区 ][ 触发方式 ]
         row3  [ 功能说明 · 文字分两栏 ]
       .grid 用 display:contents 退化成「透明壳」：它自己不生成盒子，
       里面的两张卡片直接进外层网格，才能和触发方式排成同一行。 */
    .wrap{max-width:1240px;margin:0 auto;position:relative;z-index:2}
    @media (min-width:1440px){
      /* 【2026-10-16 第二轮反馈：三栏要「齐」，光排开不够】
         第一版只做到「加列」，但三张卡各自按内容高度自然撑开，
         底部参差；三段的说明文字长短不一，又把下面的第一行控件
         顶到不同高度 —— 观感就是「杂乱无章」。补两件事：
           ① align-items 用默认的 stretch：三栏**等高**、底部边框齐平
              （原来写 align-items:start，三栏各高各的）；
           ② 三张卡的说明段给统一 min-height：下面的第一行控件
              （鼠标按键 / 生效按键 / 三个触发格）才会落在同一水平线上。
         ⚠ 不要为了「尾部沉底」给卡片加 display:flex：卡片内是 block 流，
         .row(margin-bottom) 与 .btnrow(margin-top)、.sublabel(margin-top)
         靠**相邻边距折叠**维持在 11~16px；一旦变 flex，折叠失效，
         间距会变成 11+16=27px 这类叠加值，而且三栏元素组合不同、
         各自膨胀量还不一样 —— 越改越乱。等高就够整齐了。 */
      .wrap{max-width:1840px;display:grid;gap:14px;
            grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1.08fr)}
      .wrap>.hero{grid-column:1/-1;grid-row:1}
      /* 【v31 新元素必须显式排位】流程条是 .wrap 的直接子元素，若不给
         grid-row，它会被自动放置到「第一个空格子」——实测在 2560 下
         掉到卡片下方（第 3 行第 1 列），顶部反而空着。 */
      .wrap>.flow{grid-column:1/-1;grid-row:2;margin-top:0}
      .wrap>.grid{display:contents}
      .wrap>.grid>#cardMouse{grid-column:1;grid-row:3}
      .wrap>.grid>#cardKey{grid-column:2;grid-row:3}
      /* ⚠ .trig 在窄屏有 margin-top:20px（和上方卡片的间距）。
         三栏并排后它还留着，会把触发方式卡**整体压低 20px**，
         顶部对不齐 —— 这才是「三个区杂乱无章」的主因之一，实测过。 */
      .wrap>.trig{grid-column:3;grid-row:3;margin-top:0}
      /* 宏录制区（v27）整行铺在第 4 排，不让它挤进三栏：宏是另一套工作流，
         且三栏等高（规则 43）量的就是鼠标/键盘/触发这三张卡。 */
      .wrap>.macro{grid-column:1/-1;grid-row:4;margin-top:0}
      /* 说明段等高：下面第一行控件的起始线才会对齐。
         取「能容三行」的高度（实测最长的那段是 3 行），
         短的那两栏留一点白，换三栏严格齐平 —— 对齐优先。 */
      .wrap>.grid>#cardMouse>.hint,
      .wrap>.grid>#cardKey>.hint,
      .wrap>.trig>.hint{min-height:58px;margin-bottom:14px}
      /* v31：整块「功能说明」已删，原来的 .desc 两栏规则一并去掉（死规则）。 */
      /* 触发方式挤进 1/3 宽（~620px）后，三格并排每格只剩 190px；
         固定两列，别让 auto-fit 在临界宽度上抖成 3 列 */
      .wrap>.trig .trig-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
      /* 【v30 主人反馈：最大化后键盘区、触发方式区底部一大片空白】
         三栏等高（stretch）本身是对的，问题在内容短的两栏底部空着。
         参考仪表盘的通行做法：把「页脚」钉在卡片底部 —— 卡片改纵向
         flex，状态行 / 自检条用 margin-top:auto 吃掉剩余空间。
         视觉上从「卡片下方一块死白」变成「内容与页脚之间的呼吸间距」，
         鼠标区内容最多、位置基本不变，三栏底边仍然齐平。
         ⚠ flex 下相邻 margin 不再折叠（.row 的 mb 11 + .btnrow 的 mt 16
         会叠成 27），所以把这种情况的间隔抵回 16 —— 不然三栏里
         有 .row 的那两张会整体被推低 11px，顶部就又不齐了。 */
      .wrap>.grid>#cardMouse,
      .wrap>.grid>#cardKey,
      .wrap>.trig{display:flex;flex-direction:column}
      .wrap>.grid>#cardMouse>.status,
      .wrap>.grid>#cardKey>.status,
      .wrap>.trig>.rawdiag{margin-top:auto}
      .wrap .card>.row+.btnrow{margin-top:5px}
    }
    /* 再宽（21:9 / 32:9 带鱼屏）就上第 4 栏，别让 1840 的上限把两边留空。
       【v31 修正】v18 那会儿第 4 栏放的是「功能说明」段，v31 把整块说明删了，
       第 4 栏就成了空黑框（实测 2560 截图右半边全空）。改成把宏录制区搬进
       第 4 栏、纵向跨第 2~3 行：宽度用满、三张主卡仍是 1/4 宽不被拉长，
       宏区内部四格在窄栏里改单列竖排（否则每格只剩 140px，字全折行）。 */
    @media (min-width:2200px){
      .wrap{max-width:none;grid-template-columns:repeat(4,minmax(0,1fr))}
      .wrap>.trig{grid-column:3;grid-row:3}
      .wrap>.macro{grid-column:4;grid-row:3/5;margin-top:0}
      .wrap>.macro .macro-grid{grid-template-columns:1fr}
    }

    /* ---------- 面板通用（切角 + 发光描边） ---------- */
    .panel{
      position:relative;
      background:linear-gradient(160deg, rgba(139,92,246,.10) 0%,
                 rgba(18,10,36,.72) 42%, rgba(24,14,48,.55) 100%);
      border:1px solid var(--line);
      clip-path:polygon(var(--chamfer) 0,100% 0,
                        100% calc(100% - var(--chamfer)),
                        calc(100% - var(--chamfer)) 100%,0 100%,0 var(--chamfer));
      box-shadow:0 0 24px var(--glow-soft), inset 0 0 32px rgba(139,92,246,.06);
      padding:16px 24px;
    }
    /* 顶部全息高光描边 */
    .panel::before{
      content:"";position:absolute;left:var(--chamfer);right:0;top:0;height:1px;
      background:linear-gradient(90deg,transparent,var(--glow) 30%,var(--glow) 70%,transparent);
    }
    /* ---------- 四角 HUD 角标 ---------- */
    .hud{position:relative}
    .hud i{position:absolute;width:14px;height:14px;
           border:1px solid var(--accent);opacity:.9;pointer-events:none}
    .hud i:nth-child(1){left:-1px;top:-1px;border-right:0;border-bottom:0}
    .hud i:nth-child(2){right:-1px;top:-1px;border-left:0;border-bottom:0}
    .hud i:nth-child(3){left:-1px;bottom:-1px;border-right:0;border-top:0}
    .hud i:nth-child(4){right:-1px;bottom:-1px;border-left:0;border-top:0}

/* ---------- Hero ---------- */
  .hero{padding:18px 26px 16px}
  .topbar{
    display:flex;align-items:center;gap:14px;
    padding-bottom:12px;border-bottom:1px solid var(--line-soft);
  }
  .hero h1{
    font-size:24px;letter-spacing:.14em;font-weight:700;line-height:1.1;
    color:var(--tx-hi);
    text-shadow:0 0 10px var(--glow),0 0 28px rgba(139,92,246,.25);
  }
  .hero h1 .thin{color:var(--accent);font-weight:400}
  .topbar .sub{
    font-size:10.5px;letter-spacing:.22em;text-transform:uppercase;
    color:var(--tx-dim);margin-top:4px;
  }
  .topbar .right{margin-left:auto;display:flex;align-items:center;gap:10px}
  /* 状态标签：圆点 + 全大写 */
  .tag{
    display:inline-flex;align-items:center;gap:6px;
    padding:4px 11px;font-size:10px;font-weight:600;
    letter-spacing:.14em;text-transform:uppercase;
    color:var(--accent-mint);
    border:1px solid rgba(62,240,196,.38);
    background:rgba(62,240,196,.08);
    clip-path:polygon(4px 0,100% 0,100% calc(100% - 4px),
                      calc(100% - 4px) 100%,0 100%,0 4px);
  }
  .tag::before{
    content:"";width:5px;height:5px;border-radius:50%;
    background:var(--accent-mint);box-shadow:0 0 8px var(--accent-mint);
    animation:pulse 1.8s var(--ease) infinite;
  }
  .tag.run{color:var(--accent);border-color:rgba(139,92,246,.45);background:rgba(139,92,246,.10)}
  .tag.run::before{background:var(--accent);box-shadow:0 0 8px var(--accent)}
  /* 暂停态：琥珀色，和「运行中」的青色明确区分 */
  .tag.pause{color:#FFB347;border-color:rgba(255,179,71,.45);background:rgba(255,179,71,.10)}
  .tag.pause::before{background:#FFB347;box-shadow:0 0 8px #FFB347}

  /* 右上角暂停按钮：幽灵样式，不抢主按钮的视觉重心 */
  .ghostbtn{
    flex:0 0 auto;min-width:0;
    height:34px;padding:0 15px;
    display:inline-flex;align-items:center;justify-content:center;
    font:600 11px/1 "Inter","Microsoft YaHei",inherit;letter-spacing:.12em;
    color:var(--tx-mid);background:rgba(255,255,255,.04);
    border:1px solid var(--line-soft);
    clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),
                      calc(100% - 8px) 100%,0 100%,0 8px);
    cursor:pointer;transition:color .15s var(--ease),border-color .15s var(--ease);
  }
  .ghostbtn:hover{color:var(--accent);border-color:rgba(139,92,246,.45)}
  .ghostbtn.paused{color:#FFB347;border-color:rgba(255,179,71,.5);
                   background:rgba(255,179,71,.08)}

  /* 全局消息行：单独一行，和各区分区状态分开 */
  .gmsg{
    margin-top:10px;padding:7px 12px;font-size:12px;color:var(--tx-mid);
    background:rgba(18,10,36,.5);border-left:2px solid var(--accent-deep);
  }

  .hero .tagline{margin-top:11px;color:var(--tx-mid);font-size:12.5px;line-height:1.7}
  /* KPI 行：等宽大数字 + 标签 + 迷你条 */
  .hero-grid{
    display:grid;grid-template-columns:repeat(4,1fr);
    gap:12px;margin-top:13px;
  }
  .kpi{
    position:relative;
    background:linear-gradient(180deg,rgba(139,92,246,.06),rgba(18,10,36,.55));
    border:1px solid var(--line-soft);
    border-left:2px solid var(--accent-deep);
    padding:10px 13px;
    clip-path:polygon(0 0,100% 0,100% calc(100% - 8px),
                      calc(100% - 8px) 100%,0 100%);
  }
  /* 辉光用 ::after 画在文字下层，而不是给文字加 text-shadow。
     【踩过的坑】text-shadow + overflow:hidden + text-overflow:ellipsis
     三个一起用，Chromium(WebView2) 会把阴影渲成一大块纯色盖住文字，
     表现为「KPI 变成蓝底白块，字看不清」。
     用独立伪元素做辉光就没有这个冲突。 */
  .kpi .n{
    position:relative;
    font-family:"JetBrains Mono","Cascadia Mono",Consolas,monospace;
    font-size:20px;font-weight:700;line-height:1.15;color:var(--accent);
    /* 必须加：数字每秒刷新时不左右抖动 */
    font-variant-numeric:tabular-nums;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  }
  /* 【踩过的坑，别再加回来】KPI 文字的辉光试过两种写法，两种都炸：
     1) text-shadow + overflow:hidden + text-overflow:ellipsis
        -> Chromium(WebView2) 把阴影渲成一大块纯色，直接盖住文字；
     2) 用 .n::after + filter:blur() 画在下层当辉光
        -> 在 clip-path 的父容器里 blur 会溢出成蓝块。
     所以这里就**不加任何文字辉光**，靠颜色和字重区分层次。
     面板标题的 text-shadow 没有 overflow/ellipsis，是安全的，可以留。 */
  /* 「就绪」这类非运行态用弱化色 + 小一号字，不要抢主数据的视觉权重 */
  .kpi .n.idle{color:var(--tx-mid);font-size:16px;padding-top:3px}
  .kpi .n.on{color:var(--accent-mint)}
  .kpi .t{
    font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;
    color:var(--tx-dim);margin-top:3px;
  }
  /* 迷你进度条。
     【踩过的大坑】原本这里写的是
        background:linear-gradient(90deg,#5B1E9E,#8B5CF6); box-shadow:0 0 8px;
     在 2px 高 + overflow:hidden 的容器里，Chromium(WebView2) 会把
     box-shadow 辉光渲染成**一整块亮蓝色矩形**盖住 KPI 文字，
     表现为「就绪 / 0 / 0s 全变成蓝底白块，完全看不清」。
     定位过程见交付说明：单独去掉 box-shadow 或单独去掉渐变都无效，
     必须整条 bar 一起去掉才归零 —— 是两者叠加触发的合成异常。
     现在改成：渐变只画在 2px 的<i> 上，辉光靠 ::after 的低透明度色，
     且不给它box-shadow。 */
  .kpi .bar{height:2px;background:var(--line-soft);margin-top:7px;overflow:hidden}
  .kpi .bar>i{
    position:relative;display:block;height:100%;width:40%;
    background:linear-gradient(90deg,var(--accent-blue),var(--accent));
  }
  .kpi .bar>i::after{
    content:"";position:absolute;inset:0;
    background:var(--accent);opacity:.35;filter:blur(2px);
    pointer-events:none;
  }

/* ---------- 一句话流程条 ----------
     原来是整块「功能说明 / FUNCTION」两大段（占一屏），v31 删掉，
     只留这条三步流程 + 一句注意。要查细节看使用说明.md。 */
  .flow{
    display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    margin-top:14px;padding:9px 16px;
    border:1px solid var(--line-soft);
    border-left:2px solid var(--accent-deep);
    background:linear-gradient(90deg,rgba(139,92,246,.07),rgba(18,10,36,.45));
    font-size:12px;color:var(--tx-mid);
  }
  .flow .fs b{color:var(--accent);margin-right:5px;font-weight:700}
  .flow .fa{color:var(--accent-deep)}
  .flow .fnote{margin-left:auto;color:var(--tx-dim);font-size:11px}

    /* ---------- HUD 分割线 ---------- */
    .divider{position:relative;height:1px;background:var(--line-soft);margin:16px 0}
    .divider::before{
      content:"";position:absolute;left:0;top:0;width:56px;height:1px;
      background:var(--accent);box-shadow:0 0 8px var(--glow);
    }
    .divider::after{
      content:"";position:absolute;right:0;top:-3px;width:96px;height:7px;
      background:repeating-linear-gradient(90deg,var(--line) 0 1px,transparent 1px 8px);
    }

    /* ---------- 卡片网格：左鼠标 / 右键盘 ---------- */
    .grid{margin-top:16px;display:grid;gap:16px;
          grid-template-columns:repeat(2,minmax(0,1fr))}
    @media (max-width:880px){
      .grid{grid-template-columns:1fr}
      .hero-grid{grid-template-columns:repeat(2,1fr)}
    }
    .card{padding:18px 24px 20px}
    /* 运行时激活态：主色描边 + 呼吸辉光 */
    .card.on{
      border-color:rgba(139,92,246,.55);
      animation:breathe 3.2s var(--ease) infinite;
    }
    .card.k.on{border-color:rgba(255,154,60,.55)}
    .card h2{
      font-size:15px;letter-spacing:.16em;text-transform:uppercase;
      display:flex;align-items:center;gap:9px;color:var(--tx-hi);
      text-shadow:0 0 10px var(--glow);
    }
    .card.k h2{color:var(--tx-hi)}
    .card.k h2 .dot{background:var(--spark);box-shadow:0 0 10px var(--spark)}
    .card h2 .dot{
      width:6px;height:6px;border-radius:50%;
      background:var(--accent);box-shadow:0 0 10px var(--accent);
      flex:none;
    }
    .card .hint{font-size:11.5px;color:var(--tx-dim);margin:7px 0 15px;line-height:1.7}
    .hint.tiny{font-size:11px;line-height:1.65;margin:2px 0 14px}
    /* 降级提示：只改颜色不加框——它是 hkNote 位置的内联提示，
       套 .warn 的大框会把触发方式区撑变形 */
    .hint.tiny.warn{color:var(--spark)}

    /* 热键按键帽 */
    .keycap{
      margin-left:auto;font-family:"Cascadia Mono",Consolas,monospace;
      font-size:11px;font-weight:700;letter-spacing:.1em;
      padding:3px 10px;color:var(--accent);
      border:1px solid rgba(139,92,246,.5);background:rgba(139,92,246,.10);
      clip-path:polygon(5px 0,100% 0,100% calc(100% - 5px),
                        calc(100% - 5px) 100%,0 100%,0 5px);
      box-shadow:0 0 14px rgba(139,92,246,.28);
    }
    .keycap.amber{
      color:var(--spark);border-color:rgba(255,154,60,.5);
      background:rgba(255,154,60,.10);box-shadow:0 0 14px rgba(255,154,60,.25);
    }

    /* ---------- 表单 ---------- */
    .row{
      display:grid;grid-template-columns:126px 1fr;
      gap:12px;align-items:center;margin-bottom:11px;
    }
    .row>label{
      font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
      color:var(--tx-dim);text-align:right;
    }
    input,select{
      background:rgba(24,14,48,.75);border:1px solid var(--line-soft);
      color:var(--tx-hi);padding:8px 11px;font-size:13px;outline:none;width:100%;
      font-family:inherit;
      clip-path:polygon(6px 0,100% 0,100% calc(100% - 6px),
                        calc(100% - 6px) 100%,0 100%,0 6px);
      transition:all var(--t-fast) var(--ease);
    }
    input:focus,select:focus{
      border-color:var(--accent);background:rgba(139,92,246,.08);
      box-shadow:0 0 0 1px rgba(139,92,246,.3),0 0 16px rgba(139,92,246,.18);
    }
    select option{background:#120A24;color:var(--tx-hi)}
    /* 复选框行：整行可点，不能继承右对齐的小字标签样式 */
    .row label.chk{
      display:flex;align-items:center;gap:8px;text-align:left;
      color:var(--tx-mid);font-size:12px;letter-spacing:0;
      text-transform:none;cursor:pointer;line-height:1.5;
    }
    .row label.chk input{
      width:auto;flex:none;margin:0;cursor:pointer;accent-color:#8B5CF6;
      padding:0;
    }
    /* 固定坐标行：输入框和「取当前」按钮并排（.row 是两列 grid，
       按钮必须包一层，直接当第三个子元素会撑出第三列） */
    .poswrap{display:flex;gap:8px;align-items:center;min-width:0}
    .poswrap input{flex:1;min-width:0}
    .poswrap button{flex:none;min-width:0;width:auto;padding:8px 12px;
      letter-spacing:.08em}
    /* v26 多点坐标：文本框在上、按钮在下（竖排），否则 3 行文本挤成一团 */
    .poswrap.col{flex-direction:column;align-items:stretch}
    .poswrap textarea{
      width:100%;resize:vertical;min-height:64px;
      font:12px/1.5 "Consolas","Microsoft YaHei",monospace;
      letter-spacing:.04em
    }
    .btnrow.tight{margin-top:6px;gap:6px}

    /* ---------- 按钮 ---------- */
    .btnrow{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
    /* 【v31 按钮统一】以前同一个界面里三种尺寸（大主按钮 / 小幽灵按钮 /
       标题栏图标按钮），行高、切角、字距各写一套，看着散。
       现在统一成一套：高 34px、切角 8px、11px/600、字距 .14em，
       宽度仍然 flex:1 均分（同一行按钮等宽）。 */
    button{
      flex:1;min-width:112px;height:34px;padding:0 16px;cursor:pointer;
      display:inline-flex;align-items:center;justify-content:center;gap:7px;
      font:600 11px/1 "Inter","Microsoft YaHei",inherit;
      letter-spacing:.14em;text-transform:uppercase;
      color:var(--tx-hi);
      background:linear-gradient(180deg,rgba(139,92,246,.16),rgba(139,92,246,.04));
      border:1px solid rgba(139,92,246,.42);
      clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),
                        calc(100% - 8px) 100%,0 100%,0 8px);
      transition:all var(--t-fast) var(--ease);
    }
    button:hover{background:rgba(139,92,246,.28);box-shadow:0 0 18px rgba(139,92,246,.45)}
    button:active{transform:translateY(1px)}
    button.primary{
      background:linear-gradient(180deg,var(--accent-blue),var(--accent-ultra));
      border-color:rgba(139,92,246,.8);
      box-shadow:0 0 20px rgba(42,108,255,.45);
    }
    button.primary:hover{box-shadow:0 0 26px rgba(42,108,255,.7)}
    button.stop{
      background:linear-gradient(180deg,rgba(255,45,75,.22),rgba(255,45,75,.05));
      border-color:rgba(255,45,75,.48);color:#FFD2DA;
    }
    button.stop:hover{background:rgba(255,45,75,.32);box-shadow:0 0 18px rgba(255,45,75,.35)}

    /* ---------- 状态条 ---------- */
    .status{
      margin-top:15px;padding-top:12px;
      border-top:1px solid var(--line-soft);
      font-size:12px;color:var(--tx-dim);min-height:32px;line-height:1.6;
    }
    .status.live{color:var(--accent-mint)}
    /* 分区状态的「已暂停」：琥珀色斜体，一眼看出是暂停不是停止 */
    .status.live.paused{color:#FFB347;font-style:italic}
    /* 就绪态（两段式流程的「参数已装填，等触发键」）：青色。
       和运行中的薄荷绿、暂停的琥珀色三者必须能一眼区分——
       主人 2026-10-15 的 bug 就是「就绪」和「已启动」看起来没区别。 */
    .status.armed{color:var(--accent)}
    .kpi .n.armed{color:var(--accent);font-size:16px;padding-top:3px}
    /* 【分区暂停·2026-10-16】触发键切的是暂停，KPI 第三个态：
       运行=薄荷绿、就绪=青、暂停=琥珀。以前只有两个态，
       「暂停中」显示成和「运行中」一样的绿色，看不出停了。 */
    .kpi .n.pause{color:#ffa73a;font-size:16px;padding-top:3px}
    .card.armed{border-color:rgba(139,92,246,.45)}
    .card.k.armed{border-color:rgba(255,154,60,.45)}
    /* 提权警告横幅：目标窗口权限更高、注入被 UIPI 拦截时显示，
       带「以管理员身份重启」按钮（调 relaunch_as_admin） */
    .elevban{
      display:none;margin-top:12px;
      border:1px solid rgba(255,154,60,.5);background:rgba(255,154,60,.10);
      color:var(--spark);padding:10px 14px;font-size:12px;line-height:1.7;
      clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),
                        calc(100% - 8px) 100%,0 100%,0 8px);
    }
    .elevban.show{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
    .elevban .eb-txt{flex:1 1 260px}
    .elevban button{
      flex:0 0 auto;cursor:pointer;font-size:12px;padding:6px 14px;
      background:rgba(255,154,60,.18);border:1px solid rgba(255,154,60,.55);
      color:var(--spark);border-radius:2px;
    }
    .elevban button:hover{background:rgba(255,154,60,.30)}
    .warn{
      margin-top:14px;border:1px solid rgba(255,154,60,.38);
      background:rgba(255,154,60,.08);color:var(--spark);
      padding:10px 13px;font-size:12px;
      clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),
                        calc(100% - 8px) 100%,0 100%,0 8px);
    }
    @media (prefers-reduced-motion:reduce){ *{animation:none!important} }
  /* ---------- 配置面板（导入/导出/方案） ---------- */
  /* 【默认必须隐藏】这个面板只在点齿轮按钮时出现。之前漏写 display:none，
     div 按默认 block 渲染 → 程序一启动就挂在右上角；而 toggleCfg() 读的是
     **内联** style.display（初始是空串，不是 "block"）→ 第一次点算出
     「当前没开」→ 又设成 block（毫无变化）→ 第二次点才真隐藏。
     现在默认隐藏 + 用 class 切换，两种错都不会再犯。 */
  .cfg-panel{ position:fixed; top:34px; right:10px; z-index:50; width:300px;
    background:var(--panel); border:1px solid var(--line);
    border-radius:var(--chamfer); padding:12px;
    box-shadow:0 10px 40px rgba(0,0,0,.5); display:none; }
  .cfg-panel.open{ display:block; }
  .cfg-sec{ margin-bottom:12px; }
  .cfg-sec:last-of-type{ margin-bottom:0; }
  .cfg-sec-t{ font-size:11px; letter-spacing:1px; color:var(--accent-mint);
    text-transform:uppercase; margin-bottom:7px; }
  .cfg-row{ display:flex; gap:6px; flex-wrap:wrap; }
  .cfg-btn{ flex:1; min-width:0; padding:7px 8px; font-size:12px; cursor:pointer;
    color:var(--tx-hi); background:rgba(139,92,246,.10);
    border:1px solid var(--line); border-radius:7px; }
  .cfg-btn:hover{ background:rgba(139,92,246,.20); }
  .cfg-btn.danger{ color:#ff9a9a; border-color:rgba(255,120,120,.35);
    background:rgba(255,120,120,.10); }
  .cfg-sel{ width:100%; margin-bottom:7px; padding:6px; font-size:12px;
    color:var(--tx-hi); background:var(--bg-abyss);
    border:1px solid var(--line); border-radius:7px; box-sizing:border-box; }
  .cfg-hint{ font-size:10.5px; line-height:1.5; color:var(--tx-dim); margin-top:6px; }
  .cfg-msg{ font-size:11px; margin-top:4px; min-height:14px; color:var(--accent-mint); }
  .cfg-msg.err{ color:#ff9a9a; }
</style>
</head>
<body>
<div class="scanbeam"></div>

<!-- 边框缩放热区：无边框窗口没有系统边框，边缘几像素拖不动。
     每条热区mousedown 时把缩放循环交还 Windows（见 title_resize），
     这也是唯一能保住「拖到屏幕边缘弹出分屏」的做法。 -->
<div class="rz rz-t"  data-edge="t"  onpointerdown="rzDown(event,'t')"></div>
<div class="rz rz-b"  data-edge="b"  onpointerdown="rzDown(event,'b')"></div>
<div class="rz rz-l"  data-edge="l"  onpointerdown="rzDown(event,'l')"></div>
<div class="rz rz-r"  data-edge="r"  onpointerdown="rzDown(event,'r')"></div>
<div class="rz rz-tl" data-edge="tl" onpointerdown="rzDown(event,'tl')"></div>
<div class="rz rz-tr" data-edge="tr" onpointerdown="rzDown(event,'tr')"></div>
<div class="rz rz-bl" data-edge="bl" onpointerdown="rzDown(event,'bl')"></div>
<div class="rz rz-br" data-edge="br" onpointerdown="rzDown(event,'br')"></div>

<!-- 自绘标题栏：无边框窗口没有系统标题栏，
     拖动/最小化/最大化/关闭都在这里做。
     id="titlebar" 的 mousedown/move/up 交给 winDrag* 系列，
     实际拖动走 Win32 的 WM_NCLBUTTONDOWN（见 win_drag_move），
     这样贴边分屏、拖出屏幕这些系统行为才能正常工作。 -->
<div class="titlebar" id="titlebar">
  <div class="tb-left">
    <span class="tb-logo"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><defs><linearGradient id="pawG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#DEB0FF"/><stop offset="1" stop-color="#7A24D2"/></linearGradient></defs><path d="M11.2 1 L13.8 9.6 L11.9 23 L9.5 8.8 Z" fill="url(#pawG)"/><path d="M9.9 7.6 L2.9 16.6 L9.1 12.1 Z" fill="url(#pawG)" opacity=".92"/><path d="M14 7.1 L21 15.6 L14.7 11.1 Z" fill="url(#pawG)" opacity=".92"/></svg></span>
    <span class="tb-title">爪Pro</span>
    <span class="tb-ver">PAW PRO · 键鼠自动化</span>
  </div>
  <div class="tb-right">
    <button class="tb-btn" id="cfgBtn" title="配置导入 / 导出 / 方案" onclick="toggleCfg()">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
    </button>
    <button class="tb-btn" id="btnMin"  title="最小化" onclick="winMin()">
      <svg width="11" height="11" viewBox="0 0 11 11"><rect x="0" y="5" width="11" height="1.2" fill="currentColor"/></svg>
    </button>
    <button class="tb-btn" id="btnMax"  title="最大化 / 还原" onclick="winMax()">
      <svg width="11" height="11" viewBox="0 0 11 11">
        <rect x="0.5" y="0.5" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.2"/>
      </svg>
    </button>
    <button class="tb-btn tb-close" title="关闭" onclick="winClose()">
      <svg width="11" height="11" viewBox="0 0 11 11">
        <path d="M0 0 L11 11 M11 0 L0 11" stroke="currentColor" stroke-width="1.2" fill="none"/>
      </svg>
    </button>
  </div>
</div>

<div class="cfg-panel" id="cfgPanel">
  <div class="cfg-sec">
    <div class="cfg-sec-t">导出 / 分享</div>
    <div class="cfg-row">
      <button class="cfg-btn" onclick="exportFile()">导出到文件</button>
      <button class="cfg-btn" onclick="copyClip()">复制到剪贴板</button>
    </div>
    <div class="cfg-hint">导出到程序所在目录（文件名带时间戳）；或复制文本直接发给别人。</div>
  </div>
  <div class="cfg-sec">
    <div class="cfg-sec-t">导入</div>
    <input type="file" id="cfgFile" accept=".json,application/json" style="display:none" onchange="importFile(this)">
    <div class="cfg-row">
      <button class="cfg-btn" onclick="document.getElementById('cfgFile').click()">从文件导入</button>
      <button class="cfg-btn" onclick="pasteClip()">从剪贴板粘贴</button>
    </div>
    <div class="cfg-hint">导入前自动备份当前配置；触发键绑定需在本地重设（设备相关）。</div>
  </div>
  <div class="cfg-sec">
    <div class="cfg-sec-t">配置方案</div>
    <input type="text" id="cfgProfileName" class="cfg-sel" placeholder="方案名">
    <select id="cfgProfileSel" class="cfg-sel"><option value="">— 选择方案 —</option></select>
    <div class="cfg-row">
      <button class="cfg-btn" onclick="saveProfile()">存为方案</button>
      <button class="cfg-btn" onclick="loadProfile()">加载</button>
      <button class="cfg-btn danger" onclick="delProfile()">删除</button>
    </div>
    <div class="cfg-hint">方案保存在程序目录的 profiles/ 文件夹，可跨机器拷贝分享。</div>
  </div>
  <div class="cfg-msg" id="cfgMsg"></div>
</div>

<div class="wrap">

  <!-- Hero：名称展示区 -->
  <div class="panel hero hud">
    <i></i><i></i><i></i><i></i>
    <div class="topbar">
      <div>
        <h1>爪<span class="thin"> Pro</span></h1>
        <div class="sub">Paw Pro · 键鼠自动化</div>
      </div>
      <div class="right">
        <span class="tag" id="badge">SYS READY</span>
        <button class="ghostbtn" id="pauseBtn" onclick="togglePause()"
                title="全局暂停：定住所有注入动作，参数保留，再按一次恢复">暂停</button>
      </div>
    </div>
    <div class="gmsg" id="gMsg">就绪</div>
    <!-- 提权警告横幅：默认隐藏。poll 发现后端 elev_warn=True 时显示
         （前台是提权窗口——如管理员身份运行的游戏——而本进程是普通权限，
         注入被 UIPI 静默拦截：界面显示"已启动"，目标程序却收不到）。
         附「以管理员身份重启」一键入口，见 relaunchAdmin()。 -->
    <div class="elevban" id="elevBan">
      <span class="eb-txt">⚠ 检测到前台窗口权限更高（例如以管理员身份运行的游戏），
        本工具是普通权限——<b>点击和按键会被系统静默拦截</b>：
        界面显示“已启动”，目标程序里却没有任何反应。
        请以管理员身份运行本工具（点右侧按钮，弹一次 UAC 即可）。</span>
      <button id="elevBtn" onclick="relaunchAdmin()">以管理员身份重启</button>
    </div>
    <div class="hero-grid">
      <div class="kpi">
        <div class="n" id="kMouse">就绪</div>
        <div class="t">鼠标</div>
      </div>
      <div class="kpi">
        <div class="n" id="kKey">就绪</div>
        <div class="t">键盘</div>
      </div>
      <div class="kpi">
        <div class="n" id="kClick">0</div>
        <div class="t">点击</div>
      </div>
      <div class="kpi">
        <div class="n" id="kTime">0.0s</div>
        <div class="t">时长</div>
      </div>
    </div>
  </div>

  <!-- 一句话流程（v31：替掉原来的整块「功能说明」） -->
  <div class="flow hud">
    <span class="fs"><b>①</b> 装好参数</span>
    <span class="fa">→</span>
    <span class="fs"><b>②</b> 按触发键开始 / 暂停</span>
    <span class="fa">→</span>
    <span class="fs"><b>③</b> 回来看「停止」</span>
    <span class="fnote">参数只在点「就绪」时装填；注入只发给当前前台窗口，所以要点完再切到目标程序。</span>
  </div>

  <!-- 卡片网格：左鼠标 / 右键盘 -->
  <div class="grid">

    <!-- 鼠标区 -->
    <div class="panel card hud" id="cardMouse">
      <i></i><i></i><i></i><i></i>
      <h2><span class="dot"></span>鼠标区</h2>
      <div class="hint">点「就绪」只装参不会启动；按 <b id="hintMouseKey">中键</b> 才开始 / 暂停，「停止鼠标」才结束。</div>

      <div class="row">
        <label>鼠标按键</label>
        <select id="mBtn"></select>
      </div>
      <div class="row">
        <label>动作</label>
        <select id="mAct">
          <option value="click">点击（连点）</option>
          <option value="double">双击（每轮点两下）</option>
          <option value="triple">三击（每轮点三下）</option>
          <option value="hold">长按（按住不放）</option>
          <option value="jiggle">抖器（只挪不点，防息屏）</option>
        </select>
      </div>
      <div class="row" id="mJigRow" style="display:none">
        <label>挪动间隔(秒)</label>
        <input id="mJig" type="number" min="1" max="3600" step="1" value="30" placeholder="每隔几秒挪 1 像素">
      </div>
      <div class="row">
        <label>点击次数</label>
        <input id="mCount" type="number" min="0" value="0" placeholder="0=无限">
      </div>
      <div class="row">
        <label>持续时间(秒)</label>
        <input id="mDur" type="number" min="0" step="0.5" value="0" placeholder="0=无限">
      </div>
      <div class="row">
        <label>CPS</label>
        <input id="mCps" type="number" min="0.5" step="0.5" value="10">
      </div>
      <div class="row">
        <label>间隔浮动(±ms)</label>
        <input id="mJitter" type="number" min="0" step="1" value="0" placeholder="0=节奏恒定">
      </div>
      <div class="row">
        <label>点击位置</label>
        <select id="mMode" onchange="syncMouseRows()">
          <option value="cursor">跟随光标</option>
          <option value="fixed">固定坐标</option>
          <option value="multi">多点轮询（依次点）</option>
          <option value="box">框内随机</option>
        </select>
      </div>
      <div class="row" id="mPosRow" style="display:none">
        <label>坐标 X,Y</label>
        <div class="poswrap">
          <input id="mPos" placeholder="如 960,540">
          <button type="button" onclick="recordPos()">取当前</button>
        </div>
      </div>
      <div class="row" id="mPointsRow" style="display:none">
        <label>坐标列表</label>
        <div class="poswrap col">
          <textarea id="mPoints" rows="3" spellcheck="false"
                    placeholder="每行一个 x,y&#10;多点轮询：2 个以上依次点&#10;框内随机：正好 2 个（左上、右下）"></textarea>
          <div class="btnrow tight">
            <button type="button" onclick="recordPos()">追加当前坐标</button>
            <button type="button" onclick="clearPoints()">清空</button>
          </div>
        </div>
      </div>
      <div class="row">
        <label>启动倒计时(秒)</label>
        <input id="mCd" type="number" min="0" max="600" step="1" value="0" placeholder="0=立即开始">
      </div>
      <div class="row">
        <label>完成后动作</label>
        <select id="mDone">
          <option value="none">不动作</option>
          <option value="sound">提示音</option>
          <option value="lock">锁屏</option>
          <option value="shutdown">关机（30 秒倒计时可取消）</option>
        </select>
      </div>
      <div class="row">
        <label>启动后最小化</label>
        <label class="chk"><input type="checkbox" id="mHide" checked> 启动后自动隐藏仪表盘，把前台让给目标程序</label>
      </div>
      <div class="row">
        <label>关闭到托盘</label>
        <label class="chk"><input type="checkbox" id="mTray"> 点右上角 X 时缩到托盘继续跑（不勾=直接退出）</label>
      </div>

      <div class="btnrow">
        <button class="primary" onclick="startMouse()">① 就绪</button>
        <button class="stop" onclick="stopMouse()">停止鼠标</button>
      </div>
      <div class="row" id="doneCancelRow" style="display:none">
        <label>收尾动作</label>
        <button type="button" onclick="cancelDone()">取消待执行动作</button>
      </div>
      <div class="hint tiny">就绪＝只装参不启动；触发键开始 / 暂停；<b>「停止鼠标」才结束</b>。</div>
      <div class="hint tiny">双击/三击按<b>轮次</b>算；浮动＝±随机；完成后动作只在<b>自然用完</b>时触发。</div>

      <div class="sublabel">自定义鼠标键（游戏鼠标的额外按键）</div>
      <div class="btnrow">
        <button id="armBtn" onclick="armMouse()">① 开始识别</button>
        <button id="addBtn" onclick="addMouse()">② 完成添加</button>
      </div>
      <div class="hint tiny">点「开始识别」后 8 秒内按一下侧键，再点「完成添加」。</div>

      <div class="status" id="mStatus">鼠标区未启动。</div>
    </div>

    <!-- 键盘区 -->
    <div class="panel card k hud" id="cardKey">
      <i></i><i></i><i></i><i></i>
      <h2><span class="dot"></span>键盘区</h2>
      <div class="hint">长按目标键到阈值即锁定；触发键 <b id="hintKeyKey">F6</b> 是暂停，会同时松手。</div>

      <div class="row">
        <label>生效按键</label>
        <div class="capwrap" id="keyCapWrap">
          <input id="kKeys" type="text" value="w" placeholder="点右侧按钮直接按键">
          <button class="capbtn" id="capKeysBtn" onclick="armKeyCapture()"
                  style="flex:0 0 auto;min-width:104px">按一下录入</button>
          <button class="capbtn" onclick="delKeyChip()"
                  style="flex:0 0 auto;min-width:88px">删除末键</button>
        </div>
      </div>
      <div class="hint tiny" id="keyCapHint">
        点「按一下录入」再按目标键，可加 Ctrl/Alt/Shift；筹码点 × 单独删。
      </div>
      <div class="row">
        <label>长按多少秒锁定</label>
        <input id="kThr" type="number" min="0.1" step="0.1" value="2">
      </div>
      <div class="row">
        <label>重复频率(次/秒)</label>
        <input id="kHz" type="number" min="1" value="30">
      </div>
      <div class="row">
        <label>锁定行为</label>
        <select id="kMode">
          <option value="true">一直按着</option>
          <option value="false">反复点击</option>
        </select>
      </div>
      <div class="row" id="gapRow" style="display:none">
        <label>按下时长(毫秒)</label>
        <input id="kGap" type="number" min="0" step="1" value="20">
      </div>
      <div class="hint tiny" id="gapHint">
        「一直按着」只注入按下、不注入抬起，重复频率即按住状态的刷新频率。
      </div>

      <div class="btnrow">
        <button class="primary" onclick="startKey()">① 就绪</button>
        <button class="stop" onclick="stopKey()">停止</button>
      </div>
      <div class="hint tiny">触发键启动 / <b>暂停（会松手）</b>；「停止」才结束。</div>
      <div class="status" id="kStatus">键盘区未启动。</div>
      <div id="warnBox"></div>
    </div>

  </div>

  <!-- 触发方式区：解决 F5 被游戏/远程控制软件占用的问题 -->
  <div class="panel hud trig" id="cardTrig">
    <i></i><i></i><i></i><i></i>
    <h2><span class="dot"></span>触发方式<span class="keycap mint" id="hkNoteKey">推荐</span></h2>
    <div class="hint">
      系统热键容易被游戏抢，鼠标键几乎不冲突；<b>两种可同时绑，按哪个都行</b>。
    </div>

    <div class="trig-grid">
      <div class="trig-cell">
        <div class="tc-title">鼠标连点启停</div>
        <div class="tc-body">
          <span class="tc-kbd" id="hkMouse">未绑定</span>
          <span class="tc-hint">当前生效的触发键</span>
        </div>
      </div>
      <div class="trig-cell">
        <div class="tc-title">键盘锁定启停</div>
        <div class="tc-body">
          <span class="tc-kbd amber" id="hkKey2">未绑定</span>
          <span class="tc-hint">当前生效的触发键</span>
        </div>
      </div>
      <div class="trig-cell">
        <div class="tc-title">全局暂停 / 恢复</div>
        <div class="tc-body">
          <span class="tc-kbd" id="hkPause">Pause</span>
          <span class="tc-hint">游戏中定住所有动作，再按恢复</span>
        </div>
      </div>
      <!-- v34：宏自己的启停键。回放一启动窗口就最小化了，
           没有系统级热键就没法中途暂停 —— 和连点/键盘锁定同构。 -->
      <div class="trig-cell">
        <div class="tc-title">宏录制 / 回放启停</div>
        <div class="tc-body">
          <span class="tc-kbd" id="hkMacro">未绑定</span>
          <span class="tc-hint">录制中收尾 · 回放中暂停 / 继续</span>
        </div>
      </div>
    </div>

    <div class="trig-bind">
      <div class="tb-label">绑鼠标键（按一下就绑）：</div>
      <select id="hotKeyAct">
        <option value="mouse">→启停鼠标连点</option>
        <option value="key">→ 启停键盘锁定</option>
        <option value="macro">→ 启停宏回放</option>
      </select>
      <button class="primary" id="capTrigBtn" onclick="armTriggerCapture()">
        按下要绑的键
      </button>
      <button onclick="clearTrigKey()">清除</button>
    </div>
    <div class="trig-bind">
      <div class="tb-label">或改系统热键：</div>
      <select id="hotKeySysAct">
        <option value="mouse">→ 鼠标连点热键</option>
        <option value="key">→ 键盘锁定热键</option>
        <option value="macro">→ 宏启停热键</option>
      </select>
      <button class="capbtn" id="capSysBtn" onclick="armSysCapture()"
              style="flex:0 0 auto;min-width:104px">按一下录入</button>
      <button onclick="clearSysHotkey()">恢复默认</button>
    </div>
    <div class="hint tiny" id="hkNote"></div>
    <div class="rawdiag" id="rawDiag" title="原始输入链路自检。按一下鼠标键，消息数应当增加。">
      <span class="rd-item">监听：<b id="rdMode">检测中…</b></span>
      <span class="rd-item">消息数：<b id="rdMsg">0</b></span>
      <span class="rd-item">最近按键：<b id="rdLast">—</b></span>
      <span class="rd-hint" id="rdHint"></span>
    </div>
  </div>

  <!-- 宏录制区（v27 / S2）：独立整宽分区 -->
  <div class="panel hud macro" id="cardMacro">
    <i></i><i></i><i></i><i></i>
    <h2><span class="dot"></span>宏录制区</h2>
    <div class="hint">
      录制 →（首次点击为起点）→ 操作 → <b>Pause 结束</b> → 点步骤改参数 → 回放。
    </div>

    <div class="macro-grid">
      <!-- ① 录制 -->
      <div class="trig-cell">
        <div class="tc-title">① 录制</div>
        <div class="tc-body" style="display:block">
          <div class="mc-inline">
            <button class="capbtn" id="macRecBtn" onclick="macToggleRecord()">开始录制</button>
            <label for="macStart" style="margin-left:2px">起点</label>
            <select id="macStart" title="起点决定「哪一刻算第 0 步」">
              <option value="first_click" selected>首次点击开始</option>
              <option value="countdown">3 秒后开始</option>
              <option value="now">立即开始</option>
            </select>
          </div>
          <label class="mc-check" for="macIgnoreInj">
            <input type="checkbox" id="macIgnoreInj" checked>
            忽略程序注入的按键
          </label>
          <div class="status" id="macRecStatus">未录制。</div>
        </div>
      </div>

      <!-- ② 步骤（v30：可编辑的节点列表） -->
      <div class="trig-cell">
        <div class="tc-title">② 步骤（点一行可编辑）</div>
        <div class="tc-body" style="display:block">
          <div class="mc-stat">
            <b id="macCount">0</b> 个步骤 · 约 <b id="macSecs">0.0</b> 秒 ·
            点击 <b id="macClicks">0</b> · 按键 <b id="macKeys">0</b>
          </div>
          <div class="mc-steps" id="macSteps">
            <div class="mc-empty">还没有步骤。录一段试试。</div>
          </div>
          <div class="mc-steps-more" id="macStepsMore"></div>
          <div class="mc-inline" style="margin-top:6px">
            <button onclick="macStepInsert()">＋ 插入等待</button>
            <button onclick="macDiscard()">清空</button>
          </div>
        </div>
      </div>

      <!-- ③ 回放 -->
      <div class="trig-cell">
        <div class="tc-title">③ 回放</div>
        <div class="tc-body" style="display:block">
          <div class="mc-inline">
            <label for="macSpeed">倍速</label>
            <select id="macSpeed">
              <option value="0.5">0.5x 慢放</option>
              <option value="1" selected>1x 原速</option>
              <option value="1.5">1.5x</option>
              <option value="2">2x</option>
              <option value="4">4x 快进</option>
            </select>
            <label for="macRepeat">次数</label>
            <input id="macRepeat" type="number" min="1" max="9999" step="1" value="1">
          </div>
          <div class="mc-inline">
            <button class="capbtn" id="macPlayBtn" onclick="macPlay()">开始回放</button>
            <button id="macStopPlayBtn" onclick="macStopPlay()">停止回放</button>
          </div>
          <!-- v34：宏的启停键。回放一启动窗口就最小化让位，
               没有热键就只能切回来点按钮，等于回放期间没法干预。 -->
          <div class="status" id="macTrigHint" style="margin-top:6px">
            启停键 <b id="macTrigKey">F7</b>：录制中收尾 · 回放中暂停 / 继续
          </div>
          <div class="status" id="macPlayStatus">未回放。</div>
        </div>
      </div>

      <!-- ④ 保存 / 载入 -->
      <div class="trig-cell">
        <div class="tc-title">④ 保存 / 载入</div>
        <div class="tc-body" style="display:block">
          <div class="mc-inline">
            <input id="macName" type="text" placeholder="宏名，如 每日签到"
                   spellcheck="false" maxlength="64">
            <button onclick="macSave()">保存</button>
          </div>
          <div class="mc-inline">
            <select id="macList"></select>
            <button onclick="macLoad()">载入</button>
            <button onclick="macDelete()">删除</button>
          </div>
          <div class="status" id="macSaveStatus">宏存在设置目录的 macros/ 下。</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 节点设置弹窗（v30）：照参考流程编辑器的形态——点列表里一行就打开它。
     不用 window.prompt/confirm：pywebview 里有的平台会直接返回 undefined，
     等于永远取消（删除确认那里已经踩过同一个坑）。 -->
<div class="mc-modal" id="macModal">
  <div class="mc-dialog">
    <div class="mc-dh">
      <b>节点设置</b>
      <span id="mcNodeHead">当前节点：—</span>
      <button class="mc-x" onclick="macCloseModal()" title="关闭">✕</button>
    </div>

    <div class="mc-field">
      <label for="mcName">节点名称</label>
      <input id="mcName" type="text" maxlength="32" spellcheck="false">
    </div>

    <div class="mc-two" id="mcXYRow">
      <div class="mc-field">
        <label for="mcX">X 轴</label>
        <input id="mcX" type="number" step="1">
      </div>
      <div class="mc-field">
        <label for="mcY">Y 轴</label>
        <input id="mcY" type="number" step="1">
      </div>
    </div>

    <div class="mc-field">
      <label for="mcDelayType">延时类型</label>
      <select id="mcDelayType">
        <option value="fixed">固定延时</option>
        <option value="random">随机延时</option>
      </select>
    </div>

    <div class="mc-two">
      <div class="mc-field">
        <label for="mcDelay" id="mcDelayLabel">延时（毫秒）</label>
        <input id="mcDelay" type="number" min="0" step="1" value="0">
      </div>
      <div class="mc-field" id="mcDelay2Wrap" style="display:none">
        <label for="mcDelay2">到（毫秒）</label>
        <input id="mcDelay2" type="number" min="0" step="1" value="0">
      </div>
    </div>
    <div class="mc-tip">延时 = <b>这一步做完后等多久再做下一步</b>。1000 毫秒 = 1 秒。</div>

    <div class="mc-field">
      <label for="mcAfter">执行完成后</label>
      <select id="mcAfter">
        <option value="next">执行下一步</option>
        <option value="end">结束宏</option>
      </select>
    </div>

    <div class="mc-dfoot">
      <button class="mc-danger" onclick="macStepDel()">删除</button>
      <button onclick="macStepDup()">复制</button>
      <button onclick="macStepMove(-1)">上移</button>
      <button onclick="macStepMove(1)">下移</button>
      <button class="primary" onclick="macStepSave()">确定</button>
    </div>
  </div>
</div>

<script>
  const $ = id => document.getElementById(id);
  function q(p){ return document.querySelector(p); }

  function toast(sel, msg){
    const el = $(sel);
    el.innerHTML = msg;
    el.className = "status live";
    setTimeout(()=>{ el.className="status"; }, 1800);
  }

  /* ---------- 宏录制 / 回放（v27 / S2） ----------
     进度靠轮询：pywebview 只有「前端问、后端答」，没有后端主动推，
     所以录制中每 700ms 取一次摘要（只回条数/秒数/预览，不回事件本体，
     几千条事件塞进 webview 会直接卡死界面）。 */
  let macRecording = false, macPlaying = false;
  let macDelArmed = 0;              // 删除的两步确认计时
  let macStepVer = -1;              // 后端步骤版本（变了才重画列表）
  let macStepCache = [], macStepTotal = 0, macEditIdx = -1;

  async function macCall(name, ...args){
    try{
      return await window.pywebview.api[name](...args);
    }catch(e){
      // 【不空吞】以前这类 catch(e){} 让「点了没反应」查了三层才定位到
      // （规则 36）。这里直接把异常写到状态行。
      macSetStatus("macRecStatus", "调用失败：" + e, true);
      return {ok:false, msg:String(e)};
    }
  }

  function macSetStatus(id, msg, live){
    const el = $(id);
    if(!el) return;
    el.textContent = msg || "";
    el.className = live ? "status live" : "status";
  }

  function macFillList(names, pick){
    const sel = $("macList");
    const cur = pick || sel.value;
    sel.innerHTML = "";
    if(!names || !names.length){
      const o = document.createElement("option");
      o.value = ""; o.textContent = "（还没有保存的宏）"; o.disabled = true;
      o.selected = true;
      sel.appendChild(o);
      return;
    }
    names.forEach(n => {
      const o = document.createElement("option");
      o.value = n; o.textContent = n;
      sel.appendChild(o);
    });
    if(cur && names.indexOf(cur) >= 0) sel.value = cur;
  }

  function macSetSpeed(v){
    // select 的 value 是字符串，后端回的是 1.0 这样的 float；
    // 直接赋值 "1.0" 匹配不上 "1" 这个 option，会选中空项。
    const want = Number(v);
    let best = null, bestDiff = 1e9;
    for(const o of $("macSpeed").options){
      const d = Math.abs(Number(o.value) - want);
      if(d < bestDiff){ bestDiff = d; best = o.value; }
    }
    if(best !== null) $("macSpeed").value = best;
  }

  async function macRefreshList(pick){
    const r = await macCall("macro_list");
    if(r && r.ok) macFillList(r.names, pick);
  }

  async function macPoll(){
    if(!window.pywebview || !window.pywebview.api) return;
    let d;
    try{ d = await window.pywebview.api.macro_status(); }
    catch(e){ return; }
    if(!d || !d.ok) return;

    macRecording = !!d.recording;
    macPlaying = !!d.playing;
    $("macCount").textContent = d.count;
    $("macSecs").textContent = Number(d.secs || 0).toFixed(1);
    $("macClicks").textContent = d.clicks || 0;
    $("macKeys").textContent = d.keys || 0;

    $("macRecBtn").textContent = d.recording ? "停止录制" : "开始录制";
    $("macPlayBtn").disabled = d.recording || d.playing;
    $("macStopPlayBtn").disabled = !d.playing;

    if(d.recording){
      // v30：起点模式要能看出来。否则用户点完「开始录制」看界面没动静，
      // 会以为坏了——其实它在等第一次点击。
      const head = d.armed
        ? ("等起点… 已等 " + Number(d.waited || 0).toFixed(1)
           + " 秒（第一次点击即开始；按 Pause 取消）")
        : ("录制中… 已记 " + d.count + " 条动作 / "
           + Number(d.secs || 0).toFixed(1) + " 秒（按 Pause 结束）");
      macSetStatus("macRecStatus", d.overflow || head, true);
    }else if(d.overflow){
      macSetStatus("macRecStatus", d.overflow, true);
    }
    if(d.playing){
      // v34：暂停和卡顿必须分得开。回放中按启停键定住后，
      // 界面还写「回放中」用户会以为程序死了。
      macSetStatus("macPlayStatus",
        d.paused
          ? ("已暂停 · 再按 " + (d.trig || "启停键") + " 继续")
          : ("回放中……按 " + (d.trig || "启停键") + " 暂停"),
        true);
    }
    // 启停键名始终显示真键（降级后可能是 Ctrl+F7）：
    // 写死 F7 会出现「照提示按了没反应」的老毛病（2026-10-15 实测过）。
    if(d.trig) $("macTrigKey").textContent = d.trig;
    if(d.name) $("macName").value = d.name;

    // 步骤列表只在版本变了才重画：700ms 一次全量重绘会把用户
    // 正指着的那一行刷走（表现是「列表点不中」）。
    if((d.step_ver | 0) !== macStepVer) macSyncSteps();
  }

  const MAC_KIND_CN = {click:"点击", down:"按住", up:"松开", key:"按键",
                       wheel:"滚轮", hwheel:"横滚", move:"移动", wait:"等待"};

  function macEsc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
      {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  async function macSyncSteps(){
    let d;
    try{ d = await window.pywebview.api.macro_steps(0, 300); }
    catch(e){ macSetStatus("macRecStatus", "步骤读取失败：" + e, true); return; }
    if(!d || !d.ok) return;
    macStepVer = d.step_ver | 0;
    macStepCache = d.items || [];
    macStepTotal = d.total || 0;
    const box = $("macSteps");
    if(!macStepTotal){
      box.innerHTML = '<div class="mc-empty">还没有步骤。录一段试试。</div>';
      $("macStepsMore").textContent = "";
      return;
    }
    box.innerHTML = macStepCache.map(it => {
      const delay = Number(it.delay2 || 0) > Number(it.delay || 0)
        ? (it.delay + "~" + it.delay2 + "ms") : (it.delay + "ms");
      const endMark = it.after === "end" ? " ⏹" : "";
      return '<div class="mc-step' + (it.i === macEditIdx ? " sel" : "")
        + '" onclick="macEditStep(' + it.i + ')">'
        + '<span class="ms-i">' + it.i + '</span>'
        + '<span class="ms-l">' + macEsc(it.label || MAC_KIND_CN[it.k] || it.k)
        + endMark + '</span>'
        + '<span class="ms-d">' + macEsc(delay) + '</span></div>';
    }).join("");
    $("macStepsMore").textContent = macStepTotal > macStepCache.length
      ? ("还有 " + (macStepTotal - macStepCache.length) + " 步未显示（列表只画前 300 条）")
      : "";
  }

  async function macEditStep(i){
    if(macRecording || macPlaying){
      macSetStatus("macRecStatus", "录制 / 回放中不能编辑步骤", true);
      return;
    }
    let it = macStepCache.find(x => x.i === i);
    if(!it){ await macSyncSteps(); it = macStepCache.find(x => x.i === i); }
    if(!it) return;
    macEditIdx = i;
    $("mcNodeHead").textContent = "当前节点：" + i + "（共 " + macStepTotal + " 步）";
    $("mcName").value = it.name || "";
    const hasXY = (it.k === "click" || it.k === "down" || it.k === "move");
    $("mcXYRow").style.display = hasXY ? "grid" : "none";
    if(hasXY){
      $("mcX").value = (it.x != null ? it.x : 0);
      $("mcY").value = (it.y != null ? it.y : 0);
    }
    const rand = Number(it.delay2 || 0) > Number(it.delay || 0);
    $("mcDelayType").value = rand ? "random" : "fixed";
    $("mcDelay2Wrap").style.display = rand ? "flex" : "none";
    $("mcDelay").value = it.delay || 0;
    $("mcDelay2").value = it.delay2 || 0;
    $("mcAfter").value = it.after === "end" ? "end" : "next";
    $("macModal").classList.add("show");
    macSyncSteps();
  }

  function macCloseModal(){
    $("macModal").classList.remove("show");
    macEditIdx = -1;
    macSyncSteps();
  }

  async function macStepSave(){
    if(macEditIdx < 0){ macCloseModal(); return; }
    const patch = {
      name: ($("mcName").value || "").trim(),
      delay: parseInt($("mcDelay").value, 10) || 0,
      delay2: $("mcDelayType").value === "random"
              ? (parseInt($("mcDelay2").value, 10) || 0) : 0,
      after: $("mcAfter").value,
    };
    if($("mcXYRow").style.display !== "none"){
      patch.x = parseInt($("mcX").value, 10) || 0;
      patch.y = parseInt($("mcY").value, 10) || 0;
    }
    const r = await macCall("macro_step_set", macEditIdx, patch);
    macSetStatus("macRecStatus", r.msg, true);
    if(r.ok){ macCloseModal(); return; }
  }

  async function macStepDel(){
    if(macEditIdx < 0) return;
    const r = await macCall("macro_step_del", macEditIdx);
    macSetStatus("macRecStatus", r.msg, true);
    if(r.ok) macCloseModal();
  }

  async function macStepDup(){
    if(macEditIdx < 0) return;
    const r = await macCall("macro_step_dup", macEditIdx);
    macSetStatus("macRecStatus", r.msg, true);
    if(r.ok) macCloseModal();
  }

  async function macStepMove(delta){
    if(macEditIdx < 0) return;
    const r = await macCall("macro_step_move", macEditIdx, delta);
    macSetStatus("macRecStatus", r.msg, true);
    if(r.ok) macCloseModal();
  }

  async function macStepInsert(){
    const r = await macCall("macro_step_insert", macStepTotal, "wait", 500);
    macSetStatus("macRecStatus", r.msg, true);
    if(r.ok){ macStepVer = -1; await macSyncSteps(); macEditStep(r.index); }
  }

  // 弹窗点遮罩关闭（点对话框本身不关）
  $("macModal").addEventListener("click", (e)=>{
    if(e.target && e.target.id === "macModal") macCloseModal();
  });
  $("mcDelayType").addEventListener("change", ()=>{
    $("mcDelay2Wrap").style.display =
      $("mcDelayType").value === "random" ? "flex" : "none";
  });

  async function macToggleRecord(){
    if(macRecording){
      const r = await macCall("macro_stop_record");
      macSetStatus("macRecStatus", r.msg, true);
    }else{
      // v30：起点模式一起送过去。默认「首次点击」——
      // 点完「开始录制」到切进目标程序之间的点击/移动不会被录进来。
      const mode = $("macStart").value || "first_click";
      const r = await macCall("macro_start_record", $("macIgnoreInj").checked,
                              mode, 3);
      macSetStatus("macRecStatus", r.msg, true);
    }
    await macPoll();
  }

  async function macDiscard(){
    const r = await macCall("macro_discard");
    macSetStatus("macRecStatus", r.msg, true);
    await macPoll();
  }

  async function macPlay(){
    const speed = parseFloat($("macSpeed").value) || 1;
    const rep = parseInt($("macRepeat").value, 10) || 1;
    const r = await macCall("macro_play", speed, rep);
    macSetStatus("macPlayStatus", r.msg, true);
    await macPoll();
  }

  async function macStopPlay(){
    const r = await macCall("macro_stop_play");
    macSetStatus("macPlayStatus", r.msg, true);
    await macPoll();
  }

  async function macSave(){
    const name = ($("macName").value || "").trim();
    if(!name){ macSetStatus("macSaveStatus", "先给宏起个名字", true); return; }
    const speed = parseFloat($("macSpeed").value) || 1;
    const rep = parseInt($("macRepeat").value, 10) || 1;
    const r = await macCall("macro_save", name, speed, rep);
    macSetStatus("macSaveStatus", r.msg, true);
    if(r.ok && r.names) macFillList(r.names, name);
  }

  async function macLoad(){
    const name = $("macList").value;
    if(!name){ macSetStatus("macSaveStatus", "下拉里还没有宏", true); return; }
    const r = await macCall("macro_load", name);
    macSetStatus("macSaveStatus", r.msg, true);
    if(r.ok){
      if(r.speed != null) macSetSpeed(r.speed);
      if(r.repeat != null) $("macRepeat").value = r.repeat;
      await macPoll();
    }
  }

  async function macDelete(){
    const name = $("macList").value;
    if(!name){ macSetStatus("macSaveStatus", "下拉里还没有宏", true); return; }
    // 两步确认：不用 window.confirm —— pywebview 不同平台下
    // 有的会直接返回 undefined（等于永远取消），有的弹窗会卡住界面。
    const now = Date.now();
    if(now - macDelArmed > 3000){
      macDelArmed = now;
      macSetStatus("macSaveStatus",
        "再点一次「删除」确认删掉「" + name + "」（3 秒内有效）", true);
      return;
    }
    macDelArmed = 0;
    const r = await macCall("macro_delete", name);
    macSetStatus("macSaveStatus", r.msg, true);
    if(r.names) macFillList(r.names);
  }

  /* ---------- 自绘标题栏：拖动 + 窗口控制 ----------
     【v31 改自研拖动】原来把拖动交还 Windows（ReleaseCapture +
     WM_NCLBUTTONDOWN(HTCAPTION)），实测在这个窗口上完全不动：
     WebView2 子窗口持有鼠标捕获，系统的拖动循环收不到 mousemove。
     现在改成「前端抓指针、后端 SetWindowPos」：
       pointerdown → title_drag_begin（记下窗口矩形与鼠标起点）
       pointermove → title_drag_move（算位移，后端搬窗口）
       pointerup   → title_drag_end
     用 pointer 事件 + setPointerCapture，鼠标甩出窗口也不丢。 */
  let dragPid = null, dragEl = null, dragDpr = 1, dragLastT = 0;
  function tbDown(e){
    if(e.button !== 0) return;
    if(e.target.closest(".tb-btn")) return;      // 按钮不参与拖动
    dragPid = e.pointerId;
    dragEl = e.currentTarget;
    dragDpr = window.devicePixelRatio || 1;
    try{ dragEl.setPointerCapture(dragPid); }catch(err){}
    window.pywebview.api.title_drag_begin(e.screenX, e.screenY, dragDpr);
  }
  function tbMove(e){
    if(dragPid === null || e.pointerId !== dragPid) return;
    const now = performance.now();
    if(now - dragLastT < 12) return;             // 60fps 够用，别再密了
    dragLastT = now;
    window.pywebview.api.title_drag_move(e.screenX, e.screenY);
  }
  function tbUp(e){
    if(dragPid === null || (e && e.pointerId !== dragPid)) return;
    try{ dragEl && dragEl.releasePointerCapture(dragPid); }catch(err){}
    dragPid = null; dragEl = null;
    window.pywebview.api.title_drag_end();
  }
  document.getElementById("titlebar").addEventListener("pointerdown", tbDown);
  window.addEventListener("pointermove", tbMove);
  window.addEventListener("pointerup", tbUp);
  window.addEventListener("pointercancel", tbUp);
  // 标题栏双击 = 最大化/还原，和Windows 习惯一致。
  // 注意要排除标题栏上的按钮：双击关闭按钮不该是「最大化」。
  window.addEventListener("dblclick", (e)=>{
    if(e.target.closest(".tb-btn")) return;
    if(!e.target.closest("#titlebar")) return;
    window.pywebview.api.title_maximize().then(syncMaxIcon);
  });
  async function winMin(){ await window.pywebview.api.title_minimize(); }
  async function winMax(){
    await window.pywebview.api.title_maximize();
    // 后端是阻塞式切换的，等一小会儿再问状态，图标才跟得上
    setTimeout(syncMaxIcon, 120);
  }
  async function winClose(){ await window.pywebview.api.title_close(); }

  /* ---------- 边框缩放（v31 自研） ----------
     和拖动同一套：pointerdown 记基准，pointermove 算新矩形，
     后端 SetWindowPos。不再依赖系统模态缩放循环。
     节流 12ms：IPC 每帧一次就够，过密反而拖不动。 */
  let rzPid = null, rzEl = null, rzDpr = 1, rzLastT = 0;
  async function rzDown(e, edge){
    if(e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    if(rzPid !== null) return;
    rzPid = e.pointerId;
    rzEl = e.currentTarget;
    rzDpr = window.devicePixelRatio || 1;
    try{ rzEl.setPointerCapture(rzPid); }catch(err){}
    const r = await window.pywebview.api.title_resize(edge, e.screenX,
                                                      e.screenY, rzDpr);
    if(!r || !r.ok){ rzUp(); }
  }
  function rzMove(e){
    if(rzPid === null || e.pointerId !== rzPid) return;
    const now = performance.now();
    if(now - rzLastT < 12) return;
    rzLastT = now;
    window.pywebview.api.title_resize_move(e.screenX, e.screenY);
  }
  function rzUp(e){
    if(rzPid === null) return;
    try{ rzEl && rzEl.releasePointerCapture(rzPid); }catch(err){}
    rzPid = null; rzEl = null;
    window.pywebview.api.title_resize_end();
    setTimeout(syncMaxIcon, 60);
  }
  window.addEventListener("pointermove", rzMove);
  window.addEventListener("pointerup", rzUp);
  window.addEventListener("pointercancel", rzUp);

  //最大化后给「还原」按钮换个图标（双矩形），不然用户不知道当前状态。
  // 同时给 body 加 .maxed：最大化时隐藏边框热区，
  // 否则拖窗口边缘会触发「还原 + 缩放」这种反直觉行为
  //（Windows 原生窗口也是最大化后不能拖边框）。
  //
  // 【为什么不用 IsZoomed 判断】后端是自己SetWindowPos 改矩形的，
  // 系统维护的 maximized 标志位不会跟着变，IsZoomed 永远是 False。
  // 后端已经改成「比对矩形和屏幕工作区」，这里只管把结果显示出来。
  async function syncMaxIcon(){
    try{
      const r = await window.pywebview.api.is_maximized();
      $("btnMax").innerHTML = r.maximized
        ? '<svg width="11" height="11" viewBox="0 0 11 11">'
          + '<rect x="0" y="2.2" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.2"/>'
          + '<path d="M3.4 2.2 V0.5 H11 V8 H9.2" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>'
        : '<svg width="11" height="11" viewBox="0 0 11 11">'
          + '<rect x="0.5" y="0.5" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>';
      $("btnMax").title = r.maximized ? "还原" : "最大化";
      document.body.classList.toggle("maxed", !!r.maximized);
    }catch(e){}
  }
  // 既听resize 事件，也定时查一次：
  // 最大化是我们自己改矩形的，不一定会触发 WebView2 的 resize 事件，
  // 只靠事件会让按钮图标停在旧状态。
  window.addEventListener("resize", ()=>{ syncMaxIcon(); });

  async function initButtons(list){
    const s = $("mBtn");
    // 【关键】每次重建前必须清空，否则选项会一次次累积
    // （bootstrap 和 addMouse 都会调这里，不清空就变成两份、三份）
    const keep = s.value;
    s.innerHTML = "";
    list.forEach(b=>{
      const o = document.createElement("option");
      o.value = b.id; o.textContent = b.name; s.appendChild(o);
    });
    if(keep) s.value = keep;      // 尽量保留用户当前选择
  }

  /* 【两段式·2026-10-15】按钮只装填参数进入就绪态，不再直接启动。
     主人原话：「设定好参数以后点启动应该是准备就绪，然后按鼠标连点
     启停键（默认 F5）再开始」。旧代码点按钮即启动，第一次按 F5
     被当成「停止正在跑的任务」——就是「点第一次 F5 就暂停」。
     后端 arm_mouse 只存参数 + 置 mouse_armed，不起引擎；
     真启动由 toggle_mouse（F5 热键）完成，且此时仪表盘自动让位。 */
  async function startMouse(){
    const r = await window.pywebview.api.arm_mouse({
      button: $("mBtn").value, action: $("mAct").value,
      count: $("mCount").value, duration: $("mDur").value, cps: $("mCps").value,
      jitter: $("mJitter").value, mode: $("mMode").value,
      pos: $("mPos").value, countdown: $("mCd").value,
      jig: $("mJig").value, done: $("mDone").value
    }, $("mHide").checked);
    toast("mStatus", r.msg);
    saveSettings();
  }

  /* 鼠标区几行的联动显隐（v22 + v23）：
     · 固定坐标模式才显示坐标行
     · 长按不支持倒计时和固定坐标（长按路径不走连点引擎）——
       禁用而不是隐藏：隐藏会让用户以为这个功能消失了。
     · v23：抖器动作才显示「挪动间隔」；长按不支持收尾动作
       （press/release 路径没有自然完成事件），禁用并归零。
     · v23：抖器不支持启动倒计时（它不点任何东西、也不需要切前台），
       禁用并归零——不做「收了参数却不用」的静默忽略。
     【为什么不用 style.display 读内联值判断状态】v20 踩过的坑：
     内联 style.display 初始是空串不是 "block"，读它判断开关会把
     第一次点击判成「没开」。这里只写不读，没有判断问题。
     · v26：坐标输入框跟着点击位置走——fixed 显示单行（mPosRow），
       multi/box 显示多行（mPointsRow），两者互斥不同时出现；
       长按不点任何位置，两个都藏掉。 */
  function syncMouseRows(){
    const act = $("mAct").value;
    const hold = act === "hold";
    const jig = act === "jiggle";
    const mode = $("mMode").value;
    const fixed = mode === "fixed";
    const multi = (mode === "multi" || mode === "box");
    $("mPosRow").style.display = (fixed && !hold) ? "" : "none";
    $("mPointsRow").style.display = (multi && !hold) ? "" : "none";
    $("mPoints").disabled = hold;
    $("mCd").disabled = hold || jig;
    if(hold || jig) $("mCd").value = "0";
    $("mMode").disabled = hold;
    $("mJigRow").style.display = jig ? "" : "none";
    $("mDone").disabled = hold;
    if(hold) $("mDone").value = "none";
  }

  /* 「取当前」：让后端读真实鼠标坐标填进输入框。
     DPI 缩放在网页里算坐标会偏，必须问后端（GetCursorPos）。 */
  async function recordPos(){
    const r = await window.pywebview.api.cursor_pos();
    if(r && r.ok){
      const pt = r.x + "," + r.y;
      // v26：多点/框内模式下「取当前」应该是**追加一行**（逐个点位录），
      // 固定坐标模式才是覆盖。同一个按钮两种语义，靠当前模式判断。
      const mode = $("mMode").value;
      if(mode === "multi" || mode === "box"){
        const cur = $("mPoints").value.replace(/\s+$/,"");
        $("mPoints").value = (cur ? cur + "\n" : "") + pt;
        toast("mStatus", "已追加坐标 " + pt + "（共 "
              + $("mPoints").value.split("\n").filter(s=>s.trim()).length + " 个）");
      }else{
        $("mPos").value = pt;
        toast("mStatus", "已记录当前坐标 " + pt);
      }
    }else{
      toast("mStatus", "取坐标失败：" + ((r && r.msg) || "未知错误"));
    }
  }
  /* v26：清空坐标列表（录错了重来；不一次清一个，挂着机没人愿意逐行删） */
  function clearPoints(){
    $("mPoints").value = "";
    toast("mStatus", "坐标列表已清空");
  }
  async function stopMouse(){
    const r = await window.pywebview.api.stop_mouse();
    toast("mStatus", r.msg);
  }

  /* v23 S4：取消待执行的收尾动作（完成后动作倒计时中可点）。 */
  async function cancelDone(){
    const r = await window.pywebview.api.cancel_done_action();
    toast("mStatus", r.msg || "已处理");
  }

  /* ---- 自定义鼠标键：两步式识别 ----
     第一步「开始识别」清空历史按键记录，
     第二步「完成」提交。一次性做法会读到几秒前的旧按键，
     所以用户必须先按侧键、再点按钮，顺序不能反。 */
  async function armMouse(){
    const r = await window.pywebview.api.arm_custom_mouse();
    $("armBtn").disabled = true;
    $("addBtn").classList.add("pulse");
    $("addBtn").disabled = false;
    $("mStatus").innerHTML = r.msg;
    $("mStatus").className = "status live";
    // 8 秒后自动收尾，防止用户按了侧键忘记点完成
    setTimeout(()=>{
      $("armBtn").disabled = false;
      $("addBtn").classList.remove("pulse");
    }, 8000);
  }
  async function addMouse(){
    const r = await window.pywebview.api.add_custom_mouse("");
    $("armBtn").disabled = false;
    $("addBtn").classList.remove("pulse");
    // 后端现在返回 {ok, msg, buttons}，不再是裸数组
    if(r.buttons) await initButtons(r.buttons);
    $("mStatus").innerHTML = r.msg;
    $("mStatus").className = r.ok ? "status live" : "status";
  }

  /* ---- 触发键：按一下就绑，替代只能选三项的下拉框 ----
     后端 arm_trigger_capture() 亮起等待，Raw Input 线程收到
     「刚按下」的事件后记下编号，这里轮询取走结果并立刻绑定。
     用 seq 做「只处理新结果」的判据：不给seq 的话每轮轮询都会
     把上一个键重复绑一遍。 */
  let trigArmed = false;
  let trigSeq = 0;
  let trigTimer = 0;
  const TRIG_TIMEOUT_MS = 8000;
  /* v29：超时提示。后端 _CAPTURE_TIMEOUT_MSG 里有同一句，
     两边都写是为了「桥断了、后端没回话」时也能给用户一句人话。 */
  const TRIG_TIMEOUT_TXT = "没收到任何标准鼠标键，请检查鼠标模式/驱动："
    + "① 优先用有线或 2.4G（蓝牙模式常把侧键报成键盘/多媒体事件）；"
    + "② 官方驱动里把侧键设为「鼠标按键 4/5」，不要设成键盘按键或宏；"
    + "③ 先试中键——中键能绑，就说明是侧键上报的问题。";

  function trigNote(msg, bad){
    const n = $("hkNote");
    n.innerHTML = msg;
    n.className = "hint tiny";
    n.style.color = bad ? "var(--spark)" : "var(--accent-mint)";
  }

  async function armTriggerCapture(){
    const act = $("hotKeyAct").value;
    trigArmed = true;
    const btn = $("capTrigBtn");
    btn.classList.add("pulse");
    btn.textContent = "请按下鼠标键…";
    await window.pywebview.api.arm_trigger_capture();
    trigNote("正在等待：请按下要绑定的鼠标键"
      + "（中键或任意侧键 G1~G8；左键/右键不可用）。"
      + "8 秒内没有按键会自动取消，按 Esc 也可取消。", false);
    // 前端定时器只是兜底：真超时由后端判（raw_diag 的 armed 会变 False）。
    // 到点主动问一次，把后端给的原因原样显示出来。
    clearTimeout(trigTimer);
    trigTimer = setTimeout(function(){ pollTriggerCapture(true); },
                           TRIG_TIMEOUT_MS + 300);
  }
  function disarmTrigger(){
    trigArmed = false;
    // 不清定时器的话，成功绑定 8 秒后还会再刷一条超时提示
    clearTimeout(trigTimer);
    const btn = $("capTrigBtn");
    btn.classList.remove("pulse");
    btn.textContent = "按下要绑的键";
    window.pywebview.api.cancel_trigger_capture();
  }
  async function pollTriggerCapture(force){
    if(!trigArmed && !force) return;
    let r = null;
    try{ r = await window.pywebview.api.take_captured_key(trigSeq); }
    catch(e){ r = null; }
    if(r && r.got){
      trigSeq = r.seq;
      disarmTrigger();
      if(r.timeout){
        // 后端判的超时：原文照抄，含「消息收到几条」这种细节
        trigNote(r.msg || TRIG_TIMEOUT_TXT, true);
        return;
      }
      if(!r.ok){
        // 比如用户按了左键 —— 明确告诉用户不能用，而不是静默失败
        trigNote(r.msg || "这个键不能用作触发键", true);
        return;
      }
      const act = $("hotKeyAct").value;
      const b = await window.pywebview.api.bind_captured_key(r.raw_id, act);
      trigNote(b.msg || r.msg, !b.ok);
      await refreshHotkeys();
      return;
    }
    // 到点还没结果（后端没跑 tick / 桥断了）：前端兜底收场。
    // 不能让「请按下鼠标键…」一直挂着 —— 下一次点左键就会被录进来。
    if(force && trigArmed){
      disarmTrigger();
      trigNote(TRIG_TIMEOUT_TXT, true);
    }
  }
  async function clearTrigKey(){
    const r = await window.pywebview.api.set_button_hotkey("", "");
    toast("mStatus", r.msg);
    await refreshHotkeys();
  }
  async function refreshHotkeys(){
    try{
      const b = await window.pywebview.api.button_hotkeys();
      const h = await window.pywebview.api.hotkey_text();
      // 显示两类触发方式：系统热键（可能被占）+ 鼠标键（几乎不会冲突）
      // 鼠标键优先显示 —— 它才是那条永远不会被抢占的通道。
      //
      // 【关键】读 by_action 而不是 labels：
      // 界面问的是「鼠标连点现在绑的是哪个键」，这是按**动作**查；
      // labels 是按**按键名**索引的，拿 labels.mouse 永远取不到，
      // 于是回退显示系统热键 F5 —— 「绑了 G2 触发方式还显示 F5」
      // 就是这么来的（后端已按动作返回 by_action）。
      const by = b.by_action || {};
      const mBtn = (by.mouse && by.mouse.label) || h.mouse_pretty
                   || h.mouse || "未绑定";
      const kBtn = (by.key && by.key.label) || h.key_pretty
                   || h.key || "未绑定";
      $("hkMouse").textContent = mBtn;
      $("hkKey2").textContent  = kBtn;
      // v34：宏启停键同样是「绑了鼠标键就报鼠标键，没绑才回落系统热键」
      const cBtn = (by.macro && by.macro.label) || h.macro_pretty
                   || h.macro || "未绑定";
      $("hkMacro").textContent = cBtn;
      // 卡片里的提示文案同步成实际生效的键：
      // 2026-10-15 实测本机 F5/F6 被其他软件占用（RegisterHotKey 1409），
      // 程序自动降级到 F8/F9，但文案写死常量——用户按 F6 没反应
      // 还以为程序坏了。真键以 hotkey_text 返回的 pretty 为准。
      // 提示里写「用户真正会按的那个键」：绑了鼠标键就显示中键/侧键，
      // 没绑才回落 F8/F9 这类系统热键。
      if(h.mouse_trigger && $("hintMouseKey"))
        $("hintMouseKey").textContent = h.mouse_trigger;
      else if(h.mouse_pretty && $("hintMouseKey"))
        $("hintMouseKey").textContent = h.mouse_pretty;
      if(h.key_trigger && $("hintKeyKey"))
        $("hintKeyKey").textContent = h.key_trigger;
      else if(h.key_pretty && $("hintKeyKey"))
        $("hintKeyKey").textContent = h.key_pretty;
      // 降级说明必须上界面：这是「按提示的键没反应」的唯一线索
      const notes = [h.note_mouse, h.note_key, h.note_macro].filter(x=>x);
      if(notes.length){
        $("hkNote").innerHTML = "⚠ " + notes.join("；");
        $("hkNote").className = "hint tiny warn";
      }else if(!$("hkNote").innerHTML){
        $("hkNote").innerHTML = "两种触发方式任选其一，按哪个都行。";
        $("hkNote").className = "hint tiny";
      }
    }catch(e){}
  }

  // ---- 全局暂停 / 恢复 ----
  // 暂停 ≠ 停止：动作定住、参数保留、一个事件都不注入。
  // 游戏里按 Pause 键（或右上角这个按钮）即可定住画面，
  // 再按一次原地恢复 —— 不用切回仪表盘重设参数。
  // 弹窗提示由后端发（游戏全屏时仪表盘看不到，弹窗才看得到）。

  /* API 调用失败必须露出来。
     【2026-10-16 主人反馈「点了没反应、也不弹窗」的帮凶】
     原来这里（以及别处）是 catch(e){} 一把吞掉：后端一旦抛异常
     （那次是 NameError），按钮文字不变、没有通知、控制台也不看，
     界面上**一点痕迹都没有**，只能靠猜。
     现在把错误原样贴到 hkNote 上，同时写 console。 */
  function apiFail(e, what){
    const msg = (e && (e.message || e.toString())) || "未知错误";
    const n = $("hkNote");
    if(n){
      n.innerHTML = "⚠ " + what + "失败：" + msg;
      n.className = "hint tiny warn";
      n.style.color = "";
    }
    try{ console.error("[api] " + what + " 失败", e); }catch(_){}
  }

  // 【防抖 · 2026-10-17 主人实测】暂停/恢复是个「切换」动作，
  // 而连点/双击会触发两次 click —— 第一次恢复、第二次又暂停，
  // 净效果为零。主人报的原话：「鼠标区和键盘区都暂停的时候，点那个
  // 全局恢复运行/暂停按钮无论如何都不会恢复」（真因就是双击/连点）。
  // 600ms 内的第二次点击直接忽略。
  let _pauseBtnTs = 0;
  async function togglePause(){
    const nowTs = Date.now ? Date.now() : 0;
    if (nowTs && nowTs - _pauseBtnTs < 600) return;
    _pauseBtnTs = nowTs;
    try{
      const r = await window.pywebview.api.toggle_pause();
      $("pauseBtn").textContent = r.paused ? "恢复运行" : "暂停";
      $("pauseBtn").classList.toggle("paused", !!r.paused);
      $("hkNote").innerHTML = r.msg;
      $("hkNote").className = "hint tiny";
      $("hkNote").style.color = r.paused ? "var(--spark)" : "var(--accent-mint)";
    }catch(e){ apiFail(e, "暂停/恢复"); }
  }

  // 暂停热键显示名（后端传回 pause/scrolllock/ctrl+pause 这类字符串）
  function prettyPauseKey(k){
    if(!k) return "未注册";
    const map = {pause:"Pause", scrolllock:"Scroll Lock",
                 "ctrl+pause":"Ctrl + Pause"};
    return map[k] || k;
  }

  /* ---- 系统热键改绑：同样「按一下就录」 ----
     捕获在网页里做（keydown），所以录的是真实按下组合，
     不用手打字符串也就不会打错键名。
     注意：捕获期间必须 preventDefault，否则 F5 会刷新页面、
     F12 会弹出开发者工具。 */
  let sysArmed = false;
  async function armSysCapture(){
    sysArmed = true;
    const btn = $("capSysBtn");
    btn.classList.add("pulse");
    btn.textContent = "请按键…";
    $("hkNote").innerHTML = "正在等待：请按下要用的系统热键"
      + "（可带 Ctrl / Alt / Shift）。按 Esc 取消。";
    $("hkNote").style.color = "var(--accent-mint)";
  }
  function disarmSys(){
    sysArmed = false;
    const btn = $("capSysBtn");
    btn.classList.remove("pulse");
    btn.textContent = "按一下录入";
  }
  function keyComboText(e){
    // 把键盘事件拼成后端认得的字符串：ctrl+shift+f8
    const parts = [];
    if(e.ctrlKey) parts.push("ctrl");
    if(e.altKey)  parts.push("alt");
    if(e.shiftKey) parts.push("shift");
    if(e.metaKey) parts.push("win");
    let k = e.key;
    if(k === " ") k = "space";
    else if(k === "Escape") k = "esc";
    else if(k && k.length === 1) k = k.toLowerCase();
    if(!k || ["Control","Alt","Shift","Meta"].includes(k)) return "";
    parts.push(k.toLowerCase());
    return parts.join("+");
  }
  async function applySysCapture(e){
    const combo = keyComboText(e);
    if(!combo) return;            // 只按了修饰键，等真正的键
    const act = $("hotKeySysAct").value;
    disarmSys();
    const r = await window.pywebview.api.set_system_hotkey(act, combo);
    $("hkNote").innerHTML = r.msg;
    $("hkNote").style.color = r.ok ? "var(--accent-mint)" : "var(--spark)";
    await refreshHotkeys();
  }
  async function clearSysHotkey(){
    const act = $("hotKeySysAct").value;
    const r = await window.pywebview.api.clear_system_hotkey(act);
    $("hkNote").innerHTML = r.msg;
    $("hkNote").style.color = r.ok ? "var(--accent-mint)" : "var(--spark)";
    await refreshHotkeys();
  }

  // 捕获期间全局拦 keydown：Esc 取消，其余按键转成组合字符串
  window.addEventListener("keydown", (e)=>{
    if(sysArmed){
      e.preventDefault(); e.stopPropagation();
      if(e.key === "Escape"){ disarmSys(); refreshHotkeys(); return; }
      applySysCapture(e);
      return;
    }
    if(keyCapArmed){
      e.preventDefault(); e.stopPropagation();
      if(e.key === "Escape"){ disarmKeyCap(); return; }
      addKeyChip(keyComboText(e));
      return;
    }
    if(trigArmed && e.key === "Escape"){ disarmTrigger(); }
  }, true);
  /* ---- 生效按键：按键捕获 + 筹码显示 ----
     【为什么不做成下拉】要绑的键有 104 个，加上 Ctrl/Alt/Shift 组合是几百种，
     下拉选一次得点十几次，比手打还慢；而且游戏里用的可能是
     小键盘/方向键区扩展键，下拉根本列不全。
     改成「按一下就录」：录进去的是一个字符串（w / ctrl+shift+w），
     多键用逗号分隔，界面上显示成一排筹码，点 × 单独删。 */
  let keyCapArmed = false;
  function prettyCombo(text){
    if(!text) return "";
    return text.split("+").map(p=>{
      p = p.trim();
      const low = p.toLowerCase();
      if(low === "ctrl") return "Ctrl";
      if(low === "shift") return "Shift";
      if(low === "alt") return "Alt";
      if(low === "win") return "Win";
      if(low === "space") return "Space";
      if(low === "esc") return "Esc";
      if(low === "enter") return "Enter";
      if(low === "tab") return "Tab";
      if(low === "backspace") return "Bksp";
      if(low === "delete") return "Del";
      if(low === "insert") return "Ins";
      if(low === "pageup") return "PgUp";
      if(low === "pagedown") return "PgDn";
      if(low === "arrowup") return "↑";
      if(low === "arrowdown") return "↓";
      if(low === "arrowleft") return "←";
      if(low === "arrowright") return "→";
      if(low.startsWith("f") && low.slice(1).match(/^\d+$/)) return low.toUpperCase();
      if(low.startsWith("num") && low.slice(3).match(/^\d+$/)) return "Num" + low.slice(3);
      return p.length === 1 ? p.toUpperCase() : p;
    }).join(" + ");
  }
  function keyList(){
    return $("kKeys").value.split(",").map(s=>s.trim()).filter(Boolean);
  }
  function setKeyList(list){
    $("kKeys").value = list.join(",");
    renderKeyChips();
    saveSettings();
  }
  function renderKeyChips(){
    const wrap = $("keyCapWrap");
    // 只删掉旧的筹码，保留输入框和两个按钮
    wrap.querySelectorAll(".capchip").forEach(n=>n.remove());
    keyList().forEach((k, i)=>{
      const s = document.createElement("span");
      s.className = "capchip";
      // textContent 用于按键名（避免用户输入被当 HTML 解析）
      s.appendChild(document.createTextNode(prettyCombo(k)));
      const x = document.createElement("span");
      x.className = "x";
      x.textContent = "×";
      x.title = "删除这个键";
      x.onclick = ()=>{
        const l = keyList();
        l.splice(i, 1);
        setKeyList(l);
      };
      s.appendChild(x);
      wrap.appendChild(s);
    });
  }
  function addKeyChip(combo){
    if(!combo) return;
    disarmKeyCap();
    const l = keyList();
    // 已经录过就不重复加，避免不小心按两次导致同一键出现两遍
    if(l.includes(combo)) return;
    l.push(combo);
    setKeyList(l);
  }
  function delKeyChip(){
    const l = keyList();
    l.pop();
    setKeyList(l);
  }
  function armKeyCapture(){
    keyCapArmed = true;
    const btn = $("capKeysBtn");
    btn.classList.add("pulse");
    btn.textContent = "请按键…";
    $("keyCapHint").innerHTML = "正在等待：请按下要录入的键"
      + "（可带 Ctrl / Alt / Shift）。按 Esc 取消。";
  }
  function disarmKeyCap(){
    keyCapArmed = false;
    const btn = $("capKeysBtn");
    btn.classList.remove("pulse");
    btn.textContent = "按一下录入";
    $("keyCapHint").innerHTML = "点「按一下录入」再按你游戏里用的键，"
      + "可录单个键也可加修饰键（如 Ctrl+W）。"
      + "多个键会显示成一个个筹码，点 × 单独删。";
  }
  // 输入框里手打的文字也要同步出筹码，否则两处显示会打架
  $("kKeys").addEventListener("input", renderKeyChips);

  /* ---------- 参数持久化 ----------
     【为什么必须存】连点参数（哪个键、多少 CPS、按多久）调一次很费劲，
     关掉重开全丢回默认值 = 每次都要重调一遍。
     存到 **exe 同目录**的 ui_settings.json：用户能直接找到、直接改，
     也能拷到别的机器（触发键绑定跟着一起走）。
     同目录写不进去（放在 Program Files、只读盘）时，后端会自动退回
     %LOCALAPPDATA%\MouseAutoClicker\，界面无感。
     ⚠ 后端算这个路径**不能用 __file__**：PyInstaller 单文件模式下
     __file__ 指向 %TEMP%\_MEIxxxxx\，而该目录名每次启动都变，
     写进去的文件下次启动根本不在 → 等于没存。
     （2026-10-16 主人报的「绑好 G1/G2 重开又变回默认」就是这个坑。） */
  function collectSettings(){
    return {
      mouse: { button: $("mBtn").value, action: $("mAct").value,
               count: $("mCount").value, duration: $("mDur").value,
               cps: $("mCps").value, jitter: $("mJitter").value,
               mode: $("mMode").value, pos: $("mPos").value,
               points: $("mPoints").value,     // v26：多点/框内坐标列表
               countdown: $("mCd").value, hide: $("mHide").checked,
               jig: $("mJig").value, done: $("mDone").value },
      key:   { keys: $("kKeys").value, threshold: $("kThr").value,
               hz: $("kHz").value, hold: $("kMode").value,
               gap: $("kGap").value },
      hide:  $("mHide").checked,
      tray:  $("mTray").checked,   // v24 S1：关闭时缩到托盘
    };
  }
  async function saveSettings(){
    try{ await window.pywebview.api.save_settings(collectSettings()); }
    catch(e){}
  }
  function applySettings(s){
    if(!s || typeof s !== "object") return;
    const m = s.mouse || {};
    if(m.button != null) $("mBtn").value = m.button;
    if(m.action)  $("mAct").value  = m.action;
    if(m.count != null)   $("mCount").value = m.count;
    if(m.duration != null) $("mDur").value  = m.duration;
    if(m.cps != null)      $("mCps").value   = m.cps;
    if(m.jitter != null)   $("mJitter").value = m.jitter;
    if(m.mode)             $("mMode").value  = m.mode;
    if(m.pos != null)      $("mPos").value   = m.pos;
    if(m.points != null)   $("mPoints").value = m.points;   // v26 坐标列表
    if(m.countdown != null) $("mCd").value   = m.countdown;
    if(m.hide != null)     $("mHide").checked = !!m.hide;
    if(m.jig != null)      $("mJig").value   = m.jig;
    if(m.done)             $("mDone").value  = m.done;
    if(s.tray != null)     $("mTray").checked = !!s.tray;   // v24 托盘开关
    // v28：顶层 hide 是历史遗留的**副本**（真值在 mouse.hide）。
    // 只有老方案文件里缺少 mouse.hide 时才用它兜底 —— 这样它不再是一个
    // 只写不读的死键，旧文件也不会丢设置。
    if(s.hide != null && m.hide == null) $("mHide").checked = !!s.hide;
    syncMouseRows();          // 恢复后联动状态要对（固定坐标行显隐等）
    const k = s.key || {};
    if(k.keys != null)      $("kKeys").value = k.keys;
    if(k.threshold != null) $("kThr").value  = k.threshold;
    if(k.hz != null)        $("kHz").value   = k.hz;
    if(k.hold)             $("kMode").value = k.hold;
    if(k.gap != null)       $("kGap").value  = k.gap;
  }

  // 键盘区与鼠标区同一套两段式（见 startMouse 的注释）：
  // 点「① 就绪」只装参数，按 F6 才真启动。
  async function startKey(){
    const r = await window.pywebview.api.arm_keyboard({
      keys: $("kKeys").value, threshold: $("kThr").value,
      hz: $("kHz").value, hold: $("kMode").value, gap: $("kGap").value
    });
    toast("kStatus", r.msg);
    saveSettings();
  }
  async function stopKey(){
    const r = await window.pywebview.api.stop_keyboard();
    toast("kStatus", r.msg);
  }

  /* ---- 一键提权重启 ----
     后端 relaunch_as_admin 用 ShellExecuteW(runas) 拉起一个管理员
     权限的新实例（弹一次 UAC），新窗口建好后老实例自动退位。
     UAC 被取消时后端返回 ok:false，这里恢复按钮并把原因显示出来。 */
  async function relaunchAdmin(){
    const btn = $("elevBtn");
    if(!btn) return;
    btn.disabled = true;
    btn.textContent = "等待 UAC 确认…";
    let r = null;
    try{ r = await window.pywebview.api.relaunch_as_admin(); }
    catch(e){ r = {ok:false, msg:"提权重启出错：" + e}; }
    if(r && r.ok){
      btn.textContent = "正在重启…";
      $("gMsg").textContent = "正在以管理员身份重启本工具…";
    }else{
      btn.disabled = false;
      btn.textContent = "以管理员身份重启";
      $("gMsg").textContent = (r && r.msg) || "提权重启失败";
    }
  }

  // 锁定行为切换时，只在「反复点击」下才需要显示按下时长
  function syncGap(){
    const pair = $("kMode").value === "false";
    $("gapRow").style.display = pair ? "" : "none";
    $("gapHint").textContent = pair
      ? "「反复点击」每次会注入「按下→等待→抬起」。「按下时长」太短会被目标程序当成抖动忽略，建议 15~40 毫秒。"
      : "「一直按着」只注入按下、不注入抬起，重复频率即按住状态的刷新频率。";
  }
  $("kMode").addEventListener("change", syncGap);

  /* ---- 原始输入自检条 ----
     「绑键没反应」时最难判断的是：到底程序压根没收到按键，
     还是收到了但绑定那一步出错。前者要修链路，后者要修绑定逻辑，
     两条路完全不同。所以这里把后端计数直接摆出来：
     按一下鼠标键，消息数 +1 就说明链路是通的。 */
  async function refreshRawDiag(){
    let d;
    try{ d = await window.pywebview.api.raw_diag(); }catch(e){ return; }
    if(!d) return;
    const modeEl = $("rdMode");
    if(d.mode === "raw"){
      modeEl.textContent = "原始输入（正常）";
      modeEl.className = "ok";
    }else if(d.mode === "poll"){
      modeEl.textContent = "降级轮询";
      modeEl.className = "bad";
    }else{
      modeEl.textContent = "未就绪";
      modeEl.className = "bad";
    }
    $("rdMsg").textContent = d.messages || 0;
    const ids = d.last_ids || [];
    const NAMES = {0:"左键",1:"右键",2:"中键",3:"G1",4:"G2",
                   5:"G3",6:"G4",7:"G5",8:"G6",9:"G7",10:"G8",
                   11:"G9",12:"G10",13:"G11",14:"G12",15:"G13"};
    const lp = d.last_press || null;
    if(ids.length){
      // 正按着：直接报按住的键
      $("rdLast").textContent = ids.map(i => NAMES[i] || ("#" + i)).join("+");
      $("rdLast").className = "rd-hot";
    }else if(lp && lp.age <= 10){
      // 已经松开：显示「刚刚按过什么」。
      // 这是本条的修复点——旧版抬起就把 last_ids 清空，500ms 轮询
      // 采样不到一次 80ms 的点击，看起来像「只偶尔认一次左键」。
      $("rdLast").textContent = (lp.name || ("#" + lp.id))
        + "（" + Number(lp.age).toFixed(1) + " 秒前）";
      $("rdLast").className = "rd-hot";
    }else{
      $("rdLast").textContent = "—";
      $("rdLast").className = "";
    }
    $("rdHint").textContent = d.hint || "";
  }

  // 轮询状态
  let lastMouseRun = null, lastKeyRun = null;
  // 就绪态（两段式的「参数已装填」）也要做翻转检测，
  // 否则每 500ms 重复 toggle 会白刷样式
  let lastMouseArmed = null, lastKeyArmed = null;
  // 必须在 poll 之前声明：poll 里会读它。
  // （声明写在后面会撞 let 的暂时性死区，poll 首次执行就 ReferenceError，
  //  而它整个包在 try 里，结果是「状态永远不刷新」——很难联想到是这一行）
  let lastMaxCheck = 0;
  async function poll(){
    try{
      const s = await window.pywebview.api.status();
      // 状态文字：运行中用高亮色，就绪用弱化色，
      // 否则四个 KPI 里两个写着「就绪」却最显眼，视觉重心全错。
      // data-v 是给 .n::after 用的（画在下层的辉光副本），
      // 必须跟着 textContent 一起更新，否则辉光会显示旧值。
      // 【两段式·2026-10-15】新增「已就绪」态：参数装好了、等触发键，
      // 用 .n.armed 青色，和运行的薄荷绿、闲置的灰区分开——
      // 旧版「就绪」和「已启动」看起来没区别，是状态机 bug 的帮凶。
      // 【分区暂停·2026-10-16】触发键现在切的是「暂停/继续」而不是「停止」，
      // 所以 KPI 必须能显示「已暂停」——否则界面写着「left 连点」、
      // 游戏里却一动不动，看着像坏了。
      const mt = s.mouse_running ? (s.mouse_paused ? "已暂停" : s.mouse_info)
                : (s.mouse_armed ? "已就绪" : "就绪");
      const kt = s.key_running ? (s.key_paused ? "已暂停" : s.key_info)
                : (s.key_armed ? "已就绪" : "就绪");
      $("kMouse").textContent = mt;
      $("kMouse").dataset.v = mt;
      $("kMouse").className = "n" + (s.mouse_running ? (s.mouse_paused ? " pause" : " on")
                                  : (s.mouse_armed ? " armed" : " idle"));
      $("kKey").textContent = kt;
      $("kKey").dataset.v = kt;
      $("kKey").className = "n" + (s.key_running ? (s.key_paused ? " pause" : " on")
                                  : (s.key_armed ? " armed" : " idle"));
      $("kClick").textContent = s.click;
      $("kClick").dataset.v = s.click;
      $("kTime").textContent = s.elapsed + "s";
      $("kTime").dataset.v = s.elapsed + "s";
      // ---- 分区状态：每个区只显示自己的消息 ----
      // 【为什么改】以前两区都渲染全局 s.message，
      // 「鼠标暂停了」也会显示在键盘区（主人反馈：
      // 「几个区的显示状态是全局的而不是单独的」）。
      // 后端已按 mouse/key 分区写 mouse_msg / key_msg，这里分开渲染。
      if(s.mouse_running){
        $("mStatus").innerHTML = (s.mouse_paused ? "已暂停 · " : "运行中：")
                               + (s.mouse_msg || s.mouse_info);
        $("mStatus").className = "status live" + (s.mouse_paused ? " paused" : "");
      }else if(s.mouse_armed){
        // 就绪态：后端 mouse_msg 此时是「已就绪：按 F5 开始连点…」
        $("mStatus").innerHTML = s.mouse_msg || "已就绪：按触发键开始连点";
        $("mStatus").className = "status armed";
      }else{
        $("mStatus").innerHTML = s.mouse_msg || "鼠标区未启动。";
        $("mStatus").className = "status";
      }
      // v23 S4：收尾动作（完成后动作）倒计时中才显示「取消」按钮。
      // pending 在关机指令已发后仍为 true——系统层还有 60 秒窗口，
      // 这时候取消照样能救回来（后端会发 shutdown /a）。
      const dcr = $("doneCancelRow");
      if(dcr){
        const dp = s.done_pending || {};
        dcr.style.display = dp.pending ? "" : "none";
      }
      if(s.key_running){
        $("kStatus").innerHTML = (s.key_paused ? "已暂停 · " : "运行中：")
                                + (s.key_msg || s.key_info);
        $("kStatus").className = "status live" + (s.key_paused ? " paused" : "");
      }else if(s.key_armed){
        $("kStatus").innerHTML = s.key_msg || "已就绪：按触发键启动锁定";
        $("kStatus").className = "status armed";
      }else{
        $("kStatus").innerHTML = s.key_msg || "键盘区未启动。";
        $("kStatus").className = "status";
      }
      // 全局消息单独一行（顶栏下面），不再和各区状态混在一起
      $("gMsg").textContent = s.message || "";
      // 暂停按钮文字跟着状态走：热键/别处改了状态，按钮也要变
      $("pauseBtn").textContent = s.paused ? "恢复运行" : "暂停";
      $("pauseBtn").classList.toggle("paused", !!s.paused);
      const busy = s.mouse_running || s.key_running;
      $("badge").textContent = s.paused ? "PAUSED"
                             : (busy ? "RUNNING" : "SYS READY");
      $("badge").className = "tag" + (busy ? " run" : "")
                            + (s.paused ? " pause" : "");
      // 卡片激活态：只在状态翻转时改 class，避免每 500ms 重复触发
      // breathe 动画的样式重算（clip-path 重绘在 WebView2 上不便宜）
      if(s.mouse_running !== lastMouseRun){
        lastMouseRun = s.mouse_running;
        $("cardMouse").classList.toggle("on", !!s.mouse_running);
      }
      // 就绪态给卡片描个边（比运行的辉光弱，但一眼能看出「装好了」）
      if(s.mouse_armed !== lastMouseArmed){
        lastMouseArmed = s.mouse_armed;
        $("cardMouse").classList.toggle("armed",
                                        !!s.mouse_armed && !s.mouse_running);
      }
      if(s.key_running !== lastKeyRun){
        lastKeyRun = s.key_running;
        $("cardKey").classList.toggle("on", !!s.key_running);
      }
      if(s.key_armed !== lastKeyArmed){
        lastKeyArmed = s.key_armed;
        $("cardKey").classList.toggle("armed",
                                      !!s.key_armed && !s.key_running);
      }
      // 提权警告横幅：后端看门狗发现「前台是提权窗口而本进程普通权限」
      // （注入会被 UIPI 静默拦截）时置 elev_warn，这里显示横幅 +
      // 「以管理员身份重启」按钮（relaunchAdmin -> relaunch_as_admin）
      const eb = $("elevBan");
      if(eb) eb.classList.toggle("show", !!s.elev_warn);
    }catch(e){}
    // 捕获轮询放在 try 外面：捕获逻辑本身有 try，
    // 但万一它抛异常，不能连带把状态刷新一起中断
    try{ await pollTriggerCapture(); }catch(e){}
    // 最大化状态跟着主轮询走（每 700ms 一次，别太频繁）。
    // 【为什么要单独轮询而不是只听 resize】
    // 后端是 SetWindowPos 改矩形实现的最大化，不保证触发 WebView2 的
    // resize 事件；只听事件的话「最大化」按钮的图标不会变成还原图标，
    // 表现为「窗口已经是最大化的了，但按钮还显示最大化，再点没反应」。
    if(Date.now() - lastMaxCheck > 700){
      lastMaxCheck = Date.now();
      try{ await syncMaxIcon(); }catch(e){}
      // 自检条不用每 500ms 刷，跟着最大化一起 700ms 刷一次就够看了
      try{ await refreshRawDiag(); }catch(e){}
    }
  }

  window.addEventListener("pywebviewready", async () => {
    const bs = await window.pywebview.api.bootstrap();
    await initButtons(bs.buttons);
    // 先恢复上次的参数，再渲染按键筹码 ——
    // 顺序反了的话筹码会按默认值(w)渲染，之后又被输入框的值覆盖，
    // 界面上就会出现「输入框是 ctrl+w、筹码却只有 W」的不一致
    applySettings(bs.settings);
    renderKeyChips();
    // 暂停热键：Pause 优先，被占用会降级（后端已试过一串候选）
    $("hkPause").textContent = prettyPauseKey(bs.pause_key);
    // 热键显示统一交给 refreshHotkeys：它会同时显示
    // 「系统热键」和「鼠标键」两类，鼠标键优先（它不会被占用）
    await refreshHotkeys();
    syncMaxIcon();
    syncGap();
    // 数字输入框滚轮防护：
    // 滚轮滑过 CPS/次数/秒数输入框时，WebView 会把它当成「改值」，
    // 表现为「启动鼠标区后滚轮输入有点奇怪」的一半原因
    // （另一半是原始输入里的滚轮事件，后端已显式接住）。
    // 这里让滚轮焦点上的数字框直接失焦，值不动、页面正常滚。
    document.addEventListener("wheel", (e)=>{
      const t = e.target;
      if(t && t.tagName === "INPUT" && t.type === "number"){
        e.preventDefault();
        t.blur();
      }
    }, {passive:false});
    setInterval(poll, 500);
    poll();
    // 宏录制区（v27）：先拉一次已保存的宏，再起状态轮询。
    // 录制中窗口是最小化的，WebView 会节流 setTimeout，
    // 但用户一按 Pause 后端就把窗口叫回来，恢复后第一轮 poll 立刻追上。
    macRefreshList();
    macPoll();
    setInterval(macPoll, 700);
  });

  // 窗口关闭前再存一次（兜底）。
  window.addEventListener("beforeunload", ()=>{ saveSettings(); });

  /* 改动即存（去抖 400ms）。
     【为什么不能只靠 beforeunload】pywebview 关窗口的路子不止一条
     （自绘标题栏的关闭按钮、任务栏右键菜单、系统强制关闭），
     beforeunload 不一定每次都跑得到；用户改完 CPS 直接点 X
     就等于白改。所以改成「任何输入一变就排队写盘」，
     关窗前那次只是双保险。去抖是为了不把磁盘写得太吵。 */
  (function(){
    const ids = ["mBtn","mAct","mCount","mDur","mCps","mJitter",
                 "mMode","mPos","mPoints","mCd","mHide","mJig","mDone","mTray",
                 "kKeys","kThr","kHz","kMode","kGap"];
    let timer = null;
    const soon = () => { clearTimeout(timer); timer = setTimeout(saveSettings, 400); };
    ids.forEach(id => {
      const el = $(id);
      if(!el) return;
      el.addEventListener("input", soon);
      el.addEventListener("change", soon);
    });
    // 动作/位置模式变化时刷新联动显隐（v22）
    $("mAct").addEventListener("change", syncMouseRows);
    $("mMode").addEventListener("change", syncMouseRows);
    // 首屏也要刷一次：恢复设置前默认值就得是对的
    syncMouseRows();
  })();
  // ---- 配置导入 / 导出 / 剪贴板 / 多方案 ----
  /* 【为什么用 class 而不是 style.display】v19 的写法是把内联 display
     读出来和 block 比：CSS 没给默认值时内联是空串，第一次点会被判成
     「当前没开」→ 设成 block（其实早就开着）→ 要点两次才隐藏。
     ⚠ 注释里**不要**写出与旧代码逐字相同的串（例如三个等号的判法）——
     静态断言会把解释文字当成残留 bug（这个坑本项目已踩三次）。
     改用 classList + CSS 的 .open，显示状态只有一个来源。 */
  function toggleCfg(){
    const p = $("cfgPanel"); if(!p) return;
    p.classList.toggle("open");
    if(p.classList.contains("open")) refreshProfiles();
  }
  function cfgMsg(msg, err){
    const el = $("cfgMsg"); if(!el) return;
    el.textContent = msg || "";
    el.className = "cfg-msg" + (err ? " err" : "");
  }
  async function exportFile(){
    try{
      const r = await window.pywebview.api.export_config_file();
      if(r && r.ok){ cfgMsg("已导出：" + r.path); }
      else{ cfgMsg((r && r.msg) || "导出失败", true); }
    }catch(e){ cfgMsg("导出出错：" + e, true); }
  }
  async function copyClip(){
    try{
      const r = await window.pywebview.api.copy_config_clipboard();
      if(r && r.ok){ cfgMsg(r.msg || "已复制到剪贴板"); }
      else{ cfgMsg((r && r.msg) || "复制失败", true); }
    }catch(e){ cfgMsg("复制出错：" + e, true); }
  }
  async function importFile(input){
    const f = input.files && input.files[0];
    if(!f) return;
    try{ const text = await f.text(); await doImportText(text); }
    catch(e){ cfgMsg("读文件失败：" + e, true); }
    input.value = "";
  }
  async function pasteClip(){
    try{
      const r = await window.pywebview.api.paste_config_clipboard();
      if(r && r.ok){ applySettingsFromImport(r.msg); }
      else{ cfgMsg((r && r.msg) || "粘贴失败", true); }
    }catch(e){ cfgMsg("粘贴出错：" + e, true); }
  }
  async function doImportText(text){
    try{
      const r = await window.pywebview.api.import_config_text(text);
      if(r && r.ok){ applySettingsFromImport(r.msg); }
      else{ cfgMsg((r && r.msg) || "导入失败", true); }
    }catch(e){ cfgMsg("导入出错：" + e, true); }
  }
  // 导入/加载成功后：重新拉全量 settings 回填表单（trigger 不在导入范围，保持本机绑定）
  function applySettingsFromImport(msg){
    try{ window.pywebview.api.load_settings().then(s => applySettings(s)); }catch(e){}
    cfgMsg(msg || "已导入配置");
  }
  async function refreshProfiles(){
    try{
      const r = await window.pywebview.api.list_profiles();
      const sel = $("cfgProfileSel"); if(!sel) return;
      sel.innerHTML = '<option value="">— 选择方案 —</option>';
      if(r && r.ok && r.profiles){
        for(const name of r.profiles){
          const o = document.createElement("option");
          o.value = name; o.textContent = name; sel.appendChild(o);
        }
      }
    }catch(e){}
  }
  async function saveProfile(){
    const inp = $("cfgProfileName"); const name = inp && inp.value;
    if(!name || !name.trim()){ cfgMsg("请先输入方案名", true); return; }
    try{
      const r = await window.pywebview.api.save_profile(name.trim());
      if(r && r.ok){ cfgMsg(r.msg); refreshProfiles(); if(inp) inp.value = ""; }
      else{ cfgMsg((r && r.msg) || "保存失败", true); }
    }catch(e){ cfgMsg("保存出错：" + e, true); }
  }
  async function loadProfile(){
    const sel = $("cfgProfileSel"); const name = sel && sel.value;
    if(!name){ cfgMsg("请先选择一个方案", true); return; }
    try{
      const r = await window.pywebview.api.load_profile(name);
      if(r && r.ok){ applySettingsFromImport(r.msg); }
      else{ cfgMsg((r && r.msg) || "加载失败", true); }
    }catch(e){ cfgMsg("加载出错：" + e, true); }
  }
  async function delProfile(){
    const sel = $("cfgProfileSel"); const name = sel && sel.value;
    if(!name){ cfgMsg("请先选择一个方案", true); return; }
    try{
      const r = await window.pywebview.api.delete_profile(name);
      if(r && r.ok){ cfgMsg(r.msg); refreshProfiles(); }
      else{ cfgMsg((r && r.msg) || "删除失败", true); }
    }catch(e){ cfgMsg("删除出错：" + e, true); }
  }
</script>
</body>
</html>"""


_APP_MUTEX_NAME = "Local\\MouseAutoClicker_Dashboard_v1"
_app_mutex = None


# ======================================================================
# 启动日志
# ======================================================================
_LOG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "MouseAutoClicker", "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "launch.log")
_log_fp = None


def _log_open():
    """
    打开日志文件（追加写），失败也不影响程序启动。

    故意写到 %LOCALAPPDATA% 而不是 exe 旁边：
      · exe 可能在只读目录（Program Files）里，写不了；
      · 和 exe 放一起，用户拷 exe 时容易把日志一起拷走、
        或者误以为日志是多余文件删掉。
    """
    global _log_fp
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        _log_fp = open(_LOG_FILE, "a", encoding="utf-8", buffering=1)
        return True
    except Exception:
        _log_fp = None
        return False


def _log(msg):
    """写一行日志：时间戳 + 内容。永远不抛异常。"""
    try:
        if _log_fp is None and not _log_open():
            return
        _log_fp.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _log_exc(where):
    """记录当前异常 + 完整堆栈。堆栈是定位问题的关键，不能省。"""
    import traceback
    try:
        _log("%s 时出错：\n%s" % (where, traceback.format_exc()))
    except Exception:
        pass


def _log_trim(max_bytes=2 * 1024 * 1024, keep_lines=400):
    """
    日志超过 2MB 就只留最后 400 行。

    【为什么不做成无限追加】这个程序可能一天启动几十次，
    不加限制的话半年后日志会有几十 MB，打开都卡。
    而且真正有用的永远是最近几次启动。
    """
    try:
        if not os.path.exists(_LOG_FILE):
            return
        if os.path.getsize(_LOG_FILE) < max_bytes:
            return
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-keep_lines:]
        with open(_LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


def _log_environment():
    """
    记一次「这台机器是什么情况」。

    排查时最花时间的往往不是代码，而是搞不清用户的机器长什么样。
    把这些一次性写清楚，用户直接把日志发过来就够了。
    """
    _log("-" * 60)
    _log("启动一次新的实例")
    _log("  DPI 感知     : %s" % _dpi_state)
    _log("  可执行文件   : %s" % sys.executable)
    _log("  工作目录     : %s" % os.getcwd())
    _log("  脚本路径     : %s" % (os.path.abspath(__file__)
                                  if "__file__" in globals() else "?"))
    try:
        import platform
        _log("  Python       : %s" % platform.python_version())
        _log("  系统         : %s %s (build %s)"
             % (platform.system(), platform.release(), platform.version()))
    except Exception:
        pass
    # 打包态判断：frozen 说明是 PyInstaller 的单文件 exe，
    # 这决定了后面「找不到文件」类的报错该怎么解读
    _log("  运行方式     : %s" % ("打包 exe" if getattr(sys, "frozen", False)
                                  else "源码"))
    # 界面设置（ui_settings.json）的真实路径。
    # 2026-10-16 修「绑好的键关掉重开又变回默认」时加的自检行：
    # 打包后 __file__ 指向 %TEMP%\\_MEIxxxxx\\（每次启动都换目录），
    # 设置文件必须落在 exe 同目录才不会丢。这行直接打真实路径，
    # 以后再有「设置没记住」的反馈，看这行就知道路径对不对。
    try:
        _log("  界面设置文件 : %s" % _settings_path())
    except Exception as e:
        _log("  界面设置文件 : 解析失败 %r" % e)
    try:
        _log("  命令行       : %s" % " ".join(sys.argv))
    except Exception:
        pass
    # 通知弹窗自检：2026-10-16 v13~v14 实测「没有通知」，根因是
    # PostThreadMessageW 投的线程消息 DispatchMessageW 送不到窗口过程，
    # 且 toast 全链路静默无日志。这里留一行状态，用户报「没通知」时
    # 先看这行区分「模块没进去」还是「运行时问题」。
    try:
        _log("  通知弹窗     : %s"
             % ("模块就绪（user32 可用）" if toast._user32 is not None
                else "user32 不可用（非 Windows 环境？）"))
    except Exception as e:
        _log("  通知弹窗     : 自检失败 %r" % e)


def _ensure_single_instance():
    """
    保证同时只有一个实例在跑。

    【为什么要加这条】
    实测出现过两个进程同时运行。两个实例的坏处是实打实的：
      · 抢同一批系统热键 —— 后注册的会失败，但前端可能还显示"已绑定"，
        于是用户按了没反应，却查不出原因；
      · 只要有一个没退干净，exe 就被占着删不掉、覆盖不了，
        只能重启电脑。

    【为什么用互斥量而不是遍历进程列表】
    互斥量由内核维护，进程被强杀 / 崩溃时内核会自动释放它，
    不会像"自己写个 pid 文件"那样留下假的"已运行"状态把人锁死在门外。

    返回 True = 我是第一个实例，继续启动；False = 已经有实例在跑了。
    """
    global _app_mutex
    ERROR_ALREADY_EXISTS = 183
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL,
                                          wt.LPCWSTR]
        kernel32.CreateMutexW.restype = wt.HANDLE
        h = kernel32.CreateMutexW(None, False, _APP_MUTEX_NAME)
        if not h:
            return True          # 建不出来就别拦，宁可放行也别把用户锁死
        _app_mutex = h           # 故意不释放：进程活着它就要一直有效
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _focus_existing_window():
    """
    把已经跑着的那个实例顶到前台。

    没有这一步的话，用户第二次双击 exe 会「什么都没发生」，
    只会以为程序坏了，然后开第三个、第四个……
    """
    try:
        hwnd = user32.FindWindowW(None, "爪Pro")
        if hwnd:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def main():
    _fix_console()

    # 【DPI 感知·2026-10-16】必须在建窗口之前。
    # 不设的话系统会把坐标按 96dpi 虚拟化：缩放 150% 的屏幕上，
    # GetCursorPos 拿到的 (100,100) 实际落在物理 (150,150)。
    # 纯「点当前位置」感知不到，但固定坐标 / 宏 / 以后的鼠标录制
    # 回放整体偏移。详见 core/dpi.py 的说明。
    # 【坑·v14】_dpi_state 必须是模块级全局：_log_environment() 要读它，
    # 写成 main() 的局部变量会 NameError，exe 启动即崩
    # （日志停在「启动一次新的实例」后面什么都没有）。
    # 真机点一遍 exe 才发现 —— 打桩测试覆盖不到这种跨函数作用域。
    global _dpi_state
    try:
        from core import dpi as _dpi
        _dpi_state = _dpi.enable_dpi_awareness()
    except Exception:
        _dpi_state = "设置异常（保持旧行为）"

    # 日志必须第一个开：后面任何一步崩了都要能留下线索。
    # 【它还有一个诊断作用】如果连这个日志文件都没生成，
    # 说明进程压根没跑起来 —— 那就是被系统层面拦了
    # （Smart App Control / SmartScreen / Defender），
    # 不是程序的问题。这是区分「程序坏了」和「系统不放行」的关键判据。
    _log_trim()
    _log_environment()

    if not _ensure_single_instance():
        _log("检测到已有实例在运行，本次不启动第二个（仅把旧窗口顶到前台）")
        _focus_existing_window()
        print("[提示] 程序已经在运行，已把窗口切到前台。")
        return 0

    api = Api()

    try:
        start_mouse_listener()
    except Exception:
        _log_exc("start_mouse_listener")
        raise
    # 记一下原始输入监听的当前状态：以后界面自检条看不出问题时，
    # 日志里这一行能直接说明「注册成功了没有」
    try:
        _log("鼠标原始输入监听已启动：%s" % (api.raw_diag(),))
    except Exception:
        _log_exc("raw_diag")

    # 热键要在窗口创建前注册好：RegisterHotKey 抢的是系统级按键，
    # 晚于网页加载注册会让用户「刚打开界面按 F5 没反应」。
    # 【先把异常上报钩子挂上】core/hotkey 不能反向 import dashboard，
    # 回调里的异常只有在挂了这个钩子之后才会写进 launch.log；
    # 否则炸了只剩 stderr 一行堆栈，游戏里根本看不到
    # （2026-10-16 pausctl 拼错那个坑就是这么藏了大半天）。
    try:
        hk.set_error_logger(_log_exc)
    except Exception:
        pass
    try:
        api.register_hotkeys()
        _log("系统热键与鼠标键触发器已注册")
    except Exception:
        _log_exc("register_hotkeys")
        # 热键注册失败不致命：界面还能用，只是没快捷键
        print("[警告] 热键注册失败，界面仍可使用。详情见日志。")

    try:
        import webview
    except ImportError as e:
        _log("致命：缺少 pywebview -> %s" % e)
        print("[打包错误] 缺少 pywebview，请用带 webview 的解释器运行或先 pip install pywebview")
        return 1

    html_path = os.path.join(HERE, "ui", "dashboard.html")
    os.makedirs(os.path.dirname(html_path), exist_ok=True)
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(HTML)
        _log("界面文件已写出：%s（%d 字节）"
             % (html_path, os.path.getsize(html_path)))
    except Exception:
        _log_exc("写出界面文件 dashboard.html")
        raise

    try:
        win = webview.create_window(
            "爪Pro",
            url=("file://" + html_path.replace("\\", "/")),
            js_api=api,
            width=1180, height=820,
            min_size=(900, 700),
            background_color="#06030D",
            text_select=False,
            frameless=True,        # 自绘标题栏，见_apply_frameless
            # 【坑·2026-10-17 复核发现】frameless 时 pywebview **默认开启**
            # easy_drag：它会给 window 挂一个 mousedown，之后任何鼠标移动
            # 都调内部的 pywebviewMoveWindow → winforms 侧 SetWindowPos 的
            # cx/cy 收到 None → 每次拖动都往 stderr 刷一条 traceback。
            # 我们的标题栏拖拽是自研的（ReleaseCapture + WM_NCLBUTTONDOWN，
            # 见 win_drag_begin），与 easy_drag 无关，关掉不影响拖动，
            # 只是不让 pywebview 的那条坏链路刷噪声。
            easy_drag=False,
        )
        _log("窗口已创建")
    except Exception:
        _log_exc("create_window")
        raise
    # 【坑·2026-10-15 实测】必须是 api._window（下划线开头），
    # 绝不能是 api.window：pywebview 6.x 的 util.get_functions 在生成
    # JS 桥时会递归展开 js_api 上每个**公开**的非函数对象属性
    # （跳过 _ 开头），且没有环检测——Window 对象经
    # native → AccessibilityObject → Bounds → Empty → Empty → ...
    # 无限自引用，递归爆 RecursionError；桥接线程异常后 loaded 事件
    # 不触发，主线程死等 → 窗口「无响应」30 秒被系统强杀（AppHangB1）。
    # 2026-10-15 v9 实测三次全中；窗口句柄请走 api_ref（_win_hwnd），
    # 别把 Window 对象挂到 Api 的公开属性上。
    api._window = win

    # 窗口被关掉时注销热键，避免 F5 被一个看不见的进程占着。
    # 同时挂上退出看门狗：清理卡死（SendInput 永不返回）时强制退出。
    #
    # 【v24 S1】勾了「关闭到托盘」时，X 不是退出而是隐藏：
    # pywebview 的 closing 事件返回值 True = 取消关闭（看
    # platforms/winforms.py 的 on_closing：should_cancel 直接写进
    # args.Cancel）。所以「藏还是退」的判断放在 _on_window_closing 里
    # （模块级，可被探针直接测），这里只是把 api 绑进去。
    try:
        def _on_closing():
            return _on_window_closing(api)

        win.events.closing += _on_closing
    except Exception:
        _log_exc("挂 closing 事件")

    # 【v24 S1】托盘图标：放在窗口创建之后（托盘菜单要把窗口叫回来）、
    # webview.start() 之前（早于主循环挂上，用户第一时间能看到）。
    try:
        _tray_start(api)
    except Exception:
        _log_exc("_tray_start")

    # 无边框窗口：自己画标题栏。
    # 为什么不用系统标题栏：系统那条是亮色 Win10 风格，和整体的
    # 深色（#06030D）完全不搭，而且「未响应」时它会额外显示
    # 括号说明，视觉上更突兀。改成自绘后整窗统一风格。
    try:
        _apply_frameless(win)
        _log("自绘标题栏已应用")
    except Exception:
        _log_exc("_apply_frameless")
        # 标题栏画失败只是样子难看，不该拦住启动

    _log("进入 webview.start() 消息循环（界面即将显示）")
    try:
        webview.start()
    except Exception:
        _log_exc("webview.start")
        raise
    finally:
        # 退出收尾同样挂看门狗。
        # 正常情况下 shutdown() 很快返回，随后 os._exit(0) 立刻结束进程，
        # 不等任何残留线程 —— 这正是"绝不留僵尸进程"的关键。
        _arm_exit_watchdog(8.0)
        try:
            api.shutdown()
        except Exception:
            pass
        _log("进程退出")
        os._exit(0)
    return 0


# ======================================================================
# 无边框窗口支持
# ======================================================================
_dwm = None
try:
    _dwm = ctypes.windll.dwmapi
except Exception:
    _dwm = None


def _apply_frameless(win):
    """
    去掉系统标题栏，并补上自己缺的那部分能力：
      1. 圆角 + 阴影 —— DWM 圆角在 Win11 上原生可用，Win10 要手动开
      2. 拖动窗口 —— 标题栏拖动时走 WM_NCLBUTTONDOWN，
         这是唯一能让 Windows 正确处理拖动/贴边分屏/Aero Snap 的路子
      3. 真正的窗口句柄 —— pywebview 不直接暴露，要从native 层取
      4. **补回 WS_THICKFRAME / WS_MAXIMIZEBOX / WS_SYSMENU**
         见下面 _restore_styles 的详细说明，这是最大化无效的根因
    """
    def _hwnd():
        # pywebview 的 window.native 是 WinForms 控件，.Handle 才是 HWND
        n = getattr(win, "native", None)
        if n is None:
            return 0
        for attr in ("Handle", "hwnd"):
            v = getattr(n, attr, None)
            if v:
                try:
                    return int(v.ToInt64()) if hasattr(v, "ToInt64") else int(v)
                except Exception:
                    pass
        return 0

    def _init():
        h = _hwnd()
        if not h:
            return
        # ---- 圆角 + 深色标题阴影 ----
        if _dwm:
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33，值 2 = 小圆角
            corner = ctypes.c_int(2)
            try:
                _dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(h), ctypes.c_int(33),
                    ctypes.byref(corner), ctypes.sizeof(corner))
            except Exception:
                pass
        # 补样式位（THICKFRAME 等，缩放循环要用）+ 子类化压掉
        # THICKFRAME 的白边 + 接管外部最大化状态同步
        _ensure_frameless(h)
        # ---- 拖动支持 ----
        # 记录按下位置，JS 里mousedown 时先通知一下，
        # mousemove 超过阈值才真正发起系统拖动（否则点一下就拖走窗口）
        _drag_state["armed"] = False
        _drag_state["down"] = (0, 0)

    try:
        win.events.loaded += lambda: _init()
    except Exception:
        pass
    # ----兜底：loaded 事件偶尔不触发（页面加载失败/极快关闭），
    # 那时窗口就一直没有 WS_THICKFRAME，表现为「边框拖不动」。
    # 所以再挂一个短延时重试，拿到句柄就补样式。
    def _retry():
        for _ in range(20):
            h = _hwnd()
            if h:
                try:
                    style = user32.GetWindowLongW(h, GWL_STYLE)
                    # 已有 THICKFRAME 说明 loaded 那条已生效，不用重复做
                    if style & WS_THICKFRAME:
                        return
                except Exception:
                    pass
                _ensure_frameless(h)
            time.sleep(0.15)
    try:
        win.events.shown += lambda: threading.Thread(
            target=_retry, daemon=True).start()
    except Exception:
        pass

    api_ref["win"] = win


# ---- 窗口样式位（Win32 常量，写成名字是为了可读） ----
GWL_STYLE = -16
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000      # WS_BORDER | WS_DLGFRAME
WS_THICKFRAME = 0x00040000   # 可缩放窗口的关键位
WS_SYSMENU = 0x00080000      # 有系统菜单
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

# ---- 子类化窗口过程要用的常量和声明 ----
GWLP_WNDPROC = -4
WM_NCCALCSIZE = 0x0083
WM_SIZE = 0x0005
WM_WINDOWPOSCHANGED = 0x0047
SIZE_RESTORED = 0
SIZE_MAXIMIZED = 2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SW_RESTORE = 9               # ShowWindow：清掉系统 zoomed 状态

user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
# 硬规则 2：不声明 argtypes 时 64 位 HWND 会被截断成 32 位，
# IsZoomed 会对着一个不存在的窗口句柄返回 False —— 静默失效。
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsZoomed.restype = wt.BOOL
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
user32.CallWindowProcW.restype = ctypes.c_ssize_t

# 窗口过程回调类型。【防 GC】和 _create_message_window 里同一个道理：
# ctypes 回调没有强引用会被回收，系统再调用时函数指针已失效 → 进程直接崩。
# 所以建完必须存进 _subclass_state。
_FRAMELESS_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND,
                                        ctypes.c_uint, ctypes.c_size_t,
                                        ctypes.c_ssize_t)


def _restore_styles(h):
    """
    把 frameless 时丢掉的窗口样式位补回来，重点是 WS_THICKFRAME。

    【实测出来的样式位】frameless 窗口实际是 0x16010000，
    即 WS_VISIBLE | WS_CLIPSIBLING | WS_CLIPCHILDREN | WS_MAXIMIZEBOX，
    缺的正是 WS_THICKFRAME / WS_SYSMENU / WS_MINIMIZEBOX。

    【为什么必须有 WS_THICKFRAME】
    它是「本窗口可缩放」的声明位。缺它时：
      ·鼠标移到边缘不会出现双向光标
      · DefWindowProc 不接受 SC_SIZE 系列命令，
        所以拖边框完全没反应
    前端的8 向热区最终是靠 WM_NCLBUTTONDOWN 发起系统缩放循环，
    而那个循环同样要 THICKFRAME 才认（probe_border3 候选 A 实测：
    去掉 THICKFRAME 后 win_resize_begin 毫无反应，宽度变化 0）。

    【THICKFRAME 的白边问题怎么解决】
    THICKFRAME 会让系统在窗口四周预留约 7px 的非客户边框（Win10 上
    这条边框是白色的），布局重算一次就实体化——probe_border3 实测：
    SetWindowLongW 补样式后边框还是 0（布局陈旧），但一旦
    SWP_FRAMECHANGED / 最大化 / 缩放触发重算，四边全部变成 7px，
    还原后也不消失。这就是「最上方白边」的根因。
    解决办法见 _subclass_frameless：子类化 WM_NCCALCSIZE 把非客户区
    压成 0，THICKFRAME 留着（缩放循环要继续用），边框再也不出现。

    【为什么不加 WS_CAPTION】
    加了会重新出现系统原生标题栏，和我们自绘的那条叠在一起
    （两个标题栏 + 我们的圆角全被系统标题栏占掉）。
    所以只补样式位，不补标题栏。

    注：最大化已经不依赖这里了（见 win_toggle_maximize，
    那边自己算矩形，不看样式位）。这里只负责「能不能拖边框缩放」。
    """
    try:
        style = user32.GetWindowLongW(h, GWL_STYLE)
        want = style | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX \
            | WS_MAXIMIZEBOX
        want &= ~WS_CAPTION
        if want != style:
            user32.SetWindowLongW(h, GWL_STYLE, want)
        return want
    except Exception:
        return 0


# ======================================================================
# frameless 窗口子类化：压掉 THICKFRAME 的非客户边框 + 同步最大化状态
# ======================================================================
# 我们自己正在改窗口矩形（_set_rect）时置位。SetWindowPos 是同步的，
# 消息在调用期间就派发完，所以这个旗标能精确框住「我们自己造成的」
# WM_SIZE / WM_WINDOWPOSCHANGED，把它们和用户/系统造成的区分开。
_setting_rect = {"flag": False}

# 「非最大化状态」下最后记住的窗口矩形。
# 【为什么需要它】外部最大化（Win+Up、拖到屏幕顶部、SW_MAXIMIZE）
# 是系统直接改窗口矩形，不会问过我们。此时 _norm_rect 里还是过期数据，
# 用户点「还原」就会走最大化分支，把当前的全屏矩形当成小矩形存下来
# —— 还原后还是全屏，正是「还原后大小几乎不变」的另一种触发路径。
# 有了这份记忆，外部最大化后 _norm_rect 立刻被修正成真正的小矩形。
_last_free_rect = {"x": 0, "y": 0, "w": 0, "h": 0, "ok": False}

# 子类化只做一次。重复 SetWindowLongPtrW 会把旧的过程层层套住，
# 每层都多做一次无用功，还多一处崩溃风险。
_subclass_state = {"hwnd": 0, "old_proc": 0, "cb": None}


def _get_rect(h):
    """取窗口矩形，返回 (x, y, w, h)；失败返回 None。"""
    try:
        r = wt.RECT()
        if user32.GetWindowRect(h, ctypes.byref(r)):
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        pass
    return None


def _rect_covers_work_area(h):
    """
    窗口矩形是否盖住整个工作区。

    为什么用「盖住」而不是「相等」：我们自己的最大化把矩形设成
    刚好等于工作区；而系统最大化（Win+Up / snap）会在四边各多留
    一个边框厚度，让客户区正好铺满工作区。两种都要认。
    """
    wa = _work_area(h)
    if not wa:
        return False
    r = _get_rect(h)
    if not r:
        return False
    x, y, w, ht = r
    return (x <= wa[0] + 3 and y <= wa[1] + 3
            and x + w >= wa[0] + wa[2] - 3
            and y + ht >= wa[1] + wa[3] - 3)


def _remember_free_rect(h):
    """记住当前窗口矩形（非最大化状态）。尺寸异常小就不要，防脏数据。"""
    r = _get_rect(h)
    if r and r[2] > 200 and r[3] > 200:
        _last_free_rect.update(x=r[0], y=r[1], w=r[2], h=r[3], ok=True)


def _on_window_pos_changed(hwnd):
    """
    WM_WINDOWPOSCHANGED：窗口位置/大小变了（拖动、缩放、系统 snap 都会发）。

    两种正事：
      1. 我们以为在最大化（_norm_rect["valid"]），但窗口已经不盖住
         工作区了 → 发生了「外部还原」（Win+Down / 从顶部拖离），
         清掉标记，前端按钮才能变回「最大化」图标
      2. 自由状态 → 记住这个矩形，万一下一刻被外部最大化，
         这就是要还原回去的目标

    【为什么自由矩形要跳过「盖住工作区」的尺寸】
    probe_visual 实测：外部最大化时 WM_WINDOWPOSCHANGED 比
    WM_SIZE(SIZE_MAXIMIZED) 先到，此刻窗口矩形已经是全屏尺寸。
    不防这一步，全屏矩形就会被当成还原目标存进 _last_free_rect，
    接着 WM_SIZE 同步出去 —— 正是要防的「还原后大小几乎不变」。
    """
    if _setting_rect["flag"]:
        return
    if _norm_rect["valid"]:
        if not _rect_covers_work_area(hwnd):
            _norm_rect["valid"] = False
        return
    if not _rect_covers_work_area(hwnd):
        _remember_free_rect(hwnd)


def _on_window_size(hwnd, wparam):
    """
    WM_SIZE：SIZE_MAXIMIZED = 被外部最大化；SIZE_RESTORED = 还原/变小。

    外部最大化（Win+Up / 贴边 / SW_MAXIMIZE）时系统不会问过我们，
    窗口矩形直接被改掉。这里把 _last_free_rect 里记住的小矩形
    存进 _norm_rect 并置 valid=True，用户点「还原」才能回到原来的大小。
    """
    if _setting_rect["flag"]:
        return
    if wparam == SIZE_MAXIMIZED:
        if not _norm_rect["valid"]:
            f = _last_free_rect
            if f["ok"]:
                _norm_rect.update(x=f["x"], y=f["y"], w=f["w"], h=f["h"],
                                  valid=True)
            else:
                # 从来没记住过自由矩形（用户从没移动/缩放过窗口）：
                # 用默认居中尺寸兜底，总比把全屏矩形存成还原目标好
                wa = _work_area(hwnd) or (0, 0, 1180, 820)
                _norm_rect.update(x=wa[0] + 60, y=wa[1] + 60,
                                  w=min(1180, wa[2] - 120),
                                  h=min(820, wa[3] - 120), valid=True)
    elif wparam == SIZE_RESTORED:
        if _norm_rect["valid"] and not _rect_covers_work_area(hwnd):
            _norm_rect["valid"] = False


def _frameless_msg(hwnd, msg, wparam, lparam, old_proc):
    """
    子类化窗口过程的消息分发本体。

    抽成模块级函数是为了能单测（闭包没法直接调）。
    调用方负责 try/except 兜底。
    """
    if msg == WM_NCCALCSIZE:
        # 【白边修复的核心】直接 return 0 不动 rgrc[0]，
        # 系统就把整个窗口当成客户区，THICKFRAME 的 7px 边框被压掉。
        # wParam=TRUE / FALSE 两种形态都不碰。
        return 0
    if msg == WM_WINDOWPOSCHANGED:
        _on_window_pos_changed(hwnd)
    elif msg == WM_SIZE:
        _on_window_size(hwnd, int(wparam))
    return user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)


def _subclass_frameless(h):
    """
    子类化 frameless 主窗口，接管三类消息：

    1. WM_NCCALCSIZE —— 【白边修复的核心】
       实测（probe_border3）：wParam=TRUE 时系统传进来的 rgrc[0]
       就是整个窗口矩形；直接 return 0 一个字节都不 inset，
       系统就把「整个窗口」当成客户区。WS_THICKFRAME 预留的
       7px 非客户边框被压掉，白边在任何状态（正常/移动/缩放/
       最大化/还原/snap）下都不再出现。
       wParam=FALSE 时 lparam 直接是 RECT*，同样不动它。
       （Chromium/Electron 的 frameless 窗口用的就是这套机制）

    2. WM_WINDOWPOSCHANGED / WM_SIZE —— 同步外部最大化/还原状态，
       见 _on_window_pos_changed / _on_window_size。

    返回 True 表示已接管；幂等，同一个窗口只子类化一次。
    """
    if not h:
        return False
    if _subclass_state["hwnd"] == h and _subclass_state["cb"] is not None:
        return True
    try:
        old_proc = user32.GetWindowLongPtrW(h, GWLP_WNDPROC)
        if not old_proc:
            return False

        def _wndproc(hwnd, msg, wparam, lparam):
            # 窗口过程里抛异常会直接掐断整条消息链（表现为界面卡死），
            # 所以任何意外都退回默认处理，绝不让弹窗/边框逻辑带崩主循环
            try:
                return _frameless_msg(hwnd, msg, wparam, lparam, old_proc)
            except Exception:
                return user32.CallWindowProcW(old_proc, hwnd, msg,
                                              wparam, lparam)

        cb = _FRAMELESS_WNDPROC(_wndproc)
        user32.SetWindowLongPtrW(h, GWLP_WNDPROC,
                                 ctypes.cast(cb, ctypes.c_void_p).value)
        _subclass_state.update(hwnd=h, old_proc=old_proc, cb=cb)
        # 记住安装时的自由矩形：万一用户从没移动过窗口就外部最大化，
        # 也有个靠谱的还原目标
        _remember_free_rect(h)
        # 立刻让系统按新窗口过程重算一次边框，把状态稳定下来
        user32.SetWindowPos(h, None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        _log_exc("_subclass_frameless")
        return False


def _unsubclass_frameless():
    """退出时把窗口过程还回去（进程马上就 os._exit，主要是为了干净）。"""
    s = _subclass_state
    if s["hwnd"] and s["cb"] is not None and s["old_proc"]:
        try:
            user32.SetWindowLongPtrW(s["hwnd"], GWLP_WNDPROC,
                                     s["old_proc"])
        except Exception:
            pass
    s.update(hwnd=0, old_proc=0, cb=None)


def _ensure_frameless(h):
    """
    补齐 frameless 窗口缺的样式位 + 子类化压边框。

    loaded 事件和兜底重试两条路都走这里，保证：
      · _restore_styles 幂等（只补缺的位）
      · _subclass_frameless 幂等（同一个窗口只接管一次）
    """
    if not h:
        return
    _restore_styles(h)
    _subclass_frameless(h)


_drag_state = {"armed": False, "down": (0, 0), "dragging": False}
api_ref = {}

# ---- 最大化 / 还原：自己算矩形 ----
# 保存「最大化之前」的窗口矩形，还原时用。
# 为什么要自己存： frameless 窗口没有 WS_THICKFRAME，
# 系统那条「最大化到工作区」的路走不通，只能自己记住原位置。
_norm_rect = {"x": 0, "y": 0, "w": 0, "h": 0, "valid": False}


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32),
                ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT),
                ("dwFlags", ctypes.c_uint32)]


def _work_area(h):
    """
    取窗口所在显示器的工作区（=屏幕去掉任务栏）。

    用 MonitorFromWindow 而不是 GetSystemMetrics：
    后者只能拿到主显示器的尺寸，多显示器时窗口在副屏就会算错。
    """
    MONITOR_DEFAULTTONEAREST = 2
    mon = user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None
    r = mi.rcWork
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def win_is_maximized(h):
    """
    判断是否处于「最大化」。

    不用 IsZoomed：它读的是系统维护的 maximized 标志位，
    而我们是SetWindowPos 改的矩形，标志位不会跟着变，
    IsZoomed 会永远返回 False —— 表现为「能最大化但按钮图标不变、
    再点一次没反应」。所以改成比对矩形和屏幕工作区。
    """
    if not h:
        return False
    wa = _work_area(h)
    if not wa:
        return False
    r = wt.RECT()
    if not user32.GetWindowRect(h, ctypes.byref(r)):
        return False
    # 允许 2 像素误差：DPI 缩放/边框会让整数坐标差一两个像素，
    # 卡得太死会���「看起来是最大化的，但状态判断成没最大化」
    return (abs(r.left - wa[0]) <= 2 and abs(r.top - wa[1]) <= 2
            and abs((r.right - r.left) - wa[2]) <= 2
            and abs((r.bottom - r.top) - wa[3]) <= 2)


def _covers(x, y, w, h):
    """给一组矩形数值判断「是否盖住工作区」（和 _rect_covers_work_area 同口径）。"""
    wa = _work_area(_win_hwnd())
    if not wa:
        wa = (0, 0, 1920, 1080)
    return (x <= wa[0] + 3 and y <= wa[1] + 3
            and x + w >= wa[0] + wa[2] - 3
            and y + h >= wa[1] + wa[3] - 3)


def _default_free_rect(h):
    """居中默认尺寸（兜底用），保证一定不盖住工作区。"""
    wa = _work_area(h) or (0, 0, 1920, 1080)
    return (wa[0] + 60, wa[1] + 60,
            min(1180, max(320, wa[2] - 120)),
            min(820, max(240, wa[3] - 120)))


def _free_target(h):
    """
    挑一个「还原目标」矩形。

    【铁律】绝不返回盖住工作区的矩形。
    2026-10-16 主人反馈「最大化以后再点还原，窗口没有变化」——
    成因就是还原目标本身是全屏矩形（状态位一丢，最大化分支会把当前的
    全屏矩形当成"原来的小窗口"存下来），SetWindowPos 到同样的尺寸
    当然看不出任何变化。宁可退回默认居中尺寸，也不许还全屏。
    """
    wa = _work_area(h) or (0, 0, 1920, 1080)
    cands = [(_norm_rect["x"], _norm_rect["y"],
              _norm_rect["w"], _norm_rect["h"]),
             (_last_free_rect["x"], _last_free_rect["y"],
              _last_free_rect["w"], _last_free_rect["h"])]
    for x, y, w, ht in cands:
        if w > 200 and ht > 200 and not _covers(x, y, w, ht):
            return (x, y, w, ht)
    return _default_free_rect(h)


def win_toggle_maximize(h):
    """
    最大化 <-> 还原。返回 {"ok":…, "maximized":…}。

    【状态为什么用 _norm_rect["valid"] 而不是比对矩形】
    以前用「比对窗口矩形和工作区」判断当前是否最大化，实测出了个
    很难缠的问题：用户点「还原」后，窗口大小几乎不变。

    根因是这条链路：
      1. 前端每 700ms 轮询 is_maximized()，比对矩形 → 显示「还原」图标
      2. 用户点「还原」，后端又比对一次
      3. 这一次因为 DPI 抖动 / 窗口正在做动画 / 边框像素差，
         比对结果变成「不是最大化」→ 走了**最大化**分支
      4. 而最大化分支会把**当前矩形**（此刻已经是最大化的矩形）
         存进 _norm_rect，把真正的小窗口矩形**覆盖掉**
      5. 然后再 SetWindowPos 到工作区 —— 窗口当然还是那么大
      6. 更要命的是原矩形已经丢了，之后再点还原也是这个大矩形

    也就是说：**一次误判就造成永久性的状态污染**。

    改用 _norm_rect["valid"] 后，状态完全由我们自己维护
    （最大化时置 True，还原时置 False），前后端读的是同一个值，
    不存在竞态，也不受 DPI 和动画影响。

    【2026-10-16 加固：让上面那类污染彻底不可能】
    状态位是靠消息（WM_WINDOWPOSCHANGED / WM_SIZE）养的，任何一条
    意外消息（外部最大化、显示变化、别的进程改我们的窗口）都可能把它
    打回 False，而窗口其实还全屏。原来的代码在这时会把**全屏矩形**
    当还原目标存下来 —— 于是「点还原没反应」。
    现在三道保险：
      ① 最大化分支：当前矩形若已盖住工作区，就不拿它当还原目标
         （优先 _last_free_rect，再退默认居中尺寸）
      ② 还原分支：先用 SW_RESTORE 清掉系统的 zoomed 状态
         （系统最大化过的窗口带着 WS_MAXIMIZE，只 SetWindowPos 的话
          状态位还在，实测 IsZoomed 会一直是 True，以后还会自己弹回全屏）
      ③ 还原后复核：矩形若仍然盖住工作区，说明这次 SetWindowPos 没生效，
         立刻用兜底尺寸再来一次，并把现场写进 launch.log
    """
    if not h:
        return {"ok": False, "maximized": False}
    if _get_rect(h) is None:
        # 无效句柄（窗口已销毁等）：安静失败，别拿它去 SetWindowPos
        return {"ok": False, "maximized": False}
    before = _get_rect(h)
    if _win_is_maximized(h):
        # ---- 还原 ----
        try:
            if user32.IsZoomed(h):
                user32.ShowWindow(h, SW_RESTORE)
        except Exception:
            _log_exc("SW_RESTORE")
        x, y, w, ht = _free_target(h)
        ok = _set_rect(h, x, y, w, ht)
        after = _get_rect(h)
        # 复核：还原后还盖着工作区 = 没生效，兜底再来一次
        if _rect_covers_work_area(h):
            dx, dy, dw, dh = _default_free_rect(h)
            _log("还原未生效（ok=%s 目标=%s 实际=%s），改用兜底尺寸 %s"
                 % (ok, (x, y, w, ht), after, (dx, dy, dw, dh)))
            _set_rect(h, dx, dy, dw, dh)
            after = _get_rect(h)
        _norm_rect["valid"] = False
        _log("窗口还原：%s -> %s" % (before, after))
        return {"ok": True, "maximized": False,
                "rect": after}
    # ---- 最大化：先记住当前矩形，还原时要用 ----
    cur = before
    if cur and not _rect_covers_work_area(h) \
            and cur[2] > 200 and cur[3] > 200:
        _norm_rect.update(x=cur[0], y=cur[1], w=cur[2], h=cur[3], valid=True)
    else:
        # 当前矩形已经盖住工作区（状态位丢过，其实还全屏）：
        # 拿它当还原目标 = 还原后还是全屏，绝不能这么干
        fx, fy, fw, fh = _free_target(h)
        _log("最大化时窗口已是全屏（rect=%s）→ 还原目标改用 %s"
             % (cur, (fx, fy, fw, fh)))
        _norm_rect.update(x=fx, y=fy, w=fw, h=fh, valid=True)
    wa = _work_area(h)
    if not wa:
        return {"ok": False, "maximized": False}
    _set_rect(h, wa[0], wa[1], wa[2], wa[3])
    _log("窗口最大化：%s -> %s（还原目标 %s）"
         % (before, _get_rect(h),
            (_norm_rect["x"], _norm_rect["y"],
             _norm_rect["w"], _norm_rect["h"])))
    return {"ok": True, "maximized": True}


def _set_rect(h, x, y, w, ht):
    """
    改窗口位置和大小。

    SWP_NOZORDER | SWP_NOACTIVATE：
      · 不改 Z 序——否则点最大化会让窗口跳到最前，
        把正在玩的游戏盖住，恰好和「启动后最小化让位」的思路打架
      · 不抢焦点——最大化不该打断用户当前的输入焦点

    【_setting_rect 旗标是干什么的】
    SetWindowPos 会同步派发 WM_SIZE / WM_WINDOWPOSCHANGED 给窗口过程。
    子类化那边要靠这些消息判断「外部最大化/还原」，但我们自己改矩形时
    也必须让它们被无视，否则「我们自己最大化」会被误判成外部事件，
    _norm_rect 状态立刻乱套。SetWindowPos 是同步调用，旗标刚好框住
    整个消息派发期，一条不漏也一条不多。
    """
    try:
        _setting_rect["flag"] = True
        try:
            return bool(user32.SetWindowPos(h, 0, int(x), int(y),
                                            int(w), int(ht),
                                            0x0004 | 0x0010))
        finally:
            _setting_rect["flag"] = False
    except Exception:
        return False


def _win_is_maximized(h=None):
    """
    当前是否处于最大化。**前端图标和 win_toggle_maximize 共用这一个判断。**

    【直接读 _norm_rect["valid"]】
    这是唯一权威的状态源：我们自己置 True/False，
    和 win_toggle_maximize 用的是同一个变量，所以
    「前端显示的图标」和「后端实际会走哪个分支」永远一致。

    【2026-10-16 加自愈：窗口实际盖住工作区就认最大化】
    状态位是靠 WM_WINDOWPOSCHANGED / WM_SIZE 养的，任何一条意外消息
    （外部最大化、显示变化、别的进程动我们的窗口）都可能把它打回
    False，而窗口其实还是全屏的 —— 此时：
      · 图标显示「最大化」而不是「还原」（状态不对）
      · 点一下会走**最大化**分支：SetWindowPos 到工作区 = 尺寸没变，
        看起来「点了没反应」（主人 2026-10-16 报的正是这个）
    所以这里以**实际矩形**为准来纠正状态位：盖住工作区就是最大化，
    顺手把 valid 补回 True，图标和下一步行为立刻都对了。
    """
    if h is None:
        h = _win_hwnd()
    if h and _rect_covers_work_area(h):
        _norm_rect["valid"] = True
    return bool(_norm_rect.get("valid"))

# ---- 边框缩放：方向 -> WM_NCHITTEST 命中测试常量 ----
# 这 8 个值是 Win32 定义的「非客户区命中测试」返回值，
# 发 WM_NCLBUTTONDOWN 时原样传进去，Windows 就知道往哪边缩。
HT_Topleft = 13
HT_TOP = 12
HT_TOPRIGHT = 14
HT_RIGHT = 11
HT_BOTTOMRIGHT = 17
HT_BOTTOM = 15
HT_BOTTOMLEFT = 16
HT_LEFT = 10

_RESIZE_HT = {
    "tl": HT_Topleft, "t": HT_TOP, "tr": HT_TOPRIGHT,
    "r": HT_RIGHT,
    "br": HT_BOTTOMRIGHT, "b": HT_BOTTOM, "bl": HT_BOTTOMLEFT,
    "l": HT_LEFT,
}


def _makelparam(x, y):
    """
    把两个 16 位坐标打包成 LPARAM（低字 = x，高字 = y）。

    【必须 & 0xFFFF】副屏在主屏左边时 screenX 是负数，
    Python 的 -100 & 0xFFFF = 0xFF9C，正是 Win32 要的补码 16 位；
    不掩一下会溢出到高字，x 和 y 搅在一起，点偏到别的地方。
    """
    return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)


# ======================================================================
# 自研拖动 / 缩放（v31）
# ======================================================================
# 【为什么不再「交还 Windows」】
# 原来走的是经典套路：ReleaseCapture + SendMessage(WM_NCLBUTTONDOWN,
# HTCAPTION / HTLEFT)。2026-10-17 用 `_diag_resize_live.py` 实测，两条路都是死的：
#   ① 窗口的 WM_NCHITTEST 在四边全返回 HTCLIENT —— 因为 _frameless_msg
#      对 WM_NCCALCSIZE return 0，非客户区宽度是 0，系统不认为这里有边框；
#   ② 消息发进去也进不了模态循环：WebView2 子窗口持有鼠标捕获，
#      系统的拖动/缩放循环收不到 mousemove，一进去就退出。
#      日志证据：返回 end=True、before/after 完全一样（0 位移）。
# 结论：不是参数写错（lParam 也补过屏幕坐标），是这条路在本窗口上不成立。
# 现在自己算：前端按住时把屏幕坐标源源送上来，后端 SetWindowPos。
# 代价是失去「拖到屏幕边缘自动分屏」；跟手、最小尺寸夹住、光标形状都在下面自己做。
_MIN_W, _MIN_H = 900, 700     # 与 create_window(min_size=) 保持一致

# 拖动的几何基准：按下那一刻的窗口矩形 + 鼠标屏幕坐标
_drag_geo = {"on": False, "hwnd": 0, "rect": (0, 0, 0, 0),
             "ox": 0, "oy": 0, "scale": 1.0}
# 缩放的几何基准：同上，多一个方向
_rz_geo = {"on": False, "edge": "", "hwnd": 0, "rect": (0, 0, 0, 0),
           "ox": 0, "oy": 0, "scale": 1.0}


def _win_rect(h):
    r = wt.RECT()
    if not h or not user32.GetWindowRect(h, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def win_drag_begin(x=None, y=None, scale=1.0):
    """
    JS: 标题栏 pointerdown。记下「窗口矩形 + 鼠标起点」。

    x/y 是 e.screenX / e.screenY（CSS 像素）；scale 是 devicePixelRatio。
    缩放屏上 CSS 像素 ≠ 物理像素，位移必须乘回去，否则 150% 缩放下
    窗口只走 2/3 的距离，越拖越落后于鼠标。
    """
    h = _win_hwnd()
    rect = _win_rect(h)
    if not rect:
        return {"ok": False, "msg": "取不到窗口句柄"}
    _drag_geo.update(on=True, hwnd=h, rect=rect,
                     ox=int(x or 0), oy=int(y or 0),
                     scale=float(scale or 1.0) or 1.0)
    return {"ok": True}


def win_drag_move(x, y):
    """
    JS: 标题栏 pointermove。后端自己搬窗口。

    【为什么留 4px 阈值】只点一下标题栏不许把窗口拖走，
    否则双击最大化之前窗口会先滑一下。
    """
    if not _drag_geo["on"]:
        return {"ok": True, "dragging": False}
    try:
        x, y = int(x), int(y)
        s = _drag_geo["scale"]
        dx = int(round((x - _drag_geo["ox"]) * s))
        dy = int(round((y - _drag_geo["oy"]) * s))
        if abs(dx) < 4 and abs(dy) < 4:
            return {"ok": True, "dragging": False}
        rx, ry, rw, rh = _drag_geo["rect"]
        _set_rect(_drag_geo["hwnd"], rx + dx, ry + dy, rw, rh)
        return {"ok": True, "dragging": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def win_drag_end():
    """JS: pointerup。清掉几何基准。"""
    _drag_geo["on"] = False
    return {"ok": True}


def win_resize_begin(edge, x=0, y=0, scale=1.0):
    """
    从窗口某条边/角开始拖拽缩放。

    edge 取 tl/t/tr/r/br/b/bl/l。前端按住后每动一次调 win_resize_move。
    """
    edge = str(edge or "").strip().lower()
    if edge not in _RESIZE_HT:
        return {"ok": False, "msg": "未知的缩放方向: %r" % (edge,)}
    h = _win_hwnd()
    rect = _win_rect(h)
    if not rect:
        return {"ok": False, "msg": "取不到窗口句柄"}
    if _win_is_maximized(h):
        # 最大化时边缘热区在 CSS 里已隐藏，这里再拦一道：
        # 从最大化状态拖边缘 = 先还原再缩放，行为反直觉
        return {"ok": False, "msg": "最大化状态下不能拖边缘，先还原"}
    _rz_geo.update(on=True, edge=edge, hwnd=h, rect=rect,
                   ox=int(x or 0), oy=int(y or 0),
                   scale=float(scale or 1.0) or 1.0)
    return {"ok": True, "edge": edge}


def win_resize_move(x, y, scale=None):
    """
    按住边缘拖动时调：算出新矩形并 SetWindowPos。

    【最小尺寸怎么夹】不是简单地把宽高抬到 900x700 ——
    拖左边/上边时窗口的「右/下边」是不动的，宽度不够时必须
    反过来把 x 往回推，否则窗口会一边变窄一边整体右移。
    """
    if not _rz_geo["on"]:
        return {"ok": True}
    try:
        x, y = int(x), int(y)
        s = _rz_geo["scale"]
        dx = int(round((x - _rz_geo["ox"]) * s))
        dy = int(round((y - _rz_geo["oy"]) * s))
        rx, ry, rw, rh = _rz_geo["rect"]
        edge = _rz_geo["edge"]

        nx, ny, nw, nh = rx, ry, rw, rh
        if "l" in edge:
            nw = rw - dx
        if "r" in edge:
            nw = rw + dx
        if "t" in edge:
            nh = rh - dy
        if "b" in edge:
            nh = rh + dy

        if nw < _MIN_W:
            if "l" in edge:
                nx = rx + rw - _MIN_W
            nw = _MIN_W
        if nh < _MIN_H:
            if "t" in edge:
                ny = ry + rh - _MIN_H
            nh = _MIN_H

        # 别让窗口被拖到屏幕外只剩一条边（左/上方向）
        wa = _work_area(_rz_geo["hwnd"])
        if wa:
            nx = _clamp(nx, wa[0] - nw + 80, wa[0] + wa[2] - 80)
            ny = _clamp(ny, wa[1], wa[1] + wa[3] - 48)
        _set_rect(_rz_geo["hwnd"], nx, ny, nw, nh)
        return {"ok": True, "w": nw, "h": nh}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def win_resize_end():
    """JS: pointerup。收尾并同步一次最大化图标状态。"""
    _rz_geo["on"] = False
    return {"ok": True}


def _win_hwnd():
    w = api_ref.get("win")
    if w is None:
        return 0
    n = getattr(w, "native", None)
    if n is None:
        return 0
    for attr in ("Handle", "hwnd"):
        v = getattr(n, attr, None)
        if v:
            try:
                return int(v.ToInt64()) if hasattr(v, "ToInt64") else int(v)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())