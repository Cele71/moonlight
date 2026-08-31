#!/usr/bin/env python3
"""loopguard - health check for AI coding agents running on a cron loop.

Reads the log files produced by an unattended agent loop (Claude Code, or any
CLI agent invoked from cron) and reports, per cycle:

  * how long the cycle ran, and whether it was killed by a timeout
  * whether the provider refused the run (usage / rate limit)
  * whether the cycle produced any real output, or just spun
  * whether cycles are repeating themselves (stuck loop)
  * whether the loop has stopped running at all (no cycle for far too long)

It exits non-zero when something needs attention, so it can itself be run
from cron and piped into a notification.

No dependencies beyond the standard library. Python 3.8+.

Usage:
    loopguard.py logs/                     # scan a directory of log files
    loopguard.py logs/2026-08-31.log       # a single file
    loopguard.py logs/ --json              # machine readable
    loopguard.py logs/ --since 3           # only cycles from the last 3 days

When a log yields no cycles -- because the loop brackets its runs differently
from the default -- loopguard says so per file and prints a --start-re/--end-re
guessed from the log's own lines, rather than silently reporting on whatever
else it could read. It also still answers what it can without the markers: when
the log last had anything written to it, and whether the provider's or the
login's vocabulary appears in it. The checks that need cycles are named as not
run, so a short report cannot be mistaken for a clean one.

The loop stopping is the failure this tool exists to catch, and it is the
one that leaves no evidence: a loop that no longer runs writes no cycle, so
every cycle on record still reads "ok". loopguard therefore also judges the
silence after the last cycle, against the loop's own median interval.

Exit codes:
    0  all cycles healthy
    1  at least one cycle needs attention, or the loop appears to have stopped,
       or a file that could not be parsed still had something to report
    2  nothing could be judged -- no log read, or no cycle markers matched and
       the markerless checks found nothing. Deliberately not 0: the cycle-level
       checks did not run, and "not seen" is not "not there"
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Iterator

__version__ = "0.4.0"

# --- how a cycle is delimited in the log ------------------------------------
# The defaults match a loop that brackets each run with a start and end line,
# e.g.  ===== 2026-08-31 05:00:01 JST cycle start =====
# Override with --start-re / --end-re for a different harness.
# What a timestamp is allowed to look like. Until 0.4.0 this was exactly two
# shapes -- `2026-08-31 05:00:01` and the same with a T -- which is the shape
# this loop's own wrapper happens to write. Every other logger was unreadable,
# and an unreadable timestamp does not degrade gracefully here: it takes the
# start time away, and a cycle with no start time cannot be dated, ordered, or
# checked for silence. So the accepted set is now the ones loggers actually
# emit: `/` for `-`, fractional seconds, and a trailing offset.
TS_DATE = r"\d{4}[-/]\d{2}[-/]\d{2}"
TS_CLOCK = r"\d{2}:\d{2}:\d{2}"
# The offset is captured and applied, not discarded: a container logging in UTC,
# read on a JST laptop, must not look nine hours stale. A bare zone *name*
# (`JST`, `UTC`) is still ignored, because a name is not an offset without a
# table this tool has no business carrying.
TS_TAIL = r"(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?"
TS_REGEX = TS_DATE + r"[ T]" + TS_CLOCK + TS_TAIL

DEFAULT_START_RE = r"=+\s*(?P<ts>" + TS_REGEX + r")[^=]*?(?:start|開始)"
DEFAULT_END_RE = r"=+\s*(?P<ts>" + TS_REGEX + r")[^=]*?(?:end|終了)[^=]*?rc=(?P<rc>-?\d+)"

TS_PARSE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:[.,]\d{1,9})?(?P<off>Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE)

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

# --- deciding that the loop itself has stopped ------------------------------
# A stopped loop is invisible in a log: the last cycle it did run is still a
# healthy one. So the silence *after* the last cycle is judged too, against the
# loop's own median interval rather than a number picked here -- a loop that
# runs every 15 minutes and one that runs twice a day cannot share a threshold.
STALE_MULTIPLIER = 3
# ...but never call a loop stopped sooner than this. One missed run on a fast
# loop is normal; being woken for it is not.
MIN_STALE_S = 3600
# Fewer starts than this and there is no "usual interval" to compare against,
# so loopguard says nothing rather than guessing from a single gap.
MIN_STARTS_FOR_INTERVAL = 3

# --- guessing a marker for a log loopguard cannot read ----------------------
# Used only when a file yields no cycles. The point is to hand back a command
# line that works, not to parse the log: a wrong guess costs the reader one
# edit, whereas saying nothing costs them the tool.
TS_PATTERN = r"(?P<ts>" + TS_REGEX + r")"
# The broad pattern above is for commands the reader copies; this short one is
# for the "e.g." lines, which the reader has to actually read.
TS_PATTERN_HINT = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
TS_ANY_RE = re.compile(TS_REGEX)

# Ordered: the first word that appears in a file is the one suggested.
START_WORDS = ["cycle start", "run start", "start", "starting", "begin", "beginning",
               "launch", "invoking", "開始"]
END_WORDS = ["cycle end", "run end", "end", "ended", "finish", "finished", "done",
             "complete", "completed", "exit", "終了"]

# How a return code tends to be written next to an end marker.
RC_KEYS = ["rc", "exit", "exit_code", "exitcode", "status", "code"]
RC_VALUE = r"[=: ]\s*(?P<rc>-?\d+)"


def _compile(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


LIMIT_RES = _compile(LIMIT_PATTERNS)
AUTH_RES = _compile(AUTH_PATTERNS)
NEGATION_RES = _compile(NEGATION_PATTERNS)


def _offset(raw: str) -> timedelta:
    if raw.upper() == "Z":
        return timedelta(0)
    digits = raw[1:].replace(":", "")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return delta if raw[0] == "+" else -delta


def _parse_ts(raw: str) -> datetime | None:
    """A naive local datetime, or None if this is not a timestamp we can read.

    Everything downstream compares against `datetime.now()`, which is naive
    local, so a stamp carrying an offset is converted rather than trusted as
    wall clock. Without that, a UTC log read at JST reports six hours of
    silence that never happened.
    """
    m = TS_PARSE_RE.match(raw.strip().replace("/", "-"))
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m['date']} {m['clock']}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None  # matched the shape but is not a date, e.g. 2026-13-45
    if m["off"]:
        dt = dt.replace(tzinfo=timezone(_offset(m["off"]))).astimezone().replace(tzinfo=None)
    return dt


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
    abandoned: bool = False   # unfinished, and a later cycle proves it is not still running

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
    footer -- both worth reporting, so it is kept and marked `unfinished`.

    Until 0.3.0 only the *last* cycle in a file got that mark. A cycle
    interrupted by the next start -- the shape a hard kill leaves -- was
    appended with no end, no rc and no flag, and printed as `ok ... ? rc=?`.
    The tool said "I cannot tell what happened here" and counted it healthy.
    """
    cycles: list[Cycle] = []
    current: Cycle | None = None
    buf: list[str] = []

    for line in text.splitlines():
        m_start = start_re.search(line)
        if m_start:
            if current is not None:
                current.body = "\n".join(buf)
                current.unfinished = True
                current.notes.append(
                    "no end marker: the next cycle started before this one finished")
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
        if cycle.unfinished and not cycle.abandoned:
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


