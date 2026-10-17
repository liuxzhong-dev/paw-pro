# -*- coding: utf-8 -*-
"""
core/toast.py —— 全局状态弹窗（置顶 / 不抢焦点 / 点不穿）
=======================================================

## 为什么需要它（主人反馈）

「我想添加一个开启/停止的弹窗/通知，方便确认状态，
  并且要显示在最前方，比如玩原神启动的话也能够看到弹窗」

连点/锁定一旦启动，用户的注意力就在**目标程序**（游戏）上，
仪表盘被最小化让位了，此时「到底启动没有、暂停没有」
只能靠猜。所以需要一个独立小窗，不管前台是谁都浮在最上面。

## 三个关键样式（少任何一个都会毁掉体验）

| 样式 | 常量 | 没有它会怎样 |
|---|---|---|
| 置顶 | WS_EX_TOPMOST | 游戏全屏时弹窗被挡住，等于没有 |
| 不抢焦点 | WS_EX_NOACTIVATE | 弹窗一出现，游戏焦点被夺走，角色立刻失控 |
| 点击穿透 | WS_EX_TRANSPARENT | 弹窗挡住游戏画面，点它没反应、点后面也点不到 |

另加 WS_EX_TOOLWINDOW（不出现在任务栏/Alt+Tab）
和 WS_EX_LAYERED（半透明背景，不挡画面）。

## 架构：自己一条线程 + 自己的消息循环

Win32 窗口必须由「创建它的那个线程」跑消息循环，
而 show() 会从热键线程、pywebview 线程等各处被调用 ——
不可能让每个调用方都去跑消息循环。
所以弹窗独占一条线程：创建窗口、收消息、定时消失全在里面。
调用方（任意线程）只需要 show(text)，内部 PostMessage 过来。

## 已知边界

绝大多数游戏（窗口化/无边框）都能看到它；
**独占全屏（Exclusive Fullscreen）的 DX 游戏可能盖住它** ——
那是 DirectX 独占显示管线的限制，任何普通窗口都无能为力，
只能建议用户把游戏调成「无边框窗口」模式。
"""

import ctypes
import ctypes.wintypes as wt
import threading
import time

try:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _kernel32 = ctypes.windll.kernel32
    _msimg32 = ctypes.windll.msimg32      # GradientFill（深底渐变）
except Exception:            # 非 Windows 环境（打包/测试机外）静默降级
    _user32 = None
    _msimg32 = None

# ---- 窗口样式常量 ----
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_APP_SHOW = 0x8000 + 1        # 自定义消息：显示一条文本
WM_APP_QUIT = 0x8000 + 2        # 自定义消息：退出线程

LWA_ALPHA = 0x2
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020

# 弹窗尺寸（像素）与默认停留时长
# 【右下角卡片 · 2026-10-16】主人要的是「WorkBuddy 那种右下角通知」：
# 位置从屏幕中上挪到工作区右下角，尺寸放大、留两行（标题 + 说明），
# 停留从 2.5s 提到 3.2s —— 游戏里瞄着画面，2.5 秒常常只来得及瞟一眼。
TOAST_W = 360
TOAST_H = 78
TOAST_MARGIN = 24               # 距工作区右/下边缘
TOAST_CHAMFER = 12              # 对角切角（左上/右下）——与主界面 --chamfer 一致
TOAST_SECONDS = 3.2
TOAST_ALPHA = 232               # 0~255

