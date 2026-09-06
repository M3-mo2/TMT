"""Reporter: throttled progress cards and final summaries (fake bot, offline)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.bot.reporter import Reporter
from app.config import Config
from app.core.events import EventBus, JobFinishedEvent, JobProgressEvent
from app.core.models import JobStatus


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self._next_id = 100
        self.fail_edits = False

    async def send_message(self, chat_id: int, text: str, **kwargs):
        message = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        self.sent.append((chat_id, text))
        return message

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs):
        if self.fail_edits:
            raise RuntimeError("telegram down")
        self.edits.append((chat_id, message_id, text))


@pytest.fixture
def config() -> Config:
    return Config(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        progress_edit_min_interval=60.0,  # only phase/wait changes edit promptly
    )


def _progress(job_id: int = 1, phase: str = "inviting", done: int = 5, wait_left: int = 0) -> JobProgressEvent:
    return JobProgressEvent(
        job_id=job_id, phase=phase, done=done, total=100,
        invited=3, skipped=1, failed=0, wait_left=wait_left,
    )


async def test_register_sends_initial_card(config: Config) -> None:
    bot = FakeBot()
    reporter = Reporter(bot, EventBus(), config)
    await reporter.register(1, chat_id=55)
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 55
    assert "#1" in text


async def test_throttle_edits_by_interval_but_phase_waits_are_immediate(config: Config) -> None:
    bot = FakeBot()
    bus = EventBus()
    reporter = Reporter(bot, bus, config)
    reporter.subscribe()
    await reporter.register(1, chat_id=55)

    # First event counts as a phase change (register cleared phase memory):
    # edits immediately.
    await bus.publish(_progress(phase="inviting", done=5))
    assert len(bot.edits) == 1

    # Same phase, within the (huge) interval: NOT edited.
    await bus.publish(_progress(phase="inviting", done=6))
    assert len(bot.edits) == 1

    # Same phase, wait_left > 0: edits immediately (FloodWait countdown).
    await bus.publish(_progress(phase="waiting", done=6, wait_left=42))
    assert len(bot.edits) == 2

    # New phase: edits immediately.
    await bus.publish(_progress(phase="inviting", done=9))
    assert len(bot.edits) == 3

    # Same phase again within interval: still throttled.
    await bus.publish(_progress(phase="inviting", done=12))
    assert len(bot.edits) == 3

    chat_id, message_id, text = bot.edits[-1]
    assert chat_id == 55 and message_id == 100
    assert "9/100" in text
    assert "42" in bot.edits[1][2]  # the FloodWait countdown card


async def test_finished_edits_summary_and_unregisters(config: Config) -> None:
    bot = FakeBot()
    bus = EventBus()
    reporter = Reporter(bot, bus, config)
    reporter.subscribe()
    await reporter.register(1, chat_id=55)
    await bus.publish(
        JobFinishedEvent(job_id=1, status=JobStatus.COMPLETED, invited=9, skipped=2, failed=0, error=None)
    )
    assert len(bot.edits) == 1
    assert "✨|العمليه تمت" in bot.edits[0][2]

    # Later events for the finished job are ignored.
    await bus.publish(_progress(job_id=1))
    assert len(bot.edits) == 1


async def test_wrong_event_type_does_not_destroy_the_card(config: Config) -> None:
    """The bus broadcasts every event to every subscriber: a JobProgressEvent
    arriving at the finished-handler must not pop the card mapping
    (regression: the live card died after the first progress event)."""
    bot = FakeBot()
    bus = EventBus()
    reporter = Reporter(bot, bus, config)
    reporter.subscribe()
    await reporter.register(1, chat_id=55)
    await bus.publish(_progress(job_id=1))  # also hits _on_finished
    assert 1 in reporter._cards  # card survives

    await bus.publish(
        JobFinishedEvent(job_id=1, status=JobStatus.COMPLETED, invited=1, skipped=0, failed=0, error=None)
    )  # also hits _on_progress
    assert 1 not in reporter._cards  # finished still unregisters


async def test_unknown_job_events_are_ignored(config: Config) -> None:
    bot = FakeBot()
    bus = EventBus()
    reporter = Reporter(bot, bus, config)
    reporter.subscribe()
    await bus.publish(_progress(job_id=404))
    await bus.publish(
        JobFinishedEvent(job_id=404, status=JobStatus.FAILED, invited=0, skipped=0, failed=0, error="x")
    )
    assert bot.sent == [] and bot.edits == []


async def test_failing_edit_never_raises(config: Config, caplog) -> None:
    bot = FakeBot()
    bot.fail_edits = True
    bus = EventBus()
    reporter = Reporter(bot, bus, config)
    reporter.subscribe()
    await reporter.register(1, chat_id=55)
    with caplog.at_level(logging.WARNING, logger="app.bot.reporter"):
        await bus.publish(_progress())  # must not raise
    assert any("progress edit failed" in r.message for r in caplog.records)
