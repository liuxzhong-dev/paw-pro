#!/bin/bash
# =====================================================================
# _build_v33.sh —— v33 全量打包（7 个 exe，统一图标）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v31 内容（主人 2026-10-17 19:2x 的两条反馈）：
#   ① 界面精简 + 按钮统一：删掉整块「功能说明」（占近一屏），改一行流程提示
#      `① 选触发方式 → ② 设参数 → ③ 启动`；主按钮/次按钮/卡内小按钮统一
#      34px 高 + 8px 切角 + 11px/600 + 字距 .14em。
#   ② 窗口能拖能缩（长期老 bug）：根因是无边框窗口把非客户区压成 0 像素，
#      WM_NCHITTEST 对四边全返回 HTCLIENT → 系统拖动/缩放模态循环根本进不去。
#      改自研：前端算位移 → 后端 SetWindowPos（最小 900x620 夹取），
#      标题栏拖动 4px 阈值防误触；缩放热区 5px→8px。
#   ③ 改名 爪Pro / Paw Pro：窗口标题、托盘提示、配置导出名、FindWindowW、
#      spec 的 name、build_cli 清单同步；旧名「连点锁定器」源码归零。
#      宏 schema 不变（v30 的 schema 2 直接兼容）。
#
# v30（保留在 dist_v30）：诊断锁存 + 三栏页脚 + 录制起点/步骤编辑器。
#   ① 「最近按键」只偶尔闪一下 → 诊断锁存：down 时记 last_press + 时刻，
#      抬起不清（另存 last_ids 表示"正按住"），界面显示「G1（0.4 秒前）」。
#      真因：500ms 轮询采样不到几十毫秒的按键，旧实现抬起即清空。
#   ② 最大化后键盘区/触发方式区底部大片空白 → 三卡改纵向 flex，
#      状态行与自检条 margin-top:auto 贴底（并抵回 flex 下不再折叠的间距）。
#   ③ 录制流程重做：
#      · 起点可选（首次点击 / 3 秒倒计时 / 立即），起点前的移动滚轮不记；
#      · 录完压缩成步骤（core/macro_steps.py 新增）：点击/按键/滚轮/等待，
#        鼠标轨迹默认不占步骤，拖拽会拆成按下+抬起而不是静默丢；
#      · 步骤可编辑（名称/坐标/固定或随机延时/执行完成后/删除/复制/上下移/
#        插入等待），弹窗形态；回放吃展开后的步骤，改完立刻生效；
#      · 宏文件 schema 2：多存 steps，v29 及以前打不开新宏（明确报版本）。
#   防线：diag_v30_editor_probe.py 77 项（压缩/展开/校验/起点/锁存/编辑/静态）。
#
# v29（保留在 dist_v29）：触发键捕获 8 秒超时 + 左键误录文案 + 最近按键补写。
# v28（保留在 dist_v28）：录制期按 Pause 会顺带打开全局暂停 → 回放卡在第一步。
# v27（保留在 dist_v27）：S2 宏录制回放 GUI（键鼠双低层钩 + 回放插件 + 独立分区）。
#
# v25：副屏/虚拟桌面坐标（S3 的前置改造，保留在 dist_v25）—— _to_absolute
#     按虚屏归一化 + MOUSEEVENTF_VIRTUALDESK，clamp 改按虚屏矩形。
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
# 回归基线（.venv **串行**全绿，21 套）：
#   verify_window **214（v29 +10）/ verify_mouse_zone 39 / verify_binding 49 /
#   verify_trigger **48（v29 +5）/ verify_bugfix 29 / **verify_two_stage 339** /
#   regression_keyhold 66 / verify_toast_delivery 13 / verify_toast_style 56 /
#   verify_toast 冒烟 / verify_reported_20261016 64 / diag_keypause_probe 21 /
#   diag_settings_probe 32 / verify_config_io 32 / diag_globalpause_probe 18 /
#   diag_v22_features_probe 72 / diag_v23_features_probe 130 /
#   diag_v24_tray_probe 66 / diag_v25_vscreen_probe 26 /
#   **diag_v26_multipos_probe 89 /
#   **diag_v27_macro_probe 183** / **diag_v30_editor_probe 77（v30 新增）** /
#   selftest 21（vsum，带 pywebview）
# ⚠ 回归必须**串行**跑：有计时敏感断言，两个套件并行会互相抢 CPU
#   造成偶发假红（2026-10-16 实测过一次）。
#
# ---------------------------------------------------------------------
# v33（2026-10-17 22:2x，主人反馈两条）：
#   ① 缩略图跟大图标不是同一个东西：v32 的 ico 里 16/24/32 是母版硬缩，
#      母版高光太亮（#ffd8ff），细尖角在 16px 上糊成三条白条。
#      → _make_ico.py 重做小尺寸：保留剪影、颜色换成饱和紫渐变。
#   ② 交付文件夹里除 爪Pro.exe 外 6 个 exe 还是 PyInstaller 默认黄软盘：
#      只有 spec_dashboard.spec 设了 icon，其余 6 个 spec 都没有。
#      → 6 个 spec 全部补 icon=assets/PawPro-violet.ico。
#   只动图标与 spec，不改任何功能代码。
#
# 运行：bash _build_v33.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v33"
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
EXPECT="爪Pro.exe 长按锁定器-图形版.exe 鼠标连点器-长按锁定.exe 鼠标连点器.exe 注入窗口测试.exe 注入目标诊断.exe 注入诊断工具.exe"
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
  if "$PY" _verify_exe_html.py "$DIST/爪Pro.exe"; then
    echo "  [OK] exe 内嵌 HTML 与源码一致"
  else
    echo "  [失败] exe 内嵌 HTML 与源码不一致（漏回嵌 dashboard.py？）"; FAIL=1
  fi
fi

# 模块完整性：动态 import 的模块会被 PyInstaller 静默漏掉
# （源码能跑、日志全绿、exe 里就是没有）。CArchive + PYZ 两层都查。
if [ "$FAIL" = "0" ]; then
  echo "[3.6/4] 校验 exe 内项目模块齐全（CArchive + PYZ）"
  if "$PY" _verify_exe_modules.py "$DIST/爪Pro.exe"; then
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
