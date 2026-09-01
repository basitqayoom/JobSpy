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

    def _route(self) -> str:
        return (self.path.split("?", 1)[0]).rstrip("/") or "/"

    def do_GET(self):
        route = self._route()
        if route == "/api/jobs":
            self._serve_jobs_search()
            return
        if route == "/api/data":
            self._serve_aggregated_data()
            return
        if route == "/api/profiles":
            self._serve_profile_list()
            return
        if route == "/api/companies":
            self._serve_companies()
            return
        if route == "/api/stats":
            self._serve_stats()
            return
        if route == "/api/telegram/status":
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

    def _serve_aggregated_data(self):
        """Read jobs from SQLite (source of truth) and merge with profile metadata."""
        try:
            import db as _db
            profile_root = os.path.join(WEB_DIR, "profiles")
            profile_meta = {}
            latest_ts = ""
            if os.path.isdir(profile_root):
                for fname in sorted(os.listdir(profile_root)):
                    if not fname.endswith(".json"):
                        continue
                    with open(os.path.join(profile_root, fname), encoding="utf-8") as fh:
                        cfg = json.load(fh)
                    profile_meta[fname[:-5]] = cfg

            counts = _db.counts_by_profile()
            profiles = []
            for p, meta in profile_meta.items():
                c = counts.get(p, {})
                # Use latest_seen or generated file if available for freshness
                state_data = os.path.join(WEB_DIR, "state", p, "data.json")
                gen = ""
                if os.path.exists(state_data):
                    try:
                        with open(state_data, encoding="utf-8") as fh:
                            gen = json.load(fh).get("generated_at", "")
                    except Exception:
                        pass
                if gen > latest_ts:
                    latest_ts = gen
                profiles.append({
                    "profile": p,
                    "flag": meta.get("flag", ""),
                    "display_name": meta.get("display_name", p),
                    "location": meta.get("location", ""),
                    "count": c.get("total", 0),
                    "generated_at": gen,
                })

            merged_jobs = []
            for r in _db.list_jobs(order_by="first_seen_at", desc=True):
                p = r["profile"]
                meta = profile_meta.get(p, {})
                merged_jobs.append({
                    "title": r["title"],
                    "company": r["company"],
                    "location": r["location"],
                    "date_posted": r["date_posted"],
                    "job_url": r["url"],
                    "is_priority": bool(r["is_priority"]),
                    "first_seen_at": r["first_seen_at"],
                    "profile": p,
                    "flag": meta.get("flag", ""),
                    "display_name": meta.get("display_name", p),
                })
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {
            "generated_at": latest_ts,
            "profiles": profiles,
            "count": len(merged_jobs),
            "jobs": merged_jobs,
        })

    def _serve_profile_list(self):
        root = os.path.join(WEB_DIR, "profiles")
        out = []
        try:
            for f in sorted(os.listdir(root)):
                if not f.endswith(".json"):
                    continue
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    cfg = json.load(fh)
                out.append({
                    "profile": f[:-5],
                    "flag": cfg.get("flag", ""),
                    "display_name": cfg.get("display_name", f[:-5]),
                    "location": cfg.get("location", ""),
                })
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"profiles": out})

    def _serve_jobs_search(self):
        """GET /api/jobs?q=&profile=&region=&company=&priority=&applied=&saved=&hidden=&posted=&order=&desc=&limit=&offset="""
        import db as _db
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        def _g(k, default=""): v = qs.get(k, [default]); return v[0] if v else default
        def _b(k):
            v = qs.get(k, [None])
            if not v or v[0] is None: return None
            return v[0].lower() in ("1", "true", "yes", "on")
        def _i(k, default):
            try: return int(_g(k, str(default)))
            except: return default
        try:
            rows, total = _db.search(
                q=_g("q").strip(),
                profile=_g("profile").strip(),
                region_group=_g("region").strip().lower(),
                company=_g("company").strip(),
                priority=_b("priority"),
                applied=_b("applied"),
                saved=_b("saved"),
                hidden=_b("hidden"),
                posted_within_hours=(_i("posted", 0) or None),
                order=_g("order", "first_seen_at"),
                desc=(_g("desc", "true").lower() != "false"),
                limit=max(1, min(500, _i("limit", 100))),
                offset=max(0, _i("offset", 0)),
            )
            # Attach flag/display_name from profile files (cached would be nicer).
            profile_meta = {}
            proot = os.path.join(WEB_DIR, "profiles")
            if os.path.isdir(proot):
                for f in os.listdir(proot):
                    if f.endswith(".json"):
                        with open(os.path.join(proot, f), encoding="utf-8") as fh:
                            profile_meta[f[:-5]] = json.load(fh)
            jobs = []
            for r in rows:
                m = profile_meta.get(r["profile"], {})
                jobs.append({
                    "url": r["url"],
                    "title": r["title"],
                    "company": r["company"],
                    "location": r["location"],
                    "date_posted": r["date_posted"],
                    "is_priority": bool(r["is_priority"]),
                    "first_seen_at": r["first_seen_at"],
                    "applied": bool(r.get("applied_at")),
                    "saved": bool(r.get("saved_at")),
                    "hidden": bool(r.get("hidden_at")),
                    "profile": r["profile"],
                    "flag": m.get("flag", ""),
                    "display_name": m.get("display_name", r["profile"]),
                })
            self._send_json(200, {"total": total, "count": len(jobs), "jobs": jobs})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _serve_companies(self):
        import db as _db
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        profile = (qs.get("profile", [""])[0] or "").strip()
        try:
            self._send_json(200, {"companies": _db.distinct_companies(profile=profile)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _serve_stats(self):
        import db as _db
        try:
            self._send_json(200, {
                "totals": _db.stats(),
                "by_profile": _db.counts_by_profile(),
            })
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        if self._route() == "/api/telegram/test":
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

        if self._route() == "/api/config":
            try:
                cfg = sanitize_config(self._read_json_body())
                with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2)
                self._send_json(200, {"ok": True, "config": cfg})
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        # Toggle flags: POST /api/jobs/flag  {url, kind:applied|saved|hidden, value:bool}
        if self._route() == "/api/jobs/flag":
            import db as _db
            try:
                body = self._read_json_body()
                url = (body.get("url") or "").strip()
                kind = (body.get("kind") or "").strip()
                value = bool(body.get("value"))
                col = {"applied": "applied_at", "saved": "saved_at", "hidden": "hidden_at"}.get(kind)
                if not url or not col:
                    self._send_json(400, {"ok": False, "error": "url and kind (applied|saved|hidden) required"})
                    return
                ok = _db.set_flag(url, col, value)
                self._send_json(200, {"ok": ok, "url": url, "kind": kind, "value": value})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if self._route() == "/api/run":
            try:
                body = self._read_json_body() if int(self.headers.get("content-length", 0) or 0) else {}
                profile = (body.get("profile") or "all").strip()
                subprocess.Popen(
                    ["/bin/bash", RUNNER, profile],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._send_json(200, {"ok": True, "started": True, "profile": profile})
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
