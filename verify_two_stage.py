# -*- coding: utf-8 -*-
"""
verify_two_stage.py —— 两段式状态机专项回归（按钮=就绪，热键=启停）
=====================================================================

【它验证什么】
2026-10-15 主人报的状态机 bug：
  「设定好参数以后点启动应该是准备就绪，然后按鼠标连点启停键
   （默认 F5）再开始，而不是像现在一样点启动就触发，
   点第一次 F5 就暂停」

旧行为：点「启动」按钮 = 立即起引擎；再按 F5（热键）= 停止
        → 主人看到「点第一次 F5 就暂停」。
新行为（两段式）：
  点「① 就绪」按钮 = 只装填参数 + 置就绪态，**不起引擎**；
  按 F5（就绪态）  = 真启动，且仪表盘自动让位（hide=True）；
  按 F5（运行中）  = 停止；
  运行中再点按钮   = 明确报错，不静默改参数。

同时覆盖键盘区同模式改造（arm_keyboard / toggle_keyboard /
stop_keyboard），以及三个「不报错但行为错」的历史坑：
  1. toggle_mouse 热键启动必须 hide=True（旧代码传 False，
     仪表盘还在前台时按 F5，连点全点在自己窗口上）
  2. 没点按钮直接按热键 = 用上次/默认参数启动（保留纯热键用法）
  3. hold 参数 "true"/"false" 字符串三步转 bool（含空串当缺省）

【测试手法】与 verify_mouse_zone.py 一致：全部打桩，**零真实注入**——
ClickerEngine / KeyHoldLock / inputs / toast / 提权检测全部换成桩，
绝不往系统发真实键鼠事件。
"""

import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def section(t):
    print("\n=== %s ===" % t)


def code_only_dashboard(path):
    """
    读 dashboard.py 并剥掉**三引号块和 # 注释行**，只留代码形态。

    【为什么必须剥 · 硬规则 38】静态断言查「某段代码在不在」时，
    注释里写「原来错在哪」会把断言带红（注释里会逐字出现旧写法的串）。
    Python/CSS/JS 三种注释语法都踩过，这里统一剥。
    """
    src = open(path, encoding="utf-8").read()
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


# ----------------------------------------------------------------------
# 桩：全部记录调用，不起任何真实线程、不发任何真实输入
# ----------------------------------------------------------------------
class FakeCtx(object):
    clicked = 0


class FakeEngine(object):
    """替身 ClickerEngine：记录 start/stop，不注入。"""
    instances = []

    def __init__(self, plugin, options=None, base_cps=0,
                 duration=0, count_limit=0, jitter_ms=0, fixed_pos=None):
        self.plugin = plugin
        self.base_cps = base_cps
        self.duration = duration
        self.count_limit = count_limit
        # 【v22】新接线参数：不记的话 start_mouse 一传就 TypeError，
        # 而且「抖动有没有传给引擎」这条断言也无从验证
        self.jitter_ms = jitter_ms
        self.fixed_pos = fixed_pos
        self.options = options
        self.started = False
        self.stopped = False
        self.stop_wait = None
        self.on_finish = None
        self.ctx = FakeCtx()
        FakeEngine.instances.append(self)

    def start(self):
        self.started = True
        return True

    def stop(self, wait=False):
        self.stopped = True
        self.stop_wait = wait


class FakeKeyHoldLock(object):
    """替身 KeyHoldLock：记录 start/stop，不装钩子、不注入。"""
    instances = []

    def __init__(self, keys, threshold_sec, repeat_hz, on_state_change,
                 panic_key, hold_down, pair_gap_ms):
        self.keys = keys
        self.threshold_sec = threshold_sec
        self.repeat_hz = repeat_hz
        self.on_state_change = on_state_change
        self.panic_key = panic_key
        self.hold_down = hold_down
        self.pair_gap_ms = pair_gap_ms
        self.started = False
        self.stopped = False
        FakeKeyHoldLock.instances.append(self)

    def start(self):
        self.started = True
        return (True, None)

    def stop(self):
        self.stopped = True


class FakeInputs(object):
    """替身 core.inputs：所有注入类调用只记录不发送。"""

    def __init__(self):
        self.calls = []

    def _rec(self, name, *a, **k):
        self.calls.append((name,) + a)
        return True

    def press_down(self, *a, **k):
        return self._rec("press_down", *a, **k)

    def release(self, *a, **k):
        return self._rec("release", *a, **k)

    def release_xbutton(self, *a, **k):
        return self._rec("release_xbutton", *a, **k)

    def click(self, *a, **k):
        return self._rec("click", *a, **k)

    def tap(self, *a, **k):
        return self._rec("tap", *a, **k)

    # 【v22】固定坐标的 clamp / 「取当前」按钮要用的三个方法。
    # 少了它们 _parse_mouse_params 会认为「固定坐标无效」而整体拒绝
    # arm——那是桩不全造成的假失败，不是产品 bug。
    def screen_size(self, *a, **k):
        return (1920, 1080)

    # 【v25】坐标 clamp 改按虚拟桌面算（副屏/负坐标）。桩必须给全，
    # 少一个方法 → 解析报「固定坐标无效」→ arm 早退 → 后面全灭
    # （v22 那次 FakeInputs 缺 screen_size 的教训，规则 17 的同类）。
    def virtual_screen(self, *a, **k):
        return (0, 0, 1920, 1080)

    def move_to(self, *a, **k):
        return self._rec("move_to", *a, **k)

    def get_cursor_pos(self, *a, **k):
        return (960, 540)


class FakeToast(object):
    """替身 core.toast：弹窗是锦上添花，测试里只记录不弹。"""

    def __init__(self):
        self.shown = []

    def __getattr__(self, name):
        def _rec(*a, **k):
            self.shown.append((name,) + a)
            return True
        return _rec


class FakeWindow(object):
    """替身 pywebview 窗口：只记录 minimize。"""

    def __init__(self):
        self.minimized = 0

    def minimize(self):
        self.minimized += 1


class PatchGuard(object):
    """进上下文打桩、出上下文还原，保证不污染同进程的其他测试。"""

    def __init__(self):
        import dashboard as D
        self.D = D
        self.saved = {}
        self.fake_inputs = FakeInputs()
        self.fake_toast = FakeToast()

    def __enter__(self):
        D = self.D
        self.saved["eng.ClickerEngine"] = D.eng.ClickerEngine
        self.saved["kb.KeyHoldLock"] = D.kb.KeyHoldLock
        self.saved["inp"] = D.inp
        self.saved["toast"] = D.toast
        self.saved["injection_blocked_by_uipi"] = \
            D.injection_blocked_by_uipi
        self.saved["our_elevated"] = D.our_elevated
        D.eng.ClickerEngine = FakeEngine
        D.kb.KeyHoldLock = FakeKeyHoldLock
        D.inp = self.fake_inputs
        D.toast = self.fake_toast
        # 提权检测置 False：测试环境的前台窗口不确定，
        # 不置的话看门狗可能随机置 elev_warn，断言会飘。
        D.injection_blocked_by_uipi = lambda: False
        D.our_elevated = lambda: False
        FakeEngine.instances = []
        FakeKeyHoldLock.instances = []
        return self

    def __exit__(self, *a):
        D = self.D
        D.eng.ClickerEngine = self.saved["eng.ClickerEngine"]
        D.kb.KeyHoldLock = self.saved["kb.KeyHoldLock"]
        D.inp = self.saved["inp"]
        D.toast = self.saved["toast"]
        D.injection_blocked_by_uipi = \
            self.saved["injection_blocked_by_uipi"]
        D.our_elevated = self.saved["our_elevated"]
        return False


def reset_state(api):
    """
    把共享 State 清回空闲，避免用例之间互相污染。

    【2026-10-16】必须连暂停标志一起清：触发键现在切的是「暂停/继续」
    而不是「停止」，用例收尾若只按触发键，pausectl 的分区标志会留到
    下一个用例 —— 表现为后面某个用例「引擎起来了却 clicked 永远是 0」。
    """
    import dashboard as D
    D.pausectl.set_paused(False)          # 清全局
    D.pausectl.set_paused(False, "mouse")
    D.pausectl.set_paused(False, "key")
    with D.ST.lock:
        D.ST.mouse_running = False
        D.ST.mouse_engine = None
        D.ST.mouse_info = ""
        D.ST.mouse_armed = False
        D.ST.mouse_msg = ""
        D.ST.started_at = 0
        D.ST.key_running = False
        D.ST.key_lock = None
        D.ST.key_info = ""
        D.ST.key_armed = False
        D.ST.key_msg = ""
        D.ST.elev_warn = False


MOUSE_PARAMS = {"button": "left", "action": "click",
                "count": 0, "duration": 0, "cps": 10,
                # v22 新增四项（缺省 = 关）
                "jitter": 0, "mode": "cursor", "pos": "", "countdown": 0,
                # v23 新增两项：抖器挪动间隔 / 收尾动作（缺省 = 关）
                "jig": 30, "done": "none"}
KEY_PARAMS = {"keys": "w", "threshold": 2, "hz": 30,
              "hold": "true", "gap": 20}


# ----------------------------------------------------------------------
# 鼠标区
# ----------------------------------------------------------------------
def test_arm_mouse_only_ready():
    section("鼠标区 · 点「① 就绪」只装填参数，不起引擎")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        r = api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        check("arm_mouse 返回 ok", r.get("ok") is True, r.get("msg"))
        check("提示语说「已就绪」并指向 F5",
              "就绪" in r.get("msg", "") and "F5" in r.get("msg", ""),
              r.get("msg"))
        check("mouse_armed = True", D.ST.mouse_armed is True)
        check("mouse_running 仍为 False（没有真启动）",
              D.ST.mouse_running is False)
        check("没有创建任何引擎", len(FakeEngine.instances) == 0,
              "实际创建了 %d 个" % len(FakeEngine.instances))
        check("参数已记住（热键启动要用）",
              api.last_mouse.get("button") == "left"
              and api.last_mouse.get("cps") == 10)
        check("记住「启动后最小化」勾选",
              api._mouse_hide_on_start is True)
        # 就绪态消息要写进鼠标分区（不是全局 message）
        check("就绪消息写入鼠标分区",
              "F5" in (D.ST.mouse_msg or ""), D.ST.mouse_msg)


