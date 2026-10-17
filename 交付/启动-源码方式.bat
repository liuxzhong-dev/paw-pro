@echo off
chcp 65001 >nul
REM ============================================================
REM  备用启动方式：跑源码，不经 PyInstaller 打包出来的 exe
REM
REM  【什么时候用这个】
REM  系统开了「智能应用控制」(Smart App Control, SAC)。
REM  它只放行"已验证且声誉良好"的应用，
REM  而 PyInstaller 打包出来的 exe 没有签名也没有声誉，
REM  双击就会被拦。
REM
REM  这个 bat 直接调 Python 跑源码。python.exe 是正常安装的、
REM  通常不在 SAC 拦截范围内，所以能绕过。
REM
REM  【如果这个也被拦】
REM  说明 SAC 把 python.exe 也拦了，只能关掉 SAC ——
REM  关的方法见《使用说明.md》的「exe 打不开/被系统拦截」一节。
REM ============================================================

REM 回到项目根目录（本文件在 交付\ 下，项目根是它的上一级）
cd /d "%~dp0.."

REM 打包/测试用的解释器：带 pywebview 的那个
set PY=C:\Users\LXZ\.workbuddy\binaries\python\envs\vsum\Scripts\python.exe

if not exist "%PY%" (
    echo [错误] 找不到 Python：%PY%
    echo 请编辑本文件，把 PY= 那行改成你机器上带 pywebview 的 python.exe 路径。
    pause
    exit /b 1
)

echo 正在用源码方式启动...
echo 关闭这个窗口就等于关闭程序。
echo.

"%PY%" dashboard.py

if errorlevel 1 (
    echo.
    echo [出错了] 程序异常退出。日志在这里：
    echo   %LOCALAPPDATA%\MouseAutoClicker\logs\launch.log
    echo 把那个文件发出来就能定位问题。
    pause
)
