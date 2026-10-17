# -*- coding: utf-8 -*-
"""
_verify_exe_modules.py —— 校验 exe 里必须存在的项目模块一个都没少。

【为什么要有这个脚本】
PyInstaller 的静态分析抓不到动态 import 时，模块会**静默消失**：
源码能跑、构建日志全绿、回归全绿，exe 里就是没有那个模块。
本项目踩过两次同类坑：
  · plugins 靠文件路径动态加载 → 打包后扫不到（plugins/__init__.py 注释）
  · 2026-10-16 v14：新增 core/dpi.py 后人工校验时只看了 CArchive 的
    TOC（那里只有 dashboard 一个项目模块），差点误判「没打进去」
    ——项目模块其实都在 PYZ 里。这个脚本把正确的查法固化下来。

【查法（正确姿势，别再查错层）】
  · CArchive（外层）：只有入口脚本 dashboard/cli/app/keyhold
  · PYZ（内层，CArchive 里的 'PYZ.pyz' 条目）：core.* 与 plugins.*
  两层都要查，缺一层就是漏。

【用法】
  python _verify_exe_modules.py [exe路径]
  不带参数默认校验 dist_v31/爪Pro.exe。
  用带 PyInstaller 的解释器跑（vsum 环境）。
  退出码 0 = 齐全；1 = 有缺失或读不出。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXE = os.path.join(HERE, "dist_v31", "爪Pro.exe")

# 爪Pro.exe 必须含有的项目模块（新增 core/xxx.py 就往里加一行）
EXPECT_CARCHIVE = ["dashboard"]
EXPECT_PYZ = [
    "core", "core.config", "core.inputs", "core.engine", "core.keyboard",
    "core.pausectl", "core.toast", "core.hotkey", "core.button_trigger",
    "core.dpi",                       # v14 新增：DPI 感知
    "core.actions",                   # v23 新增：收尾动作（提示音/锁屏/关机）
    "core.tray",                      # v24 新增：系统托盘（S1）
    "core.recorder",                  # v27 新增：宏录制（低级键鼠钩子）
    "core.macro_steps",               # v30 新增：宏步骤表（压缩/展开/校验）
    "plugins", "plugins.normal_click", "plugins.drag_loop", "plugins.macro",
    "plugins.jiggler",                # v23 新增：鼠标抖器
    "plugins.macro_replay",           # v27 新增：宏回放
]


def check(exe_path):
    from PyInstaller.archive.readers import CArchiveReader
    from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    import tempfile

    arch = CArchiveReader(exe_path)
    cnames = set(arch.toc.keys())
    missing = [m for m in EXPECT_CARCHIVE if m not in cnames]
    if missing:
        print("[失败] CArchive 缺模块: %s" % "、".join(missing))
        return 1

    # PYZ 要先落盘再读（ZlibArchiveReader 只吃文件名）
    raw = arch.extract("PYZ.pyz")
    tmp = os.path.join(tempfile.gettempdir(), "_verify_modules.pyz")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        z = ZlibArchiveReader(tmp)
        pnames = set(z.toc.keys())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    missing = [m for m in EXPECT_PYZ if m not in pnames]
    if missing:
        print("[失败] PYZ 缺模块: %s" % "、".join(missing))
        print("       新模块没被打包：检查是否动态 import（PyInstaller 静态分析抓不到）")
        return 1

    print("[通过] 项目模块齐全：CArchive %d 个 + PYZ %d 个"
          % (len(EXPECT_CARCHIVE), len(EXPECT_PYZ)))
    return 0


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE
    if not os.path.isfile(exe):
        print("[失败] exe 不存在: %s" % exe)
        return 1
    try:
        return check(exe)
    except Exception as e:
        print("[失败] 读取 exe 出错：%r" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
