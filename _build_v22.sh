#!/bin/bash
# =====================================================================
# _build_v22.sh —— v22 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v22 内容（2026-10-17 参考 GitHub 同类程序优化，主人授权「参考着优化一下」）：
#   调研 6 个真实开源项目（EC-Autoclicker / WinAutoClick / yankivare-autoclicker /
#   OP Auto Clicker / AutoFire / FluAutoClicker）后确认：随机间隔、点击类型
#   （双击/三击）、固定坐标、启动倒计时是同类标配。而本项目**引擎层早就
#   实现**（core/engine.py 的 jitter_ms / fixed_pos / click_type，cli.py 也
#   早有 --jitter / --mode fixed --pos）——只有 dashboard 图形界面没接线。
#   本版本把四项能力接进界面：
#     ① 间隔浮动（±ms）：每次轮次间隔在 ±ms 内随机，节奏不机械
#     ② 双击 / 三击：动作下拉新增，每轮点 2/3 下（次数按轮次算）
#     ③ 固定坐标：跟随光标 or 锁定坐标（含「取当前」一键录入 + 越界 clamp）
#     ④ 启动倒计时：开始前数 N 秒（可暂停停表、可停止取消）
#   配套：arm_mouse/start_mouse 参数解析收敛成共享的 _parse_mouse_params
#         （以前两处各写一遍，新增参数后必然漂移）；新增 Api.cursor_pos；
#         toast 启动弹窗在抖动 > 0 时注明；launch.log 记录新参数。
#   防线：新增 diag_v22_features_probe.py（72 项，含 3 条负向对照：
#         把 _make_mouse_plugin 打回旧行为，双击必须退化成单击、固定坐标
#         必须不搬鼠标、倒计时必须失效——否则说明断言没在测东西）；
#         verify_two_stage 193→226（+33 条 v22 断言：静态接线 + FakeEngine
#         参数传递 + 缺省值全关 + 长按归一）。
#   已知限制（写进使用说明）：固定坐标按**主屏**像素计（多显示器用户在
#         副屏上手输坐标会偏；「取当前」录的是 GetCursorPos 虚屏坐标，
#         单显示器环境下两者一致）。
#
# v21 已交付内容（保留在 dist_v21）：
#   全局暂停防抖（toggle+连点净效果为零）+ clear_global 分区恢复不牵连
#   + save_settings 同步内存（修 27 字节空壳方案）。
# v20：齿轮面板默认隐藏 + 点一次一切 + 关 easy_drag 噪声。
# v19：配置导入/导出/多方案。
# v18：宽屏三栏对齐 + 设置持久化路径修复。
#
# 回归基线（.venv **串行**全绿，16 套）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / **verify_two_stage 226** /
#   regression_keyhold 66 / verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_toast 冒烟 / verify_reported_20261016 64 / diag_keypause_probe 21 /
#   diag_settings_probe 32 / verify_config_io 32 / diag_globalpause_probe 18 /
#   **diag_v22_features_probe 72**
# ⚠ 回归必须**串行**跑：有计时敏感断言，两个套件并行会互相抢 CPU
#   造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v22.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v22"
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
