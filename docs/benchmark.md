# Benchmark

`bench/tasks.json` holds real fixes from the fleet. Each one is graded by tests
the team never sees, in the same way as
[SWE-bench](https://www.swebench.com/): the team starts from the commit before
the fix, with only a problem statement, and passes if the fix's own test files
pass on its change.

## Tasks

| Id | Repository | Size of the real fix |
| --- | --- | --- |
| `pw-redteam-missing-key` | platform-workflows | 1 file, 4 lines |
| `pw-score-malformed` | platform-workflows | 1 file, 9 lines |
| `pw-delivery-retry` | platform-workflows | 1 file, 8 lines |
| `sh-unmapped-domain-error` | shortlink | 1 file, 13 lines |
| `pw-analysis-title` | platform-workflows | 1 file, 2 lines |
| `pw-runner-budget-stdin` | platform-workflows | 1 file and 4 prompts, 22 lines |
| `pw-scan-context-secrets` | platform-workflows | 2 files, 30 lines |
| `pw-thinking-switch` | platform-workflows | 1 file, 38 lines |
| `pw-security-update-type` | platform-workflows | 1 file, 31 lines |
| `sh-head-metadata` | shortlink | 4 files, 82 lines |
| `pw-agent-privacy-metadata` | platform-workflows | 2 files, 54 lines |
| `pw-provider-key-redaction` | platform-workflows | 2 files, 58 lines |
| `pw-analysis-budget-retries` | platform-workflows | 2 files, 93 lines |
| `pw-experiment-profile-cli` | platform-workflows | 1 file, 45 lines |
| `pw-experiment-budgets` | platform-workflows | 1 file, 109 lines |
| `pw-context-languages` | platform-workflows | 1 file, 50 lines |
| `pw-context-priority` | platform-workflows | 1 file, 105 lines |
| `pw-bounded-retries` | platform-workflows | 1 file, 79 lines (much of it re-indentation) |
| `pw-dsse-archive` | platform-workflows | 2 files, 48 lines |
| `ad-requeue-finished` | agent-dispatcher | 1 file, 32 lines |
| `ad-ci-error-lines` | agent-dispatcher | 1 file, 30 lines |
| `ad-actions-job-links` | agent-dispatcher | 3 files, 57 lines |
| `ad-pr-title-acronyms` | agent-dispatcher | 1 file, 10 lines |
| `ad-fence-scrub` | agent-dispatcher | 1 file, 25 lines |
| `ad-sandbox-failure-detail` | agent-dispatcher | 3 files, 28 lines |

The first four use the audit issue each fix closed, word for word: the same
input the team got in production. The other 21 have written statements that
describe the problem and the interface their tests use (names, arguments,
exact values), never the implementation, as SWE-bench Verified requires.

The `ad-` tasks are fixes to this repository. The team works on a checkout of
the commit before each fix, offline like every other task, so the installed
controller is never touched. `pw-dsse-archive`'s tests call the `openssl` CLI,
which the sandbox image has.

Every task is validated: its hidden tests fail on the commit before the fix and
pass on the fix, in the sandbox.

## Run it

The benchmark uses the production `.env`: the same providers, team rules,
budgets and sandbox. It needs the sandbox, because grading runs there.

```powershell
# Check every grader still fails before its fix and passes on it (no model).
.\.venv\Scripts\python.exe -m agent_dispatcher.bench validate --root D:\showcase\work\benchdata

# Run the team on every task, or a few with --only.
.\.venv\Scripts\python.exe -m agent_dispatcher.bench run --root D:\showcase\work\benchdata
```

To compare with the team as it worked before the sandbox, add
`--team-sandbox off`: the team gets no run tool and no checks, and grading still
runs in the sandbox. Give each run its own `--first-run-id` range (for example
91000 and 92000) so their telemetry never mixes.

A run takes hours. On Windows the benchmark process asks Windows to keep the
PC from idle sleep until it ends, as `scripts/cycle-awake.ps1` does for scheduled
cycles: a sleep in the middle of a call would run the clock out on its task. If
Windows refuses, a line on stderr says so and the run goes on unprotected. It
doesn't stop a manual sleep, a closed lid or a restart.

Each task prints one JSON line: the team, outcome, whether a change was
published, whether the hidden tests pass, `resolved` (both), wall seconds,
tokens, model seconds and calls. A summary line follows.

### Can "done" be trusted?

A published change is the team saying "done". So besides `resolved`, the
summary measures how far that claim can be trusted:

- `published`: how many changes the team published.
- `wrong_done`: published changes whose hidden tests fail. The team said done,
  and it wasn't.
- `correct_unpublished`: changes the team didn't publish whose hidden tests pass.
  The work was right, but it was lost.
- `done_precision`: the share of graded published changes that were right.
- `ungraded`: tasks that were never graded. Grading tries the sandbox three
  times, 30 seconds apart, when ssh or docker fails before the hidden tests
  start. If all three fail, the task is left ungraded and the benchmark goes
  on. An ungraded task counts as neither right nor wrong. The fault's detail
  goes to stderr only, because it can name hosts and key paths.

Before this was measured, every run from 2026-11-01 to 2026-11-09 together
(192 results) had published 168 changes, 14 of them wrong (precision 0.92).
Eight of the 14 were the same task, `pw-scan-context-secrets`. The team's own
tests replaced the scanner with a fake, so they passed on a change that hid the
secret from the real scanner, and the reviewer approved it. The aim is a
`wrong_done` of 0: when the team says done, it should be right.

### Several tasks at once

`--parallel 2` works on two tasks at a time. Each result prints when its task
finishes, so lines can come out of file order. Run ids still follow the file
order. The default is 1, one task at a time.

About 90% of a task's time is spent waiting for the model, so tasks side by side
barely slow each other down. On 2026-11-09 two held-out tasks run together
finished in 20 minutes, against 39 one after the other, with the same results.

The limits are memory, not CPU:

- The PC (8 GB) runs one OpenCode worker per task, at about 0.5 GB each.
- The sandbox VPS (2 CPUs, 3.8 GB) also serves live services, and each sandbox
  container may take 1 CPU and 1 GB.

During that test the PC never had less than 447 MB free, and the VPS never had
less than 1.4 GB available, with no new swap. Keep it at 2 until there is more
memory on both.

If a task raises, no new task starts, the tasks already running finish and
print, and then the error stops the run.

## Held-out tasks

The harness has been changed nine times on the strength of the 25 tasks above,
so their score now partly measures how well it fits them. `bench/heldout.json`
holds 12 more real fixes that no harness change was made from. Its score is the
honest one. Fresh tasks the system was never fitted to are the same idea as
[SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) and
[SWE-rebench](https://swe-rebench.com/).

| Id | Repository | Size of the real fix |
| --- | --- | --- |
| `ad-verdict-after-braces` | agent-dispatcher | 1 file, 30 lines |
| `ad-review-format-retry` | agent-dispatcher | 1 file, 21 lines |
| `ad-repair-required-checks` | agent-dispatcher | 3 files, 18 lines |
| `ad-repair-hook-lint` | agent-dispatcher | 1 file, 12 lines |
| `ad-retry-unexplained` | agent-dispatcher | 1 file, 37 lines |
| `ad-capped-planner-notes` | agent-dispatcher | 1 file, 50 lines |
| `ad-failure-detail` | agent-dispatcher | 4 files, 57 lines |
| `ad-provider-opt-out` | agent-dispatcher | 4 files, 57 lines |
| `ad-run-summary` | agent-dispatcher | 2 files, 45 lines |
| `ad-turns-raise-level` | agent-dispatcher | 2 files, 76 lines |
| `ad-requirement-check` | agent-dispatcher | 1 file, 64 lines |
| `pw-quoted-injection` | platform-workflows | 1 file, 5 lines |

The statements follow the same rules as above, and every task is validated the
same way. Two rules keep the set held out:

- Run it to measure, not to tune. A miss here is a sign to look for the same
  failure in the 25 tasks; a fix made for one held-out task makes the set one
  task smaller, and that task moves to `bench/tasks.json`.
- A test checks that the two files never share a task or a fix commit.

```powershell
.\.venv\Scripts\python.exe -m agent_dispatcher.bench run --tasks bench\heldout.json --root D:\showcase\work\heldout-data --first-run-id 104000
```

## What stays offline

- **GitHub:** a fake `gh` accepts every write and does nothing. The team pushes
  to a local bare repository whose `main` is the commit before the fix; the
  fix itself is never in it.
- **Telemetry** goes to `<root>/telemetry/run-<id>`, not the controller's, so
  benchmark runs never reach the dashboard, and runs side by side never write to
  the same file.
- **Run ids** start at 90000, apart from real runs.

## Cost

Every task is a real team run on the configured providers. MiMo is a paid plan,
so a full run spends real tokens: on 2026-11-05 the 13 original tasks used 1.66M
tokens together (40k to 280k each), so all 25 take about 3M. Run the benchmark deliberately, before and after a change worth measuring,
and report the tokens with the result.