# ----------------------------------------------------------------------
# 紫霓虹科技风配色（2026-10-18 v34，与主界面 ui/dashboard.html 统一）
# ----------------------------------------------------------------------
# 【为什么整段重做】旧版是「整卡片纯色」：蓝/橙/灰/青四块大色块，
# 和主界面的 void-black 深底 + holo-cyan 辉光体系完全两个世界，
# 主人实测反馈「UI 与主界面风格不统一」。
#
# 主界面的形状与色彩语言（照抄，GDI 可实现的子集）：
#   · 深底渐变面板：rgba(139,92,246,.10) → rgba(18,10,36,.72) → rgba(24,14,48,.55)
#   · 1px 紫色描边：rgba(139,92,246,.22) → 叠在深底上的实色近似 (45,28,82)
#   · 12px 对角切角（左上 + 右下），科技风靠切角不是圆角
#   · 顶部一条全息高光线
#   · 文字双色：标题 #F0E8FF（粗）/ 说明 #9C8CC4（细），左对齐
#   · 状态色只做小面积点缀（dot / 描边暗部），不做大面积——主色 ≤15%
# 做不到的（GDI 限制，在注释里留痕）：box-shadow 辉光、半透明叠色、
# 动画——统一用「预乘实色近似」逼近。
TOAST_BG_TOP = (30, 18, 57)     # 渐变顶（微亮，模拟 rgba(139,92,246,.10) 叠层）
TOAST_BG_BOTTOM = (18, 10, 36)  # 渐变底 #120A24
TOAST_BORDER = (45, 28, 82)     # rgba(139,92,246,.22) on #120A24 的实色近似
TOAST_TOPLINE = (58, 21, 103)   # 顶部高光线（accent-deep 亮部的实色近似）
TOAST_TEXT_HI = (0xF0, 0xE8, 0xFF)   # 标题色，同 --tx-hi
TOAST_TEXT_MID = (0x9C, 0x8C, 0xC4)  # 说明色，同 --tx-mid

# 每种状态的点缀色（RGB）——语义与主界面卡片状态色对齐：
# 运行=accent-violet、就绪=accent-mint（待命）、暂停=spark-amber、停止=text-dim、
# 错误=alert-red。dot / 描边用它，面积小但识别度强。
TOAST_STATE = {
    "start":  (0x8B, 0x5C, 0xF6),   # accent-violet 已开始/已继续
    "ready":  (0x3E, 0xF0, 0xC4),   # accent-mint   已就绪
    "pause":  (0xFF, 0x9A, 0x3C),   # spark-amber   已暂停
    "warn":   (0xFF, 0x9A, 0x3C),   # spark-amber   降级/失败
    "error":  (0xFF, 0x2D, 0x4B),   # alert-red     错误
    "stop":   (0x5F, 0x4E, 0x85),   # text-dim      已停止（中性结束）
    "info":   (0x8B, 0x5C, 0xF6),   # accent-violet 一般信息
}

def chamfer_points():
    """
    12px 对角切角六边形顶点（左上 + 右下），与主界面
    `polygon(12px 0, 100% 0, 100% calc(100%-12px),
             calc(100%-12px) 100%, 0 100%, 0 12px)` 同构。

    【为什么是函数】窗口裁剪（SetWindowRgn）和描边（FrameRgn）都要这份
    顶点，两处各抄一遍迟早写歪一处（切角形状对不上，边框会露在外面）。
    """
    c = TOAST_CHAMFER
    return (wt.POINT * 6)(
        wt.POINT(c, 0),                      # 左上切
        wt.POINT(TOAST_W, 0),
        wt.POINT(TOAST_W, TOAST_H - c),      # 右下切
        wt.POINT(TOAST_W - c, TOAST_H),
        wt.POINT(0, TOAST_H),
        wt.POINT(0, c),
    )


def make_chamfer_region():
    """切角六边形 HRGN（失败返回 None）。"""
    if _gdi32 is None:
        return None
    try:
        return _gdi32.CreatePolygonRgn(chamfer_points(), 6, 1)  # 1=ALTERNATE
    except Exception:
        return None


# 取工作区（排除任务栏）用：SPI_GETWORKAREA
SPI_GETWORKAREA = 0x0030

# 字体名：中文界面用微软雅黑，缺失时 GDI 自动回退
FONT_NAME = "Microsoft YaHei"


# ----------------------------------------------------------------------
# ctypes 结构体
# ----------------------------------------------------------------------
class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class RECT(ctypes.Structure):
    """工作区矩形（SystemParametersInfoW(SPI_GETWORKAREA) 用）。"""
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", ctypes.c_void_p),
        ("fErase", ctypes.c_int),
        ("rcPaint", ctypes.c_long * 4),
        ("fRestore", ctypes.c_int),
        ("fIncUpdate", ctypes.c_int),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class TRIVERTEX(ctypes.Structure):
    """GradientFill 顶点：16 位色深（0~65535，8 位值 ×257）。"""
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                ("Red", ctypes.c_ushort), ("Green", ctypes.c_ushort),
                ("Blue", ctypes.c_ushort), ("Alpha", ctypes.c_ushort)]


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)

