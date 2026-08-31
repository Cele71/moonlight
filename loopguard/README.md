# loopguard

Health check for an AI coding agent running on a cron loop.

*Claude (Anthropic) wrote this, running unattended on the loop it watches. Details at the bottom.*

You put Claude Code (or any CLI agent) on a schedule, point it at a prompt, and
walk away. Then the honest question is: **is it still doing anything?**

> **Which `loopguard` is this?** There are, as of 2026-09-01, thirty-six
> repositories on GitHub with this name, and the name on PyPI belongs to a
> different project. **This one is not a decorator and does not run inside your
> agent.** It is a post-mortem reader: you point it at the log after the fact and
> it tells you what the loop did, including that it stopped. Nothing to import,
> nothing to wrap. If you want something that interrupts a repeating call while
> it happens, you want one of the others — this reads the wreckage.

The log tells you, but only if you read all of it. `loopguard` reads it for you
and reports the ways an unattended loop actually fails:

| failure | how it looks in the log | what loopguard says |
| --- | --- | --- |
| the provider stopped you | `usage limit reached`, `429`, `resets at ...` | `provider limit hit - widen the interval` |
| the session lost its login | `invalid api key`, `401`, `not authenticated` | `authentication failed - the loop cannot run until a human logs in` |
| the cycle ran out of time | exit code `124` / `137` from `timeout` | `killed by timeout - the cycle needs longer, or less to do` |
| the cycle did nothing | a few bytes between the start and end markers | `almost no output - the cycle probably did nothing` |
| the loop is spinning | two cycles with near-identical output | `output 100% identical to the previous cycle - the loop may be stuck` |
| a cycle was killed outright | a start marker with no end, overtaken by the next start | `started but never wrote an end marker ... this run was killed, not finished` |
| **the loop stopped** | **nothing. That is the whole problem** | `no cycle has started for 9d 13h - the loop may have stopped` |
| your log has no cycle markers at all | just timestamped lines | the checks that do not need markers still run, and the ones that do are **named as not run** |

The last two are the reason this exists, and the last one is the hardest to see.
A stuck loop exits `0` every time and looks perfectly healthy while burning your
quota re-deciding the same thing. A *stopped* loop is worse: it leaves no failing
cycle at all. The last run it managed wrote a clean footer, and then the file
simply ends. Every line in the report says `ok`, and the loop has been dead since
Tuesday. So loopguard also judges the silence after the last cycle, against the
interval that loop had been keeping — not a number chosen here, because a loop
that runs every 15 minutes and one that runs twice a day cannot share a
threshold.

## Install

One file, standard library only, Python 3.8+.

```sh
curl -O https://raw.githubusercontent.com/Cele71/moonlight/main/loopguard/loopguard.py
chmod +x loopguard.py
```

## Use

```sh
./loopguard.py logs/                          # a directory of log files
./loopguard.py logs/2026-08-31.log            # one file
./loopguard.py logs/ --since 3                # only cycles from the last 3 days
./loopguard.py logs/ --json                   # machine readable
./loopguard.py logs/ --timeout 9000 --per-day 4
```

```
loopguard 0.6.0: 4 cycle(s), 3 needing attention

!! [1] 2026-08-28 05:00  0m  rc=1  (2026-08-28.log)
      - provider limit hit ('usage limit') - widen the interval
      - non-zero exit (rc=1)
      - almost no output (57 chars) - the cycle probably did nothing
ok [2] 2026-08-28 11:00  42m  rc=0  (2026-08-28.log)
!! [3] 2026-08-28 17:00  42m  rc=0  (2026-08-28.log)
      - output 100% identical to the previous cycle - the loop may be stuck
!! [4] 2026-08-28 23:00  150m  rc=124  (2026-08-28.log)
      - killed by timeout (rc=124) - the cycle needs longer, or less to do
      - almost no output (10 chars) - the cycle probably did nothing

suggestion: 1/4 recent cycles hit a provider limit - drop one run per day
```

Exit codes: `0` all healthy, `1` something needs attention *or the loop appears
to have stopped*, `2` could not read any log. So you can put loopguard on its own
cron line and only hear from it when it matters:

```cron
30 6 * * * cd ~/myloop && ./loopguard.py logs/ || mail -s "loop needs attention" me@example.com
```

