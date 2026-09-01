"""Long-polling Telegram bot for interactive job queries.

Security: only responds to the configured DM chat_id. Silently ignores all
other chats (groups, other users). Never leaves the DM.

Commands (auto-registered via setMyCommands):
  /start, /help           - menu
  /today [region]         - jobs first-seen today
  /priority [region]      - priority-company jobs today
  /latest [region]        - last 10 jobs
  /count                  - counts per region
  /company <name>         - jobs from company across regions
  /search <term>          - keyword search across regions
  /companies [region]     - priority companies list
  /status                 - system health
  /dashboard              - dashboard link
  /run <region>           - trigger a scrape
  /regions                - list of profile names

Run via systemd unit (see jobspy-bot.service).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import notify_telegram as N

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PROFILES_DIR = os.path.join(WEB_DIR, "profiles")
STATE_ROOT = os.path.join(WEB_DIR, "state")
OFFSET_FILE = os.path.join(HERE, ".bot_offset")
RUNNER = os.path.join(HERE, "run_scrape.sh")

POLL_TIMEOUT = 30
MAX_MSG_CHARS = 3800
RESULTS_PER_PAGE = 20

# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _list_profiles() -> list[str]:
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json"))


def _profile_meta(profile: str) -> dict:
    return _load_json(os.path.join(PROFILES_DIR, f"{profile}.json"))


def _profile_jobs(profile: str) -> list[dict]:
    d = _load_json(os.path.join(STATE_ROOT, profile, "data.json"))
    return d.get("jobs", []) if isinstance(d, dict) else []


def _all_jobs(region: str | None = None) -> list[dict]:
    profiles = [region] if region else _list_profiles()
    out: list[dict] = []
    for p in profiles:
        if not os.path.exists(os.path.join(PROFILES_DIR, f"{p}.json")):
            continue
        meta = _profile_meta(p)
        for j in _profile_jobs(p):
            row = dict(j)
            row.setdefault("profile", p)
            row.setdefault("flag", meta.get("flag", ""))
            row.setdefault("display_name", meta.get("display_name", p))
            out.append(row)
    return out


def _resolve_region(arg: str) -> str | None:
    """Map any of 'india', 'IN', 'in', '🇮🇳' -> 'india'."""
    if not arg:
        return None
    a = arg.strip().lower().lstrip("/")
    for p in _list_profiles():
        meta = _profile_meta(p)
        if a in (p, meta.get("display_name", "").lower(), meta.get("flag", "")):
            return p
    aliases = {"in": "india", "us": "usa", "gb": "uk", "de": "germany",
               "nl": "netherlands", "ie": "ireland", "pl": "poland",
               "ch": "switzerland", "se": "sweden", "ca": "canada",
               "au": "australia", "sg": "singapore"}
    return aliases.get(a)


def _india_first(profiles: list[str]) -> list[str]:
    india = ["india"] if "india" in profiles else []
    others = sorted(p for p in profiles if p != "india")
    return india + others


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _fmt_card(job: dict, *, show_flag: bool = True) -> str:
    priority = bool(job.get("is_priority"))
    flag = job.get("flag", "") if show_flag else ""
    prefix = f"{flag} " if flag else ""
    body = N._format_job_card_body(job, 0, priority=priority)
    # Prepend flag inline on the first line for context.
    if flag:
        lines = body.split("\n", 1)
        if len(lines) == 2:
            body = f"{prefix}{lines[0]}\n{lines[1]}"
        else:
            body = f"{prefix}{lines[0]}"
    return f"<blockquote>{body}</blockquote>"


def _chunk_send(chat_id: str, lines: list[str], *, silent: bool = False) -> None:
    msgs: list[str] = []
    cur, cur_len = [], 0
    for line in lines:
        add = len(line) + 1
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
    for m in msgs:
        N.send_message(m, silent=silent)
        time.sleep(0.4)


def _list_jobs_reply(jobs: list[dict], header: str, footer: str = "") -> list[str]:
    if not jobs:
        return [f"{header}\n\n<i>No matches.</i>"]
    lines = [header, ""]
    # Group by region, India first, then by count.
    by_region: dict[str, list[dict]] = {}
    for j in jobs:
        by_region.setdefault(j.get("profile", "?"), []).append(j)
    ordered = _india_first(list(by_region.keys()))
    ordered.sort(key=lambda p: (0 if p == "india" else 1, -len(by_region[p]), p))
    total = sum(len(v) for v in by_region.values())
    for p in ordered:
        meta = _profile_meta(p)
        section = by_region[p]
        lines.append("─" * 14)
        lines.append(f"{meta.get('flag','')} <b>{N._esc(meta.get('display_name', p))}</b>  ·  {len(section)}")
        lines.append("")
        visible = section[:RESULTS_PER_PAGE]
        for j in visible:
            lines.append(_fmt_card(j, show_flag=False))
        if len(section) > RESULTS_PER_PAGE:
            lines.append(f"<i>… +{len(section) - RESULTS_PER_PAGE} more (see dashboard)</i>")
    if footer:
        lines.append("")
        lines.append("─" * 14)
        lines.append(footer)
    return lines


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
def _today(job: dict) -> bool:
    iso = job.get("first_seen_at") or job.get("date_posted")
    if not iso:
        return False
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.strptime(str(iso), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.date() == datetime.now(timezone.utc).date()


def cmd_help(_arg: str) -> list[str]:
    return [
        "<b>JobSpy Bot — commands</b>\n"
        "\n"
        "<b>Filters</b> — all take an optional region (e.g. <code>/today usa</code>, <code>/priority india</code>)\n"
        "• /today — jobs first-seen today\n"
        "• /priority — priority-company jobs today\n"
        "• /latest — last 10 jobs\n"
        "• /company &lt;name&gt; — e.g. <code>/company Google</code>\n"
        "• /search &lt;term&gt; — e.g. <code>/search react senior</code>\n"
        "\n"
        "<b>Info</b>\n"
        "• /count — counts per region\n"
        "• /companies [region] — priority list\n"
        "• /regions — list all region codes\n"
        "• /status — system health\n"
        "• /dashboard — open the web UI\n"
        "\n"
        "<b>Actions</b>\n"
        "• /run &lt;region&gt; — trigger scrape now\n"
    ]


def cmd_regions(_arg: str) -> list[str]:
    lines = ["<b>Regions</b>", ""]
    for p in _india_first(_list_profiles()):
        m = _profile_meta(p)
        lines.append(f"{m.get('flag','')}  <code>{p}</code>  ·  {N._esc(m.get('display_name', p))}")
    return ["\n".join(lines)]


def cmd_status(_arg: str) -> list[str]:
    # last scrape per profile + pending sizes
    lines = ["<b>System status</b>", ""]
    for p in _india_first(_list_profiles()):
        d = _load_json(os.path.join(STATE_ROOT, p, "data.json"))
        pri = _load_json(os.path.join(STATE_ROOT, p, "pending_priority.json"))
        oth = _load_json(os.path.join(STATE_ROOT, p, "pending_other.json"))
        m = _profile_meta(p)
        gen = d.get("generated_at", "?")
        # age
        age = "?"
        try:
            g = datetime.fromisoformat(gen)
            if g.tzinfo is None:
                g = g.replace(tzinfo=timezone.utc)
            secs = (datetime.now(timezone.utc) - g).total_seconds()
            age = f"{int(secs//60)}m ago" if secs < 3600 else f"{int(secs//3600)}h ago"
        except Exception:
            pass
        lines.append(f"{m.get('flag','')} <b>{N._esc(m.get('display_name', p))}</b> · {d.get('count',0)} jobs · last {age} · pending {len(pri)}⭐ / {len(oth)}🆕")
    dashboard = N._config().get("dashboard", "")
    if dashboard:
        lines.append("")
        lines.append(f'📊 <a href="{N._esc(dashboard)}">Open dashboard</a>')
    return ["\n".join(lines)]


def cmd_dashboard(_arg: str) -> list[str]:
    dashboard = N._config().get("dashboard", "") or "<i>Not configured</i>"
    return [f'📊 <a href="{N._esc(dashboard)}">Open dashboard</a>']


def cmd_count(_arg: str) -> list[str]:
    lines = ["<b>Job counts</b>", ""]
    total_today = 0
    total_pri = 0
    for p in _india_first(_list_profiles()):
        jobs = _profile_jobs(p)
        today = [j for j in jobs if _today(j)]
        pri = [j for j in jobs if j.get("is_priority")]
        total_today += len(today)
        total_pri += len(pri)
        m = _profile_meta(p)
        lines.append(f"{m.get('flag','')} <b>{N._esc(m.get('display_name', p))}</b> · {len(jobs)} total · {len(today)} today · {len(pri)}⭐")
    lines.append("")
    lines.append(f"<b>Total:</b> {total_today} today · {total_pri} priority")
    return ["\n".join(lines)]


def cmd_today(arg: str) -> list[str]:
    region = _resolve_region(arg) if arg else None
    jobs = [j for j in _all_jobs(region) if _today(j)]
    label = _profile_meta(region).get("display_name", region) if region else "All regions"
    header = f"<b>📅 Today</b>  ·  {N._esc(label)}  ·  {len(jobs)} jobs"
    return _list_jobs_reply(jobs, header)


def cmd_priority(arg: str) -> list[str]:
    region = _resolve_region(arg) if arg else None
    jobs = [j for j in _all_jobs(region) if j.get("is_priority") and _today(j)]
    label = _profile_meta(region).get("display_name", region) if region else "All regions"
    header = f"<b>⭐ Priority · Today</b>  ·  {N._esc(label)}  ·  {len(jobs)} jobs"
    return _list_jobs_reply(jobs, header)


def cmd_latest(arg: str) -> list[str]:
    region = _resolve_region(arg) if arg else None
    jobs = _all_jobs(region)
    jobs.sort(key=lambda j: j.get("first_seen_at", ""), reverse=True)
    jobs = jobs[:10]
    label = _profile_meta(region).get("display_name", region) if region else "All regions"
    header = f"<b>🕐 Latest 10</b>  ·  {N._esc(label)}"
    return _list_jobs_reply(jobs, header)


def cmd_company(arg: str) -> list[str]:
    q = (arg or "").strip().lower()
    if not q:
        return ["Usage: <code>/company &lt;name&gt;</code>"]
    jobs = [j for j in _all_jobs() if q in (j.get("company") or "").lower()]
    header = f"<b>🏢 Company:</b> {N._esc(q)}  ·  {len(jobs)} jobs"
    return _list_jobs_reply(jobs, header)


def cmd_search(arg: str) -> list[str]:
    q = (arg or "").strip().lower()
    if not q:
        return ["Usage: <code>/search &lt;term&gt;</code>"]
    tokens = q.split()
    jobs = []
    for j in _all_jobs():
        hay = f"{j.get('title','')} {j.get('company','')} {j.get('location','')}".lower()
        if all(t in hay for t in tokens):
            jobs.append(j)
    header = f"<b>🔎 Search:</b> {N._esc(q)}  ·  {len(jobs)} jobs"
    return _list_jobs_reply(jobs, header)


def cmd_companies(arg: str) -> list[str]:
    region = _resolve_region(arg) if arg else None
    profiles = [region] if region else _india_first(_list_profiles())
    lines = ["<b>⭐ Priority companies</b>", ""]
    for p in profiles:
        m = _profile_meta(p)
        pcs = m.get("priority_companies", [])
        lines.append(f"{m.get('flag','')} <b>{N._esc(m.get('display_name', p))}</b>  ·  {len(pcs)}")
        lines.append("<blockquote expandable>" + N._esc(", ".join(pcs)) + "</blockquote>")
    return ["\n".join(lines)]


def cmd_run(arg: str) -> list[str]:
    profile = _resolve_region(arg) if arg else None
    if not profile:
        return ["Usage: <code>/run &lt;region&gt;</code> (e.g. <code>/run india</code>, <code>/run all</code>)"]
    # Special case: allow 'all'
    target = "all" if arg.strip().lower() == "all" else profile
    try:
        subprocess.Popen(
            ["/bin/bash", RUNNER, target],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return [f"🚀 Scrape started: <code>{N._esc(target)}</code>\n<i>Priority alerts arrive within ~1 min if any hits.</i>"]
    except Exception as exc:
        return [f"❌ Failed to start scrape: {N._esc(str(exc))}"]


COMMANDS = {
    "start":    cmd_help,
    "help":     cmd_help,
    "today":    cmd_today,
    "priority": cmd_priority,
    "latest":   cmd_latest,
    "count":    cmd_count,
    "company":  cmd_company,
    "search":   cmd_search,
    "companies":cmd_companies,
    "regions":  cmd_regions,
    "status":   cmd_status,
    "dashboard":cmd_dashboard,
    "run":      cmd_run,
}

MENU = [
    ("today",     "Jobs first-seen today"),
    ("priority",  "Priority-company jobs today"),
    ("latest",    "Last 10 jobs"),
    ("count",     "Counts per region"),
    ("company",   "Filter by company"),
    ("search",    "Keyword search"),
    ("companies", "Priority companies list"),
    ("regions",   "List of region codes"),
    ("status",    "System health"),
    ("dashboard", "Dashboard link"),
    ("run",       "Trigger scrape"),
    ("help",      "Command list"),
]


# --------------------------------------------------------------------------- #
# Telegram API (long-polling)
# --------------------------------------------------------------------------- #
def _api(method: str, params: dict, timeout: int = POLL_TIMEOUT + 5) -> dict:
    cfg = N._config()
    url = f"https://api.telegram.org/bot{cfg['token']}/{method}"
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": str(e)}


def _get_updates(offset: int) -> list[dict]:
    r = _api("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT})
    return r.get("result", []) if r.get("ok") else []


def _set_commands() -> None:
    commands = [{"command": c, "description": d} for c, d in MENU]
    r = _api("setMyCommands", {"commands": json.dumps(commands)})
    print("setMyCommands:", r.get("ok", r))


def _load_offset() -> int:
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _save_offset(v: int) -> None:
    with open(OFFSET_FILE, "w") as f:
        f.write(str(v))


# --------------------------------------------------------------------------- #
# Message loop
# --------------------------------------------------------------------------- #
def _handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    allowed = N._config().get("chat_id", "")
    if chat_id != allowed:
        print(f"IGNORED: chat_id={chat_id!r} (not owner)")
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        # Non-command in DM: gentle nudge.
        N.send_message("Tap <b>/</b> to see commands, or /help for details.", silent=True)
        return
    # /cmd@BotUser or /cmd arg1 arg2
    body = text[1:]
    parts = body.split(None, 1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    if not handler:
        N.send_message(f"Unknown command: <code>/{N._esc(cmd)}</code>. Try /help.", silent=True)
        return
    try:
        replies = handler(arg)
        for r in replies:
            _chunk_send(chat_id, [r], silent=False)
    except Exception as exc:
        N.send_message(f"❌ Error running <code>/{N._esc(cmd)}</code>: {N._esc(str(exc))}", silent=True)


def main() -> None:
    N.load_env()
    if not N.is_configured():
        raise SystemExit("notify_telegram not configured (.env missing keys)")
    _set_commands()
    offset = _load_offset()
    print(f"bot_chat: starting, offset={offset}")
    while True:
        try:
            updates = _get_updates(offset)
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message") or u.get("edited_message")
                if msg:
                    _handle_message(msg)
            if updates:
                _save_offset(offset)
        except KeyboardInterrupt:
            print("bot_chat: exiting")
            _save_offset(offset)
            return
        except Exception as exc:
            print(f"bot_chat: loop error: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
