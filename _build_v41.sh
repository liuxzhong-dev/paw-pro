#!/bin/bash
# =====================================================================
# _build_v41.sh —— v41 全量打包（7 个 exe，图形版 爪Pro.exe）
# =====================================================================
# v41 内容（主人两条反馈，都属于「按钮语义和实际行为对不上」）：
#
#  ① 【两个入口打开的是同一个面板】主人原话：
#     「最上方设置按钮和外观按钮打开的是同一个界面，这两个打开的新页面
#      应该分开，而且设置按钮再点一次可以关闭，外观按钮再点一次没有反应」
#     改法：
#       · 齿轮 → 右上角 .cfg-panel：只剩 导入 / 导出 / 配置方案（再点收起）
#       · 图片 → 居中 .look-modal：背景 / 质感·透明度 / 按钮样式（再点收起）
#     两个浮层**互斥**（打开一个就收起另一个）。v40 的症状根因就是把
#     「外观」塞进了设置面板，两个按钮都 toggle 同一个 div：第二次点
#     只是把已经开着的面板再开一次，观感就是「没反应」。
#     另外补：Esc 关闭、遮罩关闭、入口按钮高亮（.tb-btn.on）。
#
#  ② 【录制区启停键没有意义】主人原话：
#     「点击开启录制键——点击启停键（F7）——录制行为——录制行为结束点击
#      启停键（F7）——点击停止录制键」
#     旧实现：「开始录制」点下去就在录（或等首次点击），F7 直接把会话
#     收尾且**不把窗口叫回来**（_macro_finish(auto=False) 不 show），
#     用户看到的就是「按了没反应」。新流程四态：
#       · 点「开始录制」→ 待命（钩子装好、一条不记、窗口不最小化）
#       · 按 F7 → 开录（此刻才最小化让位）
#       · 按 F7 → 暂停（补抬起、窗口回来、时间轴扣除暂停时长）
#       · 再按 F7 → 继续 / 点「停止录制」→ 收尾定稿
#     recorder 新增 START_WAIT_KEY 模式与 begin()/pause()/resume()；
#     F7 自身的 vk 加进 skip_vks —— RegisterHotKey 和低级键盘钩子是两套
#     机制，钩子照样收得到 F7，不排除的话回放到那一步会自己触发暂停。
#     待命超时单独用 WAIT_KEY_TIMEOUT=600s（ARM_TIMEOUT 的 30 秒是给
#     「等首次点击」的，对「先点开始、再去调参数」太紧）。
#     界面删掉「起点」下拉（三种起点与 F7 心智模型冲突），换成启停键流程
#     提示；状态文案区分 待命 / 录制中 / 已暂停（这是主要防线：用户看到
#     「录制中」会以为已经在录）。
#
# 本轮踩到的坑：
#   · 静态断言按「选择器到第一个右花括号」截段落，用字符串窗口搜函数体
#     容易截错（先写 'lookModal").classList.remove' 就漏了实际写法）——
#     改成取函数起点后 700 字符再查两个关键词。
#
# 复刻 build.bat 的 7 个 PyInstaller 目标。三条铁律来自本机实测教训：
#   1. **绝不删除任何旧目录** —— 本机有批量删除保护，PyInstaller 重建时
#      删不掉旧产物，表现为「日志显示 Build complete 但 exe 还是上一轮的」。
#   2. **不加 --clean** —— 它会先整目录删 build\，本机可能被拦导致打包失败。
#   3. **workpath 按轮独立** —— 复用旧 workpath 时「源码没变的目标」
#      会被判 up-to-date 直接跳过重写，产物是上一轮的，时间戳校验误报。
#
# v40 内容（主人四条需求 + 追加两条：多背景 / 透明度 / 按钮样式 / 持久化）：
#   ① 背景管理器：内置三套（暮光·栈桥 / 纸白 / 墨黑）+ 自定义图片；
#      配置面板里做成卡片网格，选中即生效即落盘。
#   ② 裁剪 / 预览：自研轻量裁剪器（拖拽平移 + 滑块/滚轮缩放 + 宽高比 +
#      三分构图线 + canvas 导出），交互模型对标 Cropper.js 的核心子集；
#      拖动时背后实时试穿新背景（所见即所得）。
#   ③ 区板块透明度：两个滑块（面板 / 内槽）+ 四档预设（通透/均衡/厚重/默认）。
#      CSS 侧把填充 alpha 改成 calc(* var(--glass-a))，色板仍只有 :root 一处源头。
#   ④ 按钮样式四套：宝石 / 玻璃 / 描边 / 胶囊，body[data-btn] 切换，即选即存。
#   ⑤ **持久化**（主人点名「不要再复现关了再开恢复默认」）：bg / glass /
#      btnstyle 三个顶层键全部「改动即落盘」，白名单与 _save_settings 同步，
#      启动 bootstrap 回读。v35 事故同型——外观值只活在表单快照里就会被擦。
#   ⑥ 浅色适配（做「纸白」时才暴露的真问题）：深色玻璃 + 亮文字的组合只对
#      深底成立，纸白背景上面板变浅、亮字消失（实测整屏发白）。补一套
#      html.light token 层（文字/玻璃/内槽/边线/语义色全套反相）+
#      十几条硬编码深色面板的定点覆盖；自定义图按平均亮度（24×24 sRGB
#      加权采样，阈值 0.60）自动判浅色/深色，压暗层在浅色下变成提亮层。
#
# 本轮踩到并记下的坑：
#   · _clamp 重名：本文件末尾还有个三参数的 _clamp（坐标用），后定义覆盖
#     先定义，四参数调用直接 TypeError —— save_appearance 保存全部失败。
#     改名 _clamp_num。探针第一轮就抓到了。
#   · bootstrap 漏字段：glass/btnstyle 只加进 save_appearance 没加进
#     bootstrap，表现为「改了透明度和按钮样式，重启回默认」。
#   · 静态断言被注释里的花括号绊倒：.cfg-panel 的注释写了
#     ::-webkit-scrollbar{width:0}，而 verify_two_stage 是按「选择器到第一个
#     右花括号」截段落的 → 断言假红。注释里别写花括号（硬规则 38 的延伸）。
#   · 截图假象：Edge 无头 + virtual-time 下 transition 会停在中间帧，
#     输入框从暗槽过渡到白槽被截成半透明灰，误判「主题没生效」。
#     _make_shot.py 现在统一冻结动效（*,*::before,*::after）。
#   · 配置面板加了 3 节后比窗口高，max-height + overflow-y 内部滚动；
#     全局滚动条 width:0 会让溢出内容连滚轮提示都没有。
#
# 回归基线（.venv **串行**全绿）：
#   verify_window 214 / mouse_zone 39 / binding 49 / trigger 48 /
#   bugfix 29 / two_stage **340** / config_io **36（schema v2：多 glass/btnstyle）** /
#   toast_delivery 13 / toast_style 56 / toast 冒烟 / reported 65 / keypause 21 /
#   settings 47 / globalpause 18 / regression_keyhold 66 / v22 72 / v23 130 /
#   v24 66 / v25 26 / v26 89 / v27 184 / v30 77 / v34 26 / v35 e2e 11 /
#   **bg probe 46（v40 扩展）** / **v41 flow probe 69（待命/暂停/面板拆分）** / selftest 21（vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言，并行会互相抢 CPU 造成偶发假红。
#
# 运行：bash _build_v41.sh   （约 3-5 分钟）
# =====================================================================
# v39 内容（主人：「颜色还是土，怀疑背景纯黑；要自定义背景按钮；
# 紫参考 B 站《天之骄紫》+ 栈桥暮光图」）：
#   · 默认背景从近黑 #07030E 改为「暮光紫天空」六档垂直梯度
#     （顶深紫罗兰→中粉紫→天际暖粉→海面深紫蓝，参考图全程无纯黑），
#     body::before 补整体压暗 + vignette 保证亮底上的文字对比度。
#   · 新增自定义背景：标题栏图片按钮 + 配置面板「背景」小节
#     （选择图片 / 恢复默认）。FileReader dataURL → save_background
#     解码存 backgrounds/bg_custom.<ext> → 设置顶层键 bg 持久化 →
#     启动 bootstrap.bg 回读应用。白名单加 bg；探针 diag_bg_probe.py
#     24 项（存盘/换格式覆盖/重启恢复/自动保存不擦/坏输入负向/
#     文件丢失降级/路径穿越防护）。
#   · 【实测坑，三条路只有第三条通】自定义背景层最初用 z-index:-3 的
#     fixed div：与 body::before(-2) 伪元素共存时整层被吞（computed
#     全对就是不画，软件渲染合成顺序 bug）；改 inline 写 body/html 的
#     background-image：画布传播背景被首轮样式快照缓存，computed 有图
#     截图仍是旧背景；最终用 z-index:0 的普通定位元素 + 多层背景
#     （vignette+压暗+用户图同元素，零层叠竞争）。
#   · 面板 token 微调（暮光亮底上压深一档），toast 3 个底色常量同步
#     （verify_toast_style 56 项全绿）。，
#   参考 B 站《漂亮的紫水晶仙子》BV14fYnzuEm9）：
#   「土」的真因（调研 + 色板诊断）：① 主 accent 是荧光紫 #8B5CF6
#   （Tailwind violet-500），大面积用=霓虹塑料感，业内共识是「大面积低饱和 +
#   小面积高饱和点睛」；② 整套只有一个紫色调的不同 alpha，色相单一层次扁平，
#   真实紫水晶色谱跨度大（透亮粉紫→宝石紫→深紫→近黑紫）；③ 文字/中性色发灰
#   （#9C8CC4 灰紫显脏）；④ 极光是高 alpha 单色荧光团；⑤ 高光用纯白——
#   白高光在紫底上发灰蓝，真实晶体透光处是粉紫（薰衣草系）。
#   落地：accent → #9773D2 水晶紫（降饱和）；新增 --crystal-lift 透亮紫
#   （点睛/文字/图标，原 --accent-blue 是 v34 遗留错命名，全局改名）与
#   --crystal-hi 粉紫受光面（rim/sheen/高光一律换粉紫，白高光清零 32 处）；
#   玻璃填充改四档模拟晶体内部（顶=粉紫受光面、中=本色紫、下=根部近黑紫）；
#   saturate 160%→150% 防过艳；极光三团按色散拉开（粉紫/紫蓝/深紫）并降 alpha；
#   底色亮一档去闷（#120A24→#16102E）；文字去灰提亮；主按钮三段紫宝石
#   （crystal-hi→accent→accent-deep→accent-ultra 全 token 化）。
#   toast 8 个颜色常量同步跨源（verify_toast_style 56 项全绿）。
#   调研来源：colormagic 紫水晶色板（Midnight/Royal Amethyst）、madegooddesigns
#   紫色色谱表（Amethyst #9966CC / Deep Violet / Royal Violet）、莫兰迪紫灰
#   「AI紫灰」高级感逻辑（laur.cn）、vibe-designer skill 的 Purple/Violet
#   Linear-Raycast 档位、kitbase 2026 暗色仪表盘 token 化趋势。
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
#   v30 probe 77 / **v34 probe 26** / v35 e2e 11 / **bg probe 24（v39 新增）** /
#   selftest 21（vsum）
# ⚠ 回归必须**串行**跑：有计时敏感断言，并行会互相抢 CPU 造成偶发假红。
#
# 运行：bash _build_v41.sh   （约 3-5 分钟）
# =====================================================================
set -e
cd "$(dirname "$0")"

PY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
DIST="dist_v41"
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
