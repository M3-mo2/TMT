"""Tests for app.core.events: pub/sub, unsubscribe, handler isolation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from app.core.events import EventBus, JobFinishedEvent, JobProgressEvent
from app.core.models import JobStatus


async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBus()
    await bus.publish(JobProgressEvent(job_id=1, phase="inviting", done=1, total=2,
                                       invited=1, skipped=0, failed=0))


async def test_subscribe_and_publish_delivers_event() -> None:
    bus = EventBus()
    seen: list[Any] = []

    async def handler(event: Any) -> None:
        seen.append(event)

    bus.subscribe(handler)
    event = JobFinishedEvent(job_id=7, status=JobStatus.COMPLETED, invited=1,
                             skipped=0, failed=0, error=None)
    await bus.publish(event)
    assert seen == [event]


async def test_unsubscribe_stops_delivery_and_is_idempotent() -> None:
    bus = EventBus()
    seen: list[Any] = []

    async def handler(event: Any) -> None:
        seen.append(event)

    unsubscribe = bus.subscribe(handler)
    unsubscribe()
    unsubscribe()  # idempotent
    await bus.publish(JobFinishedEvent(job_id=1, status=JobStatus.FAILED, invited=0,
                                       skipped=0, failed=0, error="x"))
    assert seen == []


async def test_handler_exception_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    called: list[str] = []

    async def bad(_event: Any) -> None:
        raise RuntimeError("subscriber blew up")

    async def good(_event: Any) -> None:
        called.append("good")

    bus.subscribe(bad)
    bus.subscribe(good)
    with caplog.at_level(logging.ERROR, logger="app.core.events"):
        await bus.publish(JobProgressEvent(job_id=1, phase="inviting", done=0, total=0,
                                           invited=0, skipped=0, failed=0))
    assert called == ["good"]  # later subscribers still ran
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records and error_records[0].exc_info is not None


async def test_unsubscribing_inside_publish_does_not_confuse_iteration() -> None:
    bus = EventBus()
    seen: list[Any] = []

    unsubscribe: Callable[[], None]

    async def first(event: Any) -> None:
        seen.append("first")
        unsubscribe()  # mutate while publishing

    unsubscribe = bus.subscribe(first)

    async def second(event: Any) -> None:
        seen.append("second")

    bus.subscribe(second)
    event = JobProgressEvent(job_id=1, phase="inviting", done=0, total=0,
                             invited=0, skipped=0, failed=0)
    await bus.publish(event)
    assert seen == ["first", "second"]
    await bus.publish(event)  # first is gone
    assert seen == ["first", "second", "second"]
