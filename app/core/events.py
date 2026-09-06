"""In-process async event bus for job progress (PRD §12, §14).

Job events flow engine → JobManager → :class:`EventBus` → bot-side reporter.
Handlers must be fast and non-blocking; a slow subscriber delays the engine's
``on_progress`` callback (it is awaited inline), so heavy work belongs in the
subscriber's own task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core.models import JobStatus

logger = logging.getLogger(__name__)

__all__ = ["EventBus", "JobFinishedEvent", "JobProgressEvent"]

Handler = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobProgressEvent:
    job_id: int
    phase: str
    done: int
    total: int
    invited: int
    skipped: int
    failed: int
    wait_left: int = 0
    note: str = ""


@dataclass(frozen=True, slots=True)
class JobFinishedEvent:
    job_id: int
    status: JobStatus
    invited: int
    skipped: int
    failed: int
    error: str | None


class EventBus:
    """Minimal async pub/sub.

    - Handler exceptions are logged, never propagated to the publisher.
    - Publishing with no subscribers is a no-op.
    - ``subscribe`` returns an idempotent unsubscribe function.
    """

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, cb: Handler) -> Callable[[], None]:
        self._handlers.append(cb)

        def unsubscribe() -> None:
            try:
                self._handlers.remove(cb)
            except ValueError:
                pass  # already unsubscribed; unsubscribe stays idempotent

        return unsubscribe

    async def publish(self, event: Any) -> None:
        for handler in list(self._handlers):
            try:
                await handler(event)
            except Exception:
                logger.exception("event handler failed for %r", event)
