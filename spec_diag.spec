# -*- mode: python ; coding: utf-8 -*-
# 注入诊断工具的打包配置（独立 workpath，避免与其它 exe 冲突）

import os   # v33：拼图标路径用
block_cipher = None

a = Analysis(
    ['diag_inject.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'core',
        'core.keyboard',
        'core.inputs',
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
    name='注入诊断工具',
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