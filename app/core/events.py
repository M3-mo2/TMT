"""In-process async event bus for job progress (PRD §12, §14).

Job events flow engine → JobManager → :class:`EventBus` → bot-side reporter.
Handlers must be fast and non-blocking; a slow subscriber delays the engine's
``on_progress`` callback (it is awaited inline), so heavy work belongs in the
subscriber's own task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.models import JobStatus

logger = logging.getLogger(__name__)

__all__ = [
    "EventBus",
    "JobFinishedEvent",
    "JobProgressEvent",
    "SystemEvent",
    "SYSTEM_EVENTS",
]

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


@dataclass(frozen=True, slots=True)
class SystemEvent:
    """A system-level event that should be surfaced to the bot operator.

    Published by any layer (middleware, services, job manager) via the shared
    ``EventBus``.  The :class:`~app.core.notifications.NotificationService`
    subscribes, persists a row in ``notifications``, and DMed's every admin.

    ``event_type`` is a short snake-case identifier — see :data:`SYSTEM_EVENTS`
    for the canonical list.  ``data`` carries event-specific metadata (e.g. the
    new user's id) so the handler can render a useful message.
    """

    event_type: str
    severity: str = "info"          # info | warning | error
    title: str = ""                  # short, Arabic, user-facing (pre-rendered by caller)
    body: str = ""                   # longer Arabic detail (pre-rendered)
    data: dict[str, Any] = field(default_factory=dict)


#: Canonical set of system event types (the NotificationService renders these
#: with pre-built Arabic text; unknown types fall through to a generic handler).
SYSTEM_EVENTS: frozenset[str] = frozenset(
    {
        "user_joined",          # new Telegram user started the bot
        "account_added",        # a user added a Telegram account
        "account_removed",      # a user removed a Telegram account
        "account_unauthorized", # an account went unauthorized mid-job
        "job_started",          # a transfer job began running
        "job_completed",        # a transfer job finished successfully
        "job_failed",           # a transfer job failed
        "job_cancelled",        # a transfer job was cancelled by the user
        "job_interrupted",      # a job was marked interrupted on boot recovery
        "broadcast_started",    # an admin broadcast campaign began
        "broadcast_completed",  # a broadcast campaign finished
        "broadcast_failed",     # a broadcast campaign failed
        "backup_started",       # a backup snapshot began running
        "backup_completed",     # a backup snapshot finished successfully
        "backup_failed",        # a backup snapshot failed
        "backup_scheduled",     # a backup was scheduled for a future time
        "backup_cancelled",     # a backup was cancelled by the admin
        "backup_restored",      # an admin restored a backup over the live DB
        "flood_wait",           # a FloodWait was hit (rate-limit alert)
        "peer_flood",           # an account was PeerFlood-limited
    }
)