def _dur(seconds: float) -> str:
    """A duration a human reads at a glance: 35m, 4h 12m, 9d 13h."""
    s = int(round(seconds))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600:02d}h"


def median_interval_s(cycles: list[Cycle]) -> float | None:
    """The loop's usual gap between starts, or None if there are too few."""
    starts = sorted(c.started for c in cycles if c.started)
    if len(starts) < MIN_STARTS_FOR_INTERVAL:
        return None
    gaps = sorted((b - a).total_seconds() for a, b in zip(starts, starts[1:]))
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2


def check_staleness(cycles: list[Cycle], now: datetime,
                    stale_after_s: int | None = None) -> str | None:
    """One line if the loop looks stopped, else None.

    This is the only check that judges something the log does not contain.
    Every cycle in the file can be healthy while the loop has been dead for a
    week: the last run to happen wrote a clean footer and then nothing ever
    wrote again. Reporting "0 needing attention" there is the tool failing at
    its one job, so the gap between the last start and now is a finding.
    """
    starts = [c.started for c in cycles if c.started]
    if not starts:
        return None
    last = max(starts)
    silence = (now - last).total_seconds()
    if silence < 0:
        return None  # log is ahead of the clock; not something to guess about

    if stale_after_s is None:
        median = median_interval_s(cycles)
        if median is None:
            return None  # no idea what normal is for this loop
        limit = max(median * STALE_MULTIPLIER, MIN_STALE_S)
        because = f"it had been starting about every {_dur(median)}"
    else:
        if stale_after_s <= 0:
            return None  # explicitly switched off
        limit = stale_after_s
        because = f"--stale-after {_dur(stale_after_s)}"

    if silence <= limit:
        return None
    return (f"no cycle has started for {_dur(silence)} - the loop may have stopped "
            f"(last start {last:%Y-%m-%d %H:%M}; {because})")


