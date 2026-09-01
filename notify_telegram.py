"""Telegram notifier for JobSpy.

Sends a summary message + one or more digest messages containing the newly
discovered priority-company jobs for each scrape run.

Secrets/config come from environment variables (or a .env file in the project
root). No third-party dependencies -- uses stdlib urllib.

Usage:
    from notify_telegram import send_run_notification, load_env
    load_env()
    send_run_notification(summary, priority_jobs)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
STATE_FILE = os.path.join(HERE, "web", "telegram_sent.json")

MAX_MSG_CHARS = 3800          # Telegram hard limit is 4096; leave headroom.
SEND_DELAY_S = 0.4            # Gentle rate-limit spacing.
DEDUPE_KEEP_DAYS = 30         # How long to remember sent jobs.
MAX_OTHER_LISTED = 20         # Cap on non-priority job lines per digest.





# --------------------------------------------------------------------------- #
# Environment / configuration
# --------------------------------------------------------------------------- #
def load_env(path: str = ENV_FILE) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file (no shell expansion).

    Existing environment variables are NOT overwritten -- CI / systemd env
    always wins.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _config() -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    disabled = os.environ.get("TELEGRAM_DISABLED", "").strip() in {"1", "true", "yes"}
    dashboard = os.environ.get("DASHBOARD_URL", "").strip()
    return {
        "token": token,
        "chat_id": chat_id,
        "disabled": disabled,
        "dashboard": dashboard,
        "configured": bool(token and chat_id),
        # Feed bots per region
        "india_token":  os.environ.get("TELEGRAM_INDIA_BOT_TOKEN", "").strip(),
        "india_chat_id": os.environ.get("TELEGRAM_INDIA_CHAT_ID", "").strip(),
        "global_token": os.environ.get("TELEGRAM_GLOBAL_BOT_TOKEN", "").strip(),
        "global_chat_id": os.environ.get("TELEGRAM_GLOBAL_CHAT_ID", "").strip(),
    }


def feed_bot(region: str) -> tuple[str, str]:
    """Return (token, chat_id) for a feed bot. region='india' or anything else."""
    cfg = _config()
    if region == "india":
        return (cfg["india_token"], cfg["india_chat_id"])
    return (cfg["global_token"], cfg["global_chat_id"])


def send_via(token: str, chat_id: str, text: str, *, disable_preview: bool = True, silent: bool = False) -> dict:
    """Direct send via a specific bot/chat pair (bypasses the DM bot)."""
    if not token or not chat_id:
        raise RuntimeError("feed bot not configured")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
        "disable_notification": "true" if silent else "false",
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    import urllib.parse, urllib.request, urllib.error
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode())
        except Exception:
            j = {"description": str(e)}
        if e.code == 429 and isinstance(j.get("parameters"), dict):
            import time as _t
            _t.sleep(int(j["parameters"].get("retry_after", 1)) + 1)
            return send_via(token, chat_id, text, disable_preview=disable_preview, silent=silent)
        raise RuntimeError(f"Telegram HTTP {e.code}: {j.get('description', '')}") from e


def is_configured() -> bool:
    return _config()["configured"]


def status() -> dict:
    """Return a public-safe status blob for the dashboard."""
    c = _config()
    return {
        "configured": c["configured"],
        "disabled": c["disabled"],
        "chat_id": c["chat_id"] if c["configured"] else "",
        "token_tail": ("…" + c["token"][-4:]) if c["token"] else "",
        "dashboard": c["dashboard"],
        "sent_total": _sent_count(),
    }


# --------------------------------------------------------------------------- #
# Dedupe state
# --------------------------------------------------------------------------- #
# Note: dedupe state now lives in web/jobs.db (see db.py). These shims stay
# for legacy callers (status endpoint, send_run_notification tests).
def _load_sent() -> dict:
    try:
        import db as _db
        return {u: "" for u in _db.sent_urls_set()}
    except Exception:
        return {}


def _save_sent(_sent: dict) -> None:
    # No-op: DB persists via db.mark_sent() in delivery.py
    return None


def _prune(sent: dict) -> dict:
    return sent  # DB is pruned by db.prune() on a schedule


def _sent_count() -> int:
    try:
        import db as _db
        return _db.stats()["sent"]
    except Exception:
        return 0


def _job_key(job: dict) -> str:
    return (
        job.get("job_url")
        or f"{job.get('company','')}|{job.get('title','')}|{job.get('location','')}"
    )


# --------------------------------------------------------------------------- #
# Formatting (HTML parse_mode)
# --------------------------------------------------------------------------- #
def _esc(s) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _rel_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        try:
            d = datetime.strptime(str(iso), "%Y-%m-%d")
        except ValueError:
            return str(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc).date() - d.date()).days
    if days <= 0:
        rel = "Today"
    elif days == 1:
        rel = "Yesterday"
    elif days < 7:
        rel = f"{days}d ago"
    else:
        rel = d.strftime("%d %b")
    return f"{rel} · {d.strftime('%d %b %Y')}"


def _short_location(loc: str) -> str:
    """Return just the city (first comma segment) to keep lines short."""
    if not loc:
        return ""
    return loc.split(",")[0].strip()


def _short_rel(iso: str) -> str:
    """Compact relative date: 'Today', 'Yesterday', '3d ago', or 'DD MMM'."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        try:
            d = datetime.strptime(str(iso), "%Y-%m-%d")
        except ValueError:
            return str(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc).date() - d.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days}d ago"
    return d.strftime("%d %b")


