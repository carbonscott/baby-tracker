"""Answer a parent's question about the log with Claude, which queries the log itself (read-only).

The server passes in export_rows() (one flat dict per event). Each question gets a fresh in-memory
SQLite copy with a few computed columns and tables. Claude's only tool is `sql`: one SELECT on that
copy, with a time limit and a row cap. Claude never sees the real database file, and nothing it
runs can change the log.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

import claude_ai
from claude_ai import AIError

MODEL = os.environ.get("BABY_ASK_MODEL", "sonnet")
TIMEOUT_S = float(os.environ.get("BABY_ASK_TIMEOUT", "120"))
MAX_TURNS = 12               # each sql call is one turn
SESSION_GAP = dt.timedelta(minutes=15)   # feeds closer than this are one session (same rule as the page)
QUERY_SECONDS = 2.0          # per sql call
MAX_ROWS = 200               # rows returned per sql call
MAX_CELL = 300               # chars per value in tool output
MAX_OUTPUT = 20_000          # chars per tool result
NIGHT_START, NIGHT_END = 19, 6   # local hours: entries starting 7 PM-6 AM belong to that evening's night

# The snapshot Claude queries. The comments are the documentation Claude reads (sent in the prompt).
SCHEMA = """\
CREATE TABLE events (        -- one row per log entry
  id INTEGER PRIMARY KEY,
  kind TEXT,                 -- 'feed' | 'diaper' | 'sleep' | 'activity'
  start_utc TEXT,            -- 'YYYY-MM-DD HH:MM:SS' (UTC)
  end_utc TEXT,              -- UTC; NULL = timer still running; equals start_utc for point events
  start_local TEXT,          -- 'YYYY-MM-DD HH:MM' local wall-clock time
  end_local TEXT,            -- local; NULL while running
  local_date TEXT,           -- 'YYYY-MM-DD' local date of the start
  local_hour INTEGER,        -- 0-23, local hour of the start
  night_of TEXT,             -- entries starting 7 PM-6 AM: date of that evening (1 AM Nov 2 -> '2026-11-01');
                             -- NULL in the daytime. Use it for "per night" questions.
  duration_min REAL,         -- breast feeds, sleep, timed activities (minutes so far if running);
                             -- NULL for point events: diapers, bottles, activities logged as a moment,
                             -- and for sleeps / breast feeds logged without a duration (end_utc = start_utc)
  running INTEGER,           -- 1 = timer still running now
  name TEXT,                 -- activity name, free text (group by lower(name))
  method TEXT,               -- feed: 'breast' | 'bottle'
  side TEXT,                 -- breast feed: 'left' | 'right' | 'both'
  milk TEXT,                 -- bottle: 'breastmilk' | 'formula' | NULL (not recorded)
  amount_ml REAL,            -- bottle amount in ml; NULL = not recorded
  pee INTEGER,               -- diaper: 1/0
  poop INTEGER,              -- diaper: 1/0 (pee=0 and poop=0 = dry)
  color TEXT,                -- poop: yellow|orange|green|brown|black|red|pale; NULL = not recorded
  texture TEXT,              -- poop: watery|loose|seedy|soft|pasty|formed|hard; NULL = not recorded
  place TEXT,                -- where it happened (free text), often NULL
  note TEXT,                 -- parent's note, often ''
  feed_session INTEGER,      -- feed rows: feed_sessions.session this row belongs to
  logged_by TEXT,            -- who logged it (first part of their login), may be NULL
  via_claude TEXT            -- 'photo' | 'text' | 'photo+text' when logged from a Claude suggestion
);
CREATE TABLE feed_sessions ( -- one row per feed as the parent counts them: feed rows < 15 min apart
                             -- (left then right, breast then a top-up bottle) are one session
  session INTEGER PRIMARY KEY,
  start_utc TEXT, start_local TEXT, local_date TEXT, local_hour INTEGER,
  end_local TEXT,            -- NULL while running
  feeds INTEGER,             -- feed rows in the session
  breast_min REAL,           -- minutes at the breast (NULL if no breast feed in it has a duration)
  bottle_ml REAL,            -- ml from bottles with a recorded amount (NULL if none)
  bottles INTEGER,           -- bottle rows (incl. ones with no amount)
  methods TEXT,              -- 'breast' | 'bottle' | 'breast+bottle'
  first_side TEXT,           -- side the breastfeeding started on
  sides TEXT,                -- e.g. 'left,right'
  gap_min REAL,              -- minutes since the previous session's start (start-to-start), NULL for the first
  running INTEGER
);
CREATE TABLE day_segments (  -- timed entries split at local midnight (running ones up to now):
                             -- SUM(minutes) GROUP BY local_date gives correct per-day totals
  event_id INTEGER,          -- events.id
  kind TEXT, name TEXT, method TEXT,
  local_date TEXT, start_local TEXT, end_local TEXT,
  minutes REAL,
  running INTEGER
);
CREATE TABLE days (          -- one row per local date with entries: use it for daily averages and trends
  local_date TEXT PRIMARY KEY,
  full_day INTEGER,          -- 1 = the log covers the whole day (0 for the log's first day and for today)
  feeds INTEGER,             -- feed sessions that started that day
  breast_min REAL,           -- minutes at the breast (split at midnight)
  bottles INTEGER,
  bottle_ml REAL,            -- recorded bottle amounts only
  formula_ml REAL,           -- the formula part of bottle_ml
  diapers INTEGER, wet INTEGER, dirty INTEGER,
  sleep_min REAL,            -- total sleep (split at midnight)
  longest_sleep_min REAL     -- longest single sleep that started that day
);
"""

# Not shown to Claude: keeps building `days` (and Claude's per-day queries) fast with a year of data.
INDEXES = """
CREATE INDEX events_day ON events(local_date, kind);
CREATE INDEX segments_day ON day_segments(local_date);
CREATE INDEX sessions_day ON feed_sessions(local_date);
"""

# Fills `days` from the other tables (run before the snapshot is locked).
DAYS_SQL = """
INSERT INTO days
SELECT d, d > (SELECT min(local_date) FROM events) AND d < :today,
  (SELECT count(*) FROM feed_sessions WHERE local_date = d),
  (SELECT coalesce(round(sum(minutes), 1), 0) FROM day_segments WHERE local_date = d AND method = 'breast'),
  (SELECT count(*) FROM events WHERE local_date = d AND method = 'bottle'),
  (SELECT coalesce(sum(amount_ml), 0) FROM events WHERE local_date = d AND method = 'bottle'),
  (SELECT coalesce(sum(amount_ml), 0) FROM events WHERE local_date = d AND method = 'bottle' AND milk = 'formula'),
  (SELECT count(*) FROM events WHERE local_date = d AND kind = 'diaper'),
  (SELECT coalesce(sum(pee), 0) FROM events WHERE local_date = d AND kind = 'diaper'),
  (SELECT coalesce(sum(poop), 0) FROM events WHERE local_date = d AND kind = 'diaper'),
  (SELECT coalesce(round(sum(minutes), 1), 0) FROM day_segments WHERE local_date = d AND kind = 'sleep'),
  (SELECT max(duration_min) FROM events WHERE local_date = d AND kind = 'sleep')
FROM (SELECT local_date AS d FROM events UNION SELECT local_date FROM day_segments)
"""

SYSTEM_PROMPT = """You answer a parent's questions about their baby's log (feeds, diapers, sleep, activities), \
using the `sql` tool to read it. {baby}

Now: {now_local} ({zone}). In SQL use these literals, not SQLite's 'now': now_utc = '{now_utc}', \
today = '{today}', yesterday = '{yesterday}'.

The `sql` tool runs ONE read-only SQLite SELECT (or WITH ... SELECT) on a snapshot of the log and returns at \
most {max_rows} rows. Tables:

{schema}
At a glance: {glance}

Conventions:
- Feeds are counted as sessions (feed_sessions), like the app does. Time between feeds is start to start (gap_min).
- Per-day numbers: the days table (feeds, diapers, sleep, ...). Other per-day time (e.g. an activity): \
SUM(day_segments.minutes) GROUP BY local_date. Both split night sleep at midnight.
- Durations: use duration_min, minutes, gap_min, or julianday() on *_utc columns; never subtract *_local times \
(daylight saving). Time inside a window (e.g. sleep in the last 24 hours): \
SUM(overlap_min(start_utc, end_utc, window_start_utc, window_end_utc)) clips entries to the window and counts \
running timers up to now.
- Day ranges: filter on local_date. "Last 24 hours" = start_utc >= datetime(now_utc, '-24 hours'). "This week" = \
the last 7 days including today, "last week" = the 7 days before that, unless the parent says otherwise. Today and \
the log's first day are partial: leave them out of daily averages (days.full_day = 1), or say so. In a \
comparison, use the same dates for every number.
- NULL amount/color/texture = not recorded, not zero: average over recorded values and mention what's missing.
- end == start on a sleep or breast feed = duration not logged (duration_min NULL): it counts as an entry but \
adds no minutes. Say how many such entries a total or average leaves out when it matters.
- Activity names are free text: group by lower(name). Naps vs night sleep aren't labeled: use night_of (sleep \
starting 7 PM-6 AM) and say that's the rule you used.
- Amounts are stored in ml. {unit_rule}

How to answer:
- Only from the data. Before saying something isn't recorded, check. Compute every number you state (counts, \
averages, ranges) in SQL; don't count rows by eye. Use a few aggregate queries rather than many raw ones \
(1-2 for quick questions; for reports, several facts per query and at most about 8 queries).
- Quick questions: 1-3 short sentences, answer first. Times in 12-hour local time ("2:05 PM", with "yesterday" \
or the weekday when not today) and how long ago.
- Summaries/reports (e.g. for a doctor visit): markdown with ## headings, short bullets, and small tables. Cover \
feeding, diapers (incl. stool color/texture), sleep, activities, trends, things worth mentioning, and a few \
questions to ask the doctor. Start from days (full days only, e.g. averages per week in one query). For trends \
compare recent full weeks: feeds, bottle ml, diapers, and sleep per day, and the longest sleep per night (night_of).
- Markdown only: headings, lists, **bold**, *italic*, simple tables. No HTML, links, or images. Don't mention \
SQL, tables, or column names.
- You are not a doctor: no diagnoses and no judging what's normal for age; for health worries suggest asking the \
pediatrician. Pale/white, red, or black stool is worth a call to the pediatrician. Never judge sleep safety. \
Nothing about medicine doses."""

UNIT_RULES = {
    "oz": "The parent uses oz: show amounts in oz with 1 decimal (1 oz = 29.5735 ml).",
    "ml": "The parent uses ml: show amounts in ml.",
}


# ---------- the snapshot ----------

def zone_for(name, fallback: ZoneInfo | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except Exception:   # empty, unknown, or malformed zone name
        return fallback or ZoneInfo("UTC")


def _ts(value) -> dt.datetime | None:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


def _utc(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _loc(t: dt.datetime, zone: ZoneInfo) -> str:
    return t.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _min(a: dt.datetime, b: dt.datetime) -> float:
    return round(max(0.0, (b - a).total_seconds() / 60), 1)


def _night_of(local: dt.datetime) -> str | None:
    if local.hour >= NIGHT_START:
        return local.date().isoformat()
    if local.hour < NIGHT_END:
        return (local.date() - dt.timedelta(days=1)).isoformat()
    return None


def _via(row: dict) -> str | None:
    return row.get("ai_source") or ("photo" if row.get("photo_suggested") else None)


def build_snapshot(rows: list[dict], tz: str, now: dt.datetime) -> sqlite3.Connection:
    """In-memory SQLite copy of the export rows plus computed columns/tables, then locked read-only."""
    default_zone = zone_for(tz)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(SCHEMA)
    events, sessions, segments = [], [], []
    cur = None   # the feed session being built
    for r in sorted(rows, key=lambda r: (r["start_at"], r["id"])):
        zone = zone_for(r.get("tz"), default_zone)
        start, end = _ts(r["start_at"]), _ts(r.get("end_at"))
        kind, method = r["kind"], r.get("method")
        point = kind == "diaper" or method == "bottle"
        running = end is None and not point
        stop = now if running else (end or start)   # effective end
        timed = not point and stop > start
        local = start.astimezone(zone)
        ev = {
            "id": r["id"], "kind": kind,
            "start_utc": _utc(start), "end_utc": None if running else _utc(end or start),
            "start_local": _loc(start, zone), "end_local": None if running else _loc(end or start, zone),
            "local_date": local.date().isoformat(), "local_hour": local.hour, "night_of": _night_of(local),
            # running: minutes so far (0 when its start is a few seconds ahead of the server's clock)
            "duration_min": _min(start, stop) if timed or running else None, "running": int(running),
            "name": r.get("name"), "method": method, "side": r.get("side"), "milk": r.get("milk"),
            "amount_ml": r.get("amount_ml"), "pee": r.get("pee"), "poop": r.get("poop"),
            "color": r.get("color"), "texture": r.get("texture"), "place": r.get("place"),
            "note": r.get("note") or "", "feed_session": None,
            "logged_by": (r.get("logged_by") or "").split("@")[0] or None, "via_claude": _via(r),
        }
        if kind == "feed":
            if cur is None or start - cur["stop"] > SESSION_GAP:
                cur = {"rows": [], "stop": stop, "start": start, "zone": zone}
                sessions.append(cur)
            cur["rows"].append(ev)
            cur["stop"] = max(cur["stop"], stop)
            ev["feed_session"] = len(sessions)
        if timed:   # split at each local midnight
            a = start
            while a < stop:
                midnight = dt.datetime.combine(a.astimezone(zone).date() + dt.timedelta(days=1), dt.time(), zone)
                b = min(stop, midnight.astimezone(dt.timezone.utc))
                if b <= a:
                    break
                segments.append((r["id"], kind, ev["name"], method, a.astimezone(zone).date().isoformat(),
                                 _loc(a, zone), _loc(b, zone), _min(a, b), int(running)))
                a = b
        events.append(ev)
    if events:
        conn.executemany(f"INSERT INTO events ({','.join(events[0])}) VALUES ({','.join('?' * len(events[0]))})",
                         [tuple(e.values()) for e in events])
    prev = None
    for n, s in enumerate(sessions, 1):
        items = s["rows"]
        breast = [e for e in items if e["method"] == "breast"]
        bottles = [e for e in items if e["method"] == "bottle"]
        amounts = [e["amount_ml"] for e in bottles if e["amount_ml"] is not None]
        breast_min = [e["duration_min"] for e in breast if e["duration_min"] is not None]   # skips ones without a duration
        running = any(e["running"] for e in items)
        conn.execute("INSERT INTO feed_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            n, _utc(s["start"]), items[0]["start_local"], items[0]["local_date"], items[0]["local_hour"],
            None if running else _loc(s["stop"], s["zone"]), len(items),
            round(sum(breast_min), 1) if breast_min else None,
            sum(amounts) if amounts else None, len(bottles),
            "+".join(m for m in ("breast", "bottle") if any(e["method"] == m for e in items)),
            breast[0]["side"] if breast else None, ",".join(e["side"] or "?" for e in breast) or None,
            _min(prev, s["start"]) if prev else None, int(running)))
        prev = s["start"]
    conn.executemany("INSERT INTO day_segments VALUES (?,?,?,?,?,?,?,?,?)", segments)
    conn.executescript(INDEXES)
    conn.execute(DAYS_SQL, {"today": now.astimezone(default_zone).date().isoformat()})
    conn.commit()
    conn.create_function("overlap_min", 4, lambda *a: overlap_min(*a, now=now), deterministic=True)
    lock(conn)
    return conn


def overlap_min(start, end, a, b, *, now: dt.datetime):
    """SQL helper: minutes of [start, end or now] inside the window [a, b] (all UTC text)."""
    if not (start and a and b):
        return None
    t = [dt.datetime.fromisoformat(str(x).replace("Z", "")).replace(tzinfo=dt.timezone.utc)
         for x in (start, end or _utc(now), a, b)]
    return _min(max(t[0], t[2]), min(t[1], t[3]))


def lock(conn: sqlite3.Connection) -> None:
    """Make the connection read-only for good: SELECT only, no PRAGMA/ATTACH/writes, small limits."""
    conn.execute("PRAGMA query_only = ON")
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)       # max string/blob: no memory blowups
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

    def authorize(action, arg1, arg2, db_name, source):
        if action == sqlite3.SQLITE_FUNCTION and arg2 == "load_extension":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorize)   # also stops turning query_only back off (PRAGMA is denied)


