"""One-time migration: JSON state -> SQLite (web/jobs.db).

Merges:
  - web/state/<profile>/data.json         (current jobs)
  - web/state/<profile>/first_seen.json   (historical first-seen timestamps)
  - web/state/<profile>/pending_priority.json + pending_other.json  (unsent queue)
  - web/telegram_sent.json                (dedupe / sent_at)

Safe to re-run: uses INSERT OR IGNORE + UPDATE. Existing DB rows are preserved.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import db

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE = os.path.join(WEB, "state")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _profiles() -> list[str]:
    if not os.path.isdir(STATE):
        return []
    return sorted(d for d in os.listdir(STATE) if os.path.isdir(os.path.join(STATE, d)))


def migrate() -> dict:
    sent = _load(os.path.join(WEB, "telegram_sent.json"))
    stats = {"profiles": {}, "total_inserted": 0, "total_sent_marked": 0}

    for profile in _profiles():
        pdir = os.path.join(STATE, profile)
        first_seen = _load(os.path.join(pdir, "first_seen.json"))
        data = _load(os.path.join(pdir, "data.json"))
        pri_pending = _load(os.path.join(pdir, "pending_priority.json"))
        oth_pending = _load(os.path.join(pdir, "pending_other.json"))

        # Build canonical row set for this profile: union of first_seen keys
        # and any current data.json/pending entries. first_seen holds keys that
        # aren't valid URLs (composite fallback keys) -- skip those.
        seen_urls: dict[str, str] = {u: iso for u, iso in first_seen.items() if u.startswith("http")}

        # Prefer richest source for job metadata: data.json > pending_* > first_seen alone.
        rich: dict[str, dict] = {}
        for j in data.get("jobs", []):
            url = j.get("job_url")
            if url and url.startswith("http"):
                rich[url] = j
        for src in (pri_pending, oth_pending):
            for url, j in src.items():
                if url.startswith("http"):
                    rich.setdefault(url, j)

        rows = []
        now_iso = datetime.now(timezone.utc).astimezone().isoformat()
        for url in set(list(seen_urls.keys()) + list(rich.keys())):
            j = rich.get(url, {})
            rows.append({
                "job_url": url,
                "title": j.get("title"),
                "company": j.get("company"),
                "location": j.get("location"),
                "date_posted": j.get("date_posted"),
                "is_priority": bool(j.get("is_priority", False)),
                "first_seen_at": seen_urls.get(url) or j.get("first_seen_at") or now_iso,
            })

        # Insert (with correct first_seen).
        # upsert_jobs stamps first_seen_at = now for inserts; override by direct SQL.
        inserted = 0
        with db.tx() as conn:
            for r in rows:
                cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (r["job_url"],))
                if cur.fetchone():
                    continue
                conn.execute(
                    "INSERT INTO jobs (url, profile, company, title, location, date_posted, is_priority, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["job_url"], profile, r.get("company"), r.get("title"), r.get("location"),
                     r.get("date_posted"), 1 if r["is_priority"] else 0, r["first_seen_at"], r["first_seen_at"]),
                )
                inserted += 1

        stats["profiles"][profile] = {"first_seen": len(seen_urls), "inserted": inserted}
        stats["total_inserted"] += inserted

    # Mark sent_at for anything already delivered.
    with db.tx() as conn:
        marked = 0
        for url, iso in sent.items():
            if not url.startswith("http"):
                continue
            cur = conn.execute("UPDATE jobs SET sent_at = ? WHERE url = ? AND sent_at IS NULL", (iso, url))
            if cur.rowcount:
                marked += cur.rowcount
    stats["total_sent_marked"] = marked
    return stats


if __name__ == "__main__":
    print("Migrating JSON -> SQLite...")
    result = migrate()
    print(json.dumps(result, indent=2))
    s = db.stats()
    print(f"\nDB now has: {s['total']} rows ({s['sent']} sent, {s['pending']} pending)")
