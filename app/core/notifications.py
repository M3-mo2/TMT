"""System notification service (docs/notifications/NotificationSystem.md).

Listens to :class:`~app.core.events.SystemEvent` on the shared ``EventBus``,
persists each event as a row in ``notifications`` (one per admin so read-state
is per-user), and DMed's every admin whose per-type setting is enabled.

Layer rule (RULES §1): ``core/`` may import ``db/`` and ``app.core.*`` but
not ``app.bot/`` or ``app.tg/``.  A pre-rendered Arabic title/body travels
inside the ``SystemEvent`` so this module never touches ``texts.py``.  The
``bot`` is injected per-call (``start(bot)`` / ``notify(...)``) following the
:class:`~app.core.broadcast.Broadcaster` pattern, because ``Bot`` does not
exist until ``main.py`` creates it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.config import Config
from app.core.events import EventBus, SystemEvent
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger(__name__)

__all__ = ["NotificationService"]


class NotificationService:
    """In-process system-event → admin-notification bridge.

    Subscribes to ``SystemEvent`` on the bus.  For each event it:

    1. Looks up the per-admin ``notification_settings`` toggle for the event
       type (missing = enabled, fail-open so operators always see new types).
    2. Writes a ``notifications`` row per enabled admin (so each admin has
       independent read/dismiss state in the admin panel).
    3. Sends a Telegram DM to each enabled admin (best-effort — a send
       failure is logged and the row keeps ``delivered=0``).

    Rate limiting / debouncing is intentionally **not** applied to
    ``user_joined`` or ``error`` events (operators want to see those in real
    time), but the DB row itself acts as a natural throttle for the admin
    panel's live-updating inbox.
    """

    def __init__(self, db: Database, config: Config, bus: EventBus) -> None:
        self._db = db
        self._config = config
        self._bus = bus
        self._unsubscribe: list[Callable[[], None]] = []
        self._bot: Any = None

    # ------------------------------------------------------------------ lifecycle

    def subscribe(self) -> None:
        """Attach the system-event handler to the bus."""
        self._unsubscribe.append(self._bus.subscribe(self._on_system_event))

    def unsubscribe(self) -> None:
        """Detach all handlers (for clean shutdown / tests)."""
        for fn in self._unsubscribe:
            fn()
        self._unsubscribe.clear()

    def set_bot(self, bot: Any) -> None:
        """Inject the Bot instance (created in main.py after the dispatcher)."""
        self._bot = bot

    # ------------------------------------------------------------------ handler

    async def _on_system_event(self, event: Any) -> None:
        if not isinstance(event, SystemEvent):
            return
        # Global config kill-switch: if the event type is entirely disabled at
        # the config level, skip everything (DB write + DM).
        if not self._config_enabled(event.event_type):
            return

        admin_ids = self._config.admin_id_list
        if not admin_ids:
            logger.info(
                "system event '%s' but no admin IDs configured — skipping DM",
                event.event_type,
            )
            return

        for admin_id in admin_ids:
            enabled = await repo.is_notification_enabled(self._db, admin_id, event.event_type)
            if not enabled:
                continue
            # Persist a row for the admin panel.
            await repo.create_notification(
                self._db,
                owner_id=admin_id,
                event_type=event.event_type,
                severity=event.severity,
                title=event.title,
                body=event.body,
                data=event.data,
            )
            # Best-effort DM.
            if self._bot is not None:
                await self._try_dm(admin_id, event)

    # ------------------------------------------------------------------ DM

    async def _try_dm(self, admin_id: int, event: SystemEvent) -> None:
        """Send a DM to *admin_id*; log and continue on any failure."""
        if self._bot is None:
            return
        text = (
            f"<b>{event.title}</b>\n\n{event.body}"
            if event.body
            else f"<b>{event.title}</b>"
        )
        try:
            await self._bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                disable_notification=event.severity != "error",
            )
        except Exception:
            logger.warning(
                "notification DM failed for admin %d (event=%s)",
                admin_id, event.event_type, exc_info=True,
            )

    # ------------------------------------------------------------------ config

    def _config_enabled(self, event_type: str) -> bool:
        """Global config gate for notification categories."""
        if event_type == "user_joined":
            return self._config.notify_on_user_join
        if event_type in (
            "job_started", "job_completed", "job_failed",
            "job_cancelled", "job_interrupted",
        ):
            return self._config.notify_on_job_events
        if event_type in ("flood_wait", "peer_flood"):
            return self._config.notify_on_error
        if event_type in (
            "broadcast_started", "broadcast_completed",
            "broadcast_failed",
        ):
            return self._config.notify_on_broadcast_events
        if event_type.startswith("backup_"):
            return self._config.notify_on_backup_events
        # account_added, account_removed, account_unauthorized — always on
        # (these are security-relevant and small in volume)
        return True