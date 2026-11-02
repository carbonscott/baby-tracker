"""Tests for proposals (propose_ai.py) and the server parts around them. No live Claude.

Run:  .venv/bin/python -m unittest tests.test_propose -v     (from the project folder)
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing, redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.TemporaryDirectory()
os.environ["BABY_DATA_DIR"] = _tmp.name   # before importing server: it reads this at import time

import claude_ai    # noqa: E402
import propose_ai   # noqa: E402
import server       # noqa: E402

UTC = dt.timezone.utc
LA = ZoneInfo("America/Los_Angeles")
# Monday 2026-11-02, 14:00 in Los Angeles (PST, UTC-8; DST ended the day before)
NOW = dt.datetime(2026, 11, 2, 22, 0, tzinfo=UTC)


def utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def row(eid, kind, start, end, data=None, note=""):
    return {"id": eid, "kind": kind, "start_at": start, "end_at": end, "data": data or {}, "note": note,
            "tz": "America/Los_Angeles", "logged_by": None, "created_at": start, "updated_at": start}


EVENTS = [   # newest first, like GET /api/events
    row(9, "sleep", "2026-11-02T21:05:00Z", None, {"place": "crib"}),                        # 13:05, running
    row(8, "feed", "2026-11-02T20:40:00Z", "2026-11-02T20:55:00Z", {"method": "breast", "side": "left"}),
    row(7, "diaper", "2026-11-02T20:30:00Z", "2026-11-02T20:30:00Z", {"pee": True, "poop": False}),
    row(6, "activity", "2026-11-02T19:00:00Z", "2026-11-02T19:00:00Z", {"name": "Tummy time"}),
    row(5, "feed", "2026-11-02T11:00:00Z", "2026-11-02T11:00:00Z",
        {"method": "bottle", "milk": "formula", "amount_ml": 88.7}),                             # 03:00
    row(1, "sleep", "2026-10-29T05:00:00Z", "2026-10-29T08:00:00Z"),                           # > 48 h ago
]


def ctx(**kw) -> propose_ai.Ctx:
    base = dict(now=NOW, zone=LA, events=EVENTS, activity_names=["Tummy time", "Walk"], unit="ml")
    return propose_ai.Ctx(**{**base, **kw})


def item(**kw) -> dict:
    return {"op": "create", "id": None, "kind": "feed", "start": None, "timing": None, "note": "",
            "confidence": 0.9, "why": "test", **kw}


def change(it, c=None):
    with redirect_stderr(io.StringIO()):   # dropped items are logged; keep test output clean
        return propose_ai.to_change(it, c or ctx())


class LocalTimeTest(unittest.TestCase):
    def conv(self, s, now=NOW, zone=LA):
        t = propose_ai.local_to_utc(s, zone, now)
        return propose_ai.iso(t) if t else None

    def test_plain(self):
        self.assertEqual(self.conv("2026-11-02T13:05"), "2026-11-02T21:05:00Z")
        self.assertEqual(self.conv("2026-11-02 13:05:30"), "2026-11-02T21:05:00Z")

    def test_future_moves_back_one_day(self):
        # "11 pm" said at 2 pm means last night
        self.assertEqual(self.conv("2026-11-02T23:00"), "2026-11-02T07:00:00Z")
        # within a minute of now is not the future
        self.assertEqual(self.conv("2026-11-02T14:00"), "2026-11-02T22:00:00Z")

    def test_future_across_dst_end_keeps_wall_clock(self):
        # Sunday 2026-11-01 12:00 PST; "11:30 pm" is the night before, still in PDT (UTC-7)
        now = dt.datetime(2026, 11, 1, 20, 0, tzinfo=UTC)
        self.assertEqual(self.conv("2026-11-01T23:30", now), "2026-11-01T06:30:00Z")   # not 07:30 (24 h back)

    def test_dst_gap_and_repeat(self):
        now = dt.datetime(2027, 3, 15, 0, 0, tzinfo=UTC)
        self.assertEqual(self.conv("2027-03-14T02:30", now), "2027-03-14T10:30:00Z")   # skipped hour: 3:30 PDT
        self.assertEqual(self.conv("2026-11-01T01:30"), "2026-11-01T08:30:00Z")        # repeated hour: first (PDT)

    def test_other_zones(self):
        self.assertEqual(self.conv("2026-11-02T20:00", zone=ZoneInfo("Asia/Kolkata")), "2026-11-02T14:30:00Z")
        self.assertEqual(self.conv("2026-11-02T22:00", zone=ZoneInfo("Europe/Berlin")), "2026-11-02T21:00:00Z")

    def test_refuses_utc_and_garbage(self):
        for bad in ("2026-11-02T13:05Z", "2026-11-02T13:05:00-08:00", "13:05", "yesterday", "", None, 5,
                    "2026-02-30T10:00", "2026-11-02T25:00"):
            self.assertIsNone(self.conv(bad), bad)


class CreateTest(unittest.TestCase):
    def test_bottle_oz_to_ml_and_point(self):
        c = change(item(start="2026-11-02T13:30", timing="running", method="bottle", milk="formula",
                        amount=4, amount_unit="oz"))
        ev = c["event"]
        self.assertEqual(ev["data"], {"method": "bottle", "milk": "formula", "amount_ml": 118.3})
        self.assertEqual(ev["start_at"], "2026-11-02T21:30:00Z")
        self.assertEqual(ev["end_at"], ev["start_at"])   # bottles are point events, whatever the timing says
        self.assertEqual(c["needs"], [])

    def test_amount_unit_defaults_to_parent_unit_and_bad_amounts_are_dropped(self):
        it = item(start="2026-11-02T13:30", method="bottle", amount=3)
        self.assertEqual(change(it, ctx(unit="oz"))["event"]["data"]["amount_ml"], 88.7)
        self.assertEqual(change(it)["event"]["data"]["amount_ml"], 3.0)
        for amount in (0, -5, 2000, "lots", True):
            self.assertIsNone(change({**it, "amount": amount})["event"]["data"]["amount_ml"])

    def test_breast_running_and_side_fallback(self):
        c = change(item(start="2026-11-02T13:50", timing="running", method="breast", side=None))
        self.assertIsNone(c["event"]["end_at"])
        self.assertEqual(c["needs"], [])
        self.assertEqual(c["event"]["data"], {"method": "breast", "side": "right"})   # last session began left

    def test_method_inferred(self):
        self.assertEqual(change(item(timing="running", side="left"))["event"]["data"]["method"], "breast")
        self.assertEqual(change(item(amount=90, amount_unit="ml"))["event"]["data"]["method"], "bottle")
        self.assertIsNone(change(item(timing="running")))

    def test_timed_moment_needs_duration(self):
        c = change(item(kind="sleep", start="2026-11-02T10:00", timing="moment"))
        self.assertEqual((c["event"]["end_at"], c["needs"]), (None, ["duration"]))
        c = change(item(start="2026-11-02T10:00", timing="needs_duration", method="breast", side="both"))
        self.assertEqual((c["event"]["end_at"], c["needs"]), (None, ["duration"]))

    def test_range_end_and_duration(self):
        c = change(item(kind="sleep", start="2026-11-02T10:00", end="2026-11-02T11:15", timing="range"))
        self.assertEqual((c["event"]["end_at"], c["needs"]), ("2026-11-02T19:15:00Z", []))
        c = change(item(kind="sleep", start="2026-11-02T10:00", duration_min=45, timing="range"))
        self.assertEqual(c["event"]["end_at"], "2026-11-02T18:45:00Z")

    def test_range_across_midnight_with_wrong_end_date(self):
        # 11 pm to 2 am, end written with the start's date
        c = change(item(kind="sleep", start="2026-11-01T23:00", end="2026-11-01T02:00", timing="range"))
        self.assertEqual((c["event"]["start_at"], c["event"]["end_at"]), ("2026-11-02T07:00:00Z", "2026-11-02T10:00:00Z"))

    def test_range_with_unusable_end_asks_duration(self):
        for end in ("2026-11-02T16:00", "2026-11-02T09:00", "garbage"):   # future / before start / unreadable
            c = change(item(kind="sleep", start="2026-11-02T10:00", end=end, timing="range"))
            self.assertEqual((c["event"]["end_at"], c["needs"]), (None, ["duration"]), end)
        c = change(item(kind="sleep", start="2026-11-02T13:00", duration_min=300, timing="range"))   # ends in future
        self.assertEqual(c["needs"], ["duration"])

    def test_start_defaults_to_photo_time(self):
        photo = dt.datetime(2026, 11, 2, 21, 40, 12, tzinfo=UTC)
        c = change(item(kind="diaper", pee=True, poop=False), ctx(photo_at=photo))
        self.assertEqual(c["event"]["start_at"], "2026-11-02T21:40:12Z")
        c = change(item(kind="diaper", pee=True, poop=False))
        self.assertEqual(c["event"]["start_at"], propose_ai.iso(NOW))

    def test_bad_start_drops(self):
        self.assertIsNone(change(item(kind="sleep", start="2026-11-02T21:05Z", timing="running")))
        self.assertIsNone(change(item(kind="sleep", start="2026-11-04T10:00", timing="running")))   # future

    def test_activity(self):
        c = change(item(kind="activity", activity_name="  tummy   TIME ", timing="moment", start="2026-11-02T12:00"))
        self.assertEqual(c["event"]["data"], {"name": "Tummy time"})   # known spelling
        self.assertEqual(c["event"]["end_at"], c["event"]["start_at"])
        c = change(item(kind="activity", activity_name="bath", start="2026-11-02T12:00"))   # no timing: a moment
        self.assertEqual((c["event"]["data"]["name"], c["event"]["end_at"]), ("Bath", c["event"]["start_at"]))
        c = change(item(kind="activity", activity_name="Outdoors", timing="running", place=" the  park "))
        self.assertEqual((c["event"]["end_at"], c["event"]["data"]["place"]), (None, "the park"))
        self.assertIsNone(change(item(kind="activity", activity_name="  ")))

    def test_diaper_details(self):
        c = change(item(kind="diaper", pee=True, poop=True, color="Mustard", texture="seedy"))
        self.assertEqual(c["event"]["data"], {"pee": True, "poop": True, "color": "yellow", "texture": "seedy"})
        c = change(item(kind="diaper", pee=True, poop=None, color="green"))
        self.assertEqual(c["event"]["data"], {"pee": True, "poop": False})
        c = change(item(kind="diaper", pee=False, poop=True, color="purple", texture=7))
        self.assertEqual(c["event"]["data"], {"pee": False, "poop": True})

    def test_garbage_items(self):
        for it in (None, "x", [], {"op": "delete", "id": 7}, item(kind="bath"), item(op=None)):
            self.assertIsNone(change(it), it)

    def test_row_ids_never_reach_the_parent(self):
        ids = [9, 8, 7]
        for said, shown in [
            ("Already in the log (#7), so nothing new.", "Already in the log, so nothing new."),
            ("That nap (#9, from 1:05 pm) is still running.", "That nap (from 1:05 pm) is still running."),
            ("Sleep timer #9 was still running; ending it.", "Sleep timer was still running; ending it."),
            ("Matches diaper #7.", "Matches diaper."),
            ("bottle #2 of the day; #90 too", "bottle #2 of the day; #90 too"),   # not ids Claude was shown
        ]:
            self.assertEqual(propose_ai.scrub_ids(said, ids), shown)
        c = change(item(op="update", id=9, kind="sleep", end="2026-11-02T13:50", why="woke up; #9 was running",
                        note="Woke (#9) happy"))
        self.assertEqual((c["why"], c["patch"]["note"]), ("woke up; was running", "Woke happy"))

    def test_confidence_and_why(self):
        self.assertEqual(change(item(kind="sleep", timing="running", confidence=85))["confidence"], 0.85)
        self.assertIsNone(change(item(kind="sleep", timing="running", confidence="high"))["confidence"])
        self.assertEqual(change(item(kind="sleep", timing="running", confidence=1.4))["confidence"], 1.0)
        self.assertEqual(change(item(kind="sleep", timing="running", why=" a\n b "))["why"], "a b")


class UpdateTest(unittest.TestCase):
    def test_end_running_sleep(self):
        c = change(item(op="update", id=9, kind="sleep", end="2026-11-02T13:50"))
        self.assertEqual(c, {"op": "update", "id": 9, "patch": {"end_at": "2026-11-02T21:50:00Z"},
                             "confidence": 0.9, "why": "test"})

    def test_side_change_sends_full_data(self):
        c = change(item(op="update", id="#8", kind="feed", side="right"))
        self.assertEqual(c["patch"], {"data": {"method": "breast", "side": "right"}})

    def test_amount_change_in_oz(self):
        c = change(item(op="update", id=5, kind="feed", amount=5, amount_unit="oz"))
        self.assertEqual(c["patch"], {"data": {"method": "bottle", "milk": "formula", "amount_ml": 147.9}})

    def test_note_replaces_and_unchanged_fields_are_left_out(self):
        c = change(item(op="update", id=8, kind="feed", start="2026-11-02T12:40", side="left", note="spit up a bit"))
        self.assertEqual(c["patch"], {"note": "spit up a bit"})

    def test_bad_targets_are_dropped(self):
        self.assertIsNone(change(item(op="update", id=1, kind="sleep", end="2026-10-29T02:00")))   # not shown
        self.assertIsNone(change(item(op="update", id=42, kind="sleep", end="2026-11-02T13:50")))  # unknown
        self.assertIsNone(change(item(op="update", id=None, kind="sleep", end="2026-11-02T13:50")))
        self.assertIsNone(change(item(op="update", id=9, kind="feed", end="2026-11-02T13:50")))    # wrong kind
        self.assertIsNone(change(item(op="update", id=9, kind="sleep")))                           # no change
        self.assertIsNone(change(item(op="update", id=9, kind="sleep", end="2026-11-02T12:00")))   # before start

    def test_now_and_photo_minute_snap_to_the_exact_instant(self):
        now = NOW + dt.timedelta(seconds=40)
        c = change(item(op="update", id=9, kind="sleep", end="2026-11-02T14:00"), ctx(now=now))
        self.assertEqual(c["patch"], {"end_at": "2026-11-02T22:00:40Z"})   # "stop it" = now, not 40 s ago
        photo = dt.datetime(2026, 11, 2, 21, 40, 12, tzinfo=UTC)
        c = change(item(op="update", id=9, kind="sleep", end="2026-11-02T13:40"), ctx(photo_at=photo))
        self.assertEqual(c["patch"], {"end_at": "2026-11-02T21:40:12Z"})
        c = change(item(kind="sleep", start="2026-11-02T13:40", timing="running"), ctx(photo_at=photo))
        self.assertEqual(c["event"]["start_at"], "2026-11-02T21:40:12Z")

    def test_resume_timer(self):
        c = change(item(op="update", id=8, kind="feed", timing="running"))
        self.assertEqual(c["patch"], {"end_at": None})


class SideAndTimersTest(unittest.TestCase):
    def test_suggested_side_uses_sessions(self):
        evs = [row(3, "feed", "2026-11-02T20:20:00Z", "2026-11-02T20:30:00Z", {"method": "breast", "side": "right"}),
               row(2, "feed", "2026-11-02T20:00:00Z", "2026-11-02T20:10:00Z", {"method": "breast", "side": "left"})]
        self.assertEqual(propose_ai.suggested_side(evs, NOW), "right")   # one session that began on the left
        evs[0]["start_at"] = "2026-11-02T20:30:00Z"   # 20 min after the left feed ended: a new session
        self.assertEqual(propose_ai.suggested_side(evs, NOW), "left")
        both = [row(4, "feed", "2026-11-02T21:00:00Z", None, {"method": "breast", "side": "both"})]
        self.assertIsNone(propose_ai.suggested_side(both, NOW))
        self.assertIsNone(propose_ai.suggested_side([], NOW))

    def test_duplicate_running_timer_dropped_unless_ended(self):
        c = ctx()
        new_sleep = change(item(kind="sleep", start="2026-11-02T13:40", timing="running"), c)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(propose_ai.drop_double_logs([new_sleep], c), [])
        end_old = change(item(op="update", id=9, kind="sleep", end="2026-11-02T13:30"), c)
        self.assertEqual(propose_ai.drop_double_logs([end_old, new_sleep], c), [end_old, new_sleep])
        walk = change(item(kind="activity", activity_name="Walk", timing="running"), c)
        self.assertEqual(propose_ai.drop_double_logs([walk], c), [walk])

    def test_double_logs_dropped(self):
        c = ctx()
        keep = lambda changes: [x for x in propose_ai.drop_double_logs(changes, c)]
        with redirect_stderr(io.StringIO()) as err:
            # a sleep overlapping the running nap #9 (13:05-): dropped; one that ends before it: kept
            overlap = change(item(kind="sleep", start="2026-11-02T12:30", end="2026-11-02T13:30", timing="range"), c)
            before = change(item(kind="sleep", start="2026-11-02T10:00", end="2026-11-02T13:05", timing="range"), c)
            self.assertEqual(keep([overlap, before]), [before])
            # an old photo of a nap that's already logged (needs duration inside #9): dropped
            inside = change(item(kind="sleep", start="2026-11-02T13:30", timing="needs_duration"), c)
            self.assertEqual(keep([inside]), [])
            # a breast feed inside the logged one #8 (12:40-12:55); a bottle at that time is fine
            feed = change(item(start="2026-11-02T12:45", end="2026-11-02T12:50", timing="range", method="breast",
                               side="right"), c)
            bottle = change(item(start="2026-11-02T12:45", method="bottle", amount=60), c)
            self.assertEqual(keep([feed, bottle]), [bottle])
            # the same diaper twice (within 5 min, same details): dropped; other details or 10 min later: kept
            same = change(item(kind="diaper", start="2026-11-02T12:33", pee=True, poop=False), c)
            dirty = change(item(kind="diaper", start="2026-11-02T12:31", pee=True, poop=True), c)
            later = change(item(kind="diaper", start="2026-11-02T12:40", pee=True, poop=False), c)
            self.assertEqual(keep([same, dirty, later]), [dirty, later])
            # two overlapping sleeps proposed together: the second is dropped
            a = change(item(kind="sleep", start="2026-11-02T08:00", end="2026-11-02T09:00", timing="range"), c)
            b = change(item(kind="sleep", start="2026-11-02T08:30", end="2026-11-02T09:30", timing="range"), c)
            self.assertEqual(keep([a, b]), [a])
            # an activity of another name may overlap; the same name may not
            walk = change(item(kind="activity", activity_name="Walk", start="2026-11-02T11:00", timing="moment"), c)
            tummy = change(item(kind="activity", activity_name="tummy time", start="2026-11-02T11:02", timing="moment"), c)
            self.assertEqual(keep([walk, tummy]), [walk])   # #6 Tummy time at 11:00
        self.assertIn("overlaps #9", err.getvalue())

    def test_overlap_judged_after_this_batch_s_updates(self):
        c = ctx()
        fix = change(item(op="update", id=9, kind="sleep", start="2026-11-02T13:20"), c)   # nap now starts 13:20
        early = change(item(kind="sleep", start="2026-11-02T12:00", end="2026-11-02T13:15", timing="range"), c)
        self.assertEqual(propose_ai.drop_double_logs([fix, early], c), [fix, early])
        moved = change(item(op="update", id=8, kind="feed", start="2026-11-02T12:20"), c)   # feed #8 12:20-12:55
        clash = change(item(start="2026-11-02T12:25", end="2026-11-02T12:35", timing="range", method="breast",
                            side="left"), c)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(propose_ai.drop_double_logs([moved, clash], c), [moved])

    def test_left_on_timer_hint(self):
        stale = row(20, "sleep", "2026-11-02T17:00:00Z", None)            # 09:00, still "running"
        evs = [row(21, "feed", "2026-11-02T19:30:00Z", "2026-11-02T19:45:00Z", {"method": "breast", "side": "left"}),
               row(22, "diaper", "2026-11-02T18:00:00Z", "2026-11-02T18:00:00Z", {"pee": True}), stale]
        text = propose_ai.build_context(ctx(events=evs), None)
        self.assertIn("#20 | 2026-11-02 09:00, RUNNING (5h 00m so far; probably left on: a breast feed at 11:30"
                      " was logged after it started) | sleep", text)
        feed = row(30, "feed", "2026-11-02T21:00:00Z", None, {"method": "breast", "side": "left"})
        self.assertNotIn("probably left on", propose_ai.build_context(ctx(events=[feed] + evs[1:2]), None))
        nap = row(31, "sleep", "2026-11-02T21:00:00Z", None)              # a diaper during a nap is normal
        later_diaper = row(32, "diaper", "2026-11-02T21:30:00Z", "2026-11-02T21:30:00Z", {"pee": True})
        self.assertNotIn("probably left on", propose_ai.build_context(ctx(events=[later_diaper, nap]), None))


class ContextTest(unittest.TestCase):
    def test_context_text(self):
        photo = dt.datetime(2026, 11, 2, 21, 40, tzinfo=UTC)
        text = propose_ai.build_context(ctx(photo_at=photo, baby_name="Mia", unit="oz"), "she woke up")
        self.assertIn("Now: Monday, 2026-11-02 14:00 (America/Los_Angeles).", text)
        self.assertIn("Photo: taken 2026-11-02 13:40, 20m ago.", text)
        self.assertIn("Baby's name: Mia.", text)
        self.assertIn("Suggested breast side for the next feed: right", text)
        self.assertIn("Known activity names: Tummy time, Walk.", text)
        self.assertIn("Running timers:\n#9 | 2026-11-02 13:05, RUNNING (55m so far) | sleep, place: crib", text)
        self.assertIn("#5 | 2026-11-02 03:00 | bottle feed, formula, 88.7 ml (3.0 oz)", text)
        self.assertIn("#8 | 2026-11-02 12:40 to 12:55 (15m) | breast feed, left side", text)
        self.assertNotIn("#1 |", text)   # older than 48 h
        self.assertIn("<<<\nshe woke up\n>>>", text)

    def test_running_timers_always_shown(self):
        old_timer = row(2, "sleep", "2026-10-25T05:00:00Z", None)   # forgotten for a week
        many = [row(100 + i, "diaper", f"2026-11-02T{i // 6:02d}:{i % 6 * 10:02d}:00Z",
                    f"2026-11-02T{i // 6:02d}:{i % 6 * 10:02d}:00Z", {"pee": True}) for i in range(120)][::-1]
        shown = propose_ai.context_events(ctx(events=many + [old_timer]))
        self.assertEqual(len(shown), propose_ai.MAX_CONTEXT_ROWS + 1)
        self.assertEqual(shown[-1]["id"], 2)
        self.assertEqual(shown[0]["id"], 219)   # newest first

    def test_schema_is_json(self):
        json.dumps(propose_ai.OUTPUT_SCHEMA)
        self.assertEqual(set(propose_ai.ITEM_SCHEMA["required"]) - set(propose_ai.ITEM_SCHEMA["properties"]), set())



class EffortTest(unittest.TestCase):
    def test_parse(self):
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual([propose_ai._effort(v) for v in ("low", " HIGH ", "", None, "default", "turbo")],
                             ["low", "high", None, None, None, None])
        self.assertIn("ignoring BABY_AI_EFFORT='turbo'", err.getvalue())

    def test_defaults(self):
        import importlib

        def load(**env):
            with mock.patch.dict(os.environ, env):
                for k in ("BABY_AI_MODEL", "BABY_PHOTO_MODEL", "BABY_AI_EFFORT"):
                    if k not in env:
                        os.environ.pop(k, None)
                m = importlib.reload(propose_ai)
                return m.MODEL, m.EFFORT
        try:
            self.assertEqual(load(), ("sonnet", "low"))   # the default model runs at low effort
            self.assertEqual(load(BABY_AI_EFFORT="default"), ("sonnet", None))
            self.assertEqual(load(BABY_AI_MODEL="haiku"), ("haiku", None))   # another model: Claude Code's default
            self.assertEqual(load(BABY_PHOTO_MODEL="opus"), ("opus", None))
            self.assertEqual(load(BABY_AI_MODEL="haiku", BABY_AI_EFFORT="medium"), ("haiku", "medium"))
        finally:
            importlib.reload(propose_ai)

    def test_passed_to_claude(self):
        run, calls = fake_claude({"message": "", "items": []})
        with mock.patch.object(claude_ai, "run_claude", run), mock.patch.object(propose_ai, "EFFORT", "medium"), \
                redirect_stderr(io.StringIO()):
            propose_ai.propose(text="hi", jpeg=None, photo_taken_at=None, tz="America/Los_Angeles", baby_name="",
                               unit="ml", recent_events=[], activity_names=[], now=NOW)
        self.assertEqual((calls[0][1]["effort"], calls[0][1]["model"]), ("medium", propose_ai.MODEL))

# ---------- server ----------

def reset_db():
    server.init_db()
    with closing(server.connect()) as conn, conn:
        conn.execute("DELETE FROM events")


def fake_claude(structured):
    """Stand-in for claude_ai.run_claude returning a fixed structured answer."""
    calls = []

    def run(content, **kw):
        calls.append((content, kw))
        return claude_ai.ClaudeResult(text=json.dumps(structured), structured=structured, elapsed_ms=1234,
                                      num_turns=1, cost_usd=None, model=kw["model"])
    return run, calls


def jpeg_b64() -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 180, 160)).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


class CleanDataTest(unittest.TestCase):
    def test_whitelist_per_kind(self):
        d = server.clean_data("feed", {"method": "bottle", "milk": None, "amount_ml": 90, "side": "left", "x": 1})
        self.assertEqual(d, {"method": "bottle", "milk": None, "amount_ml": 90})
        d = server.clean_data("feed", {"method": "breast", "side": "left", "amount_ml": 90, "milk": "formula"})
        self.assertEqual(d, {"method": "breast", "side": "left"})
        self.assertEqual(server.clean_data("sleep", {"name": "x", "place": "crib"}), {"place": "crib"})
        self.assertEqual(server.clean_data("activity", {"name": " Bath ", "side": "left"}), {"name": "Bath"})

    def test_place(self):
        d = server.clean_data("sleep", {"place": "  car   seat " + "x" * 50})
        self.assertEqual(len(d["place"]), 40)
        self.assertNotIn("place", server.clean_data("sleep", {"place": "   "}))
        with self.assertRaises(server.HttpError):
            server.clean_data("sleep", {"place": 5})

    def test_stool(self):
        d = server.clean_data("diaper", {"pee": False, "poop": True, "color": "green", "texture": "seedy"})
        self.assertEqual(d, {"pee": False, "poop": True, "color": "green", "texture": "seedy"})
        d = server.clean_data("diaper", {"pee": True, "poop": False, "color": "green", "texture": "seedy"})
        self.assertEqual(d, {"pee": True, "poop": False})
        for bad in ({"color": "purple"}, {"texture": "lumpy"}):
            with self.assertRaises(server.HttpError):
                server.clean_data("diaper", {"poop": True, **bad})

    def test_ai_tag(self):
        d = server.clean_data("sleep", {"ai": {"source": "photo", "model": "sonnet" * 10, "confidence": 2,
                                               "edited": "yes", "extra": 1}})
        self.assertEqual(d["ai"], {"source": "photo", "model": ("sonnet" * 10)[:30], "confidence": None, "edited": False})
        with self.assertRaises(server.HttpError):
            server.clean_data("sleep", {"ai": {"source": "magic"}})
        old = {"photo": {"suggested": "feeding", "confidence": 0.97}}
        self.assertEqual(server.clean_data("sleep", dict(old)), old)

    def test_patch_keeps_ai_and_photo_tags(self):
        ev = row(1, "feed", "2026-11-02T20:00:00Z", "2026-11-02T20:00:00Z",
                 {"method": "bottle", "amount_ml": 90, "ai": {"source": "text", "model": "sonnet",
                                                             "confidence": 0.9, "edited": False},
                  "photo": {"suggested": "feeding", "confidence": 0.5}})
        after = server.apply_patch(ev, {"data": {"method": "bottle", "amount_ml": 120}})
        self.assertEqual(after["data"]["ai"]["source"], "text")
        self.assertEqual(after["data"]["photo"]["suggested"], "feeding")
        self.assertEqual(ev["data"]["amount_ml"], 90)   # the input row is not modified
        after = server.apply_patch(ev, {"data": {"method": "bottle", "amount_ml": 120, "ai": None}})
        self.assertNotIn("ai", after["data"])


class ExportTest(unittest.TestCase):
    def test_new_columns(self):
        reset_db()
        server.create_event({"kind": "sleep", "start_at": "2026-11-02T08:00:00Z", "end_at": "2026-11-02T09:30:00Z",
                             "data": {"place": "crib", "ai": {"source": "photo", "model": "sonnet",
                                                              "confidence": 0.8, "edited": True}},
                             "tz": "America/Los_Angeles"}, None)
        server.create_event({"kind": "diaper", "start_at": "2026-11-02T10:00:00Z",
                             "data": {"pee": True, "poop": True, "color": "yellow", "texture": "seedy"}}, None)
        sleep, diaper = server.export_rows()
        self.assertEqual(sleep["end_local"], "2026-11-02T01:30:00-08:00")
        self.assertEqual((sleep["place"], sleep["ai_source"], sleep["ai_model"], sleep["ai_confidence"],
                          sleep["ai_edited"]), ("crib", "photo", "sonnet", 0.8, 1))
        self.assertEqual((diaper["color"], diaper["texture"], diaper["ai_edited"]), ("yellow", "seedy", None))
        header = server.export_csv().decode().splitlines()[0].split(",")
        self.assertEqual(header, server.CSV_COLUMNS)
        for col in ("end_local", "place", "color", "texture", "ai_source", "ai_model", "ai_confidence", "ai_edited"):
            self.assertIn(col, header)


class ProposeEndpointTest(unittest.TestCase):
    def setUp(self):
        reset_db()
        now = dt.datetime.now(UTC).replace(microsecond=0)
        self.now = now
        self.sleep = server.create_event({"kind": "sleep", "start_at": propose_ai.iso(now - dt.timedelta(minutes=90)),
                                          "data": {"place": "crib"}, "tz": "America/Los_Angeles"}, None)
        self.feed = server.create_event({"kind": "feed", "start_at": propose_ai.iso(now - dt.timedelta(hours=3)),
                                         "end_at": propose_ai.iso(now - dt.timedelta(hours=3) + dt.timedelta(minutes=12)),
                                         "data": {"method": "breast", "side": "left",
                                                  "ai": {"source": "text", "model": "sonnet", "confidence": 0.9,
                                                         "edited": False}},
                                         "tz": "America/Los_Angeles"}, None)

    def local(self, minutes_ago: int) -> str:
        return (self.now - dt.timedelta(minutes=minutes_ago)).astimezone(LA).strftime("%Y-%m-%dT%H:%M")

    def propose(self, structured, body):
        run, calls = fake_claude(structured)
        with mock.patch.object(claude_ai, "run_claude", run), redirect_stderr(io.StringIO()) as err:
            out = server.ai_propose(body)
        return out, calls, err.getvalue()

    def test_creates_and_updates_are_validated(self):
        structured = {"message": "Got it.", "items": [
            item(kind="feed", start=self.local(30), timing="moment", method="bottle", milk="formula",
                 amount=4, amount_unit="oz", note="Drank it all."),
            item(op="update", id=self.sleep["id"], kind="sleep", end=self.local(20)),
            item(op="update", id=self.feed["id"], kind="feed", side="right"),
            item(kind="diaper", start=self.local(10), pee=True, poop=True, color="pale", texture="pasty"),
            item(op="update", id=self.sleep["id"] + 100, kind="sleep", end=self.local(20)),   # unknown id
            item(kind="sleep", start=self.local(5), timing="running"),   # duplicate of the (still running) nap? no: ended above
        ]}
        out, calls, log = self.propose(structured, {"text": "4 oz formula 30 min ago ...", "tz": "America/Los_Angeles"})
        self.assertEqual((out["source"], out["message"], out["elapsed_ms"], out["model"]), ("text", "Got it. (Left out 1 suggestion that didn't fit the log.)", 1234, "sonnet"))
        ops = [(p["op"], p.get("event", {}).get("kind") or p["before"]["kind"]) for p in out["proposals"]]
        self.assertEqual(ops, [("create", "feed"), ("update", "sleep"), ("update", "feed"), ("create", "diaper"),
                               ("create", "sleep")])
        bottle, wake, side, diaper, nap = out["proposals"]
        self.assertEqual(bottle["event"]["data"], {"method": "bottle", "milk": "formula", "amount_ml": 118.3})
        self.assertEqual(bottle["event"]["end_at"], bottle["event"]["start_at"])
        self.assertEqual((bottle["event"]["note"], bottle["needs"], bottle["confidence"]), ("Drank it all.", [], 0.9))
        self.assertEqual(set(wake["patch"]), {"end_at"})
        self.assertEqual(wake["before"], self.sleep)
        self.assertEqual(wake["after"]["end_at"], wake["patch"]["end_at"])
        self.assertEqual(wake["after"]["data"], {"place": "crib"})
        self.assertEqual(side["patch"]["data"]["side"], "right")
        self.assertEqual(side["patch"]["data"]["ai"]["source"], "text")   # tag kept, like a manual edit
        self.assertEqual(diaper["event"]["data"]["color"], "pale")
        self.assertIsNone(nap["event"]["end_at"])
        self.assertIn("update of an entry Claude wasn't shown", log)
        # what Claude saw: the running nap with its id, the time, the message
        content, kw = calls[0]
        self.assertEqual(len(content), 1)
        self.assertIn(f"#{self.sleep['id']} | ", content[0]["text"])
        self.assertIn("RUNNING", content[0]["text"])
        self.assertEqual(kw["output_schema"], propose_ai.OUTPUT_SCHEMA)
        # nothing was saved
        self.assertEqual(len(server.list_events({})), 2)
        self.assertIsNone(server.list_events({})[0]["end_at"])

    def test_proposals_post_and_patch_as_is(self):
        structured = {"message": "", "items": [
            item(kind="activity", activity_name="Tummy time", start=self.local(3), timing="moment", place="play mat"),
            item(op="update", id=self.sleep["id"], kind="sleep", end=self.local(1)),
        ]}
        out, _, _ = self.propose(structured, {"text": "tummy time, she woke up a minute ago"})
        create, update = out["proposals"]
        saved = server.create_event({**create["event"], "tz": "America/Los_Angeles"}, None)
        self.assertEqual({k: saved[k] for k in create["event"]}, create["event"])
        updated = server.update_event(update["id"], update["patch"])
        self.assertEqual({k: updated[k] for k in ("start_at", "end_at", "data", "note")},
                         {k: update["after"][k] for k in ("start_at", "end_at", "data", "note")})

    def test_photo(self):
        taken = self.now - dt.timedelta(minutes=12)
        structured = {"message": "Baby is awake on the play mat.", "items": [
            item(op="update", id=self.sleep["id"], kind="sleep", end=self.local(12)),
            item(kind="activity", activity_name="Play", timing="moment", start=None, note="Smiling at the mobile."),
        ]}
        out, calls, _ = self.propose(structured, {"image": jpeg_b64(), "photo_taken_at": taken.isoformat(),
                                                  "tz": "America/Los_Angeles", "baby_name": "Mia", "unit": "oz"})
        self.assertEqual(out["source"], "photo")
        play = out["proposals"][1]["event"]
        self.assertEqual(play["start_at"], propose_ai.iso(taken))   # no start from Claude: the photo time
        content = calls[0][0]
        self.assertEqual([b["type"] for b in content], ["image", "text"])
        self.assertIn("Photo: taken", content[1]["text"])
        self.assertIn("Baby's name: Mia.", content[1]["text"])
        out, calls, _ = self.propose({"message": "x", "items": []}, {"image": jpeg_b64(), "text": "at 3"})
        self.assertEqual((out["source"], out["proposals"]), ("photo+text", []))
        self.assertIn("Photo: just taken", calls[0][0][1]["text"])   # no capture time: now

    def test_model_garbage_never_crashes(self):
        for structured in (None, [], {"items": "x"}, {"items": [None, 5, {"op": "create"}, {"op": "update", "id": "x"}]},
                           {"items": [item(kind="feed", method="bottle", amount=float("nan"))], "message": 7}):
            out, _, _ = self.propose(structured, {"text": "hello"})
            self.assertIsInstance(out["proposals"], list)
            self.assertIsInstance(out["message"], str)

    def test_check_change_drops_invalid(self):
        with self.assertRaises(server.HttpError):
            server.check_change({"op": "create", "event": {"kind": "feed", "start_at": "2026-11-02T10:00:00Z",
                                                           "end_at": None, "data": {"method": "bottle", "amount_ml": 5000},
                                                           "note": ""}, "needs": []})
        with self.assertRaises(server.HttpError):   # deleted meanwhile
            server.check_change({"op": "update", "id": 999, "patch": {"end_at": None}})
        with self.assertRaises(server.HttpError):   # changes nothing
            server.check_change({"op": "update", "id": self.sleep["id"], "patch": {"data": {"place": "crib"}}})

    def test_ai_error_passes_through(self):
        def boom(*a, **kw):
            raise claude_ai.AIError("Claude didn't answer within 45 s. Try again.", 504)
        with mock.patch.object(claude_ai, "run_claude", boom):
            with self.assertRaises(server.HttpError) as e:
                server.ai_propose({"text": "hi"})
        self.assertEqual((e.exception.status, str(e.exception)), (504, "Claude didn't answer within 45 s. Try again."))

    def test_request_checks(self):
        cases = [
            ({}, "send a photo or some text"),
            ({"text": "   "}, "send a photo or some text"),
            ({"text": 5}, "text must be text"),
            ({"text": "x" * 2001}, "text is too long (max 2000 characters)"),
            ({"text": "hi", "tz": "Mars/Base"}, "tz must be an IANA time zone"),
            ({"text": "hi", "tz": 7}, "tz must be an IANA time zone"),
            ({"text": "hi", "unit": "cups"}, "unit must be ml or oz"),
            ({"image": "not base64!!"}, "image is not valid base64"),
            ({"image": 12}, "image must be a base64 string"),
            ({"image": base64.b64encode(b"hello").decode()}, "Couldn't read that image"),
            ({"image": jpeg_b64(), "photo_taken_at": "2026-11-02T10:00"}, "photo_taken_at: timestamp needs a timezone"),
        ]
        run, calls = fake_claude({"message": "", "items": []})
        with mock.patch.object(claude_ai, "run_claude", run):
            for body, msg in cases:
                with self.assertRaises(server.HttpError, msg=body) as e:
                    server.ai_propose(body)
                self.assertEqual(e.exception.status, 400, body)
                self.assertIn(msg, str(e.exception), body)
        self.assertEqual(calls, [])   # none of these reached Claude

    def test_data_url_and_long_name_tolerated(self):
        out, calls, _ = self.propose({"message": "", "items": []},
                                     {"image": "data:image/jpeg;base64," + jpeg_b64(), "baby_name": "M" * 60})
        self.assertIn("Baby's name: " + "M" * 40 + ".", calls[0][0][1]["text"])

    def test_not_set_up(self):
        with mock.patch.object(server, "propose_ai", None), self.assertRaises(server.HttpError) as e:
            server.ai_propose({"text": "hi"})
        self.assertEqual(e.exception.status, 503)
        with mock.patch.object(server, "ask_ai", None), self.assertRaises(server.HttpError) as e:
            server.ai_ask({"question": "hi"})
        self.assertEqual(e.exception.status, 503)


class AskEndpointTest(unittest.TestCase):
    def test_wiring(self):
        reset_db()
        seen = {}

        def ask(**kw):
            seen.update(kw)
            return {"answer": "**2** diapers", "elapsed_ms": 9, "model": "sonnet", "queries": 1}
        fake = mock.Mock(ask=ask)
        history = [{"q": f"q{i}", "a": "a" * 5000} for i in range(8)]
        with mock.patch.object(server, "ask_ai", fake):
            out = server.ai_ask({"question": " how many? ", "history": history, "tz": "Europe/Berlin", "unit": "oz"})
            self.assertEqual(out["answer"], "**2** diapers")
            self.assertEqual((seen["question"], seen["tz"], seen["unit"], seen["baby_name"]),
                             ("how many?", "Europe/Berlin", "oz", ""))
            self.assertEqual([t["q"] for t in seen["history"]], ["q2", "q3", "q4", "q5", "q6", "q7"])
            self.assertEqual(len(seen["history"][0]["a"]), 4000)
            self.assertEqual(seen["rows"], [])
            for body in ({"question": ""}, {"question": "x" * 1001}, {"question": "hi", "history": "x"},
                         {"question": "hi", "history": [{"q": 1}]}):
                with self.assertRaises(server.HttpError, msg=body):
                    server.ai_ask(body)


class HttpTest(unittest.TestCase):
    """Body size and content-type checks through a real server on a random port."""

    @classmethod
    def setUpClass(cls):
        reset_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, path, body: bytes, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_limits(self):
        big = json.dumps({"text": "hi", "image": "A" * 100_000}).encode()   # > 64 KB, < 8 MB
        self.assertEqual(self.post("/api/events", big)[0], 413)
        status, body = self.post("/api/ai/propose", big)
        self.assertEqual((status, body["error"]), (400, "Couldn't read that image. Try a JPEG or PNG photo."))
        # too big: refused from the Content-Length alone, before reading the body
        conn = http.client.HTTPConnection(*self.httpd.server_address)
        conn.putrequest("POST", "/api/ai/propose")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "8000001")
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["error"]), (413, "request body too large (max 8000 KB)"))
        conn.close()
        self.assertEqual(self.post("/api/ai/propose", b"{}", "text/plain")[0], 415)
        self.assertEqual(self.post("/api/ai/propose", b"[1]")[0], 400)
        self.assertEqual(self.post("/api/ai/propose", b'{"text": NaN}')[0], 400)

    def test_old_endpoint_is_gone(self):
        self.assertEqual(self.post("/api/photo/classify", b"x", "image/jpeg")[0], 404)


if __name__ == "__main__":
    unittest.main()
