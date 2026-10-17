# -*- coding: utf-8 -*-
"""
diag_focus_probe.py —— 定位「GUI 版在原神里无效」的元凶

原理
====
SendInput 把键鼠事件投给【当前前台窗口】。现在已经确认：
CLI 版（鼠标连点器.exe / 鼠标连点器-长按锁定.exe）在原神里能用，
GUI 版（爪Pro.exe）不能用，两边**调用的是同一个 core.inputs**，
也**都是管理员权限**。

既然注入代码和权限都一样，唯一的差别就是 **GUI 版多做的那些动作**
（自己的窗口、最小化、toast、Raw Input 监听、热键）。
所以本脚本的做法是：

    先把游戏置于前台 → 逐个复现 GUI 的动作 → 每做一步检查前台还是不是游戏
    → **变了的那一步就是元凶**，不需要靠肉眼在游戏里观察

用法
====
1. 必须【右键 → 以管理员身份运行】PowerShell / 终端（和跑连点器同样的权限）
2. **必须先把 `爪Pro.exe` 打开放着**（窗口在就行，不用启动连点！
   不开的话最关键的第 ⑤ 步会被跳过，跑出来全是 OK，等于白跑——
   2026-10-16 第一轮就是这么白白跑了一趟）
3. 运行：`.venv\\Scripts\\python.exe diag_focus_probe.py`
4. 脚本倒数 10 秒，这期间你【切到原神】；之后手离开键鼠，脚本全自动跑完
   （中途会有几秒把仪表盘窗口顶到前台再最小化，属正常，跑完会帮你还原）
5. 看最后那张表：**⑤-A 才是最接近你真实操作的那一步**

安全：脚本会往系统注入极少量真实输入（一次点击 + toast），
出门前会把所有鼠标键松开。中途 Ctrl+C 可随时中断。
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

# 项目 core 与本脚本同级，保证 import 到的是生产代码而非副本
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.c_void_p]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
_user32.FindWindowW.restype = wt.HWND
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wt.BOOL
_user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
_user32.RegisterHotKey.restype = wt.BOOL
_user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                            ctypes.c_wchar_p,
                                            ctypes.POINTER(wt.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_MINIMIZE = 6
SW_RESTORE = 9

_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------
def pid_of(hwnd):
    """取窗口所属进程 PID。"""
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def proc_name(pid):
    """取进程的可执行文件名（拿不到返回 '?'）。"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        _k32.CloseHandle(h)
    return "?"


def title_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def fg():
    """当前前台窗口：返回 (hwnd, pid, 进程名)。"""
    h = _user32.GetForegroundWindow()
    if not h:
        return (0, 0, "(无)")
    p = pid_of(h)
    return (int(h), p, proc_name(p))


def same_window(a, b):
    """前台是否还是同一个窗口（按 hwnd 比，_pid 变化说明换了进程）。"""
    return a[0] == b[0] and a[0] != 0


