#!/bin/bash
# =====================================================================
# _build_v16.sh —— v16 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v16 内容（2026-10-16 主人实测报的三个问题）：
#   ①【暂停按钮点了没反应】dashboard.py 的 Api.toggle_pause 把模块名写成
#     `pausctl`（少个 e）→ 一按就 NameError；前端 catch(e){} 又把它吞了，
#     界面零痕迹。修复：拼写 + 判定改用 pausectl.any_paused()
#     （按钮文案跟 any_paused 走，分区暂停时按钮显示「恢复运行」，
#      旧写法 is_paused() 会算出 not False = True，点「恢复」反而全冻住），
#     前端改成把错误贴到 hkNote + console。
#   ②【全局暂停热键从来没用过】同一个函数被 Pause 热键复用，而
#     core/hotkey.py 在 while 里**裸调** self.callback() —— 异常穿出循环、
#     finally 注销热键、监听线程死亡 → 按一次就永久失效。
#     修复：新增 _invoke() 统一兜住回调异常 + set_error_logger 钩子
#     （dashboard.main 里挂 _log_exc，异常进 launch.log）。
#   ③【最大化后两边空黑 / 拉伸难看】.wrap 写死 max-width:1180px。
#     修复：宽屏**加列**（≥1500px 三栏：鼠标区/键盘区/触发方式，
#     说明整行文字分两栏；≥2200px 上第四栏），窄窗口外观完全不变。
#   ④【最大化后点还原没反应】_norm_rect["valid"] 被意外消息打回 False 时，
#     最大化分支会把当前的全屏矩形当成还原目标存下来 → 尺寸不变。
#     修复：_free_target() 绝不返回盖住工作区的矩形；还原前 IsZoomed
#     → ShowWindow(SW_RESTORE) 清系统 zoomed 状态；还原后复核 + 兜底重试；
#     两个分支都写 launch.log（下次真出问题有现场）。
#     IsZoomed 补 argtypes（硬规则 2）。
#   【测试】新增 verify_reported_20261016.py 35 项；
#   verify_window 的签名断言表补上 IsZoomed（201 → 204 项）。
#
# 回归基线（.venv 串行全绿）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / verify_two_stage 144 /
#   regression_keyhold 63 / verify_toast 冒烟 /
#   verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_reported_20261016 35 / selftest 20（仅 vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言（down/up 间隔、限时停止），
#   两个套件并行会互相抢 CPU 造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v16.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v16"
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
