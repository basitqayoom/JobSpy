"""Delivery workers for multi-profile Telegram notifications.

- flush_priority(): drains web/state/<profile>/pending_priority.json across ALL
  profiles into one combined message (India first, then by count).
- flush_other():    same, but for pending_other.json. Silent.

Both are idempotent: dedupe via notify_telegram.telegram_sent.json, and only
remove from pending after successful send.

Invoke:  python delivery.py priority
         python delivery.py other
         python delivery.py overnight   (audible combined flush)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import notify_telegram as N

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PROFILES_DIR = os.path.join(WEB_DIR, "profiles")
STATE_ROOT = os.path.join(WEB_DIR, "state")

SEND_DELAY_S = 0.4
MAX_MSG_CHARS = 3800


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


def _list_profiles() -> list[str]:
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json"))


def _load_profile_meta(profile: str) -> dict:
    return _load_json(os.path.join(PROFILES_DIR, f"{profile}.json"))


def _ordered_profiles(profiles_with_hits: dict[str, list]) -> list[str]:
    """India first, then remaining by count desc, then alpha."""
    keys = list(profiles_with_hits.keys())
    india = ["india"] if "india" in keys else []
    others = [k for k in keys if k != "india"]
    others.sort(key=lambda k: (-len(profiles_with_hits[k]), k))
    return india + others


def _drain_pending(bucket: str) -> dict[str, list[dict]]:
    """Return {profile: [jobs]} for jobs in <bucket> that haven't been sent."""
    sent = N._prune(N._load_sent())
    result: dict[str, list[dict]] = {}
    for profile in _list_profiles():
        path = os.path.join(STATE_ROOT, profile, f"pending_{bucket}.json")
        pending = _load_json(path)
        # Filter out anything already sent.
        fresh = [j for u, j in pending.items() if u and u not in sent]
        if fresh:
            result[profile] = fresh
    return result


def _mark_sent_and_clear(bucket: str, jobs_by_profile: dict[str, list[dict]]) -> None:
    """Update telegram_sent.json and remove from each profile's pending file."""
    sent = N._load_sent()
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    for profile, jobs in jobs_by_profile.items():
        path = os.path.join(STATE_ROOT, profile, f"pending_{bucket}.json")
        pending = _load_json(path)
        for j in jobs:
            url = j.get("job_url")
            if not url:
                continue
            sent[url] = now_iso
            pending.pop(url, None)
        _save_json(path, pending)
    N._save_sent(sent)


# --------------------------------------------------------------------------- #
# Message builders
# --------------------------------------------------------------------------- #
def _build_priority_message(jobs_by_profile: dict[str, list[dict]], dashboard: str) -> list[str]:
    total = sum(len(v) for v in jobs_by_profile.values())
    now = datetime.now().strftime("%d %b %H:%M")

    header = [
        f"🔔 <b>{total} new priority job{'s' if total != 1 else ''}</b>",
        f"<i>{N._esc(now)}</i>",
        "",
    ]

    body_lines: list[str] = []
    for profile in _ordered_profiles(jobs_by_profile):
        meta = _load_profile_meta(profile)
        flag = meta.get("flag", "")
        display = meta.get("display_name", profile)
        jobs = jobs_by_profile[profile]
        body_lines.append("─" * 14)
        body_lines.append(f"{flag} <b>{N._esc(display)}</b>  ·  {len(jobs)}")
        body_lines.append("")
        for j in jobs:
            body_lines.append(N.format_job_card(j, 0, priority=True))

    footer = [
        "",
        "─" * 14,
        f'📊 <a href="{N._esc(dashboard)}">Open dashboard</a>' if dashboard else "",
    ]
    return _chunk(header + body_lines + footer)


def _build_other_message(jobs_by_profile: dict[str, list[dict]], dashboard: str, *, window_label: str) -> list[str]:
    total = sum(len(v) for v in jobs_by_profile.values())
    now = datetime.now().strftime("%H:%M")

    # Summary chips (India first, then by count)
    ordered = _ordered_profiles(jobs_by_profile)
    chips = []
    for p in ordered:
        meta = _load_profile_meta(p)
        chips.append(f"{meta.get('flag','')} {N._esc(meta.get('display_name', p))} ({len(jobs_by_profile[p])})")

    header = [
        f"🕐 <b>{window_label}</b>  ·  {total} new job{'s' if total != 1 else ''}  ·  <i>silent</i>",
        "  ".join(chips),
        "",
    ]

    body_lines: list[str] = []
    for profile in ordered:
        meta = _load_profile_meta(profile)
        flag = meta.get("flag", "")
        display = meta.get("display_name", profile)
        jobs = jobs_by_profile[profile]
        body_lines.append("─" * 14)
        body_lines.append(f"{flag} <b>{N._esc(display)}</b>  ·  {len(jobs)}")
        body_lines.append("")
        visible = jobs[:3]
        hidden = jobs[3:]
        for j in visible:
            body_lines.append(N.format_job_card(j, 0, priority=False))
        if hidden:
            # Split expandable body into blocks that each fit safely in one message.
            # Approx: ~150 chars per card avg, so ~20 cards per blockquote.
            CHUNK = 20
            for i in range(0, len(hidden), CHUNK):
                slice_ = hidden[i:i + CHUNK]
                expand_body = "\n\n".join(
                    N._format_job_card_body(j, 0, priority=False) for j in slice_
                )
                remaining = len(hidden) - i - len(slice_)
                label = f"👇 <b>+{len(slice_)} more</b>" if i == 0 else f"👇 <b>{len(slice_)} more</b>"
                if remaining:
                    label += f" ({remaining} still hidden)"
                body_lines.append(f"<blockquote expandable>{label}\n\n{expand_body}</blockquote>")

    footer = [
        "",
        "─" * 14,
        f'📊 <a href="{N._esc(dashboard)}">Open dashboard</a>' if dashboard else "",
    ]
    return _chunk(header + body_lines + footer)


