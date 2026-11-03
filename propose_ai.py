"""Photo and/or text -> proposed log entries (creates and edits), via Claude.

Claude reads the photo (if any), the parent's message (if any), and a text summary of the
log (running timers, the last 48 h of entries with ids), and answers with flat items in
local wall-clock time. This module turns those into changes the server can validate:
  {"op": "create", "event": {kind, start_at, end_at, data, note}, "needs": [], "confidence", "why"}
  {"op": "update", "id": 12, "patch": {start_at?, end_at?, data?, note?}, "confidence", "why"}
A create whose end isn't known (a sleep, breast feed, or timed activity) also has
  "open_end": {"default": "running" | "ended" | "unknown", "ended_at", "reason"}
(see open_end()); its event's end_at follows that default, so it can be saved as it is.
Nothing is saved here: the server dry-runs each change with its own validation, and the
parent confirms in the page.

Pure apart from propose() (which calls Claude). No imports from server.py.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import claude_ai

_MODEL_ENV = os.environ.get("BABY_AI_MODEL") or os.environ.get("BABY_PHOTO_MODEL")
MODEL = _MODEL_ENV or "sonnet"
TIMEOUT_S = float(os.environ.get("BABY_AI_TIMEOUT", "45"))
EFFORTS = ("low", "medium", "high", "xhigh", "max")   # Claude Code's --effort levels


def _effort(value: str | None) -> str | None:
    """BABY_AI_EFFORT: one of EFFORTS, or "default"/empty to let Claude Code decide.
    Unset: "low" for the default model (in the eval, as accurate as Claude Code's default
    effort and faster: less thinking before the answer); Claude Code's default for a model
    chosen with BABY_AI_MODEL."""
    v = (value or "").strip().lower()
    if v in ("", "default", "none"):
        return None
    if v not in EFFORTS:
        print(f"propose: ignoring BABY_AI_EFFORT={value!r} (use one of {', '.join(EFFORTS)})", file=sys.stderr)
        return None
    return v


EFFORT = _effort(os.environ.get("BABY_AI_EFFORT", "" if _MODEL_ENV else "low"))

UTC = dt.timezone.utc
ML_PER_OZ = 29.5735
SESSION_GAP = dt.timedelta(minutes=15)   # feeds closer than this are one session (as in the page)
SLACK = dt.timedelta(minutes=1)          # "now" tolerance, as in the page's resolvePast()
CONTEXT_HOURS = 48
MAX_CONTEXT_ROWS = 80
MAX_ITEMS = 12
# How long ago an entry with no known end may have started and still plausibly be going on now (open_end())
STILL_GOING = {"sleep": dt.timedelta(hours=4), "feed": dt.timedelta(hours=1), "activity": dt.timedelta(hours=2)}
MIN_ENDED = dt.timedelta(minutes=5)   # an "ended" default shorter than this is no guess (e.g. a photo 1 min before the next entry)

KINDS = ("feed", "diaper", "sleep", "activity")
SIDES = ("left", "right", "both")
MILKS = ("formula", "breastmilk")
COLORS = ("yellow", "orange", "green", "brown", "black", "red", "pale")
TEXTURES = ("watery", "loose", "seedy", "soft", "pasty", "formed", "hard")
COLOR_ALIASES = {"mustard": "yellow", "white": "pale", "grey": "pale", "gray": "pale", "chalky": "pale",
                 "clay": "pale", "tan": "brown", "dark green": "green"}
TIMINGS = ("moment", "range", "running", "needs_duration")

# ---------- what Claude answers ----------


def _nullable(kind: str, enum=None, **extra) -> dict:
    s = {"type": [kind, "null"], **extra}
    if enum:
        s["enum"] = [*enum, None]
    return s


LOCAL_TIME = _nullable("string", pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$",
                       description="local wall-clock time YYYY-MM-DDTHH:MM (never UTC)")

ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op", "id", "kind", "start", "timing", "note", "confidence", "why"],
    "properties": {
        "op": {"type": "string", "enum": ["create", "update"]},
        "id": _nullable("integer", description="update: the #id of the entry to change; create: null"),
        "kind": {"type": "string", "enum": list(KINDS)},
        "start": LOCAL_TIME,
        "end": LOCAL_TIME,
        "duration_min": _nullable("number"),
        "timing": _nullable("string", TIMINGS),
        "method": _nullable("string", ("breast", "bottle")),
        "side": _nullable("string", SIDES),
        "milk": _nullable("string", MILKS),
        "amount": _nullable("number", description="bottle amount as read or said"),
        "amount_unit": _nullable("string", ("ml", "oz")),
        "pee": _nullable("boolean"),
        "poop": _nullable("boolean"),
        "color": _nullable("string", COLORS),
        "texture": _nullable("string", TEXTURES),
        "activity_name": _nullable("string"),
        "place": _nullable("string"),
        "note": _nullable("string"),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "why": {"type": "string"},
    },
}

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "message"],
    "properties": {
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "message": {"type": "string"},
    },
}

SYSTEM_PROMPT = """You fill in entries for a parent's private baby log from a photo and/or a short message. The parent is tired, often holding the baby, and saves what you propose with one tap, so every detail must be right. Propose only what the photo or message supports. Never invent amounts, sides, durations, or times: leave unknown fields null.

