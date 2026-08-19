"""Search LinkedIn jobs (all companies) with priority companies floated to the top."""

import csv
import json
import os
from datetime import datetime, timezone

from jobspy import scrape_jobs

try:
    import notify_telegram
except ImportError:  # pragma: no cover - module ships alongside this script
    notify_telegram = None

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
JSON_OUTPUT = os.path.join(WEB_DIR, "data.json")
CSV_OUTPUT = os.path.join(HERE, "scrape 3.csv")
CONFIG_FILE = os.path.join(WEB_DIR, "config.json")
FIRST_SEEN_FILE = os.path.join(WEB_DIR, "first_seen.json")
FIRST_SEEN_KEEP_DAYS = 30

# Defaults. These can be overridden from the dashboard, which writes
# web/config.json (read below on every run).
SEARCH_TERM = "software engineer"
LOCATION = "India"
RESULTS_WANTED = 200
HOURS_OLD = 2
MINUTES_OLD = None


def _coerce_int(value):
    """Return an int, or None for blank/None/invalid values."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_config() -> None:
    """Override the search parameters from web/config.json if it exists."""
    global SEARCH_TERM, LOCATION, RESULTS_WANTED, HOURS_OLD, MINUTES_OLD
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    if isinstance(cfg.get("search_term"), str) and cfg["search_term"].strip():
        SEARCH_TERM = cfg["search_term"].strip()
    if isinstance(cfg.get("location"), str) and cfg["location"].strip():
        LOCATION = cfg["location"].strip()

    results = _coerce_int(cfg.get("results_wanted"))
    if results and results > 0:
        RESULTS_WANTED = results

    # hours_old / minutes_old may legitimately be null, so set them as given
    # (only when the key is present in the config).
    if "hours_old" in cfg:
        HOURS_OLD = _coerce_int(cfg.get("hours_old"))
    if "minutes_old" in cfg:
        MINUTES_OLD = _coerce_int(cfg.get("minutes_old"))


load_config()

# Companies to pin at the TOP of the results. No filtering is applied -- every
# company is scraped; jobs whose company name matches any of these are simply
# shown first (in this order), then everyone else follows by most-recent.
PRIORITY_COMPANIES = [
    "Adobe",
    "Microsoft",
    "Amazon",
    "Uber",
    "Apple",
    "Oracle",
    "Salesforce",
    "SAP",
    "LinkedIn",
    "Qualcomm",
    "Snowflake",
    "Databricks",
    "Atlassian",
    "Intuit",
    "Goldman Sachs",
    "JPMorgan",
    "Citi",
    "Barclays",
    "Flipkart",
    "PhonePe",
    "Swiggy",
    "Zerodha",
    "Zoho",
    "Juspay",
    "InMobi",
    "Google",
    "Meta",
    "IBM",
    "Intel",
    "NVIDIA",
    "AMD",
    "Cisco",
    "ServiceNow",
    "Workday",
    "VMware",
    "MongoDB",
    "Elastic",
    "Twilio",
    "Stripe",
    "Nutanix",
    "ThoughtSpot",
    "Palo Alto Networks",
    "CrowdStrike",
    "Texas Instruments",
    "Micron",
    "Samsung",
    "Dell",
    "HPE",
    "Siemens",
    "Bosch",
    "Netflix",
    "Spotify",
    "Airbnb",
    "Morgan Stanley",
    "Deutsche Bank",
    "UBS",
    "Razorpay",
    "Zomato",
    "CRED",
    "Freshworks",
    "Paytm",
    "Meesho",
    "Postman",
    "BrowserStack",
    "Chargebee",
    "Dream11",
    "Groww",
    "Zepto",
    "Nykaa",
    "PolicyBazaar",
    "Ola",
    "Myntra",
    "ShareChat",
    "Ather",
]


def priority_rank(company: str) -> int:
    """Return the index of the first matching priority company, or a large
    number so non-priority companies sort to the bottom."""
    name = (company or "").lower()
    for i, keyword in enumerate(PRIORITY_COMPANIES):
        if keyword.lower() in name:
            return i
    return len(PRIORITY_COMPANIES)


jobs = scrape_jobs(
    site_name="linkedin",
    search_term=SEARCH_TERM,
    location=LOCATION,
    results_wanted=RESULTS_WANTED,
    hours_old=HOURS_OLD,
    minutes_old=MINUTES_OLD,
    linkedin_sort="DD",  # most recent first
    verbose=1,
)

def load_first_seen() -> dict:
    try:
        with open(FIRST_SEEN_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_first_seen(state: dict) -> None:
    os.makedirs(WEB_DIR, exist_ok=True)
    tmp = FIRST_SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, FIRST_SEEN_FILE)


def prune_first_seen(state: dict) -> dict:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=FIRST_SEEN_KEEP_DAYS)
    out = {}
    for k, iso in state.items():
        try:
            when = datetime.fromisoformat(iso)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                out[k] = iso
        except (TypeError, ValueError):
            out[k] = iso
    return out


def job_key(row: dict) -> str:
    return (
        row.get("job_url")
        or f"{row.get('company','')}|{row.get('title','')}|{row.get('location','')}"
    )


def write_json(rows: list, count: int) -> None:
    """Overwrite web/data.json with a fresh list (never merged with old runs)."""
    os.makedirs(WEB_DIR, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "search_term": SEARCH_TERM,
        "location": LOCATION,
        "count": count,
        "jobs": rows,
    }
    with open(JSON_OUTPUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


print(f"Found {len(jobs)} jobs")

rows: list = []
if not jobs.empty:
    # Float priority companies to the top while keeping the existing
    # most-recent order within each group (stable sort).
    jobs["_rank"] = jobs["company"].apply(priority_rank)
    jobs = jobs.sort_values("_rank", kind="stable").reset_index(drop=True)

    for _, job in jobs.iterrows():
        rows.append(
            {
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "date_posted": job.get("date_posted"),
                "job_url": job.get("job_url"),
                "is_priority": bool(job["_rank"] < len(PRIORITY_COMPANIES)),
            }
        )

    # Stamp first_seen_at (persisted across runs so age keeps growing).
    seen = prune_first_seen(load_first_seen())
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    for r in rows:
        key = job_key(r)
        if key not in seen:
            seen[key] = now_iso
        r["first_seen_at"] = seen[key]
    save_first_seen(seen)

    jobs = jobs.drop(columns="_rank")

    print(jobs[["title", "company", "location", "date_posted", "job_url"]].head(10))
    jobs.to_csv(
        CSV_OUTPUT,
        quoting=csv.QUOTE_NONNUMERIC,
        escapechar="\\",
        index=False,
    )
    print(f"Saved to {CSV_OUTPUT}")

# Always overwrite the dashboard data so the page shows a fresh list each run.
write_json(rows, len(rows))
print(f"Saved to {JSON_OUTPUT}")

# --------------------------------------------------------------------------- #
# Telegram notification (priority-company jobs only). Never breaks the scrape.
# --------------------------------------------------------------------------- #
if notify_telegram is not None:
    try:
        notify_telegram.load_env()
        if notify_telegram.is_configured():
            priority_rows = [r for r in rows if r.get("is_priority")]
            other_rows = [r for r in rows if not r.get("is_priority")]
            result = notify_telegram.send_run_notification(
                generated_at=datetime.now(timezone.utc).astimezone().isoformat(),
                search_term=SEARCH_TERM,
                location=LOCATION,
                scraped_count=len(rows),
                priority_jobs=priority_rows,
                other_jobs=other_rows,
            )
            print(f"Telegram: {result}")
        else:
            print("Telegram: not configured (skipping)")
    except Exception as exc:  # noqa: BLE001 - notification failures must not abort the run
        print(f"Telegram: notification failed: {exc}")
