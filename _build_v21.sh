#!/bin/bash
# =====================================================================
# _build_v21.sh —— v21 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v21 内容（2026-10-17 深夜，主人实测报回两条 bug）：
#   【bug 1 · 全局暂停按钮「无论如何都不会恢复」+「G1 恢复牵连鼠标区」】
#     主人原话：「当鼠标区和键盘区都暂停的时候，点那个全局恢复运行/暂停
#     按钮无论如何都不会恢复，但这个时候按绑了键盘锁定的键（G1）就会
#     全局恢复，已暂停的鼠标区也会恢复」。
#     两个真因（launch.log 时间线 + diag_globalpause_probe 18 项钉死）：
#       ① 「点了没反应」= toggle 语义 + 连点/双击 = 切两次 = 净效果为零
#          （后端单次点击本就正确恢复）。防线：前端 togglePause 加
#          600ms 防抖守卫（_pauseBtnTs），第二次点击直接忽略。
#       ② 「G1 牵连鼠标区」= set_zone_paused 恢复某区时若全局暂停还开着，
#          旧代码调 set_paused(False)（zone=None）把**两个区的标志一起
#          清掉**。修复：新增 pausectl.clear_global()，只清全局位，
#          别的区暂停与否原样保留。
#     附带：全局暂停/恢复路径补 launch.log（这条路径以前一行日志都不写，
#           主人报「点了没反应」时日志里什么都看不到，只能靠猜）。
#   【bug 2 · v19 存的「默认原神」方案加载报「没有可识别的参数」】
#     真因：save_settings 只写盘、不更新 self._settings，而
#     _export_dict()（导出/剪贴板/存方案）读的是**内存** —— 全新启动
#     （还没有 ui_settings.json）的用户「调好参数 → 存为方案」存出来的是
#     {"schema_version": 1} 27 字节空壳，下次加载自然报没有可识别参数。
#     磁盘写成功了内存却是空的，两边不一致才是本质。修复：save_settings
#     顺手把 mouse/key/trigger/window/hide 合并进 self._settings。
#   防线：verify_two_stage 170→193（+23 条 v21 断言，含负向对照）；
#         新增 diag_globalpause_probe.py（18 项）。
#
# v20 已交付内容（保留在 dist_v20，本脚本每轮全量重建会一并带上）：
#   齿轮面板默认隐藏 + 点一次一切（CSS display:none + .open + classList）；
#   关 pywebview easy_drag 拖动噪声。
#
# v19 已交付内容（保留在 dist_v19）：
#   配置导入/导出/多方案（齿轮面板 + 自动 .bak + schema 校验 + 白名单迁移）。
#
# v18 已交付内容（保留在 dist_v18）：
#   宽屏三栏对齐 + 设置持久化路径修复（_app_dir 按 frozen 取 exe 目录）。
#
# 回归基线（.venv **串行**全绿，15 套）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / **verify_two_stage 193** /
#   regression_keyhold 66 / verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_toast 冒烟 / verify_reported_20261016 64 / diag_keypause_probe 21 /
#   diag_settings_probe 32 / verify_config_io 32 / **diag_globalpause_probe 18**
# ⚠ 回归必须**串行**跑：有计时敏感断言，两个套件并行会互相抢 CPU
#   造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v21.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v21"
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
