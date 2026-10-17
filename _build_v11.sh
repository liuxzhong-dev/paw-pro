#!/bin/bash
# =====================================================================
# _build_v11.sh —— v11 全量打包（7 个 exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。两条铁律来自本机实测教训
# （build.bat 顶部有完整说明）：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
# 所以每轮打到一个全新的 dist_vN，并在最后校验时间戳确认是本轮新生成。
#
# v11 内容：
#   - 鼠标/键盘区两段式状态机（按钮=就绪，F5/F6=启停）
#   - 提权检测（UIPI）+ 一键管理员重启 + 运行期心跳日志
#   - CLI「跑完不退出」菜单 + 回答参数崩溃修复（并行会话的 cli.py 成果，
#     一并从当前源码重建，保证交付与文档一致）
#
# 运行：bash _build_v11.sh   （约 2-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v11"
STAMP=$(date +%s)

echo "[0/4] 依赖硬校验（缺 pywebview 直接中止，不静默产出打不开的 exe）"
"$PY" -c "import webview" || { echo "[错误] 缺少 pywebview"; exit 1; }
echo "  pywebview OK"

echo "[1/4] 开始打包 7 个目标 -> $DIST （不删旧目录、不加 --clean）"
"$PY" -m PyInstaller spec_dashboard.spec    --noconfirm --workpath build/dash    --distpath "$DIST"
"$PY" -m PyInstaller spec_keyhold_gui.spec  --noconfirm --workpath build/gui     --distpath "$DIST"
"$PY" -m PyInstaller spec_keyhold.spec      --noconfirm --workpath build/keyhold --distpath "$DIST"
"$PY" -m PyInstaller spec_cli.spec          --noconfirm --workpath build/cli     --distpath "$DIST"
"$PY" -m PyInstaller spec_diagwin.spec      --noconfirm --workpath build/dw      --distpath "$DIST"
"$PY" -m PyInstaller spec_diagtarget.spec   --noconfirm --workpath build/dt      --distpath "$DIST"
"$PY" -m PyInstaller spec_diag.spec         --noconfirm --workpath build/di      --distpath "$DIST"

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

echo "[4/4] 结果"
if [ "$FAIL" = "0" ]; then
  echo "BUILD_OK：7 个 exe 全部为本轮新生成，产物在 $DIST/"
else
  echo "BUILD_FAILED：有产物校验失败（见上），换一个从未用过的版本目录重打"
  exit 1
fi
