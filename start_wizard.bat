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
rem
rem  A candidate is accepted only if it can actually create a file.
rem  Windows ships stub python.exe / python3.exe in WindowsApps that
rem  print "Python was not found..." and open the Microsoft Store.
rem  Their exit code is not dependable, so testing errorlevel alone
rem  can mistake a stub for a working interpreter. A stub cannot run
rem  code, so it cannot create the marker file.
rem ---------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "MARKER=%TEMP%\calendar_bot_pycheck.tmp"
set "PY="

call :try "py -3"
if not defined PY call :try "python"
if not defined PY call :try "python3"
if not defined PY call :try "py"

if not defined PY goto :nopython

%PY% setup_launcher.py
if errorlevel 1 pause
exit /b 0


rem ----- test one candidate -----
:try
set "CAND=%~1"
if exist "%MARKER%" del /q "%MARKER%" >nul 2>&1
%CAND% -c "open(r'%MARKER%','w').write('ok')" >nul 2>&1
if exist "%MARKER%" set "PY=%CAND%"
if exist "%MARKER%" del /q "%MARKER%" >nul 2>&1
exit /b 0


rem ----- nothing usable was found -----
:nopython
echo.
echo   A working Python was not found on this computer.
echo.
echo   Please install Python 3.10 or newer from:
echo       https://www.python.org/downloads/
echo.
echo   During installation, tick "Add python.exe to PATH".
echo   Then double-click this file again.
echo.
echo   If typing "python" opens the Microsoft Store, Windows is
echo   using a placeholder instead of a real Python. Turn it off at:
echo       Settings ^> Apps ^> Advanced app settings
echo               ^> App execution aliases
echo   and switch off both "python.exe" and "python3.exe".
echo.
pause
exit /b 1