You only propose: nothing is saved until the parent taps Save, and the app shows each proposal as a card with all its details.

The user turn gives you the current local time, the photo's capture time, running timers, the log entries of the last 48 hours (each with an #id), the suggested breast side, known activity names, and the parent's message. All times there are the parent's local wall-clock time, and so are the times you write: "YYYY-MM-DDTHH:MM" with the full date. Never UTC, never a time in the future.

# Entry kinds
- feed, method "breast": side "left" | "right" | "both". Timed.
- feed, method "bottle": milk "formula" | "breastmilk" (null unless there is evidence: a formula tin, scoop, or powder in view, or the parent says so); amount + amount_unit exactly as read or said ("4 oz" -> 4, "oz"; the app converts). A single moment.
- diaper: pee and poop (true/false); when poop is true, also color and texture if seen or said. A single moment.
  color: yellow (incl. mustard), orange, green, brown, black, red, pale (white, grey, chalky, very pale).
  texture: watery, loose, seedy, soft, pasty, formed, hard.
- sleep: timed.
- activity: everything else, with activity_name. Reuse a known name when it fits, with the same spelling. Usual names: Tummy time, Play, Bath, Reading, Outdoors, Walk, Solids (spoon or finger food: the food goes in the note), Vitamin D.
- place (any kind): where it happened, when visible or said, in a few lowercase words: "crib", "bassinet", "car seat", "stroller", "play mat", "bouncer", "bathtub", "changing table", "high chair", "parents' bed", "in arms", "park". Else null.

# timing (for creates)
- "moment": one point in time: diapers, bottles, an activity noted once. Put the time in start; end null.
- "range": finished, with start and end known (give end, or duration_min).
- "running": started at start and still going now (the app runs a timer).
- "needs_duration": finished, start known, but not how long it lasted.
Breast feeds and sleep are never a "moment". Activities are a "moment" unless a duration or a running timer fits (tummy time, outdoors and walks are often timed).
When the end isn't known, the card lets the parent choose Still going, Ended (with its end), or Don't know (saved without a duration), so never skip an entry because you don't know how long it lasted. No end or duration said ("she fell asleep at 2", "nursing on the left since 1:30") -> "running": a parent who doesn't say how long usually means it's still going. "needs_duration" only when the parent's words say it's over ("she napped around noon", "nursed her at 11") or an entry logged after it shows it ended. In message and why, don't say whether such an entry is still going, ended, or has no duration: the card preselects that choice and shows its own reason.

# One event, one entry
Before each create, look for a log entry of the same kind (activities: same name) that it could be:
- Already logged (diapers and bottles within about 15 minutes with matching details; sleeps, breast feeds, or timed activities whose times overlap) -> don't create it again. If the parent adds or corrects details or times, update that entry instead (e.g. log has a nap 10:00-11:30 and the parent says "napped 10:15 to 11:45": update its start and end).
- A running timer that fits is the same event: "she woke up at 1:55" or "she slept from 1:20 to 1:55" ends the running sleep; it is not a new sleep.
- A running timer that can't be the same event: a new sleep while an old sleep timer runs, or a timer marked "probably left on" (an entry logged after its start shows the baby was awake or done). End it with an update at its likely real end: the start of the entry named in that mark, else the new entry's start. Give that update confidence 0.5 or less and mention it in message. Then create the new entry.
- Never leave two sleeps, or two breast feeds, overlapping in time. If you can't tell whether it's the same event, don't create; ask in message.

# Photos
The photo's capture time is the entry time. Look closely at the details.
- Breastfeeding: no feed timer running -> a breast feed starting at the photo time: "running" if it could plausibly still be going now and nothing logged after the photo contradicts that, else "needs_duration". A feed timer already running -> no new feed. Side only when you can clearly tell; otherwise use the suggested side and say so in why.
- Bottle: a bottle feed at the photo time. Read the amount from the bottle's markings when legible (find the milk level on the ml or oz scale) and say in why that it is the amount in the bottle at photo time. Formula vs breast milk only with evidence.
- Asleep (eyes closed, settled): no sleep timer running -> a sleep starting at the photo time: "running" if the baby could plausibly still be asleep now and nothing logged after the photo contradicts that, else "needs_duration". A sleep timer already running (not marked "probably left on") -> no new entry; say so in message. One marked "probably left on" -> end it as in "One event, one entry", then start a new sleep at the photo time.
- Awake (eyes open) while a sleep timer is running and the photo was taken after that sleep started -> update that sleep: end at the photo time. Add an entry for what the baby is doing too if it is loggable (feeding, tummy time...).
- A feed timer running while the photo clearly shows the baby not feeding (asleep alone in the crib, playing on the mat) -> update that feed to end at the photo time, with lower confidence.
- Diaper in view: pee from a wet, swollen look or a wetness indicator line that changed color (usually yellow -> blue); poop only when stool is clearly visible in or on the diaper, with its color and texture. If you can't see what was in it, treat it like "changed a diaper": pee true, poop false, low confidence, and say so in why.
- Play, tummy time, bath, reading, outdoors, solids -> an activity at the photo time.
- A handwritten log, daycare sheet, or app screenshot -> one entry per line, with its times.
- The parent took the photo to log something. If it is ambiguous (e.g. a baby lying still in a crib, face not visible), propose the most likely entry with low confidence and say what you couldn't tell in why, rather than nothing.
- No baby and nothing loggable -> no items; say briefly what you see in message.

