@echo off
rem ---------------------------------------------------------------
rem  Calendar bot - setup wizard launcher (Windows)
rem
rem  This file is intentionally ASCII only.
rem  cmd.exe reads batch files using the system ANSI code page
rem  (CP932 on Japanese Windows), so UTF-8 Japanese text here would be
rem  mangled and the broken lines would be run as commands.
rem  All messages and all the real work live in setup_launcher.py,
rem  which handles UTF-8 the same way on every platform.
rem ---------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    python3 --version >nul 2>&1
    if not errorlevel 1 set "PY=python3"
)

if not defined PY (
    echo.
    echo   Python was not found on this computer.
    echo.
    echo   Please install Python 3.10 or newer from:
    echo       https://www.python.org/downloads/
    echo.
    echo   During installation, tick "Add python.exe to PATH".
    echo   Then double-click this file again.
    echo.
    pause
    exit /b 1
)

%PY% setup_launcher.py
if errorlevel 1 pause
exit /b 0
