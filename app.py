# -*- coding: utf-8 -*-
"""
app.py —— 界面主程序（tkinter，标准库自带，打包不用额外依赖）

界面结构：
  顶部  模式选择（插件页签）
  中部  通用设置（CPS / 时长 / 次数上限 / 固定坐标）
  动态  插件自己的表单（按插件 FIELD 声明自动生成）
  底部  开始/停止 + 状态显示 + 热键提示

可扩展点：新增功能只要写一个 plugins/xxx.py 继承 Plugin，
界面会自动多出对应页签和表单项，这里一行都不用改。
"""

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

# 把项目根目录塞进 sys.path，保证 core / plugins 能被找到（打包后同理）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import config as cfg
from core import inputs
from core.engine import ClickerEngine, Registry
from core.hotkey import HotkeyListener

COLOR_BG = "#f5f6f8"
COLOR_CARD = "#ffffff"
COLOR_FG = "#1f2328"
COLOR_MUTED = "#6b7280"
COLOR_ACCENT = "#2563eb"
COLOR_DANGER = "#dc2626"
COLOR_OK = "#16a34a"

# 窗口固定宽高（不让用户拖，布局稳定）
WIN_W, WIN_H = 460, 660


class AutoClickerApp(object):
    def __init__(self):
        self.cfg = cfg.load()
        self.engine = None
        self.hotkey_listener = None
        self.plugin_vars = {}      # 插件表单变量：key -> tk.Variable
        self.plugin_frames = {}    # 插件表单容器：插件名 -> Frame
        self.state_lock = threading.Lock()

        self._build_window()
        self._build_ui()
        self._load_plugins()
        self._apply_config()
        self._start_hotkey()
        self._tick_status()
        self._set_running(False)

        if self.cfg.get("run_at_start"):
            self.root.after(600, self.start_clicking)

    # ------------------------------------------------------------------
    # 窗口
    # ------------------------------------------------------------------
    def _build_window(self):
        self.root = tk.Tk()
        self.root.title("鼠标连点器")
        self.root.configure(bg=COLOR_BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", bool(self.cfg.get("stay_on_top", True)))

        # 居中显示
        sw, sh = inputs.screen_size()
        x = max(0, (sw - WIN_W) // 2)
        y = max(0, (sh - WIN_H) // 2)
        self.root.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, x, y))

    def _card(self, parent, title=""):
        """造一个带标题的白色卡片，返回内部 Frame。"""
        outer = tk.Frame(parent, bg=COLOR_CARD, bd=0, highlightthickness=1,
                         highlightbackground="#e5e7eb")
        inner = tk.Frame(outer, bg=COLOR_CARD)
        inner.pack(fill="both", expand=True, padx=14, pady=12)
        if title:
            tk.Label(inner, text=title, bg=COLOR_CARD, fg=COLOR_FG,
                     font=("Microsoft YaHei UI", 10, "bold"), anchor="w").pack(
                fill="x", pady=(0, 8))
        return inner

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self):
        # ---------- 模式选择 ----------
        card_mode = self._card(self.root, "模式")
        self.plugin_var = tk.StringVar()
        row_mode = tk.Frame(card_mode, bg=COLOR_CARD)
        row_mode.pack(fill="x")
        for name in Registry.names():
            p = Registry.get(name)
            ttk.Radiobutton(
                row_mode, text=p.title, value=name, variable=self.plugin_var,
                command=self._on_plugin_switch, takefocus=False,
            ).pack(side="left", padx=(0, 14))
        self.desc_var = tk.StringVar()
        tk.Label(card_mode, textvariable=self.desc_var, bg=COLOR_CARD,
                 fg=COLOR_MUTED, font=("Microsoft YaHei UI", 8),
                 wraplength=390, justify="left", anchor="w").pack(fill="x", pady=(6, 0))

        # ---------- 通用设置 ----------
        card_gen = self._card(self.root, "通用设置")
        self._build_general_form(card_gen)

        # ---------- 插件表单区（动态生成） ----------
        self.plugin_area = tk.Frame(self.root, bg=COLOR_BG)
        self.plugin_area.pack(fill="both", expand=True, padx=0)

        # ---------- 底部操作 ----------
        card_act = self._card(self.root, "")
        self._build_action_bar(card_act)

    def _build_general_form(self, parent):
        pad = {"padx": 4, "pady": 3}

        # CPS
        row1 = tk.Frame(parent, bg=COLOR_CARD)
        row1.pack(fill="x")
        tk.Label(row1, text="每秒点击(CPS)", bg=COLOR_CARD, fg=COLOR_FG,
                 font=("Microsoft YaHei UI", 9), width=12, anchor="w").pack(**pad)
        self.cps_var = tk.IntVar(value=10)
        ttk.Spinbox(row1, from_=1, to=100, textvariable=self.cps_var, width=8).pack(**pad)
        tk.Label(row1, text="次", bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Microsoft YaHei UI", 8)).pack()

        # 随机抖动
        row2 = tk.Frame(parent, bg=COLOR_CARD)
        row2.pack(fill="x")
        tk.Label(row2, text="随机抖动", bg=COLOR_CARD, fg=COLOR_FG,
                 font=("Microsoft YaHei UI", 9), width=12, anchor="w").pack(**pad)
        self.jitter_var = tk.IntVar(value=0)
        ttk.Spinbox(row2, from_=0, to=1000, textvariable=self.jitter_var, width=8).pack(**pad)
        tk.Label(row2, text="毫秒（让节奏不那么机械）", bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Microsoft YaHei UI", 8)).pack()

        # 时长 + 次数上限
        row3 = tk.Frame(parent, bg=COLOR_CARD)
        row3.pack(fill="x")
        tk.Label(row3, text="限时", bg=COLOR_CARD, fg=COLOR_FG,
                 font=("Microsoft YaHei UI", 9), width=12, anchor="w").pack(**pad)
        self.duration_var = tk.IntVar(value=0)
        ttk.Spinbox(row3, from_=0, to=99999, textvariable=self.duration_var, width=8).pack(**pad)
        tk.Label(row3, text="秒 (0=不限)   次数上限", bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(**pad)
        self.count_var = tk.IntVar(value=0)
        ttk.Spinbox(row3, from_=0, to=999999, textvariable=self.count_var, width=8).pack(**pad)
        tk.Label(row3, text="次", bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Microsoft YaHei UI", 8)).pack()

        # 固定坐标
        row4 = tk.Frame(parent, bg=COLOR_CARD)
        row4.pack(fill="x")
        tk.Label(row4, text="固定坐标", bg=COLOR_CARD, fg=COLOR_FG,
                 font=("Microsoft YaHei UI", 9), width=12, anchor="w").pack(**pad)
        self.fx_var = tk.StringVar(value="0")
        self.fy_var = tk.StringVar(value="0")
        ttk.Entry(row4, textvariable=self.fx_var, width=6).pack(side="left", **pad)
        tk.Label(row4, text="," , bg=COLOR_CARD, fg=COLOR_MUTED).pack(side="left")
        ttk.Entry(row4, textvariable=self.fy_var, width=6).pack(side="left", **pad)
        ttk.Button(row4, text="取当前位置", command=self._pick_position,
                   width=11).pack(side="left", padx=(10, 0))

        # 热键 + 置顶
        row5 = tk.Frame(parent, bg=COLOR_CARD)
        row5.pack(fill="x")
        tk.Label(row5, text="全局热键", bg=COLOR_CARD, fg=COLOR_FG,
                 font=("Microsoft YaHei UI", 9), width=12, anchor="w").pack(**pad)
        self.hotkey_var = tk.StringVar(value="F8")
        ttk.Combobox(row5, textvariable=self.hotkey_var, width=8, state="normal",
                     values=["F6", "F7", "F8", "F9", "F12", "Ctrl+Q", "Alt+Q"]).pack(**pad)
        ttk.Button(row5, text="应用", command=self._restart_hotkey, width=6).pack(side="left")
        self.topmost_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row5, text="窗口置顶", variable=self.topmost_var,
                        command=self._toggle_topmost, takefocus=False).pack(side="left", padx=(10, 0))

    def _build_action_bar(self, parent):
        self.start_btn = tk.Button(parent, text="开始", bg=COLOR_OK, fg="#ffffff",
                                  activebackground="#15803d", activeforeground="#ffffff",
                                  relief="flat", bd=0, height=1,
                                  font=("Microsoft YaHei UI", 11, "bold"),
                                  command=self.toggle_clicking, cursor="hand2")
        self.start_btn.pack(fill="x", ipady=6)

        self.status_var = tk.StringVar(value="就绪")
        tk.Label(parent, textvariable=self.status_var, bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(9, 0))

    # ------------------------------------------------------------------
    # 插件加载与动态表单
    # ------------------------------------------------------------------
    def _load_plugins(self):
        import plugins as loader

        ok, bad = loader.load_all()
        if bad:
            print("[插件加载] 失败项: %s" % "; ".join(bad))
        if not Registry.names():
            messagebox.showerror("错误", "没有加载到任何插件，程序无法工作。")
            raise SystemExit(1)
        # 只留一个插件时自动选中
        if not self.plugin_var.get():
            self.plugin_var.set(Registry.names()[0])

    def _on_plugin_switch(self):
        name = self.plugin_var.get()
        plugin = Registry.get(name)
        if not plugin:
            return
        self.desc_var.set(plugin.description)

        # 先隐藏所有插件表单，再只显示当前插件的
        for pname, frame in self.plugin_frames.items():
            if pname == name:
                frame.pack(fill="both", expand=True, padx=0, pady=(6, 0))
            else:
                frame.pack_forget()

        self._save_plugin_state()   # 切走前先存好上一个插件填的值

    def _render_plugin_form(self, plugin):
        """按插件 FIELD 声明自动生成表单。新增功能不用改这里。"""
        frame = self._card(self.root, "%s设置" % plugin.title)
        self.plugin_frames[plugin.name] = frame

        saved = (self.cfg.get("plugin_state") or {}).get(plugin.name, {})

        for field in plugin.build():
            key = field["key"]
            ftype = field.get("type", "text")
            label = field.get("label", key)
            hint = field.get("hint", "")
            default = saved.get(key, field.get("default"))

            if ftype == "bool":
                var = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(frame, text=label, variable=var, takefocus=False).pack(
                    anchor="w", pady=2)
                self.plugin_vars[key] = var

            elif ftype == "choice":
                row = tk.Frame(frame, bg=COLOR_CARD)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=label, bg=COLOR_CARD, fg=COLOR_FG,
                         font=("Microsoft YaHei UI", 9), width=14, anchor="w").pack()
                var = tk.StringVar(value=default)
                for value, text in field.get("options", []):
                    ttk.Radiobutton(row, text=text, value=value, variable=var,
                                    takefocus=False).pack(side="left", padx=(0, 10))
                self.plugin_vars[key] = var

            elif ftype == "int":
                row = tk.Frame(frame, bg=COLOR_CARD)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=label, bg=COLOR_CARD, fg=COLOR_FG,
                         font=("Microsoft YaHei UI", 9), width=14, anchor="w").pack()
                var = tk.IntVar(value=int(default or 0))
                ttk.Spinbox(row, from_=field.get("min", 0), to=field.get("max", 9999),
                            textvariable=var, width=8).pack()
                self.plugin_vars[key] = var

            else:  # text
                row = tk.Frame(frame, bg=COLOR_CARD)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=label, bg=COLOR_CARD, fg=COLOR_FG,
                         font=("Microsoft YaHei UI", 9), width=14, anchor="n").pack(
                    side="left", anchor="n")
                var = tk.StringVar(value=str(default or ""))
                ttk.Entry(row, textvariable=var).pack(side="left", fill="x",
                                                      expand=True, padx=(0, 0))
                self.plugin_vars[key] = var

            if hint:
                tk.Label(frame, text=hint, bg=COLOR_CARD, fg=COLOR_MUTED,
                         font=("Microsoft YaHei UI", 8), anchor="w",
                         wraplength=390, justify="left").pack(fill="x")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _apply_config(self):
        self.cps_var.set(self.cfg.get("cps", 10))
        self.jitter_var.set(self.cfg.get("random_jitter", 0))
        self.duration_var.set(self.cfg.get("duration", 0))
        self.count_var.set(self.cfg.get("count_limit", 0))
        self.hotkey_var.set(self.cfg.get("hotkey_start", "F8"))
        self.topmost_var.set(self.cfg.get("stay_on_top", True))
        pos = self.cfg.get("fixed_pos") or [0, 0]
        self.fx_var.set(str(pos[0]))
        self.fy_var.set(str(pos[1]))

        # 逐个渲染插件表单（此时 plugin_state 需已按插件分组）
        for name in Registry.names():
            self._render_plugin_form(Registry.get(name))
        self._on_plugin_switch()

    def _collect(self):
        """把界面当前值收集成一个配置 dict。"""
        self._save_plugin_state()
        data = {
            "cps": self.cps_var.get(),
            "button": "left",
            "click_type": "single",
            "mode": "current",
            "fixed_pos": [self._safe_int(self.fx_var.get(), 0),
                          self._safe_int(self.fy_var.get(), 0)],
            "random_jitter": self.jitter_var.get(),
            "duration": self.duration_var.get(),
            "count_limit": self.count_var.get(),
            "hotkey_start": self.hotkey_var.get().strip() or "F8",
            "run_at_start": False,
            "stay_on_top": bool(self.topmost_var.get()),
            "click_sound": False,
            "plugin_state": self.cfg.get("plugin_state") or {},
        }
        return data

    def _save_plugin_state(self):
        """把插件表单当前值存进 self.cfg['plugin_state']。"""
        pname = self.plugin_var.get()
        if not pname:
            return
        bucket = (self.cfg.get("plugin_state") or {}).setdefault(pname, {})
        for key, var in self.plugin_vars.items():
            # key 形如 f_<插件名>_<字段名>
            short = key.split("_", 2)[-1] if key.startswith("f_") else key
            try:
                bucket[short] = var.get()
            except tk.TclError:
                pass
        self.cfg["plugin_state"] = self.cfg.get("plugin_state") or {}

    @staticmethod
    def _safe_int(text, fallback=0):
        try:
            return int(float(str(text)))
        except (TypeError, ValueError):
            return fallback

    def _save_config(self):
        ok, err = cfg.save(self._collect())
        return ok, err

    # ------------------------------------------------------------------
    # 热键
    # ------------------------------------------------------------------
    def _start_hotkey(self):
        text = self.hotkey_var.get().strip() or "F8"
        try:
            self.hotkey_listener = HotkeyListener(text, self.toggle_clicking)
            ok, err = self.hotkey_listener.start()
            if ok:
                self.hotkey_text = text
            else:
                self.hotkey_text = "（热键不可用）"
                print("[热键] %s" % err)
        except Exception as exc:
            self.hotkey_text = "（热键不可用）"
            print("[热键] %s" % exc)

    def _restart_hotkey(self):
        if self.hotkey_listener:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        self._start_hotkey()
        messagebox.showinfo("热键", "当前热键：%s" % self.hotkey_text)

    def _toggle_topmost(self):
        self.root.attributes("-topmost", bool(self.topmost_var.get()))

    def _pick_position(self):
        pos = inputs.get_cursor_pos()
        if not pos:
            messagebox.showwarning("提示", "读取鼠标位置失败")
            return
        self.fx_var.set(str(pos[0]))
        self.fy_var.set(str(pos[1]))
        # 顺手把该插件的「点击位置」切成固定坐标
        for key in self.plugin_vars:
            if key.endswith("_mode"):
                self.plugin_vars[key].set("fixed")

    # ------------------------------------------------------------------
    # 运行控制
    # ------------------------------------------------------------------
    def _gather_options(self):
        """按当前插件的表单值组装选项 dict，交给引擎。"""
        pname = self.plugin_var.get()
        bucket = ((self.cfg.get("plugin_state") or {}).get(pname) or {})
        return dict(bucket)

    def toggle_clicking(self):
        if self.engine and self.engine.running:
            self.stop_clicking()
        else:
            self.start_clicking()

    def start_clicking(self):
        if self.engine and self.engine.running:
            return
        pname = self.plugin_var.get()
        plugin = Registry.get(pname)
        if not plugin:
            messagebox.showerror("错误", "未选中有效模式")
            return

        data = self._collect()
        self.cfg.update(data)
        self._save_config()

        self.engine = ClickerEngine(
            plugin,
            options=self._gather_options(),
            state=(data["plugin_state"].get(pname) or {}),
            base_cps=self.cps_var.get(),
            jitter_ms=self.jitter_var.get(),
            duration=self.duration_var.get(),
            count_limit=self.count_var.get(),
            fixed_pos=data["fixed_pos"],
        )
        self.engine.on_finish = self._on_engine_finish
        if not self.engine.start():
            messagebox.showerror("错误", "启动失败")
            return
        self._set_running(True)

    def stop_clicking(self):
        if self.engine and self.engine.running:
            self.engine.stop(wait=False)
        self._set_running(False)

    def _on_engine_finish(self, ctx):
        # 引擎线程回调，必须切回主线程更新界面
        self.root.after(0, lambda: self._set_running(False))

    def _set_running(self, running):
        if running:
            self.start_btn.configure(text="停止", bg=COLOR_DANGER,
                                     activebackground="#b91c1c")
            self.status_var.set("运行中…（%s 停止）" % self.hotkey_text)
        else:
            self.start_btn.configure(text="开始", bg=COLOR_OK,
                                     activebackground="#15803d")
            if self.engine:
                self.status_var.set(
                    "已停止：点击 %d 次 / 用时 %.1f 秒" % (self.engine.ctx.clicked,
                                                    self.engine.ctx.elapsed()))

    def _tick_status(self):
        """每秒刷新一次运行中的状态。"""
        if self.engine and self.engine.running:
            ctx = self.engine.ctx
            self.status_var.set(
                "运行中 %d 次 / %.0f 秒  （%s 停止）"
                % (ctx.clicked, ctx.elapsed(), self.hotkey_text))
        self.root.after(500, self._tick_status)

    # ------------------------------------------------------------------
    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        try:
            self._save_config()
        except Exception:
            pass
        if self.engine and self.engine.running:
            self.engine.stop(wait=False)
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        self.root.destroy()


def main():
    if sys.platform != "win32":
        print("本程序仅支持 Windows。")
        return
    # DPI 感知必须在 tk.Tk() 之前，否则坐标按 96dpi 虚拟化
    try:
        from core import dpi
        dpi.enable_dpi_awareness()
    except Exception:
        pass
    app = AutoClickerApp()
    app.run()


if __name__ == "__main__":
    main()