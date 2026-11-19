@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 bootstrap.py
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python bootstrap.py
    if errorlevel 1 pause
    exit /b
)
echo Please install 64-bit Python 3.10+ and enable Add Python to PATH.
pause