# Messages from the parent
They may describe several events, edits, or both. One item per event, oldest first.
- Resolve times against now. A bare clock time ("at 3") means the most recent past occurrence, am or pm, of that time. "20 min ago", "an hour later", "till 5" are relative to now or to the event just mentioned.
- "then she slept till 5" without a start: start at the time of the event mentioned just before it (for a bottle or diaper, its time; for a breast feed, its end). If nothing gives the start: a running sleep timer -> end it (update); otherwise propose nothing for it and ask in message.
- Edits change an existing entry: op "update", its #id, its kind, and only the fields that change; every other field null (null = keep). Examples: "the last feed was the right side" (side of the latest feed); "she woke up at 4:10" (end the running sleep at 4:10, or fix the end of the latest sleep); "stop the feed" (end the running feed now); "the 3 am bottle was 5 oz" (amount). A note on an update replaces the whole note.
- "she just fell asleep" / "starting a feed on the left" -> running from now; "fell asleep 10 min ago" -> running from 10 min ago.
- "changed a diaper" with no details: pee true, poop false, and say in why that you assumed wet. "Poopy"/"dirty": poop true (pee only if said). "Wet": pee only.
- You can't delete entries: say so in message (the parent can delete from the timeline).
- A question instead of something to log ("when did she last eat?") -> no items; message: "That's a question: tap Ask."
- Medicine or vitamins the parent mentions: an activity named for it, their words in the note. Never comment on doses.

# Notes
note: for a photo, 1-2 short, specific sentences a parent will love to remember later: what the baby is doing, expression and mood, toys, clothes, the setting. E.g. "Wide awake on the play mat, kicking at the hanging giraffe and smiling." For a message, only extra details the parent said that have no field ("spit up a little", "fussy before"); else "". Don't repeat the fields (time, side, amount, place). Don't describe or name adults or anyone else, and never guess who someone is.

# Every item
- confidence: honest, 0-1. Lower it for anything you assumed.
- why: one short evidence line: "formula tin in view; bottle reads ~120 ml at photo time", "you said 4 oz at 3".

# message (shown above the cards)
- With proposals: one short sentence, under 15 words, about what you saw or understood, worded as a suggestion, never as done. Good: "Mia asleep in the stroller.", "Here's the bottle and the poopy diaper.", "I'd end the nap at 1:55.". Never write that you logged, added, saved, updated, changed, ended, stopped, or started anything: nothing is saved yet. Don't repeat what the cards show (times, amounts, sides, colors).
- Add a second short sentence only to flag something the parent should check: an assumed time or side, or an old timer you propose to end ("I guessed the diaper's time.").
- With no proposals: one or two short sentences on why (already in the log, a question, what you see).

