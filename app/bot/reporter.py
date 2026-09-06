"""Job progress reporter: bus events -> throttled live Telegram card (PRD §14).

The engine awaits ``on_progress`` inline, so every handler here is defensive:
a reporting failure is logged and never allowed to kill a job.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from aiogram import Bot

from app.bot.keyboards import job_detail
from app.bot.texts import PARSE_MODE, render_final_summary, render_progress_card
from app.config import Config
from app.core.events import EventBus, JobFinishedEvent, JobProgressEvent
from app.core.models import Job, JobStatus

logger = logging.getLogger(__name__)

__all__ = ["Reporter"]


def _card_job(job_id: int) -> Job:
    """Minimal Job view so a live card can carry a cancel button without
    touching the DB (cancel re-verifies ownership in JobManager)."""
    return Job(
        id=job_id, owner_id=0, account_id=None, source_ref="", dest_ref="",
        status=JobStatus.RUNNING,
    )


class Reporter:
    """One live progress message per job, throttled per
    ``config.progress_edit_min_interval``; phase changes and FloodWait
    countdowns always edit immediately."""

    def __init__(self, bot: Any, bus: EventBus, config: Config) -> None:
        self._bot: Any = bot
        self._bus = bus
        self._config = config
        self._cards: dict[int, tuple[int, int]] = {}  # job_id -> (chat_id, message_id)
        self._last_edit: dict[int, float] = {}
        self._last_phase: dict[int, str] = {}
        self._unsubscribers: list[Callable[[], None]] = []

    # -- lifecycle ---------------------------------------------------------------

    async def register(self, job_id: int, chat_id: int) -> None:
        """Send the initial progress card and remember where it lives."""
        message = await self._bot.send_message(
            chat_id,
            render_progress_card(job_id, phase="", done=0, total=0, invited=0,
                                 skipped=0, failed=0),
            parse_mode=PARSE_MODE,
            reply_markup=job_detail(_card_job(job_id)),
        )
        self._cards[job_id] = (chat_id, message.message_id)
        self._last_phase.pop(job_id, None)
        self._last_edit.pop(job_id, None)

    def subscribe(self) -> None:
        """Attach the progress/finished handlers to the event bus."""
        self._unsubscribers.append(self._bus.subscribe(self._on_progress))
        self._unsubscribers.append(self._bus.subscribe(self._on_finished))

    def unregister(self, job_id: int) -> None:
        """Drop the card mapping; later events for the job are ignored."""
        self._cards.pop(job_id, None)
        self._last_edit.pop(job_id, None)
        self._last_phase.pop(job_id, None)

    # -- handlers -----------------------------------------------------------------

    async def _on_progress(self, event: JobProgressEvent) -> None:
        if not isinstance(event, JobProgressEvent):
            return  # the bus broadcasts every event to every subscriber
        try:
            card = self._cards.get(event.job_id)
            if card is None:
                return  # unknown/unregistered job: ignore
            chat_id, message_id = card
            now = time.monotonic()
            phase_changed = event.phase != self._last_phase.get(event.job_id)
            self._last_phase[event.job_id] = event.phase
            due = (
                phase_changed
                or event.wait_left > 0
                or now - self._last_edit.get(event.job_id, 0.0)
                >= self._config.progress_edit_min_interval
            )
            if not due:
                return
            self._last_edit[event.job_id] = now
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=render_progress_card(
                    event.job_id, event.phase, event.done, event.total,
                    event.invited, event.skipped, event.failed,
                    event.wait_left, event.note,
                ),
                parse_mode=PARSE_MODE,
                reply_markup=job_detail(_card_job(event.job_id)),
            )
        except Exception:
            logger.warning(
                "progress edit failed for job %s", event.job_id, exc_info=True
            )

    async def _on_finished(self, event: JobFinishedEvent) -> None:
        if not isinstance(event, JobFinishedEvent):
            return  # the bus broadcasts every event to every subscriber
        try:
            card = self._cards.pop(event.job_id, None)
            self._last_edit.pop(event.job_id, None)
            self._last_phase.pop(event.job_id, None)
            if card is None:
                return
            chat_id, message_id = card
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=render_final_summary(
                    event.job_id, event.status, event.invited, event.skipped,
                    event.failed, event.error,
                ),
                parse_mode=PARSE_MODE,
            )
        except Exception:
            logger.warning(
                "final summary edit failed for job %s", event.job_id, exc_info=True
            )
