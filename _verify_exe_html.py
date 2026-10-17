# -*- coding: utf-8 -*-
"""
_verify_exe_html.py —— 从打好的 exe 里把内嵌 HTML 抠出来，和源码逐字节比对。

【为什么要有这个脚本】
2026-10-16 v12 事故：UI 改动只写了 ui/dashboard.html，没回嵌
dashboard.py 的 HTML 常量 —— 回归全绿、截图正常，exe 里却是旧界面。
截图截的是源文件，证明不了「exe 里打包的是哪份」。
本脚本补上最后一环：**直接开 exe 的档案，抽出 dashboard 模块，
反序列化出 HTML 字符串，和 ui/dashboard.html 比 md5。**

链路（三跳，每跳都有人看丢过）：
  ui/dashboard.html --(回嵌)--> dashboard.py 的 HTML 常量
      --(PyInstaller 打包)--> exe 内嵌字符串
  第一跳由 verify_two_stage.py::test_embedded_html_in_sync 守着；
  第二跳由本脚本守着（构建脚本 _build_v12.sh 会自动调）。

【用法】
  python _verify_exe_html.py [exe路径]
  不带参数默认校验 dist_v31/爪Pro.exe。
  用带 PyInstaller 的解释器跑（vsum 环境）。
  退出码 0 = 一致；1 = 不一致或抽不出。
"""
import hashlib
import io
import marshal
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXE = os.path.join(HERE, "dist_v31", "爪Pro.exe")
SRC_HTML = os.path.join(HERE, "ui", "dashboard.html")

# v12 UI 的关键标记：缺任何一个都说明 exe 里是旧界面
MARKERS = [
    'id="hintMouseKey"',      # 鼠标卡片提示的动态键名元素
    'id="hintKeyKey"',        # 键盘卡片提示的动态键名元素
    "note_mouse",             # 降级说明（鼠标）读取
    "note_key",               # 降级说明（键盘）读取
    "hint tiny warn",         # 降级提示的警告样式类
    "⚠ ",                     # 降级提示前缀
    "h.mouse_pretty",         # 用真键更新卡片提示
]


def extract_html_from_exe(exe_path):
    """从 PyInstaller onefile exe 里抽出 dashboard 模块的最长字符串常量。"""
    from PyInstaller.archive.readers import CArchiveReader

    arch = CArchiveReader(exe_path)
    # 项目自带模块（dashboard/cli/core/plugins）在 CArchive 里是
    # 's'（脚本）/ 'm'（模块）条目，不在 PYZ——PYZ 只装标准库和第三方包
    pyc = arch.extract("dashboard")
    code = marshal.loads(pyc)      # 脚本条目是裸 marshal，没有 pyc 头
    best = ""

    def walk(c):
        nonlocal best
        for k in c.co_consts:
            if isinstance(k, str) and len(k) > len(best):
                best = k
            elif hasattr(k, "co_consts"):
                walk(k)

    walk(code)
    return best


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE
    if not os.path.isfile(exe):
        print("[失败] exe 不存在: %s" % exe)
        return 1
    if not os.path.isfile(SRC_HTML):
        print("[失败] 源 HTML 不存在: %s" % SRC_HTML)
        return 1

    try:
        emb = extract_html_from_exe(exe)
    except Exception as e:
        print("[失败] 抽不出 exe 内嵌 HTML：%r" % e)
        return 1

    src = open(SRC_HTML, encoding="utf-8").read()
    h_exe = hashlib.md5(emb.strip().encode("utf-8")).hexdigest()
    h_src = hashlib.md5(src.strip().encode("utf-8")).hexdigest()

    print("exe 内嵌 HTML: %d 字节  md5=%s" % (len(emb), h_exe))
    print("源 ui/dashboard.html: %d 字节  md5=%s" % (len(src), h_src))

    if h_exe != h_src:
        print("[失败] exe 内嵌 HTML 与源文件不一致——打包进去的是旧界面！"
              "检查是否漏回嵌 dashboard.py 的 HTML 常量")
        return 1

    missing = [m for m in MARKERS if m not in emb]
    if missing:
        print("[失败] 标记缺失：%s" % "、".join(missing))
        return 1

    print("[通过] exe 内嵌 HTML 与 ui/dashboard.html 逐字节一致，"
          "v12 UI 标记 %d/%d 齐全" % (len(MARKERS), len(MARKERS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
