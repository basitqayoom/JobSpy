"""SQLite storage layer for JobSpy.

Schema:
    jobs (
        url TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        company TEXT,
        title TEXT,
        location TEXT,
        date_posted TEXT,
        is_priority INTEGER NOT NULL DEFAULT 0,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        sent_at TEXT
    )
    indexes: (profile), (first_seen_at DESC), (company), (is_priority)

`sent_at` NULL means "not yet Telegram-acknowledged" (superset of pending state).
`last_seen_at` bumps every scrape so we can detect stale/removed listings.

All rows are keyed by URL (LinkedIn URL is the natural PK). Same URL never
changes profile once assigned; if LinkedIn returns it in a different region
later, we keep the original profile (first-seen wins).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "web", "jobs.db")

_lock = threading.Lock()
_connections: dict[int, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    url            TEXT PRIMARY KEY,
    profile        TEXT NOT NULL,
    company        TEXT,
    title          TEXT,
    location       TEXT,
    date_posted    TEXT,
    is_priority    INTEGER NOT NULL DEFAULT 0,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    sent_at        TEXT,
    applied_at     TEXT,
    saved_at       TEXT,
    hidden_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_profile      ON jobs(profile);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen   ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company      ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_is_priority  ON jobs(is_priority);
CREATE INDEX IF NOT EXISTS idx_jobs_sent_at      ON jobs(sent_at);
CREATE INDEX IF NOT EXISTS idx_jobs_applied      ON jobs(applied_at);
CREATE INDEX IF NOT EXISTS idx_jobs_saved        ON jobs(saved_at);
CREATE INDEX IF NOT EXISTS idx_jobs_hidden       ON jobs(hidden_at);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    url UNINDEXED, title, company, location,
    content='jobs', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, url, title, company, location)
    VALUES (new.rowid, new.url, new.title, new.company, new.location);
END;
CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, url, title, company, location)
    VALUES ('delete', old.rowid, old.url, old.title, old.company, old.location);
END;
CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, url, title, company, location)
    VALUES ('delete', old.rowid, old.url, old.title, old.company, old.location);
    INSERT INTO jobs_fts(rowid, url, title, company, location)
    VALUES (new.rowid, new.url, new.title, new.company, new.location);
END;

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def _connect() -> sqlite3.Connection:
    tid = threading.get_ident()
    conn = _connections.get(tid)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _connections[tid] = conn
    return conn


@contextmanager
def tx():
    conn = _connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def upsert_jobs(profile: str, rows: list[dict]) -> tuple[int, int]:
    """Insert new jobs and bump last_seen for existing.

    Returns (inserted, updated).
    """
    if not rows:
        return (0, 0)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    inserted = 0
    updated = 0
    with tx() as conn:
        for r in rows:
            url = r.get("job_url") or r.get("url")
            if not url:
                continue
            cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (url,))
            existing = cur.fetchone()
            if existing:
                conn.execute(
                    "UPDATE jobs SET last_seen_at = ?, title = COALESCE(?, title), location = COALESCE(?, location), date_posted = COALESCE(?, date_posted) WHERE url = ?",
                    (now, r.get("title"), r.get("location"), r.get("date_posted"), url),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO jobs (url, profile, company, title, location, date_posted, is_priority, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        url, profile, r.get("company"), r.get("title"), r.get("location"),
                        r.get("date_posted"), 1 if r.get("is_priority") else 0, now, now,
                    ),
                )
                inserted += 1
    return (inserted, updated)


def mark_sent(urls: list[str]) -> int:
    if not urls:
        return 0
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with tx() as conn:
        conn.executemany("UPDATE jobs SET sent_at = ? WHERE url = ? AND sent_at IS NULL", [(now, u) for u in urls])
        cur = conn.execute("SELECT changes()")
        return cur.fetchone()[0]


def prune(days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with tx() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE first_seen_at < ?", (cutoff,))
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def list_jobs(*, profile: str | None = None, sent: bool | None = None,
              priority: bool | None = None, since_iso: str | None = None,
              order_by: str = "first_seen_at", desc: bool = True,
              limit: int | None = None) -> list[dict]:
    q = "SELECT * FROM jobs WHERE 1=1"
    args: list = []
    if profile:
        q += " AND profile = ?"; args.append(profile)
    if sent is True:
        q += " AND sent_at IS NOT NULL"
    elif sent is False:
        q += " AND sent_at IS NULL"
    if priority is True:
        q += " AND is_priority = 1"
    elif priority is False:
        q += " AND is_priority = 0"
    if since_iso:
        q += " AND first_seen_at >= ?"; args.append(since_iso)
    q += f" ORDER BY {order_by} {'DESC' if desc else 'ASC'}"
    if limit:
        q += " LIMIT ?"; args.append(limit)
    with _lock:
        conn = _connect()
        return _rows_to_dicts(conn.execute(q, args).fetchall())


def sent_urls_set() -> set[str]:
    with _lock:
        conn = _connect()
        return {r["url"] for r in conn.execute("SELECT url FROM jobs WHERE sent_at IS NOT NULL").fetchall()}


def counts_by_profile() -> dict[str, dict]:
    q = """
        SELECT profile,
               COUNT(*)                                       AS total,
               SUM(CASE WHEN is_priority = 1 THEN 1 ELSE 0 END) AS priority,
               SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END) AS pending
        FROM jobs
        GROUP BY profile
    """
    with _lock:
        conn = _connect()
        out: dict[str, dict] = {}
        for r in conn.execute(q).fetchall():
            out[r["profile"]] = {"total": r["total"], "priority": r["priority"] or 0, "pending": r["pending"] or 0}
        return out


def stats() -> dict:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent, SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END) AS pending FROM jobs").fetchone()
        return {"total": row["total"] or 0, "sent": row["sent"] or 0, "pending": row["pending"] or 0}


def pending_by_profile(priority: bool | None = None) -> dict[str, list[dict]]:
    """Return {profile: [rows]} for jobs not yet Telegram-acknowledged."""
    q = "SELECT * FROM jobs WHERE sent_at IS NULL"
    args: list = []
    if priority is True:
        q += " AND is_priority = 1"
    elif priority is False:
        q += " AND is_priority = 0"
    q += " ORDER BY first_seen_at DESC"
    out: dict[str, list[dict]] = {}
    with _lock:
        conn = _connect()
        for r in conn.execute(q, args).fetchall():
            out.setdefault(r["profile"], []).append(dict(r))
    return out


def row_to_job_dict(r: dict) -> dict:
    """Adapter: DB row -> legacy job dict shape used by notify_telegram."""
    return {
        "title": r.get("title"),
        "company": r.get("company"),
        "location": r.get("location"),
        "date_posted": r.get("date_posted"),
        "job_url": r.get("url"),
        "is_priority": bool(r.get("is_priority")),
        "first_seen_at": r.get("first_seen_at"),
        "profile": r.get("profile"),
    }


def set_flag(url: str, column: str, on: bool) -> bool:
    """Toggle applied_at / saved_at / hidden_at atomically."""
    if column not in {"applied_at", "saved_at", "hidden_at"}:
        raise ValueError(f"invalid column: {column}")
    now = datetime.now(timezone.utc).astimezone().isoformat() if on else None
    with tx() as conn:
        cur = conn.execute(f"UPDATE jobs SET {column} = ? WHERE url = ?", (now, url))
        return cur.rowcount > 0


def search(*, q: str = "", profile: str = "", region_group: str = "",
           company: str = "", priority: bool | None = None,
           applied: bool | None = None, saved: bool | None = None,
           hidden: bool | None = None, posted_within_hours: int | None = None,
           order: str = "first_seen_at", desc: bool = True,
           limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    """Filtered paginated search. Returns (rows, total_matching).

    - q: full-text over title/company/location (FTS5, MATCH). Empty = no filter.
    - region_group: 'india' or 'global' shortcut.
    - profile: exact profile match.
    - Booleans: filter by presence/absence of *_at.
    """
    where = ["1=1"]
    args: list = []
    joins = ""

    if q:
        # FTS5 MATCH; escape quotes and wrap loose tokens with prefix search.
        clean = " ".join(t + "*" for t in q.replace('"', ' ').split() if t)
        joins = "JOIN jobs_fts ON jobs_fts.rowid = jobs.rowid"
        where.append("jobs_fts MATCH ?"); args.append(clean)

    if profile:
        where.append("jobs.profile = ?"); args.append(profile)

    if region_group == "india":
        where.append("jobs.profile = 'india'")
    elif region_group == "global":
        where.append("jobs.profile != 'india'")

    if company:
        where.append("LOWER(jobs.company) LIKE ?"); args.append(f"%{company.lower()}%")

    if priority is True:
        where.append("jobs.is_priority = 1")
    elif priority is False:
        where.append("jobs.is_priority = 0")

    for col, flag in (("applied_at", applied), ("saved_at", saved), ("hidden_at", hidden)):
        if flag is True:
            where.append(f"jobs.{col} IS NOT NULL")
        elif flag is False:
            where.append(f"jobs.{col} IS NULL")

    if posted_within_hours:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=posted_within_hours)).isoformat()
        where.append("jobs.first_seen_at >= ?"); args.append(cutoff)

    order_col = {
        "first_seen_at": "jobs.first_seen_at",
        "date_posted":   "jobs.date_posted",
        "company":       "jobs.company",
        "is_priority":   "jobs.is_priority",
    }.get(order, "jobs.first_seen_at")
    direction = "DESC" if desc else "ASC"

    where_sql = " AND ".join(where)
    count_sql = f"SELECT COUNT(*) FROM jobs {joins} WHERE {where_sql}"
    data_sql  = (f"SELECT jobs.* FROM jobs {joins} WHERE {where_sql} "
                 f"ORDER BY jobs.is_priority DESC, {order_col} {direction} "
                 f"LIMIT ? OFFSET ?")

    with _lock:
        conn = _connect()
        total = conn.execute(count_sql, args).fetchone()[0]
        rows = _rows_to_dicts(conn.execute(data_sql, args + [limit, offset]).fetchall())
    return rows, total


def distinct_companies(*, profile: str = "", limit: int = 500) -> list[str]:
    q = "SELECT DISTINCT company FROM jobs"
    args: list = []
    if profile:
        q += " WHERE profile = ?"
        args.append(profile)
    q += " ORDER BY company"
    with _lock:
        conn = _connect()
        return [r[0] for r in conn.execute(q, args).fetchall() if r[0]][:limit]