def countdown(sec, tip):
    print("\n" + "-" * 58)
    print(tip)
    print("-" * 58)
    for i in range(sec, 0, -1):
        sys.stdout.write("\r  %2d 秒后开始……（Ctrl+C 可中止）" % i)
        sys.stdout.flush()
        time.sleep(1)
    print("\r  %-40s\r" % "开始！")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    print("=" * 58)
    print(" 前台窗口抢占探测器 —— GUI 版在原神失效定位")
    print("=" * 58)

    # dashboard 窗口：如果开着就用真的，没有就退化说明
    dash = _user32.FindWindowW(None, u"爪Pro")
    if dash:
        print("已找到真实的爪Pro窗口：hwnd=%s pid=%s"
              % (dash, pid_of(dash)))
    else:
        print("未找到「爪Pro」窗口（GUI 没开着）。")
        print("本轮只测「纯注入 / toast / 热键」三项，")
        print("GUI 窗口相关的最小化步骤会跳过 —— 建议开着 GUI 再跑一次。")

    countdown(10, "现在请【切到原神】，然后手离开键鼠。")

    target = fg()
    print("\n[基线] 前台窗口 = hwnd=%s  pid=%s  %s"
          % (target[0], target[1], target[2]))
    if target[2] in ("?", "(无)"):
        print("  ⚠ 没取到前台进程名，结果可能不可靠。")

    rows = []

    def probe(label, action):
        """执行一个动作，然后检查前台是否被夺走。"""
        try:
            action()
        except Exception as e:
            rows.append((label, "动作异常: %s" % e, "-"))
            return
        time.sleep(0.7)      # 焦点切换有延迟，立刻读会误判
        now = fg()
        ok = same_window(now, target)
        rows.append((label,
                     "前台未变 ✅" if ok else "前台被夺 ❌ → %s (%s)"
                     % (now[2], now[0]),
                     now[2]))
        print("  %-26s %s" % (label,
                              "OK" if ok else "变了 → %s" % now[2]))

    print("\n[开始] 逐步复现 GUI 的动作并检测前台：")

    # --- Step 1：纯注入（基线：CLI 版做的事情，预期不会改前台）---
    def s1():
        from core import inputs
        inputs.click("left", count=1)

    # 【为什么这里要警告】
    # 这次左键点击会真的送到前台的游戏里 —— 原神里就是**角色普攻一下**。
    # 2026-10-16 主人第一次跑的时候被吓到，以为自己误触了。
    # 诊断价值：它同时也证明了 SendInput 确实到得了游戏（若误把它当噪音，
    # 就丢掉了「注入链路是通的」这条正面证据）。想要它跑，但要先打招呼。
    if "--skip-inject" in sys.argv:
        rows.append(("① 纯 SendInput 注入", "跳过（--skip-inject）", "-"))
        print("  ① 已跳过，本轮不会打扰游戏")
    else:
        print("\n" + "!" * 58)
        print("! 下一步会真的向【当前前台窗口】发一次鼠标左键点击")
        print("! → 原神里的角色会【普攻一下】，这是脚本故意发的，不是误触")
        print("! 不想让角色挥一刀：加参数 --skip-inject 重新运行")
        print("!" * 58)
        countdown(3, "3 秒后注入一次左键（不想测就现在 Ctrl+C）")
        probe("① 纯 SendInput 注入", s1)

    # --- Step 2：toast 弹窗（dashboard 启动/停止必走）---
    def s2():
        from core import toast
        toast.show_custom("前台检测：这是测试弹窗", "start")
    probe("② 弹 toast 状态条", s2)

    # --- Step 3：Raw Input 监听（dashboard 启动时注册，CLI 没有）---
    def s3():
        import threading

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", wt.USHORT),
                        ("usUsage", wt.USHORT),
                        ("dwFlags", wt.DWORD),
                        ("hwndTarget", wt.HWND)]

        RIDEV_INPUTSINK = 0x00000100

        def proc():
            # 建一个隐藏的消息窗口，再注册 INPUTSINK —— 和 dashboard 一致
            hwnd = ctypes.windll.user32.CreateWindowExW(
                0, u"STATIC", u"probe-sink", 0, 0, 0, 0, 0,
                None, None, None, None)
            if not hwnd:
                return
            devs = (RAWINPUTDEVICE * 1)()
            devs[0].usUsagePage = 0x01
            devs[0].usUsage = 0x02
            devs[0].dwFlags = RIDEV_INPUTSINK
            devs[0].hwndTarget = hwnd
            ctypes.windll.user32.RegisterRawInputDevices(
                ctypes.byref(devs), 1, ctypes.sizeof(RAWINPUTDEVICE))

        t = threading.Thread(target=proc, name="probe-raw", daemon=True)
        t.start()
        t.join(timeout=2.0)
    probe("③ 注册 Raw Input 监听", s3)

    # --- Step 4：全局热键（dashboard 用 RegisterHotKey 抢 F5/F6）---
    def s4():
        _user32.RegisterHotKey(None, 991, 0, 0x75)   # MOD_NONE + VK_F6
    probe("④ 注册全局热键 F6", s4)

    # --- Step 5：真实场景 A —— 你在仪表盘界面上按热键启动 ---
    # 【为什么必须先把 dashboard 顶到前台】
    # 对一个本来就在后台的窗口做 minimize，Windows 通常不动前台
    # —— 那样测出来的 OK 是假的，代表不了你的真实操作。
    # 你的真实路径是：人在仪表盘界面上按热键 → 程序 minimization 自己让位。
    # 所以必须先造出「dashboard 是前台」这个前提，再看焦点去了哪。
    if dash:
        def s5a():
            _user32.ShowWindow(dash, SW_RESTORE)      # 先确保它没被最小化
            time.sleep(0.2)
            _user32.SetForegroundWindow(dash)          # 模拟：你正在用它
            time.sleep(0.3)
            _user32.ShowWindow(dash, SW_MINIMIZE)      # 模拟：按热键后自动让位
        probe("⑤-A 界面上按热键→自己最小化", s5a)
    else:
        rows.append(("⑤-A 界面上按热键→自己最小化", "跳过（GUI 没开）", "-"))

    # --- Step 6：真实场景 B —— 你先切到游戏，再按热键 ---
    if dash:
        def s5b():
            try:
                _user32.ShowWindow(dash, SW_RESTORE)
            except Exception:
                pass
            time.sleep(0.2)
            try:
                _user32.SetForegroundWindow(target[0])   # 先把焦点还给游戏
            except Exception:
                pass
            time.sleep(0.3)
            _user32.ShowWindow(dash, SW_MINIMIZE)        # dashboard 在后台被最小化
        probe("⑤-B 已在游戏中按热键", s5b)

        # 测完把仪表盘还原，别让用户的 GUI 莫名消失在任务栏
        try:
            _user32.ShowWindow(dash, SW_RESTORE)
        except Exception:
            pass

    # --- 收尾：松开所有鼠标键，避免残留按下状态 ---（项目硬规则：测完验还原）
    try:
        from core import inputs
        for b in ("left", "right", "middle"):
            inputs.release(b)
    except Exception:
        pass

    # ------------------------------------------------------------------
    print("\n" + "=" * 58)
    print(" 结果")
    print("=" * 58)
    print("基线（切到游戏后的前台）：%s  pid=%s\n" % (target[2], target[1]))
    print("%-26s %s" % ("步骤", "前台是否被夺走"))
    print("-" * 58)
    for label, verdict, _ in rows:
        print("%-26s %s" % (label, verdict))
    print("-" * 58)

    bad = [r for r in rows if "被夺" in r[1]]
    if bad:
        print("\n👉 元凶候选：%s" % "、".join(r[0] for r in bad))
        print("   下一步：让 dashboard 在这一步之后把焦点交还游戏")
        print("   （SetForegroundWindow 到启动前的那个前台窗口）。")
    else:
        print("\n✅ 五步都没夺走前台 → 前台不是原因。")
        print("   那问题在更下层：把这份输出发给我，转查进程线程/输入队列层。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中止。")
    if not hasattr(sys, "frozen"):
        try:
            input("\n按回车关闭……")
        except Exception:
            pass
