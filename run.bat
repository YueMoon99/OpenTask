@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title OpenTask 自动化任务节点编辑器

echo ============================================================
echo   OpenTask 自动化任务节点编辑器
echo   工作目录: %CD%
echo ============================================================
echo.

where python >nul 2>nul
if not errorlevel 1 goto CHECK_DEPS

where py >nul 2>nul
if not errorlevel 1 goto CHECK_DEPS_PY

echo [错误] 未检测到 Python 运行环境。
echo        请先安装 Python 3.8 及以上版本，安装时务必勾选 Add Python to PATH。
echo        下载地址: https://www.python.org/downloads/
echo.
pause
exit /b 1

:CHECK_DEPS
python -c "import fastapi,uvicorn,mss,cv2,pyautogui,webview" >nul 2>nul
if not errorlevel 1 goto START
echo [提示] 首次运行，正在安装依赖，大约需要 1~3 分钟，请耐心等待...
python -m pip install -r requirements.txt
if errorlevel 1 goto INSTALL_FAIL
goto START

:CHECK_DEPS_PY
py -3 -c "import fastapi,uvicorn,mss,cv2,pyautogui,webview" >nul 2>nul
if not errorlevel 1 goto START_PY
echo [提示] 首次运行，正在安装依赖，大约需要 1~3 分钟，请耐心等待...
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto INSTALL_FAIL
goto START_PY

:START
echo [启动] 正在拉起本地服务与桌面窗口，请稍候...
python main.py
goto EXIT

:START_PY
echo [启动] 正在拉起本地服务与桌面窗口，请稍候...
py -3 main.py
goto EXIT

:INSTALL_FAIL
echo.
echo [错误] 依赖安装失败。请检查网络（可临时挂国内镜像源）后重试，或手动执行:
echo        pip install -r requirements.txt
echo.
pause
exit /b 1

:EXIT
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，退出码 %errorlevel%。请查看上方报错信息。
    pause
)
