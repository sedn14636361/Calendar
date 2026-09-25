#!/bin/bash
# セットアップウィザードを起動します（macOS / Linux）
#
# このファイルをダブルクリックすると、必要なライブラリの導入から
# ウィザードの起動までを自動で行います。ターミナルを開く必要はありません。
#
# 実際の処理は setup_launcher.py にあります。Windows 用の
# start_wizard.bat と同じものを呼ぶので、動作は両方で揃います。

# このファイルが置かれている場所に移動する。
# dirname などの外部コマンドを使わない。使えない環境だと
# cd "" が成功扱いになり、別の場所で動き続けてしまうため。
SELF="${BASH_SOURCE[0]:-$0}"
case "$SELF" in
    */*) cd "${SELF%/*}" || exit 1 ;;
esac

for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        "$candidate" setup_launcher.py
        exit $?
    fi
done

echo
echo "  Python がこのパソコンに入っていないようです。"
echo
echo "  https://www.python.org/downloads/ から 3.10 以上を入れてください。"
echo "  入れたあと、このファイルをもう一度ダブルクリックしてください。"
echo
read -r -p "Enter キーを押すと閉じます "
exit 1
