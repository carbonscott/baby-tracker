"""Tests for ask_ai.py (no live Claude). Run: .venv/bin/python -m unittest tests.test_ask"""

import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ask_ai  # noqa: E402
import claude_ai  # noqa: E402

LA = "America/Los_Angeles"
NOW = dt.datetime(2026, 11, 2, 22, 30, tzinfo=dt.timezone.utc)   # Mon 2:30 PM PST
_ids = iter(range(1, 10_000))


def row(kind, start, end="same", tz=LA, **data):
    """An export_rows()-style dict. Old export shape: no place/color/texture/ai_* keys unless given."""
    r = {"id": next(_ids), "kind": kind, "start_at": start, "end_at": start if end == "same" else end,
         "note": "", "tz": tz, "logged_by": "alex@example.com"}
    r.update(data)
    return r


def snap(rows, tz=LA, now=NOW):
    return ask_ai.build_snapshot(rows, tz, now)


def q(conn, sql):
    text, err = ask_ai.run_sql(conn, sql)
    assert not err, text
    return [line.split(" | ") for line in text.splitlines()[1:]]


class SnapshotTest(unittest.TestCase):
    def test_old_export_rows_without_new_columns(self):
        conn = snap([row("diaper", "2026-11-02T19:21:00Z", pee=1, poop=1)])
        self.assertEqual(q(conn, "SELECT start_local, local_date, local_hour, pee, poop, color, place, duration_min"
                                 " FROM events"),
                         [["2026-11-02 11:21", "2026-11-02", "11", "1", "1", "NULL", "NULL", "NULL"]])

    def test_new_columns_and_derived_fields(self):
        conn = snap([row("diaper", "2026-11-02T19:21:00Z", pee=1, poop=1, color="green", texture="loose",
                         place="changing table", ai_source="photo", ai_model="sonnet", end_local="x"),
                     row("feed", "2026-11-02T20:00:00Z", method="bottle", amount_ml=None, photo_suggested="feeding",
                         logged_by=None)])
        self.assertEqual(q(conn, "SELECT color, texture, place, via_claude, logged_by FROM events ORDER BY id"),
                         [["green", "loose", "changing table", "photo", "alex"],
                          ["NULL", "NULL", "NULL", "photo", "NULL"]])

    def test_local_time_uses_row_tz_then_request_tz(self):
        conn = snap([row("diaper", "2026-11-02T13:00:00Z", tz="America/New_York"),
                     row("diaper", "2026-11-02T13:00:00Z", tz=None),
                     row("diaper", "2026-11-02T13:00:00Z", tz="Not/AZone")])
        self.assertEqual(q(conn, "SELECT start_local FROM events ORDER BY id"),
                         [["2026-11-02 08:00"], ["2026-11-02 05:00"], ["2026-11-02 05:00"]])

    def test_bad_request_tz_falls_back_to_utc(self):
        conn = snap([row("diaper", "2026-11-02T13:00:00Z", tz=None)], tz="../../etc/passwd")
        self.assertEqual(q(conn, "SELECT start_local FROM events"), [["2026-11-02 13:00"]])

    def test_dst_fall_back_duration_is_real_time(self):
        # 00:30 PDT -> 03:00 PST on 2026-11-01 is 3.5 hours, not 2.5
        conn = snap([row("sleep", "2026-11-01T07:30:00Z", "2026-11-01T11:00:00Z")])
        self.assertEqual(q(conn, "SELECT start_local, end_local, duration_min FROM events"),
                         [["2026-11-01 00:30", "2026-11-01 03:00", "210"]])

    def test_sleep_split_at_local_midnight_on_spring_forward(self):
        # 2026-03-07 23:00 PST -> 2026-03-08 04:00 PDT: 4 real hours, 60 min before midnight, 180 after
        conn = snap([row("sleep", "2026-03-08T07:00:00Z", "2026-03-08T11:00:00Z")])
        self.assertEqual(q(conn, "SELECT local_date, start_local, end_local, minutes FROM day_segments"),
                         [["2026-03-07", "2026-03-07 23:00", "2026-03-08 00:00", "60"],
                          ["2026-03-08", "2026-03-08 00:00", "2026-03-08 04:00", "180"]])

    def test_multi_day_sleep_and_running_timer(self):
        conn = snap([row("sleep", "2026-11-01T06:00:00Z", None)])   # 11 PM Oct 31 PDT, still running
        self.assertEqual(q(conn, "SELECT end_utc, end_local, running, duration_min FROM events"),
                         [["NULL", "NULL", "1", str(round((NOW - dt.datetime(2026, 11, 1, 6, tzinfo=dt.timezone.utc))
                                                          .total_seconds() / 60))]])
        days = q(conn, "SELECT local_date, minutes, running FROM day_segments")
        self.assertEqual([d[0] for d in days], ["2026-10-31", "2026-11-01", "2026-11-02"])
        self.assertEqual(days[0][1:], ["60", "1"])
        self.assertEqual(days[1][1], str(25 * 60))   # the 25-hour DST day
        self.assertEqual(days[2][1], str(14 * 60 + 30))

    def test_night_of(self):
        conn = snap([row("sleep", f"2026-11-02T{h:02d}:00:00Z", "2026-11-02T23:59:00Z") for h in (2, 3, 13, 14)])
        # 6 PM and 7 PM Nov 1, 5 AM and 6 AM Nov 2 (all PST)
        self.assertEqual(q(conn, "SELECT start_local, night_of FROM events ORDER BY id"),
                         [["2026-11-01 18:00", "NULL"], ["2026-11-01 19:00", "2026-11-01"],
                          ["2026-11-02 05:00", "2026-11-01"], ["2026-11-02 06:00", "NULL"]])

    def test_point_events_have_no_duration(self):
        conn = snap([row("diaper", "2026-11-02T10:00:00Z", pee=1, poop=0),
                     row("feed", "2026-11-02T10:00:00Z", method="bottle", amount_ml=90),
                     row("activity", "2026-11-02T10:00:00Z", name="Bath"),
                     row("activity", "2026-11-02T11:00:00Z", "2026-11-02T11:10:00Z", name="Tummy time")])
        self.assertEqual(q(conn, "SELECT duration_min FROM events ORDER BY id"),
                         [["NULL"], ["NULL"], ["NULL"], ["10"]])
        self.assertEqual(q(conn, "SELECT count(*), name FROM day_segments"), [["1", "Tummy time"]])

    def test_feed_sessions(self):
        conn = snap([
            row("feed", "2026-11-02T10:00:00Z", "2026-11-02T10:15:00Z", method="breast", side="left"),
            row("feed", "2026-11-02T10:16:00Z", "2026-11-02T10:25:00Z", method="breast", side="right"),
            row("feed", "2026-11-02T10:40:00Z", method="bottle", milk="breastmilk", amount_ml=30),  # 15 min: same
            row("feed", "2026-11-02T12:00:00Z", method="bottle", amount_ml=None),                 # new session
            row("feed", "2026-11-02T12:15:01Z", method="bottle", amount_ml=60),                   # 15m01s: new
            row("feed", "2026-11-02T14:00:00Z", None, method="breast", side="right"),             # running
        ])
        self.assertEqual(q(conn, "SELECT feed_session FROM events ORDER BY id"),
                         [["1"], ["1"], ["1"], ["2"], ["3"], ["4"]])
        self.assertEqual(
            q(conn, "SELECT session, start_local, end_local, feeds, breast_min, bottle_ml, bottles, methods,"
                    " first_side, sides, gap_min, running FROM feed_sessions"),
            [["1", "2026-11-02 02:00", "2026-11-02 02:40", "3", "24", "30", "1", "breast+bottle", "left",
              "left,right", "NULL", "0"],
             ["2", "2026-11-02 04:00", "2026-11-02 04:00", "1", "NULL", "NULL", "1", "bottle", "NULL", "NULL",
              "120", "0"],
             ["3", "2026-11-02 04:15", "2026-11-02 04:15", "1", "NULL", "60", "1", "bottle", "NULL", "NULL",
              "15", "0"],
             ["4", "2026-11-02 06:00", "NULL", "1", "510", "NULL", "0", "breast", "right", "right", "105", "1"]])

    def test_days_table(self):
        conn = snap([
            row("diaper", "2026-10-31T15:00:00Z", pee=1, poop=0),                         # Oct 31, the log's 1st day
            row("sleep", "2026-11-01T06:00:00Z", "2026-11-01T09:00:00Z"),                 # 11 PM PDT -> 1 AM PST
            row("feed", "2026-11-01T18:00:00Z", "2026-11-01T18:20:00Z", method="breast", side="left"),
            row("feed", "2026-11-01T18:21:00Z", "2026-11-01T18:30:00Z", method="breast", side="right"),
            row("feed", "2026-11-01T20:00:00Z", method="bottle", milk="formula", amount_ml=60),
            row("feed", "2026-11-01T23:00:00Z", method="bottle", milk="breastmilk", amount_ml=None),
            row("diaper", "2026-11-01T21:00:00Z", pee=1, poop=1),
            row("diaper", "2026-11-01T22:00:00Z", pee=0, poop=0),
            row("diaper", "2026-11-02T20:00:00Z", pee=1, poop=0),                         # today
        ])
        self.assertEqual(q(conn, "SELECT * FROM days ORDER BY local_date"), [
            ["2026-10-31", "0", "0", "0", "0", "0", "0", "1", "1", "0", "60", "180"],
            ["2026-11-01", "1", "3", "29", "2", "60", "60", "2", "1", "1", "120", "NULL"],
            ["2026-11-02", "0", "0", "0", "0", "0", "0", "1", "1", "0", "0", "NULL"]])

    def test_overlap_min_helper(self):
        conn = snap([row("sleep", "2026-11-01T20:00:00Z", "2026-11-01T23:00:00Z"),   # half inside the window
                     row("sleep", "2026-11-02T22:00:00Z", None)])                      # running: 30 min so far
        self.assertEqual(q(conn, "SELECT overlap_min(start_utc, end_utc, datetime('2026-11-02 22:30:00', '-24 hours'),"
                                 " '2026-11-02 22:30:00') FROM events ORDER BY id"), [["30"], ["30"]])
        self.assertEqual(q(conn, "SELECT overlap_min(start_utc, end_utc, '2026-11-03 00:00:00', '2026-11-04 00:00:00'),"
                                 " overlap_min(NULL, NULL, 'x', 'y') FROM events LIMIT 1"), [["0", "NULL"]])

    def test_sleep_and_breast_feed_without_a_duration(self):
        # end == start ("Don't know"): duration NULL, no minutes added, still counted
        conn = snap([
            row("sleep", "2026-11-02T18:00:00Z"),                                                      # 10 AM
            row("sleep", "2026-11-02T19:00:00Z", "2026-11-02T20:00:00Z"),
            row("feed", "2026-11-02T17:00:00Z", method="breast", side="left"),                         # 9 AM
            row("feed", "2026-11-02T21:00:00Z", method="breast", side="left"),                         # 1 PM
            row("feed", "2026-11-02T21:05:00Z", "2026-11-02T21:15:00Z", method="breast", side="right"),
        ])
        self.assertEqual(q(conn, "SELECT kind, duration_min, running, end_local FROM events ORDER BY id"),
                         [["sleep", "NULL", "0", "2026-11-02 10:00"], ["sleep", "60", "0", "2026-11-02 12:00"],
                          ["feed", "NULL", "0", "2026-11-02 09:00"], ["feed", "NULL", "0", "2026-11-02 13:00"],
                          ["feed", "10", "0", "2026-11-02 13:15"]])
        self.assertEqual(q(conn, "SELECT count(*) FROM day_segments"), [["2"]])
        self.assertEqual(q(conn, "SELECT feeds, breast_min, sleep_min, longest_sleep_min FROM days"),
                         [["2", "10", "60", "60"]])
        self.assertEqual(q(conn, "SELECT feeds, breast_min, sides FROM feed_sessions ORDER BY session"),
                         [["1", "NULL", "left"], ["2", "10", "left,right"]])

    def test_running_timer_started_after_server_now(self):
        # the phone's clock a little ahead, or a start typed up to a minute ahead (the page allows that)
        conn = snap([row("sleep", "2026-11-02T22:30:20Z", None)])
        self.assertEqual(q(conn, "SELECT duration_min, running FROM events"), [["0", "1"]])
        self.assertIn("Running now: sleep since 2:30 PM (0 min).", ask_ai.glance(conn, ask_ai.zone_for(LA)))

    def test_empty_log(self):
        conn = snap([])
        self.assertEqual(q(conn, "SELECT count(*) FROM events"), [["0"]])
        self.assertEqual(ask_ai.glance(conn, ask_ai.zone_for(LA)), "the log is empty.")


