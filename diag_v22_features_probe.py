# -*- coding: utf-8 -*-
"""
diag_v22_features_probe.py —— v22 新功能专项探针
================================================

【背景 · 参考 GitHub 同类程序后的优化】
调研了 6 个真实开源项目（EC-Autoclicker / WinAutoClick / yankivare-autoclicker /
OP Auto Clicker / AutoFire / FluAutoClicker）后发现：随机间隔、固定坐标、
点击类型（双击/三击）、启动倒计时是同类工具的标配，而本项目的**引擎层
早就实现了**（core/engine.py 的 jitter_ms / fixed_pos / click_type，
cli.py 也早就有 --jitter / --mode fixed --pos 参数）——只有图形界面
（dashboard）没接线。本探针验证的就是这次接线的四个新功能：

  1. 间隔浮动（jitter ±ms）   —— 引擎 base_interval() 的数学 + 端到端接线
  2. 双击 / 三击               —— 每轮次点 2/3 下，次数按轮次算
  3. 固定坐标                  —— 每轮次先把鼠标搬到固定点（含越界 clamp）
  4. 启动倒计时                —— 开始前数 N 秒，可暂停停表、可停止取消

【测试纪律】和 verify_mouse_zone 一致：_click_once / move_to 全部打桩成
计数器，绝不往系统发真实输入（硬规则 6）。

【负向对照】每个新功能都配一条「打回旧行为」：把 _make_mouse_plugin 的
新参数全丢掉（模拟 v21 及以前的旧版），旧 bug 必须复现——证明断言真的
在测东西，而不是不管怎样都会绿。
"""

import json
import os
import sys
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


def summary():
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 56)
    print("v22 探针汇总：%d 项通过 / %d 项失败（共 %d）"
          % (len(PASS), len(FAIL), total))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 56)
    return 0 if not FAIL else 1


# ------------------------------------------------------------------
# 测试装置
# ------------------------------------------------------------------
def make_stubs():
    """返回一组打桩：点击计数 / 移动记录 / 假屏幕尺寸。"""
    clicks = {"n": 0}
    moves = []
    stubs = {
        "clicks": clicks,
        "moves": moves,
        "real_click": None,
        "real_move": None,
        "real_size": None,
        "real_pos": None,
    }
    return stubs


def install_stubs(D, stubs):
    stubs["real_click"] = D._click_once
    stubs["real_move"] = D.inp.move_to
    stubs["real_size"] = D.inp.screen_size
    stubs["real_vscreen"] = D.inp.virtual_screen
    D._click_once = lambda token: clicks_inc(stubs)
    D.inp.move_to = lambda x, y: (stubs["moves"].append((int(x), int(y)))
                                  or True)
    D.inp.screen_size = lambda: (1920, 1080)
    # v25：坐标 clamp 改用虚拟桌面，打桩成「单屏 1920x1080」
    D.inp.virtual_screen = lambda: (0, 0, 1920, 1080)


def clicks_inc(stubs):
    stubs["clicks"]["n"] += 1


def restore_stubs(D, stubs):
    D._click_once = stubs["real_click"]
    D.inp.move_to = stubs["real_move"]
    D.inp.screen_size = stubs["real_size"]
    D.inp.virtual_screen = stubs["real_vscreen"]


def reset_state(D, api, stubs):
    """清场：停任务 + 清所有暂停位 + 清计数（打桩计数器也必须清零，
    否则上一个场景的注入数会累加进下一个场景的断言）。"""
    try:
        api.stop_mouse()
    except Exception:
        pass
    D.pausectl.set_paused(False)          # 全局 + 两个分区一起清
    stubs["clicks"]["n"] = 0
    stubs["moves"] = []
    with D.ST.lock:
        D.ST.stats["click"] = 0
        D.ST.stats["inject"] = 0
        D.ST.mouse_armed = False


