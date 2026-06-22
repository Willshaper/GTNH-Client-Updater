@echo off
title GT New Horizons - Instance Updater
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0update_gtnh.py"
    goto :eof
)

where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0update_gtnh.py"
    goto :eof
)

echo.
echo   Python 3 was not found on your PC.
echo.
echo   1. Install it from:  https://www.python.org/downloads/
echo   2. During setup, TICK "Add python.exe to PATH".
echo   3. Then double-click this file again.
echo.
pause
