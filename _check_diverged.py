# 开工前检查：先看 UI 双源，再看 Git 改动清单。
import hashlib, os, subprocess, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(ROOT, "dashboard.py"), encoding="utf-8").read()
ui = open(os.path.join(ROOT, "ui", "dashboard.html"), encoding="utf-8").read()
mark = 'HTML = r"""'; start = src.index(mark) + len(mark); embedded = src[start:src.index('"""', start)]
same = embedded == ui; md5 = lambda s: hashlib.md5(s.encode()).hexdigest()[:8]
print("UI双源: %s | 内嵌=%s 磁盘=%s" % ("一致" if same else "已分叉", md5(embedded), md5(ui)))
print("Git改动:\n" + ((subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True).stdout or "（无）") if os.path.isdir(os.path.join(ROOT, ".git")) else "尚未初始化"))
sys.exit(0 if same else 1)
