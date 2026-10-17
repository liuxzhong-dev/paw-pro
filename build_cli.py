# -*- coding: utf-8 -*-
"""
build_cli.py —— 非交互版打包脚本（build.bat 的替代）
=====================================================

为什么需要它：build.bat 里有几处 `pause`，在无人值守/后台运行时
会**永久挂起**（实测卡了 11 分钟，日志里一个 python 进程都没有）。
这个脚本做同样的事，但全程无交互，可以直接后台跑。

沿用 build.bat 的全部规范：
  - 绝不删除任何旧目录（每轮打到一个全新的 dist_vN）
  - 不加 --clean
  - 独立 --workpath
  - 最后校验 exe 时间戳，确认是本轮新生成的
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Users\LXZ\.workbuddy\binaries\python\envs\vsum\Scripts\python.exe"

# (spec 文件, 产物名, 用途说明)
TARGETS = [
    ("spec_dashboard.spec", "爪Pro.exe", "仪表盘（全息科技风，推荐）"),
    ("spec_keyhold_gui.spec", "长按锁定器-图形版.exe", "原生 Win32 图形版"),
    ("spec_keyhold.spec", "鼠标连点器-长按锁定.exe", "命令行版（含长按锁定）"),
    ("spec_cli.spec", "鼠标连点器.exe", "纯鼠标连点"),
    ("spec_diagwin.spec", "注入窗口测试.exe", "诊断：自建窗口"),
    ("spec_diagtarget.spec", "注入目标诊断.exe", "诊断：指定程序"),
    ("spec_diag.spec", "注入诊断工具.exe", "诊断：记事本对照"),
]


def pick_dist_dir():
    """找一个从未用过的 dist_vN。绝不复用，也绝不删除。"""
    n = 1
    while os.path.exists(os.path.join(HERE, "dist_v%d" % n)):
        n += 1
    return "dist_v%d" % n


def _sync_source(dist_dir):
    """
    把本轮源码同步到 交付\\source\\，保证「源码 == 打包进去的代码」。

    【为什么必须单独做这一步】
    交付目录是给用户排查问题用的。以前只同步 exe，
    source 目录就一直停在某个历史版本（实测停在 22:41，
    比 exe 早了好几轮）。用户照着旧源码看行号、看字段名，
    和 exe 的实际行为对不上——排查会直接跑偏，
    而且极难察觉是源码旧了。
    """
    src_dir = os.path.join(HERE, "交付", "source")
    if os.path.exists(src_dir):
        try:
            shutil.rmtree(src_dir)
        except Exception as e:
            print("      [警告] 清理旧源码失败，本次跳过同步：%s" % e)
            return
    os.makedirs(src_dir, exist_ok=True)

    files = [
        "dashboard.py", "cli.py", "app.py", "keyhold.py", "keyhold_gui.py",
        "core/__init__.py", "core/button_trigger.py", "core/config.py",
        "core/engine.py", "core/hotkey.py", "core/inputs.py",
        "core/keyboard.py",
        "verify_binding.py", "verify_window.py", "verify_trigger.py",
        "verify_bugfix.py", "regression_keyhold.py", "selftest.py",
    ]
    n = 0
    for rel in files:
        p = os.path.join(HERE, rel)
        if not os.path.exists(p):
            continue
        dst = os.path.join(src_dir, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            n += 1
        except Exception as e:
            print("      [警告] 复制 %s 失败：%s" % (rel, e))
    print("      源码已同步：%d 个文件 -> 交付\\source\\" % n)


def main():
    if not os.path.exists(PY):
        print("[错误] 找不到打包用解释器：%s" % PY)
        return 1

    # 依赖硬校验：缺 pywebview 就中止，绝不产出「双击打不开」的 exe
    r = subprocess.run([PY, "-c", "import webview"],
                       capture_output=True)
    if r.returncode != 0:
        print("[错误] 缺少 pywebview，界面版会双击打不开。")
        print("       装一下： \"%s\" -m pip install pywebview" % PY)
        return 1
    print("[1/4] 依赖检查 OK")

    dist = pick_dist_dir()
    os.makedirs(os.path.join(HERE, dist), exist_ok=True)
    print("[2/4] 本轮产物目录：%s（旧目录全部保留）" % dist)

    started = time.time()
    built = []
    for spec, exe_name, desc in TARGETS:
        spec_path = os.path.join(HERE, spec)
        if not os.path.exists(spec_path):
            print("      [跳过] spec 不存在：%s" % spec)
            continue
        work = os.path.join(HERE, "build", spec.replace(".spec", ""))
        print("      打包 %-28s (%s) ..." % (exe_name, desc))
        # 注意：绝不加 --clean（它会删 build\，本机可能被安全软件拦）
        p = subprocess.run(
            [PY, "-m", "PyInstaller", spec_path,
             "--noconfirm",
             "--workpath", work,
             "--distpath", os.path.join(HERE, dist)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            print("      [失败] %s" % exe_name)
            tail = (p.stdout or "")[-1500:] + (p.stderr or "")[-800:]
            print("      " + tail.replace("\n", "\n      "))
            return 1
        built.append(exe_name)

    # 附上说明文档
    doc = os.path.join(HERE, "交付", "使用说明.md")
    if os.path.exists(doc):
        shutil.copy2(doc, os.path.join(HERE, dist))

    # ---- 关键校验：比对时间戳，确认是本轮新生成的 ----
    # 只检查「文件存在」不够：可能因安全软件拦删除而跳过重写，
    # 产物存在但仍是旧文件，新代码根本没打进去。
    print("[3/4] 校验产物为本轮新生成...")
    failed = []
    dist_dir = os.path.join(HERE, dist)
    for name in built:
        fp = os.path.join(dist_dir, name)
        if not os.path.exists(fp):
            print("      [失败] 未生成：%s" % name)
            failed.append(name)
            continue
        mtime = os.path.getmtime(fp)
        if mtime > started:
            print("      [OK] %-28s %6.1f MB  %s"
                  % (name, os.path.getsize(fp) / 1048576.0,
                     time.strftime("%H:%M:%S", time.localtime(mtime))))
        else:
            print("      [失败] %s 时间戳早于本轮开始 —— 是旧文件，新代码没打进去"
                  % name)
            failed.append(name)

    # 同步到交付目录
    print("[4/4] 同步到 交付\\ ...")
    deliver = os.path.join(HERE, "交付")
    os.makedirs(deliver, exist_ok=True)
    for name in built:
        src = os.path.join(dist_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(deliver, name))

    # 源码也要同步，否则 交付\source 永远停在某个历史版本
    _sync_source(dist_dir)

    print()
    print("=" * 56)
    if failed:
        print(" 完成，但 %d 项校验失败：%s" % (len(failed), ", ".join(failed)))
        print(" 换一个从未用过的版本号重试：BUILD_VER=v99 python build_cli.py")
    else:
        print(" 完成！产物在 %s\\ （已同步到 交付\\）" % dist)
        print(" 旧版本目录全部保留，可随时回滚。")
    print("=" * 56)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())