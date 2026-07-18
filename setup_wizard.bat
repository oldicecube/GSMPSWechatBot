@echo off
chcp 65001 >nul
title WeFlow 配置向导
cd /d "%~dp0weflow-core"
echo ============================================
echo   WeFlow 配置向导 - 提取微信解密密钥
echo ============================================
echo.
echo 请确保微信已登录！
echo.
node dist/index.js ../config.json
echo.
echo ============================================
echo 配置完成！按任意键关闭...
pause >nul
