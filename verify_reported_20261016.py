# -*- coding: utf-8 -*-
"""
verify_reported_20261016.py —— 主人 2026-10-16 报的四个问题的回归测试
======================================================================

【问题 1】右上角「暂停 / 恢复运行」按钮点了没反应、也不弹通知
    真因：dashboard.py 的 Api.toggle_pause 里把模块名写成 `pausctl`
    （少了 e）→ 一按就 NameError → 前端 `catch(e){}` 吞掉 → 界面零反应。
    连带：全局暂停热键走的是同一个函数，回调异常从 hotkey 监听线程里
    炸出去（while 里的裸调用）→ 线程结束、热键被注销 →
    **暂停热键从来没能用过，而且按一次就永久失效**。

【问题 2】最大化后两边一大片空黑，拉伸面板又很难看
    真因：.wrap 写死 max-width:1180px。
    解法：宽屏**加列**（1500px 起三栏：鼠标区/键盘区/触发方式，
    说明整行分两栏文字），面板保持自己的舒适宽度，不靠拉宽。

【问题 3】最大化后再点「还原」，窗口没变化，只有图标变成「还原」
    真因：_norm_rect["valid"] 一旦被意外消息打回 False，最大化分支会把
    **当前的全屏矩形**当成「原来的小窗口」存下来 → SetWindowPos 到同样
    尺寸 = 看不出任何变化。
    解法：还原目标绝不取「盖住工作区」的矩形；还原前先 SW_RESTORE 清掉
    系统 zoomed 状态；还原后复核，没生效就用兜底尺寸再来一次。

【问题 4】暂停键盘区后再按目标键，会冒出莫名其妙的「已解除锁定」
    真因：暂停时只补了一个 keyup（松手），**锁定状态原样保留**
    （_state 仍是 locked、_active_key 仍指向该键）→ 用户再按 W 时
    被钩子判成「锁定中再次按下 = 解除指令」。
    解法：暂停 = 松手 + 清空锁定状态，同一轮做完（_clear_lock_state_locked）。

本测试不弹任何窗口，全部是「真调函数 + 静态断言」；
[5] 额外**真驱动键盘状态机**（send_key 换成记录器，不注入真实按键）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def read(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


def code_only(src):
    """
    去掉三引号块和 # 注释行，只留可执行代码。

    【为什么需要】本轮把「踩过的坑」写进了注释（例如
    「原来写的是 pausctl.is_paused()」），静态断言如果连注释一起查，
    就会把解释文字当成残留 bug 报红 —— 断言要查的是**代码形态**。
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


# ----------------------------------------------------------------------
# 键盘状态机的「真驱动」小工具（供 [5] 用）
#
# 【为什么不能只写静态断言】暂停=解除锁定是一条**行为**约束，
# grep 不出来。必须真的构造一个 KeyHoldLock，喂它合成的按键事件，
# 看它在暂停前后到底产生了哪些状态变化事件。
# send_key 全程换成记录器，绝不注入真实按键。
# ----------------------------------------------------------------------
_KEY_SENT = []


def _key_fire(lock, kb, vk, down):
    """构造一次物理按键事件，直接喂给钩子回调。"""
    import ctypes
    info = kb.KBDLLHOOKSTRUCT()
    info.vkCode = vk
    info.scanCode = 0
    info.flags = 0
    info.time = 0
    info.dwExtraInfo = 0
    return lock._hook_callback(
        0, kb.WM_KEYDOWN if down else kb.WM_KEYUP, ctypes.addressof(info))


def _key_hold(lock, kb, vk, seconds, interval=0.05):
    """模拟长按：按下 + 按 Windows 节奏补自动重复 keydown。"""
    import time
    _key_fire(lock, kb, vk, True)
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(interval)
        _key_fire(lock, kb, vk, True)