# Style for message, why, and note
- Refer to log entries by kind and time ("the 12:15 pm feed"), never by #id.
- The baby: the baby's name if given, else "baby" or "the baby". Write he/him/his or she/her only if the parent's message calls the baby that; never guess the baby's sex from a photo, clothes, or a name. With no message, use no pronouns for the baby.
- Clock times the way the parent writes them; if unsure, 12-hour ("2:05 pm").
- The language of the parent's message (English for a photo alone).
- No health judgments, no sleep-safety advice, nothing about medicine doses, no identifying people."""


# ---------- context ----------

@dataclass
class Ctx:
    """Everything a proposal is resolved against. Datetimes are timezone-aware."""
    now: dt.datetime
    zone: ZoneInfo
    photo_at: dt.datetime | None = None     # capture time (now if unknown); None without a photo
    events: list[dict] = field(default_factory=list)   # rows as GET /api/events returns them, newest first
    activity_names: list[str] = field(default_factory=list)
    baby_name: str = ""
    unit: str = "ml"

    @property
    def ref_at(self) -> dt.datetime:
        """Default time for a new entry: when the photo was taken, else now."""
        return self.photo_at or self.now


def parse_utc(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wall(t: dt.datetime, zone: ZoneInfo, date: bool = True) -> str:
    return t.astimezone(zone).strftime("%Y-%m-%d %H:%M" if date else "%H:%M")


def fmt_dur(d: dt.timedelta) -> str:
    m = max(0, int(d.total_seconds() // 60))
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def ago(t: dt.datetime, now: dt.datetime) -> str:
    return "just now" if now - t < dt.timedelta(minutes=1) else f"{fmt_dur(now - t)} ago"


def ml_text(ml, unit: str) -> str:
    if ml is None:
        return "amount not set"
    return f"{ml:g} ml ({ml / ML_PER_OZ:.1f} oz)" if unit == "oz" else f"{ml:g} ml"


def describe(ev: dict, unit: str) -> str:
    d = ev["data"]
    if ev["kind"] == "feed":
        if d.get("method") == "bottle":
            milk = {"formula": "formula", "breastmilk": "breast milk"}.get(d.get("milk"), "milk not set")
            what = f"bottle feed, {milk}, {ml_text(d.get('amount_ml'), unit)}"
        else:
            what = f"breast feed, {'both sides' if d.get('side') == 'both' else str(d.get('side')) + ' side'}"
    elif ev["kind"] == "diaper":
        what = "diaper: " + ("pee + poop" if d.get("pee") and d.get("poop") else
                             "poop" if d.get("poop") else "pee" if d.get("pee") else "dry")
        what += "".join(f", {d[k]}" for k in ("color", "texture") if d.get(k) and d.get("poop"))
    elif ev["kind"] == "activity":
        what = f'activity "{d.get("name")}"'
    else:
        what = "sleep"
    if d.get("place"):
        what += f", place: {d['place']}"
    return what


def _what(ev: dict) -> str:
    if ev["kind"] == "feed":
        return "a bottle feed" if ev["data"].get("method") == "bottle" else "a breast feed"
    if ev["kind"] == "activity":
        return f'"{ev["data"].get("name")}"'
    return f"a {ev['kind']}"


ENDED_BY = {"sleep": ("feed", "activity"), "feed": ("sleep", "activity", "feed"), "activity": ("sleep", "activity")}


def ended_by(ev: dict, rows: list[dict]) -> dict | None:
    """The first of `rows` to start after `ev` that shows `ev` was over by then (the baby was awake
    or done): a feed or activity after a sleep; a sleep, activity, or other feed after a feed; a
    sleep or other activity after an activity. Diapers don't count (night changes, changes
    between sides). None if nothing does."""
    later = [e for e in rows if e["start_at"] > ev["start_at"] and e["kind"] in ENDED_BY.get(ev["kind"], ())]
    return min(later, key=lambda e: (e["start_at"], e["id"]), default=None)


def left_on_by(ev: dict, rows: list[dict]) -> dict | None:
    """For a running sleep or feed timer: the entry that shows the timer was probably left on (ended_by)."""
    if ev["end_at"] is not None or ev["kind"] not in ("sleep", "feed"):
        return None
    return ended_by(ev, rows)


def event_line(ev: dict, ctx: Ctx, rows: list[dict] | None = None) -> str:
    start = parse_utc(ev["start_at"])
    if ev["end_at"] is None:
        when = f"{wall(start, ctx.zone)}, RUNNING ({fmt_dur(ctx.now - start)} so far"
        if by := left_on_by(ev, rows or []):
            t = parse_utc(by["start_at"])
            same_day = t.astimezone(ctx.zone).date() == start.astimezone(ctx.zone).date()
            when += f"; probably left on: {_what(by)} at {wall(t, ctx.zone, date=not same_day)} was logged after it started"
        when += ")"
    elif ev["end_at"] == ev["start_at"]:
        when = wall(start, ctx.zone)
        if ev["kind"] == "sleep" or ev["data"].get("method") == "breast":
            when += " (no duration logged)"
    else:
        end = parse_utc(ev["end_at"])
        same_day = start.astimezone(ctx.zone).date() == end.astimezone(ctx.zone).date()
        when = f"{wall(start, ctx.zone)} to {wall(end, ctx.zone, date=not same_day)} ({fmt_dur(end - start)})"
    line = f"#{ev['id']} | {when} | {describe(ev, ctx.unit)}"
    note = " ".join((ev.get("note") or "").split())
    if note:
        line += f' | note: "{note[:150]}{"..." if len(note) > 150 else ""}"'
    return line


def suggested_side(events: list[dict], now: dt.datetime) -> str | None:
    """The side the last breastfeeding session did NOT start on (same rule as suggestedSide() in the page)."""
    feeds = sorted((e for e in events if e["kind"] == "feed"), key=lambda e: (e["start_at"], e["id"]))
    sessions: list[list[dict]] = []
    for f in feeds:
        if sessions and parse_utc(f["start_at"]) - max(
                parse_utc(x["end_at"]) if x["end_at"] else now for x in sessions[-1]) <= SESSION_GAP:
            sessions[-1].append(f)
        else:
            sessions.append([f])
    for s in reversed(sessions):
        first = next((x for x in s if x["data"].get("method") == "breast"), None)
        if first:
            return {"left": "right", "right": "left"}.get(first["data"].get("side"))
    return None


def context_events(ctx: Ctx) -> list[dict]:
    """The rows Claude sees (and may edit): running timers, plus the last 48 h, newest first."""
    since = iso(ctx.now - dt.timedelta(hours=CONTEXT_HOURS))
    recent = [e for e in ctx.events if e["end_at"] is not None and e["end_at"] >= since][:MAX_CONTEXT_ROWS]
    running = [e for e in ctx.events if e["end_at"] is None]   # always, however old
    return sorted(running + recent, key=lambda e: (e["start_at"], e["id"]), reverse=True)


def build_context(ctx: Ctx, text: str | None) -> str:
    """The text block Claude gets after the photo."""
    now_local = ctx.now.astimezone(ctx.zone)
    lines = [f"Now: {now_local:%A}, {wall(ctx.now, ctx.zone)} ({ctx.zone.key})."]
    if ctx.photo_at is not None:
        if ctx.now - ctx.photo_at < dt.timedelta(minutes=1):
            lines.append(f"Photo: just taken (or capture time unknown): treat it as taken now, {wall(ctx.now, ctx.zone)}.")
        else:
            lines.append(f"Photo: taken {wall(ctx.photo_at, ctx.zone)}, {ago(ctx.photo_at, ctx.now)}.")
    lines.append(f"Baby's name: {ctx.baby_name}." if ctx.baby_name else "Baby's name: not set (say \"baby\").")
    lines.append("The parent's unit: oz (1 oz = 29.6 ml)." if ctx.unit == "oz" else "The parent's unit: ml.")
    side = suggested_side(ctx.events, ctx.now)
    lines.append(f"Suggested breast side for the next feed: {side} (the last session started on the other side)."
                 if side else "Suggested breast side: none.")
    names = ", ".join(ctx.activity_names[:20])
    lines.append(f"Known activity names: {names}." if names else "Known activity names: none yet.")

    rows = context_events(ctx)
    running = [e for e in rows if e["end_at"] is None]
    lines += ["", "Running timers:"] + ([event_line(e, ctx, rows) for e in running] or ["none"])
    lines += ["", f"Log entries of the last {CONTEXT_HOURS} h, newest first (#id | local time | what | note):"]
    lines += [event_line(e, ctx, rows) for e in rows] or ["none"]
    lines.append("")
    if text:
        about = " about the photo" if ctx.photo_at is not None else ""
        lines += [f"The parent's message{about} (their words, between <<< and >>>):", "<<<", text, ">>>"]
    else:
        lines.append("No message: propose entries from the photo.")
    return "\n".join(lines)


# ---------- Claude's items -> changes ----------

class Skip(Exception):
    """An item that can't become a sensible change (the reason goes to the server log)."""


