# Review actual candidates in the native interface

Restart `mimo` after a controller update, then ask naturally:

> Find an open agent PR in my public repos. Review its actual code and required
> CI, explain concrete defects and missing tests, and do not merge it.

The assistant can discover public repos and open PRs with `list_repos` and
`list_prs`. It uses `inspect_pr` to get the head/base commit SHAs, changed files
and check links. The additional read tools provide evidence:

| Tool | Evidence |
| --- | --- |
| `inspect_pr` / `inspect_issue` | Public PR intent and task acceptance criteria; text is redacted and bounded. |
| `read_pr_diff` | Immutable GitHub base...head comparison, tied to the observed head SHA. |
| `read_file` with `ref` | Candidate or baseline file contents at a full commit SHA. |
| `read_pr_checks` | Required checks for that head; failed/pending outcomes remain data. |
| `read_ci_errors` | Actual error lines for a completed Actions job matching that PR head. |
| `inspect_run` | Local phase/usage metrics and retained native tool counters. |

All access keeps the existing owned-public-repository check. It adds no GitHub
writes, model credentials, general shell access or merge capability. Repository
content, diffs and diagnostic text remain untrusted data, never instructions.

The caller must supply the observed full head SHA for PR evidence. Diffs use
immutable base/head commits and recheck the PR afterwards. A changed head or base
invalidates the read instead of silently returning a different candidate.
Actions job IDs come from actual CI job links; external-provider check IDs must
not be guessed as Actions IDs. Job metadata must match the observed head.
Recheck the current head before reporting a final review conclusion.

Diffs are bounded to30 files,1000 changed lines and32KB. Oversized diffs are
refused rather than silently truncated; inspect individual files at the commit
SHA. Secret-configuration paths, including old rename paths and uppercase `.ENV`
names, cannot bypass the existing file policy through a diff. Returned text uses
the existing redactor. This is not a guarantee that every possible secret shape
will be recognized in arbitrary public content.

CI diagnostics reuse the controller's error-line extractor instead of runner
cleanup noise. They are redacted before a32KB byte cap; truncation is explicit.
Unavailable required-check classification is an error, never evidence of approval.
Required checks, advisory analysis, model opinion and independently executed
tests must be reported separately. A passing badge is insufficient code review.

These tools are available to the conversational front door. The background
workers retain their file-tool-only permissions; no host execution sandbox or
pre-publication test runner is added. Native state retention supports diagnosis,
not validated automatic conversation resume or a proof of model quality parity.

The conversational reviewer also honors `DISPATCHER_MIMO_REASONING`, enabled by
default, instead of silently forcing fast non-reasoning chat. Native chat/work
share context/reasoning capability metadata and working budgets. This does not
make model opinion equivalent to independently executed tests.