# 【为什么必须声明 DefWindowProcW 的签名】
# 窗口过程收到的 lparam 在 64 位下是 64 位值——WM_NCCALCSIZE /
# WM_CREATE / WM_WINDOWPOSCHANGING 等消息传的都是**指针**
# （栈上结构体的地址，常见形态 0x000000E1B2C3D400 这种，高位非零）。
# ctypes 对未声明 argtypes 的函数按 32 位 C int 传参，指针高位被砍掉，
# DefWindowProc 就会拿着野指针去读内存。
#
# 不声明时的实际表现（本机实测）：调用抛
# ctypes.ArgumentError: int too long to convert，被 _wnd_proc 的
# except 吞掉后 return 0。对 WM_NCCALCSIZE 恰好良性（0 = 整窗即客户区，
# 正是弹窗想要的），但这是侥幸，不是设计——换成别的消息就可能真出事。
#
# 注意：这里只能声明在 toast 自己的 _user32 实例上。dashboard.py 里
# 那个 user32 是另一个 WinDLL 实例（WinDLL() 不缓存，每次调用都是新对象），
# 它的声明管不到这里。
# 【硬规则 2：所有传句柄/指针的 API 必须声明 argtypes + restype】
# 不声明时 ctypes 按 32 位 C int 传参、按 c_int 读返回值——64 位下
# 句柄高位被砍、返回值截断，表现为「静默失效」（不报错，就是不干活）。
# 2026-10-16 实测教训：CreateWindowExW 不声明 restype 时拿到的是
# 截断句柄，后续 SetWindowPos/ShowWindow 全部操作到错误目标。
# 每个模块各自声明各自用到的签名（WinDLL() 不缓存实例，互不共享）。
if _user32 is not None:
    _user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    _user32.RegisterClassW.restype = wt.ATOM
    _user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    _user32.CreateWindowExW.restype = wt.HWND
    _user32.DestroyWindow.argtypes = [wt.HWND]
    _user32.DestroyWindow.restype = wt.BOOL
    _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wt.BOOL
    _user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wt.UINT]
    _user32.SetWindowPos.restype = wt.BOOL
    _user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
    _user32.InvalidateRect.restype = wt.BOOL
    _user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT,
                                 ctypes.c_void_p]
    _user32.SetTimer.restype = ctypes.c_size_t
    _user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
    _user32.KillTimer.restype = wt.BOOL
    # 【核心修复 · 2026-10-16】投递方式从 PostThreadMessageW 改为
    # PostMessageW(hwnd, ...)。原因：PostThreadMessageW 投的是 hwnd=NULL
    # 的线程消息，DispatchMessageW 不会把它送到任何窗口过程——
    # 消息进了队列、GetMessageW 也取得到，但分发目标为空，
    # _wnd_proc 永远收不到，弹窗全程静默不显示（v13~v14 实测无效的真因）。
    # PostMessageW 带 hwnd，DispatchMessageW 才能正确送到窗口过程。
    _user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    _user32.PostMessageW.restype = wt.BOOL
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                    wt.UINT, wt.UINT]
    _user32.GetMessageW.restype = wt.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    _user32.TranslateMessage.restype = wt.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.PostQuitMessage.restype = None
    _user32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
    _user32.BeginPaint.restype = wt.HDC
    _user32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
    _user32.EndPaint.restype = wt.BOOL
    _user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
    _user32.FillRect.restype = ctypes.c_int
    _user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int,
                                  ctypes.c_void_p, wt.UINT]
    _user32.DrawTextW.restype = ctypes.c_int
    _gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
    _gdi32.SetBkMode.restype = ctypes.c_int
    _gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
    _gdi32.SetTextColor.restype = wt.COLORREF
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF,
                                                   wt.BYTE, wt.DWORD]
    _user32.SetLayeredWindowAttributes.restype = wt.BOOL
    _user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
    _user32.SetWindowRgn.restype = ctypes.c_int
    _user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT,
                                              ctypes.c_void_p, wt.UINT]
    _user32.SystemParametersInfoW.restype = wt.BOOL
    _gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int]
    _gdi32.CreateRoundRectRgn.restype = wt.HRGN
    _gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
    _gdi32.CreateSolidBrush.restype = wt.HBRUSH
    _gdi32.DeleteObject.argtypes = [wt.HANDLE]
    _gdi32.DeleteObject.restype = wt.BOOL
    _gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
        wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
    _gdi32.CreateFontW.restype = wt.HFONT
    _gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
    _gdi32.SelectObject.restype = wt.HANDLE
    _kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wt.HMODULE
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wt.DWORD
    # ---- 全息风绘制（v15 新增，与主界面形状语言统一）----
    # 切角六边形 region：CreateRoundRectRgn 只能做圆角，主界面是
    # 12px 对角切角，必须用 CreatePolygonRgn 手搓六边形顶点。
    _gdi32.CreatePolygonRgn.argtypes = [ctypes.POINTER(wt.POINT),
                                        ctypes.c_int, ctypes.c_int]
    _gdi32.CreatePolygonRgn.restype = wt.HRGN
    # 1px 描边：FrameRgn 用画刷沿 region 边缘画一圈（比 Pen+LineTo 省事）
    # 【注意 DLL 归属】FrameRgn / MoveToEx / LineTo 都是 **gdi32** 导出，
    # 不是 user32。挂错 DLL 表现为 AttributeError: function not found，
    # 好在导入期就炸，不会静默失效。
    _gdi32.FrameRgn.argtypes = [wt.HDC, wt.HRGN, wt.HBRUSH,
                                ctypes.c_int, ctypes.c_int]
    _gdi32.FrameRgn.restype = wt.BOOL
    # 顶部高光线 / 角标线
    _gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
    _gdi32.CreatePen.restype = wt.HPEN
    _gdi32.MoveToEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                                ctypes.POINTER(wt.POINT)]
    _gdi32.MoveToEx.restype = wt.BOOL
    _gdi32.LineTo.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.LineTo.restype = wt.BOOL
    # 状态圆点
    _gdi32.Ellipse.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int]
    _gdi32.Ellipse.restype = wt.BOOL
    # 标题字距（科技感的来源之一，主界面 h1 是 letter-spacing .14em）
    _gdi32.SetTextCharacterExtra.argtypes = [wt.HDC, ctypes.c_int]
    _gdi32.SetTextCharacterExtra.restype = ctypes.c_int
