# -*- coding: utf-8 -*-
"""
diag_globalpause_probe.py —— 全局暂停/恢复按钮 + 分区恢复语义 探针
=================================================================

【背景】2026-10-17 主人实测报回（原话）：
「右上角应该是全局恢复运行/暂停的按钮有个严重bug，当鼠标区和键盘区
都暂停的时候，点那个全局恢复运行/暂停按钮无论如何都不会恢复，但这个
时候按绑了键盘锁定的键（G1）就会全局恢复，已暂停的鼠标区也会恢复」。

launch.log 还原出的真实操作序列（01:49:45~01:50:00）：
    01:49:45  触发键 侧键2(G2)：鼠标连点 -> 暂停      ← 鼠标区暂停
    01:49:51  触发键 侧键1(G1)：键盘锁定 -> 暂停      ← 键盘区暂停
              （主人点全局按钮 —— 旧代码这里一行日志都不写）
    01:50:00  分区恢复：全局暂停仍在，一并解除        ← 全局位竟是 True！
    01:50:00  触发键 侧键1(G1)：键盘锁定 -> 恢复运行

两个真因，本探针各钉一组断言：

  真因 A（按钮「没反应」）：
    toggle_pause 是**切换**语义。主人连点/双击时第一次点击确实恢复了，
    但第二次点击又暂停回去，净效果为零 → 「无论如何都不会恢复」。
    后端本身一次点击就能正确恢复（场景 A 证明）；
    防抖在前端（ui/dashboard.html 的 600ms 守卫），见回归静态断言。

  真因 B（G1 恢复牵连鼠标区）：
    set_zone_paused 恢复某个区时，若全局暂停还开着，旧代码调
    pausectl.set_paused(False)（zone=None）—— 它把**两个区的标志
    一起清掉** → 按 G1 恢复键盘区，暂停中的鼠标区也跟着恢复。
    修复：改用 pausectl.clear_global()，只清全局位（场景 C/D 证明）。

【负向对照】把 clear_global 打回旧行为（= set_paused(False)），
场景 C/D 必须复现「别的区被牵连恢复」，否则说明断言根本没在测东西。

全程打桩：toast 全部换空操作，零弹窗、零真实注入。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dashboard as D                                     # noqa: E402
from core import pausectl                                  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s   %s" % (name, detail))


def reset_pause():
    """清干净所有暂停位（含全局）。"""
    pausectl.set_paused(False)


def make_api():
    """构造 Api，并把 toast / _log 换成空操作（探针不弹窗、不写日志）。"""
    api = D.Api()
    api.toast = _NullToast()
    return api


class _NullToast(object):
    """把 toast 模块的常用方法全吞掉。"""

    def __getattr__(self, name):
        def _noop(*a, **kw):
            return None
        return _noop


def zones_running(api):
    """把两个区设成「运行中」（暂停门闩只影响注入，不影响 running 位）。"""
    with D.ST.lock:
        D.ST.mouse_running = True
        D.ST.mouse_armed = True
        D.ST.key_running = True
        D.ST.key_armed = True


def snap():
    return {
        "global": pausectl.is_paused(),
        "mouse": pausectl.is_paused("mouse"),
        "key": pausectl.is_paused("key"),
        "any": pausectl.any_paused(),
    }


def main():
    api = make_api()

    # ---------------------------------------------------------------
    print("=" * 70)
    print("场景 A：两区都被触发键暂停 → 点一次全局按钮必须全部恢复")
    print("=" * 70)
    reset_pause()
    zones_running(api)
    api.set_zone_paused("mouse", None)      # 中键/G2 暂停鼠标区
    api.set_zone_paused("key", None)        # G1 暂停键盘区
    s = snap()
    check("前置：鼠标区已暂停", s["mouse"] is True, "snap=%r" % (s,))
    check("前置：键盘区已暂停", s["key"] is True, "snap=%r" % (s,))
    check("前置：全局位未置上（两个区暂停不等于全局暂停）",
          s["global"] is False, "snap=%r" % (s,))
    r = api.toggle_pause()
    s = snap()
    check("点一次全局按钮：全部恢复（any_paused=False）",
          s["any"] is False and r.get("paused") is False,
          "snap=%r r=%r" % (s, r))
    check("点一次全局按钮：两个区的暂停标志都清掉",
          s["mouse"] is False and s["key"] is False, "snap=%r" % (s,))

    # ---------------------------------------------------------------
    print()
    print("=" * 70)
    print("场景 B：连点两次（模拟双击）= 切两次 = 回到暂停态")
    print("=" * 70)
    reset_pause()
    zones_running(api)
    api.set_zone_paused("mouse", None)
    api.set_zone_paused("key", None)
    api.toggle_pause()                       # 第 1 次：恢复
    s1 = snap()
    api.toggle_pause()                       # 第 2 次：又暂停
    s2 = snap()
    check("第 1 次点击后确实恢复了", s1["any"] is False, "snap=%r" % (s1,))
    check("第 2 次点击后又暂停（这就是主人「点了没反应」的成因，"
          "所以前端必须防抖）", s2["any"] is True, "snap=%r" % (s2,))

    # ---------------------------------------------------------------
    print()
    print("=" * 70)
    print("场景 C：全局暂停中按 G1（键盘区触发键）→ 只恢复键盘区")
    print("=" * 70)
    reset_pause()
    zones_running(api)
    api.set_paused(True)                     # 全局暂停（按钮/热键）
    s = snap()
    check("前置：全局暂停把两个区都带成暂停", s["mouse"] and s["key"],
          "snap=%r" % (s,))
    api.set_zone_paused("key", None)         # 按 G1
    s = snap()
    check("G1 恢复后：键盘区恢复", s["key"] is False, "snap=%r" % (s,))
    check("G1 恢复后：全局位已解除", s["global"] is False, "snap=%r" % (s,))
    check("G1 恢复后：鼠标区**保持暂停**（不被牵连）",
          s["mouse"] is True, "snap=%r" % (s,))

    # ---------------------------------------------------------------
    print()
    print("=" * 70)
    print("场景 D：全局暂停中按 G2（鼠标区触发键）→ 只恢复鼠标区")
    print("=" * 70)
    reset_pause()
    zones_running(api)
    api.set_paused(True)
    api.set_zone_paused("mouse", None)       # 按 G2
    s = snap()
    check("G2 恢复后：鼠标区恢复", s["mouse"] is False, "snap=%r" % (s,))
    check("G2 恢复后：全局位已解除", s["global"] is False, "snap=%r" % (s,))
    check("G2 恢复后：键盘区**保持暂停**（不被牵连）",
          s["key"] is True, "snap=%r" % (s,))

    # ---------------------------------------------------------------
    print()
    print("=" * 70)
    print("场景 E：什么都没暂停 → 点全局按钮 = 全局暂停（两个区一起停）")
    print("=" * 70)
    reset_pause()
    zones_running(api)
    r = api.toggle_pause()
    s = snap()
    check("一键全局暂停：两个区都暂停", s["mouse"] and s["key"],
          "snap=%r" % (s,))
    check("返回值 paused=True（前端按钮文案靠它）",
          r.get("paused") is True, "r=%r" % (r,))

    # ---------------------------------------------------------------
    print()
    print("=" * 70)
    print("场景 F：负向对照 —— clear_global 打回旧行为，旧 bug 必须复现")
    print("=" * 70)
    # 旧实现：分区恢复时调 pausectl.set_paused(False)（zone=None），
    # 会把两个区的标志一起清掉。把它塞回去，场景 C 的「不牵连」断言
    # 必须变红，否则说明这条断言是假绿。
    orig_clear = pausectl.clear_global
    pausectl.clear_global = lambda: pausectl.set_paused(False)
    try:
        reset_pause()
        zones_running(api)
        api.set_paused(True)                 # 全局暂停
        api.set_zone_paused("key", None)     # 按 G1
        s = snap()
        check("负向对照：旧行为下鼠标区被牵连恢复（断言确实有效）",
              s["mouse"] is False, "snap=%r" % (s,))
        check("负向对照：旧行为下两个区都恢复了",
              s["mouse"] is False and s["key"] is False, "snap=%r" % (s,))
    finally:
        pausectl.clear_global = orig_clear
        reset_pause()

    # ---------------------------------------------------------------
    print()
    print("=" * 70)
    print("汇总")
    print("=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  [FAIL] %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