# ---------- the sql tool ----------

LEADING_COMMENTS = re.compile(r"^(\s*(--[^\n]*|/\*.*?\*/))*", re.S)


def run_sql(conn: sqlite3.Connection, query: str, seconds: float = QUERY_SECONDS) -> tuple[str, bool]:
    """Run one read-only query; return (compact text for Claude, is_error)."""
    query = str(query or "").strip().rstrip(";").strip()
    # A first, friendly filter only: lock() is what actually keeps the snapshot read-only.
    if not LEADING_COMMENTS.sub("", query).lstrip(" \t\r\n(").lower().startswith(("select", "with", "values")):
        return "Only a single SELECT (or WITH ... SELECT) query is allowed.", True
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
    try:
        cur = conn.execute(query)
        rows = cur.fetchmany(MAX_ROWS + 1)
        cols = [d[0] for d in cur.description or []]
        cur.close()
    except (sqlite3.Error, sqlite3.Warning, OverflowError, ValueError) as e:
        if "interrupted" in str(e):
            return f"Query stopped: it ran longer than {seconds:g} s. Simplify it.", True
        if "not authorized" in str(e) or "authorization denied" in str(e):
            return "Not allowed: the log is read-only; only SELECT queries work.", True
        return f"SQL error: {e}", True
    finally:
        conn.set_progress_handler(None, 0)
    if not rows:
        return " | ".join(cols) + "\n(no rows)", False
    lines = [" | ".join(cols)] + [" | ".join(_cell(v) for v in row) for row in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        lines.append(f"(stopped at {MAX_ROWS} rows: aggregate or add LIMIT)")
    text = "\n".join(lines)
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n(output cut: select fewer columns or rows)"
    return text, False


def _cell(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    s = " ".join(str(v).split())
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "…"


def sql_tool(conn: sqlite3.Connection, calls: list):
    @tool("sql", "Run one read-only SQLite SELECT on the baby log snapshot (tables: events, feed_sessions, "
          f"day_segments, days). Returns up to {MAX_ROWS} rows as 'col | col' lines.", {"query": str},
          annotations=ToolAnnotations(readOnlyHint=True))
    async def sql(args):
        calls.append(args.get("query"))
        text, is_error = run_sql(conn, args.get("query"))
        return {"content": [{"type": "text", "text": text}], "is_error": is_error}
    return sql


# ---------- the prompt ----------

def _clock(t: dt.datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def glance(conn: sqlite3.Connection, zone: ZoneInfo) -> str:
    """One short paragraph so Claude knows the shape of the data before its first query."""
    n, first, last = conn.execute("SELECT count(*), min(start_local), max(local_date) FROM events").fetchone()
    if not n:
        return "the log is empty."
    first = dt.datetime.fromisoformat(first)   # its time shows that the first day is partial
    kinds = dict(conn.execute("SELECT kind, count(*) FROM events GROUP BY kind"))
    sessions = conn.execute("SELECT count(*) FROM feed_sessions").fetchone()[0]
    parts = [f"{n} entries from {first:%Y-%m-%d} {_clock(first)} (first entry) to {last}: "
             f"{kinds.get('feed', 0)} feed rows ({sessions} feed sessions), {kinds.get('diaper', 0)} diapers, "
             f"{kinds.get('sleep', 0)} sleeps, {kinds.get('activity', 0)} activities."]
    for kind, name, start, minutes in conn.execute(
            "SELECT kind, name, start_utc, duration_min FROM events WHERE running ORDER BY start_utc"):
        t = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).astimezone(zone)
        parts.append(f"Running now: {name or kind} since {_clock(t)} ({minutes:.0f} min).")
    names = conn.execute("SELECT min(name), count(*) FROM events WHERE kind = 'activity' GROUP BY lower(name)"
                         " ORDER BY count(*) DESC LIMIT 15").fetchall()
    if names:
        parts.append("Activities: " + ", ".join(f"{a} ({c})" for a, c in names) + ".")
    return " ".join(parts)


def system_prompt(conn, zone: ZoneInfo, now: dt.datetime, baby_name, unit) -> str:
    local = now.astimezone(zone)
    name = " ".join(str(baby_name or "").split())[:40]
    return SYSTEM_PROMPT.format(
        baby=f"The baby's name is {name}." if name else "The baby's name isn't set; say \"the baby\".",
        now_local=f"{local:%A %Y-%m-%d} {_clock(local)}", zone=zone.key, now_utc=_utc(now),
        today=local.date().isoformat(), yesterday=(local.date() - dt.timedelta(days=1)).isoformat(),
        max_rows=MAX_ROWS, schema=SCHEMA, glance=glance(conn, zone),
        unit_rule=UNIT_RULES.get(unit, UNIT_RULES["ml"]))


def user_message(question: str, history) -> str:
    turns = [t for t in (history or []) if isinstance(t, dict)][-6:]
    lines = []
    if turns:
        lines.append("Earlier in this conversation (for context; the log may have changed since):")
        for t in turns:
            lines += [f"Q: {str(t.get('q') or '')[:1000]}", f"A: {str(t.get('a') or '')[:4000]}", ""]
    lines.append(f"Question: {question}")
    return "\n".join(lines)


# ---------- entry point ----------

def ask(*, question, history, tz, baby_name, unit, rows, now=None) -> dict:
    """Answer one question about the log. Returns {answer, elapsed_ms, model, queries}; raises AIError."""
    question = str(question or "").strip()
    if not question:
        raise AIError("Type a question first.", 400)
    if len(question) > 1000:
        raise AIError("That question is too long (max 1000 characters).", 400)
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = zone_for(tz)
    conn = build_snapshot(rows, tz, now)
    calls: list = []
    try:
        server = create_sdk_mcp_server("log", tools=[sql_tool(conn, calls)])
        result = claude_ai.run_claude(
            user_message(question, history), system_prompt=system_prompt(conn, zone, now, baby_name, unit),
            model=MODEL, timeout_s=TIMEOUT_S, max_turns=MAX_TURNS,
            mcp_servers={"log": server}, allowed_tools=["mcp__log__sql"])
    finally:
        conn.close()
    answer = result.text.strip()
    if not answer:
        raise AIError("Claude didn't give an answer. Try again.")
    return {"answer": answer, "elapsed_ms": result.elapsed_ms, "model": result.model, "queries": len(calls)}