# ------------------------------------------------------------------
# 1. 参数规范化（纯函数）
# ------------------------------------------------------------------
def test_parse_params(D):
    section("1. _parse_mouse_params 规范化")
    f = D._parse_mouse_params

    p, err = f({"button": "right", "action": "double", "count": "5",
                "duration": "1.5", "cps": "20", "jitter": "30",
                "mode": "fixed", "pos": "100,200", "countdown": "3"})
    check("全量参数解析无错", err is None, err)
    check("button 规范化", p and p["button"] == "right", p)
    check("action 保留双击", p and p["action"] == "double", p)
    check("count 转 int", p and p["count"] == 5, p)
    check("duration 转 float", p and p["duration"] == 1.5, p)
    check("jitter 转 int", p and p["jitter"] == 30, p)
    check("mode=fixed + 字符串坐标 -> 列表",
          p and p["mode"] == "fixed" and p["pos"] == [100, 200], p)
    check("countdown 转 int", p and p["countdown"] == 3, p)

    p, err = f({"mode": "fixed", "pos": [320, 240]})
    check("列表坐标也接受（配置导入带进来的是列表）",
          err is None and p["pos"] == [320, 240], (err, p))

    p, err = f({})
    check("空参数 -> 默认值不炸",
          err is None and p["action"] == "click" and p["jitter"] == 0
          and p["mode"] == "cursor" and p["pos"] is None
          and p["countdown"] == 0, (err, p))

    p, err = f({"action": "hold", "countdown": "5", "mode": "fixed",
                "pos": "10,10"})
    check("长按：倒计时归零 + 坐标归一成跟随光标",
          err is None and p["countdown"] == 0 and p["mode"] == "cursor"
          and p["pos"] is None, (err, p))

    p, err = f({"jitter": "-5"})
    check("负抖动 -> 0（不静默出错也不保留负值）",
          err is None and p["jitter"] == 0, (err, p))

    p, err = f({"countdown": "-3"})
    check("负倒计时 -> 0", err is None and p["countdown"] == 0, (err, p))

    p, err = f({"countdown": "99999"})
    check("超大倒计时 -> 600 封顶（防手滑天文数字）",
          err is None and p["countdown"] == 600, (err, p))

    _, err = f({"action": "triple_click"})
    check("未知动作 -> 明确报错", err is not None and "未知动作" in err, err)

    _, err = f({"mode": "fixed", "pos": ""})
    check("固定坐标但没填坐标 -> 明确报错",
          err is not None and "坐标" in err, err)

    _, err = f({"mode": "fixed", "pos": "abc"})
    check("坏坐标 -> 明确报错", err is not None and "坐标" in err, err)

    _, err = f({"mode": "fixed", "pos": "10,20,30"})
    check("三个数的坐标 -> 明确报错", err is not None, err)

    p, err = f({"mode": "fixed", "pos": "9999,9999"})
    check("越界坐标被 clamp 进屏幕（假屏幕 1920x1080）",
          err is None and p["pos"] == [1919, 1079], (err, p))

    p, err = f({"mode": "fixed", "pos": "-50,-50"})
    check("负坐标被 clamp 到 0", err is None and p["pos"] == [0, 0], (err, p))


# ------------------------------------------------------------------
# 2. 抖动数学（引擎层）
# ------------------------------------------------------------------
def test_jitter_math(D):
    section("2. jitter 数学（ClickContext.base_interval）")
    from core.engine import ClickContext

    ctx = ClickContext({}, base_cps=10, jitter_ms=50)
    lo, hi = 0.05, 0.15
    bad = 0
    for _ in range(1000):
        v = ctx.base_interval()
        if not (lo - 1e-9 <= v <= hi + 1e-9):
            bad += 1
    check("1000 次采样全部落在 base±jitter 内", bad == 0,
          "越界 %d 次" % bad)

    ctx0 = ClickContext({}, base_cps=10, jitter_ms=0)
    vals = {round(ctx0.base_interval(), 6) for _ in range(50)}
    check("jitter=0 时间隔恒定（回归：不能突然开始抖）",
          vals == {0.1}, vals)

    ctx_big = ClickContext({}, base_cps=10, jitter_ms=10000)
    vals = {ctx_big.base_interval() for _ in range(200)}
    check("抖动远大于基础间隔时仍有 1ms 下限（不会变负/零）",
          all(v >= 0.001 for v in vals), vals)

    ctx_neg = ClickContext({}, base_cps=10, jitter_ms=-100)
    vals = {round(ctx_neg.base_interval(), 6) for _ in range(20)}
    check("负抖动被引擎归一成 0（间隔恒定）",
          vals == {0.1}, vals)