def _key_make_lock(kb, threshold=0.4):
    """起一个只跑 _inject_loop 的锁（不装全局钩子），返回 (lock, events)。"""
    import threading
    events = []
    lock = kb.KeyHoldLock(
        keys=["w"], threshold_sec=threshold, repeat_hz=30, hold_down=True,
        on_state_change=lambda active, vk: events.append(
            ("LOCK", vk) if active else ("UNLOCK", vk)),
    )
    t = threading.Thread(target=lock._inject_loop, daemon=True)
    t.start()
    lock._test_thread = t
    return lock, events


def _key_stop(lock):
    lock._stop_inject = True
    lock._test_thread.join(timeout=2.0)


def main():
    print("=" * 62)
    print("verify_reported_20261016：四个已报问题的回归")
    print("=" * 62)

    import dashboard as D
    from core import pausectl
    from core import hotkey as hk

    dash_src = read("dashboard.py")
    hot_src = read("core/hotkey.py")
    ui_src = read("ui/dashboard.html")
    dash_code = code_only(dash_src)
    hot_code = code_only(hot_src)

    # ================= ① 暂停按钮 =================
    print("\n[1] 暂停/恢复按钮：后端链路")
    # 拼错模块名的写法必须彻底消失（pausctl 后面不是 e 的写法）
    typo = re.findall(r"\bpausctl(?!e)", dash_code)
    check("dashboard.py 的代码里没有 pausctl 拼错", not typo,
          "还发现 %d 处：%s" % (len(typo), typo))

    check("toggle_pause 用 any_paused() 判翻转（和按钮文案对称）",
          "not pausectl.any_paused()" in dash_src,
          "分区暂停时用 is_paused() 会算出 not False = True，"
          "点「恢复运行」反而把两个区全冻住")

    # 真调：不能抛、要返回 dict
    pausectl.set_paused(False)
    api = D.Api()
    threw = None
    r1 = None
    try:
        r1 = api.toggle_pause()
    except Exception as e:                                   # noqa: BLE001
        threw = e
    check("Api.toggle_pause() 不抛异常（NameError 那个坑）",
          threw is None, threw)
    check("第 1 次点：返回结构化结果且已暂停",
          isinstance(r1, dict) and r1.get("paused") is True, r1)
    check("第 1 次点：pausectl 全局确已置位", pausectl.is_paused() is True)

    r2 = api.toggle_pause()
    check("第 2 次点（恢复）：返回 paused=False",
          isinstance(r2, dict) and r2.get("paused") is False, r2)
    check("第 2 次点：两个区都恢复", not pausectl.any_paused())

    # 分区暂停（中键）后点按钮 -> 必须真的全部恢复，而不是反手全冻住
    pausectl.set_paused(False)
    pausectl.set_paused(True, "mouse")
    check("前置：鼠标区单独暂停时，前端按钮会显示「恢复运行」",
          pausectl.any_paused() is True)
    r3 = api.toggle_pause()
    check("分区暂停时点「恢复运行」-> 真的恢复（不是反手全暂停）",
          r3.get("paused") is False and not pausectl.any_paused(),
          "pausectl 状态=%s" % (pausectl._zones,))
    pausectl.set_paused(False)

    # 前端不能再无声吞异常
    check("前端 togglePause 失败时会显示错误（不再 catch(e){} 吞掉）",
          "apiFail(e, \"暂停/恢复\")" in ui_src)

    # ================= ② 热键回调异常不许带走监听线程 =================
    print("\n[2] 全局暂停热键：回调异常不能弄死监听线程")
    check("hotkey.py 不再裸调 self.callback()",
          "self.callback()" not in hot_code,
          "裸调用时回调异常会从 while 里炸出去 -> finally 注销热键 -> 线程死亡")
    check("hotkey.py 通过 _invoke 统一包住回调", "_invoke(self.callback)" in hot_code)
    check("hotkey.py 提供 set_error_logger 上报钩子",
          "def set_error_logger" in hot_src)

    hit = []
    old_hook = hk._ERROR_HOOK
    hk.set_error_logger(lambda where: hit.append(where))
    threw2 = None
    try:
        hk._invoke(lambda: 1 / 0)          # 故意炸
    except Exception as e:                                   # noqa: BLE001
        threw2 = e
    check("_invoke 吞掉回调异常（不抛给调用方）", threw2 is None, threw2)
    check("_invoke 把异常上报给了 error hook", hit == ["热键回调"], hit)
    hk.set_error_logger(old_hook)
    check("dashboard.main 里挂上了这个钩子",
          "hk.set_error_logger(_log_exc)" in dash_src)

    # ================= ③ 最大化 / 还原 =================
    print("\n[3] 最大化 / 还原：还原目标绝不允许是全屏矩形")
    real_work = D._work_area
    D._work_area = lambda h=None: (0, 0, 1920, 1032)
    try:
        check("_covers：全屏矩形判定为盖住工作区",
              D._covers(0, 0, 1920, 1032) is True)
        check("_covers：小窗口判定为没盖住",
              D._covers(100, 100, 1164, 781) is False)

        # 造污染：_norm_rect 里存的是全屏矩形（就是那个 bug 的现场）
        saved_norm = dict(D._norm_rect)
        saved_free = dict(D._last_free_rect)
        D._norm_rect.update(x=0, y=0, w=1920, h=1032, valid=True)
        D._last_free_rect.update(x=120, y=90, w=1164, h=781, ok=True)
        t = D._free_target(0)
        check("_free_target 跳过被污染的全屏矩形，退回真正的小矩形",
              t == (120, 90, 1164, 781), t)

        D._last_free_rect.update(x=0, y=0, w=1920, h=1032, ok=True)
        t2 = D._free_target(0)
        check("两个候选都被污染时退回居中默认尺寸（仍不盖工作区）",
              not D._covers(*t2), t2)

        D._norm_rect.clear()
        D._norm_rect.update(saved_norm)
        D._last_free_rect.clear()
        D._last_free_rect.update(saved_free)
    finally:
        D._work_area = real_work

    check("IsZoomed 声明了 argtypes（防 64 位句柄截断）",
          getattr(D.user32.IsZoomed, "argtypes", None) is not None)
    check("SW_RESTORE = 9", D.SW_RESTORE == 9)
    check("还原分支先清系统 zoomed 状态（SW_RESTORE）",
          "user32.IsZoomed(h)" in dash_src and "user32.ShowWindow(h, SW_RESTORE)" in dash_src)
    check("还原后有复核兜底（SetWindowPos 没生效就再来一次）",
          "_rect_covers_work_area(h)" in dash_src
          and "还原未生效" in dash_src)
    check("无效句柄仍返回 ok=False（老行为不变）",
          D.win_toggle_maximize(0).get("ok") is False)
    check("最大化/还原会写日志（下次真出问题有现场可查）",
          "窗口最大化：%s -> %s" in dash_src and "窗口还原：%s -> %s" in dash_src)

    # ================= ④ 宽屏布局：加列 + 三栏必须对齐 =================
    # 第一轮（主人：「最大化后两边全黑」）→ 只做到「加列」
    # 第二轮（主人：「最大化后三个区看着杂乱无章，一点也不整齐」）→ 补对齐：
    #   ① 三栏等高（原来 align-items:start，各高各的）
    #   ② 三张卡说明段等高 → 下面第一行控件才会落在同一水平线
    #   ③ 触发方式卡清掉窄屏的 margin-top（否则整卡低 20px）
    #   ④ 阈值 1500 → 1440：窗口宽 ≠ 视口宽，滚动条占 17~24px，
    #      写 1500 的话用户把窗口拖到 1500 反而进不了三栏
    print("\n[4] 宽屏布局：靠加列吃宽度，且三栏必须对齐")
    # ⚠ 先把 /* CSS 注释 */ 剥掉再拼平：注释里为了说清「原来错在哪」
    # 会写出旧写法（例如 align-items:start），不剥就会把解释文字
    # 当成残留 bug —— 和硬规则 38 是同一个坑，只是注释语法换成 CSS 的。
    flat = re.sub(r"/\*.*?\*/", "", ui_src, flags=re.S)
    flat = flat.replace(" ", "").replace("\n", "")
    check("默认容器仍是 1240 上限（窄窗口外观不变）",
          ".wrap{max-width:1240px" in ui_src)
    check("存在 1440px 起的宽屏媒体查询（不是 1500：视口比窗口窄）",
          "@media(min-width:1440px)" in flat)
    check("宽屏用三列网格",
          "grid-template-columns:minmax(0,1fr)minmax(0,1fr)minmax(0,1.08fr)"
          in flat)
    check(".grid 退化成透明壳（卡片才能和触发方式同排）",
          ".wrap>.grid{display:contents}" in flat)
    # 【v31 改写】流程条插进第 2 排，三张主卡顺延到第 3 排（旧断言写死 grid-row:2）。
    check("三块功能区各有明确栏位（不会出现空洞）",
          all(k in flat for k in (
              ".wrap>.grid>#cardMouse{grid-column:1;grid-row:3}",
              ".wrap>.grid>#cardKey{grid-column:2;grid-row:3}",
              ".wrap>.trig{grid-column:3;grid-row:3")))
    check("流程条占第 2 排整行（否则被自动放置掉到卡片下方）",
          ".wrap>.flow{grid-column:1/-1;grid-row:2" in flat)
    check("不再写 align-items:start（那会让三栏各高各的、底部参差）",
          "align-items:start" not in flat)
    check("触发方式卡清掉窄屏的 margin-top（否则整卡低 20px）",
          ".wrap>.trig{grid-column:3;grid-row:3;margin-top:0}" in flat)
    check("三张卡的说明段等高（下面第一行控件才会对齐）",
          "min-height:58px" in flat)
    # 【v31 改写】整块「功能说明」已删（.desc 连规则一起清掉），
    # 第 4 栏改成放宏录制区 —— 旧断言「columns:2」盯的是已不存在的元素。
    check("说明段已删干净，第 4 栏改放宏录制区",
          ".wrap>.desc" not in flat
          and ".wrap>.macro{grid-column:4;grid-row:3/5" in flat)
    check("没有再出现「单纯放开 max-width」的写法",
          "max-width:1740px" not in flat and "max-width:1840px;margin" not in flat)
    check("带鱼屏有第 4 栏", "@media(min-width:2200px)" in flat
          and "repeat(4,minmax(0,1fr))" in flat)
    check("触发方式格子固定两列（auto-fit 会在临界宽度抖成 3 列）",
          "repeat(2,minmax(0,1fr))" in flat
          and "auto-fit,minmax(200px,1fr)" not in flat)

    # ================= ④b 设置持久化：路径不能用 __file__ =================
    # 主人报：「绑了 G1/G2 以后关掉程序再重启，又变成默认的中键和 F6」
    # 真因是路径 —— PyInstaller 单文件模式下 __file__ 指向 %TEMP%\_MEIxxxxx\，
    # 该目录名每次启动都变，上次写的设置下次启动不在那儿 → 等于没存。
    # 探针 diag_settings_probe.py 有 23 项行为级验证，这里只钉静态形态。
    print("\n[4b] 设置持久化：存放路径必须由 exe 位置决定")
    dash_code = code_only(dash_src)
    check("新增 _app_dir() 按 frozen 分支取目录",
          "def _app_dir()" in dash_code
          and 'getattr(sys, "frozen", False)' in dash_code)
    check("_app_dir() 打包态用 sys.executable",
          "os.path.dirname(os.path.abspath(sys.executable))" in dash_code)
    check("有用户目录兜底 _user_data_dir()",
          "def _user_data_dir()" in dash_code)
    check("设置路径不再拿 HERE 拼（旧 bug 的写法）",
          re.search(r"os\.path\.join\(HERE,\s*_SETTINGS_FILE\)", dash_code)
          is None)
    check("启动日志里打了设置文件真实路径（下次好排查）",
          "界面设置文件" in dash_src)
    check("前端改成「改动即存」而不是只靠 beforeunload",
          "saveSettingsSoon" in ui_src or "setTimeout(saveSettings, 400)" in ui_src)

    # ================= ⑤ 键盘区「暂停」= 解除锁定 =================
    # 主人 2026-10-16 追加报回：「键盘就绪以后，长按 w 开始，再点暂停，
    # 再按 w 还会显示已解除锁定，正常应该是暂停以后就默认解除锁定了」
    print("\n[5] 键盘区暂停：必须同时解除锁定")
    import time as _t
    from core import keyboard as kb
    kb_src_c = code_only(read("core/keyboard.py"))
    toast_src = read("core/toast.py")

    check("新增了统一清场函数 _clear_lock_state_locked",
          "def _clear_lock_state_locked" in kb_src_c)
    check("清场函数会把 _active_key 置空",
          "self._active_key = None" in kb_src_c)
    check("暂停分支调用清场（旧代码只补 keyup、状态不清）",
          "self._clear_lock_state_locked()" in kb_src_c)
    check("旧的「只补 keyup 就完事」写法已消失",
          "if not was_paused and holding_sent \\" not in kb_src_c)
    check("暂停文案点明已解除锁定",
          "已解除锁定" in dash_src and "已解除锁定" in toast_src)
    check("恢复文案不再是「已继续」",
          "键盘锁定已继续" not in dash_src and "键盘锁定 已继续" not in toast_src)
    check("恢复文案改为「可重新锁定」", "可重新锁定" in toast_src)

    # ---- 真驱动状态机（send_key 换成记录器，不注入真实按键）----
    _orig_send = kb.send_key
    _KEY_SENT[:] = []
    kb.send_key = lambda vk, up=False, mark_injected=False, use_scancode=True: (
        _KEY_SENT.append((vk, bool(up))) or True)
    try:
        W = kb.key_to_vk("w")
        pausectl.set_paused(False)
        pausectl.set_paused(False, "key")
        lock, events = _key_make_lock(kb)
        _key_hold(lock, kb, W, 1.0)
        _t.sleep(0.25)
        check("长按 W 能进入锁定（基线）",
              lock.active == (True, "W"), "active=%r" % (lock.active,))
        pausectl.set_paused(True, "key")
        _t.sleep(0.4)
        check("暂停后锁定被清掉（旧行为这里仍是 locked）",
              lock.active == (False, "") and lock.state_of(W) == "idle",
              "active=%r state=%r" % (lock.active, lock.state_of(W)))
        check("暂停时补发了 keyup（目标程序不会卡在按住）",
              (W, True) in _KEY_SENT, "SENT=%r" % (_KEY_SENT[-5:],))
        n_before = len(events)
        _key_fire(lock, kb, W, False)
        _t.sleep(0.15)
        _key_fire(lock, kb, W, True)
        _t.sleep(0.25)
        check("★暂停后再按 W 不再冒出 UNLOCK（原始 bug）",
              len(events) == n_before, "events=%r" % (events,))
        pausectl.set_paused(False, "key")
        _t.sleep(0.3)
        check("恢复后没被「排队」立即锁定",
              lock.active == (False, ""), "active=%r" % (lock.active,))
        _key_hold(lock, kb, W, 1.0)
        _t.sleep(0.25)
        check("恢复后重新长按可以再次锁定",
              lock.active == (True, "W"), "active=%r" % (lock.active,))
        check("全程零 UNLOCK 事件",
              [e for e in events if e[0] == "UNLOCK"] == [],
              "events=%r" % (events,))
        _key_stop(lock)

        # ---- 负向对照：证明上面的断言真的在测东西（否则全绿等于没测）----
        # 要复现原始 bug 必须**同时**打掉两层防护：
        #   ① 暂停期间的状态机冻结（_state_frozen）
        #   ② 暂停时的锁定状态清场（_clear_lock_state_locked）
        # 只打其中一层也能不出 UNLOCK —— 那正是「检查失效却全绿」的样子。
        _orig_clear = kb.KeyHoldLock._clear_lock_state_locked
        _orig_frozen = kb.KeyHoldLock._state_frozen
        kb.KeyHoldLock._clear_lock_state_locked = lambda self: None
        kb.KeyHoldLock._state_frozen = lambda self: False
        try:
            pausectl.set_paused(False)
            pausectl.set_paused(False, "key")
            lock2, events2 = _key_make_lock(kb)
            _key_hold(lock2, kb, W, 1.0)
            _t.sleep(0.25)
            pausectl.set_paused(True, "key")
            _t.sleep(0.4)
            _key_fire(lock2, kb, W, False)
            _t.sleep(0.15)
            _key_fire(lock2, kb, W, True)
            _t.sleep(0.25)
            check("负向对照：两层防护都打掉后 UNLOCK 复现（断言有效）",
                  [e for e in events2 if e[0] == "UNLOCK"] != [],
                  "events=%r" % (events2,))
            _key_stop(lock2)
        finally:
            kb.KeyHoldLock._clear_lock_state_locked = _orig_clear
            kb.KeyHoldLock._state_frozen = _orig_frozen
    finally:
        kb.send_key = _orig_send
        pausectl.set_paused(False)
        pausectl.set_paused(False, "mouse")
        pausectl.set_paused(False, "key")

    # ================= [6] v20：齿轮面板显隐 + easy_drag 噪声 =================
    print("\n[6] 齿轮面板：默认隐藏、点一次开再点一次关；easy_drag 噪声已关")
    # 主人 2026-10-17 报：「程序一启动就展示设置页，且点两次齿轮才隐藏」。
    # 真因两层叠加：CSS 漏 display:none（div 默认 block）+ toggleCfg 读的是
    # **内联** style.display（初值是空串，不是 "block"）→ 第一次点被判成
    # 「当前没开」→ 设 block（毫无变化）→ 第二次才隐藏。
    # 修复：CSS 默认 display:none + .cfg-panel.open 显式打开 + JS 用 classList。
    flat_ui = re.sub(r"/\*.*?\*/", "", ui_src, flags=re.S)
    flat_ui = flat_ui.replace(" ", "").replace("\n", "")
    check("面板 CSS 默认隐藏",
          re.search(r"\.cfg-panel\{[^}]*display:none", flat_ui) is not None,
          "缺 .cfg-panel 的 display:none —— 启动即展示，bug 重演")
    check("toggleCfg 用 classList.toggle(\"open\")",
          'classList.toggle("open")' in ui_src,
          "没用 classList —— 「点两次才隐藏」会重演")
    # 剥掉 JS 行注释再查「旧判法有没有残留」：注释里为了说明历史会提到
    # 旧写法，不剥就会误报（硬规则 38 的第三个变体：Python 注释 →
    # CSS 注释 → JS 注释，都踩过）。只剥行首 //，不动 URL 里的 //。
    ui_nojs = "\n".join(ln for ln in ui_src.splitlines()
                        if not ln.lstrip().startswith("//"))
    check("已无 style.display===\"block\" 旧判法",
          'style.display === "block"' not in ui_nojs,
          "残留内联样式判法")
    # easy_drag：frameless 时 pywebview 默认开启，任何 mousedown 都会挂全局
    # mousemove → 调内部 pywebviewMoveWindow → winforms 侧 SetWindowPos 收到
    # None → 每次拖动都往 stderr 刷 traceback。自研拖拽（win_drag_begin 走
    # ReleaseCapture + WM_NCLBUTTONDOWN）与它无关，关掉无副作用。
    dash_code = code_only(dash_src)
    check("create_window 已传 easy_drag=False",
          re.search(r"easy_drag\s*=\s*False", dash_code) is not None,
          "缺 easy_drag=False —— 拖动时 pywebview 内部链路会刷 traceback")
    check("easy_drag=False 与 frameless=True 同在 create_window",
          re.search(r"frameless\s*=\s*True[\s\S]{0,600}?easy_drag\s*=\s*False",
                    dash_code) is not None,
          "easy_drag=False 不在 create_window 里——可能加到别的调用上了")

    # ================= 汇总 =================
    print("\n" + "=" * 62)
    print("汇总：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  失败：%s" % n)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
