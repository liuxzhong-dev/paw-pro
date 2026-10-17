# -*- coding: utf-8 -*-
"""
v12 界面截图脚本（无头 Edge + mock pywebview 桥接）。

【为什么需要它】
交付给用户的《使用说明》里要放界面截图，但界面跑在 pywebview 里，
没有浏览器能直接开。本脚本给 ui/dashboard.html 注入一个假的
window.pywebview.api（返回和后端 Api 同结构的数据），再手动派发
pywebviewready 事件，最后用 Edge headless 截一张 1180x820 的图
（和后端 webview.create_window 的尺寸一致）。

【mock 数据怎么定】
不是随手编的：热键降级文案（"F5 被其他软件占用，已改用 Ctrl + F5"）
按 dashboard.py _try_hotkeys 的真实格式写；两区都置「已就绪」
（两段式的参数已装填态），顺带把 v12 新加的界面元素全部点亮：
  1. hkNote 的 ⚠ 降级提示（note_mouse / note_key）
  2. hintMouseKey / hintKeyKey 显示真键（Ctrl + F5 / Ctrl + F6）
  3. KPI 的「已就绪」青色态（.n.armed）
  4. 触发方式区显示「鼠标中键」（默认绑定，鼠标键优先于系统热键）
  5. 自检条显示「原始输入（正常）」

【用法】
  python _make_shot.py            # 默认输出到 交付/图片/仪表盘界面.png
  python _make_shot.py out.png    # 指定输出路径
  MKSHOT_SIZE=1920x1080 python _make_shot.py o.png  # 覆盖窗口尺寸
      （核验宽屏排版 ≥1440 三栏 / ≥2200 四栏必须用，默认 1180 是窄屏）
  MKSHOT_CLICK='#cfgBtn' python _make_shot.py o.png  # 渲染完后点击指定（齿轮按钮的真实 id 是 cfgBtn）
      选择器再截（用来截「点出来」的面板，如齿轮的配置导入/导出面板）
  MKSHOT_JS='$("mMode").value="multi"; syncMouseRows();' python _make_shot.py o.png
      渲染完后执行一段 JS 再截（v26 新增）。用来把界面切到特定状态：
      headless 点 <select> 不会弹出选项、改不了值，只能设 value 再派发
      change 让联动函数跑一遍，否则截到的还是初始态。
  MKSHOT_VERIFY=1 python _make_shot.py v.png   # 核验模式：把动态元素的
      实际文本抄进页面顶部横幅再截，用来确认 JS 真的更新了
      hintMouseKey / hintKeyKey / hkNote（Edge --screenshot 只截
      滚动位置 0，首屏下方的元素看不见，只能出此下策）
  MKSHOT_SELFTEST=1 python _make_shot.py s.png  # 功能自检：真点按钮验证
      「全局暂停按钮防抖」（双击只算一次点击，v21 修复），结果写进横幅

【一致性保证】
界面 HTML 的链路是：ui/dashboard.html（可编辑源）-> 回嵌进
dashboard.py 的 HTML 字符串（打包源头）-> exe 启动时写出到
ui/dashboard.html 再加载。改 UI 必须两边同步（verify_two_stage.py
的 test_embedded_html_in_sync 守着这件事，2026-10-15 v12 曾因
只改一边导致 exe 里没有降级提示）。当前两边 md5 一致，
截源码 == 截 exe 界面。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_HTML = os.path.join(HERE, "ui", "dashboard.html")
DEFAULT_OUT = os.path.join(HERE, "..", "mouse-autoclicker", "交付", "图片",
                           "仪表盘界面.png")
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "交付", "图片",
                                           "仪表盘界面.png"))

WIN_W, WIN_H = 1180, 820          # 与 dashboard.py webview.create_window 一致
# MKSHOT_SIZE=1920x1080 覆盖窗口尺寸。
# 核验宽屏排版（≥1500 三栏 / ≥2200 四栏）必须用它 —— 默认 1180
# 落在窄屏分支，截出来的永远是单列，看不见宽屏的问题。
_sz = os.environ.get("MKSHOT_SIZE", "")
if "x" in _sz:
    try:
        WIN_W, WIN_H = [int(v) for v in _sz.lower().split("x", 1)]
    except Exception:
        pass

# ---------------------------------------------------------------- mock 数据
# 与后端 Api 各方法返回结构逐一对应（dashboard.py）：
#   bootstrap / status / hotkey_text / button_hotkeys / raw_diag / is_maximized
BOOTSTRAP = {
    "buttons": [
        {"id": "0", "name": "左键"},
        {"id": "1", "name": "右键"},
        {"id": "2", "name": "中键"},
        {"id": "3", "name": "侧键1（后退）"},
        {"id": "4", "name": "侧键2（前进）"},
    ],
    "custom": [],
    "panic": "F12",
    "hotkey": "f8",
    "pause_key": "scrolllock",
    "hotkey_ok": True,
    "hotkey_err": "",
    # 默认参数：左键 / 连点 / 无限 / 10 CPS；键盘 W / 2 秒阈值 / 30Hz
    "settings": {
        "mouse": {"button": "0", "action": "click", "count": "0",
                  "duration": "0", "cps": "10", "hide": True},
        "key": {"keys": "w", "threshold": "2", "hz": "30",
                "hold": "true", "gap": "20"},
        "trigger": {},
    },
    "trigger": {},
}

# 两段式「已就绪」态：参数装好了、等触发键，两边都没在运行
STATUS = {
    "mouse_running": False,
    "key_running": False,
    "paused": False,
    "mouse_paused": False,
    "key_paused": False,
    "mouse_info": "就绪",
    "key_info": "就绪",
    "mouse_msg": "已就绪：按 F8 开始连点…",
    "key_msg": "已就绪：按 F9 启动键盘锁定",
    "message": "",
    "mouse_armed": True,
    "key_armed": True,
    "elev_warn": False,
    "elapsed": 0.0,
    "click": 0,
    "inject": 0,
    "panic": "F12",
}

# 键名一律用真键（_try_hotkeys 降级后存的那个），不用常量；
# note 文案格式与后端 _hotkey_notes 完全一致
HOTKEY_TEXT = {
    "mouse": "f8",
    "key": "f9",
    "mouse_pretty": "F8",
    "key_pretty": "F9",
    "note_mouse": "F5 被其他软件占用，已改用 F8",
    "note_key": "F6 被其他软件占用，已改用 F9",
    "note": "F5 被其他软件占用，已改用 F8",
    # v34：宏启停键（触发方式区第 4 格 + 宏区提示行都靠它）
    "macro": "f7",
    "macro_pretty": "F7",
    "macro_trigger": "F7",
    "note_macro": "",
}

# 默认把鼠标中键绑成连点触发键（button_trigger.HUB.list_by_action 结构）
BUTTON_HOTKEYS = {
    "labels": {"middle": "鼠标中键"},
    "bindings": [{"name": "middle", "action": "mouse", "display": "鼠标中键"}],
    "by_action": {"mouse": {"label": "鼠标中键", "raw_id": 2,
                            "name": "middle"}},
}

RAW_DIAG = {
    "mode": "raw",
    "sink": True,
    "reg": True,
    "messages": 137,
    # v30：现在两种状态都要出现在截图里 —— last_ids 是「正按着」，
    # last_press 是「刚按过」（抬起后锁存，界面显示 N 秒前）。
    "last_ids": [3],
    "last_press": {"id": 3, "name": "G1", "age": 0.4},
    "armed": False,
    "seq": 0,
    "hint": "原始输入正常：按一下鼠标键，「消息数」应当增加。",
}

MOCK = """
<script>
window.pywebview = {api: {
  bootstrap:        async () => (%(bootstrap)s),
  status:           async () => (%(status)s),
  hotkey_text:      async () => (%(hotkey_text)s),
  button_hotkeys:   async () => (%(button_hotkeys)s),
  raw_diag:         async () => (%(raw_diag)s),
  is_maximized:     async () => ({ok:true, maximized:false}),
  title_minimize:   async () => ({ok:true, minimized:true}),
  title_maximize:   async () => ({ok:true, maximized:false}),
  title_close:      async () => ({ok:true}),
  title_drag_move:  async () => ({ok:true}),
  title_drag_end:   async () => ({ok:true}),
  title_resize:     async () => ({ok:true}),
  toggle_pause:     async () => ({ok:true, paused:false}),
  save_settings:    async () => ({ok:true}),
  list_profiles:    async () => ({ok:true, profiles:["农场方案","副本速刷"]}),
  relaunch_as_admin:async () => ({ok:false, msg:"截图环境不触发提权"}),
  // 宏录制区（v27）：mock 里也要给全套方法。
  // 【为什么必须补】缺一个方法，前端 await 的就是 undefined()，
  // 状态行会直接红一片 —— v26 加 MKSHOT_JS 时踩过同类坑：
  // 截图里的报错到底是真 bug 还是 mock 缺件，事后分不清。
  // 这里给一份「已录好 42 步」的假数据，正好验证 ② 格的渲染。
  macro_status:     async () => ({ok:true, recording:false, playing:false,
                                  name:"每日签到", count:6, secs:12.4,
                                  preview:"点击 · 左键 · (789, 2444) → …",
                                  clicks:5, keys:1, overflow:"", step_ver:3,
                                  armed:false, started:true, waited:0,
                                  // v34：启停键名与暂停态，缺了前端会显示
                                  // undefined（截图里的红字分不清真假 bug）
                                  trig:"F7", paused:false}),
  macro_steps:      async () => ({ok:true, total:6, offset:0, step_ver:3, items:[
    {i:0, k:"click", name:"点击", label:"点击 · 左键 · (789, 2444)",
     x:789, y:2444, delay:1427, delay2:0, after:"next"},
    {i:1, k:"click", name:"点击", label:"点击 · 左键 · (812, 2501)",
     x:812, y:2501, delay:3081, delay2:0, after:"next"},
    {i:2, k:"key", name:"按键 W", label:"按键 W",
     vk:87, delay:2059, delay2:2400, after:"next"},
    {i:3, k:"click", name:"点击", label:"点击 · 左键 · (790, 2450)",
     x:790, y:2450, delay:4945, delay2:0, after:"next"},
    {i:4, k:"wheel", name:"滚轮 -1", label:"滚轮 -1",
     d:-1, delay:2114, delay2:0, after:"next"},
    {i:5, k:"wait", name:"等待", label:"等待",
     delay:2000, delay2:0, after:"end"}]}),
  macro_step_set:   async () => ({ok:true, msg:"已保存第 0 步"}),
  macro_step_del:   async () => ({ok:true, msg:"已删除第 0 步"}),
  macro_step_dup:   async () => ({ok:true, msg:"已复制第 0 步"}),
  macro_step_move:  async () => ({ok:true, msg:"已移动"}),
  macro_step_insert:async () => ({ok:true, msg:"已插入", index:6}),
  macro_list:       async () => ({ok:true, names:["每日签到","副本速刷"]}),
  macro_start_record: async () => ({ok:true, msg:"录制中：按 Pause 结束"}),
  macro_stop_record:  async () => ({ok:false, msg:"当前没有在录制"}),
  macro_discard:      async () => ({ok:true, msg:"已清空"}),
  macro_play:         async () => ({ok:false, msg:"还没有录好的动作"}),
  macro_stop_play:    async () => ({ok:false, msg:"当前没有在回放"}),
  macro_save:         async () => ({ok:false, msg:"还没有录好的动作可保存"}),
  macro_load:         async () => ({ok:false, msg:"宏不存在"}),
  macro_delete:       async () => ({ok:false, msg:"宏不存在"}),
}};
// pywebview 正常会在页面就绪后派发 pywebviewready；
// 无头环境没有它，页面脚本的热键/状态初始化都不会跑，这里补发。
// 放在 load 之后：页面内联脚本在解析时就注册好了监听器。
window.addEventListener("load", () => {
  setTimeout(() => window.dispatchEvent(new Event("pywebviewready")), 60);
});
// 可选滚动：触发方式区在首屏下方，核验降级提示 UI 时滚过去再截。
// 用 setTimeout 而不是立即滚：要等 bootstrap + 首轮 poll 渲染完，
// 否则滚到的地方还是旧内容（截图会骗人）。
if ("%(scroll)s") {
  setTimeout(() => {
    const el = document.querySelector("%(scroll)s");
    if (el) el.scrollIntoView({block: "start"});
  }, 2500);
}
// MKSHOT_VERIFY：核验模式。Edge --screenshot 只截滚动位置 0，
// 首屏下方的元素没法直接看，于是渲染完后把各动态元素的实际文本
// 抄进页面顶部一个固定横幅，截正常首屏就能读到值。
// （只影响临时 mock 页，交付用的截图不带这个环境变量。）
if ("%(verify)s" === "1") {
  setTimeout(() => {
    const g = (id) => { const e = document.getElementById(id);
                        return e ? (e.textContent || "").trim() : "<无元素>"; };
    const note = document.getElementById("hkNote");
    const lines = [
      "VERIFY hintMouseKey=" + g("hintMouseKey"),
      "VERIFY hintKeyKey=" + g("hintKeyKey"),
      "VERIFY hkMouse=" + g("hkMouse"),
      "VERIFY hkKey2=" + g("hkKey2"),
      "VERIFY kMouse=" + g("kMouse"),
      "VERIFY kKey=" + g("kKey"),
      "VERIFY hkNote=" + (note ? note.innerHTML.replace(/<[^>]*>/g, "") : "<无>"),
      "VERIFY hkNoteClass=" + (note ? note.className : "<无>"),
    ];
    const d = document.createElement("div");
    d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;"
      + "background:#fff;color:#c00;font:14px monospace;padding:6px 10px;"
      + "white-space:pre;border-bottom:2px solid #c00";
    d.textContent = lines.join("\\n");
    document.body.appendChild(d);
  }, 2000);
}
// MKSHOT_CLICK：渲染完后点指定选择器，用来截「点出来」的面板
// （如标题栏齿轮的配置导入/导出面板）。
// 语法：`选择器` 点一次；`选择器:N` 点 N 次（间隔 120ms）——
// 用来验证「点一次开、再点一次关」这类切换逻辑（v20 面板显隐 bug）。
if ("%(click)s") {
  setTimeout(() => {
    const el = document.querySelector("%(click)s");
    if (el) for (let i = 0; i < %(click_n)d; i++) setTimeout(() => el.click(), i * 120);
  }, 2400);
}
// MKSHOT_JS：渲染完后执行一段自定义 JS（v26 新增）。
// 用途：截图前把界面切到某个状态。headless 里 el.click() 点 <select>
// 不会弹出选项列表、改不了选中值，只能直接设 value 再派发 change
// 让联动函数（syncMouseRows）跑一遍——否则截到的还是初始态。
// ⚠ 这段用户 JS 只写 // 注释；它作为字典的值注入，不参与本模板格式化。
%(js)s
// MKSHOT_MEASURE：量布局模式。宽屏「三栏齐不齐」靠肉眼看截图判断
// 很容易看走眼（截图还会被缩放），直接把真实坐标打出来读数字。
// 底部 fixed 条不参与文档流，不会把被测元素挤动。
if ("%(measure)s" === "1") {
  setTimeout(() => {
    window.scrollTo(0, 0);
    const R = (sel) => {
      const e = document.querySelector(sel);
      if (!e) return sel + " = <无元素>";
      const r = e.getBoundingClientRect();
      return sel.padEnd(22) + " T" + Math.round(r.top)
             + " B" + Math.round(r.bottom)
             + " L" + Math.round(r.left) + " R" + Math.round(r.right);
    };
    const H = (sel) => {
      const e = document.querySelector(sel);
      return e ? Math.round(e.getBoundingClientRect().height) : -1;
    };
    const lines = [
      "MEASURE 视口 " + window.innerWidth + "x" + window.innerHeight,
      R("#cardMouse") + "  " + R("#cardKey"),
      R("#cardTrig"),
      "卡片内首行控件  " + R("#cardMouse .row") + "  " + R("#cardKey .row"),
      "首行基线差(鼠标-键盘)=" + (Math.round(
          document.querySelector("#cardMouse .row").getBoundingClientRect().top
        - document.querySelector("#cardKey .row").getBoundingClientRect().top)),
      R(".trig-grid") + "  " + R(".wrap>.desc"),
      "说明段高 hint: 鼠标=" + H("#cardMouse>.hint")
        + " 键盘=" + H("#cardKey>.hint") + " 触发=" + H("#cardTrig>.hint"),
      "卡片底差: 鼠标-键盘=" + (Math.round(
          document.querySelector("#cardMouse").getBoundingClientRect().bottom
        - document.querySelector("#cardKey").getBoundingClientRect().bottom))
        + "  触发-键盘=" + (Math.round(
          document.querySelector("#cardTrig").getBoundingClientRect().bottom
        - document.querySelector("#cardKey").getBoundingClientRect().bottom)),
    ];
    const d = document.createElement("div");
    // 置顶 + 大字：截图会被 Read 缩放，12px 的字缩完就看不清了，
    // 用矮窗口（如 1920x520）截时这层就是唯一看得清的东西。
    d.style.cssText = "position:fixed;left:0;top:0;right:0;z-index:99999;"
      + "background:#fff;color:#c00;font:700 22px monospace;padding:8px 12px;"
      + "white-space:pre;line-height:1.42;border-bottom:3px solid #c00";
    d.textContent = lines.join("\\n");
    document.body.appendChild(d);
  }, 2200);
}
// MKSHOT_SELFTEST：功能自检模式（v21 全局暂停按钮防抖）。
// 真点按钮（el.click() 走完整 onclick 链路），用**状态化** mock 顶掉
// toggle_pause / status，然后双击（间隔 120ms < 600ms 防抖窗口），
// 断言「两次点击只产生一次调用」。结果写进顶部横幅，截图即可读。
// 为什么必须状态化：页面有 500ms 轮询，静态 mock 会把按钮文案刷回旧值。
if ("%(selftest)s" === "1") {
  setTimeout(() => {
    const btn = document.getElementById("pauseBtn");
    if (!btn) return;
    const api = window.pywebview.api;
    let calls = 0, paused = false;
    api.toggle_pause = async () => {
      calls += 1;
      paused = !paused;
      return {ok: true, paused: paused,
              msg: paused ? "已暂停" : "已恢复运行"};
    };
    api.status = async () => Object.assign({}, %(status)s, {paused: paused});
    const click = () => new Promise(res => {
      btn.click();
      setTimeout(res, 120);
    });
    (async () => {
      await click(); await click();               // 第 1 组双击
      const n1 = calls;
      const label1 = (btn.textContent || "").trim();
      await new Promise(r => setTimeout(r, 800)); // 越过 600ms 防抖窗口
      await click(); await click();               // 第 2 组双击
      const n2 = calls;
      const label2 = (btn.textContent || "").trim();
      const ok = (n1 === 1 && n2 === 2
                  && label1 === "恢复运行" && label2 === "暂停");
      const lines = [
        "SELFTEST 全局暂停按钮防抖（双击只算一次）",
        "  第 1 组双击后：调用次数=" + n1 + "（期望 1）  文案=" + label1
          + "（期望 恢复运行）",
        "  第 2 组双击后：调用次数=" + n2 + "（期望 2）  文案=" + label2
          + "（期望 暂停）",
        "SELFTEST 判定 = " + (ok ? "PASS" : "FAIL"),
      ];
      const d = document.createElement("div");
      d.style.cssText = "position:fixed;left:0;top:0;right:0;z-index:99999;"
        + "background:#fff;color:" + (ok ? "#060" : "#c00")
        + ";font:700 20px monospace;padding:8px 12px;"
        + "white-space:pre;line-height:1.5;border-bottom:3px solid "
        + (ok ? "#060" : "#c00");
      d.textContent = lines.join("\\n");
      document.body.appendChild(d);
    })();
  }, 2400);
}
</script>
"""

# MKSHOT_CLICK：CSS 选择器，渲染完后点它。支持 `选择器:N` 点 N 次
# （间隔 120ms），用来验证「点一次开、再点一次关」这类切换逻辑。
_click_raw = os.environ.get("MKSHOT_CLICK", "")
if _click_raw.rsplit(":", 1)[-1].isdigit():
    _click_sel = _click_raw.rsplit(":", 1)[0]
    _click_n = int(_click_raw.rsplit(":", 1)[1])
else:
    _click_sel, _click_n = _click_raw, 1

# MKSHOT_JS：把用户 JS 包成「2 秒后执行、异常写进页面标题」的一段代码。
# 拼接（不 % 格式化）是为了原样保留 JS 里的 % 与花括号。
_js_raw = os.environ.get("MKSHOT_JS", "").strip()
if _js_raw:
    _js_snippet = ("setTimeout(() => { try { " + _js_raw
                   + " } catch (e) { document.title = "
                     "'MKSHOT_JS_ERROR: ' + e; } }, 2000);")
else:
    _js_snippet = ""

# 【2026-10-17 修复】这里原来是 SRC_HTML = SRC_HTML % {...} —— 把
# **路径字符串**拿去 % 格式化。而 Python 里「无格式占位符的字符串 %
# 字典」是**静默无操作**（不报错、原样返回），于是 MOCK 模板里的
# %(bootstrap)s / %(click)s 等占位符从未被替换，整段 mock 桥以
# 语法错误的形态注入页面 → window.pywebview 从未定义 →
# MKSHOT_CLICK / MKSHOT_VERIFY / MKSHOT_SELFTEST 全部静默失效，
# 截图只剩静态 HTML（动态元素全是最初值）。因为不报错，这个坑
# 肉眼完全看不出来。正确写法：格式化 MOCK 本身。
MOCK = MOCK % {
    "bootstrap": json.dumps(BOOTSTRAP, ensure_ascii=False),
    "status": json.dumps(STATUS, ensure_ascii=False),
    "hotkey_text": json.dumps(HOTKEY_TEXT, ensure_ascii=False),
    "button_hotkeys": json.dumps(BUTTON_HOTKEYS, ensure_ascii=False),
    "raw_diag": json.dumps(RAW_DIAG, ensure_ascii=False),
    # MKSHOT_SCROLL：CSS 选择器，注入「渲染完后滚过去」的脚本。
    # 用于核验首屏下方的触发方式区（降级提示 ⚠ / 动态键名）。
    "scroll": os.environ.get("MKSHOT_SCROLL", ""),
    # MKSHOT_CLICK：CSS 选择器，渲染完后点它。支持 `选择器:N` 点 N 次
    # （间隔 120ms），用来验证「点一次开、再点一次关」这类切换逻辑。
    "click": _click_sel,
    "click_n": _click_n,
    # MKSHOT_JS：原样注入的用户 JS，包一层 setTimeout + try 防拖崩页面。
    # 【为什么要包 try】用户 JS 写错一个字符，整段脚本会抛异常，
    # 而 Edge 无头只回一张「什么都没发生」的图 —— 看不出是 JS 挂了
    # 还是本来就没效果（v21 mock 桥静默失效的同类坑）。
    # 【为什么用字符串相加而不是 % 格式化】用户 JS 里很可能有 `%`
    # （如 width:50%），走 % 会被当成格式符炸掉；这里是拼接，原样进。
    "js": _js_snippet,
    "verify": "1" if os.environ.get("MKSHOT_VERIFY") else "0",
    "measure": "1" if os.environ.get("MKSHOT_MEASURE") else "0",
    "selftest": "1" if os.environ.get("MKSHOT_SELFTEST") else "0",
}


def find_edge():
    """找 Edge（无头截图用）。优先常见安装路径，其次 PATH。"""
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    w = shutil.which("msedge")
    if w:
        return w
    raise SystemExit("找不到 Edge（msedge.exe），无法无头截图")


def build_mock_html():
    with open(SRC_HTML, "r", encoding="utf-8") as f:
        html = f.read()
    if "<head>" not in html:
        raise SystemExit("ui/dashboard.html 里找不到 <head>，无法注入 mock")
    # 注入点必须在页面内联脚本之前——本文件的内联脚本在解析期就会
    # 给 kMode 绑 change 事件（不依赖 api），而所有 api 调用都在
    # 函数/监听器里晚一步执行，放 <head> 里最稳。
    return html.replace("<head>", "<head>" + MOCK, 1)


def main():
    out = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    os.makedirs(os.path.dirname(out), exist_ok=True)

    tmpdir = tempfile.mkdtemp(prefix="mkshot_")
    try:
        page = os.path.join(tmpdir, "dashboard_mock.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(build_mock_html())

        cmd = [
            find_edge(),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=%d,%d" % (WIN_W, WIN_H),
            # 虚拟时间预算：让 bootstrap + 几轮 poll 的异步链跑完再截
            "--virtual-time-budget=6000",
            "--screenshot=" + out,
            "file:///" + page.replace("\\", "/"),
        ]
        print("[截图命令] " + " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr)
            raise SystemExit("Edge 无头截图失败（退出码 %d）" % r.returncode)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not os.path.isfile(out) or os.path.getsize(out) < 20000:
        raise SystemExit("截图产物异常：%s（不存在或过小，可能页面没渲染出来）"
                         % out)
    print("[完成] %s（%d 字节，%dx%d）"
          % (out, os.path.getsize(out), WIN_W, WIN_H))


if __name__ == "__main__":
    main()
