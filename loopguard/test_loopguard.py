#!/usr/bin/env python3
"""Tests for loopguard. Standard library only.

    python3 -m unittest discover -s product/loopguard -v
    python3 product/loopguard/test_loopguard.py

Every case here comes from a real failure or a real line in the log this tool
was written against; the negation and unfinished-cycle cases in particular are
regression tests for bugs that shipped in 0.1.0.
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loopguard as lg  # noqa: E402

START = re.compile(lg.DEFAULT_START_RE, re.IGNORECASE)
END = re.compile(lg.DEFAULT_END_RE, re.IGNORECASE)


def log(*cycles: tuple[str, str, object]) -> str:
    """Build a log body. Each cycle is (start_ts, body, rc); rc None = unfinished."""
    out = []
    for ts, body, rc in cycles:
        out.append(f"===== {ts} JST cycle start =====")
        out.append(body)
        if rc is not None:
            out.append(f"===== {ts} JST cycle end rc={rc} =====")
    return "\n".join(out) + "\n"


def cycles_of(text: str) -> list[lg.Cycle]:
    return lg.split_cycles(text, "test.log", START, END)


def ago(*, days: float = 0, minutes: float = 0) -> str:
    """A timestamp that much before now, in the format the default markers use.

    Relative on purpose: a fixture with dates written into it stops testing
    staleness the day after it is written, and starts testing the calendar.
    """
    when = datetime.now() - timedelta(days=days, minutes=minutes)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def dated_cycles(*starts: datetime) -> list[lg.Cycle]:
    return [lg.Cycle(source="test.log", started=t, body="A" * 200, rc=0) for t in starts]


def judged(body: str, rc: object = 0, timeout=None, min_output=lg.THIN_OUTPUT_CHARS) -> lg.Cycle:
    c = cycles_of(log(("2026-08-31 05:00:00", body, rc)))[0]
    lg.judge(c, timeout, min_output)
    return c


class SplitCycles(unittest.TestCase):
    def test_pairs_start_and_end(self):
        cs = cycles_of(log(
            ("2026-08-31 05:00:00", "first", 0),
            ("2026-08-31 11:00:00", "second", 0),
        ))
        self.assertEqual(len(cs), 2)
        self.assertEqual([c.body.strip() for c in cs], ["first", "second"])
        self.assertEqual([c.rc for c in cs], [0, 0])
        self.assertFalse(any(c.unfinished for c in cs))

    def test_start_without_end_is_unfinished(self):
        cs = cycles_of(log(("2026-08-31 13:15:00", "", None)))
        self.assertEqual(len(cs), 1)
        self.assertTrue(cs[0].unfinished)
        self.assertIsNone(cs[0].rc)
        self.assertIn("no end marker", cs[0].notes[0])

    def test_start_after_start_closes_the_previous(self):
        # A crash that never wrote a footer, followed by the next run.
        text = (
            "===== 2026-08-31 05:00:00 JST cycle start =====\n"
            "half a sentence\n"
            "===== 2026-08-31 11:00:00 JST cycle start =====\n"
            "the next one\n"
            "===== 2026-08-31 11:07:00 JST cycle end rc=0 =====\n"
        )
        cs = cycles_of(text)
        self.assertEqual(len(cs), 2)
        self.assertEqual(cs[0].body.strip(), "half a sentence")
        self.assertFalse(cs[1].unfinished)

    def test_japanese_markers(self):
        text = (
            "===== 2026-08-31 05:00:01 JST サイクル開始 =====\n"
            "本文\n"
            "===== 2026-08-31 05:09:32 JST サイクル終了 rc=0 =====\n"
        )
        cs = cycles_of(text)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].rc, 0)

    def test_duration(self):
        text = (
            "===== 2026-08-31 05:00:00 JST cycle start =====\n"
            "x\n"
            "===== 2026-08-31 05:10:00 JST cycle end rc=0 =====\n"
        )
        self.assertEqual(cycles_of(text)[0].duration_s, 600)

    def test_no_markers_yields_nothing(self):
        self.assertEqual(cycles_of("just some prose\nand more\n"), [])


class Judge(unittest.TestCase):
    def test_clean_cycle_has_no_problems(self):
        c = judged("A" * 200)
        self.assertTrue(c.ok)

    def test_timeout_exit_codes(self):
        for rc in (124, 137):
            with self.subTest(rc=rc):
                c = judged("A" * 200, rc=rc)
                self.assertTrue(any("killed by timeout" in p for p in c.problems))

    def test_other_nonzero_exit(self):
        c = judged("A" * 200, rc=1)
        self.assertTrue(any("non-zero exit" in p for p in c.problems))

    def test_real_usage_limit_is_caught(self):
        c = judged("The session stopped: usage limit reached, resets at 18:00. " + "A" * 200)
        self.assertTrue(any("provider limit" in p for p in c.problems))

    def test_authentication_failure_is_caught(self):
        c = judged("Error: invalid API key. " + "A" * 200)
        self.assertTrue(any("authentication failed" in p for p in c.problems))

    # --- regression: the 0.1.1 bug ------------------------------------------
    def test_negated_limit_is_not_a_problem(self):
        # The exact shape that shipped bad advice: the agent reporting health.
        c = judged("No evidence of a usage limit this cycle. " + "A" * 200)
        self.assertTrue(c.ok, c.problems)

    def test_negated_limit_is_still_announced(self):
        c = judged("No evidence of a usage limit this cycle. " + "A" * 200)
        self.assertTrue(any("ignored as negated" in n for n in c.notes),
                        "suppressed matches must be visible, never silent")

    def test_japanese_negation(self):
        c = judged("使用量の上限に当たった形跡はなし。" + "あ" * 200)
        self.assertTrue(c.ok, c.problems)

    def test_negation_does_not_swallow_a_real_hit_elsewhere(self):
        body = ("No evidence of a usage limit in cycle 1. "
                + "A" * 300
                + " Cycle 2 stopped: rate limit reached.")
        c = judged(body)
        self.assertTrue(any("provider limit" in p for p in c.problems))

    def test_negation_window_is_local(self):
        # "no" far away must not neutralise the match.
        body = "no problems at all. " + "A" * 300 + " usage limit reached, stopping."
        c = judged(body)
        self.assertTrue(any("provider limit" in p for p in c.problems))

    # --- regression: the 0.1.0 bug ------------------------------------------
    def test_thin_output_on_finished_cycle_is_a_problem(self):
        c = judged("done", rc=0)
        self.assertTrue(any("almost no output" in p for p in c.problems))

    def test_thin_output_on_unfinished_cycle_is_only_a_note(self):
        c = cycles_of(log(("2026-08-31 13:15:00", "", None)))[0]
        lg.judge(c, None)
        self.assertTrue(c.ok, c.problems)
        self.assertTrue(any("not written yet" in n for n in c.notes))

    def test_min_output_is_configurable(self):
        self.assertTrue(judged("short body here", min_output=500).problems)
        self.assertFalse(judged("short body here", min_output=5).problems)

    def test_near_timeout_is_a_note_not_a_problem(self):
        text = (
            "===== 2026-08-31 05:00:00 JST cycle start =====\n"
            + "A" * 200 + "\n"
            "===== 2026-08-31 05:50:00 JST cycle end rc=0 =====\n"
        )
        c = cycles_of(text)[0]
        lg.judge(c, timeout_s=3300)
        self.assertTrue(c.ok)
        self.assertTrue(any("close to the" in n for n in c.notes))


class Repeats(unittest.TestCase):
    def test_identical_output_is_flagged(self):
        body = "The cycle did the same thing again. " * 6
        cs = cycles_of(log(
            ("2026-08-31 05:00:00", body, 0),
            ("2026-08-31 11:00:00", body, 0),
        ))
        lg.flag_repeats(cs)
        self.assertTrue(cs[0].ok)
        self.assertTrue(any("identical to the previous" in p for p in cs[1].problems))

    def test_different_output_is_not_flagged(self):
        cs = cycles_of(log(
            ("2026-08-31 05:00:00", "Wrote the checker and fixed a parser bug. " * 5, 0),
            ("2026-08-31 11:00:00", "Drafted four chapters and priced the note. " * 5, 0),
        ))
        lg.flag_repeats(cs)
        self.assertTrue(all(c.ok for c in cs))

    def test_unfinished_cycle_is_never_compared(self):
        body = "The cycle did the same thing again. " * 6
        cs = cycles_of(log(
            ("2026-08-31 05:00:00", body, 0),
            ("2026-08-31 11:00:00", body, None),
        ))
        lg.flag_repeats(cs)
        self.assertTrue(all(c.ok for c in cs), "a partial body must not be compared")

    def test_short_bodies_are_not_compared(self):
        cs = cycles_of(log(
            ("2026-08-31 05:00:00", "ok", 0),
            ("2026-08-31 11:00:00", "ok", 0),
        ))
        lg.flag_repeats(cs)
        self.assertFalse(any("identical" in p for c in cs for p in c.problems))


class Suggest(unittest.TestCase):
    def _limited(self, n: int, total: int) -> list[lg.Cycle]:
        cs = []
        for i in range(total):
            c = lg.Cycle(source="t", body="A" * 200)
            if i < n:
                c.problems.append("provider limit hit ('usage limit') - widen the interval")
            cs.append(c)
        return cs

    def test_majority_limited_halves_the_frequency(self):
        self.assertIn("halve", lg.suggest_interval(self._limited(3, 4), 4))

    def test_one_limit_drops_a_run(self):
        self.assertIn("drop one run", lg.suggest_interval(self._limited(1, 4), 4))

    def test_thin_cycles_blame_the_prompt_not_the_interval(self):
        cs = []
        for _ in range(4):
            c = lg.Cycle(source="t", body="x")
            c.problems.append("almost no output (1 chars) - the cycle probably did nothing")
            cs.append(c)
        self.assertIn("prompt", lg.suggest_interval(cs, 4))

    def test_healthy_and_infrequent_suggests_more_often(self):
        cs = [lg.Cycle(source="t", body="A" * 200) for _ in range(4)]
        self.assertIn("run more often", lg.suggest_interval(cs, 4))

    def test_healthy_and_frequent_says_nothing(self):
        cs = [lg.Cycle(source="t", body="A" * 200) for _ in range(4)]
        self.assertIsNone(lg.suggest_interval(cs, 24))

    def test_unfinished_cycles_are_excluded_from_the_sample(self):
        # A single in-progress cycle must not drive a recommendation.
        c = lg.Cycle(source="t", body="", unfinished=True)
        self.assertIsNone(lg.suggest_interval([c], 24))


class Cli(unittest.TestCase):
    def _run(self, body: str, *argv: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "2026-08-31.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = lg.main([path, *argv])
            return rc, out.getvalue(), err.getvalue()

    def test_healthy_log_exits_zero(self):
        rc, out, _ = self._run(log(("2026-08-31 05:00:00", "A" * 200, 0)))
        self.assertEqual(rc, 0)
        self.assertIn("0 needing attention", out)

    def test_broken_log_exits_one(self):
        rc, out, _ = self._run(log(("2026-08-31 05:00:00", "A" * 200, 1)))
        self.assertEqual(rc, 1)
        self.assertIn("non-zero exit", out)

    def test_directory_argument(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.log"), "w", encoding="utf-8") as fh:
                fh.write(log(("2026-08-31 05:00:00", "A" * 200, 0)))
            out = io.StringIO()
            with redirect_stdout(out):
                rc = lg.main([d])
        self.assertEqual(rc, 0)
        self.assertIn("1 cycle(s)", out.getvalue())

    def test_json_output_is_parseable_and_has_no_body(self):
        import json
        rc, out, _ = self._run(log(("2026-08-31 05:00:00", "A" * 200, 0)), "--json")
        data = json.loads(out)
        self.assertEqual(data["version"], lg.__version__)
        self.assertEqual(data["cycles"][0]["output_chars"], 200)
        self.assertNotIn("body", data["cycles"][0], "the log body must not leak into JSON")

    def test_unparseable_log_exits_two_with_a_hint(self):
        rc, _, err = self._run("nothing that looks like a cycle\n")
        self.assertEqual(rc, 2)
        self.assertIn("--start-re", err)

    def test_bad_regex_exits_two(self):
        rc, _, err = self._run(log(("2026-08-31 05:00:00", "A" * 200, 0)), "--start-re", "(")
        self.assertEqual(rc, 2)
        self.assertIn("bad regex", err)

    def test_missing_path_exits_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = lg.main(["/nonexistent/loopguard-test"])
        self.assertEqual(rc, 2)


FOREIGN_LOG = """[2026-08-30 09:00:00] INFO  run 1 begin
[2026-08-30 09:00:01] doing some work that is long enough to count as output
[2026-08-30 09:04:12] INFO  run 1 finished exit=0
[2026-08-30 10:00:00] INFO  run 2 begin
[2026-08-30 10:00:01] more work, also long enough to be a real cycle body here
[2026-08-30 10:02:00] INFO  run 2 finished exit=1
"""


class GuessMarkers(unittest.TestCase):
    """A log the defaults cannot read must produce advice that actually works."""

    def test_finds_start_and_end_words(self):
        g = lg.guess_markers(FOREIGN_LOG)
        self.assertEqual(g["start_word"], "begin")
        self.assertEqual(g["end_word"], "finished")
        self.assertEqual(g["rc_key"], "exit")
        self.assertEqual(g["stamped_lines"], 6)

    def test_whole_word_not_substring(self):
        # "finish" precedes "finished" in END_WORDS; picking it would generate
        # \bfinish\b, which matches nothing in this log. Regression for the bug
        # where the suggested command was printed without being tried.
        g = lg.guess_markers(FOREIGN_LOG)
        self.assertNotEqual(g["end_word"], "finish")

    def test_no_timestamp_gives_up_cleanly(self):
        g = lg.guess_markers("run begin\nrun finished\n")
        self.assertEqual(g["stamped_lines"], 0)
        self.assertIsNone(g["start_word"])

    def test_end_word_does_not_steal_the_start_line(self):
        # "restart complete" must not make "start" the start marker.
        text = ("2026-08-30 09:00:00 cycle opening here\n"
                "2026-08-30 09:01:00 restart complete\n")
        g = lg.guess_markers(text)
        self.assertNotEqual(g["start_word"], "start")

    def test_suggestion_is_verified_before_it_is_printed(self):
        out = "\n".join(lg.explain_unread("agent.log", FOREIGN_LOG))
        self.assertIn("reads it as 2 cycle(s)", out)
        self.assertIn("--start-re", out)
        self.assertIn("--end-re", out)

    def test_suggested_command_really_parses_the_log(self):
        out = "\n".join(lg.explain_unread("agent.log", FOREIGN_LOG))
        start = re.search(r"--start-re '([^']+)'", out).group(1)
        end = re.search(r"--end-re '([^']+)'", out).group(1)
        cycles = lg.split_cycles(FOREIGN_LOG, "agent.log",
                                 re.compile(start, re.IGNORECASE),
                                 re.compile(end, re.IGNORECASE))
        self.assertEqual(len(cycles), 2)
        self.assertEqual([c.rc for c in cycles], [0, 1])

    def test_unguessable_log_says_so_rather_than_bluffing(self):
        text = "2026-08-30 09:00:00 zzz\n2026-08-30 09:05:00 zzz\n"
        out = "\n".join(lg.explain_unread("agent.log", text))
        self.assertIn("could not guess", out)
        self.assertNotIn("reads it as", out)

    def test_timestamped_but_unreadable_mentions_start_re(self):
        out = "\n".join(lg.explain_unread("agent.log", "no timestamps at all\n"))
        self.assertIn("--start-re", out)
        self.assertIn("ts", out)


class SilentlySkippedFiles(unittest.TestCase):
    """The dangerous failure: one file reads, another is dropped without a word."""

    def _run_dir(self, files: dict) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as d:
            for name, body in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = lg.main([d])
            return rc, out.getvalue(), err.getvalue()

    def test_a_skipped_file_is_announced_even_when_others_read(self):
        rc, out, err = self._run_dir({
            "a.log": log(("2026-08-31 05:00:00", "A" * 200, 0)),
            "b.log": FOREIGN_LOG,
        })
        self.assertEqual(rc, 0, "the readable file is still healthy")
        self.assertIn("1 of 2 file(s) produced no cycles", err)
        self.assertIn("b.log", err)

    def test_all_files_readable_says_nothing(self):
        rc, out, err = self._run_dir({"a.log": log(("2026-08-31 05:00:00", "A" * 200, 0))})
        self.assertEqual(err, "")

    def test_json_lists_the_skipped_files(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("a.log", log(("2026-08-31 05:00:00", "A" * 200, 0))),
                               ("b.log", FOREIGN_LOG)):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                lg.main([d, "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual(len(data["files_without_cycles"]), 1)
        self.assertTrue(data["files_without_cycles"][0].endswith("b.log"))

    def test_custom_markers_are_named_in_the_message(self):
        rc, out, err = self._run_dir({"b.log": FOREIGN_LOG})
        self.assertIn("produced no cycles:", err)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.log")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(FOREIGN_LOG)
            err2 = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err2):
                lg.main([p, "--start-re", r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) NOPE"])
            self.assertIn("with the markers given", err2.getvalue())


class Staleness(unittest.TestCase):
    """A loop that has stopped writes no failing cycle. It writes nothing."""

    def _every(self, count: int, gap_min: float, silence_min: float) -> list[lg.Cycle]:
        now = datetime.now()
        last = now - timedelta(minutes=silence_min)
        return dated_cycles(*[last - timedelta(minutes=gap_min * i)
                              for i in reversed(range(count))])

    def test_dead_loop_is_reported_though_every_cycle_says_ok(self):
        # The failure this whole check exists for: daily cycles, all rc=0,
        # nothing since. Before 0.3.0 this printed "0 needing attention".
        cycles = self._every(4, gap_min=1440, silence_min=9 * 1440)
        msg = lg.check_staleness(cycles, datetime.now())
        self.assertIsNotNone(msg)
        self.assertIn("may have stopped", msg)
        self.assertIn("9d", msg)

    def test_a_running_loop_is_not_stale(self):
        cycles = self._every(6, gap_min=90, silence_min=20)
        self.assertIsNone(lg.check_staleness(cycles, datetime.now()))

    def test_one_missed_run_on_a_fast_loop_does_not_alarm(self):
        # Every 5 minutes, silent for 20. Three intervals, but 20 minutes of
        # quiet is not a stopped loop -- MIN_STALE_S is the floor that says so.
        cycles = self._every(8, gap_min=5, silence_min=20)
        self.assertIsNone(lg.check_staleness(cycles, datetime.now()))

    def test_too_few_cycles_to_know_what_normal_is(self):
        cycles = self._every(2, gap_min=90, silence_min=10 * 1440)
        self.assertIsNone(lg.check_staleness(cycles, datetime.now()),
                          "with two starts there is one gap; that is not an interval")

    def test_explicit_threshold_works_where_the_guess_declines(self):
        cycles = self._every(2, gap_min=90, silence_min=300)
        self.assertIsNone(lg.check_staleness(cycles, datetime.now()))
        msg = lg.check_staleness(cycles, datetime.now(), stale_after_s=60 * 60)
        self.assertIsNotNone(msg)
        self.assertIn("--stale-after", msg)

    def test_zero_disables_the_check(self):
        cycles = self._every(5, gap_min=1440, silence_min=30 * 1440)
        self.assertIsNotNone(lg.check_staleness(cycles, datetime.now()))
        self.assertIsNone(lg.check_staleness(cycles, datetime.now(), stale_after_s=0))

    def test_a_log_ahead_of_the_clock_is_not_stale(self):
        now = datetime.now()
        cycles = dated_cycles(*[now + timedelta(minutes=m) for m in (0, 90, 180)])
        self.assertIsNone(lg.check_staleness(cycles, now),
                          "a clock skew is not evidence about the loop")

    def test_no_dated_cycle_means_no_opinion(self):
        cycles = [lg.Cycle(source="test.log", started=None, body="A" * 200)]
        self.assertIsNone(lg.check_staleness(cycles, datetime.now()))

    def test_median_ignores_one_long_outage(self):
        now = datetime.now()
        starts = [now - timedelta(minutes=m) for m in (5000, 200, 110, 20)]
        median = lg.median_interval_s(dated_cycles(*starts))
        self.assertAlmostEqual(median / 60, 90, delta=1,
                               msg="one 80-hour gap must not become the loop's normal")

    def test_duration_reads_at_a_glance(self):
        self.assertEqual(lg._dur(35 * 60), "35m")
        self.assertEqual(lg._dur(4 * 3600 + 12 * 60), "4h 12m")
        self.assertEqual(lg._dur(9 * 86400 + 13 * 3600), "9d 13h")


class SinceDays(unittest.TestCase):
    """--since counts days. Until 0.3.0 it counted files, in name order."""

    def _run_dir(self, files: dict, *argv: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as d:
            for name, body in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = lg.main([d, "--stale-after", "0", *argv])
            return rc, out.getvalue(), err.getvalue()

    def test_filters_cycles_inside_one_file(self):
        # Old behaviour: one file in, one file out, --since did nothing at all.
        # distinct bodies: identical ones are a stuck loop, a different finding
        body = log(*[(ago(days=n), chr(65 + n) * 200, 0) for n in (3, 2, 1, 0)])
        rc, out, _ = self._run_dir({"all.log": body}, "--since", "2")
        self.assertEqual(rc, 0)
        self.assertIn("2 cycle(s)", out)

    def test_does_not_choose_files_by_name(self):
        # Regression: sorted() puts watcher.log last, so files[-1:] was it, and
        # the day actually asked for was dropped without a word.
        rc, out, err = self._run_dir(
            {"2026-08-31.log": log((ago(days=0), "A" * 200, 0)),
             "watcher.log": "a service log with no cycle markers at all\n"},
            "--since", "1")
        self.assertEqual(rc, 0)
        self.assertIn("1 cycle(s)", out)
        self.assertIn("watcher.log", err, "the unreadable file is still named")

    def test_says_what_it_excluded(self):
        body = log(*[(ago(days=n), "A" * 200, 0) for n in (5, 4, 0)])
        _, _, err = self._run_dir({"all.log": body}, "--since", "1")
        self.assertIn("2 older excluded", err)

    def test_an_empty_window_is_a_finding_not_a_blank_report(self):
        # "Nothing ran today" is the answer, not the absence of one.
        body = log(*[(ago(days=n), "A" * 200, 0) for n in (9, 8, 7)])
        rc, _, err = self._run_dir({"all.log": body}, "--since", "1")
        self.assertEqual(rc, 1, "an idle window needs attention; 2 means unreadable")
        self.assertIn("no cycle started in the last 1 day", err)

    def test_zero_days_is_rejected(self):
        rc, _, err = self._run_dir({"a.log": log((ago(days=0), "A" * 200, 0))}, "--since", "0")
        self.assertEqual(rc, 2)
        self.assertIn("--since counts days", err)

    def test_undated_cycles_are_kept_not_dropped(self):
        cycles = dated_cycles(datetime.now(), datetime.now() - timedelta(days=9))
        cycles.append(lg.Cycle(source="x", started=None, body="A" * 200))
        kept, dropped, undated = lg.filter_since(cycles, 1, date.today())
        self.assertEqual((len(kept), dropped, undated), (2, 1, 1))

    def test_json_reports_the_exclusion(self):
        body = log(*[(ago(days=n), "A" * 200, 0) for n in (6, 0)])
        _, out, _ = self._run_dir({"all.log": body}, "--since", "1", "--json")
        self.assertEqual(json.loads(out)["cycles_excluded_by_since"], 1)


class AbandonedCycles(unittest.TestCase):
    """A start with no end, overtaken by a later start, is a killed run."""

    KILLED = log(("2026-08-23 09:00:00", "B" * 200, None),
                 ("2026-08-24 09:00:00", "C" * 200, 0))

    def test_interrupted_cycle_is_marked_unfinished(self):
        cycles = cycles_of(self.KILLED)
        self.assertTrue(cycles[0].unfinished,
                        "only the last cycle in the file got this mark before 0.3.0")
        self.assertIsNone(cycles[0].rc)

    def test_it_becomes_a_problem_not_a_note(self):
        cycles = cycles_of(self.KILLED)
        lg.flag_abandoned(cycles)
        self.assertTrue(cycles[0].abandoned)
        self.assertFalse(cycles[0].ok)
        self.assertIn("killed, not finished", cycles[0].problems[0])
        self.assertTrue(cycles[1].ok)

    def test_the_cycle_still_running_is_left_alone(self):
        # loopguard is usually run *by* the last cycle. Flagging that one would
        # make the tool fail on every healthy loop that uses it.
        cycles = cycles_of(log(("2026-08-24 09:00:00", "C" * 200, 0),
                               ("2026-08-24 10:00:00", "D" * 200, None)))
        lg.flag_abandoned(cycles)
        self.assertFalse(cycles[1].abandoned)
        self.assertTrue(cycles[1].ok)

    def test_a_killed_cycle_is_not_excused_for_thin_output(self):
        cycles = cycles_of(log(("2026-08-23 09:00:00", "x", None),
                               ("2026-08-24 09:00:00", "C" * 200, 0)))
        lg.flag_abandoned(cycles)
        lg.judge(cycles[0], None)
        self.assertTrue(any("almost no output" in p for p in cycles[0].problems))
        self.assertFalse(any("still in progress" in n for n in cycles[0].notes),
                         "it is not in progress; a later cycle already started")

    def test_cli_exits_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(log((ago(days=0, minutes=120), "B" * 200, None),
                             (ago(days=0, minutes=30), "C" * 200, 0)))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = lg.main([path, "--stale-after", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("killed, not finished", out.getvalue())


class TimestampShapes(unittest.TestCase):
    """0.4.0: two accepted formats was a parochial choice, not a design.

    An unreadable timestamp is not a partial loss here - it removes the cycle's
    start time, and a cycle with no start time cannot be dated, ordered, or
    checked for silence. So the shapes other loggers actually emit are read.
    """

    def test_slash_dates(self):
        self.assertEqual(lg._parse_ts("2026/08/31 05:00:01"),
                         datetime(2026, 8, 31, 5, 0, 1))

    def test_fractional_seconds_are_dropped_not_rejected(self):
        self.assertEqual(lg._parse_ts("2026-08-31T05:00:01.123456"),
                         datetime(2026, 8, 31, 5, 0, 1))
        self.assertEqual(lg._parse_ts("2026-08-31 05:00:01,123"),
                         datetime(2026, 8, 31, 5, 0, 1))

    def test_offset_is_applied_not_ignored(self):
        # The whole point: everything downstream compares against a naive local
        # now(), so a UTC log read on a +09:00 machine must not look 9h stale.
        utc = lg._parse_ts("2026-08-31T20:00:00Z")
        plus9 = lg._parse_ts("2026-09-01T05:00:00+09:00")
        plus9_compact = lg._parse_ts("2026-09-01T05:00:00+0900")
        self.assertEqual(utc, plus9)
        self.assertEqual(utc, plus9_compact)

    def test_a_zone_name_is_not_an_offset(self):
        # 'JST' is a label this tool has no table for. It is left alone rather
        # than guessed at - the ts group in the default markers stops before it.
        m = re.compile(lg.DEFAULT_START_RE, re.IGNORECASE).search(
            "===== 2026-08-31 05:00:01 JST cycle start =====")
        self.assertEqual(m.group("ts"), "2026-08-31 05:00:01")

    def test_shaped_like_a_date_but_is_not_one(self):
        self.assertIsNone(lg._parse_ts("2026-13-45 05:00:01"))

    def test_default_markers_still_read_this_loops_own_log(self):
        cycles = cycles_of(log(("2026-08-31 05:00:01", "x" * 200, 0)))
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].started, datetime(2026, 8, 31, 5, 0, 1))


class MarkerlessLogs(unittest.TestCase):
    """0.4.0: a log with no cycle brackets used to get exit 2 and no answer.

    'Has the loop stopped?' is answerable from the last line's timestamp. It
    does not need cycles. Declining to answer it because the log was not
    bracketed the way this loop brackets its own was the tool refusing its own
    headline question.
    """

    FLAT = ("2026/08/25 04:09:58 INFO  agent woke up\n"
            "2026/08/25 04:10:31 INFO  reading prompt\n"
            "2026/08/25 04:11:02 ERROR usage limit reached, resets at 09:00\n")

    def _scan(self, text, now=datetime(2026, 9, 1, 3, 0, 0), stale_after_s=None):
        return lg.flat_scan("agent.log", text, now, stale_after_s)

    def test_last_activity_is_the_newest_readable_stamp(self):
        self.assertEqual(self._scan(self.FLAT).last_activity,
                         datetime(2026, 8, 25, 4, 11, 2))

    def test_dead_flat_loop_is_reported_when_told_what_too_long_means(self):
        scan = self._scan(self.FLAT, stale_after_s=3600)
        self.assertIn("may have stopped", scan.stale)
        self.assertIn("6d", scan.stale)

    def test_silence_is_not_judged_without_an_interval_to_judge_it_against(self):
        # No cycles means no median interval, and there is no honest default:
        # three hours of quiet is a dead loop on one schedule and mid-run on
        # another. It must say it did not judge, not that all is well.
        scan = self._scan(self.FLAT)
        self.assertIsNone(scan.stale)
        out = lg.render_flat([scan], None)
        self.assertIn("cannot be judged", out)
        self.assertNotIn("not called stopped", out)

    def test_limit_is_still_found_without_markers(self):
        self.assertIn("provider limit hit", self._scan(self.FLAT).limit)

    def test_negation_still_applies_without_markers(self):
        scan = self._scan("2026-08-31 05:00:00 no evidence of a usage limit\n")
        self.assertIsNone(scan.limit)

    def test_report_names_the_checks_it_could_not_run(self):
        out = lg.render_flat([self._scan(self.FLAT)], None)
        self.assertIn("not run:", out)
        self.assertIn("not a clean bill of health", out)

    def test_no_timestamp_at_all_says_so(self):
        scan = self._scan("hello\nworld\n")
        self.assertIsNone(scan.last_activity)
        self.assertIn("no readable timestamp", lg.render_flat([scan], None))

    def test_future_stamp_is_not_reported_as_fresh(self):
        scan = self._scan(self.FLAT, now=datetime(2026, 8, 1, 0, 0, 0))
        self.assertIsNone(scan.silence_s)
        self.assertIn("in the future", lg.render_flat([scan], 3600))


class MarkerlessCli(unittest.TestCase):
    """The exit code is the part a cron line reads. It has to be right."""

    def _run(self, files: dict, *args) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as d:
            for name, body in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = lg.main([d, *args])
        return rc, out.getvalue(), err.getvalue()

    def test_a_finding_in_an_unparseable_file_is_not_exit_zero(self):
        # The bug this pins: one file parsed cleanly, another said "usage limit
        # reached" and could not be carved into cycles, and loopguard printed
        # "0 needing attention" and exited 0. The unreadable file's finding was
        # in the stderr preamble about regexes, where a cron line never looks.
        rc, out, _ = self._run({
            "good.log": log((ago(minutes=40), "D" * 300, 0)),
            "other.log": "2026-08-25 04:11:02 ERROR usage limit reached\n",
        }, "--stale-after", "0")
        self.assertEqual(rc, 1)
        self.assertIn("provider limit hit", out)
        self.assertIn("no cycles read", out)

    def test_markerless_only_still_answers(self):
        rc, out, _ = self._run(
            {"a.log": "2026-08-25 04:11:02 ERROR usage limit reached\n"})
        self.assertEqual(rc, 1)
        self.assertIn("no cycles could be read", out)
        self.assertIn("provider limit hit", out)

    def test_nothing_to_say_is_two_not_zero(self):
        # Nothing found, but the cycle checks never ran. "I could not judge"
        # is exit 2; a green 0 here would be the tool's own chapter-4 failure.
        rc, out, _ = self._run({"a.log": "2026-08-25 04:11:02 INFO fine\n"})
        self.assertEqual(rc, 2)

    def test_json_still_emits_when_nothing_parsed(self):
        rc, out, _ = self._run(
            {"a.log": "2026-08-25 04:11:02 ERROR usage limit reached\n"}, "--json")
        data = json.loads(out)
        self.assertEqual(data["cycles"], [])
        self.assertEqual(len(data["flat"]), 1)
        self.assertIn("provider limit hit", data["flat"][0]["findings"][0])

    def test_findings_are_not_printed_twice(self):
        rc, out, err = self._run(
            {"a.log": "2026-08-25 04:11:02 ERROR usage limit reached\n"})
        self.assertEqual((out + err).count("provider limit hit"), 1)


class QuotedMentions(unittest.TestCase):
    """0.5.0: a log line *about* a phrase is not a log line reporting it.

    Found by pointing the tool at this loop's own log, where the previous
    cycle's summary quoted `usage limit reached` while describing a bug about
    that string. 0.4.0 reported a provider limit on a loop that had never hit
    one, and recommended running less often.
    """

    def _limit(self, line):
        return lg._first_match(lg.LIMIT_RES, line)

    def test_prose_quoting_the_phrase_is_unconfirmed(self):
        hit, _, quoted = self._limit(
            "the tool detected `usage limit reached` in the unreadable file")
        self.assertEqual(hit, "usage limit")
        self.assertTrue(quoted)

    def test_japanese_prose_quoting_the_phrase_is_unconfirmed(self):
        _, _, quoted = self._limit("読めない側に「usage limit reached」があると")
        self.assertTrue(quoted)

    # ⚠ The three below are the "must NOT be caught" direction. Quoting is not
    # the same as harmless: a real provider error is usually quoted.
    def test_json_error_record_is_confirmed(self):
        _, _, quoted = self._limit(
            'ERROR {"type":"rate_limit_error","message":"usage limit reached"}')
        self.assertFalse(quoted)

    def test_http_429_line_is_confirmed(self):
        _, _, quoted = self._limit('HTTP/1.1 429 body="usage limit reached"')
        self.assertFalse(quoted)

    def test_bare_match_is_confirmed(self):
        _, _, quoted = self._limit("claude: usage limit reached, resets at 05:00")
        self.assertFalse(quoted)

    def test_unquoted_match_anywhere_outranks_a_quoted_one(self):
        # One real line beats any number of lines discussing it, wherever it is.
        _, _, quoted = self._limit(
            "cycle 3 mentioned `usage limit reached`\n"
            "claude: rate limit exceeded\n")
        self.assertFalse(quoted)

    def test_quote_state_does_not_leak_across_lines(self):
        # A stray apostrophe on an earlier line must not re-classify the file.
        _, _, quoted = self._limit(
            "the loop's first night\nclaude: usage limit reached\n")
        self.assertFalse(quoted)

    def test_negation_still_wins_over_quoting(self):
        hit, skipped, _ = self._limit("no evidence of a `usage limit` this cycle")
        self.assertIsNone(hit)
        self.assertGreaterEqual(skipped, 1)


class QuotedMentionsReachTheVerdict(unittest.TestCase):
    """B25's rule: a finding must reach every place a verdict is computed."""

    def _cycle(self, body):
        c = lg.Cycle(source="a.log", rc=0, body=body + "A" * 200)
        lg.judge(c, None)
        return c

    def test_unconfirmed_hit_is_still_a_problem(self):
        # Downgrading it to a note would be trading a false alarm for a miss.
        c = self._cycle("summary: `usage limit reached` was in the other file")
        self.assertTrue(any("provider limit" in p for p in c.problems))
        self.assertTrue(any(p.startswith(lg.QUOTED_PREFIX) for p in c.problems))

    def test_unconfirmed_hit_does_not_advise_running_less_often(self):
        cycles = []
        for i in range(4):
            cycles.append(self._cycle("summary: `usage limit reached` elsewhere"))
        self.assertIsNone(lg.suggest_interval(cycles, None))

    def test_confirmed_hit_still_advises(self):
        cycles = []
        for i in range(4):
            cycles.append(self._cycle("claude: usage limit reached"))
        s = lg.suggest_interval(cycles, None)
        self.assertIsNotNone(s)
        self.assertIn("provider limit", s)


