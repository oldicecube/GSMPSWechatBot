@echo off
title Wechat Bot Launcher

echo ============================
echo   Starting Wechat Bot...
echo ============================

cd /d %~dp0

:loop
echo.
echo [INFO] Starting main.py...
python main.py

echo.
echo [WARN] Bot crashed or exited, restarting in 3 seconds...
timeout /t 3 >nul

goto loop