class SqlToolTest(unittest.TestCase):
    def setUp(self):
        self.conn = snap([row("diaper", "2026-11-02T19:21:00Z", pee=1, poop=1, note="x" * 1000),
                          row("sleep", "2026-11-02T20:00:00Z", "2026-11-02T21:00:00Z")])

    def refused(self, sql):
        text, err = ask_ai.run_sql(self.conn, sql)
        self.assertTrue(err, f"{sql!r} was allowed: {text}")
        return text

    def test_writes_and_schema_changes_refused(self):
        for sql in ["INSERT INTO events (id, kind) VALUES (99, 'feed')", "UPDATE events SET note = 'x'",
                    "DELETE FROM events", "DROP TABLE events", "CREATE TABLE t (a)", "CREATE TEMP TABLE t (a)",
                    "WITH x AS (SELECT 1) INSERT INTO events (id) SELECT * FROM x",
                    "REPLACE INTO events (id) VALUES (1)",
                    "BEGIN", "SAVEPOINT a", "ANALYZE", "REINDEX", "VACUUM"]:
            self.refused(sql)
        self.assertEqual(q(self.conn, "SELECT count(*) FROM events"), [["2"]])

    def test_attach_pragma_vacuum_into_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for sql in [f"ATTACH '{d}/x.db' AS x", f"VACUUM INTO '{d}/copy.db'", "PRAGMA query_only = OFF",
                        "PRAGMA writable_schema = ON", "SELECT load_extension('/tmp/x')"]:
                self.refused(sql)
            self.assertEqual(os.listdir(d), [])

    def test_lock_holds_even_without_the_select_check(self):
        # The SELECT-prefix check is only a first filter; the connection itself refuses everything else.
        for sql in ["PRAGMA query_only = OFF", "INSERT INTO events (id, kind) VALUES (99, 'feed')",
                    "ATTACH ':memory:' AS x", "VACUUM INTO 'nope.db'", "SELECT load_extension('x')"]:
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.conn.execute(sql)
        self.assertFalse(Path("nope.db").exists())

    def test_one_statement_only(self):
        self.refused("SELECT 1; DELETE FROM events")
        self.assertEqual(q(self.conn, "SELECT count(*) FROM events;"), [["2"]])   # trailing ; is fine

    def test_comments_and_with_allowed(self):
        self.assertEqual(q(self.conn, "-- last poop\n/* c */ WITH d AS (SELECT * FROM events WHERE poop = 1)"
                                      " SELECT count(*) FROM d"), [["1"]])

    def test_huge_values_refused(self):
        self.assertIn("too big", self.refused("SELECT zeroblob(5000000)"))
        self.refused("SELECT randomblob(5000000)")

    def test_row_cap(self):
        text, err = ask_ai.run_sql(self.conn, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n"
                                              " WHERE i < 1000) SELECT i FROM n")
        self.assertFalse(err)
        lines = text.splitlines()
        self.assertEqual(len(lines), 1 + ask_ai.MAX_ROWS + 1)
        self.assertIn("stopped at 200 rows", lines[-1])

    def test_timeout(self):
        started = dt.datetime.now()
        text, err = ask_ai.run_sql(self.conn, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n)"
                                              " SELECT count(*) FROM n", seconds=0.2)
        self.assertTrue(err)
        self.assertIn("longer than 0.2 s", text)
        self.assertLess((dt.datetime.now() - started).total_seconds(), 2)
        self.assertEqual(q(self.conn, "SELECT count(*) FROM events"), [["2"]])   # still usable afterwards

    def test_errors_are_readable(self):
        self.assertIn("no such column: nope", self.refused("SELECT nope FROM events"))
        self.assertIn("Only a single SELECT", self.refused(""))

    def test_compact_output(self):
        text, _ = ask_ai.run_sql(self.conn, "SELECT note, duration_min, 1.0 / 3 AS third, NULL AS n FROM events"
                                            " ORDER BY id")
        header, first, second = text.splitlines()
        self.assertEqual(header, "note | duration_min | third | n")
        self.assertTrue(first.startswith("x" * ask_ai.MAX_CELL + "… | NULL | 0.33 | NULL"))
        self.assertEqual(second, " | 60 | 0.33 | NULL")
        self.assertIn("(no rows)", ask_ai.run_sql(self.conn, "SELECT id FROM events WHERE 0")[0])

    def test_tool_handler_counts_calls(self):
        calls = []
        sql = ask_ai.sql_tool(self.conn, calls)
        ok = asyncio.run(sql.handler({"query": "SELECT count(*) AS n FROM events"}))
        bad = asyncio.run(sql.handler({"query": "DELETE FROM events"}))
        self.assertEqual(ok, {"content": [{"type": "text", "text": "n\n2"}], "is_error": False})
        self.assertTrue(bad["is_error"])
        self.assertEqual(len(calls), 2)


