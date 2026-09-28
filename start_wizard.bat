@echo off
rem ---------------------------------------------------------------
rem  Calendar bot - setup wizard launcher (Windows)
rem  Double-click this file to start the setup wizard.
rem ---------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "MARKER=%TEMP%\calendar_bot_pycheck_%RANDOM%%RANDOM%.tmp"
set "PYCHECK=import os,sys;sys.version_info[0]>=3 and open(os.environ['MARKER'],'w').write('ok')"
set "PY="

del /q "%MARKER%" >nul 2>&1
py -3 -c "%PYCHECK%" >nul 2>&1
if exist "%MARKER%" set "PY=py -3"

if not defined PY del /q "%MARKER%" >nul 2>&1
if not defined PY python -c "%PYCHECK%" >nul 2>&1
if not defined PY if exist "%MARKER%" set "PY=python"

if not defined PY del /q "%MARKER%" >nul 2>&1
if not defined PY python3 -c "%PYCHECK%" >nul 2>&1
if not defined PY if exist "%MARKER%" set "PY=python3"

if not defined PY del /q "%MARKER%" >nul 2>&1
if not defined PY py -c "%PYCHECK%" >nul 2>&1
if not defined PY if exist "%MARKER%" set "PY=py"

del /q "%MARKER%" >nul 2>&1

if defined PY %PY% setup\setup_launcher.py --no-pause

if not defined PY echo.
if not defined PY echo   A working Python was not found on this computer.
if not defined PY echo.
if not defined PY echo   Please install Python 3.10 or newer from:
if not defined PY echo       https://www.python.org/downloads/
if not defined PY echo.
if not defined PY echo   During installation, tick "Add python.exe to PATH".
if not defined PY echo   Then double-click this file again.
if not defined PY echo.
if not defined PY echo   If typing "python" opens the Microsoft Store, Windows is
if not defined PY echo   using a placeholder instead of a real Python. Turn it off at:
if not defined PY echo       Settings ^> Apps ^> Advanced app settings
if not defined PY echo               ^> App execution aliases
if not defined PY echo   and switch off both "python.exe" and "python3.exe".

echo.
pause
exit /b 0
