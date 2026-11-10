# Acceptance tests

When the team publishes a change, it is saying "done". That claim has to be
trustworthy. Before this, 14 of the 168 changes published across the benchmark
runs of 2026-11-01 to 2026-11-09 failed their hidden tests. In those runs the team
said done and it wasn't.

In the clearest case the worker's own tests replaced the code under test with a
fake, so they passed on a change that didn't work, and the reviewer approved it.
The worker's tests and the reviewer both see the change, so they can share its
mistakes. Acceptance tests are written before the change exists, by a call that
never sees it.

## How it works

- **Written from the issue alone.** After the plan, the worker writes one or two
  pytest files named `test_acceptance_<topic>.py` in a fresh clone of the
  unchanged repository. It sees only the issue, not the plan, and runs its tests
  with the sandbox run tool.
  - The call is capped at 20 steps and 15 minutes.
  - Running out of steps never raises the task's level.
  - A failed call leaves the run as it was; a spent quota still parks it.
- **Kept only if they fail before the change.** The controller runs the new test
  files on the unchanged code in the sandbox, by SWT-bench's fail-to-pass rule.
  It drops them all when:
  - they pass there, so they test nothing new;
  - they don't run;
  - they fail only on a syntax error of their own.
- **Run on each candidate.** After a candidate's checks, the controller runs the
  kept files on the change in the sandbox. The files go into the sandbox's copy
  only, never into the change. If they fail, the worker gets one round to meet
  them, shown the tests and their output and told the tests may be wrong. The
  round is skipped when the run's time is short.
- **Shown to the reviewer.** Every review gets the latest result: the tests may
  be wrong, a failure is a reason to check that requirement, and passing tests
  don't replace its own check.
- **Used to choose.** On a hard task with more than one candidate, a candidate
  that passes the acceptance tests ranks above one that fails them, just after
  the repository's checks.
- **Reported in the PR.** The run table says how many files were written from the
  issue alone, that they failed on the unchanged code, and whether they pass on
  the change. The wording is the controller's own: no model text.

## What it isn't

- **Not a gate.** Generated tests are often wrong: the best published systems
  produce a fail-to-pass test for only about a third to two thirds of issues. A
  failing acceptance test makes the reviewer look, and the PR says so. It never
  refuses a change by itself.
- **Not part of the change.** The tests aren't published, so they don't count
  toward the diff caps. The worker may add its own tests as usual.
- **Python only.** It needs a locked Python project in the sandbox, like the
  repository checks.

## Cost and switch

About one short coding call per task, plus a few seconds of sandbox per check.
`DISPATCHER_ACCEPTANCE_TESTS=false` turns it off.

The benchmark's `wrong_done` and `done_precision` ([benchmark](benchmark.md))
measure whether it works.

## Sources

- [Otter](https://arxiv.org/abs/2502.05368) and
  [e-Otter++](https://arxiv.org/abs/2508.06365): tests generated from issues, and
  improved by running them.
- [SWT-bench](https://arxiv.org/abs/2406.12952): the fail-to-pass rule. It found
  that generated tests used as a filter double the precision of SWE-agent's fixes.
- [Issue2Test](https://arxiv.org/abs/2503.16320): reproducing tests from issue
  reports.
