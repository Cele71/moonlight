#!/usr/bin/env python3
"""Tests for loopguard. Standard library only.

    python3 -m unittest discover -s product/loopguard -v
    python3 product/loopguard/test_loopguard.py

Every case here comes from a real failure or a real line in the log this tool
was written against; the negation and unfinished-cycle cases in particular are
regression tests for bugs that shipped in 0.1.0.
"""

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