def _enum(value, allowed, aliases=None):
    if not isinstance(value, str):
        return None
    v = " ".join(value.lower().split())
    v = (aliases or {}).get(v, v)
    return v if v in allowed else None


def _number(value) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _text(value, limit: int) -> str:
    return " ".join(value.split())[:limit].strip() if isinstance(value, str) else ""


ID_PAREN = r"\s*\(#({ids})\)"              # " (#12)"
ID_REF = r"#({ids})\b(?:,\s*|\s+)?"          # "#12 ", "#12, "
SPACE_PUNCT = re.compile(r"\s+([.,;:!?)])")


def scrub_ids(text: str, ids) -> str:
    """Parent-facing text (message, why, note) must not show internal row ids ("sleep #12"):
    the parent never sees them. Claude is told so; this removes any that slip through.
    Only ids of rows Claude was shown, so "bottle #2 of the day" stays."""
    alt = "|".join(str(i) for i in sorted(ids, reverse=True))
    if "#" not in text or not alt:
        return text
    text = re.sub(ID_REF.format(ids=alt), "", re.sub(ID_PAREN.format(ids=alt), "", text))
    text = SPACE_PUNCT.sub(r"\1", text).replace("()", "")
    return " ".join(text.split())


def _say(value, limit: int, ctx: "Ctx") -> str:
    """Parent-facing text from Claude: one line, trimmed, without row ids."""
    return scrub_ids(_text(value, limit), [e["id"] for e in context_events(ctx)])


def _confidence(value) -> float | None:
    v = _number(value)
    if v is None:
        return None
    if 2 <= v <= 100:   # "85" meaning 85%
        v /= 100
    return round(min(1.0, max(0.0, v)), 2)


LOCAL_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*$")


def parse_wall(value) -> dt.datetime | None:
    """'YYYY-MM-DDTHH:MM' -> naive local datetime. Anything with an offset or 'Z' is refused (UTC by mistake)."""
    m = LOCAL_RE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    try:
        return dt.datetime(*map(int, m.groups()))
    except ValueError:
        return None


def wall_to_utc(naive: dt.datetime, zone: ZoneInfo) -> dt.datetime:
    """Local wall-clock -> UTC. DST: a time skipped in spring maps an hour later; a repeated time
    in autumn is its first occurrence (fold=0)."""
    return naive.replace(tzinfo=zone).astimezone(UTC)


def local_to_utc(value, zone: ZoneInfo, now: dt.datetime) -> dt.datetime | None:
    """Claude's local time -> UTC. A time in the future means the last time it was that o'clock:
    move it back one day (in wall-clock terms, so DST-safe), like resolvePast() in the page."""
    naive = parse_wall(value)
    if naive is None:
        return None
    t = wall_to_utc(naive, zone)
    if t > now + SLACK:
        prev = wall_to_utc(naive - dt.timedelta(days=1), zone)
        if prev <= now + SLACK:
            return prev
    return t


def _snap(t: dt.datetime, ctx: Ctx) -> dt.datetime:
    """Claude writes whole minutes: a time in the same minute as the photo or now means that exact instant
    (so "stop the feed" ends it now, not up to 59 s earlier)."""
    for ref in (ctx.photo_at, ctx.now):
        if ref is not None and _same_minute(t, ref):
            return ref
    return t


