@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title OpenTask 打包工具

echo ============================================================
echo   OpenTask EXE 打包脚本
echo ============================================================
echo.

where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [提示] 正在安装 PyInstaller...
    python -m pip install pyinstaller
)

echo [步骤 1/2] 清理旧的构建缓存...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist OpenTask.spec del /q OpenTask.spec

echo [步骤 2/2] 正在打包（onedir 模式，可能需要 1-3 分钟）...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --name "OpenTask" ^
  --icon "static\icon.ico" ^
  --add-data "static;static" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols ^
  --hidden-import uvicorn.protocols.http ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan ^
  --hidden-import uvicorn.lifespan.on ^
  --collect-all webview ^
  --collect-all pystray ^
  main.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上方日志。
    pause
    exit /b 1
)

echo.
echo [完成] 打包成功！产物在 dist\OpenTask 目录。
echo         请将该目录内容复制到 "OpenTask 0.1.0" 文件夹。
pause
