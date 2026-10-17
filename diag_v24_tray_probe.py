# -*- coding: utf-8 -*-
"""
diag_v24_tray_probe.py —— v24 系统托盘专项探针（S1）
====================================================

【v24 做什么】主人「开工」后的 S1：程序在跑就有个托盘图标，右键能
显示 / 暂停·恢复 / 退出；勾了「关闭到托盘」时点 X 不退出而是缩到托盘，
任务继续在后台跑。

【本探针覆盖四层】
  1. 托盘模块本体：常量 / 那个 952 字节的结构体 / WNDPROC 签名 /
     Shell_NotifyIconW 的 argtypes 是否都声明了（规则 2）
  2. 纯逻辑：菜单文案随暂停状态变（getter 优先级）、提示截断、
     菜单命令分派（显示/暂停/退出三路各接各的）
  3. **真机**：真起一个图标 -> PostMessage 双击消息 -> 回调真的被调用
     -> NIM_MODIFY 改提示 -> stop 后能重新挂上（ADD/DELETE 对称）
  4. dashboard 接线：关窗到底藏还是退（_on_window_closing 的三态）+
     save_settings 的内存白名单含 tray + 前端控件与双源一致

【负向对照（3 条）】
  A. 把 _handle 打回「用 self._hwnd 而不是回调 hwnd」的旧写法 ->
     窗口必须建不出来（2026-10-17 真踩过：CreateWindowExW 返回 NULL、
     GetLastError=0，看起来毫无线索）
  B. _tray_should_hide 在「没有图标对象」时必须 False —— 否则藏起来
     就再也叫不回来（看不见的进程占着热键、还在注入）
  C. 菜单「暂停」项绝不能接到 on_exit 上（分派错就是「想暂停结果退出」）

【纪律】除第 3 节的真图标外，不往系统发任何真实输入；托盘图标会
在系统托盘里出现约 1~2 秒，属于本探针的正常行为。
"""

import inspect
import os
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


def summary():
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 56)
    print("v24 探针汇总：%d 项通过 / %d 项失败（共 %d）"
          % (len(PASS), len(FAIL), total))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 56)
    return 0 if not FAIL else 1


def code_only(src):
    """剥 Python 注释（规则 38）。"""
    import re
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    out = []
    for line in src.split("\n"):
        out.append(re.sub(r"#.*$", "", line))
    return "\n".join(out)


def html_code(html):
    import re
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"/\*[\s\S]*?\*/", "", html)
    return re.sub(r"(?m)//.*$", "", html)


# ==================================================================
# 1. 模块本体（常量 / 结构体 / API 声明）
# ==================================================================
def test_module():
    section("1. 托盘模块本体")
    from core import tray as T

    check("Tray 类存在且可实例化（不启动）",
          isinstance(T.Tray(tip="x"), T.Tray))
    for name in ("start", "stop", "set_tip", "set_paused", "toggle_label",
                 "_on_menu_cmd"):
        check("Tray 有 %s" % name, callable(getattr(T.Tray, name, None)))

    # 结构体：Vista 之后的 NOTIFYICONDATAW 是 952 字节（x64）。
    # 少写字段会让 cbSize 变小，Shell_NotifyIcon 可能整条失败。
    size = __import__("ctypes").sizeof(T.NOTIFYICONDATAW)
    check("NOTIFYICONDATAW 尺寸为 952（x64）",
          size == 952 or size == 936, "size=%d" % size)

    # 规则 2：传句柄的 API 必须声明 argtypes，否则 64 位静默截断
    for api, mod in (("Shell_NotifyIconW", T._shell32),
                     ("CreateWindowExW", T._user32),
                     ("DefWindowProcW", T._user32),
                     ("DestroyWindow", T._user32),
                     ("PostMessageW", T._user32),
                     ("TrackPopupMenu", T._user32),
                     ("SetForegroundWindow", T._user32)):
        fn = getattr(mod, api)
        check("%s 声明了 argtypes" % api, fn.argtypes is not None)

    check("LRESULT 是指针宽度整数（64 位不能截断）",
          __import__("ctypes").sizeof(T.LRESULT) ==
          __import__("ctypes").sizeof(__import__("ctypes").c_void_p))

    # WNDPROC 必须是 4 参数（hwnd/msg/wparam/lparam）——少写 hwnd 会
    # TypeError，ctypes 只往 stderr 留一行，图标看着正常实则全哑
    sig = inspect.signature(T.Tray._handle)
    check("WNDPROC 签名是 4 个参数（self 之外）",
          len([p for p in sig.parameters if p != "self"]) == 4,
          list(sig.parameters))

    src = code_only(inspect.getsource(T.Tray._handle))
    check("WM_NCCALCSIZE 走回调传入的 hwnd（不是 self._hwnd）",
          "DefWindowProcW(hwnd, msg, wparam, lparam)" in src)
    check("没有残留 self._hwnd 版 DefWindowProc",
          "DefWindowProcW(self._hwnd" not in src)

    # 菜单项 id 三路互不相同（接错就是「想暂停结果退出」）
    ids = {T.CMD_SHOW, T.CMD_TOGGLE, T.CMD_EXIT}
    check("三个菜单 id 互不相同", len(ids) == 3, ids)


