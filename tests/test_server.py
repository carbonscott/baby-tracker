"""Rows written by the previous version (old photo tag, no new keys) must survive the new
validation (per-kind key whitelist) and the page's edit / undo round trips unchanged.
Stdlib unittest, no Claude."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BABY_DATA_DIR", _tmp.name)   # before importing server: it reads this at import time

import server   # noqa: E402

# Made-up rows in the exact shapes the previous version stored (one of each, incl. its photo tag).
PROD_ROWS = [
    (1, "feed", "2026-10-05T09:10:00Z", "2026-10-05T09:24:30Z", '{"method": "breast", "side": "both"}'),
    (2, "sleep", "2026-10-05T10:00:00Z", "2026-10-05T11:40:15Z",
     '{"photo": {"confidence": 0.9, "suggested": "sleeping"}}'),
    (3, "activity", "2026-10-05T09:30:00Z", "2026-10-05T10:00:00Z", '{"name": "Walk"}'),
    (5, "feed", "2026-10-05T12:05:00Z", "2026-10-05T12:05:00Z",
     '{"amount_ml": 118.3, "method": "bottle", "milk": "formula", "photo": {"confidence": 0.9, "suggested": "feeding"}}'),
]
FIELDS = ("kind", "start_at", "end_at", "data", "note")


class ProductionRowsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, value in (("DATA_DIR", Path(d.name)), ("DB_PATH", Path(d.name) / "baby.db")):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server.init_db()
        with closing(server.connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO events (id, kind, start_at, end_at, data, note, tz, logged_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, '', 'America/Los_Angeles', 'parent@example.com', ?, ?)",
                [(*r, r[2], r[2]) for r in PROD_ROWS])
            self.before = {r["id"]: server.row_to_event(r) for r in conn.execute("SELECT * FROM events")}

    def get(self, eid):
        with closing(server.connect()) as conn:
            return server.get_event(conn, eid)

    def test_edit_sheet_save_without_changes_keeps_every_row(self):
        for eid, ev in self.before.items():
            # what openEdit sends: the data object it rebuilt (photo tag included), the note
            server.update_event(eid, {"data": json.loads(json.dumps(ev["data"])), "note": ""})
            # an older page that doesn't resend the photo tag
            server.update_event(eid, {"data": {k: v for k, v in ev["data"].items() if k != "photo"}})
            after = self.get(eid)
            self.assertEqual({k: after[k] for k in FIELDS}, {k: ev[k] for k in FIELDS}, eid)

    def test_undo_delete_recreates_the_same_entry(self):
        for eid, ev in self.before.items():
            server.delete_event(eid)
            body = {k: ev[k] for k in FIELDS}   # what the Undo button posts
            new = server.create_event({**body, "tz": "America/Los_Angeles"}, None)
            self.assertEqual({k: new[k] for k in FIELDS}, body, eid)

    def test_edit_undo_restores_the_row(self):
        ev = self.before[5]
        server.update_event(5, {"data": {"method": "breast", "side": "right"}})   # bottle -> breast
        self.assertEqual(self.get(5)["data"], {"method": "breast", "side": "right", "photo": ev["data"]["photo"]})
        server.update_event(5, {k: ev[k] for k in ("start_at", "end_at", "data", "note")})   # Undo
        self.assertEqual({k: self.get(5)[k] for k in FIELDS}, {k: ev[k] for k in FIELDS})

    def test_export_keeps_old_values(self):
        rows = {r["id"]: r for r in server.export_rows()}
        self.assertEqual((rows[5]["amount_ml"], rows[5]["milk"], rows[5]["photo_suggested"], rows[5]["ai_source"]),
                         (118.3, "formula", "feeding", None))
        self.assertEqual((rows[1]["side"], rows[1]["duration_min"], rows[1]["end_local"]),
                         ("both", 14.5, "2026-10-05T02:24:30-07:00"))
        self.assertEqual((rows[2]["photo_confidence"], rows[3]["name"]), (0.9, "Walk"))


class NoDurationTest(unittest.TestCase):
    """A sleep or breast feed saved with "Don't know" has end == start: no duration logged."""

    def test_editing_the_start_keeps_it_without_a_duration(self):
        start, earlier = "2026-11-02T20:00:00Z", "2026-11-02T19:30:00Z"
        for kind, data in (("sleep", {}), ("feed", {"method": "breast", "side": "left"}), ("activity", {"name": "Bath"})):
            ev = {"id": 1, "kind": kind, "start_at": start, "end_at": start, "data": data, "note": ""}
            after = server.apply_patch(ev, {"start_at": earlier})
            self.assertEqual((after["start_at"], after["end_at"]), (earlier, earlier), kind)
            self.assertEqual(server.apply_patch(ev, {"start_at": earlier, "end_at": "2026-11-02T20:10:00Z"})["end_at"],
                             "2026-11-02T20:10:00Z")   # given an end, it has a duration
            self.assertIsNone(server.apply_patch(ev, {"end_at": None})["end_at"])   # Still going
        timed = {"id": 1, "kind": "sleep", "start_at": start, "end_at": "2026-11-02T21:00:00Z", "data": {}, "note": ""}
        self.assertEqual(server.apply_patch(timed, {"start_at": earlier})["end_at"], "2026-11-02T21:00:00Z")

    def test_export_has_no_duration(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        with mock.patch.object(server, "DATA_DIR", Path(d.name)), \
                mock.patch.object(server, "DB_PATH", Path(d.name) / "baby.db"):
            server.init_db()
            for kind, data in (("sleep", {}), ("feed", {"method": "breast", "side": "both"})):
                server.create_event({"kind": kind, "start_at": "2026-11-02T20:00:00Z", "end_at": "2026-11-02T20:00:00Z",
                                     "data": data, "tz": "America/Los_Angeles"}, None)
            rows = server.export_rows()
        self.assertEqual([(r["kind"], r["duration_min"], r["end_local"]) for r in rows],
                         [("sleep", None, "2026-11-02T12:00:00-08:00"), ("feed", None, "2026-11-02T12:00:00-08:00")])


if __name__ == "__main__":
    unittest.main()
