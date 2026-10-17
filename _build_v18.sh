#!/bin/bash
# =====================================================================
# _build_v18.sh —— v18 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v18 内容（2026-10-16 主人实测报回的**第 5、6 个**问题）：
#   【① 最大化后三个区「杂乱无章，一点也不整齐」】
#     v16 只做到「加列」，三张卡各自按内容高度自然撑开 → 底部参差；
#     三张卡说明段长短不一 → 下面第一行控件也不在同一水平线；
#     再加上窄屏的 .trig{margin-top:20px} 没清掉，触发方式卡**整体低 20px**。
#     修复（实测坐标验证，不是肉眼判断）：
#       · align-items:start 去掉 → 三栏等高，底部边框齐平；
#       · 三张卡说明段 min-height:58px → 首行控件落在同一水平线；
#       · .wrap>.trig{margin-top:0} → 触发方式卡不再低 20px；
#       · 触发方式说明文字精简（5 行 → 3 行），trig-grid 固定两列；
#       · 媒体查询阈值 1500 → 1440（窗口宽 ≠ 视口宽，滚动条占 17~24px，
#         写 1500 时用户把窗口拖到 1500 反而进不了三栏）。
#     实测：1920 三栏 T298/B1007 三栏全同、说明段 58/58/58、首行基线差 0；
#           2536 四栏同上。改前是 298/298/318、44/44/51。
#     新增 _make_shot.py 的 MKSHOT_SIZE / MKSHOT_MEASURE：宽屏排版靠
#     肉眼看截图会看走眼，直接量坐标。
#   【② 绑定 G1/G2 后关掉程序重启，又变回默认中键 / F6】
#     主人问「只能靠 config 文件解决吗？有没有更好的方式？」
#     真因**不是配置格式**，是**路径**：_settings_path() 用了
#     HERE = dirname(abspath(__file__))；PyInstaller 单文件模式下它指向
#     %TEMP%\_MEIxxxxx\，而这个目录名**每次启动都不一样** → 上次写的
#     ui_settings.json 下次启动不在那儿 → _load_settings() 读到空 →
#     全部回落默认值。也就是说：CPS / 点击次数 / 阈值 / 触发键绑定
#     **一直就没存住过**（实测：%TEMP% 下全部 _MEI* 历史目录里只有一次
#     留下过 ui_settings.json）。
#     修复：_app_dir() 按 frozen 分支取 **exe 所在目录**（sys.executable），
#     目录不可写才退回 %LOCALAPPDATA%\MouseAutoClicker\；
#     启动日志加「界面设置文件」行，以后一眼看出路径对不对；
#     前端由「4 个动作点 + beforeunload」改成**改动即存**（去抖 400ms），
#     不依赖 pywebview 关窗时 beforeunload 一定跑得到。
#     新增 diag_settings_probe.py 23 项（含负向对照：旧语义下读成空）。
#
# v17 已交付内容（保留在 dist_v17，本脚本每轮全量重建会一并带上）：
#   【暂停键盘区后再按目标键，会冒出「已解除锁定」】真因：暂停时只补了
#     keyup，**锁定状态原样保留** → 再按 W 被判成解除指令。
#     修复两层：_clear_lock_state_locked() 清场 + _state_frozen() 冻结状态机。
#
# v16 已交付内容：
#   pausctl 拼写错 / 全局暂停热键从未可用 / 最大化两边空黑 / 还原目标被污染。
#
# 回归基线（.venv 串行全绿）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / verify_two_stage 144 /
#   regression_keyhold 66 / verify_toast 冒烟 /
#   verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_reported_20261016 59 / diag_keypause_probe 21 /
#   diag_settings_probe 23 / selftest 20（仅 vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言（down/up 间隔、限时停止），
#   两个套件并行会互相抢 CPU 造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v18.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v18"
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
