"""
build.bat —— 连点/锁定器 打包脚本
================================

参照 VideoSummarizer 的 build.bat 规范（已实测踩过坑，务必照做）。

三个目录的分工，别搞混：
    源码*.py + core\ + plugins\ = 图纸（改了才能重新打包）
    build\                = 车间（PyInstaller 中间产物，随时可删）
    dist_vN\              = 成品（**删了就没程序**，但我们从不清它）

【核心规范，来自 VideoSummarizer 的实测教训】
本机有批量删除保护（安全软件/企业策略/safe-delete 钩子），
PyInstaller 重建时删不掉旧目录，表现为：
    **日志显示 "Build complete"，但 exe 时间戳还是上一轮的**
    新代码根本没打进去，而界面毫无异常——极难察觉。
实测连 `rmdir` 自身都会被拦（SAFE_DELETE_BULK_CONFIRM_REQUIRED），
静默失败后 PyInstaller 再删一次同样被拦。

所以本脚本的做法是：**绝不删除任何旧目录**，
每轮打到一个全新的版本目录（dist_v1 / dist_v2 / dist_v3 ...），
并在最后**校验产物时间戳**确认是本轮新生成的。
想手动指定：set BUILD_VER=v20 后再运行。

【不要加 --clean】
它会先整目录删除 build\，在本机可能被拦，导致打包中途失败。
"""

@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

REM ---- 0. 定位 Python ----
REM pywebview 只装在 vsum 这个 venv 里，用它打包界面版。
set "PY=C:\Users\LXZ\.workbuddy\binaries\python\envs\vsum\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到打包用解释器：%PY%
    echo        界面版需要 pywebview，请确认该 venv 存在。
    pause & exit /b 1
)

REM ---- 1. 版本号：自动递增，找一个从未用过的目录 ----
if "%BUILD_VER%"=="" (
    set "N=1"
    :again
    if exist "dist_v!N!" (
        set /a N+=1
        goto again
    )
    set "BUILD_VER=v!N!"
)
set "DIST=dist_%BUILD_VER%"
echo [0/7] 本轮产物目录: %DIST%

echo [1/7] 使用解释器: %PY%
"%PY%" --version

REM ---- 2. 依赖硬校验 ----
REM 缺 pywebview 就直接中止。绝不静默产出「双击打不开」的 exe。
echo.
echo [2/7] 检查依赖...
"%PY%" -c "import webview" 2>nul
if errorlevel 1 (
    echo [错误] 缺少 pywebview，界面版会双击打不开。
    echo        装一下： "%PY%" -m pip install pywebview
    pause & exit /b 1
)
echo   pywebview OK

REM ---- 3. 不删任何旧目录 ----
REM 见文件顶部说明：删不动会导致「构建成功但产物是旧版」。
echo.
echo [3/7] 跳过清理（按规范不删旧目录，改用全新版本目录）
if exist "%DIST%" (
    echo   [警告] %DIST% 已存在，可能是上一轮残留。
    echo          换一个新版本号：set BUILD_VER=v99
    pause & exit /b 1
)
if not exist "%DIST%" mkdir "%DIST%"

REM ---- 4. 打包 ----
REM 注意：**不要加 --clean**（它会删 build\，本机可能被拦）。
echo.
echo [4/7] 开始打包（约 1-3 分钟）...

echo   - 爪Pro.exe（仪表盘，pywebview）...
"%PY%" -m PyInstaller "%~dp0spec_dashboard.spec" --noconfirm --workpath "%~dp0build\dash" --distpath "%~dp0%DIST%"
if errorlevel 1 (
    echo [错误] 仪表盘打包失败
    pause & exit /b 1
)

echo   - 长按锁定器-图形版.exe（原生 Win32）...
"%PY%" -m PyInstaller "%~dp0spec_keyhold_gui.spec" --noconfirm --workpath "%~dp0build\gui" --distpath "%~dp0%DIST%"
if errorlevel 1 (
    echo [错误] 图形版打包失败
    pause & exit /b 1
)

echo   - 鼠标连点器-长按锁定.exe（命令行版）...
"%PY%" -m PyInstaller "%~dp0spec_keyhold.spec" --noconfirm --workpath "%~dp0build\keyhold" --distpath "%~dp0%DIST%"
if errorlevel 1 (
    echo [错误] 命令行版打包失败
    pause & exit /b 1
)

echo   - 鼠标连点器.exe（纯鼠标连点）...
"%PY%" -m PyInstaller "%~dp0spec_cli.spec" --noconfirm --workpath "%~dp0build\cli" --distpath "%~dp0%DIST%"
if errorlevel 1 (
    echo [错误] 连点器打包失败
    pause & exit /b 1
)

REM ---- 5. 诊断工具（体积小，顺手一起打）----
echo   - 诊断工具 3个 ...
"%PY%" -m PyInstaller "%~dp0spec_diagwin.spec" --noconfirm --workpath "%~dp0build\dw" --distpath "%~dp0%DIST%"
"%PY%" -m PyInstaller "%~dp0spec_diagtarget.spec" --noconfirm --workpath "%~dp0build\dt" --distpath "%~dp0%DIST%"
"%PY%" -m PyInstaller "%~dp0spec_diag.spec" --noconfirm --workpath "%~dp0build\di" --distpath "%~dp0%DIST%"

REM ---- 6. 附带说明文档 ----
echo.
echo [5/7] 复制文档...
if exist "%~dp0交付\使用说明.md" copy /y "%~dp0交付\使用说明.md" "%~dp0%DIST%\" >nul

REM ---- 7. 校验产物确实是本轮生成的 ----
REM 只检查「文件存在」是不够的：可能因为安全软件拦删除而跳过重写，
REM 产物存在但仍是旧文件，**新代码根本没打进去**。
REM 所以比对 exe 时间戳与本轮开始时间。
echo.
echo [6/7] 校验产物为本轮新生成...
set "STAMP=%~t0"
set "FAILED=0"
for %%F in ("%~dp0%DIST%\*.exe") do (
    if not exist %%F (
        echo   [失败] 未生成: %%F
        set "FAILED=1"
    ) else (
        if %%~tF gtr "%STAMP%" (
            echo   [OK] %%~nxF  %%~tF
        ) else (
            echo   [失败] %%~nxF 时间戳 %%~tF 不晚于本轮开始 %STAMP%
            echo          ^—— 这是旧文件，新代码没打进去。^)
            echo          ^—— 换一个从未用过的版本号重试：set BUILD_VER=v99^)
            set "FAILED=1"
        )
    )
)

REM ---- 8. 同步到交付目录 ----
echo.
echo [7/7] 同步到 交付\ ...
if not exist "%~dp0交付" mkdir "%~dp0交付"
copy /y "%~dp0%DIST%\*.exe" "%~dp0交付\" >nul

echo.
echo ============================================
if "%FAILED%"=="1" (
    echo  完成，但有产物校验失败，请看上面的 [失败] 行。
) else (
    echo  完成！产物在 %DIST%\
    echo    爪Pro.exe         ^(仪表盘，推荐，单文件^)
    echo    长按锁定器-图形版.exe   ^(原生Win32 图形版^)
    echo    鼠标连点器-长按锁定.exe ^(命令行版^)
    echo    鼠标连点器.exe         ^(纯鼠标连点^)
    echo    注入窗口测试.exe / 注入目标诊断.exe / 注入诊断工具.exe ^(诊断^)
    echo.
    echo  旧版本目录全部保留，可随时回滚。
)
echo ============================================
echo.
pause