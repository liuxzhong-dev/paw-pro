# -*- coding: utf-8 -*-
"""
diag_settings_probe.py —— 「设置文件存哪、重启后还在不在」的探针
=====================================================================

【背景】2026-10-16 主人报：绑好 G1/G2 触发键、关掉程序重开，
又变回默认的中键 / F6。

真因不是配置格式，是**路径**：
    _settings_path() 原本用 HERE = os.path.dirname(os.path.abspath(__file__))。
    PyInstaller 单文件模式下 __file__ 指向 %TEMP%\\_MEIxxxxx\\，而这个
    目录名**每次启动都不一样** —— 上次写的 ui_settings.json，下次启动
    压根不在那个目录里，_load_settings() 读到空 → 全部回落默认值。
    也就是说：不只是触发键，CPS / 点击次数 / 阈值 / 所有界面参数
    从来就没存住过。

实测证据（本机 %TEMP%）：
    全部 _MEI* 历史目录里，只有一次留下了 ui_settings.json，
    其余每次启动都是一个新目录。

【本探针断言什么】
  1. 打包态：目录取 sys.executable 所在目录（= exe 同目录），不是 _MEI
  2. 源码态：取脚本目录（开发时行为不变）
  3. 目录可写 → 设置文件就放那儿（便携优先，符合界面上的承诺）
  4. 目录不可写 → 退回 %LOCALAPPDATA%\\MouseAutoClicker\\（有兜底）
  5. 写 → 读往返一致
  6. **模拟重启**后仍读得到（本 bug 的判据）
  7. 负向对照：用旧语义（每次都变的解压目录）取路径 → 读不到
     （证明第 6 条不是「怎么都能过」的假绿）
  8. 静态：register_hotkeys 真的会把存档的 raw_id 恢复绑回 HUB，
     不是「只存不读」

全程在临时目录里打桩，不碰真实用户目录。
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dashboard as D                                     # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s   %s" % (name, detail))


def reset_cache():
    D._settings_dir_pick_cache[0] = None


def code_only(src):
    """剥掉三引号块和 # 注释行，只留可执行代码形态（硬规则 38）。"""
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


