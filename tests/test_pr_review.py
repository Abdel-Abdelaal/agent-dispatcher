import json
from unittest.mock import Mock

import pytest

from agent_dispatcher import pr_review

HEAD = "a" * 40
BASE = "b" * 40


def metadata(**changes: object) -> str:
    data: dict[str, object] = {
        "headRefOid": HEAD,
        "baseRefOid": BASE,
        "changedFiles": 2,
        "additions": 5,
        "deletions": 1,
    }
    data.update(changes)
    return json.dumps(data)


def test_diff_fetches_immutable_comparison_and_redacts_before_returning() -> None:
    token = "ghp_" + "a" * 36
    gh = Mock(side_effect=[metadata(), "patch " + token, metadata()])
    data = json.loads(pr_review.diff(gh, "owner/app", 5, HEAD))
    assert data["head_sha"] == HEAD
    assert data["base_sha"] == BASE
    assert data["redacted"] is True
    assert token not in data["diff"]
    assert gh.call_args_list[1].args[-1] == f"repos/owner/app/compare/{BASE}...{HEAD}"


@pytest.mark.parametrize("head", ["", "main", "a" * 41])
def test_diff_needs_full_observed_sha_before_github_read(head: str) -> None:
    gh = Mock()
    with pytest.raises(pr_review.ReviewError, match="full head"):
        pr_review.diff(gh, "owner/app", 5, head)
    gh.assert_not_called()


@pytest.mark.parametrize(
    "after", [metadata(headRefOid="c" * 40), metadata(baseRefOid="c" * 40)]
)
def test_diff_never_returns_content_when_head_or_base_changes_during_read(
    after: str,
) -> None:
    gh = Mock(side_effect=[metadata(), "must not return mixed evidence", after])
    with pytest.raises(pr_review.ReviewError, match="changed"):
        pr_review.diff(gh, "owner/app", 5, HEAD)


def test_diff_refuses_already_stale_head_and_missing_base() -> None:
    for value in (metadata(headRefOid="c" * 40), metadata(baseRefOid=None)):
        gh = Mock(return_value=value)
        with pytest.raises(pr_review.ReviewError):
            pr_review.diff(gh, "owner/app", 5, HEAD)
        assert gh.call_count == 1


@pytest.mark.parametrize("changes", [{"changedFiles": 31}, {"additions": 1001}])
def test_oversized_change_is_refused_before_fetching_diff(
    changes: dict[str, int],
) -> None:
    gh = Mock(return_value=metadata(**changes))
    with pytest.raises(pr_review.ReviewError, match="too large"):
        pr_review.diff(gh, "owner/app", 5, HEAD)
    assert gh.call_count == 1


def test_diff_does_not_silently_truncate_oversized_text() -> None:
    gh = Mock(side_effect=[metadata(), "é" * pr_review.MAX_TEXT])
    with pytest.raises(pr_review.ReviewError, match="too large"):
        pr_review.diff(gh, "owner/app", 5, HEAD)


@pytest.mark.parametrize("path", [".env", ".ENV.local", '"a/space/.env"'])
def test_diff_cannot_bypass_secret_file_policy_including_renamed_old_file(
    path: str,
) -> None:
    gh = Mock(
        side_effect=[metadata(), f"diff --git {path} b/config.py\nsecret payload"]
    )
    with pytest.raises(pr_review.ReviewError, match="secret configuration"):
        pr_review.diff(gh, "owner/app", 5, HEAD)


def test_ci_errors_reuse_existing_extractor_and_match_the_actual_head() -> None:
    token = "ghp_" + "a" * 36
    job = json.dumps({"head_sha": HEAD, "status": "completed"})
    log = "agentservice noise\nerror: bad type " + token
    gh = Mock(side_effect=[metadata(), job, log, metadata()])
    data = json.loads(pr_review.ci_errors(gh, "owner/app", 5, HEAD, 123))
    assert data["job_id"] == 123
    assert data["head_sha"] == HEAD
    assert data["redacted"] is True
    assert data["truncated"] is False
    assert "bad type" in data["errors"]
    assert "agentservice" not in data["errors"]
    assert token not in data["errors"]


@pytest.mark.parametrize(
    "job",
    [
        {"head_sha": BASE, "status": "completed"},
        {"head_sha": HEAD, "status": "in_progress"},
    ],
)
def test_ci_log_is_not_fetched_for_old_head_or_running_job(job: dict[str, str]) -> None:
    gh = Mock(side_effect=[metadata(), json.dumps(job)])
    with pytest.raises(pr_review.ReviewError):
        pr_review.ci_errors(gh, "owner/app", 5, HEAD, 123)
    assert gh.call_count == 2


def test_ci_log_requires_job_id_and_rechecks_head_after_reading() -> None:
    gh = Mock(return_value=metadata())
    with pytest.raises(pr_review.ReviewError, match="job ID"):
        pr_review.ci_errors(gh, "owner/app", 5, HEAD, 0)
    gh = Mock(
        side_effect=[
            metadata(),
            json.dumps({"head_sha": HEAD, "status": "completed"}),
            "error: no",
            metadata(headRefOid=BASE),
        ]
    )
    with pytest.raises(pr_review.ReviewError, match="head changed"):
        pr_review.ci_errors(gh, "owner/app", 5, HEAD, 123)


def test_ci_diagnostics_report_byte_truncation_explicitly() -> None:
    gh = Mock(
        side_effect=[
            metadata(),
            json.dumps({"head_sha": HEAD, "status": "completed"}),
            "error: " + "é" * pr_review.MAX_TEXT,
            metadata(),
        ]
    )
    data = json.loads(pr_review.ci_errors(gh, "owner/app", 5, HEAD, 123))
    assert data["truncated"] is True
    assert len(data["errors"].encode("utf-8")) <= pr_review.MAX_TEXT


def test_required_checks_keep_failure_and_pending_outcomes_as_data() -> None:
    rows = [
        {"name": "Types", "state": "FAILURE"},
        {"name": "Tests", "state": "PENDING"},
    ]
    gh = Mock(side_effect=[metadata(), json.dumps(rows), metadata()])
    data = json.loads(pr_review.checks(gh, "owner/app", 5, HEAD))
    assert data["required_checks"] == rows
    assert gh.call_args_list[1].kwargs == {"allow_codes": (1, 8)}


@pytest.mark.parametrize("output", ["no required check information", "{}"])
def test_missing_required_check_classification_is_not_approval(output: str) -> None:
    gh = Mock(side_effect=[metadata(), output])
    with pytest.raises(pr_review.ReviewError, match="unavailable"):
        pr_review.checks(gh, "owner/app", 5, HEAD)
