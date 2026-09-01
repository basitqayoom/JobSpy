"""Per-tick, per-profile Telegram delivery to region-specific feed bots.

- flush_profile(profile): drains ALL unsent jobs (priority + other) for that
  profile into ONE message posted to the correct feed bot:
    india          -> TELEGRAM_INDIA_BOT_TOKEN / TELEGRAM_INDIA_CHAT_ID
    everything else-> TELEGRAM_GLOBAL_BOT_TOKEN / TELEGRAM_GLOBAL_CHAT_ID

  Priority section first, then Other section.

Invoke:
  python delivery.py flush <profile>
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
MAX_OTHER_PER_MESSAGE = 20  # cap OTHER section per profile per tick (rest in expandable)


def _load_meta(profile: str) -> dict:
    try:
        with open(os.path.join(PROFILES_DIR, f"{profile}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _fmt_priority_card(job: dict) -> str:
    return f"<blockquote>{N._format_job_card_body(job, 0, priority=True)}</blockquote>"


def _fmt_other_card(job: dict) -> str:
    return f"<blockquote>{N._format_job_card_body(job, 0, priority=False)}</blockquote>"


def _build_message(profile: str, priority_rows: list[dict], other_rows: list[dict]) -> list[str]:
    meta = _load_meta(profile)
    flag = meta.get("flag", "")
    display = meta.get("display_name", profile)
    now = datetime.now().strftime("%d %b %H:%M")
    dashboard = N._config().get("dashboard", "")

    n_pri = len(priority_rows)
    n_oth = len(other_rows)

    header_lines = [
        f"{flag} <b>{N._esc(display)}</b>  ·  {n_pri + n_oth} new",
        f"<i>{N._esc(now)}</i>",
        "",
    ]

    messages: list[str] = []
    current = list(header_lines)
    current_len = sum(len(x) + 1 for x in current)

    def flush():
        nonlocal current, current_len
        if current and any(x.strip() for x in current):
            messages.append("\n".join(current).rstrip())
        current = []
        current_len = 0

    def emit(line: str):
        nonlocal current_len
        add = len(line) + 1
        if current_len + add > MAX_MSG_CHARS:
            flush()
        current.append(line)
        current_len += add

    if priority_rows:
        emit("─" * 14)
        emit(f"⭐ <b>PRIORITY</b>  ·  {n_pri}")
        emit("")
        for j in priority_rows:
            card = _fmt_priority_card(j)
            if current_len + len(card) + 1 > MAX_MSG_CHARS:
                flush()
                current = [f"{flag} <b>{N._esc(display)}</b> (continued)", ""]
                current_len = sum(len(x) + 1 for x in current)
            emit(card)

    if other_rows:
        emit("")
        emit("─" * 14)
        emit(f"🆕 <b>OTHER</b>  ·  {n_oth}")
        emit("")
        visible = other_rows[:3]
        hidden = other_rows[3:MAX_OTHER_PER_MESSAGE]
        overflow = other_rows[MAX_OTHER_PER_MESSAGE:]
        for j in visible:
            card = _fmt_other_card(j)
            if current_len + len(card) + 1 > MAX_MSG_CHARS:
                flush()
                current = [f"{flag} <b>{N._esc(display)}</b> (continued)", ""]
                current_len = sum(len(x) + 1 for x in current)
            emit(card)
        if hidden:
            expand_body = "\n\n".join(N._format_job_card_body(j, 0, priority=False) for j in hidden)
            block = f"<blockquote expandable>👇 <b>+{len(hidden)} more</b>\n\n{expand_body}</blockquote>"
            if len(block) > MAX_MSG_CHARS:
                # split into halves
                mid = len(hidden) // 2
                b1 = "\n\n".join(N._format_job_card_body(j, 0, priority=False) for j in hidden[:mid])
                b2 = "\n\n".join(N._format_job_card_body(j, 0, priority=False) for j in hidden[mid:])
                for b, n in [(b1, mid), (b2, len(hidden) - mid)]:
                    part = f"<blockquote expandable>👇 <b>+{n} more</b>\n\n{b}</blockquote>"
                    if current_len + len(part) + 1 > MAX_MSG_CHARS:
                        flush(); current = [f"{flag} <b>{N._esc(display)}</b> (continued)", ""]; current_len = sum(len(x)+1 for x in current)
                    emit(part)
            else:
                if current_len + len(block) + 1 > MAX_MSG_CHARS:
                    flush(); current = [f"{flag} <b>{N._esc(display)}</b> (continued)", ""]; current_len = sum(len(x)+1 for x in current)
                emit(block)
        if overflow:
            emit(f"<i>… +{len(overflow)} more on the dashboard</i>")

    if dashboard:
        emit("")
        emit("─" * 14)
        emit(f'📊 <a href="{N._esc(dashboard)}">Dashboard</a>')

    flush()
    return messages


def flush_profile(profile: str) -> dict:
    N.load_env()
    token, chat_id = N.feed_bot(profile)
    if not token or not chat_id:
        return {"sent": False, "reason": f"feed bot not configured for {profile}"}

    unsent = db.list_jobs(profile=profile, sent=False)
    if not unsent:
        return {"sent": False, "reason": "nothing-new", "profile": profile}

    priority_rows = [db.row_to_job_dict(r) for r in unsent if r["is_priority"]]
    other_rows    = [db.row_to_job_dict(r) for r in unsent if not r["is_priority"]]

    messages = _build_message(profile, priority_rows, other_rows)
    urls = [r["url"] for r in unsent]

    ok = True
    for i, m in enumerate(messages):
        try:
            N.send_via(token, chat_id, m, silent=(i > 0))
        except Exception as exc:
            print(f"ERROR send {i+1}/{len(messages)} for {profile}: {exc}")
            ok = False
            break
        time.sleep(SEND_DELAY_S)

    if ok:
        db.mark_sent(urls)
    return {
        "sent": ok,
        "profile": profile,
        "priority": len(priority_rows),
        "other": len(other_rows),
        "messages": len(messages),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python delivery.py flush <profile>")
    if sys.argv[1] == "flush" and len(sys.argv) >= 3:
        print(flush_profile(sys.argv[2]))
    else:
        raise SystemExit("Usage: python delivery.py flush <profile>")