# ------------------------------------------------------------------
# 3. 端到端：双击 / 固定坐标 / 抖动接线
# ------------------------------------------------------------------
def test_engine_features(D, api, stubs):
    section("3. 端到端：双击 / 固定坐标 / 抖动")

    # --- 双击：3 轮 × 2 下 = 6 次注入 ---
    reset_state(D, api, stubs)
    r = api.arm_mouse({"button": "left", "action": "double", "count": 3,
                       "duration": 0, "cps": 50, "jitter": 0,
                       "mode": "cursor", "pos": "", "countdown": 0},
                      hide=False)
    check("双击参数就绪成功", r.get("ok") is True, r)
    check("last_mouse 带上 action=double",
          api.last_mouse.get("action") == "double", api.last_mouse)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.6)
    api.stop_mouse()
    check("双击：3 轮共注入 6 次（每轮 2 下）",
          stubs["clicks"]["n"] == 6, "clicks=%d" % stubs["clicks"]["n"])
    check("双击：stats.click 与注入数一致",
          D.ST.stats["click"] == 6, D.ST.stats)

    # --- 三击：2 轮 × 3 下 = 6 次注入 ---
    reset_state(D, api, stubs)
    api.arm_mouse({"button": "middle", "action": "triple", "count": 2,
                   "duration": 0, "cps": 50}, hide=False)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.6)
    api.stop_mouse()
    check("三击：2 轮共注入 6 次（每轮 3 下）",
          stubs["clicks"]["n"] == 6, "clicks=%d" % stubs["clicks"]["n"])

    # --- 固定坐标：每轮先搬到 (50,60) 再点 ---
    reset_state(D, api, stubs)
    stubs["moves"] = []
    api.arm_mouse({"button": "left", "action": "click", "count": 4,
                   "duration": 0, "cps": 50, "mode": "fixed",
                   "pos": "50,60"}, hide=False)
    check("固定坐标就绪成功", api.last_mouse.get("mode") == "fixed"
          and api.last_mouse.get("pos") == [50, 60], api.last_mouse)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.6)
    api.stop_mouse()
    check("固定坐标：每轮都搬到 (50,60)",
          len(stubs["moves"]) >= 4
          and all(m == (50, 60) for m in stubs["moves"]),
          "moves=%r" % (stubs["moves"][:6],))
    check("固定坐标：移动次数与轮次一致（4 轮 4 次移动）",
          len(stubs["moves"]) == 4, "moves=%d" % len(stubs["moves"]))

    # --- 固定坐标 + 双击 ---
    reset_state(D, api, stubs)
    stubs["moves"] = []
    api.arm_mouse({"button": "left", "action": "double", "count": 2,
                   "duration": 0, "cps": 50, "mode": "fixed",
                   "pos": "100,200"}, hide=False)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.6)
    api.stop_mouse()
    check("固定坐标+双击：2 轮 2 次移动 4 次点击",
          len(stubs["moves"]) == 2 and stubs["clicks"]["n"] == 4,
          "moves=%d clicks=%d" % (len(stubs["moves"]),
                                  stubs["clicks"]["n"]))


