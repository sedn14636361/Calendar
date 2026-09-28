#!/bin/bash
# セットアップウィザードを起動します（macOS / Linux）
#
# このファイルをダブルクリックすると、必要なライブラリの導入から
# ウィザードの起動までを自動で行います。ターミナルを開く必要はありません。

SELF="${BASH_SOURCE[0]:-$0}"
case "$SELF" in
    */*) cd "${SELF%/*}" || exit 1 ;;
esac

MARKER="${TMPDIR:-/tmp}/calendar_bot_pycheck.$$.$RANDOM"
PYCHECK="import os,sys;sys.version_info[0]>=3 and open(os.environ['MARKER'],'w').write('ok')"
PY=""
for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    rm -f "$MARKER"
    MARKER="$MARKER" "$candidate" -c "$PYCHECK" >/dev/null 2>&1
    if [ -f "$MARKER" ]; then
        PY="$candidate"
        rm -f "$MARKER"
        break
    fi
done
rm -f "$MARKER"

if [ -n "$PY" ]; then
    "$PY" setup/setup_launcher.py
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