def _end(item: dict, start: dt.datetime, ctx: Ctx) -> dt.datetime | None:
    """End time from 'end' (local) or 'duration_min'; None if missing or not after start / in the future."""
    naive = parse_wall(item.get("end"))
    if naive is not None:
        # as given; or the next day (an end after midnight written with the start's date); or the day before
        for days in (0, 1, -1):
            end = _snap(wall_to_utc(naive + dt.timedelta(days=days), ctx.zone), ctx)
            if start < end <= ctx.now + SLACK and end - start <= dt.timedelta(hours=24):
                return end
        return None
    minutes = _number(item.get("duration_min"))
    if minutes is not None and 0 < minutes <= 24 * 60:
        end = start + dt.timedelta(minutes=minutes)
        return end if end <= ctx.now + SLACK else None
    return None


def _amount_ml(item: dict, unit: str) -> float | None:
    amount = _number(item.get("amount"))
    if amount is None or amount <= 0:
        return None
    if (_enum(item.get("amount_unit"), ("ml", "oz")) or unit) == "oz":
        amount *= ML_PER_OZ
    return round(amount, 1) if amount <= 1000 else None


def _activity_name(value, known: list[str]) -> str | None:
    name = _text(value, 60)
    if not name:
        return None
    for k in known:   # reuse the spelling already in the log
        if k and k.lower() == name.lower():
            return k
    return name[0].upper() + name[1:]


def _data(kind: str, item: dict, ctx: Ctx, base: dict) -> dict:
    """The entry's data: `base` (the row's current data for an update, {} for a create) with
    the details Claude gave. Null fields keep what base has."""
    d = dict(base)
    if kind == "feed":
        method = _enum(item.get("method"), ("breast", "bottle")) or d.get("method")
        if method is None:   # infer from the details Claude did give
            if _number(item.get("amount")) is not None or _enum(item.get("milk"), MILKS):
                method = "bottle"
            elif _enum(item.get("side"), SIDES):
                method = "breast"
            else:
                raise Skip("feed without a method")
        d["method"] = method
        if method == "breast":
            # same default as the page's feed sheet: the suggested side, else left
            d["side"] = (_enum(item.get("side"), SIDES) or d.get("side")
                         or suggested_side(ctx.events, ctx.now) or "left")
        else:
            d["milk"] = _enum(item.get("milk"), MILKS) or d.get("milk")
            amount = _amount_ml(item, ctx.unit)
            d["amount_ml"] = amount if amount is not None else d.get("amount_ml")
    elif kind == "diaper":
        for k in ("pee", "poop"):
            if isinstance(item.get(k), bool):
                d[k] = item[k]
            d.setdefault(k, False)
        if d["poop"]:
            color = _enum(item.get("color"), COLORS, COLOR_ALIASES)
            texture = _enum(item.get("texture"), TEXTURES)
            if color:
                d["color"] = color
            if texture:
                d["texture"] = texture
    elif kind == "activity":
        name = _activity_name(item.get("activity_name"), ctx.activity_names)
        if name:
            d["name"] = name
        if not d.get("name"):
            raise Skip("activity without a name")
    place = _text(item.get("place"), 40)
    if place:
        d["place"] = place
    return d


def _start(item: dict, ctx: Ctx) -> dt.datetime:
    if item.get("start") in (None, ""):
        return ctx.ref_at
    t = local_to_utc(item["start"], ctx.zone, ctx.now)
    if t is None:
        raise Skip(f"unreadable start {item['start']!r}")
    if t > ctx.now + SLACK:
        raise Skip("start is in the future")
    return _snap(t, ctx)


def _same_minute(a: dt.datetime, b: dt.datetime) -> bool:
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


def _create(item: dict, ctx: Ctx) -> dict:
    kind = item.get("kind")
    if kind not in KINDS:
        raise Skip(f"unknown kind {kind!r}")
    data = _data(kind, item, ctx, {})
    start = _start(item, ctx)
    timing = item.get("timing")
    if kind == "diaper" or (kind == "feed" and data["method"] == "bottle"):
        end = start                                   # point events
    elif timing == "running":
        end = None
    elif timing == "moment" and kind == "activity":
        end = start
    else:   # range / needs_duration; a breast feed or sleep is never a moment
        end = _end(item, start, ctx)
        if end is None and kind == "activity" and timing is None:
            end = start
    event = {"kind": kind, "start_at": iso(start), "end_at": iso(end) if end else None,
             "data": data, "note": _say(item.get("note"), 500, ctx)}
    change = {"op": "create", "event": event, "needs": []}
    if end is None:   # running, or the end isn't known: the parent picks Still going / Ended / Don't know
        change["open_end"] = oe = open_end(event, timing, ctx)
        event["end_at"] = {"running": None, "ended": oe["ended_at"], "unknown": event["start_at"]}[oe["default"]]
    return change


def clock(t: dt.datetime, ctx: Ctx) -> str:
    """Parent-facing local time: "1:15 PM", "yesterday 11:40 PM", "Sat 9:05 AM"."""
    local = t.astimezone(ctx.zone)
    days = (ctx.now.astimezone(ctx.zone).date() - local.date()).days
    day = "" if days == 0 else "yesterday " if days == 1 else f"{local:%a} "
    return day + local.strftime("%I:%M %p").lstrip("0")