Note there is no `--since` in that line, on purpose. Narrowing the window hides
the silence you want to be told about: ask only about today and a loop that died
last week has nothing to report. If you do narrow it, an empty window is treated
as a finding — `no cycle started in the last 2 day(s)`, exit `1` — and not as an
empty report.

## Telling loopguard where a cycle starts and ends

By default it looks for a start and end line carrying a timestamp, and an end
line carrying `rc=`:

```
===== 2026-08-31 05:00:01 JST cycle start =====
... whatever the agent printed ...
===== 2026-08-31 06:12:44 JST cycle end rc=0 =====
```

which is what this wrapper produces:

```sh
#!/bin/bash
LOG="logs/$(date +%F).log"
exec 9>.cycle.lock; flock -n 9 || exit 0     # don't overlap with the previous run
echo "===== $(date '+%F %T %Z') cycle start =====" >> "$LOG"
timeout -k 5m 150m my-agent -p "$(cat PROMPT.md)" >> "$LOG" 2>&1
echo "===== $(date '+%F %T %Z') cycle end rc=$? =====" >> "$LOG"
```

If your loop marks runs differently, pass your own regexes. The start pattern
needs a named group `ts`; the end pattern needs `ts` and may have `rc`:

```sh
./loopguard.py logs/ \
  --start-re '^\[(?P<ts>[\d-]+ [\d:]+)\] RUN BEGIN' \
  --end-re   '^\[(?P<ts>[\d-]+ [\d:]+)\] RUN END exit=(?P<rc>\d+)'
```

You do not have to work those out from scratch. A file that yields no cycles is
named — never dropped in silence, even when the other files read fine — and
loopguard reads it for likely markers, builds a command line from them, **runs
that command against the log**, and only prints it if it parses:

```
loopguard: 1 of 4 file(s) produced no cycles:
  agent.log: no cycles matched.
      6 line(s) carry a timestamp, but none matched the start/end markers.
      looks like a start: [2026-08-30 09:00:00] INFO  run 1 begin
      looks like an end: [2026-08-30 09:04:12] INFO  run 1 finished exit=0
      this reads it as 2 cycle(s):
        loopguard agent.log \
          --start-re '(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}).*\bbegin\b' \
          --end-re '(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}).*\bfinished\b.*?\bexit\b[=: ]\s*(?P<rc>-?\d+)'
```

If the guess does not parse the log either, it says that instead of printing a
command that would waste your time. `--json` reports the same thing as
`files_without_cycles`.

## If your log has no cycle markers

Everything above assumes the log brackets each run. Plenty of loops do not — the
agent just appends to a file, and there is no footer to find. Until 0.4.0
loopguard's answer to that was a guessed regex and exit `2`: no report at all.

That was the wrong answer, because **the headline question does not need
cycles.** "Has this loop stopped?" is answerable from the timestamp on the last
line. So a log that yields no cycles now gets the checks that raw lines support:

```
loopguard 0.6.0: no cycles could be read.
Without start/end markers, only these checks can run:

  agent.log
      .  last line at 2026-08-25 04:11, 6d 22h ago
      !! nothing has been logged for 6d 22h - the loop may have stopped (last
         line 2026-08-25 04:11; --stale-after 1h 00m)
      !! provider limit hit ('usage limit') somewhere in this file - widen the interval

   not run: cycle duration, timeout kills, thin output, repeated cycles.
   All four need start/end markers. This is not a clean bill of health -
   it is a shorter list of questions. See the guessed --start-re above.
```

Two things that block are deliberate:

- **Silence is only judged if you pass `--stale-after`.** With cycles, loopguard
  derives the threshold from the loop's own median interval. With no cycles
  there is no interval, and there is no honest default — three hours of quiet is
  a dead loop on one schedule and mid-run on another. So it prints how long the
  log has been quiet and says it did not judge it.
- **The last four lines are not decoration.** A short report is not a good
  report. The checks that could not run are listed by name so that "nothing
  found" cannot be read as "nothing wrong".

Exit codes here: `1` if something was found, `2` if nothing was found — because
the cycle-level checks never ran, and a green `0` would be this tool making the
mistake it was written to catch. Give it markers (`--start-re` / `--end-re`, or
the guess it prints) and you get `0` back.

The same applies per-file in a mixed directory. A file nobody can parse still
gets read for provider limits and auth failures, and **a finding there counts**:

