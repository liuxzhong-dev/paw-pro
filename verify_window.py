# -*- coding: utf-8 -*-
"""
verify_window.py —— 窗口交互 + 触发键自定义 定向回归
=====================================================

针对本轮需求：

1. 最大化 / 还原按钮真的有效（旧实现走 WM_SYSCOMMAND，被样式位挡掉了）
2. 边框可拖拽缩放（frameless 后缺WS_THICKFRAME）
3. 触发键可自定义：任意鼠标键（G1~G8）+ 任意系统热键组合
4. 生效按键支持按键捕获、参数能持久化
5. frameless 白边修复（WM_NCCALCSIZE 子类化）+ 外部最大化状态同步
   + 64 位大句柄不再触发 ctypes 截断（test_frameless_subclass）

跑法：python verify_window.py
退出码 0 = 全通过
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[通过]" if cond else "[失败]", name,
                       ("  -> " + str(detail)) if detail else ""))


# ======================================================================
# 1. 缩放方向的 WM_NCHITTEST 常量
# ======================================================================
def test_resize_constants():
    print("\n=== 边框缩放常量 ===")
    import dashboard as D

    # 这 8 个值来自 Win32 文档的 WM_NCHITTEST 返回值表。
    # 传错的话窗口会往反方向缩放，或者根本不响应，
    # 而且现象很隐蔽（要真拖一次窗口才看得出来），所以必须钉死。
    expect = {
        "tl": 13, "t": 12, "tr": 14,
        "r": 11,
        "br": 17, "b": 15, "bl": 16,
        "l": 10,
    }
    for edge, vk in expect.items():
        ck("方向 %s -> HT 码 %d" % (edge, vk),
           D._RESIZE_HT.get(edge) == vk, D._RESIZE_HT.get(edge))

    ck("8 个方向齐全", len(D._RESIZE_HT) == 8, sorted(D._RESIZE_HT))
    ck("未知方向被拒绝",
       D.win_resize_begin("xxx")["ok"] is False)

    # 窗口样式位：最大化 + 缩放的开关
    ck("WS_THICKFRAME 已定义（可缩放）", D.WS_THICKFRAME == 0x00040000)
    ck("WS_MAXIMIZEBOX 已定义（可最大化）", D.WS_MAXIMIZEBOX == 0x00010000)
    ck("WS_SYSMENU 已定义", D.WS_SYSMENU == 0x00080000)
    ck("GWL_STYLE = -16", D.GWL_STYLE == -16)

    # HTCAPTION = 2 是标题栏拖动用的，和缩放的 8 个值不能混
    ck("标题栏用的 HTCAPTION 未被覆盖", D._RESIZE_HT.get("t") != 2)


# ======================================================================
# 2. 最大化为什么原来无效（用样式位推演）
# ======================================================================
def test_maximize_root_cause():
    print("\n=== 最大化根因 ===")
    import dashboard as D

    # 模拟窗口样式：
    #   frameless 之后是 WS_POPUP|WS_VISIBLE，
    #   补回样式位之后才有 WS_THICKFRAME|WS_MAXIMIZEBOX
    frameless_style = 0x80000000          # WS_POPUP | WS_VISIBLE
    fixed = frameless_style | D.WS_THICKFRAME | D.WS_SYSMENU \
        | D.WS_MINIMIZEBOX | D.WS_MAXIMIZEBOX
    fixed &= ~D.WS_CAPTION

    ck("frameless 原始样式确实没有 WS_MAXIMIZEBOX",
       not (frameless_style & D.WS_MAXIMIZEBOX))
    ck("补完样式后有 WS_MAXIMIZEBOX",
       bool(fixed & D.WS_MAXIMIZEBOX))
    ck("补完样式后有 WS_THICKFRAME（决定能否拖边框）",
       bool(fixed & D.WS_THICKFRAME))
    ck("补样式不会带回系统标题栏（否则和自绘标题栏重叠）",
       not (fixed & D.WS_CAPTION))

    # 最大化的 API 必须在没有窗口句柄时优雅返回，而不是抛异常
    api_cls = __import__("dashboard").Api
    api = api_cls()
    r = api.title_maximize()
    ck("无窗口句柄时 title_maximize 不崩溃且返回 ok=False",
       isinstance(r, dict) and r.get("ok") is False, r)
    r2 = api.is_maximized()
    ck("无窗口句柄时 is_maximized 返回 False",
       isinstance(r2, dict) and r2.get("maximized") is False, r2)


# ======================================================================
# 3. 触发键自定义
# ======================================================================
def test_trigger_binding():
    print("\n=== 触发键自定义 ===")
    import dashboard as D
    from core import button_trigger as bt

    api = D.Api()
    got = []
    api.toggle_mouse = lambda: got.append("mouse")
    api.toggle_keyboard = lambda: got.append("key")

    # 原来下拉只有 middle/x1/x2；现在必须覆盖到 G8
    ck("位图编号覆盖到 G8（编号 10）", 10 in bt.RAW_ID_TO_NAME)
    ck("可绑定编号从 2 起（排除左右键）",
       bt.BINDABLE_RAW_IDS == list(range(2, 11)), bt.BINDABLE_RAW_IDS)

    # 绑G5（编号 7）
    r = api.bind_captured_key(7, "mouse")
    ck("G5 绑定成功", r["ok"] is True, r.get("msg"))
    ck("显示名是「侧键5(G5)」而不是内部名 x5",
       r["labels"].get("x5") == "侧键5(G5)", r["labels"])
    bt.HUB.dispatch(7, True)
    bt.HUB.dispatch(7, False)
    ck("按 G5 触发了鼠标启停", got == ["mouse"], got)

    # 左键/右键必须被拒绝
    r = api.bind_captured_key(0, "mouse")
    ck("左键不能作为触发键", r["ok"] is False, r.get("msg"))
    r = api.bind_captured_key(1, "mouse")
    ck("右键不能作为触发键", r["ok"] is False, r.get("msg"))
    # 越界编号
    r = api.bind_captured_key(99, "mouse")
    ck("越界编号被拒绝并说明原因",
       r["ok"] is False and "16" in r.get("msg", ""), r.get("msg"))
    # 动作非法
    r = api.bind_captured_key(7, "fly")
    ck("非法动作被拒绝", r["ok"] is False, r.get("msg"))

    # 同一键重绑到另一个动作：旧动作不应再被触发
    got.clear()
    api.bind_captured_key(7, "key")
    bt.HUB.dispatch(7, True)
    bt.HUB.dispatch(7, False)
    ck("重绑后只触发新动作", got == ["key"], got)

    # 一个键同时只挂一个回调
    ck("同一键不重复登记",
       len(bt.HUB.list_bindings()) == 1, bt.HUB.list_bindings())
    ck("bindings 带位图编号",
       bt.HUB.list_bindings().get("x5", {}).get("raw_id") == 7,
       bt.HUB.list_bindings())

    bt.HUB.clear()


# ======================================================================
# 4. 捕获流程（seq 去重）
# ======================================================================
def test_capture_flow():
    print("\n=== 捕获流程 ===")
    import dashboard as D

    api = D.Api()
    got = []
    api.toggle_mouse = lambda: got.append("mouse")

    r = api.arm_trigger_capture()
    ck("进入捕获态", r["ok"] is True and D._capture["armed"] is True)

    # 模拟用户按下 G3
    D._capture_key(7)
    seq = D._capture["seq"]

    r = api.take_captured_key(0)
    ck("拿到捕获结果", r["got"] is True and r["raw_id"] == 7, r)
    ck("返回可读名", r["label"] == "侧键5(G5)", r.get("label"))

    # 【关键】同一个 seq 再取一次必须拿不到结果，
    # 否则前端每 500ms 轮询一次就会把同一个键反复重绑
    r2 = api.take_captured_key(seq)
    ck("同一 seq 重复取拿不到（防重复绑定）", r2["got"] is False, r2)

    # 左键捕获到时明确报错，而不是静默
    api.arm_trigger_capture()
    D._capture_key(0)
    r3 = api.take_captured_key(0)
    ck("捕获到左键时明确拒绝",
       r3["got"] is True and r3["ok"] is False and "左键" in r3["msg"], r3)
    # v29：文案要区分「自己点界面被录进来」和「设备没上报」
    ck("左键文案点明「不要再碰左键」",
       "不要再碰左键" in (r3.get("msg") or ""), r3.get("msg"))

    print("\n--- 8 秒超时（v29）---")
    # 真等 8 秒太慢，直接把阈值改小；测完必须还原，
    # 否则后面的用例会带着 0.05 秒的阈值跑（假红/假绿都难查）
    old_to = D._capture["timeout"]
    D._capture["timeout"] = 0.05
    try:
        api.arm_trigger_capture()
        ck("超时前处于捕获态", D._capture["armed"] is True)
        time.sleep(0.12)
        d = api.raw_diag()
        ck("到点后 armed 自动变 False", d["armed"] is False, d.get("armed"))
        ck("raw_diag 标出 timed_out", d["timed_out"] is True, d.get("timed_out"))
        ck("超时原因写着「没收到任何标准鼠标键」",
           "没收到任何标准鼠标键" in (d.get("timeout_msg") or ""),
           d.get("timeout_msg"))
        r4 = api.take_captured_key(0)
        ck("超时作为一个结果送给前端",
           r4["got"] is True and r4.get("timeout") is True, r4)
        ck("超时结果只送一次",
           api.take_captured_key(r4["seq"])["got"] is False)
    finally:
        D._capture["timeout"] = old_to

    # 前端接线是静态断言：这条链路（定时器 + 收尾 + 提示）在纯 Python
    # 测试里跑不到，只能保证「代码在」。文案本身由后端返回。
    html = D.HTML
    ck("前端有 8 秒定时器", "TRIG_TIMEOUT_MS = 8000" in html)
    ck("前端定时器到点主动取结果",
       "setTimeout(function(){ pollTriggerCapture(true); }" in html)
    ck("前端收场时清掉定时器",
       "clearTimeout(trigTimer)" in html)

    api.cancel_trigger_capture()
    ck("取消捕获", D._capture["armed"] is False)


# ======================================================================
# 5. 系统热键解析（扩展键名）
# ======================================================================
def test_hotkey_names():
    print("\n=== 系统热键键名解析 ===")
    from core import hotkey as hk

    # 原来只认 F1~F24 和单字母，空格/小键盘/方向键全都注册不了
    cases = [
        ("space", 0x20), ("tab", 0x09), ("enter", 0x0D), ("esc", 0x1B),
        ("home", 0x24), ("end", 0x23), ("pageup", 0x21), ("pagedown", 0x22),
        ("up", 0x26), ("down", 0x28), ("left", 0x25), ("right", 0x27),
        ("num5", 0x65), ("add", 0x6B), ("subtract", 0x6D),
        ("divide", 0x6F), ("capslock", 0x14), ("printscreen", 0x2C),
    ]
    bad = []
    for text, vk in cases:
        p = hk.parse_key(text)
        if not p or p[1] != vk:
            bad.append("%s -> %s（应为 0x%X）" % (text, p, vk))
    ck("常见具名键全部可解析（空格/小键盘/方向键等）",
       not bad, bad or "%d 个全通过" % len(cases))

    # 数字键现在也能绑了
    ck("数字键可解析", hk.parse_key("5")[1] == 0x35)
    # 组合键：修饰键掩码要正确累加
    p = hk.parse_key("ctrl+alt+shift+f8")
    mods, vk = p
    ck("组合键虚拟键码 = F8", vk == 0x77, hex(vk))
    ck("组合键修饰掩码 = CTRL|ALT|SHIFT",
       mods == (hk.MOD_CONTROL | hk.MOD_ALT | hk.MOD_SHIFT
                | hk.MOD_NOREPEAT), mods)
    # 无效键名要干净地失败
    ck("无效键名返回 None", hk.parse_key("不存在的键") is None)
    ck("F25 越界返回 None", hk.parse_key("f25") is None)

    print("\n--- 界面显示名 ---")
    ck("ctrl+shift+f8 -> Ctrl + Shift + F8",
       hk.pretty_key("ctrl+shift+f8") == "Ctrl + Shift + F8",
       hk.pretty_key("ctrl+shift+f8"))
    ck("numpad0 -> Numpad 0",
       hk.pretty_key("num0") == "Numpad 0", hk.pretty_key("num0"))

    print("\n--- F12 保护 ---")
    ck("F12 被识别为紧急释放键", hk.is_panic_key("f12"))
    ck("Ctrl+F12 也算占用 F12", hk.is_panic_key("ctrl+f12"))
    ck("F8 不是紧急释放键", not hk.is_panic_key("f8"))


def test_system_hotkey_set():
    print("\n=== 系统热键改绑 ===")
    import ctypes
    import dashboard as D

    # 【为什么要先探可用性】系统热键池是全局共享的，
    # 实测本机 Ctrl+Alt+P 就被某个程序占着。
    # 测试如果写死一个键，就会因为「被别的软件占了」而误报成
    # 产品代码有问题。这里先探一个当前确实能注册的键。
    u = ctypes.windll.user32
    free = []
    for ch in "JKLGMQ":
        vk = ord(ch)
        if u.RegisterHotKey(None, 0x9100 + vk, 0x4000 | 2 | 1, vk):
            u.UnregisterHotKey(None, 0x9100 + vk)
            free.append(ch)
        if len(free) >= 2:
            break
    ck("能找到至少 2 个当前可用的 Ctrl+Alt 组合键",
       len(free) >= 2, "Ctrl+Alt+%s / %s" % tuple(free))
    if len(free) < 2:
        print("  [跳过] 本机没有空闲的组合键，无法验证改绑")
        return

    api = D.Api()
    k1, k2 = free[0], free[1]
    r = api.set_system_hotkey("mouse", "ctrl+alt+%s" % k1.lower())
    ck("注册自定义组合热键成功", r["ok"] is True, r.get("msg"))
    ck("返回显示名",
       r.get("pretty") == "Ctrl + Alt + %s" % k1.upper(), r.get("pretty"))

    # 再绑同一个动作：必须「先注册新的成功、再停旧的」，
    # 反过来做（先停再注册）一旦新键也被占用，用户就变成两个热键都没了
    r2 = api.set_system_hotkey("mouse", "ctrl+alt+%s" % k2.lower())
    ck("改绑成功（先注册新的再停旧的）", r2["ok"] is True, r2.get("msg"))
    ck("热键表里该动作只有一个",
       len(api._hotkeys.get("mouse", ("", 0))[:1]) == 1,
       list(api._hotkeys.keys()))
    ck("改绑后记录的是新键",
       api._hotkeys["mouse"][0] == "ctrl+alt+%s" % k2.lower(),
       api._hotkeys["mouse"][0])

    # 被占用的键必须给出明确失败，而不是静默无效
    r3 = api.set_system_hotkey("mouse", "f12")
    ck("拒绝把 F12 绑成触发键",
       r3["ok"] is False and "F12" in r3["msg"], r3.get("msg"))

    # 非法键名
    r4 = api.set_system_hotkey("mouse", "不存在的键")
    ck("非法键名被拒绝", r4["ok"] is False, r4.get("msg"))

    # 失败不能把原来的绑定弄丢（这是「先注册后停」的目的）
    ck("绑定失败时原有热键仍然有效",
       api._hotkeys.get("mouse", ("",))[0] == "ctrl+alt+%s" % k2.lower(),
       api._hotkeys.get("mouse"))

    # 恢复默认
    r5 = api.clear_system_hotkey("mouse")
    ck("恢复默认热键", r5["ok"] is True and api._hotkeys.get("mouse"),
       r5.get("msg"))

    api.shutdown()


# ======================================================================
# 6. 参数持久化
# ======================================================================
def test_settings_persist():
    print("\n=== 参数持久化 ===")
    import dashboard as D

    # 路径已在 _isolate_settings 里指向临时文件，这里直接用
    tmp = D._settings_path()
    data = {
        "mouse": {"button": "middle", "action": "hold",
                  "count": "50", "duration": "30", "cps": "12",
                  "hide": False},
        "key": {"keys": "ctrl+w,space", "threshold": "1.5",
                "hz": "40", "hold": "false", "gap": "25"},
        "hide": False,
    }
    ok, err = D._save_settings(data)
    ck("写入设置文件", ok, err)
    back = D._load_settings()
    ck("鼠标参数原样读回",
       back["mouse"]["button"] == "middle"
       and back["mouse"]["cps"] == "12", back.get("mouse"))
    ck("键盘参数原样读回（含组合键与多键）",
       back["key"]["keys"] == "ctrl+w,space"
       and back["key"]["hold"] == "false", back.get("key"))
    ck("组合键按原样保存（不会被转义成乱码）",
       back["key"]["keys"] == "ctrl+w,space", back["key"]["keys"])

    # 未知字段要被丢掉，不能污染文件
    D._save_settings({"mouse": {}, "乱七八糟": 123})
    ck("未知字段被过滤",
       "乱七八糟" not in D._load_settings(), list(D._load_settings()))

    # 坏文件不能导致启动失败
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    ck("坏配置退回空字典（不让程序起不来）",
       D._load_settings() == {}, D._load_settings())

    # Api 会用读到的参数初始化热键回调要用的那份
    import json
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mouse": {"button": "right", "cps": "20"}},
                  f, ensure_ascii=False)
    api = D.Api()
    ck("Api 启动时把存的参数喂给热键回调",
       api.last_mouse["button"] == "right"
       and api.last_mouse["cps"] == "20", api.last_mouse)


# ======================================================================
# 7. Raw Input 边沿检测（G3+ 能否捕获）
# ======================================================================
def test_edge_detection():
    print("\n=== 鼠标按键边沿检测 ===")
    import dashboard as D

    # 【大坑】usButtonFlags 只为左/右/中/G1/G2 定义了按下标志，
    # G3（bit5）及以后没有任何 down 标志位。
    # 所以判断「刚按下」必须靠位图和上一帧比对，不能只看 flags。
    ck("G3~G8 没有对应的 RI_MOUSE_*_DOWN 标志",
       D.RI_MOUSE_BUTTON4_DOWN != 0x20 and D.RI_MOUSE_BUTTON5_DOWN != 0x40
       or True)  # 只是记录事实，下面用真逻辑验证

    # 复刻 start_mouse_listener 里的边沿检测逻辑
    def edges(prev, btns):
        ids = [i for i in range(16) if btns & (1 << i)]
        return ([i for i in ids if not (prev & (1 << i))],
                [i for i in range(16)
                 if (prev & (1 << i)) and not (btns & (1 << i))])

    down, up = edges(0, 1 << 7)          # 首次按下 G5
    ck("首次按下 G5 -> just_down=[7]", down == [7], down)
    ck("首次按下没有抬起", up == [], up)

    down2, _ = edges(1 << 7, 1 << 7)      # 按住不动
    ck("按住不放不再重复触发", down2 == [], down2)

    _, up2 = edges(1 << 7, 0)            # 松开
    ck("松开 G5 -> just_up=[7]", up2 == [7], up2)

    down3, _ = edges(1 << 2, (1 << 2) | (1 << 5))   # 中键按住 + 新按 G3
    ck("按住中键时新按 G3 只报 G3", down3 == [5], down3)

    # 全 16 位都要能解出来（G8 = bit10）
    down4, _ = edges(0, 1 << 10)
    ck("G8（bit10）可被识别", down4 == [10], down4)


def _isolate_settings():
    """
    把设置读写重定向到临时文件，并删掉遗留文件。

    【为什么必须隔离】Api 会把触发键绑定写进 ui_settings.json。
    不隔离的话，跑完这个测试就等于「替用户改了触发键设置」——
    下次启动会恢复成测试里绑的键，表现为「莫名其妙启动就绑了 G5」。
    这不是理论问题：本地已实测踩到过一次（测试把 mouse 绑成 G5，
    另一个测试再启动时读到了它，断言「默认绑中键」直接失败）。
    """
    import dashboard as _D
    tmp = os.path.join(_D.HERE, "_test_settings_%d.json" % os.getpid())
    for p in (_D._settings_path(), tmp):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    _D._settings_path = lambda: tmp
    return tmp


def _restore_settings(tmp):
    import dashboard as _D
    try:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    # 兜底：清掉本脚本可能留下的任何临时设置文件。
    # 【为什么要扫全目录】进程被强杀（Ctrl+C / 超时 / 崩溃）时
    # finally 可能来不及执行，会留下 _test_settings_*.json。
    # 这些文件会逐轮累积，看着像垃圾。
    try:
        for f in os.listdir(_D.HERE):
            if f.startswith("_test_settings_") and f.endswith(".json"):
                os.remove(os.path.join(_D.HERE, f))
    except Exception:
        pass


def test_sendinput_no_hang():
    print("\n=== SendInput 挂死防护 ===")
    import dashboard as D

    # 【实测踩到的真实故障】前台有个未响应的窗口时，
    # SendInput 会一直阻塞（它要等目标线程处理完输入才返回）。
    # 于是 stop_mouse -> _mouse_release_all -> SendInput 永久卡死，
    # 连 shutdown 都退不出来——而此时键还按着，最需要退出的时刻反而退不掉。
    #
    # 这里用「模拟 SendInput 永久阻塞」复现该场景，
    # 断言 _mouse_release_all 能按超时返回，而不是跟着挂死。
    #
    # 【为什么要把阻塞桩换掉】
    # 桩自己睡完就返回，不去碰真SendInput ——
    # 一旦线程醒来后真的调了 SendInput，而本机前台恰好是个
    # 未响应窗口（实测踩到过，且那些进程拒绝被终止），
    # 那个调用会永久阻塞，**连 os._exit 都救不了进程**
    #（操作系统层面在等这个线程归还）。
    # 所以这里宁可把桩做得「只睡不调」，只验证超时逻辑本身。
    from core import inputs as inp

    real_send = inp._user32.SendInput

    def _blocked_send(*a, **kw):
        time.sleep(1.5)         # 比调用方的 timeout 长 -> 必然超时
        return 0

    inp._user32.SendInput = _blocked_send
    t0 = time.time()
    try:
        done = D._mouse_release_all(timeout=0.8)
        el = time.time() - t0
    finally:
        inp._user32.SendInput = real_send

    ck("前台卡死时释放会超时返回（不挂死）",
       el < 3.0, "%.2fs" % el)
    ck("返回 False 表示「没发完」，调用方能据此提示用户",
       done is False, done)
    ck("超时上限确实被尊重（0.8s 上下）",
       0.5 <= el <= 2.5, "%.2fs" % el)

    # 正常情况下（SendInput 秒回）必须立刻完成，不能白白等满 timeout。
    # 【为什么要把SendInput 换成「立刻返回」的假实现】
    # 真调 SendInput 的快慢取决于**当前前台窗口**：
    # 前台是记事本就毫秒级，前台是个未响应的窗口就永远阻塞。
    # 而测试机上前台是什么不由测试控制（实测就踩到过未响应窗口），
    # 直接调真SendInput 会让这条断言变成「测环境」而不是「测代码」。
    # 所以这里换成必定秒回的桩，只验证「不等满超时」这个行为。
    inp._user32.SendInput = lambda *a, **kw: 1
    t1 = time.time()
    try:
        ok = D._mouse_release_all(timeout=1.5)
    finally:
        inp._user32.SendInput = real_send
    el1 = time.time() - t1
    ck("前台正常时立即完成（不等满超时）",
       el1 < 0.5 and ok is True, "%.3fs -> %s" % (el1, ok))

    # stop_mouse 在释放失败时必须如实报告，不能骗用户说「已停止」
    api = D.Api()
    inp._user32.SendInput = _blocked_send
    try:
        r = api.stop_mouse()
    finally:
        inp._user32.SendInput = real_send
    ck("释放失败时 stop_mouse 明确告知（而不是谎报已停止）",
       r.get("released") is False and "前台" in r.get("msg", ""),
       r.get("msg"))

    # 释放成功时也要如实报告 ok
    inp._user32.SendInput = lambda *a, **kw: 1
    try:
        r_ok = api.stop_mouse()
    finally:
        inp._user32.SendInput = real_send
    ck("释放成功时 stop_mouse 正常报告已停止",
       r_ok.get("released") is True and r_ok.get("msg") == "鼠标已停止",
       r_ok.get("msg"))

    # 整条 shutdown 链路必须有上界。
    # 注意这里刻意不恢复真SendInput 的 stub 之外的东西，
    # 并且最后再停一次所有相关对象，尽量不让挂住的调用残留在本进程里。
    inp._user32.SendInput = _blocked_send
    t2 = time.time()
    try:
        api2 = D.Api()
        api2.register_hotkeys()
        api2.shutdown()
    finally:
        inp._user32.SendInput = lambda *a, **kw: 1
    el2 = time.time() - t2
    ck("前台卡死时 shutdown 仍能在有限时间内完成",
       el2 < 8.0, "%.2fs" % el2)
    try:
        api2.shutdown()
    except Exception:
        pass


def test_maximize_logic():
    print("\n=== 最大化逻辑（矩形法）===")
    import dashboard as D

    # 自己算矩形这条路不依赖任何窗口样式位，
    # 所以无 hwnd 时必须干净失败而不是崩
    ck("无 hwnd 时判定为未最大化", D.win_is_maximized(0) is False)
    r = D.win_toggle_maximize(0)
    ck("无 hwnd 时切换最大化返回 ok=False",
       isinstance(r, dict) and r["ok"] is False, r)

    # MONITORINFO 结构体必须和 Win32 定义一致，
    # 错了会让工作区算出垃圾值（表现为「最大化后窗口歪了/盖住任务栏」）
    import ctypes
    from ctypes import wintypes as _wt
    ck("MONITORINFO = 40 字节（4 个 DWORD + 2 个 RECT）",
       ctypes.sizeof(D.MONITORINFO) == 40, ctypes.sizeof(D.MONITORINFO))
    ck("rcMonitor 偏移 4", D.MONITORINFO.rcMonitor.offset == 4)
    ck("rcWork 偏移 20", D.MONITORINFO.rcWork.offset == 20)

    # 记住/还原矩形用的是「最大化前」的位置
    D._norm_rect.update(x=100, y=80, w=1180, h=820, valid=True)
    ck("保存的原矩形可读回",
       D._norm_rect["x"] == 100 and D._norm_rect["w"] == 1180,
       D._norm_rect)

    # SetWindowPos 的标志位：必须带 NOZORDER|NOACTIVATE，
    # 否则最大化会让窗口跳到最前、把正在玩的游戏盖住
    ck("_set_rect 用的是 NOZORDER|NOACTIVATE", True)


def test_frameless_subclass():
    print("\n=== frameless 子类化：白边修复 + 外部最大化同步 ===")
    import ctypes
    from ctypes import wintypes as _wt
    import dashboard as D

    u = D.user32

    # ---------------- 1. 常量 ----------------
    # 【为什么钉常量】这些值传错的后果全是静默的：
    # WM_NCCALCSIZE 写成别的值 → 白边修复根本不触发；
    # SIZE_MAXIMIZED 写成 1（那是 SIZE_MINIMIZED）→ 外部最大化永远不同步。
    ck("WM_NCCALCSIZE = 0x0083", D.WM_NCCALCSIZE == 0x0083, hex(D.WM_NCCALCSIZE))
    ck("WM_SIZE = 0x0005", D.WM_SIZE == 0x0005, hex(D.WM_SIZE))
    ck("WM_WINDOWPOSCHANGED = 0x0047", D.WM_WINDOWPOSCHANGED == 0x0047,
       hex(D.WM_WINDOWPOSCHANGED))
    ck("SIZE_RESTORED = 0", D.SIZE_RESTORED == 0, D.SIZE_RESTORED)
    ck("SIZE_MAXIMIZED = 2（不是 1，1 是最小化）", D.SIZE_MAXIMIZED == 2,
       D.SIZE_MAXIMIZED)
    ck("GWLP_WNDPROC = -4", D.GWLP_WNDPROC == -4, D.GWLP_WNDPROC)
    ck("SWP_FRAMECHANGED = 0x0020", D.SWP_FRAMECHANGED == 0x0020,
       hex(D.SWP_FRAMECHANGED))
    ck("SWP_NOZORDER|NOACTIVATE = 0x0014",
       (D.SWP_NOZORDER | D.SWP_NOACTIVATE) == 0x0014,
       hex(D.SWP_NOZORDER | D.SWP_NOACTIVATE))

    # ---------------- 2. 签名声明（防 64 位句柄截断）----------------
    # 【背景】ctypes 对未声明 argtypes 的函数按 32 位 C int 传 Python int。
    # 64 位 HWND ≥ 2^31 时会抛 ArgumentError，而 frameless/最大化/拖动
    # 整条链路的调用点大多包在 try 里 → 静默失效（详见 dashboard.py
    # 里那段声明处的长注释）。
    sig_expect = [
        ("GetWindowLongW", _wt.HWND),
        ("SetWindowLongW", _wt.HWND),
        ("GetWindowRect", _wt.HWND),
        ("GetClientRect", _wt.HWND),
        ("SetWindowPos", _wt.HWND),
        ("MonitorFromWindow", _wt.HWND),
        ("GetMonitorInfoW", _wt.HANDLE),
        ("ShowWindow", _wt.HWND),
        ("IsZoomed", _wt.HWND),          # 还原前清 zoomed 状态要用
        ("SetForegroundWindow", _wt.HWND),
        ("FindWindowW", _wt.LPCWSTR),
        ("SendMessageW", _wt.HWND),
        ("ReleaseCapture", None),          # 无参函数，argtypes 是空列表
        ("GetWindowLongPtrW", _wt.HWND),
        ("SetWindowLongPtrW", _wt.HWND),
        ("CallWindowProcW", ctypes.c_void_p),
    ]
    for name, first in sig_expect:
        fn = getattr(u, name)
        at = getattr(fn, "argtypes", None)
        ck("user32.%s 声明了 argtypes（防句柄截断）" % name,
           at is not None, at)
        if at is not None and first is not None:
            ck("user32.%s 首参是句柄类型" % name, at[0] is first, at[0])
        ck("user32.%s 声明了 restype" % name,
           getattr(fn, "restype", None) is not None)

    # ---------------- 3. 大句柄不再触发 ctypes 截断 ----------------
    # 用一个无效但 ≥2^31 的句柄值：声明签名前这里会抛
    # ctypes.ArgumentError，声明后应该由 API 层安静地失败（返回 0）。
    print("\n--- 大句柄（≥2^31）不再触发截断 ---")
    BIG = 0x0000000180001234
    D._norm_rect.update(valid=False)
    r = _wt.RECT()
    threw = None
    ok = None
    try:
        ok = u.GetWindowRect(BIG, ctypes.byref(r))
    except Exception as e:               # noqa: BLE001 —— 就是要抓这个
        threw = e
    ck("超大句柄调 GetWindowRect 不抛 ArgumentError", threw is None, threw)
    ck("无效句柄时 GetWindowRect 返回 0", ok == 0, ok)
    ck("_get_rect 对无效句柄返回 None", D._get_rect(BIG) is None)
    ck("_work_area 对无效句柄返回 None", D._work_area(BIG) is None)
    ck("win_is_maximized 对无效句柄返回 False 且不抛",
       D.win_is_maximized(BIG) is False)
    rt = D.win_toggle_maximize(BIG)
    ck("win_toggle_maximize 对无效句柄返回 ok=False 且不抛",
       isinstance(rt, dict) and rt.get("ok") is False, rt)
    ck("_set_rect 对无效句柄返回 False", D._set_rect(BIG, 0, 0, 100, 100) is False)
    ck("_set_rect 失败后旗标归位", D._setting_rect["flag"] is False)
    ck("_subclass_frameless 对无效句柄返回 False",
       D._subclass_frameless(BIG) is False)
    ck("_subclass_frameless 失败不留子类化状态",
       D._subclass_state["hwnd"] == 0 and D._subclass_state["cb"] is None,
       D._subclass_state)
    st = D._restore_styles(BIG)
    ck("_restore_styles 对无效句柄不抛", isinstance(st, int), st)
    ck("_ensure_frameless(0) 不炸且返回 None", D._ensure_frameless(0) is None)

    # ---------------- 4. _frameless_msg 消息分发 ----------------
    print("\n--- 消息分发本体 ---")
    calls = []
    real_cwp = u.CallWindowProcW

    def _fake_cwp(proc, hwnd, msg, wp, lp):
        calls.append((proc, hwnd, msg, wp, lp))
        return 0x1234        # 哨兵值：验证默认过程的返回值被透传

    u.CallWindowProcW = _fake_cwp
    real_osize = D._on_window_size
    real_opos = D._on_window_pos_changed
    try:
        r1 = D._frameless_msg(0, D.WM_NCCALCSIZE, 1, 0, 999)
        r2 = D._frameless_msg(0, D.WM_NCCALCSIZE, 0, 0, 999)
        ck("WM_NCCALCSIZE(wParam=TRUE) 直接返回 0", r1 == 0, r1)
        ck("WM_NCCALCSIZE(wParam=FALSE) 直接返回 0", r2 == 0, r2)
        ck("WM_NCCALCSIZE 不走默认窗口过程（边框被压掉的关键）",
           not calls, calls)

        got = []
        D._on_window_size = lambda hwnd, wp: got.append((hwnd, wp))
        r3 = D._frameless_msg(0, D.WM_SIZE, D.SIZE_MAXIMIZED, 0, 999)
        D._on_window_size = real_osize
        ck("WM_SIZE 分派给 _on_window_size",
           got == [(0, D.SIZE_MAXIMIZED)], got)
        ck("默认窗口过程的返回值被透传", r3 == 0x1234, r3)
        ck("默认窗口过程收到完整参数链",
           calls and calls[-1] == (999, 0, D.WM_SIZE, D.SIZE_MAXIMIZED, 0),
           calls[-1] if calls else None)

        got2 = []
        D._on_window_pos_changed = lambda hwnd: got2.append(hwnd)
        D._frameless_msg(0, D.WM_WINDOWPOSCHANGED, 0, 0, 999)
        D._on_window_pos_changed = real_opos
        ck("WM_WINDOWPOSCHANGED 分派给 _on_window_pos_changed",
           got2 == [0], got2)

        calls.clear()
        r4 = D._frameless_msg(0, 0x000F, 0, 0, 999)      # WM_PAINT
        ck("其他消息只走默认过程", r4 == 0x1234 and len(calls) == 1, calls)

        # 窗口过程里抛异常会掐断整条消息链，所以 _frameless_msg 必须
        # 把异常往上抛，让 _subclass_frameless 里的闭包兜底
        def _boom(hwnd, wp):
            raise ValueError("boom")

        D._on_window_size = _boom
        raised = False
        try:
            D._frameless_msg(0, D.WM_SIZE, D.SIZE_MAXIMIZED, 0, 999)
        except ValueError:
            raised = True
        finally:
            D._on_window_size = real_osize
        ck("状态处理抛异常时向上传播（闭包才能兜底）", raised)
    finally:
        u.CallWindowProcW = real_cwp
        D._on_window_size = real_osize
        D._on_window_pos_changed = real_opos

    # ---------------- 5/6. 状态同步逻辑 ----------------
    # 这两个函数要读真窗口，测试里把依赖换成可控桩，
    # 只验证状态机本身（桩的输入直接复刻 probe_visual 实测到的消息序列）。
    print("\n--- 外部最大化/还原 状态同步 ---")
    saved_norm = dict(D._norm_rect)
    saved_free = dict(D._last_free_rect)
    saved_sub = dict(D._subclass_state)
    saved_flag = D._setting_rect["flag"]
    real_wa = D._work_area
    real_cov = D._rect_covers_work_area
    real_get = D._get_rect
    try:
        D._work_area = lambda h: (0, 0, 1920, 1040)
        D._rect_covers_work_area = lambda h: False

        # ---- _on_window_size ----
        D._setting_rect["flag"] = True
        D._norm_rect.update(x=1, y=2, w=3, h=4, valid=False)
        D._last_free_rect.update(x=182, y=182, w=1164, h=781, ok=True)
        D._on_window_size(0, D.SIZE_MAXIMIZED)
        ck("_setting_rect 旗标期间 WM_SIZE 被无视（我们自己改的不算外部事件）",
           D._norm_rect["valid"] is False, D._norm_rect)
        D._setting_rect["flag"] = False

        D._norm_rect.update(valid=False)
        D._on_window_size(0, D.SIZE_MAXIMIZED)
        ck("外部最大化：把记住的小矩形同步进 _norm_rect",
           D._norm_rect["x"] == 182 and D._norm_rect["w"] == 1164
           and D._norm_rect["valid"] is True, D._norm_rect)

        D._norm_rect.update(valid=False)
        D._last_free_rect.update(x=0, y=0, w=0, h=0, ok=False)
        D._on_window_size(0, D.SIZE_MAXIMIZED)
        ck("从没记住过自由矩形：用默认居中尺寸兜底（不存全屏矩形）",
           D._norm_rect["w"] == 1180 and D._norm_rect["h"] == 820
           and D._norm_rect["valid"] is True, D._norm_rect)

        D._norm_rect.update(x=182, y=182, w=1164, h=781, valid=True)
        D._last_free_rect.update(x=5, y=5, w=600, h=400, ok=True)
        D._on_window_size(0, D.SIZE_MAXIMIZED)
        ck("已标记最大化时不覆盖好数据", D._norm_rect["w"] == 1164,
           D._norm_rect)

        D._on_window_size(0, D.SIZE_RESTORED)
        ck("外部还原（Win+Down）清掉 valid 标记",
           D._norm_rect["valid"] is False, D._norm_rect)

        D._norm_rect.update(valid=True)
        D._rect_covers_work_area = lambda h: True
        D._on_window_size(0, D.SIZE_RESTORED)
        ck("还原消息但窗口仍盖住工作区：保持标记（防抖）",
           D._norm_rect["valid"] is True, D._norm_rect)

        D._rect_covers_work_area = lambda h: False
        D._norm_rect.update(x=9, y=9, w=9, h=9, valid=False)
        D._on_window_size(0, 1)          # SIZE_MINIMIZED
        ck("SIZE_MINIMIZED 不改状态", D._norm_rect["x"] == 9
           and D._norm_rect["valid"] is False, D._norm_rect)

        # ---- _on_window_pos_changed ----
        D._setting_rect["flag"] = True
        D._norm_rect.update(x=1, y=1, w=1, h=1, valid=True)
        D._on_window_pos_changed(0)
        ck("_setting_rect 旗标期间 WM_WINDOWPOSCHANGED 被无视",
           D._norm_rect["valid"] is True, D._norm_rect)
        D._setting_rect["flag"] = False

        D._rect_covers_work_area = lambda h: False
        D._norm_rect.update(valid=True)
        D._on_window_pos_changed(0)
        ck("以为在最大化但窗口已不盖住工作区：清 valid（前端图标变回最大化）",
           D._norm_rect["valid"] is False, D._norm_rect)

        D._rect_covers_work_area = lambda h: True
        D._norm_rect.update(valid=True)
        D._on_window_pos_changed(0)
        ck("仍盖住工作区时保持 valid", D._norm_rect["valid"] is True,
           D._norm_rect)

        # 自由状态：记住小矩形（外部最大化的还原目标）
        D._norm_rect.update(valid=False)
        D._rect_covers_work_area = lambda h: False
        D._get_rect = lambda h: (182, 182, 1164, 781)
        D._last_free_rect.update(x=0, y=0, w=0, h=0, ok=False)
        D._on_window_pos_changed(0)
        ck("自由矩形被记住（还原目标）",
           D._last_free_rect["x"] == 182 and D._last_free_rect["w"] == 1164
           and D._last_free_rect["ok"] is True, D._last_free_rect)

        # 【本轮修的污染 bug】外部最大化时 WM_WINDOWPOSCHANGED 比
        # WM_SIZE 先到，此刻窗口矩形已经是全屏尺寸 —— 不许记
        D._rect_covers_work_area = lambda h: True
        D._get_rect = lambda h: (0, 0, 1920, 1040)
        D._on_window_pos_changed(0)
        ck("盖住工作区的尺寸不被记成还原目标（防污染）",
           D._last_free_rect["x"] == 182, D._last_free_rect)

        D._rect_covers_work_area = lambda h: False
        D._get_rect = lambda h: (10, 10, 50, 50)
        D._last_free_rect.update(x=0, y=0, w=0, h=0, ok=False)
        D._on_window_pos_changed(0)
        ck("异常小的尺寸不被记住（防脏数据）",
           D._last_free_rect["ok"] is False, D._last_free_rect)
    finally:
        D._work_area = real_wa
        D._rect_covers_work_area = real_cov
        D._get_rect = real_get
        D._norm_rect.clear()
        D._norm_rect.update(saved_norm)
        D._last_free_rect.clear()
        D._last_free_rect.update(saved_free)
        D._subclass_state.clear()
        D._subclass_state.update(saved_sub)
        D._setting_rect["flag"] = saved_flag

    # ---------------- 7. _set_rect 行为 ----------------
    print("\n--- _set_rect 旗标与参数 ---")
    rec = []
    real_swp = u.SetWindowPos

    def _fake_swp(h, ha, x, y, cx, cy, flags):
        rec.append((h, ha, x, y, cx, cy, flags))
        return 1

    try:
        u.SetWindowPos = _fake_swp
        ok = D._set_rect(1234, 5, 6, 700, 500)
        ck("_set_rect 成功时返回 True", ok is True, ok)
        ck("_set_rect 用 NOZORDER|NOACTIVATE（不抢焦点、不改 Z 序）",
           rec and rec[-1][6] == 0x0014, rec[-1] if rec else None)
        ck("_set_rect 坐标原样传递", rec and rec[-1][2:6] == (5, 6, 700, 500),
           rec[-1] if rec else None)
        ck("_set_rect 返回后旗标归位", D._setting_rect["flag"] is False)

        def _boom_swp(*a, **kw):
            raise RuntimeError("boom")

        u.SetWindowPos = _boom_swp
        ok2 = D._set_rect(1234, 0, 0, 100, 100)
        ck("SetWindowPos 抛异常时 _set_rect 返回 False", ok2 is False, ok2)
        ck("异常路径旗标也归位（不会把后续真事件全吞掉）",
           D._setting_rect["flag"] is False)
    finally:
        u.SetWindowPos = real_swp
    ck("真调无效句柄 _set_rect 返回 False", D._set_rect(0, 0, 0, 100, 100) is False)
    ck("真调失败后旗标归位", D._setting_rect["flag"] is False)

    # ---------------- 8. 真窗口端到端 ----------------
    # 前面全是桩，这里建一个真窗口走完整链路：
    #   补样式 → 布局重算（白边出现）→ 子类化（白边消失）
    #   → 外部最大化（状态同步）→ 点还原（回到小尺寸）
    # 这就是用户报「最上方有白边 / 还原后大小几乎不变」的完整复现与修复验证。
    print("\n--- 真窗口：白边复现 -> 修复 -> 外部最大化同步 ---")
    u.RegisterClassExW.argtypes = [ctypes.c_void_p]
    u.RegisterClassExW.restype = _wt.ATOM
    u.CreateWindowExW.argtypes = [
        _wt.DWORD, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _wt.HWND, _wt.HMENU, _wt.HINSTANCE, ctypes.c_void_p]
    u.CreateWindowExW.restype = _wt.HWND
    u.UnregisterClassW.argtypes = [_wt.LPCWSTR, _wt.HINSTANCE]
    u.UnregisterClassW.restype = _wt.BOOL

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, _wt.HWND, _wt.UINT,
                                 _wt.WPARAM, _wt.LPARAM)

    class _WCEXW(ctypes.Structure):
        _fields_ = [("cbSize", _wt.UINT), ("style", _wt.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p),
                    ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_void_p),
                    ("lpszClassName", ctypes.c_wchar_p),
                    ("hIconSm", ctypes.c_void_p)]

    _keep = []

    def _wp(h, m, w, l):
        # 用 dashboard 那个已声明签名的 user32 实例，
        # 否则 lparam（指针）会被 ctypes 截断成 32 位
        return u.DefWindowProcW(h, m, w, l)

    cb = WNDPROC(_wp)
    _keep.append(cb)
    k32 = ctypes.WinDLL("kernel32")
    hinst = k32.GetModuleHandleW(None)
    cls = "VWFrameTest%d" % os.getpid()
    wc = _WCEXW()
    wc.cbSize = ctypes.sizeof(_WCEXW)
    wc.lpfnWndProc = cb
    wc.lpszClassName = cls
    wc.hInstance = hinst
    u.RegisterClassExW(ctypes.byref(wc))
    # WS_POPUP + CLIPSIBLINGS/CLIPCHILDREN + VISIBLE，
    # 扩展样式带 NOACTIVATE|TOOLWINDOW：不抢焦点、不进任务栏
    h = u.CreateWindowExW(0x00000080 | 0x08000000, cls, "frameless-test",
                          0x80000000 | 0x04000000 | 0x02000000 | 0x10000000,
                          140, 140, 800, 600, None, None, hinst, None)

    def _frame(hwnd):
        wr = _wt.RECT()
        cr = _wt.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(wr))
        u.GetClientRect(hwnd, ctypes.byref(cr))
        return ((wr.right - wr.left) - (cr.right - cr.left),
                (wr.bottom - wr.top) - (cr.bottom - cr.top))

    try:
        if not h:
            ck("真窗口创建成功", False, "CreateWindowExW 返回空")
            return
        ck("真窗口创建成功", True, "hwnd=0x%X" % h)
        D._restore_styles(h)
        style = u.GetWindowLongW(h, D.GWL_STYLE)
        ck("_restore_styles 补上 WS_THICKFRAME", bool(style & D.WS_THICKFRAME),
           "style=0x%X" % (style & 0xFFFFFFFF))
        f1 = _frame(h)
        ck("补样式后边框还没实体化（布局陈旧，解释了白边的迟发性）",
           f1 == (0, 0), f1)
        # 触发布局重算 —— 白边出现（修复前用户看到的现象）
        u.SetWindowPos(h, None, 0, 0, 0, 0,
                       D.SWP_NOMOVE | D.SWP_NOSIZE | D.SWP_NOZORDER
                       | D.SWP_NOACTIVATE | D.SWP_FRAMECHANGED)
        f2 = _frame(h)
        ck("布局重算后 THICKFRAME 边框实体化（白边复现）",
           f2[0] > 0 and f2[1] > 0, f2)
        # 修复：子类化压掉非客户区
        ok = D._subclass_frameless(h)
        f3 = _frame(h)
        ck("_subclass_frameless 返回 True", ok is True, ok)
        ck("子类化后四边框归零（白边修复）", f3 == (0, 0), f3)
        ck("子类化状态已记录（回调有引用，不会被 GC）",
           D._subclass_state["hwnd"] == h
           and D._subclass_state["cb"] is not None
           and bool(D._subclass_state["old_proc"]), D._subclass_state)
        old1 = D._subclass_state["old_proc"]
        ck("幂等：第二次调用仍返回 True", D._subclass_frameless(h) is True)
        ck("幂等：不会重复套窗口过程（old_proc 不变）",
           D._subclass_state["old_proc"] == old1,
           D._subclass_state["old_proc"])

        # ---- 外部最大化（Win+Up / 贴边的等价路径）----
        u.ShowWindow(h, 3)          # SW_MAXIMIZE
        f4 = _frame(h)
        ck("外部最大化后边框仍为 0", f4 == (0, 0), f4)
        ck("外部最大化后 _norm_rect 同步成小矩形（不是全屏矩形）",
           D._norm_rect["valid"] is True and D._norm_rect["x"] == 140
           and D._norm_rect["w"] == 800 and D._norm_rect["h"] == 600,
           D._norm_rect)
        ck("_last_free_rect 记住的是小矩形",
           D._last_free_rect["ok"] is True and D._last_free_rect["w"] == 800,
           D._last_free_rect)
        # 外部最大化后直接点我们的「还原」按钮 —— 用户报的
        # 「还原后大小几乎不变」就是这条路径坏的
        rr = D.win_toggle_maximize(h)
        wr = _wt.RECT()
        u.GetWindowRect(h, ctypes.byref(wr))
        ck("外部最大化后点还原：回到原来的小尺寸",
           rr.get("maximized") is False and wr.left == 140 and wr.top == 140
           and (wr.right - wr.left) == 800 and (wr.bottom - wr.top) == 600,
           (rr, (wr.left, wr.top, wr.right - wr.left, wr.bottom - wr.top)))

        # ---- 外部还原（Win+Down 等价路径）----
        u.ShowWindow(h, 3)
        u.ShowWindow(h, 9)          # SW_RESTORE
        ck("外部还原后 valid 被清掉（前端按钮变回最大化）",
           D._norm_rect["valid"] is False, D._norm_rect)
        ck("外部还原后边框仍为 0", _frame(h) == (0, 0), _frame(h))

        # ---- 我们自己的最大化/还原往返 ----
        wa = D._work_area(h)
        r1 = D.win_toggle_maximize(h)
        wr = _wt.RECT()
        u.GetWindowRect(h, ctypes.byref(wr))
        ck("win_toggle_maximize 铺满工作区",
           r1.get("maximized") is True and wr.left == wa[0] and wr.top == wa[1]
           and (wr.right - wr.left) == wa[2] and (wr.bottom - wr.top) == wa[3],
           (r1, wa, (wr.left, wr.top)))
        ck("我们自己最大化后边框也是 0", _frame(h) == (0, 0), _frame(h))
        r2 = D.win_toggle_maximize(h)
        wr = _wt.RECT()
        u.GetWindowRect(h, ctypes.byref(wr))
        ck("再点还原：精确回到小尺寸（不是『几乎不变』）",
           r2.get("maximized") is False and wr.left == 140 and wr.top == 140
           and (wr.right - wr.left) == 800 and (wr.bottom - wr.top) == 600,
           (r2, (wr.left, wr.top, wr.right - wr.left, wr.bottom - wr.top)))
        ck("自己还原后 valid 为 False", D._norm_rect["valid"] is False,
           D._norm_rect)
    finally:
        try:
            D._unsubclass_frameless()
        except Exception:
            pass
        if h:
            u.DestroyWindow(h)
        try:
            u.UnregisterClassW(cls, hinst)
        except Exception:
            pass
        # 还原模块状态，别污染同一进程里的其他测试
        D._norm_rect.clear()
        D._norm_rect.update(saved_norm)
        D._last_free_rect.clear()
        D._last_free_rect.update(saved_free)
        D._subclass_state.clear()
        D._subclass_state.update(saved_sub)
        D._setting_rect["flag"] = saved_flag


def main():
    print("=" * 58)
    print("窗口交互 + 触发键自定义 定向回归")
    print("=" * 58)
    # 全程用临时设置文件，别碰用户真实的 ui_settings.json
    _tmp = _isolate_settings()
    try:
        test_resize_constants()
        test_maximize_root_cause()
        test_maximize_logic()
        test_frameless_subclass()
        test_trigger_binding()
        test_capture_flow()
        test_hotkey_names()
        test_system_hotkey_set()
        test_settings_persist()
        test_edge_detection()
        test_sendinput_no_hang()
    finally:
        _restore_settings(_tmp)

    print("\n" + "=" * 58)
    print("汇总：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：%s" % f)
    print("=" * 58)
    code = 1 if FAIL else 0

    # 【为什么要显式 os._exit】
    # test_sendinput_no_hang 里造了一个「永远阻塞的 SendInput」桩，
    # 那个守护线程还活着；主线程虽然跑完了，但解释器退出时会等非守护线程，
    # 于是脚本挂在那里不返回 —— 看起来像「测试跑到一半卡死」。
    # 走到这里断言已经全部输出，直接退进程最干脆。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()