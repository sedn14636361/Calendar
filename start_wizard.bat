@echo off
rem セットアップウィザードを起動します（Windows）
rem
rem このファイルをダブルクリックすると、
rem   1. Python を探す
rem   2. 専用の作業場所（.venv）を作る
rem   3. 必要なライブラリを入れる
rem   4. ウィザードをブラウザで開く
rem までを順に行います。コマンドプロンプトを開く必要はありません。
rem
rem 「.venv」という専用の場所に入れるので、パソコンに元から入っている
rem Python には一切触れません。消したいときは .venv フォルダを削除するだけです。

chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ============================================================
echo  カレンダー空き日程ボット セットアップウィザード
echo ============================================================
echo.

rem ===== 1. Python を探す（3.10 以上が必要）=====
set "PY="
for %%C in ("py -3" "python3" "python") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%~C"
    )
)

if not defined PY (
    echo Python 3.10 以上が見つかりませんでした。
    echo.
    echo   https://www.python.org/downloads/ からインストールしてください。
    echo   インストール時に「Add python.exe to PATH」に必ずチェックを入れてください。
    echo   インストール後、このファイルをもう一度ダブルクリックしてください。
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo Python が見つかりました: %%V
echo.

rem ===== 2. 専用の作業場所を用意する =====
if not exist ".venv\Scripts\python.exe" (
    echo 初回のみ、専用の作業場所を作ります（1分ほどかかります）...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo 作業場所の作成に失敗しました。
        echo .venv フォルダがあれば削除してから、もう一度お試しください。
        echo.
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo 作業場所が壊れています。.venv フォルダを削除してからやり直してください。
    pause
    exit /b 1
)

rem ===== 3. ライブラリを入れる（入っていれば数秒で終わる）=====
echo 必要なライブラリを確認しています...
"%VENV_PY%" -m pip install --quiet --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo ライブラリの取得に失敗しました。
    echo インターネットに繋がっているか確認して、もう一度お試しください。
    echo.
    pause
    exit /b 1
)
echo 準備ができました。
echo.

rem ===== 4. ウィザードを起動 =====
"%VENV_PY%" setup_wizard.py

echo.
pause