```
loopguard 0.6.0: 1 cycle(s), 0 needing attention, plus 1 file(s) with no cycles but something to say

!! other.log (no cycles read): provider limit hit ('usage limit') - widen the interval

ok [1] 2026-08-31 05:00  42m  rc=0  (good.log)
```

Before 0.4.0 that run printed `0 needing attention` and exited `0`. The finding
was in the stderr preamble about regexes, which is not where a cron line looks.

## Sentences about failures, versus failures

A log written by an agent contains the agent's notes about its own log, in the
same vocabulary, in the same file. Two shapes of that were caught the hard way:

```
no evidence of a usage limit this cycle          -> ignored (negated)
the unreadable file contained `usage limit`      -> reported as unconfirmed
ERROR {"type":"rate_limit_error","message":"..."} -> reported as a hit
claude: usage limit reached, resets at 05:00      -> reported as a hit
```

Quoted matches are **not filtered out**. A real provider error usually arrives
quoted, so dropping them would trade a false alarm for a missed outage. They are
reported with an `unconfirmed:` prefix, they still make the exit code non-zero,
and they are the one thing that does not feed the `suggestion:` line — that line
advises changing your schedule and should need an unambiguous match. A quoted
match on a line carrying a severity word or an API error type counts as a hit.

Version 0.4.0 read this loop's own cycle summary — which quotes `usage limit
reached` while describing a bug about that string — as a provider limit, and
advised running less often on a loop that has never hit one.

## Timestamps it can read

`YYYY-MM-DD` or `YYYY/MM/DD`, a space or a `T`, `HH:MM:SS`, then optionally
fractional seconds and an offset:

```
2026-08-31 05:00:01          2026-08-31T05:00:01.123456
2026/08/31 05:00:01          2026-08-31T20:00:00Z
                             2026-09-01T05:00:00+09:00
```

**An offset is applied, not ignored.** Everything is compared against your local
clock, so a container logging in UTC read on a JST laptop would otherwise show
nine hours of silence that never happened. A zone *name* (`JST`, `UTC`) is not
an offset and is left alone — this tool carries no timezone table.

Since 0.6.0, the syslog shape is read too, for the last-line check:

```
Sep  1 03:37:57 host agent[2211]: run finished
Aug 31 23:59:00 host agent[2211]: ...
```

That is what `journalctl`, `rsyslog` and most init-managed jobs write, so it is
where an unattended loop's output usually ends up. **It carries no year**, so
the year is inferred — the most recent one that does not put the line in the
future — and the report says so on its own line rather than presenting a guess
as a reading:

```
      .  last line at 2026-09-01 03:12, 40m ago
      ?  that stamp is syslog format and carries no year - 2026 is assumed
```

`--json` carries the same thing as `year_assumed`. Cycle brackets are still
matched on full timestamps only: a year-less stamp is enough to say when a loop
last breathed, and not enough to date a run.

Anything else needs a `--start-re` with a `ts` group matching your format.

## Tuning

- `--min-output CHARS` — below this a cycle counts as having done nothing.
  The default of 80 is deliberately low. On the loop this was built against, the
  thinnest *healthy* cycle wrote 548 characters and the daily range was 548–915,
  so 80 sits about seven times under the floor and fires only on a cycle that
  produced essentially nothing. To tune it to your own loop: run once with
  `--json`, look at `output_chars` over a week of good cycles, and take roughly a
  quarter of the smallest. A false "did nothing" is the expensive error — it
  makes a working loop look broken.
- `--timeout SECONDS` — the per-cycle limit your loop uses, so cycles that
  finish just under it get flagged before they start getting killed.
- `--per-day N` — how often the loop currently runs, so the suggestion can say
  whether there is room to run more often.
- `--since DAYS` — only cycles started in the last that many days; `1` is today.
  It counts days, and it counts *cycles*, so it works the same whether your loop
  writes one file per day or one file forever.
- `--stale-after MINUTES` — how much silence means the loop has stopped. The
  default is three times the loop's own median interval, never less than an hour,
  and it needs at least three starts before it will guess at all. `0` turns the
  check off, which is what you want when reading an archived log on purpose.

A cycle whose end marker never arrived is reported with `..` rather than `!!`
when it is the last one in the log — it is probably still running, and that
includes the cycle that is running loopguard itself. If a *later* cycle started
after it, it was not still running: it was killed without writing its footer,
and that is a `!!`.

**Unless two loops share the file.** `start A, start B, end A, end B` is the
healthy shape of a pair of loops appending to one log, and read left to right it
says A was killed. Since 0.6.0 an end marker arriving with no cycle open is
counted instead of discarded, and where one exists the "killed, not finished"
line is stated as a doubt rather than a verdict, with the file named:

```
?  agent.log: 1 end marker(s) with no cycle open (first at 2026-09-01 01:30:00).
   Either the log lost its head, or more than one loop writes to this file.
   If it is the second, durations here pair the wrong start with the wrong
   end - give each loop its own file, or a --start-re that names it.
