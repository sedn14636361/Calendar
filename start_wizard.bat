@echo off
rem ---------------------------------------------------------------
rem  Calendar bot - setup wizard launcher (Windows)
rem
rem  Three deliberate constraints keep this file working:
rem
rem  1. ASCII only.
rem     cmd.exe reads batch files using the system ANSI code page
rem     (CP932 on Japanese Windows), so UTF-8 Japanese text here would
rem     be mangled and the broken lines would be run as commands.
rem     All messages and all the real work live in setup_launcher.py,
rem     which handles UTF-8 the same way on every platform.
rem
rem  2. No labels, no goto, no call, no parenthesised blocks.
rem     Those constructs make cmd.exe seek around the file by byte
rem     offset. If the line endings are ever damaged (an LF-only copy,
rem     an editor that rewrites them), the seek lands mid-line and the
rem     window closes after one unreadable error. A flat list of simple
rem     commands has nothing to seek to, so it degrades visibly instead.
rem     This file must stay CRLF; .gitattributes stores it byte-exact.
rem
rem  3. pause on every path.
rem     A launcher that closes without saying anything tells the user
rem     nothing and tells us nothing either. setup_launcher.py is asked
rem     not to pause (--no-pause) so there is exactly one prompt.
rem
rem  A candidate interpreter is accepted only if it can actually create
rem  a file. Windows ships stub python.exe / python3.exe in WindowsApps
rem  that print "Python was not found..." and open the Microsoft Store.
rem  Their exit code is not dependable, so testing errorlevel alone can
rem  mistake a stub for a working interpreter. A stub cannot run code,
rem  so it cannot create the marker file.
rem ---------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "MARKER=%TEMP%\calendar_bot_pycheck.tmp"
set "PY="

rem py is tried first: the App execution alias does not replace it.
del /q "%MARKER%" >nul 2>&1
py -3 -c "open(r'%MARKER%','w').write('ok')" >nul 2>&1
if exist "%MARKER%" set "PY=py -3"

if not defined PY del /q "%MARKER%" >nul 2>&1
if not defined PY python -c "open(r'%MARKER%','w').write('ok')" >nul 2>&1
if not defined PY if exist "%MARKER%" set "PY=python"

if not defined PY del /q "%MARKER%" >nul 2>&1
if not defined PY python3 -c "open(r'%MARKER%','w').write('ok')" >nul 2>&1
if not defined PY if exist "%MARKER%" set "PY=python3"

if not defined PY del /q "%MARKER%" >nul 2>&1
if not defined PY py -c "open(r'%MARKER%','w').write('ok')" >nul 2>&1
if not defined PY if exist "%MARKER%" set "PY=py"

del /q "%MARKER%" >nul 2>&1

if defined PY %PY% setup_launcher.py --no-pause

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
