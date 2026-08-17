@echo off
setlocal EnableExtensions

title VB-CABLE Setup Helper for WeChat Voice

echo ============================================================
echo   VB-CABLE setup helper for WeChat Bot voice delivery
echo ============================================================
echo.

echo This script must be run from an Administrator command prompt.
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo ERROR: Administrator privileges are required.
    echo Right-click this file and choose "Run as administrator".
    pause
    exit /b 1
)

set "WORK=%~dp0vb-cable"
set "ZIP=%WORK%\VBCABLE_Driver_Pack45.zip"
set "UNZIP=%WORK%\VBCABLE_Driver_Pack45"
set "INSTALLER=%UNZIP%\VBCABLE_Setup_x64.exe"
set "ARCHIVE_URL=https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"

if not exist "%WORK%" mkdir "%WORK%"

if not exist "%INSTALLER%" (
    echo [1/3] Downloading VB-CABLE driver package...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%ARCHIVE_URL%' -OutFile '%ZIP%' -UseBasicParsing"
    if errorlevel 1 (
        echo ERROR: Download failed. Check the network connection and try again.
        pause
        exit /b 1
    )

    echo [2/3] Extracting driver package...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%UNZIP%' -Force"
    if errorlevel 1 (
        echo ERROR: Extraction failed.
        pause
        exit /b 1
    )
)

echo [3/3] Starting the x64 installer...
echo In the installer window, select Install Driver.
"%INSTALLER%"

echo.
echo After installation, restart Windows and then:
echo   1. Confirm that CABLE Input and CABLE Output are available.
echo   2. Set the WeChat recording/call microphone to CABLE Output.
echo   3. The Bot writes audio to CABLE Input; it does not use physical speakers.
echo.
pause