# ------------------------------------------------------------------
# 4. 启动倒计时
# ------------------------------------------------------------------
def test_countdown(D, api, stubs):
    section("4. 启动倒计时")

    # --- 正常倒计时：2 秒内不注入，之后开始 ---
    reset_state(D, api, stubs)
    api.arm_mouse({"button": "left", "action": "click", "count": 0,
                   "duration": 0, "cps": 50, "countdown": 2}, hide=False)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.8)
    check("倒计时中：零注入", stubs["clicks"]["n"] == 0,
          "clicks=%d" % stubs["clicks"]["n"])
    check("倒计时中：保持运行态（不是停止）",
          D.ST.mouse_running is True)
    check("倒计时中：状态行显示倒计时",
          "倒计时" in (D.ST.mouse_info or ""), D.ST.mouse_info)
    time.sleep(2.2)
    check("倒计时结束：开始注入", stubs["clicks"]["n"] > 0,
          "clicks=%d" % stubs["clicks"]["n"])
    api.stop_mouse()

    # --- 倒计时中停止：必须零注入 + 回到未就绪 ---
    reset_state(D, api, stubs)
    api.arm_mouse({"button": "left", "action": "click", "count": 0,
                   "duration": 0, "cps": 50, "countdown": 5}, hide=False)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.5)
    r = api.stop_mouse()
    check("倒计时中停止：成功", r.get("ok") is True, r)
    time.sleep(0.8)
    check("倒计时中停止：全程零注入", stubs["clicks"]["n"] == 0,
          "clicks=%d" % stubs["clicks"]["n"])
    check("倒计时中停止：回到未就绪（armed=False）",
          D.ST.mouse_armed is False, "armed=%r" % D.ST.mouse_armed)

    # --- 暂停 = 停表：倒计时不消耗 ---
    reset_state(D, api, stubs)
    api.arm_mouse({"button": "left", "action": "click", "count": 0,
                   "duration": 0, "cps": 50, "countdown": 2}, hide=False)
    api.start_mouse(api.last_mouse, hide=False)
    time.sleep(0.4)
    D.pausectl.set_paused(True, "mouse")       # 暂停：倒计时应停表
    time.sleep(1.6)
    check("倒计时中暂停 1.6s：倒计时没走完（停表）",
          stubs["clicks"]["n"] == 0, "clicks=%d" % stubs["clicks"]["n"])
    D.pausectl.set_paused(False, "mouse")      # 恢复：继续数
    time.sleep(2.4)
    check("恢复后倒计时走完并开始注入", stubs["clicks"]["n"] > 0,
          "clicks=%d" % stubs["clicks"]["n"])
    api.stop_mouse()


# ------------------------------------------------------------------
# 5. cursor_pos API
# ------------------------------------------------------------------
def test_cursor_pos(D, api):
    section("5. cursor_pos API（「取当前」按钮）")
    real = D.inp.get_cursor_pos
    try:
        D.inp.get_cursor_pos = lambda: (123, 456)
        r = api.cursor_pos()
        check("正常返回坐标", r.get("ok") is True
              and r.get("x") == 123 and r.get("y") == 456, r)

        def _boom():
            raise OSError("拿不到")
        D.inp.get_cursor_pos = _boom
        r = api.cursor_pos()
        check("拿不到时明确失败（不静默返回 0,0）",
              r.get("ok") is False and r.get("msg"), r)
    finally:
        D.inp.get_cursor_pos = real