def flag_abandoned(cycles: list[Cycle]) -> None:
    """Mark cycles that never wrote an end marker *and* were overtaken.

    An unfinished cycle at the end of the log is usually the one running right
    now -- often the very cycle calling loopguard. An unfinished cycle with a
    later start after it is not running: something killed it hard enough that
    it never wrote its own footer, and nothing said so at the time.
    """
    starts = [c.started for c in cycles if c.started]
    for c in cycles:
        if not c.unfinished or c.started is None:
            continue
        later = [t for t in starts if t > c.started]
        if not later:
            continue
        c.abandoned = True
        c.problems.append(
            f"started but never wrote an end marker, and the next cycle began at "
            f"{min(later):%Y-%m-%d %H:%M} - this run was killed, not finished"
        )


def filter_since(cycles: list[Cycle], days: int, today: date) -> tuple[list[Cycle], int, int]:
    """Keep cycles started within the last `days` calendar days (today is day 1).

    Counted in cycles, not files: one log file can hold a month, and a
    directory can hold files whose names say nothing about their dates.
    """
    cutoff = today - timedelta(days=days - 1)
    kept, dropped, undated = [], 0, 0
    for c in cycles:
        if c.started is None:
            undated += 1
            kept.append(c)  # cannot be dated, so it is never silently dropped
        elif c.started.date() >= cutoff:
            kept.append(c)
        else:
            dropped += 1
    return kept, dropped, undated


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


def _as_regex(word: str) -> str:
    """A regex fragment matching `word`, word-bounded where that means anything."""
    escaped = re.escape(word)
    if word[0].isalnum() and word[-1].isalnum() and word.isascii():
        return r"\b" + escaped + r"\b"
    return escaped


def _word_hits(lines: list[str], words: list[str]) -> str | None:
    """The first word from `words` present as a whole word on these lines.

    Whole-word, not substring: a log saying "finished" must not be handed a
    suggestion built around "finish", because the \\b in the generated regex
    would then match nothing and the advice would be worse than silence.
    """
    joined = "\n".join(lines).lower()
    for w in words:
        if re.search(_as_regex(w), joined, re.IGNORECASE):
            return w
    return None


def guess_markers(text: str) -> dict:
    """Look at one unreadable log and guess how it delimits a run.

    Returns what was found -- timestamped lines, a candidate start word, a
    candidate end word, how the return code is written -- plus example lines,
    so the caller can print something the reader can act on.
    """
    stamped = [ln for ln in text.splitlines() if TS_ANY_RE.search(ln)]
    out: dict = {
        "stamped_lines": len(stamped),
        "start_word": None,
        "end_word": None,
        "rc_key": None,
        "start_example": None,
        "end_example": None,
    }
    if not stamped:
        return out

    end_word = _word_hits(stamped, END_WORDS)
    # A start word is only meaningful on lines that are not end lines, otherwise
    # "start" inside "restart complete" wins over the real marker.
    not_end = stamped
    if end_word:
        end_pat = re.compile(_as_regex(end_word), re.IGNORECASE)
        not_end = [ln for ln in stamped if not end_pat.search(ln)] or stamped
    start_word = _word_hits(not_end, START_WORDS)

    out["start_word"] = start_word
    out["end_word"] = end_word
    if start_word:
        start_pat = re.compile(_as_regex(start_word), re.IGNORECASE)
        out["start_example"] = next((ln for ln in not_end if start_pat.search(ln)), None)
    if end_word:
        end_lines = [ln for ln in stamped if end_pat.search(ln)]
        out["end_example"] = end_lines[0] if end_lines else None
        for key in RC_KEYS:
            if re.search(_as_regex(key) + RC_VALUE, "\n".join(end_lines), re.IGNORECASE):
                out["rc_key"] = key
                break
    return out


def _try_markers(text: str, name: str, start_re: str, end_re: str) -> list["Cycle"]:
    """Split `text` with a guessed pair of markers. [] if the guess is unusable."""
    try:
        return split_cycles(text, name, re.compile(start_re, re.IGNORECASE),
                            re.compile(end_re, re.IGNORECASE))
    except re.error:
        return []


