# -*- coding: utf-8 -*-
"""
verify_config_io.py —— 配置导入/导出/剪贴板/多方案 回归
=====================================================
【验证什么】2026-10-16 主人要的「把别人的配置导入进来」功能（v19）：
  · 导出：当前通用参数（mouse/key/hide）带 schema_version 收成 JSON；
  · 导入：校验 schema 版本 + 字段白名单 + 自动备份当前配置 +
         只更新白名单键（trigger/window 不跨机迁移）+ 同步热键参数；
  · 剪贴板：复制/粘贴（打桩，不真碰系统剪贴板）；
  · 多方案：profiles/ 目录存读删。
【手法】全部打桩，零真实注入、零真实剪贴板。文件系统重定向到临时目录，
      不读写真机上的 ui_settings.json。
"""
import os
import sys
import json
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


# 文件系统重定向到临时目录，避免读写真机配置
_TMP = tempfile.mkdtemp(prefix="cfgio_")
import dashboard as D

D._settings_dir_pick_cache[0] = None
D._app_dir = lambda: _TMP
D._user_data_dir = lambda: _TMP


def run():
    api = D.Api()
    check("Api 实例可构造", api is not None)

    # ---- [1] 导出 ----
    api._settings = {
        "mouse": {"button": "left", "action": "click", "count": "0",
                  "duration": "0", "cps": "15", "hide": True},
        "key": {"keys": "w", "threshold": "2", "hz": "30",
                "hold": "true", "gap": "20"},
        "hide": True,
        "trigger": {"raw_id": "g2"},   # 设备相关，不应被导出
        "window": {"x": 1},            # 历史死键，不应被导出
        # v40 外观：玻璃透明度 / 按钮样式跟着配置迁移（换机器不掉质感）
        "glass": {"panel": 0.7, "slot": 0.6},
        "btnstyle": "pill",
    }
    d = api._export_dict()
    check("导出含 schema_version=%d" % D._CONFIG_SCHEMA_VERSION,
          d.get("schema_version") == D._CONFIG_SCHEMA_VERSION)
    check("导出带 mouse.cps", d.get("mouse", {}).get("cps") == "15")
    check("导出带 key.keys", d.get("key", {}).get("keys") == "w")
    check("导出带 hide", d.get("hide") is True)
    check("导出不含 trigger（设备相关不迁移）", "trigger" not in d)
    check("导出不含 window（历史死键）", "window" not in d)
    check("导出带 glass", d.get("glass") == {"panel": 0.7, "slot": 0.6}, d.get("glass"))
    check("导出带 btnstyle", d.get("btnstyle") == "pill", d.get("btnstyle"))

    r = api.export_config()
    check("export_config ok", r["ok"], r.get("msg"))
    try:
        parsed = json.loads(r["text"])
        check("export_config 文本可解析且版本对",
              parsed.get("schema_version") == D._CONFIG_SCHEMA_VERSION)
    except Exception as e:
        check("export_config 文本可解析", False, str(e))

    # ---- [2] 导入：合法 ----
    # 先写一次当前配置，使导入时能触发自动备份
    api.save_settings({"mouse": {"cps": "1"}})
    good = json.dumps({"schema_version": 1, "mouse": {"cps": "25"},
                       "key": {"keys": "e"}, "hide": False})
    r = api.import_config_text(good)
    check("合法导入 ok", r["ok"], r.get("msg"))
    check("导入后 mouse.cps 更新", api._settings["mouse"].get("cps") == "25")
    check("导入后 key.keys 更新", api._settings["key"].get("keys") == "e")
    check("导入后 hide 更新", api._settings.get("hide") is False)
    # v1 老配置文件（没有 glass/btnstyle）必须照样能导入，且不动现有外观
    check("v1 老配置导入后 glass 保持不变",
          api._settings.get("glass") == {"panel": 0.7, "slot": 0.6},
          api._settings.get("glass"))
    check("v1 老配置导入后 btnstyle 保持不变",
          api._settings.get("btnstyle") == "pill", api._settings.get("btnstyle"))
    check("导入后生成备份文件",
          os.path.isfile(os.path.join(_TMP, "ui_settings.json.bak")))
    check("导入后 last_mouse 同步", api.last_mouse.get("cps") == "25")
    check("导入后 trigger 不被覆盖",
          api._settings.get("trigger", {}).get("raw_id") == "g2")

    # ---- [3] 导入：拒绝场景 ----
    r = api.import_config_text(json.dumps({"schema_version": 99, "mouse": {}}))
    check("高版本 schema 拒绝", not r["ok"], r.get("msg"))
    r = api.import_config_text("{这不是json")
    check("非法 JSON 拒绝", not r["ok"])
    r = api.import_config_text(json.dumps({}))
    check("空配置拒绝", not r["ok"])
    r = api.import_config_text(json.dumps({"schema_version": 1,
                                           "trigger": {"raw": "zz"}}))
    check("仅含 trigger 的配置拒绝（无白名单键）", not r["ok"])
    # 隐藏键注入：window 不应被写入，合法 mouse 仍生效
    r = api.import_config_text(json.dumps({"schema_version": 1,
                                           "mouse": {"cps": "7"},
                                           "window": {"hack": 1}}))
    check("导入时 window 不被写入（hack 键不存在）",
          api._settings.get("window", {}).get("hack") is None)
    check("导入时合法 mouse 仍生效", api._settings["mouse"].get("cps") == "7")

    # ---- [4] 多方案 ----
    api._settings = {"mouse": {"cps": "30"}}
    r = api.save_profile("农场方案")
    check("保存方案 ok", r["ok"], r.get("msg"))
    check("方案文件已落盘",
          os.path.isfile(os.path.join(_TMP, "profiles", "农场方案.json")))
    r = api.list_profiles()
    check("列表含农场方案", "农场方案" in r.get("profiles", []))
    api._settings = {"mouse": {"cps": "1"}}   # 改当前，验证 load 能覆盖
    r = api.load_profile("农场方案")
    check("加载方案 ok", r["ok"], r.get("msg"))
    check("加载后 mouse.cps=30", api._settings["mouse"].get("cps") == "30")
    r = api.delete_profile("农场方案")
    check("删除方案 ok", r["ok"], r.get("msg"))
    r = api.list_profiles()
    check("删除后列表不含", "农场方案" not in r.get("profiles", []))

    # ---- [5] 剪贴板（打桩，不真碰系统剪贴板）----
    class _Clip(object):
        pass
    _clip = _Clip()
    _clip.text = None
    api._set_clipboard_text = \
        lambda t: (setattr(_clip, "text", t) or (True, ""))
    api._get_clipboard_text = \
        lambda: (True, _clip.text) if _clip.text is not None else (False, "空")
    r = api.copy_config_clipboard()
    check("复制到剪贴板 ok", r["ok"], r.get("msg"))
    check("剪贴板内容已写入", _clip.text is not None)
    r = api.paste_config_clipboard()
    check("从剪贴板粘贴 ok", r["ok"], r.get("msg"))

    # ---- 汇总 ----
    shutil.rmtree(_TMP, ignore_errors=True)
    print("\n配置IO回归：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
