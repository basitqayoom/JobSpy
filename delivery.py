"""Per-tick, region-scoped Telegram delivery.

Two scopes:
  india   -> TELEGRAM_INDIA_BOT_TOKEN   (only 'india' profile)
  global  -> TELEGRAM_GLOBAL_BOT_TOKEN  (all 11 non-india profiles)

Each scope produces ONE compact, heavily-collapsed message per invocation.
Structure:

    🔔 <scope> · X priority · Y other · <time>

    ⭐ PRIORITY
    🇺🇸 USA (2)              [expandable blockquote of cards]
    🇬🇧 UK (1)               [expandable]

    🆕 OTHER
    🇺🇸 USA (18)             [expandable]
    🇬🇧 UK (4)               [expandable]

Both priority and other are inside expandable blockquotes so the message
renders very short at first glance; user taps to unfold each country.

Invoke:
  python delivery.py flush_india
  python delivery.py flush_global
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import db
import notify_telegram as N

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PROFILES_DIR = os.path.join(WEB_DIR, "profiles")

MAX_MSG_CHARS = 3800
SEND_DELAY_S  = 0.4


def _load_meta(profile: str) -> dict:
    try:
        with open(os.path.join(PROFILES_DIR, f"{profile}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _ordered(profiles: list[str]) -> list[str]:
    india = [p for p in profiles if p == "india"]
    others = sorted(p for p in profiles if p != "india")
    return india + others


def _card_body(job: dict, priority: bool) -> str:
    return N._format_job_card_body(job, 0, priority=priority)


def _blockquote_section(profile: str, jobs: list[dict], priority: bool) -> str:
    """Expandable blockquote: header line always visible; cards hidden until tap."""
    meta = _load_meta(profile)
    flag = meta.get("flag", "")
    name = meta.get("display_name", profile)
    star = "⭐" if priority else "🆕"
    header = f"{star} {flag} <b>{N._esc(name)}</b>  ·  {len(jobs)}"
    body = "\n\n".join(_card_body(j, priority) for j in jobs)
    return f"<blockquote expandable>{header}\n\n{body}</blockquote>"


def _split_section(profile: str, jobs: list[dict], priority: bool, max_chars: int) -> list[str]:
    """Split a single country's section into multiple blockquotes if it exceeds max_chars.

    Each returned string is a complete <blockquote expandable>...</blockquote>.
    """
    # Greedy: keep adding cards until section exceeds max_chars.
    if not jobs:
        return []
    parts: list[list[dict]] = [[]]
    cur_len = 0
    for j in jobs:
        card = _card_body(j, priority)
        add = len(card) + 2  # separator
        # Overhead of the wrapper + header line ~= 120 chars
        if cur_len + add > max_chars - 200 and parts[-1]:
            parts.append([])
            cur_len = 0
        parts[-1].append(j)
        cur_len += add
    return [_blockquote_section(profile, sub, priority) for sub in parts]


def _build_messages(scope: str, priority_by_profile: dict[str, list[dict]],
                    other_by_profile: dict[str, list[dict]]) -> list[str]:
    now = datetime.now().strftime("%d %b %H:%M")
    dashboard = N._config().get("dashboard", "")

    total_pri = sum(len(v) for v in priority_by_profile.values())
    total_oth = sum(len(v) for v in other_by_profile.values())
    total = total_pri + total_oth

    scope_label = "India" if scope == "india" else "US / EU / Global"
    header_lines = [
        f"🔔 <b>{N._esc(scope_label)}</b>  ·  "
        f"{total_pri} ⭐  ·  {total_oth} 🆕  ·  <b>{total} new</b>",
        f"<i>{N._esc(now)}</i>",
        "",
    ]

    # Build all sections (list of complete <blockquote> strings).
    sections: list[str] = []
    if total_pri:
        sections.append("⭐ <b>PRIORITY</b>")
        for p in _ordered(list(priority_by_profile.keys())):
            sections.extend(_split_section(p, priority_by_profile[p], True, MAX_MSG_CHARS))
        sections.append("")
    if total_oth:
        sections.append("🆕 <b>OTHER</b>")
        for p in _ordered(list(other_by_profile.keys())):
            sections.extend(_split_section(p, other_by_profile[p], False, MAX_MSG_CHARS))
        sections.append("")

    footer_lines = []
    if dashboard:
        footer_lines.append("─" * 14)
        footer_lines.append(f'📊 <a href="{N._esc(dashboard)}">Dashboard</a>')

    # Pack header + sections + footer into messages by MAX_MSG_CHARS.
    # Rules: never split a single blockquote (they're already <= max_chars).
    messages: list[str] = []
    header_str = "\n".join(header_lines)
    cur = [header_str]
    cur_len = len(header_str) + 1

    def flush(add_continued: bool = False):
        nonlocal cur, cur_len
        if cur and any(x.strip() for x in cur):
            messages.append("\n".join(cur).rstrip())
        cont = f"🔔 <b>{N._esc(scope_label)}</b> (continued)"
        cur = [cont, ""]
        cur_len = len(cont) + 2

    for line in sections + footer_lines:
        add = len(line) + 1
        if cur_len + add > MAX_MSG_CHARS:
            flush(add_continued=True)
        cur.append(line)
        cur_len += add

    if cur and any(x.strip() for x in cur):
        messages.append("\n".join(cur).rstrip())
    return messages


def _collect(profiles: list[str]) -> tuple[dict[str, list[dict]], dict[str, list[dict]], list[str]]:
    """Return (priority_by_profile, other_by_profile, all_urls)."""
    pri: dict[str, list[dict]] = {}
    oth: dict[str, list[dict]] = {}
    urls: list[str] = []
    for p in profiles:
        rows = db.list_jobs(profile=p, sent=False)
        if not rows:
            continue
        pri_rows = [db.row_to_job_dict(r) for r in rows if r["is_priority"]]
        oth_rows = [db.row_to_job_dict(r) for r in rows if not r["is_priority"]]
        if pri_rows:
            pri[p] = pri_rows
        if oth_rows:
            oth[p] = oth_rows
        urls.extend(r["url"] for r in rows)
    return pri, oth, urls


def _all_profiles() -> list[str]:
    return sorted(f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json"))


def flush_scope(scope: str) -> dict:
    N.load_env()
    token, chat_id = N.feed_bot("india" if scope == "india" else "global")
    if not token or not chat_id:
        return {"sent": False, "reason": f"feed bot not configured for {scope}"}

    if scope == "india":
        profiles = ["india"]
    else:
        profiles = [p for p in _all_profiles() if p != "india"]

    pri, oth, urls = _collect(profiles)
    total = sum(len(v) for v in pri.values()) + sum(len(v) for v in oth.values())
    if total == 0:
        return {"sent": False, "reason": "nothing-new", "scope": scope}

    messages = _build_messages(scope, pri, oth)
    ok = True
    for i, m in enumerate(messages):
        try:
            N.send_via(token, chat_id, m, silent=(i > 0))
        except Exception as exc:
            print(f"ERROR send {i+1}/{len(messages)} for {scope}: {exc}")
            ok = False
            break
        time.sleep(SEND_DELAY_S)

    if ok:
        db.mark_sent(urls)
    return {
        "sent": ok,
        "scope": scope,
        "priority": sum(len(v) for v in pri.values()),
        "other": sum(len(v) for v in oth.values()),
        "messages": len(messages),
        "profiles": {p: len(pri.get(p, [])) + len(oth.get(p, [])) for p in profiles if p in pri or p in oth},
    }


# Legacy alias so run_scrape.sh doesn't break if not updated yet.
def flush_profile(profile: str) -> dict:
    return flush_scope("india" if profile == "india" else "global")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python delivery.py flush_india|flush_global|flush <profile>")
    cmd = sys.argv[1]
    if cmd == "flush_india":
        print(flush_scope("india"))
    elif cmd == "flush_global":
        print(flush_scope("global"))
    elif cmd == "flush" and len(sys.argv) >= 3:
        # Kept for backward-compat: treats profile arg as scope selector.
        print(flush_profile(sys.argv[2]))
    else:
        raise SystemExit("Usage: python delivery.py flush_india|flush_global")
