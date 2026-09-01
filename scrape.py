"""Multi-profile LinkedIn scraper.

Usage: python scrape.py <profile_name>

Loads web/profiles/<name>.json, writes state under web/state/<name>/,
and queues jobs into pending_priority.json / pending_other.json for the
delivery workers to flush.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from jobspy import scrape_jobs

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PROFILES_DIR = os.path.join(WEB_DIR, "profiles")
STATE_ROOT = os.path.join(WEB_DIR, "state")
KEEP_DAYS = 30


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _prune_by_iso(state: dict, iso_getter=lambda v: v) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    out = {}
    for k, v in state.items():
        try:
            iso = iso_getter(v)
            when = datetime.fromisoformat(iso)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                out[k] = v
        except (TypeError, ValueError, AttributeError):
            out[k] = v
    return out


import re


def _priority_rank(company: str, priority_list: list[str]) -> int:
    """Match on word boundaries so 'SAP' doesn't hit 'Publicis Sapient'."""
    name = (company or "").lower()
    for i, kw in enumerate(priority_list):
        pattern = r"\b" + re.escape(kw.lower()) + r"\b"
        if re.search(pattern, name):
            return i
    return len(priority_list)


def scrape_profile(profile_name: str) -> dict:
    cfg_path = os.path.join(PROFILES_DIR, f"{profile_name}.json")
    cfg = _load_json(cfg_path)
    if not cfg:
        raise SystemExit(f"Profile not found: {cfg_path}")

    state_dir = os.path.join(STATE_ROOT, profile_name)
    os.makedirs(state_dir, exist_ok=True)
    data_path = os.path.join(state_dir, "data.json")
    first_seen_path = os.path.join(state_dir, "first_seen.json")
    pending_pri_path = os.path.join(state_dir, "pending_priority.json")
    pending_oth_path = os.path.join(state_dir, "pending_other.json")
    csv_path = os.path.join(state_dir, "latest.csv")

    display = f"{cfg.get('flag','')} {cfg.get('display_name', profile_name)}"
    print(f"[{profile_name}] scraping {display}  ·  location={cfg['location']}  ·  minutes_old={cfg.get('minutes_old')}")

    jobs = scrape_jobs(
        site_name="linkedin",
        search_term=cfg["search_term"],
        location=cfg["location"],
        results_wanted=cfg.get("results_wanted", 1000),
        hours_old=cfg.get("hours_old"),
        minutes_old=cfg.get("minutes_old"),
        linkedin_sort="DD",
        verbose=1,
    )
    print(f"[{profile_name}] scraped {len(jobs)} jobs")

    rows: list[dict] = []
    if not jobs.empty:
        priority_list = cfg.get("priority_companies", [])
        jobs["_rank"] = jobs["company"].apply(lambda c: _priority_rank(c, priority_list))
        jobs = jobs.sort_values("_rank", kind="stable").reset_index(drop=True)

        for _, job in jobs.iterrows():
            rows.append({
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "date_posted": job.get("date_posted"),
                "job_url": job.get("job_url"),
                "is_priority": bool(job["_rank"] < len(priority_list)),
                "profile": profile_name,
            })

        # First-seen tracking
        first_seen = _prune_by_iso(_load_json(first_seen_path))
        now_iso = datetime.now(timezone.utc).astimezone().isoformat()
        for r in rows:
            key = r.get("job_url") or f"{r['company']}|{r['title']}|{r['location']}"
            if key not in first_seen:
                first_seen[key] = now_iso
            r["first_seen_at"] = first_seen[key]
        _save_json(first_seen_path, first_seen)

        jobs = jobs.drop(columns="_rank")
        jobs.to_csv(csv_path, quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)

    # Persist snapshot for the dashboard
    _save_json(data_path, {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "profile": profile_name,
        "flag": cfg.get("flag", ""),
        "display_name": cfg.get("display_name", profile_name),
        "search_term": cfg["search_term"],
        "location": cfg["location"],
        "count": len(rows),
        "jobs": rows,
    })

    # Merge into pending queues (dedupe by job_url).
    def _merge(path: str, entries: list[dict]) -> int:
        pending = _prune_by_iso(_load_json(path), iso_getter=lambda v: v.get("first_seen_at") if isinstance(v, dict) else v)
        for r in entries:
            url = r.get("job_url")
            if not url:
                continue
            existing = pending.get(url, {})
            merged = dict(existing)
            merged.update(r)
            if existing.get("first_seen_at"):
                merged["first_seen_at"] = existing["first_seen_at"]
            pending[url] = merged
        _save_json(path, pending)
        return len(pending)

    pri_rows = [r for r in rows if r["is_priority"]]
    oth_rows = [r for r in rows if not r["is_priority"]]
    n_pri = _merge(pending_pri_path, pri_rows)
    n_oth = _merge(pending_oth_path, oth_rows)
    print(f"[{profile_name}] pending queues: priority={n_pri}  other={n_oth}")

    return {
        "profile": profile_name,
        "display": display,
        "scraped": len(rows),
        "priority_new": len(pri_rows),
        "other_new": len(oth_rows),
        "pending_priority": n_pri,
        "pending_other": n_oth,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scrape.py <profile_name>")
    result = scrape_profile(sys.argv[1])
    print(f"[{sys.argv[1]}] done: {result}")
