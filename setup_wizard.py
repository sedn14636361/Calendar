# -*- coding: utf-8 -*-
"""セットアップウィザード。自分のPC上でだけ動く小さなWebアプリ。

    python3 setup_wizard.py

ブラウザが開き、手順を1つずつ進めながら、その場で値が本当に使えるかを
確かめられる。検証は setup_checks.py（ボット本体と同じライブラリを使う）に任せる。

■ なぜローカルにサーバーを立てるのか
  Discord API はブラウザからの CORS リクエストに対応していないため、
  静的なHTMLだけでは実際に接続して確かめることができない。Google の
  サービスアカウント方式も、秘密鍵でJWTに署名する必要があり、ページ内で
  扱うべきものではない。そこで検証だけをこのプロセスに担当させる。

■ 秘密情報の扱い（設計の中心）
  1. このプロセスは秘密情報を一切保存しない。ファイルにもログにも書かない。
  2. サーバーは状態を持たない。リクエストごとに値を受け取り、検証し、捨てる。
     プロセス内に秘密情報が溜まらないので、漏れる面がそれだけ小さい。
  3. 環境変数から値を読み込む場合も、読むだけで保存しない。
  4. 設定ファイルはブラウザ内で組み立てて保存する。このプロセスは
     設定値を受け取りもしないし、ファイルも作らない。
  5. 待ち受けは 127.0.0.1 のみ。同じネットワークの他のマシンからは触れない。
  6. 起動ごとにランダムな鍵を発行し、全リクエストで照合する。同じPC上の
     無関係なページやプロセスからAPIを叩かれても弾く。
  7. Host ヘッダを検証する（DNS rebinding 対策）。
  8. CORS を一切許可しない。他オリジンのページは応答を読めない。
  9. 秘密情報はすべてPOSTの本文で送る。URLに乗せない（履歴やログに残るため）。
 10. 一定時間操作がなければ自動で終了する。起動したまま放置されるのを防ぐ。
"""

import asyncio
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import setup_checks

BASE_DIR = Path(__file__).resolve().parent
WIZARD_HTML = BASE_DIR / "wizard.html"

HOST = "127.0.0.1"                     # 外部からは絶対に触れさせない
IDLE_TIMEOUT_SEC = 30 * 60             # 無操作が続いたら自動終了
MAX_BODY_BYTES = 256 * 1024            # 鍵JSONを含むので少し余裕を見る

# 起動ごとに変わる鍵。URLに載せてブラウザへ渡し、以後の全リクエストで照合する
SESSION_KEY = secrets.token_urlsafe(32)

# 受け取る値の名前。これ以外は無視する
ENV_NAMES = ["DISCORD_BOT_TOKEN", "CHANNEL_ID", "CALENDAR_ID", "SERVICE_ACCOUNT_JSON"]

_last_activity = time.time()
_shutdown = threading.Event()


def _touch():
    global _last_activity
    _last_activity = time.time()


# ===== リクエストの検証 =====

def _key_ok(handler):
    """このウィザードが発行した鍵を持つリクエストか、定数時間で照合する"""
    given = handler.headers.get("X-Setup-Key", "")
    return secrets.compare_digest(given, SESSION_KEY)


def _host_ok(handler, port):
    """Host ヘッダが自分自身であることを確かめる。
       外部ドメインを 127.0.0.1 に向ける攻撃（DNS rebinding）を防ぐ"""
    host = (handler.headers.get("Host") or "").strip()
    return host in {f"127.0.0.1:{port}", f"localhost:{port}",
                    f"[::1]:{port}"}


def _origin_ok(handler, port):
    """他サイトのページから叩かれていないことを確かめる。
       Origin が無い（通常のページ遷移）か、自分自身のときだけ通す"""
    origin = handler.headers.get("Origin")
    if origin is None:
        return True
    return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


# ===== 応答 =====