if _msimg32 is not None:
    # 深底垂直渐变（GDI 单色做不到主界面的三段渐变，两段近似）
    _msimg32.GradientFill.argtypes = [wt.HDC,
                                      ctypes.POINTER(TRIVERTEX),
                                      ctypes.c_ulong, ctypes.c_void_p,
                                      ctypes.c_ulong, ctypes.c_ulong]
    _msimg32.GradientFill.restype = wt.BOOL


# ----------------------------------------------------------------------
# 弹窗管理器（单例，后台线程）
# ----------------------------------------------------------------------
class _ToastManager(object):

    CLASS_NAME = u"WBMouseClickerToast"

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._hwnd = None
        self._tid = None
        self._wndproc = None        # 必须持有引用，否则被 GC 后窗口过程崩
        self._pending = []          # [(text, kind, seconds)]，跨线程投递用
        self._ready = threading.Event()
        self._cur_text = ""
        self._cur_kind = "info"
        self.error = ""             # 最近一次失败原因（排查「没通知」用）

    # ---- 对外 ----
    def show(self, text, kind="info", seconds=TOAST_SECONDS):
        """
        显示一条状态弹窗，seconds 秒后自动消失。

        任意线程可调；内部 PostMessageW 到弹窗窗口执行。
        弹窗是锦上添花，任何异常都不能把连点/热键链路带崩——
        但失败必须留痕（self.error），否则「实测没通知」时无从排查。
        """
        if _user32 is None:
            self.error = "user32 不可用（非 Windows 环境）"
            return False
        try:
            try:
                secs = float(seconds)
            except Exception:
                secs = TOAST_SECONDS
            secs = max(0.5, min(secs, 30.0))
            with self._lock:
                self._pending.append((str(text), str(kind or "info"), secs))
            if not (self._thread and self._thread.is_alive()):
                self._start_thread()
            if self._hwnd:
                # 【必须带 hwnd】PostThreadMessageW 投的线程消息
                # DispatchMessageW 送不到窗口过程，弹窗永远不显示。
                _user32.PostMessageW(self._hwnd, WM_APP_SHOW, 0, 0)
            else:
                self.error = "弹窗窗口未建成（_hwnd=None）"
            return True
        except Exception as e:
            self.error = "show 异常: %r" % e
            return False

    def stop(self):
        """退出弹窗线程并销毁窗口。"""
        if _user32 is None:
            return
        try:
            if self._hwnd:
                _user32.PostMessageW(self._hwnd, WM_APP_QUIT, 0, 0)
        except Exception:
            pass

    # ---- 内部 ----
    def _start_thread(self):
        t = threading.Thread(target=self._thread_main,
                             name="toast", daemon=True)
        t.start()
        # 只等「窗口建好」这一瞬，不进消息循环等待
        self._ready.wait(timeout=2.0)
        self._thread = t

    def _thread_main(self):
        try:
            self._register_class()
            self._create_window()
        except Exception as e:
            self.error = "弹窗线程启动失败: %r" % e
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while True:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        try:
            if self._hwnd:
                _user32.DestroyWindow(self._hwnd)
        except Exception:
            pass

    def _register_class(self):
        self._wndproc = WNDPROC(self._wnd_proc)
        hinst = _kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None            # 背景自己画（半透明色）
        wc.lpszMenuName = None
        wc.lpszClassName = self.CLASS_NAME
        if not _user32.RegisterClassW(ctypes.byref(wc)):
            err = _kernel32.GetLastError()
            # 1409 = ERROR_CLASS_ALREADY_EXISTS：同进程重复注册，直接复用
            if err != 1409:
                raise ctypes.WinError(err)

    def _create_window(self):
        self._tid = _kernel32.GetCurrentThreadId()
        w = _user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
            | WS_EX_TRANSPARENT | WS_EX_LAYERED,
            self.CLASS_NAME, u"",
            WS_POPUP,
            0, 0, TOAST_W, TOAST_H,
            None, None, _kernel32.GetModuleHandleW(None), None)
        if not w:
            raise ctypes.WinError(_kernel32.GetLastError())
        self._hwnd = w
        # 切角：SetWindowRgn 把窗口裁成六边形（左上/右下各切 12px）。
        # 【为什么不是圆角】主界面 --chamfer:12px 是「科技风靠切角
        # 而不是圆角」的形状语言，圆角卡片压在全息风界面上直接出戏。
        # CreateRoundRectRgn 只能做圆角 → 六边形顶点见 chamfer_points()。
        try:
            rgn = make_chamfer_region()
            if rgn:
                _user32.SetWindowRgn(w, rgn, True)
        except Exception:
            pass
        # 半透明（不带颜色键，整窗统一透明度）
        _user32.SetLayeredWindowAttributes(w, 0, TOAST_ALPHA, LWA_ALPHA)

    # ---- 窗口过程 ----
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_SHOW:
                self._on_show()
                return 0
            if msg == WM_APP_QUIT:
                _user32.PostQuitMessage(0)
                return 0
            if msg == WM_TIMER:
                # 到点隐藏（窗口留着复用，不销毁）
                _user32.KillTimer(hwnd, 1)
                _user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == WM_PAINT:
                self._on_paint(hwnd)
                return 0
            if msg == WM_DESTROY:
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            try:
                return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            except Exception:
                return 0

    def _on_show(self):
        with self._lock:
            if not self._pending:
                return
            text, kind, secs = self._pending[-1]   # 连发多条只显示最后一条
            self._pending = []
        # 先备好文字再重绘：InvalidateRect 只是「打标记」，
        # 真正的 WM_PAINT 要等消息循环转起来才到，那时必须已经拿到新文本。
        self._cur_text = text
        self._cur_kind = kind
        x, y = self._bottom_right_xy()
        _user32.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y,
                             TOAST_W, TOAST_H,
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
        _user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        _user32.InvalidateRect(self._hwnd, None, True)
        # 重设消失定时器（连续 show 会顺延，不会闪一下就没了）
        _user32.SetTimer(self._hwnd, 1, int(secs * 1000), None)

    def _bottom_right_xy(self):
        """
        右下角坐标（贴工作区，避开任务栏）。

        为什么不用屏幕尺寸：任务栏会占掉屏幕底部一条，
        直接按 SM_CYSCREEN 算出来的 y 会让通知压在任务栏上（或被它盖住）。
        SystemParametersInfoW(SPI_GETWORKAREA) 拿到的才是可用区域。
        取不到就退回屏幕尺寸，宁可稍微偏一点也别不显示。
        """
        try:
            rc = RECT()
            if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                             ctypes.byref(rc), 0):
                return (max(0, int(rc.right) - TOAST_W - TOAST_MARGIN),
                        max(0, int(rc.bottom) - TOAST_H - TOAST_MARGIN))
        except Exception:
            pass
        sw = _user32.GetSystemMetrics(0)       # SM_CXSCREEN
        sh = _user32.GetSystemMetrics(1)       # SM_CYSCREEN
        return (max(0, int(sw) - TOAST_W - TOAST_MARGIN),
                max(0, int(sh) - TOAST_H - TOAST_MARGIN))

    def _on_paint(self, hwnd):
        """
        紫霓虹科技风卡片（2026-10-18 v34，与主界面 ui/dashboard.html 统一）：

          ① 深底垂直渐变（GradientFill 两段：#1E1239 → #120A24）
          ② 1px 紫色描边（FrameRgn 沿六边形 region）
          ③ 顶部一条全息高光线（主界面 .panel::before 的实色近似）
          ④ 标题左侧状态圆点：外层暗色大圆模拟辉光 + 内层实色
          ⑤ 文字双色左对齐：标题 #F0E8FF 粗体带 1px 字距，
             说明 #9C8CC4 常规体

        text 里带 "\\n" 就按两行画；不带就单行。
        为什么不直接 DT_WORDBREAK：DrawTextW 的换行只认词边界，
        对「标题\\n说明」这种强分行不保险，自己拆最可控。

        GDI 做不到、用近似扛的（留痕）：半透明叠色 → 预乘实色；
        box-shadow 辉光 → 双层圆；letter-spacing → SetTextCharacterExtra。

        【为什么要拆出 _paint_card】绘制逻辑必须能**离屏渲染**才能验收：
        分层窗口（WS_EX_LAYERED）的内容存在于合成器里，PrintWindow /
        屏幕回读都拿不到干净结果，UI 改完只能靠「看不出问题」蒙。
        拆出来的 _paint_card(hdc) 可以被测试喂一个内存 DC，画完直接读
        像素比对——UI 第一次变成可断言的东西。
        """
        ps = PAINTSTRUCT()
        hdc = _user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            self._paint_card(hdc)
        finally:
            _user32.EndPaint(hwnd, ctypes.byref(ps))

    def _paint_card(self, hdc):
        """把整张卡片画到任意 DC 上（窗口 DC 或内存 DC 都能喂）。"""
        try:
            state = TOAST_STATE.get(self._cur_kind, TOAST_STATE["info"])
            dim = self._dim(state, 0.35)      # 辉光近似：状态色压暗

            # ① 深底：**先铺纯色兜底，再叠渐变**。
            # 顺序不能反——GradientFill 有「返回成功却一个像素不画」的
            # 前科（见 _fill_gradient），只靠它的返回值做兜底判断不可靠。
            # 先铺底 = 无论渐变成不成，卡片都不可能空。
            full = (ctypes.c_long * 4)(0, 0, TOAST_W, TOAST_H)
            brush = _gdi32.CreateSolidBrush(self._rgb(TOAST_BG_BOTTOM))
            _user32.FillRect(hdc, ctypes.byref(full), brush)
            _gdi32.DeleteObject(brush)
            self._fill_gradient(hdc)

            # ② 1px 描边：同一个六边形 region 喂给 FrameRgn，
            # 保证描边严丝合缝贴着窗口裁剪线（两处顶点必须同源）
            c = TOAST_CHAMFER
            rgn = make_chamfer_region()
            if rgn:
                br = _gdi32.CreateSolidBrush(self._rgb(TOAST_BORDER))
                _gdi32.FrameRgn(hdc, rgn, br, 1, 1)
                _gdi32.DeleteObject(br)
                _gdi32.DeleteObject(rgn)

            # ③ 顶部高光线（避开左上切角，右上无切角画到边）
            pen = _gdi32.CreatePen(0, 1, self._rgb(TOAST_TOPLINE))
            old_pen = _gdi32.SelectObject(hdc, pen)
            _gdi32.MoveToEx(hdc, c, 0, None)
            _gdi32.LineTo(hdc, TOAST_W - 1, 0)
            _gdi32.SelectObject(hdc, old_pen)
            _gdi32.DeleteObject(pen)

            # ④ 状态圆点：外层暗（辉光近似）+ 内层实色
            # 【必须同时选刷子和画笔】Ellipse 用的是 DC 当前选中的 brush
            # 画填充、当前 pen 画轮廓。只 CreateSolidBrush 不 SelectObject，
            # 画出来是默认白刷 + 默认黑笔——两个白点带黑圈，跟状态色无关。
            # pen 用同色 1px，避免黑边把 6px 的小实心点吃掉一半。
            _gdi32.SetBkMode(hdc, 1)              # TRANSPARENT
            cx, cy = 24, 26
            for radius, col in ((5, dim), (3, state)):
                br = _gdi32.CreateSolidBrush(self._rgb(col))
                pn = _gdi32.CreatePen(0, 1, self._rgb(col))
                ob = _gdi32.SelectObject(hdc, br)
                op = _gdi32.SelectObject(hdc, pn)
                _gdi32.Ellipse(hdc, cx - radius, cy - radius,
                               cx + radius, cy + radius)
                _gdi32.SelectObject(hdc, ob)
                _gdi32.SelectObject(hdc, op)
                _gdi32.DeleteObject(br)
                _gdi32.DeleteObject(pn)

            # ⑤ 文字：左对齐双色（x 从 34 起，避开圆点）
            parts = str(self._cur_text or "").split("\n", 1)
            if len(parts) == 2 and parts[1].strip():
                title, detail = parts[0].strip(), parts[1].strip()
                rc1 = (ctypes.c_long * 4)(34, 10, TOAST_W - 16, 42)
                rc2 = (ctypes.c_long * 4)(34, 38, TOAST_W - 16, TOAST_H - 8)
                self._draw(hdc, rc1, title, -20, 700, TOAST_TEXT_HI,
                           DT_SINGLELINE, 1)
                self._draw(hdc, rc2, detail, -15, 400, TOAST_TEXT_MID,
                           DT_SINGLELINE, 0)
            else:
                rc = (ctypes.c_long * 4)(34, 8, TOAST_W - 16, TOAST_H - 8)
                self._draw(hdc, rc, parts[0].strip(), -20, 700,
                           TOAST_TEXT_HI, DT_VCENTER | DT_SINGLELINE, 1)
        except Exception as e:
            self.error = "绘制失败: %r" % e

    @staticmethod
    def _rgb(t):
        """(r,g,b) → GDI COLORREF（0x00BBGGRR，高位恒 0）。"""
        return (t[0]) | (t[1] << 8) | (t[2] << 16)

    @staticmethod
    def _dim(t, k):
        """颜色压暗 k 倍（辉光/边框的实色近似用）。"""
        return (int(t[0] * k), int(t[1] * k), int(t[2] * k))

    def _fill_gradient(self, hdc):
        """
        深底垂直渐变。msimg32 缺失或失败返回 False（调用方退化纯色）。

        【踩过的坑 · 2026-10-16】RECT_V 模式下两个顶点定义的是**矩形**，
        不是「一条竖线」：写成 (0,0) 和 (0,TOAST_H) → 矩形宽度为 0 →
        GradientFill 返回**成功(1) 却一个像素都不画**。危险在于它不报错，
        调用方拿到 True 就不走纯色兜底，整张卡片底色全空（实测症状：
        文字和状态点都在，就是没有底）。
        正确写法：v[0]=(0,0) 左上、v[1]=(TOAST_W,TOAST_H) 右下。
        """
        if _msimg32 is None:
            return False
        try:
            v = (TRIVERTEX * 2)()
            v[0].x, v[0].y = 0, 0
            v[0].Red, v[0].Green, v[0].Blue = (c * 257 for c in TOAST_BG_TOP)
            v[0].Alpha = 0
            v[1].x, v[1].y = TOAST_W, TOAST_H     # x 必须给宽度，否则零宽
            v[1].Red, v[1].Green, v[1].Blue = (c * 257
                                               for c in TOAST_BG_BOTTOM)
            v[1].Alpha = 0
            g = (ctypes.c_ulong * 2)(0, 1)        # GRADIENT_RECT{0,1}
            return bool(_msimg32.GradientFill(hdc, v, 2, g, 1, 1))
        except Exception:
            return False

    def _draw(self, hdc, rc, text, height, weight, color, flags, tracking=0):
        """
        选字体 → 设色 → 画一行 → 还原并释放。

        height 为负 = 按字符高度指定；weight 400=常规 700=粗；
        tracking = 额外字距（px，科技感标题加 1）。
        """
        font = _gdi32.CreateFontW(
            height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, FONT_NAME)
        old = _gdi32.SelectObject(hdc, font)
        try:
            _gdi32.SetTextColor(hdc, self._rgb(color))
            _gdi32.SetTextCharacterExtra(hdc, tracking)
            _user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), flags)
            _gdi32.SetTextCharacterExtra(hdc, 0)   # 还原，别污染下次
        finally:
            _gdi32.SelectObject(hdc, old)
            _gdi32.DeleteObject(font)


