@echo off
chcp 65001 >nul
title Wechat Bot

echo ============================================
echo   Wechat Bot + weflow-core
echo ============================================
echo.

cd /d %~dp0

:: 检查 weflow-core 是否已构建
if not exist "weflow-core\dist\index.js" (
    echo [SETUP] 首次运行，正在构建 weflow-core...
    cd weflow-core
    call npm install
    node esbuild.config.mjs
    cd ..
    echo [SETUP] 构建完成
    echo.
)

:: 创建 WeFlow.exe（node.exe 重命名，绕过 DLL 进程校验）
if not exist "weflow-core\WeFlow.exe" (
    echo [SETUP] 正在创建 WeFlow.exe...
    for /f "tokens=*" %%i in ('where node') do (
        copy /y "%%i" "weflow-core\WeFlow.exe" >nul
    )
    echo [SETUP] WeFlow.exe 已创建
    echo.
)

:: 安装 Python 依赖（如需要）
pip install -r requirements.txt -q 2>nul
echo.

:loop
echo [INFO] 启动...
python main.py

echo.
echo [WARN] 已退出，3 秒后重启...
timeout /t 3 >nul

goto loop