def main():
    tmp = tempfile.mkdtemp(prefix="diag_settings_")
    fake_user = os.path.join(tmp, "userdata")

    old_app_dir = D._app_dir
    old_user_dir = D._user_data_dir
    old_exec = sys.executable
    had_frozen = hasattr(sys, "frozen")

    try:
        D._user_data_dir = lambda: fake_user

        # ---------------------------------------------------------------
        print("=" * 70)
        print("场景 1：打包态 —— 目录必须等于 exe 所在目录，不能是 _MEI")
        print("=" * 70)
        exe_dir = os.path.join(tmp, "AppDir")
        os.makedirs(exe_dir, exist_ok=True)
        fake_exe = os.path.join(exe_dir, "爪Pro.exe")
        with open(fake_exe, "w", encoding="utf-8") as f:
            f.write("")
        sys.frozen = True
        sys.executable = fake_exe
        got = D._app_dir()
        check("frozen 时 _app_dir() = exe 所在目录",
              os.path.normcase(got) == os.path.normcase(exe_dir),
              "got=%r want=%r" % (got, exe_dir))
        check("frozen 时 _app_dir() 里不含 _MEI",
              "_MEI" not in got.upper(), "got=%r" % got)
        check("frozen 时 _app_dir() 不是 __file__ 所在目录",
              os.path.normcase(got) != os.path.normcase(HERE),
              "got=%r HERE=%r" % (got, HERE))

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 2：源码态 —— 目录 = 脚本目录（开发时行为不变）")
        print("=" * 70)
        del sys.frozen
        sys.executable = old_exec
        check("源码态 _app_dir() = 脚本目录", D._app_dir() == HERE,
              "got=%r want=%r" % (D._app_dir(), HERE))

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 3：可写目录优先（便携：拷 exe 时配置跟着走）")
        print("=" * 70)
        D._app_dir = lambda: exe_dir
        reset_cache()
        p = D._settings_path()
        check("可写时选 exe 同目录", os.path.dirname(p) == exe_dir, p)
        check("文件名是 ui_settings.json",
              os.path.basename(p) == "ui_settings.json", p)
        check("探针文件已被清掉（不留垃圾）",
              not os.path.isfile(os.path.join(exe_dir, "_wb_write_probe.tmp")),
              os.listdir(exe_dir))

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 4：目录不可写 —— 必须退回用户目录，不能崩")
        print("=" * 70)
        blocker = os.path.join(tmp, "is_a_file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        # 让「程序目录」的父路径是一个文件 → makedirs 必定失败
        D._app_dir = lambda: os.path.join(blocker, "AppDir")
        reset_cache()
        p = D._settings_path()
        check("不可写时退回用户目录",
              os.path.normcase(os.path.dirname(p))
              == os.path.normcase(fake_user), p)
        check("退回后真的写得进去", os.path.isdir(fake_user), fake_user)

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 5：写 → 读 往返一致")
        print("=" * 70)
        D._app_dir = lambda: exe_dir
        reset_cache()
        payload = {
            "mouse": {"button": "0", "action": "click", "cps": "25"},
            "key": {"keys": "w", "threshold": "1.5"},
            "trigger": {"mouse": 4, "key": 3,
                        "sys": {"key": "ctrl+alt+m"}},
        }
        ok, err = D._save_settings(payload)
        check("保存成功", ok, err)
        check("文件真的落了盘", os.path.isfile(D._settings_path()),
              D._settings_path())
        got = D._load_settings()
        check("读回 trigger.mouse = 4（G2）",
              got.get("trigger", {}).get("mouse") == 4, got)
        check("读回 trigger.key = 3（G1）",
              got.get("trigger", {}).get("key") == 3, got)
        check("读回 mouse.cps = 25",
              got.get("mouse", {}).get("cps") == "25", got)
        check("读回 sys 热键 ctrl+alt+m",
              got.get("trigger", {}).get("sys", {}).get("key")
              == "ctrl+alt+m", got)

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 6：模拟重启 —— 换一个「解压目录」后仍读得到（本 bug 判据）")
        print("=" * 70)
        # 模拟重启：进程内缓存清空（新进程本来就是空的），
        # 同时把「解压目录」换成另一个名字（PyInstaller 每次启动都这样）
        mei_a = os.path.join(tmp, "_MEI111111")
        mei_b = os.path.join(tmp, "_MEI222222")
        os.makedirs(mei_a, exist_ok=True)
        os.makedirs(mei_b, exist_ok=True)

        # 负向对照：用**旧语义**（HERE = 每次启动都换的解压目录）取路径
        D._app_dir = lambda: mei_b
        reset_cache()
        p_old = D._settings_path()
        check("负向对照：旧语义下文件在新解压目录里不存在（所以读不到）",
              os.path.dirname(p_old) == mei_b and not os.path.isfile(p_old),
              p_old)
        got_old = D._load_settings()
        check("负向对照：旧语义下确实读成空（原 bug 复现）",
              got_old == {}, got_old)

        # 新语义：exe 同目录，重启后照样读得到
        D._app_dir = lambda: exe_dir
        reset_cache()
        got_new = D._load_settings()
        check("新语义：模拟重启后仍读到 trigger.mouse = 4",
              got_new.get("trigger", {}).get("mouse") == 4, got_new)

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 7：静态 —— 存档真的会被「读回来」绑上（不是只存不读）")
        print("=" * 70)
        src = open(os.path.join(HERE, "dashboard.py"),
                   encoding="utf-8").read()
        code = code_only(src)
        check("Api.__init__ 从 settings 里取 trigger 存进 _btn_binding",
              re.search(r"_btn_binding\s*=\s*\(self\._settings\.get\("
                        r"\"trigger\"\)", code) is not None, "")
        check("register_hotkeys 按 action 恢复 mouse/key/macro 的 raw_id",
              re.search(r"raw_id\s*=\s*self\._btn_binding\.get\(action\)"
                        r"[\s\S]{0,350}HUB\.set_by_raw_id", code) is not None,
              "缺通用恢复循环")
        check("bind_captured_key 绑完走完整快照持久化",
              re.search(r"_btn_binding\[act\]\s*=\s*rid"
                        r"[\s\S]{0,160}_persist_bindings", code) is not None,
              "仍可能只改内存或写局部快照")
        check("_save_settings 的白名单里有 trigger",
              '"trigger"' in src, "")
        check("路径不再直接用 HERE 拼",
              re.search(r"os\.path\.join\(HERE,\s*_SETTINGS_FILE\)", code)
              is None, "还有 HERE 拼接残留")

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 8：存盘必须同步内存（导出/存方案读的是 self._settings）")
        print("=" * 70)
        # 【2026-10-17 主人实测】v19 存的「默认原神.json」只有 27 字节：
        #   {"schema_version": 1}
        # 加载时报「配置里没有可识别的参数」。真因：save_settings 只写盘、
        # 不更新 self._settings，而 _export_dict()（导出/剪贴板/存方案）
        # 读的是内存 —— 全新启动（还没有 ui_settings.json）时内存是空的，
        # 于是「调好参数 → 存为方案」存出来的是空壳。
        api2 = D.Api()
        api2._settings = {}                 # 模拟刚启动、还没有配置文件
        data = {
            "mouse": {"button": "left", "action": "click", "count": "0",
                      "duration": "0", "cps": "37", "hide": True},
            "key": {"keys": "w", "threshold": "1.5", "hz": "25",
                    "hold": "true", "gap": "15"},
            "trigger": {"raw_id": "g2"},
            "hide": True,
        }
        r = api2.save_settings(data)
        check("save_settings 返回 ok", r.get("ok") is True, "r=%r" % (r,))
        check("保存后内存里就有 mouse/key（导出读它）",
              isinstance(api2._settings.get("mouse"), dict)
              and isinstance(api2._settings.get("key"), dict),
              "_settings=%r" % (api2._settings,))
        check("load_settings() 也能取到刚存的值",
              api2.load_settings().get("mouse", {}).get("cps") == "37",
              "%r" % (api2.load_settings(),))
        d = api2._export_dict()
        check("导出字典带 mouse/key（不再是 27 字节空壳）",
              "mouse" in d and "key" in d, "d=%r" % (d,))
        # 存成方案再读回来（复刻主人的操作路径）
        os.makedirs(D._settings_dir(), exist_ok=True)
        pr = api2.save_profile("探针方案")
        check("存为方案成功", pr.get("ok") is True, "pr=%r" % (pr,))
        lr = api2.load_profile("探针方案")
        check("加载该方案成功（不再报「没有可识别的参数」）",
              lr.get("ok") is True, "lr=%r" % (lr,))
        check("加载后内存被更新（界面会跟着刷新）",
              api2._settings.get("mouse", {}).get("cps") == "37",
              "%r" % (api2._settings,))
        api2.delete_profile("探针方案")

        # ---- 负向对照：把内存合并打回旧行为（只写盘）→ 空壳必须复现 ----
        _orig_save = D.Api.save_settings

        def _old_save_settings(self, data):
            ok, err = D._save_settings(data)
            if isinstance(data, dict):
                if isinstance(data.get("mouse"), dict):
                    self.last_mouse.update(data["mouse"])
                if isinstance(data.get("key"), dict):
                    self.last_key.update(data["key"])
            return {"ok": ok, "msg": err}

        D.Api.save_settings = _old_save_settings
        try:
            api3 = D.Api()
            api3._settings = {}
            api3.save_settings(data)
            d3 = api3._export_dict()
            check("负向对照：旧行为导出空壳（断言确实有效）",
                  "mouse" not in d3 and "key" not in d3, "d3=%r" % (d3,))
            pr3 = api3.save_profile("探针空方案")
            lr3 = api3.load_profile("探针空方案")
            check("负向对照：空方案加载报错（旧 bug 复现）",
                  lr3.get("ok") is False
                  and "没有可识别" in str(lr3.get("msg")), "lr3=%r" % (lr3,))
            api3.delete_profile("探针空方案")
        finally:
            D.Api.save_settings = _orig_save

        # ---------------------------------------------------------------
        print()
        print("=" * 70)
        print("场景 9：绑键后前端自动保存 —— trigger 绝不能被局部表单擦掉")
        print("=" * 70)
        seed = {
            "mouse": {"button": "0", "cps": "10"},
            "key": {"keys": "w", "threshold": "2"},
            "trigger": {"mouse": 4, "key": 3, "macro": 5,
                        "sys": {"mouse": "Ctrl+Alt+M",
                                "key": "Ctrl+Alt+K",
                                "macro": "Ctrl+Alt+R"}},
            "tray": True,
        }
        ok, err = D._save_settings(seed)
        check("场景 9 种子配置保存成功", ok, err)
        api4 = D.Api()
        r4 = api4.save_settings({
            "mouse": {"button": "0", "cps": "27"},
            "key": {"keys": "q", "threshold": "1"},
            "hide": False,
        })
        after = D._load_settings()
        check("局部自动保存后 trigger.mouse 仍是 G2",
              after.get("trigger", {}).get("mouse") == 4, after)
        check("局部自动保存后 trigger.key 仍是 G1",
              after.get("trigger", {}).get("key") == 3, after)
        check("局部自动保存后 trigger.macro 仍是 G3",
              after.get("trigger", {}).get("macro") == 5, after)
        check("局部自动保存后 3 个系统热键全保留",
              after.get("trigger", {}).get("sys") == seed["trigger"]["sys"],
              after)
        check("局部自动保存会更新表单参数",
              r4.get("ok") is True
              and after.get("mouse", {}).get("cps") == "27", after)
        check("不在局部快照里的 tray 不会被擦掉",
              after.get("tray") is True, after)
        check("原子写后没有遗留临时文件",
              not [n for n in os.listdir(exe_dir)
                   if n.startswith(".ui_settings_") and n.endswith(".tmp")],
              os.listdir(exe_dir))

        # 模拟真正重启：重新构造 Api，必须仍拿到完整 trigger。
        api5 = D.Api()
        check("模拟重启后 _btn_binding 仍含 mouse/key/macro",
              all(api5._btn_binding.get(k) == v
                  for k, v in (("mouse", 4), ("key", 3), ("macro", 5))),
              api5._btn_binding)

        # 注册层打桩：保存的系统热键优先；若 start 返回 False（不是抛异常），
        # 也必须继续尝试默认候选，不能让该动作整条热键链消失。
        real_hl = D.hk.HotkeyListener
        real_hub = D.bt.HUB
        started = []

        class FakeHotkey(object):
            fail = set()

            def __init__(self, key, cb):
                self.key = key
                self.callback = cb

            def start(self):
                started.append(self.key)
                return (False, "占用") if self.key in self.fail else (True, "")

            def stop(self):
                return None

        D.hk.HotkeyListener = FakeHotkey
        D.bt.HUB.clear()
        try:
            api5.register_hotkeys()
            check("启动优先注册保存的 mouse 系统热键",
                  api5._hotkeys.get("mouse", (None,))[0] == "Ctrl+Alt+M",
                  api5._hotkeys)
            check("启动优先注册保存的 key 系统热键",
                  api5._hotkeys.get("key", (None,))[0] == "Ctrl+Alt+K",
                  api5._hotkeys)
            check("启动优先注册保存的 macro 系统热键",
                  api5._hotkeys.get("macro", (None,))[0] == "Ctrl+Alt+R",
                  api5._hotkeys)
            by_action = D.bt.HUB.list_by_action()
            check("重启恢复 mouse/key/macro 三个鼠标触发键",
                  {k: by_action.get(k, {}).get("raw_id")
                   for k in ("mouse", "key", "macro")}
                  == {"mouse": 4, "key": 3, "macro": 5}, by_action)

            api6 = D.Api()
            api6._btn_binding = {"sys": {"key": "Ctrl+Alt+K"}}
            FakeHotkey.fail = {"Ctrl+Alt+K"}
            api6.register_hotkeys()
            check("保存热键 start=False 时会回落默认候选",
                  api6._hotkeys.get("key", (None,))[0]
                  in D.HOTKEY_KEY_FALLBACK, api6._hotkeys)
        finally:
            D.hk.HotkeyListener = real_hl
            D.bt.HUB.clear()
            FakeHotkey.fail = set()

        # 负向对照：复刻旧行为（局部表单直接覆盖整文件），trigger 必丢。
        D._save_settings(seed)
        D._save_settings({"mouse": {"cps": "99"}, "hide": False})
        broken = D._load_settings()
        check("负向对照：整文件覆盖会擦掉 trigger（原 bug 可复现）",
              "trigger" not in broken, broken)

    finally:
        D._app_dir = old_app_dir
        D._user_data_dir = old_user_dir
        sys.executable = old_exec
        if not had_frozen and hasattr(sys, "frozen"):
            del sys.frozen
        reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 70)
    print("汇总：通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败 -> %s" % f)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