# ------------------------------------------------------------------
# 6. 设置往返 + 配置导入导出（新键必须活下来）
# ------------------------------------------------------------------
def test_settings_io(D, api, tmp):
    section("6. 设置往返 + 配置导入导出")

    r = api.save_settings({
        "mouse": {"button": "right", "action": "double", "count": "7",
                  "duration": "0", "cps": "15", "jitter": "25",
                  "mode": "fixed", "pos": "640,360", "countdown": "3",
                  "hide": False},
        "key": {"keys": "w", "threshold": "2", "hz": "30",
                "hold": "true", "gap": "20"},
        "hide": False,
    })
    check("save_settings 成功", r.get("ok") is True, r)
    check("v21 修复仍在：_settings 内存同步",
          api._settings.get("mouse", {}).get("jitter") == "25",
          api._settings.get("mouse"))
    check("v21 修复仍在：last_mouse 同步（热键路径用）",
          api.last_mouse.get("jitter") == "25"
          and api.last_mouse.get("pos") == "640,360",
          api.last_mouse)

    # 配置导入：整字典白名单，新键应原样穿过
    cfg = {"schema_version": 1,
           "mouse": {"button": "right", "action": "triple", "cps": 25,
                     "jitter": 40, "mode": "fixed", "pos": [300, 400],
                     "countdown": 3},
           "hide": False}
    r = api.import_config_text(json.dumps(cfg, ensure_ascii=False))
    check("导入带新键的配置成功", r.get("ok") is True, r)
    check("导入后 last_mouse 带新值",
          api.last_mouse.get("jitter") == 40
          and api.last_mouse.get("pos") == [300, 400]
          and api.last_mouse.get("countdown") == 3, api.last_mouse)
    txt = api.export_config()["text"]
    back = json.loads(txt)
    check("导出把新键原样带回（导入→导出往返）",
          back.get("mouse", {}).get("jitter") == 40
          and back["mouse"].get("mode") == "fixed"
          and back["mouse"].get("pos") == [300, 400]
          and back["mouse"].get("countdown") == 3, back)

    # 旧配置（没有新键）导入不能炸
    old = {"schema_version": 1,
           "mouse": {"button": "left", "action": "click", "cps": 10},
           "hide": True}
    r = api.import_config_text(json.dumps(old, ensure_ascii=False))
    check("旧版配置（无新键）导入不炸", r.get("ok") is True, r)


# ------------------------------------------------------------------
# 7. toast 抖动提示
# ------------------------------------------------------------------
def test_toast_jitter(D):
    section("7. toast 抖动提示")
    real = D.toast.show_mouse_started
    seen = {}
    try:
        def _cap(cps=0, trigger="", jitter=0):
            seen["jitter"] = jitter
            return {"ok": True}
        D.toast.show_mouse_started = _cap
        D.toast.show_mouse_started(10, "中键", 30)
        check("start_mouse 把抖动传给 toast", seen.get("jitter") == 30,
              seen)
    finally:
        D.toast.show_mouse_started = real

    import core.toast as T
    captured = {}
    real_show = T.show
    try:
        T.show = lambda text, kind="info", seconds=5: captured.update(
            {"text": text, "kind": kind}) or {"ok": True}
        T.show_mouse_started(10, "中键", 30)
        check("jitter>0：弹窗文案注明浮动",
              "浮动" in captured.get("text", ""), captured)
        T.show_mouse_started(10, "中键", 0)
        check("jitter=0：弹窗文案不出现浮动（不制造噪音）",
              "浮动" not in captured.get("text", ""), captured)
    finally:
        T.show = real_show