def _short_title(title: str, limit: int = 60) -> str:
    """Trim overly-long titles so they don't wrap into 3+ lines on mobile."""
    t = (title or "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _short_company(company: str, limit: int = 22) -> str:
    """Trim very long company names for the header line."""
    c = (company or "").strip()
    return c if len(c) <= limit else c[: limit - 1].rstrip() + "…"


def _format_job_card_body(job: dict, index: int = 0, *, priority: bool) -> str:
    """Layout one job with sharp typographic hierarchy.

    Priority (brand-first):
        \u2b50 COMPANY NAME
        Senior Frontend Engineer   (link)
        Bangalore \u00b7 Today     (italic meta)

    Other (role-first):
        Senior Frontend Engineer   (bold link)
        Zoho \u00b7 Chennai \u00b7 Today

    `index` is accepted for backward-compat but no longer rendered.
    """
    company_raw = _short_company(job.get("company") or "?")
    company = _esc(company_raw)
    title = _esc(_short_title(job.get("title") or "?"))
    loc = _short_location(job.get("location") or "")
    when = _short_rel(job.get("date_posted") or "")
    url = job.get("job_url")

    lines: list[str] = []
    if priority:
        lines.append(f"\u2b50 <b>{company.upper()}</b>")
        if url:
            lines.append(f'<a href="{_esc(url)}">{title}</a>')
        else:
            lines.append(title)
        meta_bits = [b for b in (loc, when) if b]
        if meta_bits:
            lines.append(f"<i>{_esc(' \u00b7 '.join(meta_bits))}</i>")
    else:
        if url:
            lines.append(f'<a href="{_esc(url)}"><b>{title}</b></a>')
        else:
            lines.append(f"<b>{title}</b>")
        meta_parts = [b for b in (company_raw, loc, when) if b]
        if meta_parts:
            first, *rest = meta_parts
            if rest:
                lines.append(f"{_esc(first)} <i>\u00b7 {_esc(' \u00b7 '.join(rest))}</i>")
            else:
                lines.append(_esc(first))
    return "\n".join(lines)


def format_job_card(job: dict, index: int = 0, *, priority: bool = False) -> str:
    """Render a single job as a distinct Telegram <blockquote> card."""
    return f"<blockquote>{_format_job_card_body(job, index, priority=priority)}</blockquote>"


def _unique_companies(jobs: list[dict], limit: int = 3) -> tuple[str, int]:
    """Return ('Amazon, Microsoft', extra_count) for the preview headline."""
    seen = []
    for j in jobs:
        c = (j.get("company") or "").strip()
        if c and c not in seen:
            seen.append(c)
        if len(seen) >= limit + 1:
            break
    if len(seen) <= limit:
        return ", ".join(seen), 0
    return ", ".join(seen[:limit]), len(seen) - limit


DIVIDER = "─" * 14   # lighter horizontal rule between sections (was heavy ━x20)
CARD_SEP = "· " * 6   # airy dotted rule between cards inside expandable box


def build_digest(
    *,
    generated_at: str,
    search_term: str,
    location: str,
    scraped_count: int,
    new_priority: list[dict],
    new_other: list[dict],
    skipped_priority: int = 0,
    skipped_other: int = 0,
    dashboard: str = "",
) -> list[str]:
    """Return one (or more) HTML messages, laid out for scannability.

    Layout:
        🔔 PREVIEW LINE (companies + counts)   <-- notification tray shows this
        location · timestamp
        ──────────
        ⭐ PRIORITY (N)
        [blockquote card per job]
        ──────────
        🆕 OTHER (M)
        [blockquote card per job]
        (optional context + dashboard footer)
    """
    try:
        dt = datetime.fromisoformat(generated_at) if generated_at else datetime.now()
    except ValueError:
        dt = datetime.now()
    when = dt.strftime("%d %b %H:%M").strip()
    loc = location or ""

    n_pri = len(new_priority)
    n_oth = len(new_other)

    # ---- Notification preview line (this is what shows in the phone tray) ----
    # Keep this line short: mobile lock-screen previews truncate around ~60 chars.
    total_new = n_pri + n_oth

    header: list[str] = []
    if total_new == 0:
        header.append("<b>No new jobs</b>")
    else:
        header.append(f"<b>🔔 {total_new} new job{'s' if total_new != 1 else ''}</b>")
        if n_pri:
            pri_cos, pri_extra = _unique_companies(new_priority, limit=2)
            line = f"⭐ <b>{n_pri} priority</b>"
            if pri_cos:
                line += f"  —  {_esc(pri_cos)}"
                if pri_extra:
                    line += f" +{pri_extra}"
            header.append(line)
        if n_oth:
            header.append(f"🆕 <b>{n_oth} other</b>")
    header.append(f"<i>{_esc(when)}</i>")
    header.append("")

    # ---- Optional footer (context + dashboard link) ----
    context_bits = [f"Scraped {scraped_count}"]
    skipped_total = skipped_priority + skipped_other
    if skipped_total:
        context_bits.append(f"{skipped_total} already sent")
    context = f"<i>{_esc(' · '.join(context_bits))}</i>"

    other_listed = list(new_other[:MAX_OTHER_LISTED])
    other_overflow = max(0, n_oth - len(other_listed))

    # Approx footer length (for chunking budget)
    footer_text_len = len(context)
    if dashboard:
        footer_text_len += len(f"\nDashboard → {dashboard}")

    messages: list[str] = []
    current: list[str] = list(header)
    current_len = sum(len(x) + 1 for x in current)

    def flush():
        nonlocal current, current_len
        messages.append("\n".join(current).rstrip())
        cont = f"⭐ <b>continued</b>  ·  <i>{_esc(loc)} · {_esc(when)}</i>"
        current = [cont, ""]
        current_len = sum(len(x) + 1 for x in current)

    def add(line: str, allow_split: bool = True):
        nonlocal current_len
        add_len = len(line) + 1
        if allow_split and current_len + add_len + footer_text_len > MAX_MSG_CHARS and len(current) > len(header):
            flush()
        current.append(line)
        current_len += add_len

    def emit_cards(jobs: list[dict], *, priority: bool):
        """Emit each card with a hard visual gap between them."""
        for idx, job in enumerate(jobs, 1):
            add("", allow_split=False)
            add(format_job_card(job, idx, priority=priority))

    if new_priority:
        add(f"⭐ <b>PRIORITY</b>  ·  {n_pri} job{'s' if n_pri != 1 else ''}", allow_split=False)
        add(DIVIDER, allow_split=False)
        emit_cards(new_priority, priority=True)

    if other_listed:
        add("", allow_split=False)
        add(f"🆕 <b>OTHER</b>  ·  {n_oth} job{'s' if n_oth != 1 else ''}", allow_split=False)
        add(DIVIDER, allow_split=False)

        # First card always visible; overflow tucked into an expandable card.
        if len(other_listed) <= 3:
            emit_cards(other_listed, priority=False)
            if other_overflow:
                add(f"<i>… +{other_overflow} more · see dashboard</i>")
        else:
            first = other_listed[0]
            rest = other_listed[1:]
            add("", allow_split=False)
            add(format_job_card(first, 1, priority=False))
            add("", allow_split=False)
            rest_bodies = [
                _format_job_card_body(job, i + 2, priority=False)
                for i, job in enumerate(rest)
            ]
            # Dotted separator inside the expandable box between rest cards.
            expandable_body = f"\n\n{CARD_SEP}\n\n".join(rest_bodies)
            if other_overflow:
                expandable_body += f"\n\n<i>… +{other_overflow} more · see dashboard</i>"
            add(
                f"<blockquote expandable>👇 <b>+{len(rest)} more</b>\n\n{expandable_body}</blockquote>",
                allow_split=False,
            )

    # Footer on final message only
    current.append("")
    current.append(DIVIDER)
    if dashboard:
        current.append(f'📊 <a href="{_esc(dashboard)}">Open dashboard</a>  ·  {context}')
    else:
        current.append(context)
    messages.append("\n".join(current).rstrip())
    return messages


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _post(method: str, payload: dict, timeout: int = 15) -> dict:
    cfg = _config()
    if not cfg["configured"]:
        raise RuntimeError("Telegram is not configured (missing token or chat id).")
    url = f"https://api.telegram.org/bot{cfg['token']}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            j = json.loads(body)
        except ValueError:
            j = {"ok": False, "description": body}
        # Honor rate-limit retry_after.
        if e.code == 429 and isinstance(j.get("parameters"), dict):
            retry = int(j["parameters"].get("retry_after", 1))
            time.sleep(retry + 1)
            return _post(method, payload, timeout=timeout)
        raise RuntimeError(f"Telegram HTTP {e.code}: {j.get('description', body)}") from e


def _send_to(chat_id: str, text: str, *, disable_preview: bool, silent: bool) -> dict:
    return _post("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
        "disable_notification": "true" if silent else "false",
    })


