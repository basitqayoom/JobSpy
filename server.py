"""Tiny dependency-free server for the JobSpy dashboard.

Serves the static files in web/ AND accepts:
  POST /api/config  -> saves web/config.json (the scrape reads it each run)
  POST /api/run     -> triggers run_scrape.sh once in the background

A plain `python -m http.server` can only read files, so this thin wrapper is
what makes the search parameters editable from the page.

Usage:
  python server.py            # http://localhost:8000
  PORT=9000 python server.py
"""

import json
import os
import signal
import socket
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Auto-reap background scrape children so `/api/run` does not leave zombies.
signal.signal(signal.SIGCHLD, signal.SIG_IGN)

try:
    import notify_telegram
except ImportError:  # pragma: no cover
    notify_telegram = None

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
CONFIG_FILE = os.path.join(WEB_DIR, "config.json")
RUNNER = os.path.join(HERE, "run_scrape.sh")

ALLOWED_KEYS = {
    "search_term",
    "location",
    "results_wanted",
    "hours_old",
    "minutes_old",
}


def _coerce_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sanitize_config(raw: dict) -> dict:
    """Keep only known keys and coerce them to safe types."""
    return {
        "search_term": str(raw.get("search_term", "")).strip() or "software engineer",
        "location": str(raw.get("location", "")).strip() or "India",
        "results_wanted": _coerce_int(raw.get("results_wanted")) or 200,
        "hours_old": _coerce_int(raw.get("hours_old")),
        "minutes_old": _coerce_int(raw.get("minutes_old")),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def end_headers(self):
        # Avoid the browser caching data.json/config.json between refreshes.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/api/telegram/status":
            if notify_telegram is None:
                self._send_json(200, {"ok": True, "configured": False, "reason": "module missing"})
                return
            try:
                notify_telegram.load_env()
                self._send_json(200, {"ok": True, **notify_telegram.status()})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.rstrip("/") == "/api/telegram/test":
            if notify_telegram is None:
                self._send_json(500, {"ok": False, "error": "notify_telegram module missing"})
                return
            try:
                notify_telegram.load_env()
                if not notify_telegram.is_configured():
                    self._send_json(400, {"ok": False, "error": "Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env)."})
                    return
                notify_telegram.send_test()
                self._send_json(200, {"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if self.path.rstrip("/") == "/api/config":
            try:
                cfg = sanitize_config(self._read_json_body())
                with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2)
                self._send_json(200, {"ok": True, "config": cfg})
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if self.path.rstrip("/") == "/api/run":
            try:
                subprocess.Popen(
                    ["/bin/bash", RUNNER],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._send_json(200, {"ok": True, "started": True})
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def log_message(self, *args):  # quieter logs
        pass


def _lan_ip() -> str:
    """Best-effort primary LAN IP of this machine (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    # Bind to all interfaces so other devices on the LAN can reach it.
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    lan = _lan_ip()
    print(f"Serving dashboard (Ctrl+C to stop):")
    print(f"  local:   http://localhost:{port}")
    print(f"  network: http://{lan}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
