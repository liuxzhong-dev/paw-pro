#!/bin/bash
# =====================================================================
# _build_v25.sh —— v25 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v25 内容（S3 多点轮询的**前置改造**：副屏/虚拟桌面坐标）：
#   v22 的固定坐标按主屏算：SendInput 绝对坐标不带 MOUSEEVENTF_VIRTUALDESK
#   时只铺主屏，项目里又把坐标 clamp 到 [0, 主屏宽)。副屏在主屏左边
#   （x 为负）时坐标被夹成 0 —— 表现为「鼠标飞到左上角」。
#   本版两处一起改（缺一不可）：
#     ① core/inputs.py：新增 virtual_screen()（GetSystemMetrics 76~79）；
#        _to_absolute 按虚拟桌面归一化（减原点、用虚屏宽高）；
#        move_to 的事件 flags 加 MOUSEEVENTF_VIRTUALDESK。
#        影响全部注入路径（连点/拖拽/宏/抖器/长按的位置移动），
#        单屏机器上虚屏=主屏，归一化结果逐字节相同 -> 零风险。
#     ② dashboard 固定坐标 clamp 从 [0, 主屏) 改成虚拟桌面矩形，
#        副屏负坐标/超右边界按虚屏夹。
#   防线：diag_v25_vscreen_probe.py 26 项（单屏/副屏在左/副屏在上三组
#         归一化 + 事件 flags + 参数层 clamp + 负向对照：旧 clamp 必须
#         把 -800 夹成 0）。
#   ⚠ 本机是单屏，副屏行为只能靠打桩验证 —— 真机多屏验收要主人配合
#     （插上副屏后：固定坐标点「取当前」录副屏坐标 -> 就绪 -> 触发，看落点）。
#
# v24：系统托盘（S1，保留在 dist_v24）—— 纯 ctypes，双击显示 /
#     右键 显示·暂停·退出 / 「关闭到托盘」可选开关。
# v23：S4 完成后动作 + S5 CLI --profile + S6 鼠标抖器（已交付，保留在 dist_v23）。
# v22 已交付内容（保留在 dist_v22）：
#   鼠标区四大进阶参数接线（点击类型单/双/三击 + 间隔浮动 ±ms +
#   固定坐标含「取当前」+ 启动倒计时 ≤600s 可停表可取消）。
# v21：全局暂停防抖 + clear_global 分区恢复 + save_settings 同步内存。
# v20：齿轮面板默认隐藏 + 点一次一切 + 关 easy_drag 噪声。
# v19：配置导入/导出/多方案。
# v18：宽屏三栏对齐 + 设置持久化路径修复。
#
# 回归基线（.venv **串行**全绿，19 套）：
#   verify_window 204 / verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger 43 / verify_bugfix 29 / **verify_two_stage 282** /
#   regression_keyhold 66 / verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_toast 冒烟 / verify_reported_20261016 64 / diag_keypause_probe 21 /
#   diag_settings_probe 32 / verify_config_io 32 / diag_globalpause_probe 18 /
#   diag_v22_features_probe 72 / diag_v23_features_probe 130 /
#   diag_v24_tray_probe 66 / **diag_v25_vscreen_probe 26** /
#   selftest 21（vsum，带 pywebview）
# ⚠ 回归必须**串行**跑：有计时敏感断言，两个套件并行会互相抢 CPU
#   造成偶发假红（2026-10-16 实测过一次）。
#
# 运行：bash _build_v25.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v25"
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
