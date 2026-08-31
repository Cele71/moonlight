#!/usr/bin/env python3
"""loopguard - health check for AI coding agents running on a cron loop.

Reads the log files produced by an unattended agent loop (Claude Code, or any
CLI agent invoked from cron) and reports, per cycle:

  * how long the cycle ran, and whether it was killed by a timeout
  * whether the provider refused the run (usage / rate limit)
  * whether the cycle produced any real output, or just spun
  * whether cycles are repeating themselves (stuck loop)

It exits non-zero when something needs attention, so it can itself be run
from cron and piped into a notification.

No dependencies beyond the standard library. Python 3.8+.

Usage:
    loopguard.py logs/                     # scan a directory of log files
    loopguard.py logs/2026-08-31.log       # a single file
    loopguard.py logs/ --json              # machine readable
    loopguard.py logs/ --since 3           # only the last 3 days of files

Exit codes:
    0  all cycles healthy
    1  at least one cycle needs attention
    2  could not read any log
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Iterable, Iterator

__version__ = "0.1.1"

# --- how a cycle is delimited in the log ------------------------------------
# The defaults match a loop that brackets each run with a start and end line,
# e.g.  ===== 2026-08-31 05:00:01 JST cycle start =====
# Override with --start-re / --end-re for a different harness.
DEFAULT_START_RE = r"=+\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[^=]*?(?:start|開始)"
DEFAULT_END_RE = r"=+\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[^=]*?(?:end|終了)[^=]*?rc=(?P<rc>-?\d+)"

TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")

# Phrases that mean "the provider stopped us", not "our code failed".
# Kept deliberately broad: a false positive here only widens the interval.
LIMIT_PATTERNS = [
    r"usage limit",
    r"rate limit",
    r"rate[_-]limited",
    r"\b429\b",
    r"quota (?:exceeded|reached)",
    r"too many requests",
    r"limit reached",
    r"resets? at",
    r"5-hour limit",
    r"weekly limit",
    r"overloaded_error",
    r"使用量.*(?:上限|制限)",
    r"上限に達",
]

# A log written by the agent itself contains sentences *about* limits as often
# as it contains real ones -- "no evidence of a usage limit" is a healthy cycle
# reporting that it is healthy. Matches sitting next to one of these are ignored
# (and reported as a note, so a real hit swallowed by this can still be found).
NEGATION_PATTERNS = [
    r"no evidence",
    r"\bno\b",
    r"\bnot\b",
    r"\bnone\b",
    r"\bnever\b",
    r"\bwithout\b",
    r"形跡(?:は)?(?:なし|無し)",
    r"(?:なし|無し)",
    r"ありません",
    r"ませんでした",
    r"当たって(?:い)?ない",
]

# How many characters either side of a match are inspected for a negation.
NEGATION_WINDOW = 24

AUTH_PATTERNS = [
    r"not (?:logged in|authenticated)",
    r"invalid api key",
    r"authentication[_ ]error",
    r"please run .{0,20}login",
    r"\b401\b",
]

# A cycle that emitted less than this many characters of agent output almost
# certainly did no work, whatever its exit code says.
#
# Where 80 comes from: on the loop this was written against -- which asks for a
# three-line summary at the end of every cycle -- the *thinnest* healthy cycle
# observed was 548 characters, and the range across a day was 548-915. 80 sits
# about seven times below the floor, so it fires only on a cycle that produced
# essentially nothing, never on a terse one. That margin is deliberate: a false
# "did nothing" is the expensive direction of error, because it makes a healthy
# loop look broken and invites you to change a prompt that was working.
#
# Tune it to your own loop rather than to this number: run once with --json,
# read output_chars across a week of healthy cycles, and set --min-output to
# roughly a quarter of the smallest. Keep it low for a loop that summarises in a
# character-dense language (Japanese, Chinese), where 200 characters is a full
# report; raise it for an English loop that is expected to write at length.
THIN_OUTPUT_CHARS = 80

# Below this, output is too short to compare meaningfully.
COMPARABLE_CHARS = 60

# Two cycles whose output is this similar are treated as the loop spinning.
REPEAT_SIMILARITY = 0.92


def _compile(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


LIMIT_RES = _compile(LIMIT_PATTERNS)
AUTH_RES = _compile(AUTH_PATTERNS)
NEGATION_RES = _compile(NEGATION_PATTERNS)


def _parse_ts(raw: str) -> datetime | None:
    raw = raw.strip()
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Cycle:
    """One invocation of the agent, as reconstructed from the log."""

    source: str
    started: datetime | None = None
    ended: datetime | None = None
    rc: int | None = None
    body: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unfinished: bool = False  # start marker seen, end marker never arrived

    @property
    def duration_s(self) -> float | None:
        if self.started and self.ended:
            return (self.ended - self.started).total_seconds()
        return None

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("body")
        d["started"] = self.started.isoformat() if self.started else None
        d["ended"] = self.ended.isoformat() if self.ended else None
        d["duration_s"] = self.duration_s
        d["output_chars"] = len(self.body.strip())
        d["ok"] = self.ok
        return d


def split_cycles(text: str, source: str, start_re: re.Pattern, end_re: re.Pattern) -> list[Cycle]:
    """Carve a log file into cycles.

    A cycle runs from a start marker to the next end marker. A start with no
    end means the run is either still going or died without writing its
    footer -- both worth reporting, so it is kept.
    """
    cycles: list[Cycle] = []
    current: Cycle | None = None
    buf: list[str] = []

    for line in text.splitlines():
        m_start = start_re.search(line)
        if m_start:
            if current is not None:
                current.body = "\n".join(buf)
                cycles.append(current)
            current = Cycle(source=source, started=_parse_ts(m_start.group("ts")))
            buf = []
            continue

        m_end = end_re.search(line)
        if m_end and current is not None:
            current.ended = _parse_ts(m_end.group("ts"))
            try:
                current.rc = int(m_end.group("rc"))
            except (IndexError, ValueError):
                current.rc = None
            current.body = "\n".join(buf)
            cycles.append(current)
            current = None
            buf = []
            continue

        if current is not None:
            buf.append(line)

    if current is not None:
        current.body = "\n".join(buf)
        current.unfinished = True
        current.notes.append("no end marker: still running, or died without writing its footer")
        cycles.append(current)

    return cycles


def _negated(text: str, start: int, end: int) -> bool:
    """True if a negation sits close enough to this match to invert it."""
    window = text[max(0, start - NEGATION_WINDOW):end + NEGATION_WINDOW]
    return any(r.search(window) for r in NEGATION_RES)


def _first_match(res: list[re.Pattern], text: str) -> tuple[str | None, int]:
    """First match not sitting in a negation, plus a count of the ones skipped."""
    skipped = 0
    for r in res:
        for m in r.finditer(text):
            if _negated(text, m.start(), m.end()):
                skipped += 1
                continue
            return m.group(0), skipped
    return None, skipped


def judge(cycle: Cycle, timeout_s: int | None, min_output: int = THIN_OUTPUT_CHARS) -> None:
    """Fill in cycle.problems / cycle.notes. Ordered most severe first."""
    body = cycle.body

    hit, skipped = _first_match(AUTH_RES, body)
    if hit:
        cycle.problems.append(f"authentication failed ({hit!r}) - the loop cannot run until a human logs in")
    elif skipped:
        cycle.notes.append(f"{skipped} auth-like phrase(s) ignored as negated - check by hand if in doubt")

    hit, skipped = _first_match(LIMIT_RES, body)
    if hit:
        cycle.problems.append(f"provider limit hit ({hit!r}) - widen the interval")
    elif skipped:
        cycle.notes.append(f"{skipped} limit-like phrase(s) ignored as negated (e.g. \"no usage limit\") - check by hand if in doubt")

    # 124 is GNU timeout's "killed after the limit"; 137 is SIGKILL (its -k).
    if cycle.rc in (124, 137):
        cycle.problems.append(f"killed by timeout (rc={cycle.rc}) - the cycle needs longer, or less to do")
    elif cycle.rc not in (None, 0):
        cycle.problems.append(f"non-zero exit (rc={cycle.rc})")

    if len(body.strip()) < min_output:
        msg = f"almost no output ({len(body.strip())} chars) - the cycle probably did nothing"
        if cycle.unfinished:
            # The cycle is very likely still running -- and if loopguard is being
            # run *by* that cycle, its own output has not been written yet.
            cycle.notes.append("output not written yet (cycle still in progress)")
        else:
            cycle.problems.append(msg)

    d = cycle.duration_s
    if d is not None:
        if timeout_s and d >= timeout_s * 0.9 and cycle.rc == 0:
            cycle.notes.append(f"ran {d/60:.0f} min, close to the {timeout_s/60:.0f} min limit")
        if d < 30 and cycle.rc == 0:
            cycle.notes.append(f"finished in {d:.0f}s - suspiciously fast for a full cycle")


def flag_repeats(cycles: list[Cycle]) -> None:
    """Mark cycles whose output is nearly identical to the previous one."""
    for prev, cur in zip(cycles, cycles[1:]):
        a, b = prev.body.strip(), cur.body.strip()
        if prev.unfinished or cur.unfinished:
            continue
        if len(a) < COMPARABLE_CHARS or len(b) < COMPARABLE_CHARS:
            continue  # too short to compare; thinness is reported separately
        ratio = difflib.SequenceMatcher(None, a[-4000:], b[-4000:]).quick_ratio()
        if ratio >= REPEAT_SIMILARITY:
            cur.problems.append(
                f"output {ratio:.0%} identical to the previous cycle - the loop may be stuck"
            )


def suggest_interval(cycles: list[Cycle], current_per_day: int | None) -> str | None:
    """Recommend a cron frequency based on what the log actually shows."""
    if not cycles:
        return None
    recent = [c for c in cycles if not c.unfinished][-8:]
    if not recent:
        return None
    limited = sum(1 for c in recent if any("provider limit" in p for p in c.problems))
    thin = sum(1 for c in recent if any("almost no output" in p for p in c.problems))

    if limited:
        share = limited / len(recent)
        if share >= 0.5:
            return f"{limited}/{len(recent)} recent cycles hit a provider limit - halve the frequency"
        return f"{limited}/{len(recent)} recent cycles hit a provider limit - drop one run per day"
    if thin >= len(recent) * 0.5:
        return (
            f"{thin}/{len(recent)} recent cycles produced almost nothing - the prompt, not the "
            "interval, is likely the problem"
        )
    if current_per_day and current_per_day < 6:
        return "no limits hit recently - there is room to run more often"
    return None


def collect_logs(paths: list[str], since_days: int | None) -> list[tuple[str, str]]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(
                os.path.join(p, n)
                for n in sorted(os.listdir(p))
                if n.endswith((".log", ".txt")) and os.path.isfile(os.path.join(p, n))
            )
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"loopguard: no such file or directory: {p}", file=sys.stderr)

    if since_days is not None:
        files = files[-since_days:]

    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                out.append((f, fh.read()))
        except OSError as e:
            print(f"loopguard: cannot read {f}: {e}", file=sys.stderr)
    return out


def render(cycles: list[Cycle], advice: str | None) -> str:
    lines = []
    bad = [c for c in cycles if not c.ok]
    lines.append(f"loopguard {__version__}: {len(cycles)} cycle(s), {len(bad)} needing attention")
    lines.append("")

    for i, c in enumerate(cycles, 1):
        when = c.started.strftime("%Y-%m-%d %H:%M") if c.started else "unknown time"
        dur = f"{c.duration_s/60:.0f}m" if c.duration_s is not None else "?"
        mark = ".. " if c.unfinished and c.ok else ("ok " if c.ok else "!! ")
        lines.append(f"{mark}[{i}] {when}  {dur}  rc={c.rc if c.rc is not None else '?'}  ({os.path.basename(c.source)})")
        for p in c.problems:
            lines.append(f"      - {p}")
        for n in c.notes:
            lines.append(f"      . {n}")

    if advice:
        lines.append("")
        lines.append(f"suggestion: {advice}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="loopguard",
        description="Health check for an AI coding agent running on a cron loop.",
    )
    ap.add_argument("paths", nargs="+", help="log file(s) or a directory of them")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--since", type=int, metavar="N", help="only the last N log files")
    ap.add_argument("--timeout", type=int, metavar="SECONDS",
                    help="the per-cycle timeout the loop uses, so near-misses can be flagged")
    ap.add_argument("--per-day", type=int, metavar="N", help="how many times a day the loop currently runs")
    ap.add_argument("--min-output", type=int, default=THIN_OUTPUT_CHARS, metavar="CHARS",
                    help=f"below this many characters a cycle counts as having done nothing (default {THIN_OUTPUT_CHARS})")
    ap.add_argument("--start-re", default=DEFAULT_START_RE, help="regex for a cycle start line (needs a 'ts' group)")
    ap.add_argument("--end-re", default=DEFAULT_END_RE, help="regex for a cycle end line (needs 'ts', optionally 'rc')")
    ap.add_argument("--version", action="version", version=f"loopguard {__version__}")
    args = ap.parse_args(argv)

    try:
        start_re = re.compile(args.start_re, re.IGNORECASE)
        end_re = re.compile(args.end_re, re.IGNORECASE)
    except re.error as e:
        print(f"loopguard: bad regex: {e}", file=sys.stderr)
        return 2

    logs = collect_logs(args.paths, args.since)
    if not logs:
        print("loopguard: no logs read", file=sys.stderr)
        return 2

    cycles: list[Cycle] = []
    for name, text in logs:
        cycles.extend(split_cycles(text, name, start_re, end_re))

    if not cycles:
        print(
            "loopguard: no cycles found. If your loop marks runs differently, "
            "pass --start-re / --end-re.",
            file=sys.stderr,
        )
        return 2

    for c in cycles:
        judge(c, args.timeout, args.min_output)
    flag_repeats(cycles)
    advice = suggest_interval(cycles, args.per_day)

    if args.json:
        print(json.dumps(
            {"version": __version__, "cycles": [c.as_dict() for c in cycles], "suggestion": advice},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render(cycles, advice))

    return 1 if any(not c.ok for c in cycles) else 0


if __name__ == "__main__":
    sys.exit(main())