def test_arm_mouse_rejects_when_running():
    section("鼠标区 · 运行中再点「① 就绪」= 明确报错")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        api.toggle_mouse()                      # 就绪 -> 真启动
        check("前置条件：已在运行", D.ST.mouse_running is True)
        r = api.arm_mouse({"button": "right", "action": "click",
                           "count": 0, "duration": 0, "cps": 5})
        check("运行中装填被拒绝", r.get("ok") is False, r.get("msg"))
        check("拒绝信息提示先按热键停止",
              "停止" in r.get("msg", ""), r.get("msg"))
        check("拒绝时运行状态不被破坏", D.ST.mouse_running is True)
        api.stop_mouse()                        # 收尾：真正结束


def test_toggle_from_armed_starts():
    section("鼠标区 · 就绪态按 F5 = 真启动（引擎起来 + 就绪态清除）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        r = api.toggle_mouse()
        check("toggle 返回 ok", r.get("ok") is True, r.get("msg"))
        check("创建了 1 个引擎", len(FakeEngine.instances) == 1,
              "实际 %d 个" % len(FakeEngine.instances))
        eng = FakeEngine.instances[-1]
        check("引擎已 start", eng.started is True)
        check("mouse_running = True", D.ST.mouse_running is True)
        check("mouse_armed 已清除（不再是就绪态）",
              D.ST.mouse_armed is False)
        check("mouse_info 有内容", bool(D.ST.mouse_info), D.ST.mouse_info)
        api.stop_mouse()                        # 收尾：真正结束


def test_toggle_running_pauses():
    """
    【2026-10-16 改规格】触发键不再是「停止」，而是「暂停 / 继续」。

    主人实测反馈：「就绪后按中键开始，再按中键就结束变未就绪了，
    想再按中键继续连点没反应——结束键等于没有作用」。

    所以这里断言的是**反面**：
      · 按触发键后引擎不能停、running 必须还是 True
      · 真正结束只有 stop_mouse，且它才把 armed 打回 False
    """
    section("鼠标区 · 运行中按触发键 = 暂停（不是停止，更不回未就绪）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        api.toggle_mouse()
        eng = FakeEngine.instances[-1]
        r = api.toggle_mouse()
        check("暂停返回 ok", r.get("ok") is True, r.get("msg"))
        check("引擎**没有**被 stop（暂停≠停止）", eng.stopped is not True)
        check("mouse_running 仍是 True（否则下次按触发键会撞未就绪）",
              D.ST.mouse_running is True)
        check("mouse_engine 仍在（暂停保持运行态）",
              D.ST.mouse_engine is not None)
        check("鼠标区处于暂停", D.pausectl.is_paused("mouse") is True)
        check("键盘区不受影响（分区暂停）",
              D.pausectl.is_paused("key") is False)

        # 再按一次 = 继续
        r2 = api.toggle_mouse()
        check("继续返回 ok", r2.get("ok") is True, r2.get("msg"))
        check("已恢复（鼠标区不再暂停）",
              D.pausectl.is_paused("mouse") is False)
        check("恢复后引擎仍未 stop", eng.stopped is not True)
        check("恢复后 mouse_running 仍为 True",
              D.ST.mouse_running is True)

        # 只有「停止」才真正结束
        api.stop_mouse()
        check("停止后引擎已 stop", eng.stopped is True)
        check("停止后 mouse_running = False", D.ST.mouse_running is False)
        check("停止后 mouse_engine 已清空", D.ST.mouse_engine is None)
        check("停止后 mouse_armed = False（回到未就绪）",
              D.ST.mouse_armed is False)
        check("停止顺手清掉本区暂停",
              D.pausectl.is_paused("mouse") is False)
        check("elev_warn 已清除", D.ST.elev_warn is False)


def test_zone_pause_is_independent():
    """
    分区暂停：鼠标区暂停时键盘锁定照跑，反之亦然。

    原神里「普攻暂停、前进继续」这种组合靠它 —— 主人明确要的是
    「中键只停鼠标区」，而不是全局一起冻住。
    """
    section("分区暂停 · 鼠标与键盘互不牵连")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        api.arm_keyboard(dict(KEY_PARAMS))
        api.toggle_mouse()
        api.toggle_keyboard()
        check("前置：两区都在跑",
              D.ST.mouse_running is True and D.ST.key_running is True)
        api.toggle_mouse()                  # 只停鼠标区
        check("鼠标区已暂停", D.pausectl.is_paused("mouse") is True)
        check("键盘区**没有**被暂停",
              D.pausectl.is_paused("key") is False)
        check("键盘区仍在运行", D.ST.key_running is True)
        # 全局暂停键一次停两区（老行为不变）
        api.set_paused(True)
        check("全局暂停：鼠标区停", D.pausectl.is_paused("mouse") is True)
        check("全局暂停：键盘区也停", D.pausectl.is_paused("key") is True)
        api.set_paused(False)
        check("全局恢复：两区都恢复",
              D.pausectl.is_paused("mouse") is False and
              D.pausectl.is_paused("key") is False)
        api.stop_mouse()
        api.stop_keyboard()


def test_v21_global_pause_and_profile_memory():
    """
    v21（2026-10-17 主人实测报回）—— 全局暂停按钮 + 分区恢复牵连 + 方案空壳。

    主人原话（两条 bug 一次报回）：
      ①「右上角应该是全局恢复运行/暂停的按钮有个严重bug，当鼠标区和
         键盘区都暂停的时候，点那个全局恢复运行/暂停按钮无论如何都不会
         恢复，但这个时候按绑了键盘锁定的键（G1）就会全局恢复，已暂停的
         鼠标区也会恢复」
      ②「我把 v19 保存的『默认原神』方案复制到 profiles 文件夹下，选择后
         点加载，显示配置里没有可识别参数」

    真因与防线（逐条钉在这里，改动语义就会红）：
      A. 「无论如何都不会恢复」= toggle 语义 + 连点/双击净效果为零
         （后端单次点击本就正确，防线是前端 600ms 防抖，静态断言在
         test_html_wiring 的 v21 段）
      B. 「G1 恢复牵连鼠标区」= 分区恢复时旧代码调 set_paused(False)
         （zone=None）把两个区的标志一起清掉 → 改用 clear_global()
         只清全局位（本用例功能级 + 负向对照双保险）
      C. 「方案没有可识别参数」= save_settings 只写盘不更新
         self._settings，而 _export_dict() 读内存 → 全新启动时导出
         {"schema_version": 1} 空壳（本用例功能级 + 负向对照）
    """
    section("v21 · 全局暂停不牵连 + 分区恢复不越界 + 方案不存空壳")
    import dashboard as D
    api = D.Api()
    reset_state(api)

    # ---- B：全局暂停中按分区触发键，只恢复该区 ----
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        api.arm_keyboard(dict(KEY_PARAMS))
        api.toggle_mouse()
        api.toggle_keyboard()
        check("前置：两区都在跑",
              D.ST.mouse_running is True and D.ST.key_running is True)
        api.set_paused(True)                      # 全局暂停（按钮/热键）
        check("全局暂停把两个区都带成暂停",
              D.pausectl.is_paused("mouse") is True
              and D.pausectl.is_paused("key") is True)
        api.set_zone_paused("key", None)          # 按 G1（键盘区触发键）
        check("G1 恢复后：键盘区恢复",
              D.pausectl.is_paused("key") is False)
        check("G1 恢复后：全局位已解除",
              D.pausectl.is_paused() is False)
        check("G1 恢复后：鼠标区**保持暂停**（不被牵连，bug 1 主判据）",
              D.pausectl.is_paused("mouse") is True)
        api.set_zone_paused("mouse", None)        # 再按 G2
        check("G2 恢复后：鼠标区也恢复",
              D.pausectl.is_paused("mouse") is False)
        check("G2 恢复后：键盘区**保持运行**",
              D.ST.key_running is True)
        api.stop_mouse()
        api.stop_keyboard()

    # ---- A：两区都被触发键暂停 → 点一次全局按钮全部恢复 ----
    reset_state(api)
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        api.arm_keyboard(dict(KEY_PARAMS))
        api.toggle_mouse()
        api.toggle_keyboard()
        api.toggle_mouse()                        # G2 暂停鼠标区
        api.toggle_keyboard()                     # G1 暂停键盘区
        check("前置：两个区都被触发键暂停（全局位未置上）",
              D.pausectl.is_paused("mouse") is True
              and D.pausectl.is_paused("key") is True
              and D.pausectl.is_paused() is False)
        r = api.toggle_pause()
        check("点一次全局按钮：全部恢复（bug 1 主人场景）",
              D.pausectl.any_paused() is False
              and r.get("paused") is False, "r=%r" % (r,))
        check("点一次全局按钮：两个区的暂停标志都清掉",
              D.pausectl.is_paused("mouse") is False
              and D.pausectl.is_paused("key") is False)
        api.stop_mouse()
        api.stop_keyboard()

    # ---- C：save_settings 必须同步内存（否则存出来的是空壳方案）----
    section("v21 · 存盘必须同步内存（bug 2 主判据）")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="v21_prof_")
    saved_dir = D._settings_dir
    try:
        D._settings_dir = lambda: tmp
        api2 = D.Api()
        api2._settings = {}                       # 模拟刚启动、无配置文件
        data = {
            "mouse": {"button": "left", "action": "click", "count": "0",
                      "duration": "0", "cps": "37", "hide": True},
            "key": {"keys": "w", "threshold": "1.5", "hz": "25",
                    "hold": "true", "gap": "15"},
            "trigger": {"raw_id": "g2"},
            "hide": True,
        }
        r = api2.save_settings(data)
        check("save_settings 返回 ok", r.get("ok") is True, r.get("msg"))
        check("保存后内存里就有 mouse/key（导出读它）",
              isinstance(api2._settings.get("mouse"), dict)
              and isinstance(api2._settings.get("key"), dict),
              "_settings=%r" % (api2._settings,))
        d = api2._export_dict()
        check("导出字典带 mouse/key（不再是 27 字节空壳）",
              "mouse" in d and "key" in d, "d=%r" % (d,))
        pr = api2.save_profile("v21方案")
        check("存为方案成功", pr.get("ok") is True, pr.get("msg"))
        lr = api2.load_profile("v21方案")
        check("加载该方案成功（不再报「没有可识别的参数」）",
              lr.get("ok") is True, lr.get("msg"))
        check("加载后内存被更新（界面会跟着刷新）",
              api2._settings.get("mouse", {}).get("cps") == "37",
              "%r" % (api2._settings,))
        api2.delete_profile("v21方案")

        # ---- 负向对照：把内存合并打回旧行为（只写盘）→ 空壳必须复现 ----
        def _old_save_settings(self, d):
            ok, err = D._save_settings(d)
            if isinstance(d, dict):
                if isinstance(d.get("mouse"), dict):
                    self.last_mouse.update(d["mouse"])
                if isinstance(d.get("key"), dict):
                    self.last_key.update(d["key"])
            return {"ok": ok, "msg": err}
        orig_save = D.Api.save_settings
        D.Api.save_settings = _old_save_settings
        try:
            api3 = D.Api()
            api3._settings = {}
            api3.save_settings(data)
            d3 = api3._export_dict()
            check("负向对照：旧行为导出空壳（bug 2 复现，断言有效）",
                  "mouse" not in d3 and "key" not in d3, "d3=%r" % (d3,))
            pr3 = api3.save_profile("v21空壳")
            lr3 = api3.load_profile("v21空壳")
            check("负向对照：空方案加载报错（旧 bug 复现）",
                  lr3.get("ok") is False
                  and "没有可识别的参数" in lr3.get("msg", ""),
                  "lr3=%r" % (lr3,))
            api3.delete_profile("v21空壳")
        finally:
            D.Api.save_settings = orig_save
    finally:
        D._settings_dir = saved_dir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_stop_mouse_clears_armed():
    section("鼠标区 · 就绪态直接点「停止」= 连就绪态一起清掉")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        check("前置：mouse_armed = True", D.ST.mouse_armed is True)
        r = api.stop_mouse()
        check("stop_mouse 返回 ok", r.get("ok") is True, r.get("msg"))
        check("mouse_armed 已清除", D.ST.mouse_armed is False)
        check("没有起过引擎", len(FakeEngine.instances) == 0,
              "就绪态点停止不该碰引擎")


def test_v22_mouse_zone_features():
    """
    v22（2026-10-17 参考 GitHub 同类程序优化）—— 鼠标区新功能接线。

    调研 6 个真实开源项目（EC-Autoclicker / WinAutoClick / yankivare /
    OP Auto Clicker / AutoFire / FluAutoClicker）后确认：随机间隔、
    点击类型（双击/三击）、固定坐标、启动倒计时是同类标配，而本项目
    **引擎层早就实现**（core/engine.py 的 jitter_ms / fixed_pos /
    click_type，cli.py 也有 --jitter / --mode fixed --pos）——
    只有 dashboard 没接线。本版本接线。

    功能级验证在 diag_v22_features_probe.py（72 项，含 3 条负向对照）；
    这里钉**接线本身**：参数必须一路传到引擎/插件，界面控件必须真的
    存在且被 JS 收集，缺省值必须全关。
    """
    section("v22 · 新功能接线（抖动 / 点击类型 / 固定坐标 / 倒计时）")
    import dashboard as D

    # ---- 静态：界面控件与 JS 收集（查内嵌 HTML = 查 exe 里那份）----
    html = D.HTML
    for fid in ("mJitter", "mMode", "mPos", "mCd", "mPosRow"):
        check("界面有 #%s 控件" % fid, ('id="%s"' % fid) in html)
    check("动作下拉含双击/三击",
          'value="double"' in html and 'value="triple"' in html)
    check("JS 收集 jitter/mode/pos/countdown",
          'jitter: $("mJitter").value' in html
          and 'mode: $("mMode").value' in html
          and 'pos: $("mPos").value' in html
          and 'countdown: $("mCd").value' in html)
    check("联动显隐函数存在（固定坐标行 / 长按禁用倒计时）",
          "function syncMouseRows" in html)
    check("取当前坐标函数存在", "async function recordPos" in html)
    check("取坐标走 api.cursor_pos", "api.cursor_pos" in html)

    # ---- 静态：Python 侧接线（剥注释，硬规则 38）----
    py = code_only_dashboard(os.path.join(HERE, "dashboard.py"))
    check("引擎创建传 jitter_ms", 'jitter_ms=p["jitter"]' in py)
    check("引擎创建传 fixed_pos", "fixed_pos=p[\"pos\"]" in py)
    # v23：options 抽成 engine_options —— 抖器分支要另一套 options
    # （interval/distance），其余分支仍是位置模式。断言改成「两段都在」，
    # 比钉死一行更贴合意图：引擎必须收到 mode，抖器必须收到 interval。
    # v26 再改：多点/框内的坐标由插件自己搬，引擎的 options.mode 只认
    # cursor/fixed（否则 cursor_position() 会对别的插件撒谎）。
    # 断言改为「映射表达式 + 传参」两段，意图不变。
    check("引擎创建传位置模式",
          'engine_options = {"mode": "fixed" if p["mode"] == "fixed"' in py
          and "options=engine_options" in py)
    check("多点/框内不把坐标塞给引擎（fixed_pos 只给 fixed）",
          'engine_fixed = p["pos"] if p["mode"] == "fixed" else None' in py
          and "fixed_pos=engine_fixed" in py)
    check("抖器分支传 interval/distance",
          'engine_options = {"interval": p["jig"], "distance": 1}' in py)
    check("_make_mouse_plugin 收点击类型/坐标/倒计时",
          "click_type=action if action != \"hold\" else \"single\"" in py)
    check("共享参数解析函数存在", "def _parse_mouse_params(params)" in py)
    check("arm_mouse 与 start_mouse 都走共享解析",
          py.count("_parse_mouse_params(params or {})") >= 2)
    check("Api 暴露 cursor_pos", "def cursor_pos(self)" in py)
    check("toast 支持抖动提示",
          'jitter=0' in code_only_dashboard(
              os.path.join(HERE, "core", "toast.py")))

    # ---- 功能：参数必须传到引擎和插件 ----
    api = D.Api()
    reset_state(api)
    FakeEngine.instances = []
    with PatchGuard():
        r = api.arm_mouse({"button": "left", "action": "double", "count": 5,
                           "duration": 0, "cps": 20, "jitter": 35,
                           "mode": "fixed", "pos": "640,360",
                           "countdown": 3}, hide=False)
        check("v22 全参数就绪成功", r.get("ok") is True, r)
        api.start_mouse(api.last_mouse, hide=False)
        eng_ = FakeEngine.instances[-1] if FakeEngine.instances else None
        check("启动时创建了引擎", eng_ is not None)
        if eng_:
            check("抖动传给引擎（jitter_ms=35）", eng_.jitter_ms == 35,
                  eng_.jitter_ms)
            check("固定坐标传给引擎（已 clamp 成列表）",
                  eng_.fixed_pos == [640, 360], eng_.fixed_pos)
            check("位置模式传给引擎 options",
                  (eng_.options or {}).get("mode") == "fixed", eng_.options)
            pl = eng_.plugin
            check("点击类型进插件（双击 = 每轮 2 下）",
                  getattr(pl, "per_tick", None) == 2,
                  getattr(pl, "per_tick", None))
            check("固定坐标进插件",
                  getattr(pl, "fixed_pos", None) == (640, 360),
                  getattr(pl, "fixed_pos", None))
            check("倒计时进插件", getattr(pl, "countdown", None) == 3,
                  getattr(pl, "countdown", None))
        api.stop_mouse()

    # ---- 缺省值：旧参数（无新键）不该炸，也不该开启任何新功能 ----
    FakeEngine.instances = []
    with PatchGuard():
        api.arm_mouse({"button": "left", "action": "click", "count": 0,
                       "duration": 0, "cps": 10}, hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        eng2 = FakeEngine.instances[-1] if FakeEngine.instances else None
        check("旧参数启动不炸且引擎建成", eng2 is not None)
        if eng2:
            check("旧参数：抖动为 0（新功能默认关）", eng2.jitter_ms == 0)
            check("旧参数：无固定坐标", eng2.fixed_pos is None)
            check("旧参数：插件不倒计时",
                  getattr(eng2.plugin, "countdown", None) == 0)
            check("旧参数：插件每轮 1 下",
                  getattr(eng2.plugin, "per_tick", None) == 1)
        api.stop_mouse()

    # ---- 长按归一：hold + countdown/fixed 应被清掉（不静默保留）----
    with PatchGuard():
        api.arm_mouse({"button": "left", "action": "hold", "count": 0,
                       "duration": 0, "cps": 10, "countdown": 5,
                       "mode": "fixed", "pos": "10,10"}, hide=False)
        check("长按：倒计时被归零",
              api.last_mouse.get("countdown") == 0, api.last_mouse)
        check("长按：坐标被归一成跟随光标",
              api.last_mouse.get("mode") == "cursor"
              and api.last_mouse.get("pos") is None, api.last_mouse)
        api.stop_mouse()


def test_v23_features():
    """
    v23（2026-10-17 主人拍板「开工」，S1~S6 排队，本版做小的三个）——

      S4 完成后动作：次数/时长自然用完才触发提示音/锁屏/关机；
         用户停止绝不触发（mouse_stop_requested 标志 + stop_mouse 取消）；
         关机 30 秒倒计时可取消，系统层 shutdown /a 兜底。
      S5 CLI --profile：命令行按名字加载图形版方案（profiles/），
         加载失败大声报错；方案目录规则统一下沉 core/config.py。
      S6 鼠标抖器：jiggle 动作走 plugins/jiggler（只挪不点）。

    功能级验证在 diag_v23_features_probe.py（130 项，含负向对照）；
    这里钉**接线本身**（与 v22 段同一分工）。
    """
    section("v23 · S4 收尾动作 / S5 CLI 方案 / S6 抖器接线")
    import dashboard as D

    # ---- 静态：界面控件与 JS（查内嵌 HTML = 查 exe 里那份）----
    html = D.HTML
    for fid in ("mJig", "mDone", "mJigRow", "doneCancelRow"):
        check("界面有 #%s 控件" % fid, ('id="%s"' % fid) in html)
    check("动作下拉含抖器", 'value="jiggle"' in html)
    check("收尾动作三个选项齐全",
          'value="sound"' in html and 'value="lock"' in html
          and 'value="shutdown"' in html)
    check("JS 收集 jig/done",
          'jig: $("mJig").value' in html
          and 'done: $("mDone").value' in html)
    check("collectSettings/applySettings 带 v23 新键",
          'jig: $("mJig").value' in html
          and '$("mJig").value   = m.jig' in html
          and '$("mDone").value  = m.done' in html)
    check("取消收尾动作函数存在", "async function cancelDone" in html)
    check("轮询渲染取消按钮显隐", "doneCancelRow" in html)

    # ---- 静态：Python 侧接线（剥注释，硬规则 38）----
    py = code_only_dashboard(os.path.join(HERE, "dashboard.py"))
    check("_MOUSE_ACTIONS 含 jiggle", '"jiggle"' in py)
    check("_DONE_ACTIONS 白名单存在", "_DONE_ACTIONS" in py)
    check("start_mouse 有抖器分支（走 Jiggler 插件）",
          "from plugins.jiggler import Jiggler" in py)
    check("抖器分支 options 带 interval",
          '"interval": p["jig"]' in py)
    check("_finished 区分自然完成与用户停止",
          "mouse_stop_requested" in py and "natural" in py)
    check("_finished 排定收尾动作", 'actions.arm(p["done"]' in py)
    check("stop_mouse 取消收尾动作（停止=不执行）",
          "actions.cancel()" in py)
    check("Api 暴露 cancel_done_action", "def cancel_done_action(self)" in py)
    check("snapshot 暴露 done_pending", '"done_pending"' in py)
    # v23：委托时要把本进程设置目录传进去 —— 否则图形版可重定向的
    # _settings_dir() 与 core.config 自算的目录会分叉（测试临时目录、
    # 只读盘退回 LOCALAPPDATA 两种场景都会「界面存这、CLI 找那」）。
    check("方案目录委托 core.config（S5 两边一致）",
          "cfg.profiles_dir(_settings_dir())" in py
          and "cfg.profile_path(name, _settings_dir())" in py)
    check("core/actions 模块存在且含兜底撤销",
          os.path.isfile(os.path.join(HERE, "core", "actions.py"))
          and "shutdown" in code_only_dashboard(
              os.path.join(HERE, "core", "actions.py")))
    check("plugins/jiggler.py 存在",
          os.path.isfile(os.path.join(HERE, "plugins", "jiggler.py")))
    check("BUILTIN_MODULES 含 jiggler（打包静态收集）",
          '"jiggler"' in code_only_dashboard(
              os.path.join(HERE, "plugins", "__init__.py")))
    cli_py = code_only_dashboard(os.path.join(HERE, "cli.py"))
    check("cli 有 --profile", '"--profile"' in cli_py)
    check("cli 有 data_from_profile", "def data_from_profile" in cli_py)
    check("cli 自然完成排定收尾动作", "_arm_done_action(data)" in cli_py)

    # ---- 功能：抖器分支必须拿到 Jiggler 插件和 interval ----
    api = D.Api()
    reset_state(api)
    FakeEngine.instances = []
    with PatchGuard():
        r = api.arm_mouse({"button": "left", "action": "jiggle", "jig": 12,
                           "count": 5, "duration": 0, "cps": 1,
                           "done": "sound"}, hide=False)
        check("抖器就绪成功", r.get("ok") is True, r)
        check("jig/done 进了 last_mouse（热键路径同参）",
              api.last_mouse.get("jig") == 12
              and api.last_mouse.get("done") == "sound", api.last_mouse)
        api.start_mouse(api.last_mouse, hide=False)
        eng_ = FakeEngine.instances[-1] if FakeEngine.instances else None
        check("抖器启动创建了引擎", eng_ is not None)
        if eng_:
            check("抖器引擎插件是 Jiggler",
                  type(eng_.plugin).__name__ == "Jiggler",
                  type(eng_.plugin).__name__)
            check("抖器 options 带 interval=12",
                  (eng_.options or {}).get("interval") == 12, eng_.options)
            check("抖器 options 带 distance=1",
                  (eng_.options or {}).get("distance") == 1, eng_.options)
        api.stop_mouse()

    # ---- 长按归一：hold 的收尾动作必须归零（press/release 无完成事件）----
    FakeEngine.instances = []
    with PatchGuard():
        api.arm_mouse({"button": "left", "action": "hold", "count": 0,
                       "duration": 0, "cps": 10, "done": "shutdown"},
                      hide=False)
        check("长按：收尾动作归零为 none",
              api.last_mouse.get("done") == "none", api.last_mouse)
        api.stop_mouse()

    # ---- 缺省值：旧参数不带新键也不炸、不开启 ----
    FakeEngine.instances = []
    with PatchGuard():
        api.arm_mouse({"button": "left", "action": "click", "count": 0,
                       "duration": 0, "cps": 10}, hide=False)
        check("旧参数：done 缺省 none",
              api.last_mouse.get("done") == "none", api.last_mouse)
        check("旧参数：jig 缺省 30",
              api.last_mouse.get("jig") == 30, api.last_mouse)
        api.start_mouse(api.last_mouse, hide=False)
        eng2 = FakeEngine.instances[-1] if FakeEngine.instances else None
        check("旧参数：点击动作仍走 DynClick（不变成抖器）",
              eng2 is not None
              and type(eng2.plugin).__name__ == "DynClick",
              type(eng2.plugin).__name__ if eng2 else None)
        api.stop_mouse()


def test_v24_tray():
    section("v24 · 系统托盘（关窗藏/退决策 · 设置白名单 · 前端控件）")
    import dashboard as D

    py = code_only_dashboard(os.path.join(HERE, "dashboard.py"))
    html = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    # 剥 HTML/JS 注释再查（规则 38：注释里的旧串会把断言带红）
    js = re.sub(r"(?m)//.*$", "", re.sub(r"/\*[\s\S]*?\*/", "",
                re.sub(r"<!--[\s\S]*?-->", "", html)))

    # ---- 静态接线 ----
    check("导入托盘模块", "from core import tray as tray_mod" in py)
    check("main() 启动托盘", "_tray_start(api)" in py)
    check("关闭事件走模块级决策", "return _on_window_closing(api)" in py)
    check("shutdown 撤图标", "_tray_stop()" in py)
    check("save_settings 内存白名单含 tray", '"hide", "tray"' in py)
    check("Api 暴露 hide_to_tray/show_from_tray",
          "def hide_to_tray(self)" in py
          and "def show_from_tray(self)" in py)
    check("core/tray.py 存在且实现 Shell_NotifyIconW",
          os.path.isfile(os.path.join(HERE, "core", "tray.py"))
          and "Shell_NotifyIconW" in code_only_dashboard(
              os.path.join(HERE, "core", "tray.py")))
    check("窗口过程用回调 hwnd（不是 self._hwnd）",
          "DefWindowProcW(hwnd, msg, wparam, lparam)" in
          code_only_dashboard(os.path.join(HERE, "core", "tray.py")))
    check("前端有「关闭到托盘」勾选框", 'id="mTray"' in html)
    check("collectSettings 收集 tray", 'tray:  $("mTray").checked' in js)
    check("applySettings 恢复 tray", "if(s.tray != null)" in js)
    check("自动保存 ids 含 mTray", '"mDone","mTray"' in js)

    # ---- 功能：三态决策 ----
    class FakeApi(object):
        def __init__(self, settings):
            self._settings = settings
            self.hidden = 0
            self.shutdowned = 0

        def hide_to_tray(self):
            self.hidden += 1
            return {"ok": True}

        def shutdown(self):
            self.shutdowned += 1

    old = D._TRAY["obj"]
    real_watchdog = D._arm_exit_watchdog
    D._arm_exit_watchdog = lambda *a, **k: None
    try:
        D._TRAY["obj"] = object()
        check("勾了关闭到托盘 -> 关窗该藏",
              D._tray_should_hide(FakeApi({"tray": True})) is True)
        check("没勾 -> 关窗该退",
              D._tray_should_hide(FakeApi({"tray": False})) is False)
        api = FakeApi({"tray": True})
        ret = D._on_window_closing(api)
        for _ in range(50):
            if api.hidden:
                break
            time.sleep(0.02)
        check("关窗拦截：返回 True 且调了 hide_to_tray",
              ret is True and api.hidden == 1, (ret, api.hidden))
        check("关窗拦截：不调 shutdown（任务要继续）",
              api.shutdowned == 0, api.shutdowned)
        api2 = FakeApi({"tray": False})
        ret2 = D._on_window_closing(api2)
        check("不勾托盘：放行关闭并收尾",
              ret2 is None and api2.shutdowned == 1, (ret2, api2.shutdowned))
        # 负向对照：没有图标对象时绝不能藏（藏了就再也叫不回来）
        D._TRAY["obj"] = None
        check("负向对照：无图标对象时不藏",
              D._tray_should_hide(FakeApi({"tray": True})) is False)
    finally:
        D._TRAY["obj"] = old
        D._arm_exit_watchdog = real_watchdog

    check("内嵌 HTML == ui/dashboard.html（v24 双源一致）", D.HTML == html,
          (len(D.HTML), len(html)))


def test_v27_macro():
    section("v27 · 宏录制回放（钩子过滤 · 回放时序 · 存储 · 前端分区）")
    import dashboard as D

    py = code_only_dashboard(os.path.join(HERE, "dashboard.py"))
    html = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    js = re.sub(r"(?m)//.*$", "", re.sub(r"/\*[\s\S]*?\*/", "",
                re.sub(r"<!--[\s\S]*?-->", "", html)))
    rec_src = code_only_dashboard(os.path.join(HERE, "core", "recorder.py"))
    rep_src = code_only_dashboard(os.path.join(HERE, "plugins",
                                              "macro_replay.py"))

    from core import recorder as R
    from plugins.macro_replay import MacroReplayPlugin

    # ---- 录制器：三条硬要求 ----
    check("hMod 传 NULL（传模块句柄会装了但收不到，实测）",
          R._module_handle() is None)
    check("先认自己的注入标记，再看 INJECTED 总闸（顺序不能反）",
          rec_src.index("int(extra) == MARK_INJECTED")
          < rec_src.index("self.ignore_injected and"))
    check("钩子回调整体包 try（抛出去会被系统悄悄摘钩）",
          "except Exception:" in rec_src
          and "CallNextHookEx(None, ncode, wparam, lparam)" in rec_src)
    check("stop 时给按着的键补抬起（否则回放时一直按着）",
          "_flush_held()" in rec_src)
    check("鼠标移动做抽样（不抽的话一秒上千条）",
          "dx < 4 and dy < 4" in rec_src)

    # ---- 回放插件 ----
    check("回放走 ctx.wait（才有暂停/停止）而不是 time.sleep",
          "ctx.wait(step)" in rep_src and "time.sleep" not in rep_src)
    check("回放收尾在 finally 里释放按键",
          "finally:" in rep_src and "_release_all" in rep_src)
    check("回放校验事件表（脏数据不进注入层）",
          "validate_events(events)" in rep_src)

    # ---- 存储 ----
    from core import config as cfg
    check("safe_name 挡路径穿越", cfg.safe_name("../../x") == "....x",
          cfg.safe_name("../../x"))
    check("safe_name 挡纯点号", cfg.safe_name("..") == "")
    check("宏目录与方案目录分开（宏可能上百 KB）",
          "macros" in cfg.macros_dir() and "profiles" in cfg.profiles_dir())

    # ---- dashboard 接线 ----
    check("_MACRO 状态齐全",
          all(k in py for k in ('"rec"', '"events"', '"engine"',
                                '"playing"', '"name"')))
    for m in ("macro_status", "macro_start_record", "macro_stop_record",
              "macro_play", "macro_stop_play", "macro_save", "macro_load",
              "macro_delete", "macro_list", "macro_discard"):
        check("Api 有 %s" % m, "def %s(self" % m in py)
    check("两处启动入口都拦录制中", py.count("if _macro_recording():") >= 2,
          py.count("if _macro_recording():"))
    check("shutdown 取消录制并停回放",
          "_macro_recorder().cancel()" in py
          and "e.ctx._user_stopped = True" in py)

    # ---- 前端：独立整宽分区 ----
    check("宏区是独立 panel（不在三栏 .grid 里）",
          'class="panel hud macro"' in html
          and html.index('class="panel hud macro"')
          > html.index('<div class="grid">'))
    # 文档流顺序：hero → 功能说明 → grid(鼠标/键盘) → 触发方式 → 宏录制。
    # 说明段原本就在 grid 前面（靠宽屏 CSS 的 grid-row 重排到第 3/4 排），
    # 所以这里只验「宏区在触发方式之后」，不要去和说明段比位置。
    check("宏区放在触发方式之后（文档流里最后一个分区）",
          html.index('id="cardMacro"') > html.index('id="cardTrig"'))
    # 【v31 改写】原断言盯的是「宏区第 3 排、说明段第 4 排」。
    # v31 把整块「功能说明」删了（.desc 已不存在），流程条 .flow 成为
    # 栅格的第 2 排，三张主卡顺延第 3 排、宏区第 4 排；≥2200 时宏区
    # 搬进第 4 栏（原来是空的说明段位置）。按新规格改写，不回滚旧断言。
    check("宽屏行序：流程条第 2 排、三卡第 3 排、宏区第 4 排",
          ".wrap>.flow{grid-column:1/-1;grid-row:2" in html
          and ".wrap>.grid>#cardMouse{grid-column:1;grid-row:3" in html
          and ".wrap>.macro{grid-column:1/-1;grid-row:4" in html)
    check("说明段彻底下线（.desc 元素与规则都已删）",
          'class="desc"' not in html and ".wrap>.desc" not in html)
    check("宏区四格",
          html.count('class="tc-title"') >= 4)
    check("前端轮询宏状态", "setInterval(macPoll" in js)
    check("前端不空吞异常（写进状态行）", "调用失败：" in js)

    # ---- 纯逻辑：插件倍速兜底 ----
    p = MacroReplayPlugin()
    check("倍速 0 被夹住（否则时间轴为负）", p._speed(0) >= 0.1)
    check("倍速写坏值退回 1.0", p._speed("abc") == 1.0)


def test_v26_multipos():
    section("v26 · 多点轮询 / 框内随机（解析 · 选点 · 引擎接线 · 前端）")
    import dashboard as D

    py = code_only_dashboard(os.path.join(HERE, "dashboard.py"))
    html = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    js = re.sub(r"(?m)//.*$", "", re.sub(r"/\*[\s\S]*?\*/", "",
                re.sub(r"<!--[\s\S]*?-->", "", html)))

    # ---- 解析层：四种写法 + 坏行整条报错 ----
    check("分号/换行/中文标点都能解析成同一组点",
          D._parse_points("100,200;300,400")
          == D._parse_points("100,200\n300,400")
          == D._parse_points("100，200；300，400")
          == [[100, 200], [300, 400]])
    try:
        D._parse_points("100,200;abc")
        check("坏行必须整条报错（不静默丢点）", False, "没抛异常")
    except Exception:
        check("坏行必须整条报错（不静默丢点）", True)
    try:
        D._parse_points("1,2", True)
        check("框内随机只给 1 个点必须报错", False, "没抛异常")
    except Exception:
        check("框内随机只给 1 个点必须报错", True)

    # ---- 参数层：四模式白名单 + 点数校验 + 不回归 ----
    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "multi",
                                    "points": "100,200\n300,400"})
    check("multi 进参数层：两个点、pos 为空",
          err is None and p["points"] == [[100, 200], [300, 400]]
          and p["pos"] is None, (err, p))
    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "box",
                                    "points": "300,400;100,200"})
    check("box 两点倒序自动排成左上/右下",
          err is None and p["points"] == [[100, 200], [300, 400]],
          (err, (p or {}).get("points")))
    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "multi", "points": ""})
    check("multi 空列表必须报错（不静默退化成跟随光标）", err is not None)
    p, err = D._parse_mouse_params({"button": "left", "action": "click",
                                    "mode": "fixed", "pos": "10,20"})
    check("fixed 不回归：pos 仍是单点、points 为空",
          err is None and p["pos"] == [10, 20] and p["points"] == [], (err, p))
    p, err = D._parse_mouse_params({"button": "left", "action": "hold",
                                    "mode": "multi",
                                    "points": "100,200;300,400"})
    check("长按不支持坐标：mode/points 一律归零",
          err is None and p["mode"] == "cursor" and p["points"] == [], (err, p))

    # ---- 选点层：轮询序列与框内落点 ----
    pts = [(100, 200), (300, 400), (500, 600)]

    class _C(object):
        def __init__(self, n):
            self.tick_count = n

    pl = D._make_mouse_plugin("left", 0, "single", None, 0, pts, "multi")
    seq = [pl._next_target(_C(i)) for i in range(7)]
    check("multi 按轮次依次取点（A,B,C,A,B,C,A）",
          seq == [pts[0], pts[1], pts[2]] * 2 + [pts[0]], seq)
    plb = D._make_mouse_plugin("left", 0, "single", None, 0,
                               [(100, 100), (140, 140)], "box")
    hits = [plb._next_target(_C(i)) for i in range(30)]
    check("box 落点全在框内且不全相同",
          all(100 <= x <= 140 and 100 <= y <= 140 for x, y in hits)
          and len(set(hits)) > 1, hits[:4])
    # 负向对照：打回 v22「只点一个点」，轮询断言必须能区分
    pln = D._make_mouse_plugin("left", 0, "single", pts[0], 0, pts, "fixed")
    check("负向对照：fixed 模式 7 轮全同一点（轮询断言不是恒真）",
          len({pln._next_target(_C(i)) for i in range(7)}) == 1)

    # ---- 静态接线：引擎 options 只喂 cursor/fixed ----
    check("四模式白名单在代码里", 'if mode not in ("cursor", "fixed", '
                               '"multi", "box")' in py)
    check("旧两模式白名单已消失", '("cursor", "fixed")' not in py)
    check("引擎 mode 只可能是 fixed 或 cursor",
          'engine_options = {"mode": "fixed" if p["mode"] == "fixed"' in py
          and 'else "cursor"}' in py)
    check("引擎 fixed_pos 只给 fixed 模式",
          'engine_fixed = p["pos"] if p["mode"] == "fixed" else None' in py)
    check("就绪/启动两处日志共用位置文案函数",
          py.count("def _pos_desc_of(p)") == 1
          and py.count("_pos_desc_of(p)") == 3)

    # ---- 前端接线 ----
    check("界面有坐标列表输入框与容器",
          'id="mPoints"' in js and 'id="mPointsRow"' in js)
    check("位置下拉有 multi/box 两个选项",
          'value="multi"' in js and 'value="box"' in js)
    check("联动函数控制坐标列表显隐", 'mPointsRow").style.display' in js)
    check("collectSettings 收集 points", 'points: $("mPoints").value' in js)
    check("applySettings 恢复 points", "if(m.points != null)" in js)
    check("自动保存 ids 含 mPoints", '"mPoints",' in js)
    check("「取当前」按模式分流（multi/box 追加一行）",
          'mode === "multi" || mode === "box"' in js)
    check("内嵌 HTML == ui/dashboard.html（v26 双源一致）", D.HTML == html,
          (len(D.HTML), len(html)))


