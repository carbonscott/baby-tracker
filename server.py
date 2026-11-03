#!/usr/bin/env python3
"""Baby tracker: a tiny single-user web app (Python stdlib only).

Everything is stored in one SQLite file, data/baby.db. The schema and data
conventions are documented in README.md.

Run:  python3 server.py      (env: PORT=8420 HOST=0.0.0.0 BABY_DATA_DIR=./data)
"""

from __future__ import annotations

import base64
import binascii
import csv
import datetime as dt
import importlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


def _optional(module: str):
    """The Claude features need the Agent SDK + Pillow from .venv (see README); the tracker works without them."""
    try:
        return importlib.import_module(module), None
    except ImportError as e:
        return None, str(e)


claude_ai, _ = _optional("claude_ai")                    # shared Claude runner (AIError)
propose_ai, PROPOSE_SETUP_ERROR = _optional("propose_ai")   # photo/text -> proposed entries
ask_ai, ASK_SETUP_ERROR = _optional("ask_ai")               # questions about the log

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BABY_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "baby.db"
BACKUP_DIR = DATA_DIR / "backups"
STATIC_DIR = ROOT / "static"
# Loopback only: the exe.dev proxy reaches the app via 127.0.0.1, and nothing else
# (e.g. other Tailscale devices) can bypass its login.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8420"))
KEEP_BACKUPS = 90  # daily snapshots