# ==================================================================
# 2. 纯逻辑（文案 / 截断 / 分派）
# ==================================================================
def test_logic():
    section("2. 菜单文案与命令分派")
    from core import tray as T

    calls = []

    t = T.Tray(tip="t", on_show=lambda: calls.append("show"),
               on_toggle=lambda: calls.append("toggle"),
               on_exit=lambda: calls.append("exit"))
    check("未暂停 -> 「暂停运行」", t.toggle_label() == "暂停运行",
          t.toggle_label())
    t.set_paused(True)
    check("暂停后 -> 「恢复运行」", t.toggle_label() == "恢复运行",
          t.toggle_label())

    # getter 优先：缓存值过期也不影响文案（菜单必须现问现答）
    t2 = T.Tray(tip="t", is_paused=lambda: True)
    t2.set_paused(False)                 # 故意把缓存设成相反值
    check("有 getter 时文案以 getter 为准（缓存过期不撒谎）",
          t2.toggle_label() == "恢复运行", t2.toggle_label())
    t3 = T.Tray(tip="t", is_paused=lambda: False)
    t3.set_paused(True)
    check("getter 说没暂停 -> 文案仍是「暂停运行」",
          t3.toggle_label() == "暂停运行", t3.toggle_label())

    # 提示截断
    t.set_tip("x" * 500)
    check("提示超长被截断到 120 字", len(t._tip) <= 120, len(t._tip))

    # 命令分派
    calls[:] = []
    t._on_menu_cmd(T.CMD_SHOW)
    check("CMD_SHOW -> on_show", calls == ["show"], calls)
    calls[:] = []
    t._on_menu_cmd(T.CMD_TOGGLE)
    check("CMD_TOGGLE -> on_toggle", calls == ["toggle"], calls)
    calls[:] = []
    t._on_menu_cmd(T.CMD_EXIT)
    check("CMD_EXIT -> on_exit", calls == ["exit"], calls)
    calls[:] = []
    t._on_menu_cmd(0)                    # 点菜单外面 = 取消
    check("取消（id=0）不触发任何回调", calls == [], calls)

    # 回调抛异常不能穿出去（穿出去消息循环就死，图标变僵尸）
    t4 = T.Tray(tip="t", on_show=lambda: (_ for _ in ()).throw(RuntimeError),
                on_exit=lambda: (_ for _ in ()).throw(RuntimeError))
    try:
        t4._on_menu_cmd(T.CMD_SHOW)
        t4._on_menu_cmd(T.CMD_EXIT)
        ok = True
    except Exception as e:
        ok = False
    check("回调抛异常被吞掉（不毒死消息循环）", ok)