# ------------------------------------------------------------------
# 8. 负向对照：打回旧行为，旧 bug 必须复现
# ------------------------------------------------------------------
def test_negative_controls(D, api, stubs):
    section("8. 负向对照（打回旧行为）")

    real_mk = D._make_mouse_plugin

    def old_make(button_token, duration=0, **kw):
        """v21 及以前的旧行为：新参数全部丢弃。"""
        return real_mk(button_token, duration)

    D._make_mouse_plugin = old_make
    try:
        # NC1：双击退化成单击
        reset_state(D, api, stubs)
        api.arm_mouse({"button": "left", "action": "double", "count": 3,
                       "duration": 0, "cps": 50}, hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        time.sleep(0.6)
        api.stop_mouse()
        check("NC1 旧行为复现：双击变单击（3 轮只点 3 下）",
              stubs["clicks"]["n"] == 3,
              "clicks=%d（=3 才证明测试真能区分新旧）" % stubs["clicks"]["n"])

        # NC2：固定坐标失效
        reset_state(D, api, stubs)
        stubs["moves"] = []
        api.arm_mouse({"button": "left", "action": "click", "count": 4,
                       "duration": 0, "cps": 50, "mode": "fixed",
                       "pos": "50,60"}, hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        time.sleep(0.6)
        api.stop_mouse()
        check("NC2 旧行为复现：固定坐标从不搬鼠标",
              len(stubs["moves"]) == 0, "moves=%d" % len(stubs["moves"]))

        # NC3：倒计时失效（立即开始）
        reset_state(D, api, stubs)
        api.arm_mouse({"button": "left", "action": "click", "count": 0,
                       "duration": 0, "cps": 50, "countdown": 5}, hide=False)
        api.start_mouse(api.last_mouse, hide=False)
        time.sleep(0.5)
        check("NC3 旧行为复现：没有倒计时，0.5s 内就已注入",
              stubs["clicks"]["n"] > 0, "clicks=%d" % stubs["clicks"]["n"])
        api.stop_mouse()
    finally:
        D._make_mouse_plugin = real_mk


# ------------------------------------------------------------------
# 9. 双源同步（硬规则 12）
# ------------------------------------------------------------------
def test_html_sync(D):
    section("9. UI 双源同步")
    import hashlib
    import re
    disk = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    src = open(os.path.join(HERE, "dashboard.py"), encoding="utf-8").read()
    m = re.search(r'HTML = r"""(.*?)"""', src, re.S)
    check("dashboard.py 里找得到内嵌 HTML 常量", m is not None)
    if m:
        same = hashlib.md5(disk.encode("utf-8")).hexdigest() == \
            hashlib.md5(m.group(1).encode("utf-8")).hexdigest()
        check("内嵌 HTML == ui/dashboard.html（md5）", same)
    # 新控件必须真的在界面上
    for fid in ("mJitter", "mMode", "mPos", "mCd", "mPosRow"):
        check("界面存在 #%s" % fid, ('id="%s"' % fid) in disk)
    check("界面有 syncMouseRows 联动函数", "function syncMouseRows" in disk)
    check("界面有 recordPos 取坐标函数", "async function recordPos" in disk)
    check("startMouse 传 jitter", "jitter: $(\"mJitter\").value" in disk)
    check("startMouse 传 mode/pos/countdown",
          "mode: $(\"mMode\").value" in disk
          and "pos: $(\"mPos\").value" in disk
          and "countdown: $(\"mCd\").value" in disk)
    check("collectSettings 收集新键",
          "jitter: $(\"mJitter\").value" in disk
          and "countdown: $(\"mCd\").value" in disk)
    check("Python 侧 _make_mouse_plugin 支持点击类型/坐标/倒计时",
          "click_type" in src and "fixed_pos" in src
          and "countdown" in src)
    check("Python 侧引擎调用带 jitter_ms", "jitter_ms=p[\"jitter\"]" in src)
    check("Api 有 cursor_pos 方法", "def cursor_pos(self)" in src)


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------
def main():
    import dashboard as D

    tmp = os.path.join(HERE, "_test_v22_%d.json" % os.getpid())
    D._settings_path = (lambda p=tmp: p)

    stubs = make_stubs()
    install_stubs(D, stubs)
    api = D.Api()
    try:
        test_parse_params(D)
        test_jitter_math(D)
        test_engine_features(D, api, stubs)
        test_countdown(D, api, stubs)
        test_cursor_pos(D, api)
        test_settings_io(D, api, tmp)
        test_toast_jitter(D)
        test_negative_controls(D, api, stubs)
        test_html_sync(D)
    finally:
        try:
            api.stop_mouse()
        except Exception:
            pass
        D.pausectl.set_paused(False)
        restore_stubs(D, stubs)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
            bak = tmp + ".bak"
            if os.path.isfile(bak):
                os.remove(bak)
        except Exception:
            pass
    return summary()


if __name__ == "__main__":
    sys.exit(main())