# 单例
_MANAGER = None
_MANAGER_LOCK = threading.Lock()


def _manager():
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = _ToastManager()
        return _MANAGER


def show(text, kind="info", seconds=TOAST_SECONDS):
    """显示状态弹窗。kind: start / stop / pause / error / info"""
    return _manager().show(text, kind=kind, seconds=seconds)


def show_mouse_started(cps=0, trigger="", jitter=0):
    # 【v22】抖动 > 0 时在弹窗里注明，否则用户看不出节奏为什么不匀
    text = ("鼠标连点 已开始\n每秒 %d 次 · 再按 %s 暂停"
            % (int(cps or 0), trigger or "触发键"))
    if jitter and int(jitter) > 0:
        text += "\n间隔浮动 ±%dms（防机械节奏）" % int(jitter)
    return show(text, "start")


def show_mouse_paused(trigger=""):
    return show("鼠标连点 已暂停\n再按 %s 继续" % (trigger or "触发键"), "pause")


def show_mouse_resumed(trigger=""):
    return show("鼠标连点 已继续\n再按 %s 暂停" % (trigger or "触发键"), "start")


def show_mouse_stopped():
    return show("鼠标连点 已停止\n回到未就绪，需重新点「就绪」", "stop")


def show_mouse_ready(trigger=""):
    return show("鼠标连点 已就绪\n按 %s 开始连点" % (trigger or "触发键"), "ready")


