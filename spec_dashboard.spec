# -*- mode: python ; coding: utf-8 -*-
"""
spec_dashboard.spec —— 连点/锁定器 仪表盘（单文件 exe）
=====================================================

参考 VideoSummarizer 的做法产出**单文件** exe（dist 下就是 exe，没有 _internal 目录）。

为什么单文件（不要 COLLECT）：
    EXE 传了 datas 之后**又写 COLLECT**，等于把依赖生成两份：
    一份内嵌进 exe（onefile 启动时解压到 %TEMP%），
    一份躺在 _internal 里（几百 MB）。
    实测两者启动速度一样——exe 每次都在解压，_internal 从头到尾没被读过，
    是纯浪费。只保留 EXE、不写 COLLECT，产物就是干净的单文件。
    （这段结论抄自 VideoSummarizer.spec，是那边实测验证过的。）

界面依赖 pywebview（内嵌 Edge/WebView2）。
**硬校验**：缺了就中止构建，绝不静默产出「双击打不开」的 exe。
"""

import importlib.util
import os


# pywebview 提供内嵌窗口。它没装的话必须在这里炸掉：
# 打包「成功」但 exe 双击 import 失败，是最浪费时间的一种失败。
if not importlib.util.find_spec("webview"):
    raise SystemExit(
        "[打包中止] 缺少 pywebview，界面版会双击打不开。\n"
        "           装一下再打包：pip install pywebview\n"
        "           本机可用解释器："
        r"C:\Users\LXZ\.workbuddy\binaries\python\envs\vsum\Scripts\python.exe"
    )


HIDDEN = [
    "core",
    "core.engine",
    "core.inputs",
    "core.config",
    "core.macro_steps",               # v30 新增：宏步骤表（可编辑节点）
    "core.hotkey",
    "core.keyboard",
    # 鼠标键当热键用（替代会被占用的 F5）
    "core.button_trigger",
    "plugins",
    "plugins.normal_click",
    "plugins.drag_loop",
    "plugins.macro",
    # v23：抖器插件在 start_mouse 里是**函数内 import**
    # （from plugins.jiggler import Jiggler），静态分析抓不到；
    # 收尾动作 core/actions 也是运行时 import。都要字面量列出来。
    "plugins.jiggler",
    "core.actions",
    # v24：托盘（S1）—— dashboard import 是静态的，但保险起见列上
    "core.tray",
    # v27：宏录制（S2）—— recorder 在 _macro_recorder() 里运行时 import，
    # 回放插件在 macro_play() 里运行时 import，两处静态分析都抓不到。
    # 漏一个的症状是：源码跑得好好的，打包后点「开始录制」报 ModuleNotFoundError。
    "core.recorder",
    "plugins.macro_replay",
    # 权限预检用到
    "diag_privilege",
    "diag_target",
]

EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "notebook", "sqlite3",
    # 本机Python 没 tkinter，显式排除避免打包期去找
    "tkinter", "_tkinter",
]

a = Analysis(
    ["dashboard.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    cipher=None,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# 注意：**不要**再写 COLLECT，理由见文件顶部。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="爪Pro",
    # 程序图标：手工多档 ICO（256/128/64/48/32/24/16 各自成形，不是等比缩图）。
    # 换配色只要改这里指向的 ico 文件名。
    icon=os.path.join(SPECPATH, "assets", "PawPro-violet.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # 图形界面，不要黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)