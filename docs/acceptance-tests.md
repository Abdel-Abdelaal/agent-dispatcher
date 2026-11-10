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

## Measured: off by default

Benchmark 10 ran all 25 tasks with acceptance tests on, on 2026-11-10 (ids
105000 to 105017 and 105100 to 105106). All 25 were resolved and none was
wrongly done. Benchmark 9, without them, resolved 23. But the acceptance tests
can't take the credit:

- **Cost.** 6.67M tokens against 4.45M for benchmark 9 (+50%). The
  acceptance call alone used 1.94M (29%) and about 4.4 hours of model time.
  The writer stopped at its step or time cap on 14 of the 24 tasks whose notes
  were kept.
- **What was kept.** Of those 24 tasks, the tests were kept on 16. On 6 they
  passed before the change, and on 2 no test file was written.
- **What they flagged.** They failed on the change 3 times
  (`sh-head-metadata`, `ad-actions-job-links`, `ad-fence-scrub`). All 3
  changes passed their hidden tests, so all 3 flags were wrong tests. Each one
  cost a worker round and a reviewer warning for nothing.
- **What they caught.** Nothing. The task that was most often wrongly done
  before, `pw-scan-context-secrets`, got no acceptance tests (no test file was
  written), so its first pass came from elsewhere. The other gain,
  `pw-bounded-retries`, came from the retry for malformed streams.

So the feature stays, but `DISPATCHER_ACCEPTANCE_TESTS` defaults to `false`.
Turn it on to measure it again, for example with a stronger worker. The
benchmark's `wrong_done` and `done_precision` ([benchmark](benchmark.md)) show
whether it helps.

## Sources

- [Otter](https://arxiv.org/abs/2502.05368) and
  [e-Otter++](https://arxiv.org/abs/2508.06365): tests generated from issues, and
  improved by running them.
- [SWT-bench](https://arxiv.org/abs/2406.12952): the fail-to-pass rule. It found
  that generated tests used as a filter double the precision of SWE-agent's fixes.
- [Issue2Test](https://arxiv.org/abs/2503.16320): reproducing tests from issue
  reports.