def explain_unread(name: str, text: str, scan: "FlatScan | None" = None) -> list[str]:
    """Lines explaining why one file produced no cycles, and what to pass.

    `scan` is what could still be read out of the file without markers. It is
    printed first: a file nobody can parse may still be the file that says the
    provider cut the loop off, and that should not wait behind a regex lesson.
    """
    g = guess_markers(text)
    lines = [f"  {os.path.basename(name)}: no cycles matched."]
    for f in (scan.findings if scan else []):
        lines.append(f"      !! {f}")

    if not g["stamped_lines"]:
        lines.append("      no line carries a timestamp loopguard can read")
        lines.append("      (it expects YYYY-MM-DD HH:MM:SS, or the same with a T).")
        lines.append("      every cycle needs a start time, so --start-re must contain a")
        lines.append("      'ts' group matching whatever format this log uses.")
        return lines

    lines.append(f"      {g['stamped_lines']} line(s) carry a timestamp, "
                 f"but none matched the start/end markers.")
    for label, key in (("a start", "start_example"), ("an end", "end_example")):
        if g[key]:
            lines.append(f"      looks like {label}: {g[key].strip()[:100]}")

    if not (g["start_word"] and g["end_word"]):
        lines.append("      could not guess the markers - pass --start-re / --end-re yourself;")
        lines.append(f"      both need a 'ts' group, e.g. --start-re '{TS_PATTERN_HINT}.*MY-START'")
        return lines

    start_re = f"{TS_PATTERN}.*{_as_regex(g['start_word'])}"
    end_re = f"{TS_PATTERN}.*{_as_regex(g['end_word'])}"
    if g["rc_key"]:
        end_re += f".*?{_as_regex(g['rc_key'])}{RC_VALUE}"
    # Run the guess against the log before printing it. Advice that does not
    # work is worse than no advice: it sends the reader off to debug a regex
    # this tool wrote, inside a tool they have not decided to trust yet.
    found = _try_markers(text, name, start_re, end_re)
    if not found:
        lines.append("      a guess was built from those lines but still matched nothing -")
        lines.append("      pass --start-re / --end-re yourself; both need a 'ts' group, e.g.")
        lines.append(f"      --start-re '{TS_PATTERN_HINT}.*MY-START'")
        return lines

    complete = sum(1 for c in found if not c.unfinished)
    lines.append(f"      this reads it as {len(found)} cycle(s)"
                 + (f", {complete} of them complete:" if complete != len(found) else ":"))
    lines.append(f"        loopguard {name} \\")
    lines.append(f"          --start-re '{start_re}' \\")
    lines.append(f"          --end-re '{end_re}'")
    if not g["rc_key"]:
        lines.append("      (no return code found on the end line - exit codes will show as '?')")
    return lines


# --- what can be said about a log with no cycle markers ---------------------
# The check this tool exists for -- "has the loop stopped?" -- does not actually
# need cycles. It needs the time of the last thing that happened. Until 0.4.0 a
# log loopguard could not carve into cycles was answered with a guessed regex
# and exit 2, which is the tool declining to answer its own question because the
# log was not bracketed the way this loop happens to bracket its own.
#
# So: read what is readable, say exactly which checks did not run, and never let
# "no markers" render as a clean report.


@dataclass
class FlatScan:
    """A log read without cycle markers: only what the raw lines support."""

    source: str
    stamped_lines: int = 0
    last_activity: datetime | None = None
    silence_s: float | None = None
    stale: str | None = None          # set only when silence could be judged
    limit: str | None = None
    auth: str | None = None

    @property
    def findings(self) -> list[str]:
        return [f for f in (self.stale, self.limit, self.auth) if f]

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_activity"] = self.last_activity.isoformat() if self.last_activity else None
        d["findings"] = self.findings
        return d


