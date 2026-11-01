@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open_napcat_webui.ps1"
if errorlevel 1 pause
