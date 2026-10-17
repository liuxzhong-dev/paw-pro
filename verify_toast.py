# -*- coding: utf-8 -*-
"""
verify_toast.py —— 状态弹窗冒烟测试
弹一条真的窗口出来 3 秒，然后退出。
只验证「窗口能建、能显示、能自动消失」，不验证像素级外观。
"""
import sys
import time

sys.path.insert(0, ".")

from core import toast


def main():
    ok = toast.show("测试：鼠标连点 已启动（每秒 8 次）", "start", seconds=3)
    print("[show] returned:", ok)
    time.sleep(1.0)
    ok2 = toast.show("测试：已暂停", "pause", seconds=3)
    print("[show2] returned:", ok2)
    time.sleep(1.0)
    ok3 = toast.show("测试：已停止", "stop", seconds=3)
    print("[show3] returned:", ok3)
    print("等待自动消失（4 秒）...")
    time.sleep(4.0)
    toast.stop()
    print("[done] toast 冒烟测试通过（无异常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
