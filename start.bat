@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo ==========================================================
echo  THS T-Trade Assistant  v1.0.0
echo  Work dir: %CD%
echo  Mode: semi-auto (signal + manual confirm), dry_run by default
echo ==========================================================

rem ==========================================================
rem  Locate a usable Python interpreter
rem  Priority: THS_PYTHON env var -> Marvis embedded runtime -> PATH
rem  Note: WindowsApps\python.exe is a 0-byte Store stub, must be skipped
rem ==========================================================
set "PY="

if defined THS_PYTHON (
    if exist "%THS_PYTHON%" set "PY=%THS_PYTHON%"
)

if not defined PY (
    for /f "delims=" %%I in ('dir /b /ad /o-d "I:\app\Marvis\MarvisAgent" 2^>nul') do (
        if not defined PY (
            if exist "I:\app\Marvis\MarvisAgent\%%I\runtime\python311\python.exe" (
                set "PY=I:\app\Marvis\MarvisAgent\%%I\runtime\python311\python.exe"
            )
        )
    )
)

if not defined PY (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        if not defined PY (
            echo %%I | findstr /i /c:"\WindowsApps\" >nul
            if errorlevel 1 set "PY=%%I"
        )
    )
)

if defined PY (
    "%PY%" -c "import sys" >nul 2>nul
    if errorlevel 1 set "PY="
)

if not defined PY (
    echo [ERROR] No usable Python interpreter found.
    echo         Tried: THS_PYTHON env var, Marvis embedded runtime, PATH.
    echo         Fix options:
    echo           1^) Install Python 3.9+ and add it to PATH
    echo           2^) set THS_PYTHON=C:\path\to\python.exe
    pause
    exit /b 1
)

echo [INFO] Python: %PY%
"%PY%" -V
echo.

"%PY%" -c "import requests, pyautogui, psutil, win32gui" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Missing dependency, installing: requests pyautogui pillow psutil pywin32
    "%PY%" -m pip install --upgrade requests pyautogui pillow psutil pywin32
    if errorlevel 1 (
        echo [ERROR] pip install failed. Run manually:
        echo         "%PY%" -m pip install requests pyautogui pillow psutil pywin32
        pause
        exit /b 1
    )
)

echo [INFO] Starting main.py %*
echo.
"%PY%" main.py %*
set EXITCODE=%errorlevel%
echo.
echo [INFO] Program exited, code = %EXITCODE%
pause
