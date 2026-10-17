# -*- coding: utf-8 -*-
"""
core/config.py —— 配置持久化

配置文件放在 exe 同目录的 config.json（便携模式）。
用 %APPDATA% 的话打包后路径会很深，不利于用户找，所以默认放 exe 旁边。

【v23 新增 · S5 CLI --profile】
profiles/ 方案目录原本只有 dashboard（图形版）在用，命令行版想
「按名字加载预存参数」却没有读者。这里补一套同语义的实现：
settings_dir()/profiles_dir()/profile_path()/load_profile()，
与 dashboard 的 _settings_dir()/_profiles_dir() 是**同一套挑选逻辑**
（优先 exe 同目录，写不进退 %LOCALAPPDATA%），两边路径必须一致——
diag_v23 探针里有断言互相对账，改一边忘另一边会红。
"""

import json
import os
import sys
import time


def app_dir():
    """返回程序所在目录。打包成 exe 后 sys.executable 才是 exe 自身路径。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.path.join(app_dir(), "config.json")


# --------------------------------------------------------------------------
# 界面设置 / 多方案（profiles/）—— v23 S5
# --------------------------------------------------------------------------
# 方案文件的 schema 版本与字段白名单。**必须和 dashboard 的
# _CONFIG_SCHEMA_VERSION / _CONFIG_EXPORT_KEYS 保持一致**：
# 图形版存出来的方案命令行版要能读，版本对不上就是「存得了读不了」。
# 改这里记得同步 dashboard.py（探针有对账断言）。
PROFILE_SCHEMA_VERSION = 1
PROFILE_EXPORT_KEYS = ("mouse", "key", "hide")


def _user_data_dir():
    """用户级数据目录（和启动日志同一个地方）。"""
    return os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "MouseAutoClicker")


_settings_dir_cache = [None]


def settings_dir():
    """
    选界面设置放哪：**优先 exe 同目录**（便携、看得见、跨机器可拷贝），
    目录写不进去（Program Files、只读盘）才退
    %LOCALAPPDATA%\\MouseAutoClicker\\。结果缓存一次。

    【为什么不能用 app_dir() 一路到底】打包后 exe 可能在 Program Files，
    普通用户没有写权限，静默失败比退回用户目录难查。
    """
    if _settings_dir_cache[0]:
        return _settings_dir_cache[0]
    for d in (app_dir(), _user_data_dir()):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, "_wb_write_probe.tmp")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            _settings_dir_cache[0] = d
            return d
        except Exception:
            continue
    _settings_dir_cache[0] = _user_data_dir()
    return _settings_dir_cache[0]


def profiles_dir(base=None):
    """
    方案目录：<设置目录>/profiles，不存在就建。

    base 可显式传一个设置目录（dashboard 传自己的 _settings_dir()）：
    图形版的设置目录支持重定向（测试临时目录、将来可能的自定义路径），
    传进来才能保证「命令行读到的就是图形版存的那一份」。
    """
    d = os.path.join(base or settings_dir(), "profiles")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def profile_path(name, base=None):
    """方案文件名安全化：滤掉路径分隔符，防目录穿越。"""
    safe = "".join(c for c in str(name).strip() if c not in r'\/:*?"<>|')
    return os.path.join(profiles_dir(base), safe + ".json")


def list_profiles(base=None):
    """列出全部方案名（不含 .json 后缀），排序。失败返回空列表。"""
    try:
        d = profiles_dir(base)
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
    except Exception:
        return []


def validate_profile_dict(data):
    """
    校验方案字典，返回 (ok, cleaned, msg)。cleaned 只留白名单键；
    schema 版本比本程序新的文件直接拒绝（不猜不兼容字段）。
    """
    if not isinstance(data, dict):
        return False, None, "方案不是合法的 JSON 对象"
    v = data.get("schema_version")
    if isinstance(v, int) and v > PROFILE_SCHEMA_VERSION:
        return False, None, ("方案版本不兼容（文件为 v%d，本程序支持 v%d）"
                            % (v, PROFILE_SCHEMA_VERSION))
    cleaned = {}
    for k in PROFILE_EXPORT_KEYS:
        if k not in data:
            continue
        val = data[k]
        if k == "hide":
            cleaned[k] = bool(val)
        elif isinstance(val, dict):
            cleaned[k] = val
    if not cleaned:
        return False, None, ("方案里没有可识别的参数（mouse/key/hide）。"
                            "若是旧版本存出来的空方案，请在图形界面调好"
                            "参数后重新「存为方案」")
    return True, cleaned, ""


def load_profile(name, base=None):
    """
    按名字读方案。返回 (ok, cleaned, msg)。

    失败一律**大声**报错（msg 说清楚为什么）：CLI 的 --profile 靠这个
    决定是退出还是继续——静默用默认值开点比打不开严重得多（S5 验收点）。
    """
    name = (name or "").strip()
    if not name:
        return False, None, "方案名不能为空"
    try:
        p = profile_path(name, base)
        if not os.path.isfile(p):
            return False, None, "方案不存在：%s（在 %s 下没有 %s.json）" \
                                % (name, profiles_dir(base), name)
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, None, "读方案失败：%r" % (e,)
    return validate_profile_dict(data)


# --------------------------------------------------------------------------
# 宏（v27 / S2）
# --------------------------------------------------------------------------
# 宏和方案分开放，是因为体积差两个数量级：
# 一个方案几十字节，一个宏几千条事件能有上百 KB。混在同一个文件里，
# 每次改个连点间隔都要把上百 KB JSON 重写一遍，界面保存会明显卡顿。
# 方案里只存「用哪个宏」的名字，宏本体在 macros/ 下单独一份。
# 版本 2（v30）：文件里多了 steps（步骤表，可编辑的节点列表）。
# events 仍然照存 —— 它是「回放的事实来源」的兜底，也是降级读取的退路。
# 旧版本程序（v29 及以前）读 v2 文件会明确报「版本不兼容」，
# 而不是忽略 steps 后回放一份**没有用户编辑**的旧事件表（那更危险）。
MACRO_SCHEMA_VERSION = 2


def macros_dir(base=None):
    """宏目录：<设置目录>/macros，不存在就建。base 语义同 profiles_dir。"""
    d = os.path.join(base or settings_dir(), "macros")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def safe_name(name):
    """
    文件名安全化：滤掉路径分隔符与控制字符，防目录穿越。

    只滤字符是不够的 —— 名字为 "." 或 ".." 时，"..json" 会落到
    目录外一层。所以另外挡掉纯点号的名字。
    """
    safe = "".join(c for c in str(name).strip()
                   if c not in r'\/:*?"<>|' and ord(c) >= 32)
    if not safe or set(safe) <= {"."}:
        return ""
    return safe[:64]


def macro_path(name, base=None):
    safe = safe_name(name)
    if not safe:
        return ""
    return os.path.join(macros_dir(base), safe + ".json")


def list_macros(base=None):
    """列出全部宏名（不含 .json 后缀），排序。失败返回空列表。"""
    try:
        d = macros_dir(base)
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
    except Exception:
        return []


def save_macro(name, events, speed=1.0, repeat=1, base=None, steps=None):
    """
    保存一个宏。返回 (ok, msg)。

    events 会先过一遍 validate_events —— 免得把脏数据写进盘，
    下次读出来才发现回放乱点（那时已经查不到是哪一步坏了）。

    steps（v30）：可编辑的步骤表。传了就以它为准，events 缺省时由它展开，
    两样都写进文件——steps 给编辑器用，events 给「不认识 steps 的场景」兜底。
    """
    from .recorder import validate_events, MAX_EVENTS

    safe = safe_name(name)
    if not safe:
        return False, "宏名不能为空（也不能只用点号）"

    clean_steps = []
    if steps:
        from . import macro_steps as ms
        clean_steps, serr = ms.validate_steps(steps, max_steps=MAX_EVENTS)
        if serr:
            return False, serr
        if not events:
            events = ms.to_events(clean_steps)

    clean, err = validate_events(events)
    if err:
        return False, err
    if not clean:
        return False, "没有可保存的动作"
    if len(clean) > MAX_EVENTS:
        return False, "动作太多（%d 条，上限 %d）" % (len(clean), MAX_EVENTS)

    payload = {
        "schema_version": MACRO_SCHEMA_VERSION,
        "name": safe,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "speed": float(speed or 1.0),
        "repeat": max(1, int(repeat or 1)),
        "steps": clean_steps,
        "events": clean,
    }
    p = macro_path(safe, base)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return False, "写宏文件失败：%r" % (e,)
    return True, p


def load_macro(name, base=None):
    """
    读一个宏。返回 (ok, payload, msg)。

    失败要大声（和 load_profile 同一个理由）：回放一个坏宏可能
    在错误坐标上乱点，比「打不开」危险得多。
    """
    safe = safe_name(name)
    if not safe:
        return False, None, "宏名不能为空"
    p = macro_path(safe, base)
    if not p or not os.path.isfile(p):
        return False, None, "宏不存在：%s（在 %s 下没有 %s.json）" \
                            % (safe, macros_dir(base), safe)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, None, "读宏失败：%r" % (e,)
    if not isinstance(data, dict):
        return False, None, "宏文件格式不对（不是 JSON 对象）"
    v = data.get("schema_version")
    if isinstance(v, int) and v > MACRO_SCHEMA_VERSION:
        return False, None, ("宏版本不兼容（文件为 v%d，本程序支持 v%d）"
                             % (v, MACRO_SCHEMA_VERSION))

    from .recorder import validate_events
    clean, err = validate_events(data.get("events"))
    if err:
        return False, None, "宏内容有问题：%s" % err
    data["events"] = clean
    # v30：步骤表也要校验。文件可能是手改的/别的版本写的，
    # 一条越界坐标就够让回放到处乱点了。
    raw_steps = data.get("steps") or []
    if raw_steps:
        from . import macro_steps as ms
        csteps, serr = ms.validate_steps(raw_steps)
        if serr:
            return False, None, "宏步骤有问题：%s" % serr
        data["steps"] = csteps
    else:
        data["steps"] = []
    return True, data, ""


def delete_macro(name, base=None):
    """删掉一个宏。返回 (ok, msg)。"""
    safe = safe_name(name)
    if not safe:
        return False, "宏名不能为空"
    p = macro_path(safe, base)
    if not p or not os.path.isfile(p):
        return False, "宏不存在：%s" % safe
    try:
        os.remove(p)
    except Exception as e:
        return False, "删除失败：%r" % (e,)
    return True, "已删除 %s" % safe


DEFAULT_CONFIG = {
    "cps": 10,                  # 每秒点击次数
    "button": "left",           # left / right / middle
    "click_type": "single",     # single / double
    "mode": "current",          # current 跟随鼠标 / fixed 固定坐标
    "fixed_pos": [100, 100],    # 固定坐标
    "random_jitter": 0,         # 每次点击间隔的随机抖动（毫秒），模拟人手不匀
    "duration": 0,              # 持续时间（秒），0 = 不限
    "count_limit": 0,           # 点击次数上限，0 = 不限
    "hotkey_start": "F8",       # 开始/停止 热键
    "run_at_start": False,      # 启动后是否自动开始
    "stay_on_top": True,        # 窗口置顶
    "click_sound": False,       # 每次点击是否发提示音
    "plugin_state": {},         # 各插件自己的界面状态
    # v23 S4：自然完成（次数/时长用完）后的收尾动作：
    # none 不动作 / sound 提示音 / lock 锁屏 / shutdown 关机（30s 可取消）
    "done_action": "none",
}


def load():
    """读配置。任何异常都退回默认值 —— 配置坏了不该让程序起不来。"""
    path = config_path()
    data = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                # 只接受已知键，防止旧版本残留字段污染
                for key in DEFAULT_CONFIG:
                    if key in saved:
                        data[key] = saved[key]
        except Exception:
            pass
    return data


def save(data):
    """写配置。返回 (是否成功, 错误信息)。"""
    path = config_path()
    try:
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def reset():
    """恢复默认配置（不落盘）。"""
    return dict(DEFAULT_CONFIG)