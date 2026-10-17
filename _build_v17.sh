#!/bin/bash
# =====================================================================
# _build_v17.sh —— v17 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v17 内容（2026-10-16 主人实测报回的**第 4 个**问题）：
#   【暂停键盘区后再按目标键，会冒出莫名其妙的「已解除锁定」】
#     主人原话：「键盘就绪以后，长按 w 开始，再点暂停，再按 w 还会显示
#     已解除锁定，正常应该是暂停以后就默认解除锁定了」。
#     真因：暂停时只补了一个 keyup（松手），**锁定状态原样保留**
#     （_state 仍 "locked"、_active_key 仍指向该键）→ 用户再按 W 时被钩子
#     判成「锁定中再次按下 = 解除指令」→ 走 _notify(False) → 弹「已解除」。
#     修复两层：
#       ① core/keyboard.py::_clear_lock_state_locked() —— 暂停 = 松手 + 清空
#          锁定状态（统一清场函数，panic_release 也复用它）；
#       ② _state_frozen() —— 暂停期间状态机完全冻结，按目标键等于没按。
#          不冻结的话，「暂停中按住 W → 按恢复」会在恢复瞬间自己锁上。
#     文案：暂停通知改成「已解除锁定 · 再按 X 恢复运行」；
#           恢复通知改成「已恢复运行 · 长按 W 可重新锁定」；
#           键盘区两处界面提示同步说明「暂停会同时解除锁定」。
#
# v16 已交付内容（保留在 dist_v16，本脚本一并带上，因为每轮全量重建）：
#   【暂停按钮点了没反应】Api.toggle_pause 把模块名写成 `pausctl`（少个 e）
#     → 一按就 NameError；前端 catch(e){} 又吞了。修复：拼写 +
#     判定改用 pausectl.any_paused()（与按钮文案对称）；前端 apiFail 上屏。
#   【全局暂停热键从来没用过】core/hotkey.py 在 while 里**裸调** callback()，
#     异常穿出 → finally 注销热键 → 线程死。修复：_invoke() + set_error_logger。
#   【最大化两边空黑 / 拉伸难看】修复：宽屏**加列**（≥1500 三栏、≥2200 四栏），
#     窄窗口外观完全不变。
#   【最大化后点还原没反应】修复：_free_target() 过滤 + SW_RESTORE 清 zoomed
#     + 还原后复核兜底 + 写 launch.log。
#   【测试】verify_reported_20261016.py 35 → 50 项（新增 [5] 键盘暂停段，
#   含真驱动状态机 + 两层防护的负向对照）；regression_keyhold 场景 11
#   按新规格改写（63 → 66 项）；新增 diag_keypause_probe.py 21 项。
#
# 回归基线（.venv 串行全绿）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / verify_two_stage 144 /
#   regression_keyhold 66 / verify_toast 冒烟 /
#   verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_reported_20261016 50 / selftest 20（仅 vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言（down/up 间隔、限时停止），
#   两个套件并行会互相抢 CPU 造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v17.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v17"
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
