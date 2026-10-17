#!/bin/bash
# =====================================================================
# _build_v19.sh —— v19 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v19 内容（2026-10-16，主人提需求：配置能导出/导入 + 多套方案）：
#   调研 GitHub 同类工具（MouseClicker/AutoClicker/ClickMate/
#   Gaussian-Universal-Auto-Clicker/AutoClicker Pro）+ SRE 配置管理
#   最佳实践，识别出朴素「导出/导入文件」方案的 3 个缺口，给出成熟方案：
#     · 自动备份：导入前把当前 ui_settings.json 复制成 .bak，导入翻车可回滚；
#     · 版本校验：导出带 schema_version，高版本配置拒绝导入，防格式不兼容；
#     · 白名单过滤：只迁移 mouse/key/hide（通用参数），trigger（设备相关
#       raw_id，跨机无意义）和 window（历史死键）不跨机迁移；
#     · 剪贴板通道：复制/粘贴配置 JSON，方便社群分享，不走 prompt（pywebview
#       不实现 window.prompt）；
#     · 多套方案（profiles/ 目录）：存为命名方案、下拉切换、删除。
#   标题栏新增齿轮按钮 cfgBtn，点开 cfgPanel 面板，三段：导出/分享、
#   导入、配置方案。导入/加载成功后回拉全量 settings 回填表单（trigger
#   保持本机绑定）。
#   新增 verify_config_io.py（32 项，文件系统重定向到临时目录、零注入、
#   零真实剪贴板）守住这层逻辑；verify_two_stage 增 22 条 v19 UI 标记断言。
#
# v18 已交付内容（保留在 dist_v18，本脚本每轮全量重建会一并带上）：
#   【① 最大化后三个区「杂乱无章」】三栏等高 + 说明段 min-height +
#     清掉窄屏 .trig margin-top；媒体查询阈值 1500→1440（滚动条占位）。
#   【② 绑定 G1/G2 重启变回默认】_app_dir 按 frozen 取 exe 目录，目录
#     不可写才退 %LOCALAPPDATA%；启动日志加「界面设置文件」行；前端
#     改动即存（去抖 400ms）。
#
# v17 已交付内容：
#   【暂停键盘区后再按目标键冒「已解除锁定」】_clear_lock_state_locked
#     清场 + _state_frozen 冻结状态机。
#
# 回归基线（.venv 串行全绿）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / verify_two_stage 166 /
#   verify_config_io 32 / regression_keyhold 66 / verify_toast 冒烟 /
#   verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_reported_20261016 59 / diag_keypause_probe 21 /
#   diag_settings_probe 23 / selftest 20（仅 vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言（down/up 间隔、限时停止），
#   两个套件并行会互相抢 CPU 造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v19.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v19"
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