KINDS = ("feed", "diaper", "sleep", "activity")
STOOL_COLORS = ("yellow", "orange", "green", "brown", "black", "red", "pale")
STOOL_TEXTURES = ("watery", "loose", "seedy", "soft", "pasty", "formed", "hard")
AI_SOURCES = ("photo", "text", "photo+text")
# data keys kept per kind (others are dropped, e.g. a stale `side` on a bottle row); plus COMMON_KEYS on all
DATA_KEYS = {
    "breast": ("method", "side"),
    "bottle": ("method", "milk", "amount_ml"),
    "diaper": ("pee", "poop", "color", "texture"),
    "sleep": (),
    "activity": ("name",),
}
COMMON_KEYS = ("place", "ai", "photo")
OPTIONAL_KEYS = ("place", "color", "texture", "ai", "photo")   # dropped when null or empty
JSON_LIMIT = 64_000          # request bodies
PROPOSE_LIMIT = 8_000_000    # POST /api/ai/propose carries a base64 photo
ASK_LIMIT = 256_000          # POST /api/ai/ask resends earlier answers (trimmed here to 6 x 4000 chars)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,              -- feed | diaper | sleep | activity
    start_at   TEXT NOT NULL,              -- UTC, 'YYYY-MM-DDTHH:MM:SSZ'
    end_at     TEXT,                       -- UTC; NULL = timer still running
    data       TEXT NOT NULL DEFAULT '{}', -- JSON details, see README
    note       TEXT NOT NULL DEFAULT '',
    tz         TEXT,                       -- IANA zone of the logging device
    logged_by  TEXT,                       -- exe.dev login email, if known
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_at);
"""

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}

CSV_COLUMNS = [
    "id", "kind", "start_at", "end_at", "start_local", "end_local", "duration_min",
    "name", "method", "side", "milk", "amount_ml", "pee", "poop", "color", "texture", "place",
    "photo_suggested", "photo_confidence", "ai_source", "ai_model", "ai_confidence", "ai_edited",
    "note", "tz", "logged_by", "created_at", "updated_at",
]


def _reject_constant(name: str):
    raise ValueError(f"{name} is not allowed")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def bad(msg: str) -> HttpError:
    return HttpError(400, msg)


# ---------- time helpers ----------

def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field: str) -> str | None:
    """Accept any ISO-8601 timestamp with an offset; return normalized UTC."""
    if value in (None, ""):
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise bad(f"{field}: expected an ISO-8601 timestamp, got {value!r}")
    if t.tzinfo is None:
        raise bad(f"{field}: timestamp needs a timezone (e.g. trailing Z)")
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_dt(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def local_iso(ts: str, tz: str | None) -> str:
    if not tz:
        return ""
    try:
        return to_dt(ts).astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")
    except Exception:
        return ""


# ---------- validation ----------

def _confidence(value) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1
    return value if ok else None


def clean_data(kind: str, data) -> dict:
    """Validate an event's data; returns a new dict with only the keys that kind uses."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise bad("data must be a JSON object")
    data = dict(data)
    photo = data.get("photo")   # old photo classifier: what Claude suggested (kept on old rows)
    if photo is not None:
        if not isinstance(photo, dict):
            raise bad("photo must be an object")
        data["photo"] = {"suggested": str(photo.get("suggested") or "")[:30],
                         "confidence": _confidence(photo.get("confidence"))}
    ai = data.get("ai")   # the parent saved an entry Claude proposed
    if ai is not None:
        if not isinstance(ai, dict):
            raise bad("ai must be an object")
        if ai.get("source") not in AI_SOURCES:
            raise bad("ai.source must be photo | text | photo+text")
        data["ai"] = {"source": ai["source"], "model": str(ai.get("model") or "")[:30],
                      "confidence": _confidence(ai.get("confidence")), "edited": ai.get("edited") is True}
    place = data.get("place")   # where it happened: "crib", "car seat", ...
    if place is not None:
        if not isinstance(place, str):
            raise bad("place must be text")
        data["place"] = " ".join(place.split())[:40].strip()
    keys = DATA_KEYS.get(kind, ())
    if kind == "feed":
        method = data.get("method")
        keys = DATA_KEYS.get(method, ())
        if method == "breast":
            if data.get("side") not in ("left", "right", "both"):
                raise bad("breast feed needs side: left | right | both")
        elif method == "bottle":
            if data.get("milk") not in (None, "breastmilk", "formula"):
                raise bad("bottle milk must be breastmilk | formula")
            amt = data.get("amount_ml")
            if amt is not None and (
                isinstance(amt, bool) or not isinstance(amt, (int, float)) or not 0 <= amt <= 1000
            ):
                raise bad("amount_ml must be a number between 0 and 1000")
        else:
            raise bad("feed method must be breast | bottle")
    elif kind == "diaper":
        for k in ("pee", "poop"):
            if not isinstance(data.get(k, False), bool):
                raise bad(f"diaper {k} must be true or false")
        if data.get("poop"):
            if data.get("color") not in (None, *STOOL_COLORS):
                raise bad(f"stool color must be one of: {', '.join(STOOL_COLORS)}")
            if data.get("texture") not in (None, *STOOL_TEXTURES):
                raise bad(f"stool texture must be one of: {', '.join(STOOL_TEXTURES)}")
        else:   # color/texture only describe poop
            keys = ("pee", "poop")
    elif kind == "activity":
        name = " ".join(str(data.get("name") or "").split())[:60]
        if not name:
            raise bad("activity needs a name")
        data["name"] = name
    # Unknown keys are dropped silently; so are empty optional ones.
    return {k: v for k, v in data.items()
            if (k in keys or k in COMMON_KEYS) and not (k in OPTIONAL_KEYS and v in (None, ""))}


def is_point(kind: str, data: dict) -> bool:
    """Point events have no duration: diapers and bottle feeds."""
    return kind == "diaper" or (kind == "feed" and data.get("method") == "bottle")


def normalize(ev: dict) -> dict:
    ev["data"] = clean_data(ev["kind"], ev["data"])
    if is_point(ev["kind"], ev["data"]):
        ev["end_at"] = ev["start_at"]
    if ev["end_at"] is not None and ev["end_at"] < ev["start_at"]:
        raise bad("end time is before start time")
    ev["note"] = str(ev.get("note") or "").strip()[:2000]
    return ev


