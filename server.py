#!/usr/bin/env python3
"""Baby tracker: a tiny single-user web app (Python stdlib only).

Everything is stored in one SQLite file, data/baby.db. The schema and data
conventions are documented in README.md.

Run:  python3 server.py      (env: PORT=8420 HOST=0.0.0.0 BABY_DATA_DIR=./data)
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sqlite3
import threading
import time
import traceback
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:  # photo classification needs the Agent SDK + Pillow from .venv (see README)
    import photo_ai
    PHOTO_SETUP_ERROR = None
except ImportError as e:
    photo_ai = None
    PHOTO_SETUP_ERROR = str(e)

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
    "id", "kind", "start_at", "end_at", "start_local", "duration_min",
    "name", "method", "side", "milk", "amount_ml", "pee", "poop", "photo_suggested", "photo_confidence",
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

def clean_data(kind: str, data) -> dict:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise bad("data must be a JSON object")
    photo = data.get("photo")   # set when an entry was logged from a photo: what Claude suggested
    if photo is not None:
        if not isinstance(photo, dict):
            raise bad("photo must be an object")
        conf = photo.get("confidence")
        ok = isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1
        data["photo"] = {"suggested": str(photo.get("suggested") or "")[:30], "confidence": conf if ok else None}
    if kind == "feed":
        method = data.get("method")
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
    elif kind == "activity":
        name = " ".join(str(data.get("name") or "").split())[:60]
        if not name:
            raise bad("activity needs a name")
        data["name"] = name
    return data


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


def create_event(body: dict, user: str | None) -> dict:
    kind = body.get("kind")
    if kind not in KINDS:
        raise bad(f"kind must be one of: {', '.join(KINDS)}")
    ev = normalize({
        "kind": kind,
        "start_at": parse_ts(body.get("start_at"), "start_at") or utc_now(),
        "end_at": parse_ts(body.get("end_at"), "end_at"),
        "data": body.get("data"),
        "note": body.get("note"),
    })
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


def update_event(eid: int, body: dict) -> dict:
    with closing(connect()) as conn, conn:
        ev = get_event(conn, eid)
        # An activity logged as a single moment (end == start) stays a moment when its time is edited.
        was_moment = ev["kind"] == "activity" and ev["end_at"] == ev["start_at"]
        if "kind" in body:
            if body["kind"] not in KINDS:
                raise bad(f"kind must be one of: {', '.join(KINDS)}")
            ev["kind"] = body["kind"]
        if "start_at" in body:
            ev["start_at"] = parse_ts(body["start_at"], "start_at") or ev["start_at"]
        if "end_at" in body:
            ev["end_at"] = parse_ts(body["end_at"], "end_at")
        elif was_moment:
            ev["end_at"] = ev["start_at"]
        if "data" in body:
            old_photo = ev["data"].get("photo")
            ev["data"] = body["data"]
            # Edits from the UI don't resend the photo tag; keep it unless the request sets one.
            if old_photo is not None and isinstance(ev["data"], dict) and "photo" not in ev["data"]:
                ev["data"] = {**ev["data"], "photo": old_photo}
        if "note" in body:
            ev["note"] = body["note"]
        ev = normalize(ev)
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
        rows.append({
            "id": ev["id"],
            "kind": ev["kind"],
            "start_at": ev["start_at"],
            "end_at": ev["end_at"],
            "start_local": local_iso(ev["start_at"], ev["tz"]),
            "duration_min": duration,
            "name": d.get("name"),
            "method": d.get("method"),
            "side": d.get("side"),
            "milk": d.get("milk"),
            "amount_ml": d.get("amount_ml"),
            "pee": int(bool(d.get("pee"))) if diaper else None,
            "poop": int(bool(d.get("poop"))) if diaper else None,
            "photo_suggested": (d.get("photo") or {}).get("suggested"),
            "photo_confidence": (d.get("photo") or {}).get("confidence"),
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
            elif method == "POST" and path == "/api/photo/classify":
                if photo_ai is None:
                    raise HttpError(503, "Photo AI isn't set up on the server (see README: Log from a photo"
                                         f" > Setup). Detail: {PHOTO_SETUP_ERROR}")
                try:
                    return self.send_json(photo_ai.classify(self.read_image()))
                except photo_ai.PhotoError as e:
                    raise HttpError(e.status, str(e))
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

    def read_json(self) -> dict:
        # Requiring a JSON content type also blocks simple cross-site form posts.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise HttpError(415, "send JSON with Content-Type: application/json")
        n = self.content_length()
        if n > 64_000:
            raise HttpError(413, "request body too large")
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

    def read_image(self) -> bytes:
        """Raw image body. An image/* content type also blocks simple cross-site posts."""
        if not (self.headers.get("Content-Type") or "").startswith("image/"):
            raise HttpError(415, "send the photo with an image/* Content-Type")
        n = self.content_length()
        if n > photo_ai.MAX_UPLOAD:
            raise HttpError(413, "photo too large (max 15 MB)")
        return self.rfile.read(n)

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
