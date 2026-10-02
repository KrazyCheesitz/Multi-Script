:: SPDX-License-Identifier: GPL-3.0-or-later
@echo off
setlocal
chcp 65001 >nul
title Multi-Script one-time setup
cd /d "%~dp0"
REM One-time setup. Registers the "terminal" launcher with your browser so the
REM terminal icon in the Multi-Script chat bar can start/stop the bridge itself.
REM After this you never run a launcher script again. Safe to re-run any time.

if not exist "%~dp0runtime\install_native_host.py" (
    echo.
    echo   ERROR: runtime\install_native_host.py not found.
    echo   Extract the WHOLE download first ^(right-click, "Extract All..."^), then run Setup.bat.
    echo.
    pause
    exit /b 1
)

set "PY="
where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; assert sys.version_info>=(3,9)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
        if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
    )
)
if not defined PY (
    echo.
    echo   Python 3.9 or newer is required and was not found.
    echo   Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^),
    echo   then run Setup.bat again.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

call %PY% "%~dp0runtime\install_native_host.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   Setup did not finish ^(code %RC%^). Read the message above, fix it, and run Setup.bat again.
pause
exit /b %RC%