def _chunk(lines: list[str]) -> list[str]:
    """Concatenate lines into Telegram-sized chunks (<=3800 chars).

    Any single line already longer than MAX_MSG_CHARS gets emitted on its own
    (Telegram may still reject it, but that beats a silent overflow).
    """
    msgs: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        add = len(line) + 1
        # Line itself is too big -> flush current chunk and put oversized line alone.
        if len(line) > MAX_MSG_CHARS:
            if cur:
                msgs.append("\n".join(cur).rstrip())
                cur, cur_len = [], 0
            msgs.append(line)
            continue
        if cur_len + add > MAX_MSG_CHARS and cur:
            msgs.append("\n".join(cur).rstrip())
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += add
    if cur:
        msgs.append("\n".join(cur).rstrip())
    return msgs


def _send(messages: list[str], *, silent: bool) -> bool:
    for i, m in enumerate(messages):
        try:
            N.send_message(m, silent=silent or (i > 0))
        except Exception as exc:
            print(f"ERROR sending message {i+1}/{len(messages)}: {exc}")
            return False
        time.sleep(SEND_DELAY_S)
    return True


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def flush_priority() -> dict:
    N.load_env()
    if not N.is_configured():
        return {"sent": False, "reason": "not-configured"}

    jobs = _drain_pending("priority")
    total = sum(len(v) for v in jobs.values())
    if total == 0:
        return {"sent": False, "reason": "nothing-new", "total": 0}

    dashboard = N._config().get("dashboard", "")
    msgs = _build_priority_message(jobs, dashboard)
    ok = _send(msgs, silent=False)
    if ok:
        _mark_sent_and_clear("priority", jobs)
    return {"sent": ok, "total": total, "profiles": {p: len(v) for p, v in jobs.items()}, "messages": len(msgs)}


def flush_other(window_label: str = "Hourly digest") -> dict:
    N.load_env()
    if not N.is_configured():
        return {"sent": False, "reason": "not-configured"}

    jobs = _drain_pending("other")
    total = sum(len(v) for v in jobs.values())
    if total == 0:
        return {"sent": False, "reason": "nothing-new", "total": 0}

    dashboard = N._config().get("dashboard", "")
    msgs = _build_other_message(jobs, dashboard, window_label=window_label)
    ok = _send(msgs, silent=True)
    if ok:
        _mark_sent_and_clear("other", jobs)
    return {"sent": ok, "total": total, "profiles": {p: len(v) for p, v in jobs.items()}, "messages": len(msgs)}


def flush_overnight() -> dict:
    """Audible combined dump of both buckets (used at 08:00 IST after quiet hours)."""
    N.load_env()
    if not N.is_configured():
        return {"sent": False, "reason": "not-configured"}

    pri = _drain_pending("priority")
    oth = _drain_pending("other")
    total = sum(len(v) for v in pri.values()) + sum(len(v) for v in oth.values())
    if total == 0:
        return {"sent": False, "reason": "nothing-new", "total": 0}

    dashboard = N._config().get("dashboard", "")

    # Priority first (audible), then other (silent same message queue)
    all_msgs: list[str] = []
    if any(pri.values()):
        all_msgs += _build_priority_message(pri, dashboard)
    if any(oth.values()):
        all_msgs += _build_other_message(oth, dashboard, window_label="Overnight digest")

    ok = _send(all_msgs, silent=False)
    if ok:
        if pri: _mark_sent_and_clear("priority", pri)
        if oth: _mark_sent_and_clear("other", oth)
    return {"sent": ok, "total": total, "messages": len(all_msgs)}


def _in_quiet_hours(now: datetime) -> bool:
    """23:00-08:00 IST (UTC+5:30). Holds priority pings; overnight digest at 08:00 flushes."""
    # Convert to IST-ish (UTC+5:30) without pytz
    from datetime import timedelta
    ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    h = ist.hour
    return h >= 23 or h < 8


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python delivery.py priority|other|overnight")
    mode = sys.argv[1]
    if mode == "priority":
        # Skip during quiet hours (held for overnight flush)
        if _in_quiet_hours(datetime.now(timezone.utc)):
            print("priority: quiet hours, holding")
        else:
            print(flush_priority())
    elif mode == "other":
        if _in_quiet_hours(datetime.now(timezone.utc)):
            print("other: quiet hours, holding")
        else:
            print(flush_other())
    elif mode == "overnight":
        print(flush_overnight())
    else:
        raise SystemExit(f"Unknown mode: {mode}")