# ---------- storage ----------

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA)


def row_to_event(row: sqlite3.Row) -> dict:
    ev = dict(row)
    ev["data"] = json.loads(ev["data"] or "{}")
    return ev


def get_event(conn: sqlite3.Connection, eid: int) -> dict:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    if row is None:
        raise HttpError(404, f"event {eid} not found")
    return row_to_event(row)


def list_events(query: dict) -> list[dict]:
    since = parse_ts(query.get("since", [None])[0], "since")
    until = parse_ts(query.get("until", [None])[0], "until")
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if since:
        # anything that started or ended inside the window, or is still running
        sql += " AND (start_at >= ? OR end_at >= ? OR end_at IS NULL)"
        args += [since, since]
    if until:
        sql += " AND start_at < ?"
        args.append(until)
    sql += " ORDER BY start_at DESC, id DESC"
    with closing(connect()) as conn:
        return [row_to_event(r) for r in conn.execute(sql, args)]


def build_event(body: dict) -> dict:
    """Validate a create request into {kind, start_at, end_at, data, note}. Writes nothing."""
    kind = body.get("kind")
    if kind not in KINDS:
        raise bad(f"kind must be one of: {', '.join(KINDS)}")
    return normalize({
        "kind": kind,
        "start_at": parse_ts(body.get("start_at"), "start_at") or utc_now(),
        "end_at": parse_ts(body.get("end_at"), "end_at"),
        "data": body.get("data"),
        "note": body.get("note"),
    })


def create_event(body: dict, user: str | None) -> dict:
    ev = build_event(body)
    tz = str(body.get("tz") or "")[:64] or None
    now = utc_now()
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO events (kind, start_at, end_at, data, note, tz, logged_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ev["kind"], ev["start_at"], ev["end_at"], json.dumps(ev["data"], sort_keys=True),
             ev["note"], tz, user, now, now),
        )
        return get_event(conn, cur.lastrowid)


def apply_patch(ev: dict, body: dict) -> dict:
    """A copy of row `ev` with a PATCH body applied and validated. Writes nothing."""
    ev = {**ev, "data": dict(ev["data"])}
    # An entry without a duration (end == start: an activity logged as a moment, a sleep or breast
    # feed logged with no duration) stays that way when only its start is edited.
    no_duration = ev["end_at"] == ev["start_at"]
    if "kind" in body:
        if body["kind"] not in KINDS:
            raise bad(f"kind must be one of: {', '.join(KINDS)}")
        ev["kind"] = body["kind"]
    if "start_at" in body:
        ev["start_at"] = parse_ts(body["start_at"], "start_at") or ev["start_at"]
    if "end_at" in body:
        ev["end_at"] = parse_ts(body["end_at"], "end_at")
    elif no_duration:
        ev["end_at"] = ev["start_at"]
    if "data" in body:
        new = body["data"]
        # Edits from the UI don't resend the photo/ai tags; keep them unless the request sets them.
        if isinstance(new, dict):
            new = {**{k: ev["data"][k] for k in ("photo", "ai") if k in ev["data"]}, **new}
        ev["data"] = new
    if "note" in body:
        ev["note"] = body["note"]
    return normalize(ev)


def update_event(eid: int, body: dict) -> dict:
    with closing(connect()) as conn, conn:
        ev = apply_patch(get_event(conn, eid), body)
        conn.execute(
            "UPDATE events SET kind = ?, start_at = ?, end_at = ?, data = ?, note = ?, updated_at = ?"
            " WHERE id = ?",
            (ev["kind"], ev["start_at"], ev["end_at"], json.dumps(ev["data"], sort_keys=True),
             ev["note"], utc_now(), eid),
        )
        return get_event(conn, eid)


def delete_event(eid: int) -> None:
    with closing(connect()) as conn, conn:
        if conn.execute("DELETE FROM events WHERE id = ?", (eid,)).rowcount == 0:
            raise HttpError(404, f"event {eid} not found")