def flat_scan(name: str, text: str, now: datetime,
              stale_after_s: int | None = None) -> FlatScan:
    """Judge a markerless log on the two things raw lines can support.

    Deliberately narrow. Duration, timeout, thin output and repetition are all
    per-cycle and there are no cycles here, so they are not guessed at -- they
    are reported as not run.
    """
    scan = FlatScan(source=name)
    stamps = []
    for line in text.splitlines():
        m = TS_ANY_RE.search(line)
        if not m:
            continue
        scan.stamped_lines += 1
        ts = _parse_ts(m.group(0))
        if ts:
            stamps.append(ts)

    if stamps:
        scan.last_activity = max(stamps)
        silence = (now - scan.last_activity).total_seconds()
        if silence >= 0:
            scan.silence_s = silence
            # Without cycles there is no median interval, so there is no
            # honest default here: three hours of quiet is a dead loop for one
            # schedule and mid-run for another. Judged only when told what
            # "too long" means for this loop.
            if stale_after_s and silence > stale_after_s:
                scan.stale = (f"nothing has been logged for {_dur(silence)} - the loop may have "
                              f"stopped (last line {scan.last_activity:%Y-%m-%d %H:%M}; "
                              f"--stale-after {_dur(stale_after_s)})")

    hit, _ = _first_match(LIMIT_RES, text)
    if hit:
        scan.limit = f"provider limit hit ({hit!r}) somewhere in this file - widen the interval"
    hit, _ = _first_match(AUTH_RES, text)
    if hit:
        scan.auth = f"authentication failed ({hit!r}) somewhere in this file - a human has to log in"
    return scan


def render_flat(scans: list[FlatScan], stale_after_s: int | None) -> str:
    """The report for a run where no file produced a single cycle."""
    lines = [f"loopguard {__version__}: no cycles could be read.",
             "Without start/end markers, only these checks can run:", ""]
    for scan in scans:
        lines.append(f"  {os.path.basename(scan.source)}")
        if scan.last_activity is None:
            lines.append("      ?  no readable timestamp - not even the time of the last line")
            lines.append("         is known, so nothing here can be judged")
        else:
            lines.append(f"      .  last line at {scan.last_activity:%Y-%m-%d %H:%M}"
                         + (f", {_dur(scan.silence_s)} ago" if scan.silence_s is not None else ""))
            if scan.stale:
                lines.append(f"      !! {scan.stale}")
            elif scan.silence_s is None:
                lines.append("      ?  that is in the future on this clock (a timezone the log")
                lines.append("         names but does not offset?), so the quiet since it")
                lines.append("         was not measured")
            elif stale_after_s:
                lines.append(f"      .  within --stale-after {_dur(stale_after_s)}, so not called stopped")
            else:
                lines.append("      ?  whether that silence is too long cannot be judged without")
                lines.append("         knowing how often this loop runs - pass --stale-after MINUTES")
        for f in (scan.limit, scan.auth):
            if f:
                lines.append(f"      !! {f}")
        lines.append("")
    lines.append("   not run: cycle duration, timeout kills, thin output, repeated cycles.")
    lines.append("   All four need start/end markers. This is not a clean bill of health -")
    lines.append("   it is a shorter list of questions. See the guessed --start-re above.")
    return "\n".join(lines)


def collect_logs(paths: list[str]) -> list[tuple[str, str]]:
    """Every log named, read in name order. Nothing is dropped here.

    Until 0.3.0 this took a --since count and returned `files[-N:]`, which was
    the wrong unit twice over: the flag is in days, and sorted-by-name puts
    `watcher.log` after `2026-08-31.log`. Selecting by date is now done on
    cycles, after they have been parsed, where a date actually exists.
    """
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

    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                out.append((f, fh.read()))
        except OSError as e:
            print(f"loopguard: cannot read {f}: {e}", file=sys.stderr)
    return out