def open_end(ev: dict, timing, ctx: Ctx) -> dict:
    """The preselected choice for a proposed sleep, breast feed, or timed activity with no known end:
    1. "ended" at the start of the first log entry after it that shows it was over (ended_by), if that
       makes a plausible length (MIN_ENDED up to STILL_GOING for its kind). A longer gap -> "unknown", with that
       entry's start kept in ended_at as a hint the page prefills if the parent picks "Ended…";
    2. else "running" if Claude said "running", or if Claude didn't say it's finished
       ("needs_duration") and it started recently enough to still be going (STILL_GOING);
    3. else "unknown": saved without a duration (end == start).
    Returns {"default", "ended_at" (UTC or None), "reason" (parent-facing)}."""
    start = parse_utc(ev["start_at"])
    if by := ended_by(ev, context_events(ctx)):
        at = parse_utc(by["start_at"])
        if MIN_ENDED <= at - start <= STILL_GOING[ev["kind"]]:
            return {"default": "ended", "ended_at": by["start_at"],
                    "reason": f"{_what(by)} was logged at {clock(at, ctx)}"}
        # e.g. a breast feed at 10 AM and the next bottle at 1:15 PM: over by then, but a 3 h feed is no
        # guess; nor is a 1-minute nap from a sleeping photo taken just before the next entry
        return {"default": "unknown", "ended_at": by["start_at"],
                "reason": f"over by {clock(at, ctx)} ({_what(by)}), "
                          + ("too long to guess the end" if at - start > MIN_ENDED else "too soon to tell how long")}
    if timing == "running" or (timing != "needs_duration" and ctx.now - start <= STILL_GOING[ev["kind"]]):
        return {"default": "running", "ended_at": None,
                "reason": f"started {ago(start, ctx.now)}, no end logged"}
    return {"default": "unknown", "ended_at": None,
            "reason": "probably over, no end time given" if timing == "needs_duration"
            else f"started {ago(start, ctx.now)}, no end time given"}


def _update(item: dict, ctx: Ctx) -> dict:
    raw_id = item.get("id")
    try:
        eid = int(str(raw_id).lstrip("#")) if not isinstance(raw_id, bool) else None
    except ValueError:
        eid = None
    ev = {e["id"]: e for e in context_events(ctx)}.get(eid)
    if ev is None:
        raise Skip(f"update of an entry Claude wasn't shown: {raw_id!r}")
    if item.get("kind") not in (None, ev["kind"]):
        raise Skip(f"update says {item.get('kind')!r} but #{eid} is a {ev['kind']}")
    patch: dict = {}
    start = parse_utc(ev["start_at"])
    if item.get("start") not in (None, ""):
        new_start = _start(item, ctx)
        if not _same_minute(new_start, start):
            patch["start_at"] = iso(new_start)
            start = new_start
    point = ev["kind"] == "diaper" or (ev["kind"] == "feed" and ev["data"].get("method") == "bottle")
    if not point:
        if item.get("end") not in (None, "") or item.get("duration_min") is not None:
            end = _end(item, start, ctx)
            if end is None:
                raise Skip(f"unusable end {item.get('end')!r} / duration {item.get('duration_min')!r}")
            if ev["end_at"] is None or not _same_minute(end, parse_utc(ev["end_at"])):
                patch["end_at"] = iso(end)
        elif item.get("timing") == "running" and ev["end_at"] is not None:
            patch["end_at"] = None   # e.g. "she's still asleep": the timer runs again
    data = _data(ev["kind"], item, ctx, ev["data"])
    if data != ev["data"]:
        patch["data"] = data
    note = _say(item.get("note"), 500, ctx)
    if note and note != ev["note"]:
        patch["note"] = note
    if not patch:
        raise Skip(f"update of #{eid} changes nothing")
    return {"op": "update", "id": eid, "patch": patch}


def to_change(item, ctx: Ctx) -> dict | None:
    """One item from Claude -> a create or update change (see the module docstring), or None
    (reason logged to stderr) when it can't be turned into something sensible."""
    try:
        if not isinstance(item, dict):
            raise Skip("not an object")
        op = item.get("op")
        if op not in ("create", "update"):
            raise Skip(f"unknown op {op!r}")
        change = _create(item, ctx) if op == "create" else _update(item, ctx)
    except Skip as e:
        log(f"dropped item ({e}): {json.dumps(item)[:400]}")
        return None
    change["confidence"] = _confidence(item.get("confidence"))
    change["why"] = _say(item.get("why"), 200, ctx)
    return change


FOREVER = dt.datetime.max.replace(tzinfo=UTC)
DUPLICATE_GAP = dt.timedelta(minutes=5)   # the same diaper/bottle/activity logged twice


def _span(ev: dict) -> tuple[dt.datetime, dt.datetime]:
    """(start, end) of an entry: a running timer has no end yet. A create with an open end
    follows its default (its end_at): running, ended at ended_at, or unknown (just its start)."""
    start = parse_utc(ev["start_at"])
    return start, FOREVER if ev["end_at"] is None else parse_utc(ev["end_at"])


