#!/bin/bash
# =====================================================================
# _build_v12.sh —— v12 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。两条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
# 所以每轮打到一个全新的 dist_vN，并在最后校验时间戳确认是本轮新生成。
#
# v12 内容（修 2026-10-15 主人实测的 3 个 bug + 1 个显示断裂）：
#   bug1 中键未就绪直接连点：
#     - toggle_mouse 补 armed 检查（未就绪按触发键不启动+提示，
#       与 toggle_keyboard 对称；toggle_keyboard 原本「未就绪也启动」
#       的注释一并改掉，两区行为统一）
#     - arm_mouse/start_mouse 入口做 button id->token 归一化
#       （UI 传 "0"~"4"，旧代码直达 _xbutton_number 抛
#       「Windows 只支持两个侧键」——选左键得侧键错误）
#   bug2 键盘区完全无效果：
#     - 实测本机 F5/F6/Ctrl+F5/Ctrl+F6/Pause 全被其他软件占用
#       （RegisterHotKey 1409），程序自动降级 F8/F9/ScrollLock，
#       但所有就绪文案写死常量 F5/F6 → 用户按提示的键没反应
#     - 新增 _hotkey_label()：文案一律取实际注册的真键
#     - _hotkey_note 拆成 mouse/key 两条，前端 hkNote 显示
#       「⚠ F6 被其他软件占用，已改用 F9」+ 日志记降级
#     - start_keyboard 失败路径补日志/toast/状态条（旧代码静默 return）
#   bug3 原神内全无效（统计断裂+待用户实测）：
#     - DynClick.run 补 ctx.clicked 累加（旧代码只加 tick_count，
#       而所有「注入了 N 次」显示读 clicked → 永远显示 0 次，
#       用户和排查都被「0 注入」带偏）
#     - 使用说明新增「游戏内无反应」排查路径（记事本验证注入链）
#
# v12.1（同日补，双源断裂事故修复）：
#   - 第一轮 v12 打包漏了把 UI 改动回嵌 dashboard.py 的 HTML 常量
#     （只改了 ui/dashboard.html 这个「启动时会被覆盖写的运行时产物」），
#     7 exe 时间戳全 OK、回归全绿、截图正常，exe 里却是旧界面
#   - 已整体回嵌 + verify_two_stage.py 新增双源一致性测试 +
#     本脚本新增 [3.5/4] 端到端校验（刨开 exe 抽内嵌 HTML 比 md5）
#   - workpath 改为按轮独立：PyInstaller 跨轮复用 workpath 时
#     「源码没变的目标」会被判定 up-to-date 跳过重写，时间戳校验误报
#
# 运行：bash _build_v12.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v12"
STAMP=$(date +%s)
# 按轮次独立的 workpath：PyInstaller 的 Analysis 缓存在 workpath 里，
# 复用旧 workpath 时「源码没变的目标会被判定 up-to-date 直接跳过重写」，
# 产物还是上一轮的（时间戳旧）——2026-10-16 重打就踩到：
# 4 个主 exe 因 dashboard.py 变了而重建，3 个诊断工具源码没变被跳过，
# 时间戳校验误报「旧文件，新代码没打进去」。加轮次后缀强制全量重打。
# （不删 build/ 旧目录——本机批量删除保护会拦；每个才 ~15MB，攒着无妨。）
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
# 【为什么加这道】2026-10-16 v12：UI 改动只写 ui/dashboard.html 没回嵌
# dashboard.py，上面 7 项全 OK、回归全绿、截图正常，exe 里却是旧界面。
# 时间戳只能证明「这轮打的」，证明不了「打进去的是新代码」——
# 所以直接把 exe 刨开，抽出内嵌 HTML 比 md5。
if [ "$FAIL" = "0" ]; then
  echo "[3.5/4] 端到端校验 exe 内嵌 HTML == ui/dashboard.html"
  if "$PY" _verify_exe_html.py "$DIST/连点锁定器.exe"; then
    echo "  [OK] exe 内嵌 HTML 与源码一致"
  else
    echo "  [失败] exe 内嵌 HTML 与源码不一致（漏回嵌 dashboard.py？）"; FAIL=1
  fi
fi

echo "[4/4] 结果"
if [ "$FAIL" = "0" ]; then
  echo "BUILD_OK：7 个 exe 全部为本轮新生成，产物在 $DIST/"
else
  echo "BUILD_FAILED：有产物校验失败（见上），换一个从未用过的版本目录重打"
  exit 1
fi