def render(cycles: list[Cycle], advice: str | None, stale: str | None = None,
           scans: list["FlatScan"] | None = None) -> str:
    lines = []
    bad = [c for c in cycles if not c.ok]
    unreadable = [s for s in (scans or []) if s.findings]
    header = f"loopguard {__version__}: {len(cycles)} cycle(s), {len(bad)} needing attention"
    if unreadable:
        header += f", plus {len(unreadable)} file(s) with no cycles but something to say"
    lines.append(header)
    lines.append("")
    for scan in unreadable:
        # Above the cycle list for the same reason `stale` is: every cycle
        # below can read `ok` while a file nobody could parse is the one saying
        # the provider cut the loop off. 0.4.0 fixed this counting as healthy.
        for f in scan.findings:
            lines.append(f"!! {os.path.basename(scan.source)} (no cycles read): {f}")
        lines.append("")
    if stale:
        # Above the per-cycle list on purpose: every line below it can say "ok"
        # and still describe a loop that has not run since Tuesday.
        lines.append(f"!! {stale}")
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
    ap.add_argument("--since", type=int, metavar="DAYS",
                    help="only cycles started in the last DAYS days (1 = today only)")
    ap.add_argument("--timeout", type=int, metavar="SECONDS",
                    help="the per-cycle timeout the loop uses, so near-misses can be flagged")
    ap.add_argument("--per-day", type=int, metavar="N", help="how many times a day the loop currently runs")
    ap.add_argument("--stale-after", type=int, metavar="MINUTES",
                    help="report the loop as stopped after this much silence "
                         f"(default: {STALE_MULTIPLIER}x its own median interval, "
                         f"at least {MIN_STALE_S // 60} min; 0 disables). Required "
                         "to judge silence in a log with no cycle markers, where "
                         "there is no median interval to derive it from")
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

    if args.since is not None and args.since < 1:
        print("loopguard: --since counts days and starts at 1 (1 = today only)", file=sys.stderr)
        return 2

    logs = collect_logs(args.paths)
    if not logs:
        print("loopguard: no logs read", file=sys.stderr)
        return 2

    now = datetime.now()
    stale_after_s = args.stale_after * 60 if args.stale_after is not None else None

    cycles: list[Cycle] = []
    unread: list[tuple[str, str]] = []
    for name, text in logs:
        found = split_cycles(text, name, start_re, end_re)
        if found:
            cycles.extend(found)
        else:
            unread.append((name, text))

    scans = [flat_scan(name, text, now, stale_after_s) for name, text in unread]

    # A file that yields nothing is the failure most likely to pass unnoticed:
    # the report still says "all healthy", it is just quietly missing a day.
    # So it is always announced, even when the other files read fine.
    if unread:
        custom = args.start_re != DEFAULT_START_RE or args.end_re != DEFAULT_END_RE
        print(
            f"loopguard: {len(unread)} of {len(logs)} file(s) produced no cycles"
            + (" with the markers given:" if custom else ":"),
            file=sys.stderr,
        )
        for (name, text), scan in zip(unread, scans):
            # If no file parsed at all, the flat report prints these findings
            # as the main body; repeating them here just doubles the noise.
            for line in explain_unread(name, text, None if not cycles else scan):
                print(line, file=sys.stderr)
        print("", file=sys.stderr)

    if not cycles:
        # Not "could not read any log" any more. The question this tool is for
        # -- is the loop still running? -- is answerable from the last line's
        # timestamp, and refusing to answer it because the brackets were the
        # wrong shape was the tool failing the same way it fails in chapter 5.
        if args.json:
            print(json.dumps(
                {
                    "version": __version__,
                    "cycles": [],
                    "flat": [s.as_dict() for s in scans],
                    "files_without_cycles": [n for n, _ in unread],
                },
                ensure_ascii=False, indent=2,
            ))
        else:
            print(render_flat(scans, stale_after_s))
        return 1 if any(s.findings for s in scans) else 2

    cycles.sort(key=lambda c: (c.started is None, c.started or datetime.min))

    # Staleness is measured against every cycle on record, before --since
    # narrows the view: "nothing has run for four days" is the same fact
    # whether or not you asked to look at today.
    stale = check_staleness(cycles, now, stale_after_s)

    excluded = 0
    if args.since is not None:
        before = len(cycles)
        cutoff = date.today() - timedelta(days=args.since - 1)
        cycles, excluded, undated = filter_since(cycles, args.since, date.today())
        print(f"loopguard: --since {args.since} = cycles started on or after {cutoff} "
              f"({len(cycles)} of {before} kept, {excluded} older excluded)", file=sys.stderr)
        if undated:
            print(f"loopguard: {undated} cycle(s) carry no readable start time and were kept",
                  file=sys.stderr)
        if not cycles:
            # Not an empty result: the loop did not run in the window asked about.
            print(f"loopguard: no cycle started in the last {args.since} day(s)", file=sys.stderr)
            if stale:
                print(f"loopguard: {stale}", file=sys.stderr)
            return 1

    flag_abandoned(cycles)
    for c in cycles:
        judge(c, args.timeout, args.min_output)
    flag_repeats(cycles)
    advice = suggest_interval(cycles, args.per_day)

    if args.json:
        print(json.dumps(
            {
                "version": __version__,
                "cycles": [c.as_dict() for c in cycles],
                "suggestion": advice,
                "stale": stale,
                "files_without_cycles": [n for n, _ in unread],
                "flat": [s.as_dict() for s in scans],
                "cycles_excluded_by_since": excluded,
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render(cycles, advice, stale, scans))

    return 1 if (stale or any(not c.ok for c in cycles)
                 or any(s.findings for s in scans)) else 0


if __name__ == "__main__":
    sys.exit(main())