SECURITY_HEADERS = {
    # 外部リソースを一切読み込ませない。ウィザードは自己完結している
    "Content-Security-Policy": (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # 秘密情報を含む応答をディスクキャッシュに残さない
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CalendarSetupWizard"
    sys_version = ""

    # --- ログ ---
    def log_message(self, fmt, *args):
        """既定のアクセスログを無効にする。
           秘密情報はPOST本文に限っているが、ログ自体を残さないのが確実"""
        pass

    # --- 送信の共通処理 ---
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        # CORS ヘッダは意図的に送らない（他オリジンに応答を読ませない）
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _deny(self, code=403, message="このリクエストは受け付けられません"):
        self._json(code, {"error": message})

    # --- CORS プリフライトには応じない ---
    def do_OPTIONS(self):
        self._deny(405, "許可されていません")

    # ===== GET：画面と画像だけ =====
    def do_GET(self):
        port = self.server.server_address[1]
        if not _host_ok(self, port):
            return self._deny(421, "Host が不正です")

        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            # 画面本体。鍵はここで初めてブラウザに渡す
            _touch()
            if not WIZARD_HTML.exists():
                return self._send(500, "wizard.html が見つかりません",
                                  "text/plain; charset=utf-8")
            html = WIZARD_HTML.read_text(encoding="utf-8")
            html = html.replace("__SETUP_KEY__", SESSION_KEY)
            return self._send(200, html, "text/html; charset=utf-8")

        return self._deny(404, "見つかりません")

    # ===== POST：検証 =====
    def do_POST(self):
        port = self.server.server_address[1]
        if not _host_ok(self, port):
            return self._deny(421, "Host が不正です")
        if not _origin_ok(self, port):
            return self._deny(403, "Origin が不正です")
        if not _key_ok(self):
            return self._deny(403, "鍵が一致しません。ウィザードを開き直してください")

        _touch()
        path = self.path.split("?", 1)[0]

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "Content-Length が不正です")
        if length > MAX_BODY_BYTES:
            return self._deny(413, "データが大きすぎます")

        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._deny(400, "JSONとして読めませんでした")

        # 受け取った値は、このリクエストの処理が終われば参照されない。
        # サーバー側に保存する処理はどこにも無い。
        try:
            return self._dispatch(path, data)
        except Exception as e:
            # 例外メッセージに入力値を載せない
            return self._json(500, {"error": f"検証中にエラーが発生しました: "
                                             f"{type(e).__name__}"})

    def _dispatch(self, path, data):
        if path == "/api/ping":
            return self._json(200, {
                "ok": True,
                "python": f"{sys.version_info.major}.{sys.version_info.minor}"
                          f".{sys.version_info.micro}",
                "has_discord": setup_checks.HAS_DISCORD,
                "has_google": setup_checks.HAS_GOOGLE,
            })

        if path == "/api/env":
            # 既に環境変数が設定されている場合に読み込む。読むだけで保存しない
            found = {n: bool(os.environ.get(n, "").strip()) for n in ENV_NAMES}
            if not data.get("load"):
                return self._json(200, {"found": found})
            return self._json(200, {
                "found": found,
                "values": {n: os.environ.get(n, "") for n in ENV_NAMES},
            })

        if path == "/api/check/service-account":
            result = setup_checks.check_service_account_json(
                data.get("service_account_json", ""))
            return self._json(200, {
                "result": result,
                "client_email": setup_checks.client_email_of(
                    data.get("service_account_json", "")),
            })

        if path == "/api/check/calendars":
            sa = data.get("service_account_json", "")
            ids = setup_checks.split_calendar_ids(data.get("calendar_id", ""))
            if not ids:
                return self._json(200, {"results": [{
                    "calendar_id": "",
                    **setup_checks.ng("カレンダーIDが入力されていません",
                                      fixes=["カレンダーIDを1つ以上入力してください"])}]})
            return self._json(200, {
                "results": setup_checks.check_calendars(
                    sa, ids, setup_checks.client_email_of(sa))})

        if path == "/api/check/discord":
            results = asyncio.run(setup_checks.check_discord(
                data.get("discord_bot_token", ""),
                data.get("channel_id") if data.get("with_channel") else None,
                send_test=bool(data.get("send_test"))))
            return self._json(200, {"results": results})

        if path == "/api/check/all":
            env = {n: data.get(n, "") for n in ENV_NAMES}
            report = asyncio.run(setup_checks.check_all(
                env, send_test=bool(data.get("send_test"))))
            return self._json(200, report)

        if path == "/api/quit":
            self._json(200, {"ok": True})
            _shutdown.set()
            return None

        return self._deny(404, "見つかりません")


# ===== 自動終了の監視 =====

def _idle_watchdog():
    while not _shutdown.wait(10):
        if time.time() - _last_activity > IDLE_TIMEOUT_SEC:
            print(f"\n{IDLE_TIMEOUT_SEC // 60}分間操作がなかったため終了します。")
            _shutdown.set()
            return


# ===== 起動 =====

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="セットアップウィザードをブラウザで開きます。"
                    "入力した値はこのPCの外に出ず、保存もされません。")
    parser.add_argument("--port", type=int, default=0,
                        help="使うポート番号（既定は空いているポートを自動選択）")
    parser.add_argument("--no-browser", action="store_true",
                        help="ブラウザを自動で開かない")
    args = parser.parse_args()

    if not WIZARD_HTML.exists():
        print(f"エラー: {WIZARD_HTML} が見つかりません。"
              "リポジトリの中で実行してください。")
        return 1

    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?k={SESSION_KEY}"

    missing = []
    if not setup_checks.HAS_DISCORD:
        missing.append("discord.py")
    if not setup_checks.HAS_GOOGLE:
        missing.append("google-api-python-client / google-auth")

    print("=" * 64)
    print(" カレンダー空き日程ボット セットアップウィザード")
    print("=" * 64)
    print(f" 次のURLをブラウザで開いてください:\n\n   {url}\n")
    print(" ・待ち受けは 127.0.0.1 のみです。他のマシンからは接続できません。")
    print(" ・入力した値は保存されません。ファイルにもログにも書きません。")
    print(f" ・{IDLE_TIMEOUT_SEC // 60}分間操作がないと自動で終了します。")
    print(" ・終了するには Ctrl+C を押してください。")
    if missing:
        print()
        print(" ※ 次のライブラリが見つかりません。形式チェックのみで動作します:")
        print(f"     {', '.join(missing)}")
        print("   すべて検証するには: pip install -r requirements.txt")
    print("=" * 64)

    threading.Thread(target=_idle_watchdog, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass                       # 開けなくてもURLは表示済み

    try:
        while not _shutdown.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
