# -*- coding: utf-8 -*-
"""
spec_cli.spec —— 命令行版（鼠标连点器.exe）

单独一个 spec、独立 workpath，这是规则 1 的硬要求：
同一 workpath 下构建第二个 EXE 时，PyInstaller 会先整包删除上一个的
base_library.zip，在开了批量删除保护的环境里被拦下，结果只产出第一个 exe。
"""
import os   # v33：拼图标路径用

# 内置插件必须字面量列出（规则 4）：pkgutil / collect_submodules 都扫不到，
# 源码能跑、打包后插件静默消失。
HIDDEN = [
    "core",
    "core.engine",
    "core.inputs",
    "core.config",
    "core.macro_steps",               # v30 新增：宏步骤表（可编辑节点）
    "core.hotkey",
    "core.keyboard",
    # 暂停控制 + 状态弹窗：run_headless 里是运行时 import，
    # 显式列出来，免得哪天改了 import 写法就静默丢模块
    "core.pausectl",
    "core.toast",
    "plugins",
    "plugins.normal_click",
    "plugins.drag_loop",
    "plugins.macro",
    # v23：抖器（交互菜单 5 / --mode jiggler）+ 收尾动作，
    # 都是运行时 import，必须字面量列出否则打包后静默消失
    "plugins.jiggler",
    "core.actions",
    # v27：宏（S2）—— 命令行暂时没有回放入口，但 recorder 要跟着走：
    # cli 读 profiles 时可能碰到带 macro 段的方案，缺模块会抛 ImportError。
    "core.recorder",
    "plugins.macro_replay",
]

EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "notebook", "sqlite3",
]

a = Analysis(
    ["cli.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="鼠标连点器",
    icon=os.path.join(SPECPATH, "assets", "PawPro-violet.ico"),   # v33：统一程序图标
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 命令行版必须能看到输出
    disable_windowed_traceback=False,
)