class PromptAndAskTest(unittest.TestCase):
    rows = [row("diaper", "2026-11-02T19:21:00Z", pee=1, poop=1),
            row("sleep", "2026-11-02T22:13:00Z", None),
            row("activity", "2026-11-02T18:00:00Z", name="Tummy time"),
            row("activity", "2026-11-01T18:00:00Z", name="tummy time")]

    def test_system_prompt(self):
        conn = snap(self.rows)
        p = ask_ai.system_prompt(conn, ask_ai.zone_for(LA), NOW, "  Mia ", "oz")
        self.assertIn("The baby's name is Mia.", p)
        self.assertIn("Now: Monday 2026-11-02 2:30 PM (America/Los_Angeles)", p)
        self.assertIn("now_utc = '2026-11-02 22:30:00', today = '2026-11-02', yesterday = '2026-11-01'", p)
        self.assertIn("show amounts in oz", p)
        self.assertIn("Running now: sleep since 2:13 PM (17 min).", p)
        self.assertIn("Activities: Tummy time (2).", p)
        self.assertIn("CREATE TABLE day_segments", p)
        self.assertIn("end == start on a sleep or breast feed = duration not logged", p)
        p = ask_ai.system_prompt(conn, ask_ai.zone_for(LA), NOW, "", "weird")
        self.assertIn('say "the baby"', p)
        self.assertIn("show amounts in ml", p)

    def test_user_message_keeps_last_six_turns_trimmed(self):
        history = [{"q": f"q{i}", "a": "a" * 5000} for i in range(8)] + ["junk"]
        msg = ask_ai.user_message("And yesterday?", history)
        self.assertNotIn("Q: q1\n", msg)
        self.assertIn("Q: q2\n", msg)
        self.assertIn("A: " + "a" * 4000 + "\n", msg)
        self.assertNotIn("a" * 4001, msg)
        self.assertTrue(msg.endswith("Question: And yesterday?"))
        self.assertEqual(ask_ai.user_message("Hi?", None), "Question: Hi?")

    def test_ask_runs_claude_with_only_the_sql_tool(self):
        fake = claude_ai.ClaudeResult(text=" **2:05 PM** ", structured=None, elapsed_ms=1234, num_turns=2,
                                      cost_usd=None, model="sonnet")
        with mock.patch.object(claude_ai, "run_claude", return_value=fake) as run:
            out = ask_ai.ask(question=" When did she last poop? ", history=[], tz=LA, baby_name="Mia", unit="ml",
                             rows=self.rows, now=NOW)
        self.assertEqual(out, {"answer": "**2:05 PM**", "elapsed_ms": 1234, "model": "sonnet", "queries": 0})
        kw = run.call_args.kwargs
        self.assertEqual(kw["allowed_tools"], ["mcp__log__sql"])
        self.assertEqual(list(kw["mcp_servers"]), ["log"])
        self.assertEqual((kw["model"], kw["max_turns"], kw["timeout_s"]),
                         (ask_ai.MODEL, ask_ai.MAX_TURNS, ask_ai.TIMEOUT_S))
        self.assertEqual(run.call_args.args[0], "Question: When did she last poop?")

    def test_ask_input_checks(self):
        for question in ["", "   ", "x" * 1001]:
            with self.assertRaises(claude_ai.AIError) as e:
                ask_ai.ask(question=question, history=[], tz=LA, baby_name="", unit="ml", rows=[])
            self.assertEqual(e.exception.status, 400)
        empty = claude_ai.ClaudeResult("", None, 1, 1, None, "sonnet")
        with mock.patch.object(claude_ai, "run_claude", return_value=empty), self.assertRaises(claude_ai.AIError):
            ask_ai.ask(question="hi?", history=[], tz=LA, baby_name="", unit="ml", rows=[])


if __name__ == "__main__":
    unittest.main()