def test_hotkey_without_arm_uses_last_params():
    section("鼠标区 · 没点按钮直接按 F5 = 不启动，提示先就绪（两段式）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        # 不调 arm_mouse，直接拨热键。
        # 【2026-10-15 改】旧行为：未就绪也直接启动（纯热键用法）。
        # 主人实测 bug：「直接点中键就开始连点，和鼠标区是否就绪
        # 没有任何关系」——两段式对热键路径等于不存在。现在改为
        # 未就绪一律不启动，与 toggle_keyboard 对称。
        r = api.toggle_mouse()
        check("未就绪按热键不启动", r.get("ok") is False, r.get("msg"))
        check("提示去界面点①就绪", "就绪" in (r.get("msg") or ""),
              r.get("msg"))
        check("没有起过引擎", len(FakeEngine.instances) == 0,
              "未就绪不该碰引擎")
        check("mouse_running = False", D.ST.mouse_running is False)
        check("mouse_armed 仍为 False", D.ST.mouse_armed is False)
        # 就绪后再按热键 = 正常启动（确认没有被永久挡住）
        api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
        r2 = api.toggle_mouse()
        check("就绪后按热键能启动", r2.get("ok") is True, r2.get("msg"))
        check("用的是刚设的参数（left/click）",
              FakeEngine.instances and
              FakeEngine.instances[-1].plugin is not None)
        check("mouse_running = True", D.ST.mouse_running is True)
        api.stop_mouse()                        # 收尾：真正结束


def test_keyboard_hotkey_without_arm():
    section("键盘区 · 没点按钮直接按热键 = 不启动（与鼠标区对称）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        r = api.toggle_keyboard()
        check("未就绪按热键不启动", r.get("ok") is False, r.get("msg"))
        check("提示去界面点①就绪", "就绪" in (r.get("msg") or ""),
              r.get("msg"))
        check("key_running = False", D.ST.key_running is False)
        # 就绪后再按 = 正常启动
        api.arm_keyboard(dict(KEY_PARAMS))
        r2 = api.toggle_keyboard()
        check("就绪后按热键能启动", r2.get("ok") is True, r2.get("msg"))
        check("key_running = True", D.ST.key_running is True)
        api.stop_keyboard()                     # 收尾：真正结束


def test_button_id_normalized_to_token():
    section("鼠标区 · 界面按钮 id 归一化成引擎 token（左键变侧键 bug 回归）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    # 界面下拉的 value 是 BUILTIN_MOUSE_BUTTONS 的 id "0"~"4"，
    # 历史上原样传进引擎，"0" 落进 _xbutton_number 抛
    # 「Windows 只支持两个侧键」——选左键却得侧键注入失败。
    for btn_id, want in (("0", "left"), ("1", "right"), ("2", "middle"),
                         ("3", "xb1"), ("4", "xb2")):
        got = D._normalize_button(btn_id)
        check("id %r -> token %r" % (btn_id, want), got == want, got)
    # token 原样透传
    check("token 'left' 不变", D._normalize_button("left") == "left")
    check("token 'xb2' 不变", D._normalize_button("xb2") == "xb2")
    # 自定义键 xb3+ 原样放行（让 _xbutton_number 抛明确错误）
    check("xb3 原样放行", D._normalize_button("xb3") == "xb3")
    # 空值回落 left
    check("空值回落 left", D._normalize_button("") == "left")
    with PatchGuard():
        api.arm_mouse({"button": "0", "action": "click", "count": "0",
                       "duration": "0", "cps": "10"}, hide=True)
        check("arm_mouse 存的是归一化后的 token",
              api.last_mouse["button"] == "left", api.last_mouse)
        api.toggle_mouse()
        check("id=0 走左键路径启动成功", D.ST.mouse_running is True)
        api.stop_mouse()        # 真正结束（触发键现在只切暂停）


def test_clicked_counter_increments():
    section("鼠标区 · 注入计数必须真实累加（永远显示 0 次的 bug 回归）")
    import dashboard as D
    from core.engine import ClickContext
    # 不碰 Api/引擎线程：直接跑真实插件的 run()，
    # 用 PatchGuard 的 FakeInputs 接住注入调用。
    # 旧代码只加 ctx.tick_count 不加 ctx.clicked，而所有
    # 「注入了 N 次」的显示（结束日志/心跳/停止统计）读的都是
    # clicked——注入实际发生了却永远显示 0 次。
    with PatchGuard():
        # 上一个用例若把鼠标区留在暂停态，这里的 run() 会一直在门闩上打转，
        # clicked 永远是 0 —— 表现为「计数 bug 又复发了」的假信号。
        D.pausectl.set_paused(False)
        D.pausectl.set_paused(False, "mouse")
        plugin = D._make_mouse_plugin("left", 0)
        ctx = ClickContext(options={}, base_cps=10)
        t = threading.Thread(target=plugin.run, args=(ctx,), daemon=True)
        t.start()
        time.sleep(0.6)
        check("运行 0.6 秒后 clicked 有累加（>0）", ctx.clicked > 0,
              "clicked=%r" % (ctx.clicked,))
        check("tick_count 同步累加", ctx.tick_count > 0,
              "tick=%r" % (ctx.tick_count,))
        ctx._stop_event.set()
        t.join(timeout=1.0)


def test_hotkey_start_minimizes_window():
    section("鼠标区 · 热键启动必须让位（hide bug 回归）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    api._window = FakeWindow()
    try:
        with PatchGuard():
            # hide=True（勾选「启动后最小化」）：启动后仪表盘必须最小化，
            # 否则仪表盘还在前台，连点全点在自己窗口上——
            # 这就是主人说的「全局似乎也没起效」。
            api.arm_mouse(dict(MOUSE_PARAMS), hide=True)
            api.toggle_mouse()
            deadline = time.time() + 1.5
            while time.time() < deadline and api._window.minimized == 0:
                time.sleep(0.05)
            check("hide=True：启动后仪表盘已最小化",
                  api._window.minimized >= 1,
                  "minimize 调用 %d 次" % api._window.minimized)
            api.stop_mouse()                    # 收尾：真正结束
    finally:
        api._window = None

    # hide=False（用户取消勾选）：不该最小化
    api2 = D.Api()
    reset_state(api2)
    api2._window = FakeWindow()
    try:
        with PatchGuard():
            api2.arm_mouse(dict(MOUSE_PARAMS), hide=False)
            check("hide=False 被记住", api2._mouse_hide_on_start is False)
            api2.toggle_mouse()
            time.sleep(0.6)     # 超过 _hide_if_needed 的 0.35s 延迟
            check("hide=False：不最小化（尊重用户勾选）",
                  api2._window.minimized == 0,
                  "minimize 调用 %d 次" % api2._window.minimized)
            api2.stop_mouse()                   # 收尾：真正结束
    finally:
        api2._window = None


def test_arm_mouse_hold_mode():
    section("鼠标区 · 长按模式只就绪不按住")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard() as pg:
        r = api.arm_mouse({"button": "right", "action": "hold",
                           "count": 0, "duration": 0, "cps": 10})
        check("长按参数可装填", r.get("ok") is True, r.get("msg"))
        check("mouse_armed = True", D.ST.mouse_armed is True)
        check("没有发过任何 press_down（就绪态不动作）",
              not any(c[0] == "press_down" for c in pg.fake_inputs.calls),
              "实际调用：%r" % (pg.fake_inputs.calls,))
        check("参数记住了长按动作",
              api.last_mouse.get("action") == "hold")
        api.stop_mouse()                        # 收尾


# ----------------------------------------------------------------------
# 键盘区（同模式）
# ----------------------------------------------------------------------
def test_arm_keyboard_only_ready():
    section("键盘区 · 点「① 就绪」只装填参数，不起监听")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        r = api.arm_keyboard(dict(KEY_PARAMS))
        check("arm_keyboard 返回 ok", r.get("ok") is True, r.get("msg"))
        check("提示语说「已就绪」并指向 F6",
              "就绪" in r.get("msg", "") and "F6" in r.get("msg", ""),
              r.get("msg"))
        check("key_armed = True", D.ST.key_armed is True)
        check("key_running 仍为 False", D.ST.key_running is False)
        check("没有创建 KeyHoldLock", len(FakeKeyHoldLock.instances) == 0,
              "实际创建了 %d 个" % len(FakeKeyHoldLock.instances))
        check("参数已记住", api.last_key.get("keys") == "w")
        check("就绪消息写入键盘分区",
              "F6" in (D.ST.key_msg or ""), D.ST.key_msg)


def test_arm_keyboard_rejects_when_running():
    section("键盘区 · 运行中再点「① 就绪」= 明确报错")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_keyboard(dict(KEY_PARAMS))
        api.toggle_keyboard()
        check("前置条件：已在运行", D.ST.key_running is True)
        r = api.arm_keyboard({"keys": "d", "threshold": 1, "hz": 20,
                              "hold": "false", "gap": 15})
        check("运行中装填被拒绝", r.get("ok") is False, r.get("msg"))
        check("运行状态不被破坏", D.ST.key_running is True)
        api.stop_keyboard()                     # 收尾：真正结束


def test_toggle_keyboard_from_armed_starts():
    section("键盘区 · 就绪态按 F6 = 真启动（钩子起来 + 就绪态清除）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_keyboard(dict(KEY_PARAMS))
        r = api.toggle_keyboard()
        check("toggle 返回 ok", r.get("ok") is True, r.get("msg"))
        check("创建了 1 个 KeyHoldLock",
              len(FakeKeyHoldLock.instances) == 1,
              "实际 %d 个" % len(FakeKeyHoldLock.instances))
        lock = FakeKeyHoldLock.instances[-1]
        check("lock 已 start", lock.started is True)
        check("key_running = True", D.ST.key_running is True)
        check("key_armed 已清除", D.ST.key_armed is False)
        check("panic_key 仍是 F12", lock.panic_key == "f12")
        check("hold_down 正确解析为 True（一直按着）",
              lock.hold_down is True)
        api.stop_keyboard()                     # 收尾：真正结束


def test_toggle_keyboard_running_pauses():
    """
    【2026-10-16 改规格】与鼠标区对称：触发键 = 暂停 / 继续。
    主人拍板两区一起改，避免「键盘区按一下就结束、再按没反应」
    的同一个死循环留在键盘区。
    """
    section("键盘区 · 运行中按触发键 = 暂停（不是停止）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_keyboard(dict(KEY_PARAMS))
        api.toggle_keyboard()
        lock = FakeKeyHoldLock.instances[-1]
        r = api.toggle_keyboard()
        check("暂停返回 ok", r.get("ok") is True, r.get("msg"))
        check("lock **没有**被 stop（暂停≠停止）", lock.stopped is not True)
        check("key_running 仍是 True", D.ST.key_running is True)
        check("key_lock 仍在", D.ST.key_lock is not None)
        check("键盘区处于暂停", D.pausectl.is_paused("key") is True)
        check("鼠标区不受影响", D.pausectl.is_paused("mouse") is False)
        api.toggle_keyboard()
        check("再按一次 = 继续", D.pausectl.is_paused("key") is False)
        check("继续后 key_running 仍为 True", D.ST.key_running is True)
        api.stop_keyboard()
        check("停止后 lock 已 stop", lock.stopped is True)
        check("停止后 key_running = False", D.ST.key_running is False)
        check("停止后 key_lock 已清空", D.ST.key_lock is None)
        check("停止后 key_armed = False（回到未就绪）",
              D.ST.key_armed is False)


def test_stop_keyboard_clears_armed():
    section("键盘区 · 就绪态直接点「停止」= 连就绪态一起清掉")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        api.arm_keyboard(dict(KEY_PARAMS))
        r = api.stop_keyboard()
        check("stop_keyboard 返回 ok", r.get("ok") is True, r.get("msg"))
        check("key_armed 已清除", D.ST.key_armed is False)
        check("没有起过监听", len(FakeKeyHoldLock.instances) == 0)


def test_hold_param_three_step_bool():
    section("键盘区 · hold 字符串三步转 bool（含空串当缺省）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        # "false" 必须解析成 False，不能被非空字符串恒真坑掉
        api.arm_keyboard({"keys": "w", "threshold": 2, "hz": 30,
                          "hold": "false", "gap": 20})
        api.toggle_keyboard()
        lock = FakeKeyHoldLock.instances[-1]
        check('"false" -> hold_down=False（反复点击）',
              lock.hold_down is False, "实际 %r" % lock.hold_down)
        # 触发键只切暂停，结束必须走 stop_keyboard（否则下一个
        # arm_keyboard 会被「正在运行」挡掉，测的还是上一轮的参数）
        api.stop_keyboard()

        # 空串按缺省处理 = "true"（一直按着），不能解析成 False
        r = api.arm_keyboard({"keys": "w", "threshold": 2, "hz": 30,
                              "hold": "", "gap": 20})
        check("停止后可以重新装填", r.get("ok") is True, r.get("msg"))
        api.toggle_keyboard()
        lock = FakeKeyHoldLock.instances[-1]
        check('空串 -> 按缺省 "true" 解析', lock.hold_down is True,
              "实际 %r" % lock.hold_down)
        api.stop_keyboard()

        # 真 bool 也要认（前端万一传 bool 而不是字符串）
        api.arm_keyboard({"keys": "w", "threshold": 2, "hz": 30,
                          "hold": False, "gap": 20})
        api.toggle_keyboard()
        lock = FakeKeyHoldLock.instances[-1]
        check("真 bool False 也认", lock.hold_down is False,
              "实际 %r" % lock.hold_down)
        api.stop_keyboard()


# ----------------------------------------------------------------------
# 快照 / 前端接线
# ----------------------------------------------------------------------
def test_snapshot_fields():
    section("状态快照 · 就绪态字段必须进 snapshot（前端轮询靠它）")
    import dashboard as D
    api = D.Api()
    reset_state(api)
    with PatchGuard():
        snap = api.status()
        for f in ("mouse_armed", "key_armed", "elev_warn"):
            check("snapshot 含 %s" % f, f in snap, "缺失字段 %s" % f)


def test_html_wiring():
    section("前端接线 · 按钮调 arm_*、轮询渲染就绪态、提权横幅在位")
    import dashboard as D
    html = D.HTML
    check("按钮调 api.arm_mouse", "api.arm_mouse" in html)
    check("按钮调 api.arm_keyboard", "api.arm_keyboard" in html)
    check("前端不再直接调 api.start_mouse",
          "api.start_mouse" not in html)
    check("前端不再直接调 api.start_keyboard",
          "api.start_keyboard" not in html)
    check("轮询渲染 mouse_armed", "mouse_armed" in html)
    check("轮询渲染 key_armed", "key_armed" in html)
    check("提权横幅元素存在", 'id="elevBan"' in html)
    check("一键提权重启函数存在", "async function relaunchAdmin" in html)
    check("调了 relaunch_as_admin", "api.relaunch_as_admin" in html)
    check("按钮文案是两段式（① 就绪）", "① 就绪" in html)
    check("就绪态样式类已定义", ".status.armed" in html)
    check("提示语说明「点①就绪不会启动」",
          "不会启动" in html)
    # ---- v12 热键降级 UI（2026-10-15 事故的固化断言）----
    # 事故：v12 把 UI 改动只写进了 ui/dashboard.html（运行时产物），
    # 忘了回嵌 dashboard.py 的 HTML 字符串（源头）——exe 里根本没有
    # 降级提示，用户按提示的键没反应还是找不到原因。
    # 从此内嵌 HTML 必须带齐这些标记，缺一条就说明回嵌漏了。
    check("鼠标卡片提示有动态键名元素", 'id="hintMouseKey"' in html,
          "缺 id=hintMouseKey——卡片提示只能写死 F5")
    check("键盘卡片提示有动态键名元素", 'id="hintKeyKey"' in html,
          "缺 id=hintKeyKey——卡片提示只能写死 F6")
    check("refreshHotkeys 用真键更新卡片提示",
          "mouse_pretty" in html and "hintMouseKey" in html
          and re.search(r"hintMouseKey[\s\S]{0,80}mouse_pretty|"
                        r"mouse_pretty[\s\S]{0,80}hintMouseKey", html)
          is not None,
          "没找到「mouse_pretty -> hintMouseKey」的赋值路径")
    check("降级说明读取 note_mouse/note_key",
          "note_mouse" in html and "note_key" in html,
          "缺 note_mouse/note_key——hkNote 永远空")
    check("降级提示样式类已定义", "hint tiny warn" in html,
          "缺 .hint.tiny.warn——⚠ 提示没有颜色区分")
    check("降级说明拼上 ⚠ 前缀", "⚠ " in html,
          "缺 ⚠ 前缀——用户注意不到这条提示")

    # ---- v19 配置导入/导出/多方案（2026-10-16 新增）----
    # 防 v12 双源断裂重演：配置管理面板的所有关键标记必须进内嵌 HTML。
    # 齿轮入口 / 面板 / 三分区 / 各动作按钮 / 回填逻辑缺一不可。
    check("配置齿轮入口存在", 'id="cfgBtn"' in html,
          "缺 id=cfgBtn——标题栏没有配置入口")
    check("配置面板容器存在", 'id="cfgPanel"' in html,
          "缺 id=cfgPanel——面板没渲染")
    check("面板含「配置方案」分区", "配置方案" in html,
          "缺 配置方案 分区——多 profile 入口丢了")
    check("导出到文件按钮接线 exportFile", "exportFile()" in html,
          "缺 exportFile()——点了没反应")
    check("复制到剪贴板按钮接线 copyClip", "copyClip()" in html,
          "缺 copyClip()——剪贴板分享通道没入口")
    check("从文件导入按钮接线 importFile", "importFile(" in html,
          "缺 importFile()——文件导入没入口")
    check("从剪贴板粘贴按钮接线 pasteClip", "pasteClip()" in html,
          "缺 pasteClip()——剪贴板导入没入口")
    check("存为方案按钮接线 saveProfile", "saveProfile()" in html,
          "缺 saveProfile()——多 profile 存不了")
    check("加载方案按钮接线 loadProfile", "loadProfile()" in html,
          "缺 loadProfile()——方案切不回来")
    check("删除方案按钮接线 delProfile", "delProfile()" in html,
          "缺 delProfile()——方案删不掉")
    check("方案下拉框存在", 'id="cfgProfileSel"' in html,
          "缺 id=cfgProfileSel——方案列表渲染不了")
    check("方案名输入框存在", 'id="cfgProfileName"' in html,
          "缺 id=cfgProfileName——没法命名方案")
    check("导入/加载后回填表单(applySettingsFromImport)",
          "applySettingsFromImport" in html,
          "缺 applySettingsFromImport——导入后控件不更新")
    check("回填时重新拉全量 settings(load_settings)",
          "load_settings" in html,
          "缺 load_settings 调用——trigger 等本机绑定的回填不全")
    # 后端 API 接线（pywebview 把 snake_case 直接暴露给 JS）
    check("JS 调后端 export_config_file", "api.export_config_file" in html)
    check("JS 调后端 copy_config_clipboard",
          "api.copy_config_clipboard" in html)
    check("JS 调后端 paste_config_clipboard",
          "api.paste_config_clipboard" in html)
    check("JS 调后端 import_config_text", "api.import_config_text" in html)
    check("JS 调后端 list_profiles", "api.list_profiles" in html)

    # ---- v20 面板显隐（2026-10-17 主人实测报回）----
    # 两个真因叠在一起才出现「一启动就展示 + 点两次才隐藏」：
    #   ① CSS 漏写 display:none → div 按默认 block 渲染，启动即挂右上角；
    #   ② toggleCfg 读的是**内联** style.display（初始是空串而非 "block"）
    #      → 第一次点被判成「当前没开」→ 设成 block（毫无变化）→ 第二次才隐藏。
    # 防线：CSS 默认隐藏 + .open 显式打开 + JS 用 classList 单一来源。
    check("配置面板 CSS 默认隐藏(display:none)",
          re.search(r"\.cfg-panel\{[^}]*display:none", html) is not None,
          "缺 .cfg-panel 的 display:none —— 程序一启动面板就挂在右上角")
    check("配置面板有 .open 显式打开态",
          re.search(r"\.cfg-panel\.open\{[^}]*display:block", html) is not None,
          "缺 .cfg-panel.open —— 点齿轮打不开面板")
    check("toggleCfg 用 classList 切换（不读内联 style.display）",
          'classList.toggle("open")' in html,
          "toggleCfg 没用 classList —— 「点两次才隐藏」会重演")
    # 剥 JS 行注释再查旧判法：注释里说明历史写法会把断言带红
    # （硬规则 38 已踩三次：Python / CSS / JS 注释各一次）。
    _html_nojs = "\n".join(ln for ln in html.splitlines()
                           if not ln.lstrip().startswith("//"))
    check("已无 style.display===\"block\" 的旧判法",
          'style.display === "block"' not in _html_nojs,
          "残留 style.display===\"block\" —— 内联样式初值为空串时会误判")
    check("JS 调后端 save_profile", "api.save_profile" in html)
    check("JS 调后端 load_profile", "api.load_profile" in html)
    check("JS 调后端 delete_profile", "api.delete_profile" in html)

    # ---- v21 全局暂停 / 方案空壳（2026-10-17 主人实测报回）----
    # bug 1 的两个真因 + bug 2 的真因，静态防线逐条钉住：
    #   ① 前端防抖：暂停/恢复是「切换」语义，连点/双击 = 切两次 = 净效果
    #      为零 → 600ms 内的第二次点击必须被忽略（主人「无论如何都
    #      不会恢复」的直接成因）。功能级行为见 test_v21_* 的场景 A。
    #   ② 分区恢复只清全局位：set_zone_paused 必须调 pausectl.clear_global()
    #      而不是 set_paused(False)（后者会把两个区的标志一起清掉，
    #      按 G1 恢复键盘区会牵连暂停中的鼠标区）。
    #   ③ 全局暂停/恢复路径必须写 launch.log：这条路径（右上角按钮 +
    #      Pause 热键）以前一行日志都不写，主人报「点了没反应」时
    #      launch.log 里什么都看不到，只能靠猜。
    #   ④ save_settings 必须把数据合并进 self._settings：导出/存方案
    #      读的是内存，只写盘不更新内存 → 全新启动时存出来是
    #      {"schema_version": 1} 空壳 → 加载报「没有可识别的参数」。
    _html_nojs2 = "\n".join(ln for ln in html.splitlines()
                            if not ln.lstrip().startswith("//"))
    check("togglePause 有 600ms 防抖守卫",
          "_pauseBtnTs" in _html_nojs2 and "600" in _html_nojs2,
          "缺 _pauseBtnTs/600 —— 双击会切两次回到暂停态（bug 1 重演）")
    check("防抖判据用 Date.now()（不是固定值比较）",
          "Date.now()" in _html_nojs2,
          "防抖时间戳来源不对")
    import dashboard as _D
    _src = code_only_dashboard(_D.__file__)
    check("set_zone_paused 用 clear_global() 只清全局位",
          "pausectl.clear_global()" in _src,
          "分区恢复又走 set_paused(False) 会牵连其他区（bug 1 重演）")
    check("全局暂停/恢复写 launch.log",
          '全局暂停（右上角按钮或暂停热键）' in _src
          and '全局恢复运行（右上角按钮或暂停热键）' in _src,
          "这条路径不留日志，排障时只能靠猜")
    check("save_settings 合并进内存 self._settings",
          re.search(r"def save_settings.*?self\._settings\[k\]",
                    _src, re.S) is not None,
          "只写盘不更新内存 → 存方案是空壳（bug 2 重演）")


def test_embedded_html_in_sync():
    section("双源一致 · 内嵌 HTML 与 ui/dashboard.html 不允许再分叉")
    # dashboard.py 里的 HTML 字符串是源头，启动时写出到 ui/dashboard.html
    # 再加载（见 main() 里 html_path 那段）。改 UI 的正确顺序是：
    #   改 ui/dashboard.html -> 回嵌 dashboard.py ->（跑本测试）
    # 只改一边，这个测试就红。2026-10-15 v12 打包后才发现 exe 里
    # 没有降级提示 UI，就是缺这道闸门。
    import dashboard as D
    ui_path = os.path.join(HERE, "ui", "dashboard.html")
    if not os.path.isfile(ui_path):
        check("ui/dashboard.html 存在", False, ui_path)
        return
    disk = open(ui_path, encoding="utf-8").read()
    same = disk.strip() == D.HTML.strip()
    check("内嵌 HTML == ui/dashboard.html", same,
          "两边不一致：改了一边忘了同步另一边。"
          "以 ui/dashboard.html 为准重新回嵌 dashboard.py"
          "（或把内嵌版写出覆盖 ui/），使二者逐字节相同")


def test_log_environment_survives():
    section("启动日志 · _log_environment 不抛异常且记下 DPI 状态")
    # 【为什么有这条】v14 把 DPI 状态写进 _log_environment，但
    # _dpi_state 是 main() 的局部变量 → NameError，exe 启动即崩，
    # 日志停在「启动一次新的实例」后面什么都没有。
    # 打桩测试全是 API 级，覆盖不到 main() 的启动路径——
    # 这个坑是真机点 exe 才发现的（见 2026-10-16 v14 复盘）。
    # 本测试守住「_log_environment 随时可调、不缺变量」。
    import dashboard as D

    # 模拟 main() 已经设过全局（独立跑测试时 main 不会被执行）
    saved_state = D._dpi_state
    D._dpi_state = "PER_MONITOR_AWARE_V2（测试模拟值）"
    captured = []
    saved_log = D._log
    D._log = lambda msg, *a, **k: captured.append(msg % a if a else msg)
    try:
        D._log_environment()          # 崩了这里直接抛，测试红
        ok = True
        err = ""
    except Exception as exc:
        ok = False
        err = repr(exc)
    finally:
        D._log = saved_log
        D._dpi_state = saved_state
    check("_log_environment 不抛异常", ok, err)
    joined = "\n".join(captured)
    check("日志含 DPI 感知状态行", "DPI 感知" in joined,
          "缺 DPI 行：_dpi_state 又变回局部变量了？")
    check("DPI 行反映 main 设的值", "测试模拟值" in joined,
          "读了占位值而不是 main 设的全局：global 声明又丢了？")


def test_relaunch_as_admin_paths():
    section("管理员重启 · UAC 取消/确认两条路都返回结构化结果（不弹真 UAC）")
    import ctypes
    import dashboard as D
    api = D.Api()
    reset_state(api)

    class _FakeShellFn(object):
        """可调用实例（不是裸函数）——relaunch_as_admin 要给它挂
        .argtypes/.restype，裸 method 对象没有 __dict__ 会直接抛。"""

        def __init__(self, ret):
            self.ret = ret

        def __call__(self, *a, **k):
            return self.ret

    class FakeShell(object):
        """替身 shell32：ShellExecuteW 直接返回指定码，不弹 UAC。"""

        def __init__(self, ret):
            self.ShellExecuteW = _FakeShellFn(ret)

    real_win = ctypes.WinDLL
    real_exit = D.Api._exit_later
    try:
        with PatchGuard():
            # 路 1：UAC 被取消（返回 5 = SE_ERR_ACCESSDENIED）。
            # 前端靠 ok 字段把按钮恢复成可点，所以「取消」必须是
            # 结构化失败而不是异常——异常会把按钮永久禁用。
            ctypes.WinDLL = lambda name, *a, **k: FakeShell(5)
            r = api.relaunch_as_admin()
            check("UAC 取消：返回 dict", isinstance(r, dict), repr(r))
            check("UAC 取消：ok=False", r.get("ok") is False, r.get("msg"))
            check("UAC 取消：提示手动方式",
                  "管理员" in r.get("msg", ""), r.get("msg"))

            # 路 2：UAC 确认（返回 33 > 32 即成功）。
            # _exit_later 必须打桩——它是 time.sleep + os._exit(0)，
            # 不打桩的话测试跑完前半段就把自己杀了。
            D.Api._exit_later = staticmethod(lambda delay=1.5: None)
            ctypes.WinDLL = lambda name, *a, **k: FakeShell(33)
            r = api.relaunch_as_admin()
            check("UAC 确认：返回 dict", isinstance(r, dict), repr(r))
            check("UAC 确认：ok=True", r.get("ok") is True, r.get("msg"))
            time.sleep(0.15)   # 让桩线程跑完（它啥也不干，只是别留悬空）
    finally:
        ctypes.WinDLL = real_win
        D.Api._exit_later = real_exit


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    print("=" * 64)
    print(" 三段式状态机回归（按钮=就绪，触发键=启动/暂停，停止按钮=结束）")
    print(" 全部打桩，零真实注入")
    print("=" * 64)
    test_arm_mouse_only_ready()
    test_arm_mouse_rejects_when_running()
    test_toggle_from_armed_starts()
    test_toggle_running_pauses()
    test_zone_pause_is_independent()
    test_v21_global_pause_and_profile_memory()
    test_v22_mouse_zone_features()
    test_v23_features()
    test_v24_tray()
    test_v26_multipos()
    test_v27_macro()
    test_stop_mouse_clears_armed()
    test_hotkey_without_arm_uses_last_params()
    test_keyboard_hotkey_without_arm()
    test_button_id_normalized_to_token()
    test_clicked_counter_increments()
    test_hotkey_start_minimizes_window()
    test_arm_mouse_hold_mode()
    test_arm_keyboard_only_ready()
    test_arm_keyboard_rejects_when_running()
    test_toggle_keyboard_from_armed_starts()
    test_toggle_keyboard_running_pauses()
    test_stop_keyboard_clears_armed()
    test_hold_param_three_step_bool()
    test_snapshot_fields()
    test_html_wiring()
    test_embedded_html_in_sync()
    test_log_environment_survives()
    test_relaunch_as_admin_paths()

    print("\n" + "=" * 64)
    print("结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n失败清单：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
