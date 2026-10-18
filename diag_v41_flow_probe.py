# -*- coding: utf-8 -*-
"""
diag_v41_flow_probe.py —— v41 专项探针（录制四步流程 / 两个面板拆分）
=====================================================================

【v41 修什么】

① 录制流程（主人原话）：
   「点击开启录制键 —— 点击启停键（F7）—— 录制行为 —— 录制行为结束点击
    启停键（F7）—— 点击停止录制键」
   旧实现里「开始录制」点下去就在录，F7 只是把会话收尾（而且窗口不回来），
   看起来就是「启停键没有任何意义」。新流程拆成四态：
     armed(待命) → recording → paused → 停止录制收尾
   F7 在 armed 时开录、recording 时暂停、paused 时继续。

② 两个面板分开：
   齿轮 = 右上角设置（导入导出/方案）；图片 = 居中外观（背景/透明度/按钮）。

【测试纪律】
  · 全程不发任何真实键鼠输入（硬规则 6）：feed_* 直接喂结构体，
    dashboard 层用 FakeRec 注入，不装钩子、不动窗口、不弹 toast。
  · 时间轴断言用「区间」而不是精确值：Sleep 的抖动在 CI 上没有上界。
  · 每个场景开头重置 _MACRO，避免上一场景残留（v22 的教训）。
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [OK]   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def sec(title):
    print("\n" + "-" * 58)
    print(title)
    print("-" * 58)


# ----------------------------------------------------------------------
# 1. 录制器：wait_key 待命 / 开录 / 暂停 / 恢复
# ----------------------------------------------------------------------
def test_wait_key_states():
    sec("1. 录制器 · wait_key 四态（待命不记账 → 开录 → 暂停 → 恢复）")
    from core import recorder as R

    def ms(x, y):
        return R.MSLLHOOKSTRUCT(pt=R.POINT(x=x, y=y), mouseData=0,
                                flags=0, time=0, dwExtraInfo=0)

    rec = R.Recorder(start_mode=R.START_WAIT_KEY)
    ok, err = rec.start(ignore_injected=True)
    check("wait_key 能启动", ok, err)
    st = rec.stats()
    check("启动后处于待命（started=False / armed=True）",
          st["started"] is False and st["armed"] is True, st)
    check("待命不是暂停（paused=False）", st["paused"] is False, st)

    # 待命期间喂事件：一条都不许记（这是「点了开始但还没按 F7」的核心保证）
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(10, 10))
    rec.feed_mouse(R.WM_LBUTTONUP, ms(10, 10))
    check("待命期间鼠标按下/抬起一条都不记", rec.count() == 0,
          "记了 %d 条" % rec.count())

    # F7 → 开录
    check("begin() 在待命态返回 True", rec.begin() is True)
    check("begin() 后 started=True", rec.stats()["started"] is True)
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(20, 20))
    rec.feed_mouse(R.WM_LBUTTONUP, ms(20, 20))
    check("开录后正常记账（2 条）", rec.count() == 2, "记了 %d 条" % rec.count())

    # F7 → 暂停
    check("pause() 返回 True", rec.pause() is True)
    check("暂停后 stats.paused=True", rec.stats()["paused"] is True)
    n_before = rec.count()
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(30, 30))
    rec.feed_mouse(R.WM_LBUTTONUP, ms(30, 30))
    rec.feed_key(R.WM_KEYDOWN, R.KBDLLHOOKSTRUCT(vkCode=65, scanCode=0,
                                                 flags=0, time=0,
                                                 dwExtraInfo=0))
    check("暂停期间鼠标和键盘都不记", rec.count() == n_before,
          "%d -> %d" % (n_before, rec.count()))

    # 暂停期间按 Pause 仍然能收尾（stop_vks 的判定在暂停判断之前）
    check("暂停中按停止键仍能置位 stop_requested",
          (rec.feed_key(R.WM_KEYDOWN,
                        R.KBDLLHOOKSTRUCT(vkCode=R.VK_PAUSE, scanCode=0,
                                          flags=0, time=0, dwExtraInfo=0)),
           rec.stop_requested)[1] is True)

    # F7 → 恢复
    check("resume() 返回 True", rec.resume() is True)
    check("恢复后 paused=False", rec.stats()["paused"] is False)
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(40, 40))
    check("恢复后重新记账", rec.count() == n_before + 1,
          "%d -> %d" % (n_before, rec.count()))
    rec.stop()


def test_pause_flush_and_time():
    sec("2. 录制器 · 暂停补抬起 + 时间轴扣掉暂停时长")
    from core import recorder as R

    def kb(vk):
        return R.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0,
                                 time=0, dwExtraInfo=0)

    def ms(x, y):
        return R.MSLLHOOKSTRUCT(pt=R.POINT(x=x, y=y), mouseData=0,
                                flags=0, time=0, dwExtraInfo=0)

    rec = R.Recorder(start_mode=R.START_NOW)
    rec.start(ignore_injected=True)
    check("now 模式启动即开录", rec.stats()["started"] is True)

    rec.feed_key(R.WM_KEYDOWN, kb(65))          # 按住 A
    check("按住 A 被记进 held", 65 in rec._held_keys)
    rec.pause()
    evs = rec.events()
    check("暂停时补了一条 A 的抬起",
          any(e["a"] == "ku" and e.get("vk") == 65 for e in evs), evs)
    check("补抬起后 held 清空", 65 not in rec._held_keys)

    # 暂停 300ms 再恢复：时间轴不该把这段算进去
    t_before = rec._now_ms()
    time.sleep(0.30)
    rec.resume()
    t_after = rec._now_ms()
    check("恢复后时间轴扣掉了暂停时长（差 < 120ms）",
          (t_after - t_before) < 120,
          "暂停 300ms，时间轴却前进了 %dms" % (t_after - t_before))

    # 暂停期间不产生事件，恢复后的事件接在暂停前的时间点上
    rec.feed_mouse(R.WM_LBUTTONDOWN, ms(50, 50))
    evs = rec.events()
    check("恢复后的点击时间戳没被暂停拉长",
          evs[-1]["t"] < 400, evs[-1])
    rec.stop()


def test_wait_key_accepts_f7_skip():
    sec("3. 录制器 · 启停键本身可以被排除（skip_vks）")
    from core import recorder as R
    rec = R.Recorder(start_mode=R.START_WAIT_KEY,
                     skip_vks=(R.VK_PAUSE, R.VK_SCROLL, 0x76))  # 0x76 = F7
    rec.start(ignore_injected=True)
    rec.begin()
    kb = lambda vk: R.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0,
                                      time=0, dwExtraInfo=0)
    rec.feed_key(R.WM_KEYDOWN, kb(0x76))
    rec.feed_key(R.WM_KEYUP, kb(0x76))
    check("F7 的按下/抬起不进事件表", rec.count() == 0,
          "记了 %d 条" % rec.count())
    rec.feed_key(R.WM_KEYDOWN, kb(65))
    check("其他键照常记录", rec.count() == 1)
    rec.stop()


# ----------------------------------------------------------------------
# 4. dashboard：F7 三态分派 + 待命取消
# ----------------------------------------------------------------------
class FakeRec(object):
    """最小录制器替身：只实现 toggle_macro / macro_stop_record 需要的接口。"""

    def __init__(self):
        self.running = True
        self.start_mode = "wait_key"
        self.skip_vks = frozenset()
        self.arm_timeout = False
        self.arm_timeout_mode = ""
        self._started = False
        self._paused = False
        self.calls = []

    def stats(self):
        return {"recording": self.running, "started": self._started,
                "paused": self._paused, "count": 0, "secs": 0.0,
                "armed": not self._started, "waited": 1.0,
                "mode": self.start_mode, "overflow": None, "error": None}

    def begin(self):
        if not self.running or self._started:
            return False
        self._started = True
        self.calls.append("begin")
        return True

    def pause(self):
        if not self.running or not self._started or self._paused:
            return False
        self._paused = True
        self.calls.append("pause")
        return True

    def resume(self):
        if not self.running or not self._paused:
            return False
        self._paused = False
        self.calls.append("resume")
        return True

    def cancel(self):
        self.calls.append("cancel")
        self.running = False

    def stop(self):
        self.calls.append("stop")
        self.running = False
        return []


def test_toggle_macro_states(tmpdir):
    sec("4. dashboard · F7 三态分派（待命开录 / 录制暂停 / 暂停继续）")
    import dashboard as D

    D._settings_path = lambda: os.path.join(tmpdir, "ui_settings.json")
    api = D.Api()
    # 不动真窗口、不弹系统通知（探针只验逻辑）
    api._hide_if_needed = lambda *a, **k: None
    api.show_from_tray = lambda *a, **k: None
    D.toast.show_custom = lambda *a, **k: None

    rec = FakeRec()
    D._MACRO["rec"] = rec
    D._MACRO["playing"] = False

    r = api.toggle_macro()
    check("待命时按 F7 → 开录", r["ok"] and r["msg"] == "开始录制", r)
    check("开录走的是 begin()", rec.calls == ["begin"], rec.calls)

    r = api.toggle_macro()
    check("录制中按 F7 → 暂停", r["ok"] and "暂停" in r["msg"], r)
    check("暂停走的是 pause()", rec.calls == ["begin", "pause"], rec.calls)

    r = api.toggle_macro()
    check("暂停中按 F7 → 继续", r["ok"] and "继续" in r["msg"], r)
    check("继续走的是 resume()", rec.calls == ["begin", "pause", "resume"],
          rec.calls)

    # 回放态的行为不能被动过：pausectl 分区位仍然由 F7 切
    D._MACRO["rec"] = None
    D._MACRO["playing"] = True
    D._MACRO["steps"] = [{"k": "click", "x": 1, "y": 1, "delay": 0}]
    r = api.toggle_macro()
    check("回放中按 F7 → 暂停回放", r["ok"] and "暂停" in r["msg"], r)
    D.pausectl.clear_global()
    r = api.toggle_macro()
    check("再按 F7 → 继续回放", r["ok"] and "继续" in r["msg"], r)
    D._MACRO["playing"] = False
    D._MACRO["steps"] = []
    D.pausectl.clear_global()


def test_stop_in_armed(tmpdir):
    sec("5. dashboard · 待命态点「停止录制」= 取消，不是「一步都没录到」")
    import dashboard as D

    D._settings_path = lambda: os.path.join(tmpdir, "ui_settings.json")
    api = D.Api()
    D.toast.show_custom = lambda *a, **k: None

    rec = FakeRec()
    D._MACRO["rec"] = rec
    D._MACRO["events"] = []
    D._MACRO["steps"] = []
    r = api.macro_stop_record()
    check("待命态停止返回成功", r["ok"] is True, r)
    check("文案是「取消」而不是「没录到」",
          "取消" in r["msg"] and "没录" not in r["msg"], r["msg"])
    check("走的是 cancel()（不生成空步骤表）",
          rec.calls == ["cancel"], rec.calls)
    check("没有产生步骤", not (D._MACRO.get("steps") or []))

    # 未在录制时的兜底
    D._MACRO["rec"] = None
    r = api.macro_stop_record()
    check("没有会话时点停止给出明确提示", r["ok"] is False and "没有" in r["msg"], r)


def test_status_preview(tmpdir):
    sec("6. dashboard · macro_status 暴露三态（界面文案靠它）")
    import dashboard as D

    D._settings_path = lambda: os.path.join(tmpdir, "ui_settings.json")
    api = D.Api()
    D._MACRO["playing"] = False

    rec = FakeRec()
    D._MACRO["rec"] = rec
    st = api.macro_status()
    check("status 有 rec_paused 字段（与回放 paused 分开）",
          "rec_paused" in st, sorted(st))
    check("待命 preview 说「按 … 开始录制」",
          "开始录制" in st["preview"] and "待命" in st["preview"],
          st["preview"])

    rec.begin()
    st = api.macro_status()
    check("录制中 preview 提示按启停键暂停",
          "暂停" in st["preview"], st["preview"])
    check("录制中 rec_paused=False", st["rec_paused"] is False)

    rec.pause()
    st = api.macro_status()
    check("暂停后 rec_paused=True", st["rec_paused"] is True)
    check("暂停 preview 说「已暂停」并给出继续方式",
          "已暂停" in st["preview"] and "继续" in st["preview"],
          st["preview"])
    D._MACRO["rec"] = None


# ----------------------------------------------------------------------
# 7. 前端静态接线
# ----------------------------------------------------------------------
def test_frontend():
    sec("7. 前端 · 两个面板分开 + 录制流程提示")
    p = os.path.join(HERE, "ui", "dashboard.html")
    html = open(p, encoding="utf-8").read()
    import dashboard as D
    check("内嵌 HTML == ui/dashboard.html（双源一致）", D.HTML == html,
          (len(D.HTML), len(html)))

    # --- 两个面板是两套 DOM ---
    i_cfg = html.find('id="cfgPanel"')
    i_look = html.find('id="lookModal"')
    check("设置面板和外观面板都存在", i_cfg > 0 and i_look > 0, (i_cfg, i_look))
    check("外观面板是独立容器（不再复用 cfgPanel）", i_look > i_cfg,
          (i_cfg, i_look))
    # 外观三节必须在外观面板里，且设置面板里不再有它们
    seg_cfg = html[i_cfg:html.find('<div class="look-modal"', i_cfg)]
    seg_look = html[i_look:html.find("<!-- v40 裁剪对话框", i_look)]
    check("设置面板里已无背景网格", 'id="bgGrid"' not in seg_cfg)
    check("设置面板里已无透明度滑块", 'id="glassPanel"' not in seg_cfg)
    check("设置面板里已无按钮样式卡片", "data-bscard" not in seg_cfg)
    check("外观面板里有背景网格", 'id="bgGrid"' in seg_look)
    check("外观面板里有透明度滑块", 'id="glassPanel"' in seg_look)
    check("外观面板里有按钮样式卡片", "data-bscard" in seg_look)

    # --- 开关语义：两个都是 toggle，且互斥 ---
    check("外观入口接的是 toggleLook", 'onclick="toggleLook()"' in html)
    check("设置入口接的是 toggleCfg", 'onclick="toggleCfg()"' in html)
    check("toggleLook 会关掉设置面板",
          'function toggleLook' in html
          and 'p.classList.remove("open")' in
          html[html.find("function toggleLook"):
               html.find("function toggleLook") + 700],
          "外观面板打开时没有收起设置面板")
    _i_cfgfn = html.find("function toggleCfg")
    _seg_cfgfn = html[_i_cfgfn:_i_cfgfn + 700]
    check("toggleCfg 会关掉外观面板",
          _i_cfgfn > 0 and "lookModal" in _seg_cfgfn
          and 'remove("open")' in _seg_cfgfn,
          "设置面板打开时没有收起外观面板")
    check("两个入口有打开态高亮（.tb-btn.on）", ".tb-btn.on{" in html)
    check("Esc 能关闭浮层", 'e.key !== "Escape"' in html)

    # --- 外观模态的排版约束 ---
    check("外观面板居中（align-items/justify-content:center）",
          ".look-modal{" in html and "align-items:center" in
          html[html.find(".look-modal{"):html.find(".look-modal{") + 260])
    check("外观面板正文可内部滚动（overflow-y:auto）",
          ".look-body{" in html and "overflow-y:auto" in
          html[html.find(".look-body{"):html.find(".look-body{") + 260])
    check("浅色主题下外观面板换白底", "html.light .look-box{" in html)

    # --- 录制流程 ---
    check("录制区有启停键流程提示", 'id="macFlowHint"' in html)
    check("流程提示显示真键名（两个容器）",
          'id="macFlowKey"' in html and 'id="macFlowKey2"' in html)
    check("已删除旧「起点」下拉", 'id="macStart"' not in html)
    check("开始录制固定送 wait_key", '"wait_key", 3' in html)
    check("状态文案区分待命/录制中/已暂停",
          "待命中" in html and "已暂停" in html and "录制中" in html)
    check("rec_paused 与 paused 分开消费",
          "d.rec_paused" in html and "d.paused" in html)

    # --- 后端接线 ---
    src = open(os.path.join(HERE, "dashboard.py"), encoding="utf-8").read()
    check("后端定义了 WAIT_KEY_TIMEOUT", "WAIT_KEY_TIMEOUT" in src)
    check("待命超时用长时限（不是 30 秒的 ARM_TIMEOUT）",
          "WAIT_KEY_TIMEOUT\n" in src or "WAIT_KEY_TIMEOUT " in src)
    check("启停键 vk 被加入 skip_vks（F7 不会录进回放）",
          "rec.skip_vks = frozenset" in src)
    check("toggle_macro 有 pause/resume 分支",
          "rec.resume()" in src and "rec.pause()" in src)


def main():
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="pawpro_v41_")
    test_wait_key_states()
    test_pause_flush_and_time()
    test_wait_key_accepts_f7_skip()
    test_toggle_macro_states(tmpdir)
    test_stop_in_armed(tmpdir)
    test_status_preview(tmpdir)
    test_frontend()

    print("\n" + "=" * 58)
    print("通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  ✗ " + f)
    print("=" * 58)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
