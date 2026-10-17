# -*- coding: utf-8 -*-
"""
plugins/__init__.py —— 插件加载器

打包要点（已踩过的坑，别改回去）：
exe 内部**没有 .py 文件**，只有编译后的模块。所以任何
「拿文件路径去 importlib 加载」的方式在打包后必然失效——文件不存在。
必须用标准 importlib.import_module('plugins.normal_click') 按模块名导入。

这也是为什么用字面量模块名列表而不是 pkgutil 动态扫目录：
pkgutil 靠真实文件系统目录遍历，打包后同样扫不到，
结果是源码能跑、exe 里插件静默消失，而构建日志全绿。
"""

import importlib
import os
import sys

# 内置插件：字面量声明，保证 PyInstaller 静态分析能收进去（规则 4）
BUILTIN_MODULES = [
    "normal_click",   # 普通连点
    "drag_loop",      # 循环拖拽
    "macro",          # 动作宏
    "jiggler",        # 鼠标抖器（v23 S6：只挪不点，防息屏/挂机检测）
]


def load_builtin():
    """按模块名导入内置插件。返回 (成功列表, 失败信息列表)。"""
    ok, bad = [], []
    for mod_name in BUILTIN_MODULES:
        full = "plugins.%s" % mod_name
        if full in sys.modules:      # 已导入过，别重复注册
            ok.append(mod_name)
            continue
        try:
            importlib.import_module(full)
            ok.append(mod_name)
        except Exception as exc:
            bad.append("%s: %s" % (mod_name, exc))
    return ok, bad


def load_external(extra_dir=None):
    """
    加载用户自己放的外部插件（.py 文件）。

    外部插件是真实 .py 文件，用文件路径动态导入是安全的——
    它们永远在磁盘上，不会被打进 exe。
    放到 exe 同级的 plugins_ext 目录，在文件里 import 后调Registry.register() 即可生效。
    """
    import importlib.util

    from core.config import app_dir

    target = extra_dir or os.path.join(app_dir(), "plugins_ext")
    if not os.path.isdir(target):
        return [], []

    ok, bad = [], []
    for fname in sorted(os.listdir(target)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(target, fname)
        mod_name = "mac_ext_" + os.path.splitext(fname)[0]
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                bad.append("%s: 无法创建模块规格" % fname)
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            ok.append(fname)
        except Exception as exc:
            bad.append("%s: %s" % (fname, exc))
    return ok, bad


def load_all(extra_dir=None):
    """加载全部插件，返回 (成功列表, 失败信息列表)。"""
    ok1, bad1 = load_builtin()
    ok2, bad2 = load_external(extra_dir)
    return ok1 + ok2, bad1 + bad2