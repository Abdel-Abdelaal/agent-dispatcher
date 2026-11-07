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

## What stays offline

- **GitHub:** a fake `gh` accepts every write and does nothing. The team pushes
  to a local bare repository whose `main` is the commit before the fix; the
  fix itself is never in it.
- **Telemetry** goes to `<root>/telemetry`, not the controller's, so benchmark
  runs never reach the dashboard.
- **Run ids** start at 90000, apart from real runs.

## Cost

Every task is a real team run on the configured providers. MiMo is a paid plan,
so a full run spends real tokens: on 2026-11-05 the 13 original tasks used 1.66M
tokens together (40k to 280k each), so all 25 take about 3M. Run the benchmark deliberately, before and after a change worth measuring,
and report the tokens with the result.