class SyslogTimestampTest(unittest.TestCase):
    """`Sep  1 03:37:57` - the format with no year in it.

    The tool's headline question is "has the loop stopped?", and the answer
    needs one thing: when the last line was written. journald and rsyslog write
    the majority of the world's unattended-job logs and they write no year, so
    every one of those files was previously "no readable timestamp".

    Each rule below is exercised against the input it is meant to reject as
    well as the one it is meant to read [B20].
    """

    NOW = datetime(2026, 9, 1, 12, 0, 0)

    def test_a_syslog_stamp_is_read(self):
        self.assertEqual(lg._parse_ts("Sep  1 03:37:57", self.NOW),
                         datetime(2026, 9, 1, 3, 37, 57))

    def test_a_two_digit_day_with_one_space_is_read(self):
        self.assertEqual(lg._parse_ts("Aug 31 23:59:00", self.NOW),
                         datetime(2026, 8, 31, 23, 59, 0))

    def test_a_month_later_in_the_year_is_taken_as_last_year(self):
        # December, read in September. Choosing this year would place the line
        # three months in the future and make the silence negative.
        self.assertEqual(lg._parse_ts("Dec 25 01:02:03", self.NOW),
                         datetime(2025, 12, 25, 1, 2, 3))

    def test_a_stamp_just_ahead_of_the_clock_is_still_this_year(self):
        # Clock skew between the writer and the reader must not cost a year.
        self.assertEqual(lg._parse_ts("Sep  1 18:00:00", self.NOW),
                         datetime(2026, 9, 1, 18, 0, 0))

    def test_the_29th_of_february_lands_on_a_leap_year(self):
        self.assertEqual(lg._parse_ts("Feb 29 12:00:00", datetime(2026, 3, 1, 0, 0, 0)),
                         datetime(2024, 2, 29, 12, 0, 0))

    def test_prose_that_looks_like_a_month_is_not_a_timestamp(self):
        # The one that would be silent: a wrong reading is worse than none.
        for text in ("Sept 1 03:37:57", "Sep 1 3:37:57", "Sepia 1 03:37:57",
                     "Sep 100 03:37:57", "May be 12:00:00"):
            self.assertIsNone(lg._parse_ts(text, self.NOW), text)

    def test_a_full_timestamp_still_wins(self):
        self.assertEqual(lg._parse_ts("2026-08-31 05:00:01", self.NOW),
                         datetime(2026, 8, 31, 5, 0, 1))

    def test_a_syslog_log_gets_a_last_line_and_a_staleness_verdict(self):
        text = "\n".join([
            "Sep  1 03:00:00 host agent[1]: cycle starting",
            "Sep  1 03:04:11 host agent[1]: done",
        ])
        scan = lg.flat_scan("syslog", text, self.NOW, stale_after_s=3600)
        self.assertEqual(scan.stamped_lines, 2)
        self.assertEqual(scan.last_activity, datetime(2026, 9, 1, 3, 4, 11))
        self.assertIsNotNone(scan.stale)

    def test_a_guessed_year_is_reported_as_guessed(self):
        # A datetime cannot carry "I made this up". The report has to.
        scan = lg.flat_scan("syslog", "Sep  1 03:04:11 host agent[1]: done", self.NOW)
        self.assertTrue(scan.year_assumed)
        out = lg.render_flat([scan], None)
        self.assertIn("no year", out)

    def test_a_dated_log_is_not_marked_as_guessed(self):
        scan = lg.flat_scan("dated", "2026-09-01 03:04:11 done", self.NOW)
        self.assertFalse(scan.year_assumed)
        self.assertNotIn("no year", lg.render_flat([scan], None))

    def test_the_json_report_carries_the_flag(self):
        scan = lg.flat_scan("syslog", "Sep  1 03:04:11 done", self.NOW)
        self.assertTrue(scan.as_dict()["year_assumed"])


