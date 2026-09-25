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

# 候補が「本当にコードを実行できるか」で判定する。
# 終了コードだけを見ると、名前だけ存在して中身が無いものを
# 動く Python と取り違えることがある（Windows の
# Microsoft Store 誘導スタブがこれにあたる）。
# 実際にファイルを作らせれば、その取り違えは起きない。
MARKER="${TMPDIR:-/tmp}/calendar_bot_pycheck.$$"
PY=""
for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    rm -f "$MARKER"
    "$candidate" -c "open(r'$MARKER','w').write('ok')" >/dev/null 2>&1
    if [ -f "$MARKER" ]; then
        PY="$candidate"
        rm -f "$MARKER"
        break
    fi
done
rm -f "$MARKER"

if [ -n "$PY" ]; then
    "$PY" setup_launcher.py
    exit $?
fi

echo
echo "  動作する Python がこのパソコンに見つかりませんでした。"
echo
echo "  https://www.python.org/downloads/ から 3.10 以上を入れてください。"
echo "  入れたあと、このファイルをもう一度ダブルクリックしてください。"
echo
read -r -p "Enter キーを押すと閉じます "
exit 1