def send_message(
    text: str,
    *,
    disable_preview: bool = True,
    silent: bool = False,
) -> dict:
    """Send a Telegram message to the configured DM chat."""
    cfg = _config()
    return _send_to(cfg["chat_id"], text, disable_preview=disable_preview, silent=silent)


def send_test() -> dict:
    now = datetime.now().strftime("%d %b %H:%M")
    text = (
        f"⭐ <b>Test</b> · JobSpy notifications — {_esc(now)}\n"
        "If you can read this, priority-job alerts will land here after each scrape. ✅"
    )
    return send_message(text)


# --------------------------------------------------------------------------- #
# High-level API
# --------------------------------------------------------------------------- #
def send_run_notification(
    *,
    generated_at: str,
    search_term: str,
    location: str,
    scraped_count: int,
    priority_jobs: Iterable[dict],
    other_jobs: Iterable[dict] | None = None,
) -> dict:
    """Send a combined digest of new priority + other jobs. Returns a stats dict."""
    cfg = _config()
    priority_jobs = list(priority_jobs)
    other_jobs = list(other_jobs or [])
    total_priority = len(priority_jobs)
    total_other = len(other_jobs)

    if not cfg["configured"]:
        return {"sent": False, "reason": "not-configured"}
    if cfg["disabled"]:
        return {"sent": False, "reason": "disabled"}

    sent_state = _prune(_load_sent())
    new_pri = [j for j in priority_jobs if _job_key(j) not in sent_state]
    new_oth = [j for j in other_jobs if _job_key(j) not in sent_state]
    skipped_pri = total_priority - len(new_pri)
    skipped_oth = total_other - len(new_oth)

    # SILENT SCRAPES: nothing new in either bucket = no ping.
    if not new_pri and not new_oth:
        _save_sent(sent_state)
        return {
            "sent": False, "reason": "nothing-new",
            "new_priority": 0, "new_other": 0,
            "skipped_priority": skipped_pri, "skipped_other": skipped_oth,
        }

    messages = build_digest(
        generated_at=generated_at,
        search_term=search_term,
        location=location,
        scraped_count=scraped_count,
        new_priority=new_pri,
        new_other=new_oth,
        skipped_priority=skipped_pri,
        skipped_other=skipped_oth,
        dashboard=cfg["dashboard"],
    )

    # First message pings; any continuation messages are silent.
    for i, m in enumerate(messages):
        send_message(m, silent=(i > 0))
        time.sleep(SEND_DELAY_S)

    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    for j in new_pri + new_oth:
        sent_state[_job_key(j)] = now_iso
    _save_sent(sent_state)

    return {
        "sent": True,
        "new_priority": len(new_pri),
        "new_other": len(new_oth),
        "skipped_priority": skipped_pri,
        "skipped_other": skipped_oth,
        "messages": len(messages),
    }