class UnmatchedEndMarkerTest(unittest.TestCase):
    """An end marker arriving with no cycle open.

    Two very different causes, and telling them apart is the whole check:
    before the first start it is a rotated log beginning mid-cycle, which is
    ordinary; after a start it is two loops sharing a file, and then every
    duration in the report pairs one loop's start with the other's end.
    """

    INTERLEAVED = "\n".join([
        "===== 2026-09-01 01:00:00 JST cycle start =====",
        "loop A, with enough output written into the log that the thin-output rule does not fire on this cycle at all",
        "===== 2026-09-01 01:01:00 JST cycle start =====",
        "loop B, likewise carrying plenty of output written down so that nothing else in this report fires either",
        "===== 2026-09-01 01:05:00 JST cycle end rc=0 =====",
        "===== 2026-09-01 01:30:00 JST cycle end rc=0 =====",
    ])
    ROTATED = "\n".join([
        "===== 2026-09-01 00:30:00 JST cycle end rc=0 =====",
        "===== 2026-09-01 01:00:00 JST cycle start =====",
        "the rest of a perfectly ordinary cycle, carrying output well past the minimum so that only the check under test can fire",
        "===== 2026-09-01 01:20:00 JST cycle end rc=0 =====",
    ])

    def _orphans(self, text):
        orphans = []
        lg.split_cycles(text, "test.log", START, END, orphans)
        return orphans

    def test_an_orphan_end_is_collected_not_dropped(self):
        # Before 0.6.0 this line was discarded with no trace anywhere.
        orphans = self._orphans(self.INTERLEAVED)
        self.assertEqual(len(orphans), 1)
        self.assertTrue(orphans[0]["after_a_start"])

    def test_an_orphan_before_any_start_is_marked_as_such(self):
        orphans = self._orphans(self.ROTATED)
        self.assertEqual(len(orphans), 1)
        self.assertFalse(orphans[0]["after_a_start"])

    def test_a_clean_log_has_no_orphans(self):
        self.assertEqual(self._orphans(self.ROTATED.split("\n", 1)[1]), [])

    def test_interleaving_downgrades_the_killed_verdict(self):
        cycles = lg.split_cycles(self.INTERLEAVED, "test.log", START, END)
        lg.flag_abandoned(cycles, {"test.log"})
        first = cycles[0]
        self.assertFalse(first.abandoned)
        self.assertEqual(first.problems, [])
        self.assertTrue(any("may have finished fine" in n for n in first.notes))

    def test_without_interleaving_the_killed_verdict_stands(self):
        # The downgrade must not become a blanket excuse: a real hard kill in a
        # single-loop file is still a problem.
        cycles = lg.split_cycles(self.INTERLEAVED, "test.log", START, END)
        lg.flag_abandoned(cycles)
        self.assertTrue(cycles[0].abandoned)
        self.assertTrue(any("killed, not finished" in p for p in cycles[0].problems))

    def test_an_interleaved_file_does_not_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mixed.log")
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(self.INTERLEAVED)
            self.assertEqual(lg.main([path, "--timeout", "600"]), 1)

    def test_a_rotated_file_still_exits_zero(self):
        # The false alarm this check must not raise. Log rotation is normal.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rotated.log")
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(self.ROTATED)
            self.assertEqual(lg.main([path, "--timeout", "600"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DeclaredNextInterval(unittest.TestCase):
    """0.7.0. Reported by a reader of the article, against 0.3.0, and correct.

    Every deadline in check_staleness was derived from history, and history is
    what a dying loop stops producing. Two concrete holes:

      * a loop whose interval drifts upward on the way to dying carries a
        median that grew with it, so the threshold is loosest exactly when it
        matters most;
      * a loop that died on cycle two has no median at all, so this check -
        written to stop "no information" printing as "no problem" - was doing
        precisely that.

    A loop that picks its own cadence writes the next interval down before it
    exits. That number is intent recorded *before* the silence.
    """

    def _file(self, contents):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "next_minutes")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(contents)
        return p

    def test_a_plain_integer_of_minutes_is_read(self):
        self.assertEqual(lg.declared_interval_s(self._file("25\n")), 1500.0)
        self.assertEqual(lg.declared_interval_s(self._file(" 90 ")), 5400.0)

    def test_anything_not_a_positive_integer_is_ignored(self):
        # ⚠ None, never a guess. A guess here moves the deadline in the loose
        # direction with nothing printed - the shape of every bug in this file.
        for junk in ("", "  ", "0", "-5", "25 minutes", "twenty", "25\n30\n"):
            self.assertIsNone(lg.declared_interval_s(self._file(junk)),
                              f"parsed {junk!r} as an interval")
        self.assertIsNone(lg.declared_interval_s("/no/such/file"))
        self.assertIsNone(lg.declared_interval_s(None))

    def test_a_loop_that_died_on_its_second_cycle_is_now_reported(self):
        # The hole. Two starts is below MIN_STARTS_FOR_INTERVAL, so there is no
        # median and 0.3.0 returned None - silence, forever, on a dead loop.
        now = datetime.now()
        cycles = dated_cycles(now - timedelta(hours=9), now - timedelta(hours=8))
        self.assertIsNone(lg.median_interval_s(cycles))
        self.assertIsNone(lg.check_staleness(cycles, now))     # 0.6.0 behaviour
        msg = lg.check_staleness(cycles, now, declared_s=25 * 60)
        self.assertIsNotNone(msg)
        self.assertIn("may have stopped", msg)
        self.assertIn("declared its next start", msg)

    def test_a_drifting_interval_no_longer_loosens_its_own_deadline(self):
        # Gaps of 20, 40, 80 then 160 minutes - failure 3 talking the loop into
        # sleeping longer each time - and then silence for 90 minutes. The
        # median has grown to an hour, so 0.6.0 waits three hours before
        # complaining about a loop that said it would be back in twenty.
        now = datetime.now()
        ends = (390, 370, 330, 250, 90)     # minutes ago, oldest first
        cycles = dated_cycles(*[now - timedelta(minutes=m) for m in ends])
        self.assertEqual(lg.median_interval_s(cycles), 60 * 60)
        self.assertIsNone(lg.check_staleness(cycles, now))      # 0.6.0 behaviour
        self.assertIsNotNone(lg.check_staleness(cycles, now, declared_s=20 * 60))

    def test_an_explicit_stale_after_still_wins(self):
        # Precedence: what the operator typed beats what the loop claimed.
        now = datetime.now()
        cycles = dated_cycles(now - timedelta(hours=2))
        msg = lg.check_staleness(cycles, now, stale_after_s=3600,
                                 declared_s=24 * 3600)
        self.assertIsNotNone(msg)
        self.assertIn("--stale-after", msg)

    def test_a_healthy_loop_with_a_declared_interval_says_nothing(self):
        # The other direction. A check that cannot pass gets switched off.
        now = datetime.now()
        cycles = dated_cycles(now - timedelta(minutes=40), now - timedelta(minutes=10))
        self.assertIsNone(lg.check_staleness(cycles, now, declared_s=25 * 60))

    def test_the_cli_accepts_the_file(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "run.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(log((ago(days=1), "A" * 300, 0), (ago(minutes=1400), "A" * 300, 0)))
        out = io.StringIO()
        with redirect_stdout(out):
            lg.main([p, "--next-interval-file", self._file("25")])
        self.assertIn("may have stopped", out.getvalue())


class AKilledLastCycleIsNotStillRunning(unittest.TestCase):
    """0.7.0, second half of the same report.

    The last unfinished cycle in a log is exempt from every complaint, because
    it is usually the run calling loopguard. flag_abandoned lifts that only
    when a later cycle overtook it - which never happens if the loop died
    there. So a run killed by a watchdog read as in-progress forever. When the
    caller has said what the per-cycle ceiling is, that is enough to know.
    """

    def _unfinished(self, minutes_ago):
        started = datetime.now() - timedelta(minutes=minutes_ago)
        c = cycles_of(log((started.strftime("%Y-%m-%d %H:%M:%S"), "A" * 300, None)))[0]
        return c

    def test_an_unfinished_cycle_past_the_ceiling_is_called_killed(self):
        c = self._unfinished(minutes_ago=400)
        lg.judge(c, timeout_s=3 * 3600, now=datetime.now())
        self.assertTrue(c.problems, "a cycle killed six hours ago read as healthy")
        self.assertIn("it was killed, not still running", " ".join(c.problems))
        self.assertFalse(c.ok)

    def test_a_cycle_still_inside_the_ceiling_is_left_alone(self):
        # ⚠ The direction that matters more: this is usually the very run
        # calling loopguard, and complaining about it every single time is how
        # a check gets ignored.
        c = self._unfinished(minutes_ago=20)
        lg.judge(c, timeout_s=3 * 3600, now=datetime.now())
        self.assertEqual(c.problems, [])

    def test_without_a_ceiling_nothing_is_claimed(self):
        # No --timeout means nobody outside the run owns the clock, and
        # guessing one would be inventing the fact.
        c = self._unfinished(minutes_ago=4000)
        lg.judge(c, timeout_s=None, now=datetime.now())
        self.assertEqual(c.problems, [])
