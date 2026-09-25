# -*- coding: utf-8 -*-
"""セットアップウィザードの起動準備をする。

start_wizard.bat / start_wizard.command から呼ばれる。
  1. Python のバージョンを確かめる
  2. 専用の作業場所（.venv）を作る
  3. 必要なライブラリを入れる
  4. ウィザードを起動する

■ なぜ処理をここに置くのか
  Windows の cmd.exe は、バッチファイルをシステムのコードページ（日本語環境なら
  CP932）で読む。UTF-8 で日本語を書いたバッチは文字化けし、化けた行が
  コマンドとして解釈されて壊れる。chcp 65001 を先頭に置く回避策も、
  日本語環境では確実に効くとは限らない。
  そこでバッチ側は ASCII だけに保ち、日本語の案内と実際の処理はこちらで行う。
  Python はどのプラットフォームでも UTF-8 を同じように扱えるので、
  実装が1つで済み、動作を確かめた同じコードが Windows でも走る。

■ なぜ仮想環境（.venv）に入れるのか
  Debian 12以降、Ubuntu 23.04以降、Fedora 36以降、Homebrew の Python では、
  システムの Python への pip install が externally-managed-environment として
  拒否される。--user も同様に拒否される。仮想環境の中はこの制限を受けず、
  パソコンに元から入っている Python にも一切触れない。

このファイルは、古い Python でも「バージョンが足りない」と伝えられるよう、
新しい文法を使わずに書いてある。
"""

import os
import subprocess
import sys

MIN_VERSION = (3, 10)
DOWNLOAD_URL = "https://www.python.org/downloads/"
VENV_DIR = ".venv"


def say(text):
    """コンソールに出力する。文字が出せない環境でも落ちないようにする"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def pause():
    try:
        input("\nEnter キーを押すと閉じます ")
    except (EOFError, KeyboardInterrupt):
        pass


def venv_python(root):
    """作った仮想環境の中の Python の場所を返す"""
    if os.name == "nt":
        return os.path.join(root, VENV_DIR, "Scripts", "python.exe")
    return os.path.join(root, VENV_DIR, "bin", "python")


def run(args, what):
    """外部コマンドを実行する。失敗したら内容を見せて False を返す"""
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except Exception as e:
        say("%s に失敗しました: %s" % (what, e))
        return False
    if result.returncode != 0:
        say("%s に失敗しました。" % what)
        say("")
        # 推測で診断せず、出力をそのまま見せる
        text = (result.stdout or b"").decode("utf-8", "replace").strip()
        for line in text.splitlines()[-15:]:
            say("  " + line)
        return False
    return True


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    say("=" * 60)
    say(" カレンダー空き日程ボット セットアップウィザード")
    say("=" * 60)
    say("")

    # ===== 1. バージョンを確かめる =====
    if sys.version_info < MIN_VERSION:
        say("この Python はバージョンが足りません。")
        say("")
        say("  いま動いている Python : %d.%d.%d"
            % sys.version_info[:3])
        say("  必要なバージョン      : %d.%d 以上" % MIN_VERSION)
        say("")
        say("  %s から新しいものを入れてください。" % DOWNLOAD_URL)
        if os.name == "nt":
            say("  インストール時に「Add python.exe to PATH」に")
            say("  必ずチェックを入れてください。")
        say("")
        say("  入れ直したあと、もう一度ダブルクリックしてください。")
        pause()
        return 1

    say("Python %d.%d.%d を使います。" % sys.version_info[:3])
    say("")

    # ===== 2. 専用の作業場所を用意する =====
    py = venv_python(root)
    if not os.path.exists(py):
        say("初回のみ、専用の作業場所を作ります（1分ほどかかります）…")
        if not run([sys.executable, "-m", "venv", VENV_DIR], "作業場所の作成"):
            say("")
            if os.name != "nt":
                say("Linux の場合、python3-venv パッケージが要ることがあります:")
                say("  sudo apt install python3-venv")
            else:
                say(".venv フォルダがあれば削除してから、もう一度お試しください。")
            pause()
            return 1

    if not os.path.exists(py):
        say("作業場所が壊れています。.venv フォルダを削除してやり直してください。")
        pause()
        return 1

    # ===== 3. ライブラリを入れる =====
    # 毎回実行しても、入っていれば数秒で終わる
    say("必要なライブラリを確認しています…")
    run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        "pip の更新")          # 失敗しても続行してよい
    if not run([py, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"],
               "ライブラリの取得"):
        say("")
        say("インターネットに繋がっているか確認して、もう一度お試しください。")
        pause()
        return 1

    say("準備ができました。")
    say("")

    # ===== 4. ウィザードを起動する =====
    try:
        return subprocess.call([py, "setup_wizard.py"])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
    pause()
