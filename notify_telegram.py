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
GROUP_MAX_FAILURES = 5        # Auto-disable group send after this many consecutive failures.

# Runtime state (tracks group-send health so we don't spam scrape.log).
_group_state = {"failures": 0, "disabled": False, "last_error": ""}


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
    group_chat_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    disabled = os.environ.get("TELEGRAM_DISABLED", "").strip() in {"1", "true", "yes"}
    dashboard = os.environ.get("DASHBOARD_URL", "").strip()
    return {
        "token": token,
        "chat_id": chat_id,
        "group_chat_id": group_chat_id,
        "disabled": disabled,
        "dashboard": dashboard,
        "configured": bool(token and chat_id),
    }


def is_configured() -> bool:
    return _config()["configured"]


def status() -> dict:
    """Return a public-safe status blob for the dashboard."""
    c = _config()
    return {
        "configured": c["configured"],
        "disabled": c["disabled"],
        "chat_id": c["chat_id"] if c["configured"] else "",
        "group_chat_id": c["group_chat_id"] if c["group_chat_id"] else "",
        "group_disabled": _group_state["disabled"],
        "group_failures": _group_state["failures"],
        "group_last_error": _group_state["last_error"],
        "token_tail": ("…" + c["token"][-4:]) if c["token"] else "",
        "dashboard": c["dashboard"],
        "sent_total": _sent_count(),
    }


# --------------------------------------------------------------------------- #
# Dedupe state
# --------------------------------------------------------------------------- #
def _load_sent() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save_sent(sent: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sent, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _prune(sent: dict) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUPE_KEEP_DAYS)
    pruned = {}
    for key, iso in sent.items():
        try:
            when = datetime.fromisoformat(iso)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                pruned[key] = iso
        except (TypeError, ValueError):
            # Keep unparseable entries -- safer than dropping.
            pruned[key] = iso
    return pruned


def _sent_count() -> int:
    return len(_load_sent())


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


def _format_job_card_body(job: dict, index: int, *, priority: bool) -> str:
    """Layout one job with real typographic hierarchy.

        ⭐ 1                       (small marker line)
        COMPANY NAME                (BOLD — the ONLY strong element)
        Job Title                   (plain, natural wrap)
        City · Today                (italic, muted)

        Apply on LinkedIn →        (link on its own line — clear CTA)
    """
    marker = "⭐" if priority else "•"
    company = _esc(_short_company(job.get("company") or "?"))
    title = _esc(_short_title(job.get("title") or "?"))
    loc = _short_location(job.get("location") or "")
    when = _short_rel(job.get("date_posted") or "")

    lines: list[str] = []
    # Small marker + index (visual breadcrumb, not competing with company)
    lines.append(f"{marker} {index}")
    # Company: THE anchor line. Bold + all caps.
    lines.append(f"<b>{company.upper()}</b>")
    # Title: plain text so the company still wins visual weight.
    lines.append(title)
    # Meta: italic to recede.
    meta_bits = [b for b in (loc, when) if b]
    if meta_bits:
        lines.append(f"<i>{_esc(' · '.join(meta_bits))}</i>")
    url = job.get("job_url")
    if url:
        # Blank line before the CTA gives the link visual breathing room.
        lines.append("")
        lines.append(f'▶️ <a href="{_esc(url)}">Apply on LinkedIn →</a>')
    return "\n".join(lines)


def format_job_card(job: dict, index: int, *, priority: bool = False) -> str:
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


DIVIDER = "━" * 20  # heavy horizontal rule between sections
CARD_SEP = "┈" * 18  # dotted rule between individual cards (lighter than DIVIDER)


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
    if n_pri:
        pri_cos, pri_extra = _unique_companies(new_priority, limit=2)
        pri_bit = f"⭐ <b>{n_pri} priority</b>"
        if pri_cos:
            pri_bit += f": {_esc(pri_cos)}"
            if pri_extra:
                pri_bit += f" +{pri_extra}"
    else:
        pri_bit = ""

    if n_oth:
        oth_bit = f"+<b>{n_oth} other</b>"
    else:
        oth_bit = ""

    preview_parts = [b for b in (pri_bit, oth_bit) if b]
    if not preview_parts:
        preview_parts = ["<b>no new jobs</b>"]
    headline = " · ".join(preview_parts)
    subheadline = f"<i>{_esc(when)}</i>"

    header = [headline, subheadline, ""]

    # ---- Optional footer (context + dashboard link) ----
    context_bits = [f"<i>Scraped {scraped_count} jobs"]
    skipped_bits = []
    if skipped_priority:
        skipped_bits.append(f"{skipped_priority} priority")
    if skipped_other:
        skipped_bits.append(f"{skipped_other} other")
    if skipped_bits:
        context_bits.append(f"skipped: {' + '.join(skipped_bits)} already sent")
    context = " · ".join(context_bits) + "</i>"

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
        add(DIVIDER, allow_split=False)
        add(f"⭐ <b>PRIORITY · {n_pri}</b>", allow_split=False)
        add(DIVIDER, allow_split=False)
        emit_cards(new_priority, priority=True)

    if other_listed:
        add("", allow_split=False)
        add(DIVIDER, allow_split=False)
        add(f"🆕 <b>OTHER · {n_oth}</b>", allow_split=False)
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
    current.append(context)
    if dashboard:
        current.append(f"Dashboard → {_esc(dashboard)}")
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


def _strip_dashboard(text: str) -> str:
    """Remove any 'Dashboard → ...' footer line from a message body."""
    lines = text.splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        # Skip the dashboard footer entirely (raw or wrapped in tags).
        if stripped.startswith("Dashboard →"):
            continue
        kept.append(line)
    # Trim trailing blank lines.
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def send_message(
    text: str,
    *,
    disable_preview: bool = True,
    silent: bool = False,
    group_text: str | None = None,
) -> dict:
    """Send to the DM chat, then mirror to the group (silently, best-effort).

    If ``group_text`` is None the group receives the DM copy with the Dashboard
    footer stripped (private info, not for the shared group).
    """
    cfg = _config()
    result = _send_to(cfg["chat_id"], text, disable_preview=disable_preview, silent=silent)

    if cfg["group_chat_id"] and not _group_state["disabled"]:
        payload = group_text if group_text is not None else _strip_dashboard(text)
        try:
            _send_to(cfg["group_chat_id"], payload, disable_preview=disable_preview, silent=True)
            _group_state["failures"] = 0
            _group_state["last_error"] = ""
        except Exception as exc:  # noqa: BLE001 - never let group failure break DM flow
            _group_state["failures"] += 1
            _group_state["last_error"] = str(exc)
            if _group_state["failures"] >= GROUP_MAX_FAILURES:
                _group_state["disabled"] = True

    return result


def send_test() -> dict:
    now = datetime.now().strftime("%d %b %H:%M")
    text = (
        f"⭐ <b>Test</b> · JobSpy notifications — {_esc(now)}\n"
        "If you can read this, priority-job alerts will land here after each scrape. ✅"
    )
    # Reset any prior group failure state so a manual test can re-arm the target.
    _group_state["disabled"] = False
    _group_state["failures"] = 0
    _group_state["last_error"] = ""
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