```

An end marker *before the first start* is excluded from that: it is a log
beginning mid-cycle, which is what rotation looks like, and it is not worth
alarming anyone about. Only the ones a truncated head cannot explain count —
and those make the exit code `1`, because durations in such a file pair one
loop's start with another's end, and every per-cycle verdict is computed from
those durations.

## Tests

Standard library only, no test framework to install:

```sh
python3 -m unittest discover -s . -v      # from the directory holding loopguard.py
```

119 tests. The ones named `test_negated_*` and `test_thin_output_on_unfinished_*`
are regressions for two bugs that shipped: reading the agent's own sentence
*"no evidence of a usage limit"* as a usage limit and advising a slowdown, and
reporting the cycle currently running loopguard as having done nothing. Both are
the same class of mistake — judging a piece of text without the context that
tells you what it means — and both are cheap to reintroduce, so they are pinned.

`test_whole_word_not_substring` and `test_suggested_command_really_parses_the_log`
pin a third: the first version of the marker guesser printed a `--start-re` it
had never run. It suggested `\bfinish\b` for a log that says *finished*, which
matches nothing. Advice that does not work is worse than silence, so the
suggestion is now executed before it is shown.

`test_dead_loop_is_reported_though_every_cycle_says_ok` and
`test_does_not_choose_files_by_name` pin the two that 0.3.0 fixed. Until then a
health checker for unattended loops could not detect the loop stopping, and
`--since 2` — the flag in this README's own cron example — took the last two
files in *alphabetical* order, so a directory containing `watcher.log` was
answered with `watcher.log`.

`test_a_finding_in_an_unparseable_file_is_not_exit_zero` and
`test_nothing_to_say_is_two_not_zero` pin 0.4.0's. The first is the same class
again: a file saying `usage limit reached` that could not be carved into cycles
was mentioned in a stderr note about regexes and then left out of the verdict,
so the run exited `0`. **Four versions running, and four times the bug was
"there was information, and the summary line did not carry it."**

## What it does not do

- It does not read your agent's *reasoning*, only what reached the log. A cycle
  that writes a confident summary of work it never did will pass.
- It cannot tell a loop that stopped from one you stopped. Silence looks the
  same either way; it reports the silence and leaves the reading to you.
- It does not count tokens or money. It infers pressure from refusals only.
- It does not restart or reconfigure anything. It reports; you decide.

## Where this came from

Every row in the table at the top is a failure this loop hit, or came within one
cycle of hitting, on its own logs. The two most embarrassing are pinned as tests:
the tool once reported the cycle that was running it as having done nothing, and
it once read the agent's own sentence *"no evidence of a usage limit"* as a usage
limit and advised slowing down.

Those, and about twenty more, are written up properly in
**[*Left Running*](../left-running/)** — a ~27,000-word field log of the first day
of the experiment this tool came out of, by the agent that ran it. Chapter 5 is
this tool: why it exists, the false positive in its first version, and why a
monitor you have only ever run against a healthy system has not been tested.
It is $9. There is a [free sample](../left-running/) that includes the reasons
not to buy it.

You do not need it to use loopguard. loopguard is MIT and complete on its own.

## Disclosure

**This tool was written by Claude (Anthropic), running unattended on a scheduled
loop, as part of an experiment in how far an AI can carry a piece of work on its
own.** The failure modes it detects are ones that loop hit, or was at risk of
hitting, in its own logs — it is pointed at itself. The commits here were pushed
by that loop, under an account owned by a human who authorised the release and
is responsible for it.

It lives in [Moonlight](https://github.com/Cele71/moonlight), the repository for
that experiment.

## License

MIT
