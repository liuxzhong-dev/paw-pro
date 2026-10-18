#!/bin/bash
# =====================================================================
# _build_v37.sh —— v37 全量打包（7 个 exe，图形版 爪Pro.exe）
# =====================================================================
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v37 内容（主人参考 B 站《GPT-6Astra 实现的液态玻璃效果》BV1CUeU6YE3c：
#   「每个区整体看上去要有玻璃质感」）：
#   v36 的霜玻璃只到「半透明+顶线」，差的是 Apple Liquid Glass 的三样东西：
#   ① specular rim 受光棱（单项价值最高：同样的 blur，顶部 1.5px 亮棱 +
#      底部反弹光 + 左右弱棱 + 内侧漫射 + 根部压深，立刻从「薄膜」变「玻璃」）
#   ② 斜向 sheen 镜面层（.panel::after，z-index:-1 画在填充之上内容之下）
#   ③ 圆角 + hover 高光跟随（--mx/--my 由 JS 写进 ::before 的 radial 位置）
#   「整体感」的真因：面板里 8 个 trig-cell + 4 个 kpi 等各自带切角实色块，
#   像在一块玻璃上贴满小卡片 —— 全部改为「内嵌玻璃槽」（上暗下亮的凹槽，
#   圆角，弱边），整块区才读起来是一整块玻璃。
#   形状语言切换：clip-path 切角 → 18px 大圆角（切角会把外阴影一起裁掉，
#   液态玻璃要「整块浮起」）；--chamfer token 随之删除。
#   toast 是 GDI 自绘切角（CreatePolygonRgn），改形状要重画四角像素断言，
#   **不动**：与主界面形状语言解耦、颜色仍同源（verify_toast_style 两条
#   断言同步改为「液态圆角 + rim/sheen 齐备」，56 项全绿）。
#   调研来源：webtricks.dev（frost/rim/sheen 配方 + SVG displacement 折射）、
#   theplusaddons（backdrop-filter 十个函数无一能移像素，折射必须 feDisplacementMap）、
#   skillsmp glass skill（Tier1 霜玻璃默认 / Tier2 Chromium 折射只给小件）、
#   worldprogramming（调参顺序 backdrop→tint→blur→highlights）。
#   性能铁律不减：同屏 ≤6 面板、卡片内不叠 blur、不动画 blur 半径、双降级。
#
# v36 内容（主人：UI 差点感觉，要「玻璃宝石质感」，调研 GitHub 成熟方案后落地）：
#   调研结论：glassmorphism 的完整配方是四层 —— ① 背后有可糊的内容
#   （backdrop-filter 只糊元素背后已绘制内容，**纯色背景上玻璃不可见**）
#   ② 半透明填充 + blur()saturate() ③ 1px 半透明边缘 + inset 顶部高光 /
#   底部暗边 ④ 多层外阴影悬浮感。参考 glasscn-ui（shadcn 玻璃变体）、
#   liquid-glass-component-library（70+ 组件 Glass/Liquid 双模式）、
#   Vibe UI / zentak-glass-ui（仪表盘玻璃件）与 glassmorphism skill 的
#   frost tiers / specular edge / 噪点配方。
#   落地（只改 CSS，不动任何 Python / token 断言值）：
#   · body::before 三团紫极光 + 26s 极慢漂移，body::after 5% feTurbulence
#     噪点（打散重模糊下的渐变色带 banding）；
#   · .panel / .titlebar / .cfg-panel 上半透明填充 + backdrop-filter
#     blur(18~26px) saturate(160%) + inset 高光棱线 + 多层阴影；
#   · 主按钮改「紫宝石」：顶面受光最亮 → 腰身本色 → 根部压深，
#     顺手修掉 primary 里残留的蓝色 rgba(42,108,255)（v34 遗留）；
#   · 表单控件改「凹陷玻璃槽」（底部压暗），与面板的凸起受光相反；
#   · 性能铁律：同屏玻璃面板 ≤6、卡片内部一律不叠 backdrop-filter
#     （嵌套模糊是平方开销）、不动画 blur 半径；@supports 与
#     prefers-reduced-transparency 双降级（玻璃是装饰，可读性优先）。
#   ⚠ toast 是 GDI 自绘、没有 blur 能力，**本轮不动**（verify_toast_style 56 项仍绿）。
#
# v35 内容（主人实测：改好的启停键重启后回到中键/F6）：
#   ① 真因：bind_captured_key / set_system_hotkey 已把 trigger 写盘，
#      但前端随后自动保存只提交 mouse/key/hide；旧 Api.save_settings 把这份
#      **局部表单当整文件覆盖**，几百毫秒后就把 trigger 擦掉。本轮内存仍有效，
#      所以必须重启才暴露，dist_v34/交付 的实际 json 均验证为缺 trigger。
#   ② 改成「局部数据先合并内存 SSOT → 完整快照原子落盘」；临时文件与目标
#      同目录，flush+fsync 后 os.replace，进程中断也不会留下半截 JSON。
#   ③ 启动恢复补齐 mouse/key/macro 三类鼠标触发键；系统热键改成保存值优先、
#      默认候选兜底，并修 start() 返回 False（不抛异常）时不回落的静默失效。
#   ④ set_button_hotkey / clear_system_hotkey 等旧入口统一走同一持久化函数；
#      写盘失败明确提示「本次生效但未保存」，不再假装成功。
#   防线：diag_settings_probe.py 32→47 项，含模拟重启、局部自动保存、
#      macro 恢复、start=False 回落、原子临时文件清理与旧行为负向对照。
#
# v34 内容（主人 2026-10-18 凌晨实测报回的两条）：
#   ① **回放跑到左上角去点**（大 bug）：
#      core/inputs.py 里 5 处构造点写成 `MOUSEINPUT(x=..., y=...)`，
#      而真字段名是 dx / dy。ctypes 的 Structure 对**不存在的字段关键字
#      不报错** —— 构造「成功」、SendInput 照样返回成功，坐标却永远是 0，
#      于是每一步都把鼠标送到 (0,0)。点击类事件不带坐标看不出来，
#      只有移动类事件暴露，所以它能一路活到用户手上（v25 加虚屏坐标时
#      就写错了，期间副屏、宏回放全都受影响）。
#      修成 dx=/dy=，并在 docstring 里留反例警示。
#   ② 宏区启停键（F7）：
#      · 回放一启动窗口就最小化，没有系统级热键就没法中途暂停；
#      · 新增 HOTKEY_MACRO_FALLBACK（F7 / Ctrl+F7 / F10）+ toggle_macro；
#      · 暂停控制加 "macro" 分区（以前宏跟着 mouse 区走，
#        一按启停键会连带把连点定住）；引擎 ClickContext/ ClickerEngine
#        新增 zone 参数，默认 "mouse" 老行为一字不变；
#      · 状态分派：录制中→收尾 / 回放中→暂停 / 暂停中→继续 /
#        空闲有步骤→启动 / 空闲没步骤→明确提示「先录制」；
#      · 界面：触发方式区第 4 格 + 宏区提示行（都显示**真键名**，
#        降级到 Ctrl+F7 时不会照提示按错）；改键/绑鼠标键下拉加 macro。
#   防线：diag_v34_probe.py 26 项。关键一手是**假 SendInput 抄字节**——
#      本会话进程注入不生效（move_to 返回 True 但光标不动），
#      真机验不了；但「我们送出去的 dx/dy 对不对」可以验，
#      比看光标有没有动更硬，且不依赖系统权限。
#   ③ **界面 UI 重设计：青→紫（紫霓虹·实心分层，A 方向）**：
#      名字/图标已是「爪Pro·紫爪」，界面还是青色全息风，皮与身份脱节。
#      做法 = 皮骨分离：骨（宽屏加列规则/切角/等宽数字/三态色/双源同步）
#      全留，皮（强调色/底色/辉光）围绕图标紫重做。token：
#      底 #06030D / 面板 #120A24 / 强调 #8B5CF6（图标渐变 #DEB0FF→#7A24D2
#      中值）/ 深 #5B1E9E / 亮 #A78BFA / 文字 #F0E8FF·#9C8CC4·#5F4E85；
#      语义色不变（薄荷=运行/琥珀=暂停/红=警报）。
#      core/toast.py 同源改（跨源绑定 verify_toast_style.py 56 项断言）。
#      防线：toast_style 56 + two_stage 340（含双源一致闸门）
#      + MKSHOT 1180/1500/1920/2560 四档视觉验收。
#
# v31（保留在 dist_v31）：界面精简 + 按钮统一 + 改名 爪Pro + 窗口可拖缩。
# v30（保留在 dist_v30）：诊断锁存 + 三栏页脚 + 录制起点/步骤编辑器。
# v29（保留在 dist_v29）：触发键捕获 8 秒超时 + 左键误录文案 + 最近按键补写。
# v28（保留在 dist_v28）：录制期按 Pause 会顺带打开全局暂停 → 回放卡在第一步。
# v27（保留在 dist_v27）：S2 宏录制回放 GUI（键鼠双低层钩 + 回放插件 + 独立分区）。
# v25：副屏/虚拟桌面坐标（**本版修的 bug 就是从这里写错的**）。
# v24：系统托盘（S1）/ v23：S4+S5+S6 / v22：鼠标区四大进阶参数接线。
#
# 回归基线（.venv **串行**全绿）：
#   verify_window 214 / mouse_zone 39 / binding 49 / trigger 48 /
#   bugfix 29 / two_stage 340 / config_io 32 / toast_delivery 13 /
#   toast_style 56 / toast 冒烟 / reported 65 / keypause 21 / settings **47** /
#   globalpause 18 / regression_keyhold 66 / v22 probe 72 / v23 probe 130 /
#   v24 probe 66 / v25 probe 26 / v26 probe 89 / v27 probe 182+1SKIP /
#   v30 probe 77 / **v34 probe 26（本版新增）** / selftest 21（vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言，并行会互相抢 CPU 造成偶发假红。
#
# 运行：bash _build_v37.sh   （约 3-5 分钟）
# =====================================================================
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v37"
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
