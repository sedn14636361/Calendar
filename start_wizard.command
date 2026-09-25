#!/bin/bash
# セットアップウィザードを起動します（macOS / Linux）
#
# このファイルをダブルクリックすると、
#   1. Python を探す
#   2. 専用の作業場所（.venv）を作る
#   3. 必要なライブラリを入れる
#   4. ウィザードをブラウザで開く
# までを順に行います。ターミナルを開く必要はありません。
#
# 「.venv」という専用の場所に入れるので、パソコンに元から入っている
# Python には一切触れません。消したいときは .venv フォルダを削除するだけです。

# このファイルが置かれている場所に移動する。
# dirname などの外部コマンドを使わない。使えない環境だと
# cd "" が成功扱いになり、別の場所で動き続けてしまうため。
SELF="${BASH_SOURCE[0]:-$0}"
case "$SELF" in
    */*) cd "${SELF%/*}" || exit 1 ;;
esac

echo "============================================================"
echo " カレンダー空き日程ボット セットアップウィザード"
echo "============================================================"
echo

# ===== 1. Python を探す =====
# 3.10 以上が必要（google-api-python-client などが要求するため）
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "Python 3.10 以上が見つかりませんでした。"
    echo
    echo "  https://www.python.org/downloads/ からインストールしてください。"
    echo "  インストール後、このファイルをもう一度ダブルクリックしてください。"
    echo
    if command -v python3 >/dev/null 2>&1; then
        echo "  （いま入っている Python: $(python3 --version 2>&1)）"
    fi
    echo
    read -r -p "Enter キーを押すと閉じます "
    exit 1
fi

echo "Python が見つかりました: $("$PY" --version 2>&1)"
echo

# ===== 2. 専用の作業場所を用意する =====
if [ ! -d ".venv" ]; then
    echo "初回のみ、専用の作業場所を作ります（1分ほどかかります）…"
    if ! "$PY" -m venv .venv; then
        echo
        echo "作業場所の作成に失敗しました。"
        echo "Linux の場合、python3-venv パッケージが必要なことがあります:"
        echo "  sudo apt install python3-venv"
        echo
        read -r -p "Enter キーを押すと閉じます "
        exit 1
    fi
fi

VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "作業場所が壊れています。.venv フォルダを削除してからやり直してください。"
    read -r -p "Enter キーを押すと閉じます "
    exit 1
fi

# ===== 3. ライブラリを入れる =====
# 毎回実行しても、入っていれば数秒で終わる
echo "必要なライブラリを確認しています…"
if ! "$VENV_PY" -m pip install --quiet --upgrade pip 2>/dev/null; then
    echo "（pip の更新はできませんでしたが、続行します）"
fi
if ! "$VENV_PY" -m pip install --quiet -r requirements.txt; then
    echo
    echo "ライブラリの取得に失敗しました。"
    echo "インターネットに繋がっているか確認して、もう一度お試しください。"
    echo
    read -r -p "Enter キーを押すと閉じます "
    exit 1
fi
echo "準備ができました。"
echo

# ===== 4. ウィザードを起動 =====
"$VENV_PY" setup_wizard.py

echo
read -r -p "Enter キーを押すと閉じます "
