# -*- coding: utf-8 -*-
"""
把 ui/dashboard.html 回嵌进 dashboard.py 的 HTML 常量。

【为什么必须有这个脚本】
界面有两个副本：
  · dashboard.py 的 HTML = r'''...'''  ← **打包源头**（exe 里跑的是这份）
  · ui/dashboard.html                  ← 启动时被上面那份覆盖写的产物
只改一边，exe 里就是旧界面（2026-10-15 v12 事故）。
手工往几千行字符串里粘贴容易出错，所以用脚本做，并做三重校验：
  ① HTML 里不能出现三引号（会提前终止字符串）
  ② 不能以反斜杠结尾（r-string 的语法限制）
  ③ 回读校验：重新从 dashboard.py 里抠出来逐字节比对

用法：python _sync_html.py          # 回嵌（以 ui/dashboard.html 为准）
      python _sync_html.py --check  # 只校验，不改
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "dashboard.py")
UI = os.path.join(HERE, "ui", "dashboard.html")

MARK = 'HTML = r"""'


def main():
    check_only = "--check" in sys.argv
    html = open(UI, encoding="utf-8").read()
    src = open(PY, encoding="utf-8").read()

    if '"""' in html:
        print("!! HTML 里出现了三引号，会提前终止字符串，先改掉")
        return 1
    if html.rstrip("\n").endswith("\\"):
        print("!! HTML 以反斜杠结尾，原始字符串不合法")
        return 1

    i = src.index(MARK)
    head = src[:i + len(MARK)]
    j = src.index('"""', i + len(MARK))
    tail = src[j:]
    old = src[i + len(MARK):j]

    same = old == html
    print("当前两边%s（内嵌 %d 字节 / 磁盘 %d 字节）"
          % ("一致" if same else "**不一致**", len(old), len(html)))
    if check_only or same:
        return 0 if same else 1

    open(PY, "w", encoding="utf-8", newline="").write(head + html + tail)

    # 回读校验（硬规则：写完必须验）
    src2 = open(PY, encoding="utf-8").read()
    k = src2.index(MARK)
    m = src2.index('"""', k + len(MARK))
    back = src2[k + len(MARK):m]
    if back != html:
        print("!! 回读不一致，回嵌失败")
        return 1
    import py_compile
    py_compile.compile(PY, doraise=True)
    print("回嵌完成 + 语法校验通过：dashboard.py 内嵌 HTML == ui/dashboard.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