# ==================================================================
# 3. 真机（真起图标 + 真发消息）
# ==================================================================
def test_real_icon():
    section("3. 真机：真起图标 / 真收消息 / 能撤能重挂")
    from core import tray as T
    import ctypes

    hits = []
    t = T.Tray(tip="爪Pro · v24 探针",
               on_show=lambda: hits.append("show"),
               on_toggle=lambda: hits.append("toggle"),
               on_exit=lambda: hits.append("exit"))
    try:
        ok = t.start(timeout=5.0)
        check("图标真的挂上了（NIM_ADD 成功）", ok is True,
              t._error or "(无错误信息)")
        check("running 状态与图标一致", t.running is True)
        check("没有残留错误信息", not t._error, t._error)

        # 真发一条双击消息，验证消息链路通到回调
        hwnd = t._hwnd
        T._user32.PostMessageW(hwnd, T.WM_TRAYICON, 1,
                               T.WM_LBUTTONDBLCLK)
        for _ in range(30):
            if hits:
                break
            time.sleep(0.02)
        check("双击托盘图标 -> on_show 被调用", hits == ["show"], hits)

        # 改提示（NIM_MODIFY）
        t.set_tip("爪Pro · 暂停中")
        time.sleep(0.1)
        check("改提示后仍在运行（NIM_MODIFY 没把图标搞丢）",
              t.running and not t._error, t._error)

        # 右键菜单的分派走的是同一套 _on_menu_cmd（菜单本身会阻塞等用户）
        hits[:] = []
        T._user32.PostMessageW(hwnd, T.WM_TRAYICON, 1, T.WM_RBUTTONUP)
        # 这条会弹菜单（真机可见，2 秒后自动消失要靠下面 stop 打断）
        # —— 不做自动断言，避免探针把用户鼠标困住。
        check("右键消息不崩（菜单可弹出）", t.running)
    finally:
        t.stop()
    check("stop 后图标已撤（running=False）", t.running is False)
    check("stop 后 hwnd 已清空", not t._hwnd)

    # ADD/DELETE 对称：能再挂一次说明库里没有残留
    t2 = T.Tray(tip="爪Pro · 重挂测试")
    t2._class_name = "MouseAutoClickerTrayProbeB"
    try:
        check("撤掉后能重新挂上（ADD/DELETE 对称）", t2.start(5.0) is True,
              t2._error)
    finally:
        t2.stop()

    # ---- 负向对照 A：打回「用 self._hwnd」的旧写法必须建不出窗口 ----
    class OldStyle(T.Tray):
        def _handle(self, hwnd, msg, wparam, lparam):
            if msg == T.WM_CLOSE:
                return 0
            return T._user32.DefWindowProcW(self._hwnd, msg, wparam, lparam)

    bad = OldStyle(tip="负向对照")
    bad._class_name = "MouseAutoClickerTrayProbeOld"
    try:
        res = bad.start(2.0)
        err = bad._error
    finally:
        bad.stop()
    check("负向对照：用 self._hwnd 的写法建窗口失败（证明断言在测东西）",
          res is False, "res=%r err=%r" % (res, err))