def _track(ev: dict) -> tuple | None:
    """Entries one baby can't have twice at the same time: sleeps, breast feeds, or activities of one name."""
    d = ev["data"]
    if ev["kind"] == "sleep":
        return ("sleep",)
    if ev["kind"] == "feed" and d.get("method") == "breast":
        return ("breast",)
    if ev["kind"] == "activity":
        return ("activity", str(d.get("name") or "").lower())
    return None


def _core(ev: dict) -> tuple:
    keys = {"diaper": ("pee", "poop"), "feed": ("method", "milk", "amount_ml"), "activity": ("name",)}
    return (ev["kind"], *(str(ev["data"].get(k)).lower() for k in keys.get(ev["kind"], ())))


def _clash(new: dict, old: dict) -> str | None:
    """Why `new` (a proposed create) would log `old` again, or None."""
    a0, a1 = _span(new)
    b0, b1 = _span(old)
    if a0 == a1 and b0 == b1:   # two moments: the same diaper, bottle, or activity twice?
        return "duplicates" if _core(new) == _core(old) and abs(a0 - b0) <= DUPLICATE_GAP else None
    if _track(new) is None or _track(new) != _track(old):
        return None
    if a0 == a1:
        hit = b0 <= a0 < b1
    elif b0 == b1:
        hit = a0 <= b0 < a1
    else:
        hit = max(a0, b0) < min(a1, b1)
    return "overlaps" if hit else None


def drop_double_logs(changes: list[dict], ctx: Ctx) -> list[dict]:
    """Safety net: drop a create that would log an event twice: the same diaper/bottle/activity
    within a few minutes, or a sleep, breast feed, or same-name activity overlapping one in the
    log or earlier in this batch (judged after this batch's updates, so "end the old nap, start a
    new one" is fine). That includes a new running timer while the same timer still runs:
    saving it would split the running nap or feed in two."""
    rows = {e["id"]: e for e in context_events(ctx)}
    for c in changes:
        if c["op"] == "update" and c["id"] in rows:
            rows[c["id"]] = {**rows[c["id"]], **{k: v for k, v in c["patch"].items()
                                                 if k in ("start_at", "end_at", "data")}}
    taken = list(rows.values())   # log rows, then the creates kept so far
    keep = []
    for c in changes:
        if c["op"] == "create":
            ev = c["event"]
            hit = next(((why, old) for old in taken if (why := _clash(ev, old))), None)
            if hit:
                why, old = hit
                log(f"dropped a {ev['kind']} at {ev['start_at']} that {why} "
                    f"{'#' + str(old['id']) if 'id' in old else 'another proposal'}")
                continue
            taken.append(ev)
        keep.append(c)
    return keep


def log(msg: str) -> None:
    print(f"propose: {msg}", file=sys.stderr, flush=True)


# ---------- entry point ----------

def propose(*, text: str | None, jpeg: bytes | None, photo_taken_at: dt.datetime | None, tz: str,
            baby_name: str, unit: str, recent_events: list[dict], activity_names: list[str],
            now: dt.datetime | None = None) -> dict:
    """Blocking: ask Claude, return {"source", "message", "changes", "dropped", "elapsed_ms", "model"}
    (dropped = how many of Claude's items couldn't be used).

    recent_events: rows as GET /api/events returns them (newest first), at least the last 48 h
    plus running timers. Raises claude_ai.AIError on failure."""
    now = now or dt.datetime.now(UTC)
    photo_at = None
    if jpeg is not None:   # an unknown or future capture time means just taken
        photo_at = min(photo_taken_at, now) if photo_taken_at else now
    ctx = Ctx(now=now, zone=ZoneInfo(tz), photo_at=photo_at, events=recent_events,
              activity_names=activity_names, baby_name=baby_name, unit=unit)
    content = [claude_ai.image_block(jpeg)] if jpeg is not None else []
    content.append(claude_ai.text_block(build_context(ctx, text)))
    # max_turns=3: a structured answer is usually one turn, but Claude Code sometimes needs another
    # to resubmit it (1 failed ~1 in 38 eval runs with "Reached maximum number of turns").
    r = claude_ai.run_claude(content, system_prompt=SYSTEM_PROMPT, model=MODEL, timeout_s=TIMEOUT_S,
                             max_turns=3, output_schema=OUTPUT_SCHEMA, effort=EFFORT)
    out = r.structured if isinstance(r.structured, dict) else {}
    items = out.get("items") if isinstance(out.get("items"), list) else []
    changes = []
    for item in items[:MAX_ITEMS]:
        try:
            if change := to_change(item, ctx):
                changes.append(change)
        except Exception:   # a model answer must never crash the request
            traceback.print_exc()
    changes = drop_double_logs(changes, ctx)
    dropped = len(items[:MAX_ITEMS]) - len(changes)
    source = "photo+text" if jpeg is not None and text else "photo" if jpeg is not None else "text"
    cost = f" ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
    log(f"{source} {r.model} {r.elapsed_ms} ms{cost}: {len(items)} items, {len(changes)} kept")
    return {"source": source, "message": _say(out.get("message"), 600, ctx), "changes": changes,
            "dropped": dropped, "elapsed_ms": r.elapsed_ms, "model": r.model}
