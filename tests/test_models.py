"""Tests for app.core.models vocabulary: entity kinds and job status sets."""

from __future__ import annotations

from app.core.models import (
    ACTIVE_JOB_STATUSES,
    FINAL_JOB_STATUSES,
    JobStatus,
    ResolvedEntity,
)


def _entity(kind: str, **kwargs: object) -> ResolvedEntity:
    return ResolvedEntity(id=1, kind=kind, title="T", **kwargs)  # type: ignore[arg-type]


def test_is_groupish_true_for_group_kinds() -> None:
    assert _entity("chat").is_groupish
    assert _entity("supergroup").is_groupish
    assert _entity("channel", is_megagroup=True).is_groupish


def test_is_groupish_false_for_broadcast_and_user() -> None:
    assert not _entity("channel", is_broadcast=True, is_megagroup=False).is_groupish
    assert not _entity("user").is_groupish


def test_final_job_statuses_contents() -> None:
    assert FINAL_JOB_STATUSES == {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    }


def test_active_job_statuses_contents() -> None:
    assert ACTIVE_JOB_STATUSES == {
        JobStatus.CREATED,
        JobStatus.VALIDATING,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
    }
    assert ACTIVE_JOB_STATUSES.isdisjoint(FINAL_JOB_STATUSES)
