# -*- coding: utf-8 -*-
"""セットアップウィザードの起動準備をする。

start_wizard.bat / start_wizard.command から呼ばれる。
  1. 配布物が壊れていないか確かめる
  2. Python のバージョンを確かめる
  3. 専用の作業場所（.venv）を作る
  4. 必要なライブラリを入れる
  5. ウィザードを起動する

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

■ なぜログを残すのか
  「窓が閉じるだけで何も起きない」という状態からは原因が追えない。
  この段階の出力を setup_log.txt に残しておけば、そのファイル1枚で調べられる。
  ログはウィザードを起動する直前で閉じる。ウィザードは起動URLに
  セッション鍵を含めて表示するため、そこまで記録すると鍵がディスクに残り、
  「入力した値はファイルにもログにも書かない」という設計に反する。

このファイルは、古い Python でも「バージョンが足りない」と伝えられるよう、
新しい文法を使わずに書いてある。
"""

import os
import subprocess
import sys

MIN_VERSION = (3, 10)
DOWNLOAD_URL = "https://www.python.org/downloads/"
VENV_DIR = ".venv"
LOG_NAME = "setup_log.txt"

# バッチ側から --no-pause で呼ばれる。バッチが最後に必ず pause するので、
# こちらでも待つと Enter を2回押すことになる。
NO_PAUSE = "--no-pause" in sys.argv[1:]

_log_lines = []


def say(text):
    """コンソールに出力し、同じ内容をログにも残す"""
    _log_lines.append(text)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def write_log(root):
    """ここまでの出力をファイルに残す。秘密情報は通らない段階のみ"""
    try:
        path = os.path.join(root, LOG_NAME)
        f = open(path, "w", encoding="utf-8", errors="replace")
        try:
            f.write("セットアップランチャーの記録\n")
            f.write("このファイルには、Python の検出・仮想環境の作成・"
                    "ライブラリの導入までの出力だけが入ります。\n")
            f.write("トークンや秘密鍵は、この段階では一切扱いません。\n")
            f.write("=" * 60 + "\n")
            for line in _log_lines:
                f.write(line + "\n")
        finally:
            f.close()
        return path
    except Exception:
        return None            # ログが書けないことで本題を止めない


def pause():
    if NO_PAUSE:
        return
    try:
        input("\nEnter キーを押すと閉じます ")
    except (EOFError, KeyboardInterrupt):
        pass


def check_launcher_bytes(root):
    """配布物の改行コードを確かめる。

    cmd.exe はバッチファイルの行をバイト位置で追うため、CRLF でないと
    行の途中で切れた断片が実行される。この確認は推測を挟まずに済む:
    ファイルのバイト列を数えるだけで、壊れているかどうかが決まる。
    戻り値は (CRLFの数, 単独LFの数)、読めなければ None。
    """
    path = os.path.join(root, "start_wizard.bat")
    try:
        f = open(path, "rb")
        try:
            data = f.read()
        finally:
            f.close()
    except (OSError, IOError):
        return None
    return (data.count(b"\r\n"), data.replace(b"\r\n", b"").count(b"\n"))


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
    # 調べるときに必要な事実を最初に記録する
    say("実行している Python : %s" % sys.executable)
    say("バージョン          : %s" % sys.version.replace("\n", " "))
    say("フォルダ            : %s" % root)
    say("")

    # ===== 1. 配布物が壊れていないか =====
    counts = check_launcher_bytes(root)
    if counts is None:
        say("start_wizard.bat が見つかりません。")
        say("ZIP の中身を、フォルダごとすべて取り出しているか確認してください。")
        say("")
    else:
        crlf, lone_lf = counts
        say("start_wizard.bat の改行 : CRLF %d 箇所 / 単独LF %d 箇所"
            % (crlf, lone_lf))
        if lone_lf > 0:
            say("")
            say("！ このダウンロードは古い、または改行コードが壊れています。")
            say("   Windows の cmd.exe は行をバイト位置で追うため、この状態だと")
            say("   2つの行の断片がつながった命令が実行されます。ダブルクリック")
            say("   しても窓が閉じるだけ、という症状になります。")
            say("")
            say("   GitHub から ZIP を取り直してください。")
            say("   古いフォルダは .venv ごと削除してから展開してください。")
        say("")

    # ===== 2. バージョンを確かめる =====
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
        return 1

    # ===== 3. 専用の作業場所を用意する =====
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
            return 1

    if not os.path.exists(py):
        say("作業場所が壊れています。.venv フォルダを削除してやり直してください。")
        return 1

    # ===== 4. ライブラリを入れる =====
    # 毎回実行しても、入っていれば数秒で終わる
    say("必要なライブラリを確認しています…")
    run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        "pip の更新")          # 失敗しても続行してよい
    if not run([py, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"],
               "ライブラリの取得"):
        say("")
        say("インターネットに繋がっているか確認して、もう一度お試しください。")
        return 1

    say("準備ができました。")
    say("")

    # ===== 5. ウィザードを起動する =====
    # ここでログを閉じる。これより後はセッション鍵を含む出力が出るため。
    path = write_log(root)
    if path:
        say("（ここまでの記録: %s）" % LOG_NAME)
    say("")
    try:
        return subprocess.call([py, "setup_wizard.py"])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    code = main()
    if code:
        # 失敗して終わるときは、その理由までを記録に残す
        root = os.path.dirname(os.path.abspath(__file__))
        if write_log(root):
            say("")
            say("この内容は %s にも保存しました。" % LOG_NAME)
        pause()
        sys.exit(code)
    pause()
