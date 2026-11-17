@echo off
REM NapukettoQQ lab launcher (isolated from the production NapCat on :13001).
REM PowerShell blocks pnpm.ps1, so this must run through cmd.
setlocal
cd /d C:\qq-adapter-lab\qqbot
REM Optional: point at an existing QQ install (must be ...\QQ.exe).
REM set NAPUTO_QQ_PATH=D:\Wechat Bot\depends-data\napcat\qq-client\Files\QQ.exe
call pnpm start
endlocal
