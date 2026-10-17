#!/bin/bash
# =====================================================================
# _build_v24.sh —— v24 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v24 内容（2026-10-17 主人「开工」S1：系统托盘）：
#   托盘图标：程序在跑就有图标（core/tray.py，纯 ctypes Win32，零新增依赖
#      —— 不引 pystray/Pillow，单文件 exe 体积不变）。
#      · 双击图标 = 显示仪表盘
#      · 右键菜单 = 显示 / 暂停·恢复 / 退出（暂停与按钮、Pause 热键同一入口）
#      · 悬浮提示显示「运行中 / 已暂停」
#   「关闭到托盘」是可选开关（默认不勾）：勾上后 X 不再退出而是 hide()
#      + 任务继续跑，首次会弹「仍在后台运行」的提示。默认不勾的理由：
#      X 的通用语义是退出，默认藏起来容易「以为关了其实还在点」。
#   实现要点（踩过的坑，全在 core/tray.py 注释里）：
#      · WNDPROC 必须 4 参数；窗口过程里必须用**回调传入的 hwnd**，
#        不能用 self._hwnd —— 创建期 WM_NCCREATE/WM_NCCALCSIZE 时它还是
#        NULL，DefWindowProc(NULL,...) 返回 0 会让 CreateWindowExW 直接失败，
#        而且 GetLastError 还是 0（毫无线索）。这条有负向对照守着。
#      · wintypes 没有 LRESULT，用 c_ssize_t（64 位不能截断，规则 2）
#      · TrackPopupMenu 前 SetForegroundWindow、后 PostMessage(WM_NULL)
#      · 关窗拦截：pywebview 的 closing 事件 return True = 取消关闭
#        验证过 winforms.py 的 on_closing 语义才敢这么写
#   防线：diag_v24_tray_probe.py 66 项（含 3 条负向对照：旧写法建不出窗口、
#         无图标对象时不许藏、菜单命令不许串线）；verify_two_stage 263→282。
#   ⚠ 打包必改：spec 的 HIDDEN 与 _verify_exe_modules 的 EXPECT_PYZ 都要加
#         core.tray（dashboard 是静态 import，列上更保险）。
#
# v23：S4 完成后动作 + S5 CLI --profile + S6 鼠标抖器（已交付，保留在 dist_v23）。
# v22 已交付内容（保留在 dist_v22）：
#   鼠标区四大进阶参数接线（点击类型单/双/三击 + 间隔浮动 ±ms +
#   固定坐标含「取当前」+ 启动倒计时 ≤600s 可停表可取消）。
# v21：全局暂停防抖 + clear_global 分区恢复 + save_settings 同步内存。
# v20：齿轮面板默认隐藏 + 点一次一切 + 关 easy_drag 噪声。
# v19：配置导入/导出/多方案。
# v18：宽屏三栏对齐 + 设置持久化路径修复。
#
# 回归基线（.venv **串行**全绿，18 套）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / **verify_two_stage 282** /
#   regression_keyhold 66 / verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_toast 冒烟 / verify_reported_20261016 64 / diag_keypause_probe 21 /
#   diag_settings_probe 32 / verify_config_io 32 / diag_globalpause_probe 18 /
#   diag_v22_features_probe 72 / diag_v23_features_probe 130 /
#   **diag_v24_tray_probe 66** / selftest 21（vsum，带 pywebview）
# ⚠ 回归必须**串行**跑：有计时敏感断言，两个套件并行会互相抢 CPU
#   造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v24.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v24"
STAMP=$(date +%s)
WP="build/r$(date +%m%d%H%M%S)"

echo "[0/4] 依赖硬校验（缺 pywebview 直接中止，不静默产出打不开的 exe）"
"$PY" -c "import webview" || { echo "[错误] 缺少 pywebview"; exit 1; }
echo "  pywebview OK"

