# -*- coding: utf-8 -*-
"""
权限级别对比：注入工具 vs 目标进程
=================================

判定 UIPI 是否会拦截的唯一依据：两个进程的**完整性级别**对比。
低完整性 -> 高完整性 的注入会被 UIPI 静默丢弃，而 SendInput 仍返回成功。
"""
import ctypes
import sys
import os
from ctypes import wintypes as wt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

adv = ctypes.WinDLL("advapi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 签名：OpenProcess 在 kernel32，不在 advapi32（写错会 function not found）
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.GetCurrentProcess.argtypes = []
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetCurrentProcessId.argtypes = []
k32.GetCurrentProcessId.restype = wt.DWORD

adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
adv.OpenProcessToken.restype = wt.BOOL
adv.GetTokenInformation.argtypes = [
    wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
adv.GetTokenInformation.restype = wt.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25

LEVEL = {
    0x0000: "未分配",
    0x1000: "低完整性 (沙箱/受限)",
    0x2000: "中完整性 (普通用户)",
    0x3000: "高完整性 (管理员)",
    0x4000: "系统完整性",
}


def rid_from_buffer(buf):
    """
    从 TOKEN_MANDATORY_LABEL 里取 RID。

    结构：TOKEN_MANDATORY_LABEL { PSID Label; }，Label 指向一个 SID。
    buffer 内容 = 一个指针（8 字节），**不是 SID 本身**。
    所以必须先解引用这个指针，再从 SID 里读 RID。
    SID 布局：
        [0] 修订号Revision
        [1] 子权限个数SubAuthorityCount
        [2] 标识权限Authority
        [3:8] 子权限 SubAuthority[0] = RID  <-- RID 在这里
    """
    raw = bytes(buf)
    sid_addr = int.from_bytes(raw[0:8], "little")   # 先取指针
    if not sid_addr:
        return None
    # 从 SID 结构里读：偏移 8 起是 SubAuthority[0]
    rid = ctypes.c_uint32.from_address(sid_addr + 8).value
    return rid


def rid_value(pid=None):
    if pid is None:
        ph = k32.GetCurrentProcess()
    else:
        ph = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not ph:
            return None
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(ph, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    n = wt.DWORD()
    adv.GetTokenInformation(tok, TokenIntegrityLevel, None, 0, ctypes.byref(n))
    buf = ctypes.create_string_buffer(n.value)
    adv.GetTokenInformation(tok, TokenIntegrityLevel, buf, n.value,
                            ctypes.byref(n))
    return rid_from_buffer(buf)


def integrity(pid=None):
    """取进程的完整性级别描述字符串。"""
    r = rid_value(pid)
    if r is None:
        return "读取失败（进程权限高于本进程）"
    return "%s (RID=0x%04X)" % (LEVEL.get(r, "未知"), r)


def main():
    import diag_target as T

    print("=" * 70)
    print("权限级别对比")
    print("=" * 70)
    me = k32.GetCurrentProcessId()
    my_rid = rid_value()
    print("本进程 pid           : %d" % me)
    print("本进程完整性级别     : %s" % integrity())
    print()

    wins = T.list_windows()
    if not wins:
        print("没有可见窗口，无法对比。")
        return

    print("所有可见窗口的完整性级别：")
    rows = []
    for w in wins:
        r = rid_value(w.pid)
        tag = "  <= 本进程" if r == my_rid else (
            "  <= **高于本进程，UIPI 会拦截注入**" if r and r > my_rid else "")
        rows.append((w.title, w.pid, r, tag))

    for i, (t, pid, r, tag) in enumerate(rows):
        print("  [%2d] pid=%-7s RID=%-6s %r%s"
              % (i, pid, ("0x%04X" % r) if r is not None else "读取失败",
                 t[:30], tag))

    higher = [(t, pid) for (t, pid, r, _) in rows
              if r is not None and my_rid is not None and r > my_rid]
    print()
    if higher:
        print(">>> 有%d 个窗口的进程权限**高于本进程**：" % len(higher))
        for t, pid in higher:
            print("      pid=%s%r" % (pid, t[:40]))
        print(">>> 向这些进程注入键盘输入会被 **UIPI 静默丢弃**，")
        print(">>> 而 SendInput依然返回成功 —— 这就是「注入成功但没反应」的成因。")
        print(">>>")
        print(">>> 解决办法：把注入工具以管理员身份运行，")
        print(">>> 完整性级别变成 High，两边同级就不再拦截。")
    else:
        print(">>> 没有任何窗口权限高于本进程，UIPI 不是原因。")
        print(">>> 若注入仍无效，则目标程序在**主动丢弃注入事件**（反作弊）。")


if __name__ == "__main__":
    main()