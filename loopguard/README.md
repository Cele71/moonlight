# loopguard

Health check for an AI coding agent running on a cron loop.

You put Claude Code (or any CLI agent) on a schedule, point it at a prompt, and
walk away. Then the honest question is: **is it still doing anything?**

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
loopguard 0.3.0: 4 cycle(s), 3 needing attention

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

## Tests

Standard library only, no test framework to install:

```sh
python3 -m unittest discover -s . -v      # from the directory holding loopguard.py
```

71 tests. The ones named `test_negated_*` and `test_thin_output_on_unfinished_*`
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

## What it does not do

- It does not read your agent's *reasoning*, only what reached the log. A cycle
  that writes a confident summary of work it never did will pass.
- It cannot tell a loop that stopped from one you stopped. Silence looks the
  same either way; it reports the silence and leaves the reading to you.
- It does not count tokens or money. It infers pressure from refusals only.
- It does not restart or reconfigure anything. It reports; you decide.

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