echo "[1/4] 开始打包 7 个目标 -> $DIST （不删旧目录、不加 --clean、workpath 按轮独立）"
"$PY" -m PyInstaller spec_dashboard.spec    --noconfirm --workpath "$WP/dash"    --distpath "$DIST"
"$PY" -m PyInstaller spec_keyhold_gui.spec  --noconfirm --workpath "$WP/gui"     --distpath "$DIST"
"$PY" -m PyInstaller spec_keyhold.spec      --noconfirm --workpath "$WP/keyhold" --distpath "$DIST"
"$PY" -m PyInstaller spec_cli.spec          --noconfirm --workpath "$WP/cli"     --distpath "$DIST"
"$PY" -m PyInstaller spec_diagwin.spec      --noconfirm --workpath "$WP/dw"      --distpath "$DIST"
"$PY" -m PyInstaller spec_diagtarget.spec   --noconfirm --workpath "$WP/dt"      --distpath "$DIST"
"$PY" -m PyInstaller spec_diag.spec         --noconfirm --workpath "$WP/di"      --distpath "$DIST"

echo "[2/4] 附带说明文档"
cp -f "交付/使用说明.md" "$DIST/"

echo "[3/4] 校验 7 个产物都是本轮新生成（时间戳 >= 本轮开始）"
FAIL=0
EXPECT="连点锁定器.exe 长按锁定器-图形版.exe 鼠标连点器-长按锁定.exe 鼠标连点器.exe 注入窗口测试.exe 注入目标诊断.exe 注入诊断工具.exe"
for name in $EXPECT; do
  f="$DIST/$name"
  if [ ! -f "$f" ]; then
    echo "  [失败] 未生成: $name"; FAIL=1; continue
  fi
  MT=$(stat -c %Y "$f")
  if [ "$MT" -lt "$STAMP" ]; then
    echo "  [失败] $name 时间戳旧于本轮开始 —— 旧文件，新代码没打进去"; FAIL=1
  else
    echo "  [OK] $name  $(stat -c %y "$f" | cut -d. -f1)  $(stat -c %s "$f") 字节"
  fi
done

# 端到端校验：exe 里内嵌的 HTML 必须和 ui/dashboard.html 逐字节一致。
# 【为什么加这道】2026-10-16 v12 双源断裂事故：UI 改动只写了
# ui/dashboard.html 没回嵌 dashboard.py，上面 7 项全 OK、回归全绿、
# 截图正常，exe 里却是旧界面。时间戳只能证明「这轮打的」，
# 证明不了「打进去的是新代码」——所以直接把 exe 刨开比 md5。
if [ "$FAIL" = "0" ]; then
  echo "[3.5/4] 端到端校验 exe 内嵌 HTML == ui/dashboard.html"
  if "$PY" _verify_exe_html.py "$DIST/连点锁定器.exe"; then
    echo "  [OK] exe 内嵌 HTML 与源码一致"
  else
    echo "  [失败] exe 内嵌 HTML 与源码不一致（漏回嵌 dashboard.py？）"; FAIL=1
  fi
fi

# 模块完整性：动态 import 的模块会被 PyInstaller 静默漏掉
# （源码能跑、日志全绿、exe 里就是没有）。CArchive + PYZ 两层都查。
if [ "$FAIL" = "0" ]; then
  echo "[3.6/4] 校验 exe 内项目模块齐全（CArchive + PYZ）"
  if "$PY" _verify_exe_modules.py "$DIST/连点锁定器.exe"; then
    echo "  [OK] 项目模块无缺失"
  else
    echo "  [失败] exe 内缺项目模块（新增模块没进包？）"; FAIL=1
  fi
fi

echo "[4/4] 结果"
if [ "$FAIL" = "0" ]; then
  echo "BUILD_OK：7 个 exe 全部为本轮新生成，产物在 $DIST/"
else
  echo "BUILD_FAILED：有产物校验失败（见上），换一个从未用过的版本目录重打"
  exit 1
fi