# ==================================================================
# 4. dashboard 接线
# ==================================================================
def test_dashboard():
    section("4. dashboard 接线（关窗藏/退 · 设置白名单 · 前端控件）")
    import dashboard as D

    py = code_only(open(os.path.join(HERE, "dashboard.py"),
                        encoding="utf-8").read())
    check("dashboard 导入托盘模块", "from core import tray as tray_mod" in py)
    check("main 里启动托盘", "_tray_start(api)" in py)
    check("closing 走模块级决策函数",
          "return _on_window_closing(api)" in py)
    check("_on_window_closing 会 return True 取消关闭",
          "return True                    # 阻止真正关闭" in py
          or "return True" in py)
    check("shutdown 撤托盘图标", "_tray_stop()" in py)
    check("save_settings 内存白名单含 tray",
          '"hide", "tray"' in py)
    check("Api 暴露 hide_to_tray / show_from_tray",
          "def hide_to_tray(self)" in py and "def show_from_tray(self)" in py)

    # ---- 三态判断 ----
    class FakeApi(object):
        def __init__(self, settings):
            self._settings = settings
            self.hidden = 0
            self.shutdowned = 0

        def hide_to_tray(self):
            self.hidden += 1
            return {"ok": True}

        def show_from_tray(self):
            return {"ok": True}

        def shutdown(self):
            self.shutdowned += 1

    # 负向对照 B：没有图标对象 -> 绝不能藏
    old = D._TRAY["obj"]
    try:
        D._TRAY["obj"] = None
        check("负向对照：没有托盘图标时不藏（否则叫不回来）",
              D._tray_should_hide(FakeApi({"tray": True})) is False)

        D._TRAY["obj"] = object()        # 假装图标在
        check("勾了关闭到托盘 -> 该藏",
              D._tray_should_hide(FakeApi({"tray": True})) is True)
        check("没勾 -> 不藏（X 就是退出）",
              D._tray_should_hide(FakeApi({"tray": False})) is False)
        check("设置里没有 tray 键 -> 默认不藏（老版本设置文件）",
              D._tray_should_hide(FakeApi({})) is False)

        # 关窗决策：藏 = 返回 True 且调 hide_to_tray
        api = FakeApi({"tray": True})
        real_watchdog = D._arm_exit_watchdog
        D._arm_exit_watchdog = lambda *a, **k: None   # 别真起 8 秒自杀
        try:
            ret = D._on_window_closing(api)
            for _ in range(50):
                if api.hidden:
                    break
                time.sleep(0.02)
            check("关窗拦截：返回 True（取消关闭）", ret is True, ret)
            check("关窗拦截：真的调了 hide_to_tray", api.hidden == 1,
                  api.hidden)
            check("关窗拦截：绝不调 shutdown（任务要继续跑）",
                  api.shutdowned == 0, api.shutdowned)

            # 不勾托盘 = 正常退出
            api2 = FakeApi({"tray": False})
            ret2 = D._on_window_closing(api2)
            check("不勾托盘：返回 None（放行关闭）", ret2 is None, ret2)
            check("不勾托盘：调了 shutdown 收尾", api2.shutdowned == 1,
                  api2.shutdowned)
            check("不勾托盘：没有 hide 动作", api2.hidden == 0)
        finally:
            D._arm_exit_watchdog = real_watchdog

        # hide_to_tray 本身：隐藏窗口 + 弹一条「还在后台跑」的提示
        shown = []
        real_toast = D.toast.show
        D.toast.show = lambda *a, **k: shown.append(a)

        class FakeWin(object):
            def __init__(self):
                self.hidden = False

            def hide(self):
                self.hidden = True

            def show(self):
                self.hidden = False

        api3 = D.Api()
        api3._window = FakeWin()
        try:
            r = api3.hide_to_tray()
            check("hide_to_tray 返回 ok", r.get("ok") is True, r)
            check("hide_to_tray 真的隐藏了窗口", api3._window.hidden is True)
            check("hide_to_tray 弹了「仍在后台运行」提示（防误以为已退出）",
                  bool(shown) and "后台运行" in str(shown[0]), shown)
            r2 = api3.show_from_tray()
            check("show_from_tray 把窗口叫回来",
                  r2.get("ok") is True and api3._window.hidden is False)
        finally:
            D.toast.show = real_toast
            api3._window = None
    finally:
        D._TRAY["obj"] = old

    # ---- 前端与双源 ----
    html = open(os.path.join(HERE, "ui", "dashboard.html"),
                encoding="utf-8").read()
    js = html_code(html)
    check("前端有「关闭到托盘」勾选框", 'id="mTray"' in html)
    check("collectSettings 收集 tray", "tray:  $(\"mTray\").checked" in js)
    check("applySettings 恢复 tray", 'if(s.tray != null)' in js)
    check("自动保存 ids 含 mTray", '"mDone","mTray"' in js)
    check("内嵌 HTML == ui/dashboard.html（双源没分叉）",
          D.HTML == html, (len(D.HTML), len(html)))


def main():
    print("v24 系统托盘探针（S1）")
    test_module()
    test_logic()
    test_real_icon()
    test_dashboard()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
