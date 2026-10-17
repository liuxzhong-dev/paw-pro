# -*- mode: python ; coding: utf-8 -*-
# 自带窗口注入测试的打包配置

import os   # v33：拼图标路径用
block_cipher = None

a = Analysis(
    ['diag_window.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'core',
        'core.keyboard',
        'core.inputs',
        'diag_target',
        'diag_privilege',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='注入窗口测试',
    icon=os.path.join(SPECPATH, "assets", "PawPro-violet.ico"),   # v33：统一程序图标
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)