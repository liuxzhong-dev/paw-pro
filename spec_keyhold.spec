# -*- coding: utf-8 -*-
"""
spec_keyhold.spec —— 长按锁定版（鼠标连点器-长按锁定.exe）

与 spec_cli.spec 分开构建、独立 workpath（规则 1）。
"""
import os   # v33：拼图标路径用

HIDDEN = [
    "core",
    "core.engine",
    "core.inputs",
    "core.config",
    "core.hotkey",
    "core.keyboard",
    "plugins",
    "plugins.normal_click",
    "plugins.drag_loop",
    "plugins.macro",
    # 权限预检用到的模块（keyhold.py 启动时会 import）
    "diag_privilege",
    "diag_target",
]

EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "notebook", "sqlite3",
]

a = Analysis(
    ["keyhold.py"],
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
    name="鼠标连点器-长按锁定",
    icon=os.path.join(SPECPATH, "assets", "PawPro-violet.ico"),   # v33：统一程序图标
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)