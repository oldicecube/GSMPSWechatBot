@echo off
chcp 65001 >nul
title Wechat Bot

echo ============================================
echo   Wechat Bot + weflow-core
echo ============================================
echo.

cd /d %~dp0

:: Check if weflow-core has been built
if not exist "weflow-core\dist\index.js" (
    echo [SETUP] First run, building weflow-core...
    if not exist "weflow-core\package.json" (
        echo [ERROR] weflow-core\package.json not found, skipping build.
        goto skip_weflow_build
    )
    pushd weflow-core
    call npm install
    if %errorlevel% neq 0 (
        echo [WARN] npm install failed, continuing anyway...
    )
    node esbuild.config.mjs
    popd
    echo [SETUP] Build complete
    echo.
)
:skip_weflow_build

:: Create WeFlow.exe (rename node.exe to bypass DLL process check)
if not exist "weflow-core\WeFlow.exe" (
    echo [SETUP] Creating WeFlow.exe...
    for /f "tokens=*" %%i in ('where node 2^>nul') do (
        copy /y "%%i" "weflow-core\WeFlow.exe" >nul
    )
    if exist "weflow-core\WeFlow.exe" (
        echo [SETUP] WeFlow.exe created
    ) else (
        echo [WARN] WeFlow.exe not created, node.exe may not be in PATH
    )
    echo.
)

:: Install Python dependencies (if needed)
pip install -r requirements.txt -q 2>nul
echo.

:loop
echo [INFO] Starting...
python main.py

echo.
echo [WARN] Exited, restarting in 3 seconds...
timeout /t 3 >nul

goto loop