def show_key_started(key=""):
    return show("键盘锁定 已开始\n%s · 再按触发键暂停" % (key or ""), "start")


def show_key_paused(trigger=""):
    """
    暂停键盘区。

    【2026-10-16】文案点明「已解除锁定」：暂停的语义就是松手，
    锁定状态在同一时刻被清掉（见 core/keyboard.py::_inject_loop），
    不说清楚的话，用户再按目标键时会以为是「恢复」而不是「重新长按」。
    """
    return show("键盘锁定 已暂停\n已解除锁定 · 再按 %s 恢复运行"
                % (trigger or "触发键"), "pause")


def show_key_resumed(trigger="", keys=""):
    """键盘区恢复运行（锁定已在暂停时解除，这里是「重新待命」）。"""
    return show("键盘锁定 已恢复运行\n长按 %s 可重新锁定" % (keys or "目标键"),
                "start")


def show_key_stopped():
    return show("键盘锁定 已停止\n回到未就绪，需重新点「就绪」", "stop")


def show_key_ready(trigger=""):
    return show("键盘锁定 已就绪\n按 %s 开始锁定" % (trigger or "触发键"), "ready")


def show_paused():
    return show("已全局暂停\n鼠标与键盘都停了，再按暂停键恢复", "pause")


def show_resumed():
    return show("已恢复运行\n鼠标与键盘继续", "start")


def show_custom(text, kind="info"):
    """
    自定义一条通知。

    ⚠ **只收两个参数**（text, kind）—— 第三个 `seconds` 会 TypeError。
    这条坑 2026-10-15 踩过：离线测试把 toast 整个 patch 掉就永远测不出来，
    必须真跑 exe 才会炸。要控制停留时长请用 `show(text, kind, seconds)`。
    """
    return show(text, kind=kind)


def stop():
    """退出弹窗线程（程序退出时调用）。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.stop()