def activity_names() -> list[str]:
    """Distinct activity names, most recently used first (keeps the latest spelling)."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT json_extract(data, '$.name') AS name, max(start_at) AS last FROM events"
            " WHERE kind = 'activity' GROUP BY lower(json_extract(data, '$.name'))"
            " ORDER BY last DESC LIMIT 20"
        ).fetchall()
    return [r["name"] for r in rows]


def export_rows() -> list[dict]:
    """Flat rows (one per event) for CSV/JSON export."""
    with closing(connect()) as conn:
        events = [row_to_event(r) for r in conn.execute("SELECT * FROM events ORDER BY start_at, id")]
    rows = []
    for ev in events:
        d = ev["data"]
        duration = None
        if ev["end_at"] and ev["end_at"] != ev["start_at"] and not is_point(ev["kind"], d):
            duration = round((to_dt(ev["end_at"]) - to_dt(ev["start_at"])).total_seconds() / 60, 1)
        diaper = ev["kind"] == "diaper"
        ai = d.get("ai") or {}
        rows.append({
            "id": ev["id"],
            "kind": ev["kind"],
            "start_at": ev["start_at"],
            "end_at": ev["end_at"],
            "start_local": local_iso(ev["start_at"], ev["tz"]),
            "end_local": local_iso(ev["end_at"], ev["tz"]) if ev["end_at"] else None,
            "duration_min": duration,
            "name": d.get("name"),
            "method": d.get("method"),
            "side": d.get("side"),
            "milk": d.get("milk"),
            "amount_ml": d.get("amount_ml"),
            "pee": int(bool(d.get("pee"))) if diaper else None,
            "poop": int(bool(d.get("poop"))) if diaper else None,
            "color": d.get("color"),
            "texture": d.get("texture"),
            "place": d.get("place"),
            "photo_suggested": (d.get("photo") or {}).get("suggested"),
            "photo_confidence": (d.get("photo") or {}).get("confidence"),
            "ai_source": ai.get("source"),
            "ai_model": ai.get("model"),
            "ai_confidence": ai.get("confidence"),
            "ai_edited": int(bool(ai.get("edited"))) if ai else None,
            "note": ev["note"],
            "tz": ev["tz"],
            "logged_by": ev["logged_by"],
            "created_at": ev["created_at"],
            "updated_at": ev["updated_at"],
        })
    return rows


def export_csv() -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    w.writerows(export_rows())
    return buf.getvalue().encode()


# ---------- backups ----------

def backup_once() -> Path | None:
    """Write one consistent snapshot per calendar day; keep the newest KEEP_BACKUPS."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"baby-{dt.date.today().isoformat()}.db"
    if target.exists():
        return None
    tmp = target.with_suffix(".db.tmp")
    with closing(connect()) as src, closing(sqlite3.connect(tmp)) as dst:
        src.backup(dst)
    os.replace(tmp, target)
    for old in sorted(BACKUP_DIR.glob("baby-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    return target


def backup_loop() -> None:
    while True:
        try:
            if path := backup_once():
                print(f"backup written: {path}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(3600)


# ---------- Claude: proposed entries and questions (README: Claude features) ----------

def setup_error(detail: str | None) -> HttpError:
    return HttpError(503, "Claude features aren't set up on the server (see README: Claude features > Setup)."
                          f" Detail: {detail}")


def text_field(body: dict, key: str, limit: int) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise bad(f"{key} must be text")
    value = value.strip()
    if len(value) > limit:
        raise bad(f"{key} is too long (max {limit} characters)")
    return value


def parent_prefs(body: dict) -> tuple[str, str, str]:
    """(tz, baby_name, unit) from a Claude request. No tz: the zone of the latest entry, else UTC."""
    tz = body.get("tz")
    if tz in (None, ""):
        with closing(connect()) as conn:
            row = conn.execute("SELECT tz FROM events WHERE tz IS NOT NULL ORDER BY start_at DESC LIMIT 1").fetchone()
        tz = row["tz"] if row else "UTC"
    try:
        ZoneInfo(tz)
    except Exception:
        raise bad(f"tz must be an IANA time zone like America/Los_Angeles, got {tz!r}"[:150])
    name = body.get("baby_name")
    name = " ".join(name.split())[:40] if isinstance(name, str) else ""
    unit = body.get("unit") or "ml"
    if unit not in ("ml", "oz"):
        raise bad("unit must be ml or oz")
    return tz, name, unit


def decode_image(value) -> bytes:
    if not isinstance(value, str):
        raise bad("image must be a base64 string")
    if value.startswith("data:"):   # tolerate a data: URL
        value = value.partition(",")[2]
    try:
        return base64.b64decode("".join(value.split()), validate=True)
    except (binascii.Error, ValueError):
        raise bad("image is not valid base64")


def check_change(change: dict) -> dict:
    """Dry-run one proposed change through the normal validation; return it in the API shape."""
    if change["op"] == "create":
        ev = build_event(change["event"])
        return {**change, "event": {k: ev[k] for k in ("kind", "start_at", "end_at", "data", "note")}}
    with closing(connect()) as conn:
        before = get_event(conn, change["id"])
    after = apply_patch(before, change["patch"])
    patch = {k: after[k] for k in change["patch"] if after[k] != before[k]}   # only what really changes
    if not patch:
        raise bad("changes nothing")
    return {"op": "update", "id": change["id"], "patch": patch, "before": before, "after": after,
            "needs": [], "confidence": change.get("confidence"), "why": change.get("why", "")}


def ai_propose(body: dict) -> dict:
    """POST /api/ai/propose: Claude proposes finished entries from a photo and/or text. Saves nothing."""
    if propose_ai is None:
        raise setup_error(PROPOSE_SETUP_ERROR)
    text = text_field(body, "text", 2000)
    image = body.get("image")
    if not text and image in (None, ""):
        raise bad("send a photo or some text")
    tz, baby_name, unit = parent_prefs(body)
    taken = parse_ts(body.get("photo_taken_at"), "photo_taken_at")
    jpeg = None
    try:
        if image not in (None, ""):
            jpeg = claude_ai.prepare_image(decode_image(image))   # shrinks, re-encodes, drops EXIF
        now = dt.datetime.now(dt.timezone.utc)
        since = (now - dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = propose_ai.propose(
            text=text or None, jpeg=jpeg, photo_taken_at=to_dt(taken) if taken else None, tz=tz,
            baby_name=baby_name, unit=unit, recent_events=list_events({"since": [since]}),
            activity_names=activity_names(), now=now)
    except claude_ai.AIError as e:
        raise HttpError(e.status, str(e))
    proposals, dropped = [], result.pop("dropped", 0)
    for change in result.pop("changes"):
        try:
            proposals.append(check_change(change))
        except HttpError as e:
            dropped += 1
            print(f"propose: dropped {json.dumps(change)[:400]}: {e}", file=sys.stderr, flush=True)
    if dropped:   # Claude's message may mention them; say why they aren't shown
        result["message"] = (result["message"] + f" (Left out {dropped} suggestion{'s' if dropped > 1 else ''}"
                             " that didn't fit the log.)").strip()
    return {**result, "proposals": proposals}


def ai_ask(body: dict) -> dict:
    """POST /api/ai/ask: Claude answers a question about the log (read-only, in-memory copy)."""
    if ask_ai is None:
        raise setup_error(ASK_SETUP_ERROR)
    question = text_field(body, "question", 1000)
    if not question:
        raise bad("ask a question")
    history = body.get("history") or []
    if not isinstance(history, list):
        raise bad("history must be a list of {q, a}")
    turns = []
    for turn in history[-6:]:
        if not (isinstance(turn, dict) and isinstance(turn.get("q"), str) and isinstance(turn.get("a"), str)):
            raise bad("history must be a list of {q, a}")
        turns.append({"q": turn["q"][:1000], "a": turn["a"][:4000]})
    tz, baby_name, unit = parent_prefs(body)
    try:
        return ask_ai.ask(question=question, history=turns, tz=tz, baby_name=baby_name, unit=unit,
                          rows=export_rows())
    except claude_ai.AIError as e:
        raise HttpError(e.status, str(e))


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "BabyTracker/1.0"

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        try:
            if method == "GET" and path in STATIC:
                name, ctype = STATIC[path]
                return self.send_bytes(200, (STATIC_DIR / name).read_bytes(), ctype, "no-cache")
            if path == "/api/events":
                if method == "GET":
                    return self.send_json(list_events(parse_qs(url.query)))
                if method == "POST":
                    return self.send_json(create_event(self.read_json(), self.user()), 201)
            elif path.startswith("/api/events/"):
                try:
                    eid = int(path.removeprefix("/api/events/"))
                except ValueError:
                    raise HttpError(404, "not found")
                if method == "GET":
                    with closing(connect()) as conn:
                        return self.send_json(get_event(conn, eid))
                if method == "PATCH":
                    return self.send_json(update_event(eid, self.read_json()))
                if method == "DELETE":
                    delete_event(eid)
                    return self.send_json({"deleted": eid})
            elif method == "POST" and path == "/api/ai/propose":
                return self.send_json(ai_propose(self.read_json(PROPOSE_LIMIT)))
            elif method == "POST" and path == "/api/ai/ask":
                return self.send_json(ai_ask(self.read_json(ASK_LIMIT)))
            elif method == "GET" and path == "/api/activity-names":
                return self.send_json(activity_names())
            elif method == "GET" and path == "/api/export.csv":
                name = f"baby-events-{dt.date.today().isoformat()}.csv"
                return self.send_bytes(200, export_csv(), "text/csv; charset=utf-8", "no-store",
                                       {"Content-Disposition": f'attachment; filename="{name}"'})
            elif method == "GET" and path == "/api/export.json":
                name = f"baby-events-{dt.date.today().isoformat()}.json"
                body = json.dumps(export_rows(), indent=1).encode()
                return self.send_bytes(200, body, "application/json", "no-store",
                                       {"Content-Disposition": f'attachment; filename="{name}"'})
            elif method == "GET" and path == "/healthz":
                return self.send_json({"ok": True})
            raise HttpError(404, "not found")
        except HttpError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "internal server error"}, 500)

    def user(self) -> str | None:
        return self.headers.get("X-ExeDev-Email") or None

    def read_json(self, limit: int = JSON_LIMIT) -> dict:
        # Requiring a JSON content type also blocks simple cross-site form posts.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise HttpError(415, "send JSON with Content-Type: application/json")
        n = self.content_length()
        if n > limit:
            raise HttpError(413, f"request body too large (max {limit // 1000} KB)")
        try:   # NaN/Infinity would make the stored JSON unreadable by browsers
            body = json.loads(self.rfile.read(n) or b"{}", parse_constant=_reject_constant)
        except ValueError:
            raise bad("invalid JSON")
        if not isinstance(body, dict):
            raise bad("JSON body must be an object")
        return body

    def content_length(self) -> int:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise bad("bad Content-Length")
        if n < 0:
            raise bad("bad Content-Length")
        return n

    def send_json(self, obj, status: int = 200) -> None:
        self.send_bytes(status, json.dumps(obj).encode(), "application/json", "no-store")

    def send_bytes(self, status: int, body: bytes, ctype: str, cache: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    init_db()
    threading.Thread(target=backup_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"baby tracker listening on http://{HOST}:{PORT}  (db: {DB_PATH})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
