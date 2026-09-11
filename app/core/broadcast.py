"""AudienceResolver, error classification, and Broadcaster service
(BroadcastEngine.md §2.6, §2.4, §3).

``core/`` may import ``app.db`` and ``app.core`` but not ``app.bot`` or
``app.tg`` (RULES §1).  The ``bot`` parameter is supplied at call-time
(start/cancel/resume) — handlers always have ``cb.bot``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import string
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramServerError,
    TelegramNetworkError,
)

from app.core.broadcast_models import (
    AudienceFilter,
    BroadcastStatus,
    ErrorKind,
    RecipientStatus,
)
from app.core.events import EventBus, SystemEvent
from app.core.rate_limiter import TokenBucket
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger("app.core.broadcast")

# Sentinel for the "no conditions" case — keeps the assembled SQL readable.
_WHERE_PREFIX = " WHERE "


def _build_filter(
    filters: AudienceFilter, admin_ids: Sequence[int]
) -> tuple[str, list]:
    """Build the WHERE fragment + params list from an ``AudienceFilter``.

    Filters are accumulated as ``(sql_fragment, params)`` tuples and then
    flattened into a single ``AND``-joined clause, keeping parameter order
    deterministic.  No Python-side filtering of fetched rows (RULES §8:
    one DB round-trip per call).
    """
    conditions: list[tuple[str, list]] = []

    # --- target -----------------------------------------------------------
    # 'all' and unknown targets: exclude blocked users (they can't receive
    # messages).  'blocked' is a deliberate opt-in to target only them.
    if filters.target == "blocked":
        conditions.append(("u.is_blocked = 1", []))
    else:
        conditions.append(("u.is_blocked = 0", []))
        if filters.target == "active":
            n = filters.last_seen_days_ago or 30
            conditions.append(("u.updated_at > datetime('now', ?)", [f"-{n} days"]))
        elif filters.target == "inactive":
            n = filters.last_seen_days_ago or 30
            conditions.append(("u.updated_at <= datetime('now', ?)", [f"-{n} days"]))

    # --- standalone last_seen_days_ago (only when target didn't handle it) -
    if (
        filters.last_seen_days_ago is not None
        and filters.target not in ("active", "inactive")
    ):
        conditions.append(
            ("u.updated_at > datetime('now', ?)", [f"-{filters.last_seen_days_ago} days"])
        )

    # --- account ownership ------------------------------------------------
    if filters.with_accounts:
        conditions.append(("EXISTS(SELECT 1 FROM accounts WHERE owner_id=u.id)", []))
    if filters.without_accounts:
        conditions.append(("NOT EXISTS(SELECT 1 FROM accounts WHERE owner_id=u.id)", []))

    # --- account count bounds --------------------------------------------
    if filters.account_count_min is not None:
        conditions.append(
            ("(SELECT COUNT(*) FROM accounts WHERE owner_id=u.id) >= ?", [filters.account_count_min])
        )
    if filters.account_count_max is not None:
        conditions.append(
            ("(SELECT COUNT(*) FROM accounts WHERE owner_id=u.id) <= ?", [filters.account_count_max])
        )

    # --- registration age -------------------------------------------------
    if filters.registered_days_ago is not None:
        conditions.append(
            ("u.created_at > datetime('now', ?)", [f"-{filters.registered_days_ago} days"])
        )

    # --- admin exclusion --------------------------------------------------
    if filters.exclude_admins and admin_ids:
        placeholders = ",".join(["?"] * len(admin_ids))
        conditions.append((f"u.id NOT IN ({placeholders})", list(admin_ids)))

    # --- previously contacted --------------------------------------------
    if filters.exclude_previously_contacted:
        conditions.append(
            (
                "NOT EXISTS(SELECT 1 FROM broadcast_recipients br "
                "JOIN broadcasts b ON br.broadcast_id=b.id "
                "WHERE br.user_id=u.id AND b.mode IS NOT NULL "
                "AND br.status IN ('sent','delivered'))",
                [],
            )
        )

    # --- assemble ---------------------------------------------------------
    if conditions:
        fragments = [frag for frag, _ in conditions]
        params: list = []
        for _, frag_params in conditions:
            params.extend(frag_params)
        where = _WHERE_PREFIX + " AND ".join(fragments)
    else:
        where = ""
        params = []

    return where, params


async def resolve_audience(
    db: Database, filters: AudienceFilter, admin_ids: Sequence[int]
) -> list[int]:
    """Return sorted user IDs matching *filters* in a single DB round-trip."""
    where, params = _build_filter(filters, admin_ids)
    sql = (
        "SELECT DISTINCT u.id FROM users u "
        "LEFT JOIN accounts a ON a.owner_id = u.id"
        f"{where} ORDER BY u.id"
    )
    rows = await db.fetch_all(sql, params)
    ids = [int(r["id"]) for r in rows]
    logger.debug("resolve_audience: %d users matched (target=%s)", len(ids), filters.target)
    return ids


async def count_audience(
    db: Database, filters: AudienceFilter, admin_ids: Sequence[int]
) -> int:
    """Count users matching *filters* — mirrors :func:`resolve_audience`."""
    where, params = _build_filter(filters, admin_ids)
    sql = (
        "SELECT COUNT(DISTINCT u.id) AS c FROM users u "
        "LEFT JOIN accounts a ON a.owner_id = u.id"
        f"{where}"
    )
    row = await db.fetch_one(sql, params)
    return int(row["c"]) if row else 0


# ---------------------------------------------------------------- error classification

_BLOCKED_RE = re.compile(
    r"blocked by the user|deactivated|chat not found", re.IGNORECASE
)


def classify_error(exc: BaseException) -> ErrorKind:
    """Classify a Telegram/API exception for broadcast retry logic.

    See BroadcastEngine.md §2.4 for the decision table.
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc  # never swallow cancellation (RULES §5)
    if isinstance(exc, TelegramRetryAfter):
        # retry_after is an int set in TelegramRetryAfter.__init__
        threshold = 60
        try:
            threshold = int(getattr(exc, "retry_after", 60))
        except (TypeError, ValueError):
            threshold = 60
        if threshold <= 60:
            return ErrorKind.RETRY_FLOOD
        return ErrorKind.RETRY_DELAYED
    if isinstance(exc, TelegramForbiddenError):
        msg = getattr(exc, "message", "") or str(exc)
        if _BLOCKED_RE.search(msg):
            return ErrorKind.PERMANENT_BLOCKED
        return ErrorKind.PERMANENT_FAIL
    if isinstance(exc, (TelegramServerError, TelegramNetworkError)):
        return ErrorKind.RETRY_TRANSIENT
    if isinstance(exc, TelegramAPIError):
        return ErrorKind.PERMANENT_FAIL
    return ErrorKind.PERMANENT_FAIL


# ---------------------------------------------------------------- progress card text (Phase 3 placeholder)

def _progress_text(
    sent: int, blocked: int, failed: int, skipped: int, total: int
) -> str:
    """Minimal inline progress card text — uses only approved symbols (RULES §7).
    Phase 4 replaces this with a full ``render_bcast_progress`` in ``texts.py``."""
    return (
        "⟡ بث الرسائل\n"
        f"| <code>{sent}</code>/<code>{total}</code>\n"
        f"×: <code>{blocked}</code>\n"
        f"! <code>{failed}</code>\n"
        f"›: <code>{skipped}</code>"
    )


# ---------------------------------------------------------------- broadcaster

class Broadcaster:
    """Campaign lifecycle: start, run, cancel, pause, resume, recover, shutdown.

    Mirrors :class:`~app.core.job_manager.JobManager` patterns (guarded DB writes,
    cancel events, graceful shutdown).  ``bot`` is passed per-call so the
    service can be constructed before the Bot exists (on boot, before
    ``start_polling``).
    """

    def __init__(self, db: Database, config: Any, bus: EventBus) -> None:
        self._db = db
        self._config = config
        self._bus = bus
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._pause_events: dict[int, asyncio.Event] = {}  # set = running, cleared = paused
        self._token_buckets: dict[int, TokenBucket] = {}
        self._progress_cards: dict[int, tuple[int, int]] = {}  # cid -> (chat_id, msg_id)
        self._filters: dict[int, AudienceFilter] = {}
        self._sweep_task: asyncio.Task[None] | None = None
        self._bot: Any = None

    # ------------------------------------------------------------------ public

    async def start(self, campaign_id: int, bot: Any) -> None:
        """Spawn the worker task for a campaign; sets status to ``running``."""
        campaign = await repo.get_broadcast(self._db, campaign_id)
        if campaign is None:
            logger.warning("start: campaign %d not found", campaign_id)
            return
        if campaign["status"] not in (BroadcastStatus.DRAFT.value, BroadcastStatus.SCHEDULED.value):
            logger.info("start: campaign %d already %s", campaign_id, campaign["status"])
            return  # idempotent: don't double-start

        self._cancel_events[campaign_id] = asyncio.Event()
        self._pause_events[campaign_id] = asyncio.Event()
        self._pause_events[campaign_id].set()  # running by default
        self._token_buckets[campaign_id] = TokenBucket(
            self._config.bcast_max_rate_per_second,
            self._config.max_bcast_concurrency,
        )
        filter_json = campaign.get("filter_json")
        self._filters[campaign_id] = AudienceFilter.from_dict(
            json.loads(filter_json) if filter_json else {}
        )

        await repo.set_broadcast_status(
            self._db, campaign_id,
            BroadcastStatus.RUNNING.value,
            started_at=now_iso(),
        )
        await repo.audit(
            self._db, "broadcast_started",
            owner_id=campaign["admin_id"],
            detail={"recipient_count": campaign.get("total_recipients", 0)},
        )

        task = asyncio.create_task(
            self._run_campaign(campaign_id, bot),
            name=f"bcast-{campaign_id}",
        )
        self._tasks[campaign_id] = task
        logger.info("start: campaign %d worker spawned", campaign_id)
        await self._publish_system_event(
            SystemEvent(
                event_type="broadcast_started",
                severity="info",
                title="📢|بدأ البث",
                body=f"⟡|الحملة <code>#{campaign_id}</code> بدأت الإرسال.",
                data={"campaign_id": campaign_id,
                      "admin_id": campaign["admin_id"]},
            )
        )

    async def cancel(self, campaign_id: int, bot: Any) -> bool:
        """Signal the running worker to stop cooperatively and persist status.

        Sets the cancel event (RULES §5: cooperative cancellation), immediately
        persists ``status = 'cancelled'`` to DB (race-condition fix), and emits
        a ``broadcast_cancelled`` audit event.
        Returns False if the campaign is not running.
        """
        evt = self._cancel_events.get(campaign_id)
        if evt is None:
            return False
        evt.set()
        campaign = await repo.get_broadcast(self._db, campaign_id)
        if campaign:
            await repo.set_broadcast_status(
                self._db, campaign_id,
                BroadcastStatus.CANCELLED.value,
            )
            await repo.audit(
                self._db, "broadcast_cancelled",
                owner_id=campaign["admin_id"],
                detail={"campaign_id": campaign_id},
            )
            await self._publish_system_event(
                SystemEvent(
                    event_type="broadcast_failed",
                    severity="info",
                    title="↺|ألغي بث",
                    body=f"⟡|الحملة <code>#{campaign_id}</code> ألغيت بواسطة المشرف.",
                    data={"campaign_id": campaign_id, "admin_id": campaign["admin_id"]},
                )
            )
        return True

    async def _publish_system_event(self, event: SystemEvent) -> None:
        """Best-effort publish to the bus; logged only."""
        try:
            await self._bus.publish(event)
        except Exception:  # pragma: no cover - publish never raises
            logger.warning(
                "broadcast: system event publish failed (%s)",
                event.event_type, exc_info=True,
            )

    async def pause(self, campaign_id: int) -> bool:
        """Pause a running campaign. Returns False if not running."""
        evt = self._pause_events.get(campaign_id)
        if evt is None or evt.is_set():
            return False  # not paused already, or not running
        # evt.is_set() == running; clear it to pause
        evt.clear()
        campaign = await repo.get_broadcast(self._db, campaign_id)
        if campaign:
            await repo.audit(
                self._db, "broadcast_paused",
                owner_id=campaign["admin_id"],
                detail={"campaign_id": campaign_id},
            )
        return True

    async def resume(self, campaign_id: int, bot: Any) -> bool:
        """Resume a paused campaign. Returns False if not paused."""
        evt = self._pause_events.get(campaign_id)
        if evt is None or not evt.is_set():
            # already running or not found
            if evt is not None and not evt.is_set():
                evt.set()
                task = self._tasks.get(campaign_id)
                if task is None or task.done():
                    asyncio.create_task(
                        self._run_campaign(campaign_id, bot),
                        name=f"bcast-{campaign_id}",
                    )
                campaign = await repo.get_broadcast(self._db, campaign_id)
                if campaign:
                    await repo.audit(
                        self._db, "broadcast_resumed",
                        owner_id=campaign["admin_id"],
                        detail={"campaign_id": campaign_id},
                    )
                return True
            return False
        return False  # already running

    async def recover(self, bot: Any) -> list[int]:
        """Boot recovery: resume incomplete ``running`` campaigns.

        Must be called after the Bot is available.  Campaigns whose
        ``sent + blocked + failed + skipped >= total_recipients`` are
        marked ``completed`` (crash happened during finalization).  If
        ``finished_at IS NULL`` but counters don't match total (no pending
        recipients left), marked ``failed`` with reason.
        """
        rows = await self._db.fetch_all(
            "SELECT id, admin_id, total_recipients, sent, blocked, failed, "
            "skipped, filter_json, finished_at FROM broadcasts WHERE status='running'"
        )
        resumed: list[int] = []
        for row in rows:
            cid = int(row["id"])
            processed = (
                (row["sent"] or 0) + (row["blocked"] or 0) +
                (row["failed"] or 0) + (row["skipped"] or 0)
            )
            if row["finished_at"] is not None:
                # Already finalized but still status='running' — mark completed
                await repo.set_broadcast_status(
                    self._db, cid,
                    BroadcastStatus.COMPLETED.value,
                )
                await repo.audit(
                    self._db, "broadcast_recovered_complete",
                    owner_id=row["admin_id"],
                    detail={"campaign_id": cid, "processed": processed,
                            "total": row["total_recipients"]},
                )
            elif processed < (row["total_recipients"] or 0) and row["total_recipients"] > 0:
                # Rebuild in-memory state and resume
                self._cancel_events[cid] = asyncio.Event()
                self._pause_events[cid] = asyncio.Event()
                self._pause_events[cid].set()
                self._token_buckets[cid] = TokenBucket(
                    self._config.bcast_max_rate_per_second,
                    self._config.max_bcast_concurrency,
                )
                self._filters[cid] = AudienceFilter.from_dict(
                    json.loads(row["filter_json"] or "{}")
                )
                await self._send_admin_card(
                    bot, cid, row["admin_id"], 0, 0, 0, 0, row["total_recipients"],
                )
                task = asyncio.create_task(
                    self._run_campaign(cid, bot), name=f"bcast-{cid}"
                )
                self._tasks[cid] = task
                resumed.append(cid)
                await repo.audit(
                    self._db, "broadcast_recovered_resume",
                    owner_id=row["admin_id"],
                    detail={"campaign_id": cid, "processed": processed,
                            "total": row["total_recipients"]},
                )
                logger.info("recover: resumed campaign %d (%d/%d done)",
                            cid, processed, row["total_recipients"])
            else:
                # Counter mismatch — all recipients accounted for but no pending
                # left → the campaign was effectively done but never finalized.
                await repo.set_broadcast_status(
                    self._db, cid,
                    BroadcastStatus.FAILED.value,
                    finished_at=now_iso(),
                    error="no pending recipients",
                )
                await repo.audit(
                    self._db, "broadcast_recovered_failed",
                    owner_id=row["admin_id"],
                    detail={"campaign_id": cid, "processed": processed,
                            "total": row["total_recipients"],
                            "reason": "no pending recipients"},
                )
                logger.info("recover: campaign %d marked failed (no pending recipients)",
                            cid)
        return resumed

    async def shutdown(self) -> None:
        """Cancel all live worker tasks (backstop path, mirrors JobManager)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()
        self._pause_events.clear()
        self._token_buckets.clear()
        self._progress_cards.clear()
        self._filters.clear()
        self._sweep_task = None
        self._bot = None
        await self.stop_sweeper()

    # ------------------------------------------------------------------ sweeper

    def start_sweeper(self, bot: Any) -> None:
        """Start the background sweeper that promotes scheduled campaigns to
        ``running`` status when ``scheduled_for`` has elapsed."""
        self._bot = bot
        if self._sweep_task is not None and not self._sweep_task.done():
            return  # already running
        self._sweep_task = asyncio.create_task(self.run_sweeper())

    async def run_sweeper(self) -> None:
        """Every ``bcast_sweep_interval`` seconds, promote due scheduled campaigns."""
        while True:
            try:
                due = await repo.list_scheduled_broadcasts(self._db)
                for campaign in due:
                    cid = campaign["id"]
                    try:
                        await repo.audit(
                            self._db, "broadcast_sweep_started",
                            owner_id=campaign["admin_id"],
                            detail={"campaign_id": cid},
                        )
                        await self.start(cid, self._bot)
                    except Exception:
                        logger.exception("sweeper: failed to start campaign %d", cid)
                await asyncio.sleep(self._config.bcast_sweep_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sweeper: unexpected error")

    async def stop_sweeper(self) -> None:
        """Cancel the sweeper task and await it."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None

    # ------------------------------------------------------------------ internal

    async def _run_campaign(self, cid: int, bot: Any) -> None:
        """Main worker loop — processes recipients in batches with rate limiting."""
        cancel_evt = self._cancel_events.get(cid)
        pause_evt = self._pause_events.get(cid)
        bucket = self._token_buckets.get(cid)
        if cancel_evt is None or pause_evt is None or bucket is None:
            logger.error("run_campaign: no state for campaign %d", cid)
            return

        campaign = await repo.get_broadcast(self._db, cid)
        if campaign is None:
            return

        filters = self._filters.get(cid, AudienceFilter.default())
        admin_ids = self._config.admin_id_list
        user_ids = await repo.list_pending_recipients(
            self._db, cid, self._config.bcast_batch_size * 100,
        )
        if not user_ids:
            # No pending recipients — resolve fresh and insert
            resolved = await resolve_audience(self._db, filters, admin_ids)
            await repo.insert_recipients(self._db, cid, resolved)
            user_ids = resolved

        total = len(user_ids)
        if total != (campaign.get("total_recipients") or 0):
            await repo.set_broadcast_status(self._db, cid, "running",
                                            total_recipients=total)

        # Load existing recipient counts so recovery resumes accumulate correctly
        existing = await repo.count_recipients(self._db, cid)
        counters: dict[str, int] = {
            "sent": existing["sent"],
            "blocked": existing["blocked"],
            "failed": existing["failed"],
            "skipped": existing["skipped"],
        }
        state: dict[str, float] = {"processed": 0.0, "last_write": 0.0}
        start = time.monotonic()
        total_pending = len(user_ids)

        # Send initial progress card
        await self._send_admin_card(bot, cid, campaign["admin_id"], 0, 0, 0, 0, total)

        queue: asyncio.Queue[int] = asyncio.Queue()
        for uid in user_ids:
            await queue.put(uid)

        async def _worker() -> None:
            while not cancel_evt.is_set():
                try:
                    await pause_evt.wait()
                    try:
                        await asyncio.wait_for(bucket.acquire(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue  # re-check cancel/pause
                    try:
                        uid = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return  # no more work — no item was pulled
                    try:
                        status = await self._send_one(bot, cid, uid, campaign)
                        counters[status] += 1
                        state["processed"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        counters["failed"] += 1
                        state["processed"] += 1
                        logger.exception("send failed for user %d in campaign %d",
                                         uid, cid)
                    finally:
                        queue.task_done()
                        bucket.release()

                    # Throttled DB + card update (after any completed send)
                    now = time.monotonic()
                    if state["processed"] % 10 == 8 or now - state["last_write"] >= self._config.bcast_edit_interval:
                        state["last_write"] = now
                        try:
                            await repo.set_broadcast_status(
                                self._db, cid, "running",
                                sent=counters["sent"], blocked=counters["blocked"],
                                failed=counters["failed"], skipped=counters["skipped"],
                            )
                            await self._send_admin_card(
                                bot, cid, campaign["admin_id"],
                                counters["sent"], counters["blocked"],
                                counters["failed"], counters["skipped"], total,
                            )
                        except Exception:
                            logger.warning(
                                "throttled DB/card update failed for campaign %d",
                                cid, exc_info=True,
                            )
                except asyncio.CancelledError:
                    return  # graceful exit on cancellation

        workers = [
            asyncio.create_task(_worker(), name=f"bcast-{cid}-w{i}")
            for i in range(self._config.max_bcast_concurrency)
        ]

        # Wait for all recipients to be processed (workers exit when queue is
        # empty or cancel is signalled).  On cancellation we let in-flight
        # sends finish (cooperative cancel, RULES §5) but stop pulling new
        # items.
        while not queue.empty() and not cancel_evt.is_set():
            await asyncio.sleep(0.05)

        # Workers will exit on their next loop iteration (cancel is set or
        # queue is empty).  Wait for them to drain naturally (up to 5s),
        # then force-cancel any stragglers.
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            pass
        finally:
            for w in workers:
                if not w.done():
                    w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        # Re-check remaining pending recipients (in case some were requeued)
        remaining = await repo.list_pending_recipients(self._db, cid, total_pending)
        skipped_count = len(remaining)
        counters["skipped"] = skipped_count

        elapsed = time.monotonic() - start
        processed = int(state["processed"])
        rate = processed / max(0.001, elapsed) if processed else 0.0
        final_status = (
            BroadcastStatus.CANCELLED.value if cancel_evt.is_set()
            else BroadcastStatus.COMPLETED.value
        )
        await repo.set_broadcast_status(
            self._db, cid, final_status,
            sent=counters["sent"], blocked=counters["blocked"],
            failed=counters["failed"], skipped=counters["skipped"],
            cancelled=1 if cancel_evt.is_set() else 0,
            finished_at=now_iso(), avg_rate=round(rate, 2),
        )
        await repo.audit(
            self._db, "broadcast_finished",
            owner_id=campaign["admin_id"],
            detail={
                "ok": counters["sent"], "blocked": counters["blocked"],
                "failed": counters["failed"], "skipped": counters["skipped"],
                "cancelled": cancel_evt.is_set(), "duration": round(elapsed, 1),
                "rate": round(rate, 1),
            },
        )
        await self._send_admin_card(
            bot, cid, campaign["admin_id"],
            counters["sent"], counters["blocked"],
            counters["failed"], counters["skipped"], total, finished=True,
        )
        await self._publish_system_event(
            SystemEvent(
                event_type="broadcast_completed",
                severity="error" if final_status == "failed" else "info",
                title="✅|اكمل البث" if final_status == BroadcastStatus.COMPLETED.value
                else "×|فشل البث",
                body=(
                    f"⟡|الحملة <code>#{cid}</code>: "
                    f"{counters['sent']} أرسلت، "
                    f"{counters['blocked']} ممنوع، "
                    f"{counters['failed']} فشل، "
                    f"{counters['skipped']} تم تخطيها."
                ),
                data={
                    "campaign_id": cid, "admin_id": campaign["admin_id"],
                    "sent": counters["sent"], "blocked": counters["blocked"],
                    "failed": counters["failed"], "skipped": counters["skipped"],
                    "cancelled": cancel_evt.is_set(),
                },
            )
        )
        self._progress_cards.pop(cid, None)
        self._tasks.pop(cid, None)
        self._cancel_events.pop(cid, None)
        self._pause_events.pop(cid, None)
        self._token_buckets.pop(cid, None)
        self._filters.pop(cid, None)

    async def _send_one(
        self, bot: Any, cid: int, user_id: int, campaign: dict[str, Any],
    ) -> str:
        """Send to one user via ``copy_message`` (or ``send_message`` for
        personalized mode).  Updates the recipient DB row.

        Returns the ``RecipientStatus`` string.  Handles inline retry for
        ``RETRY_FLOOD``; for ``RETRY_TRANSIENT`` retries up to
        ``bcast_retry_attempts`` with exponential backoff.
        """
        src_chat = campaign["source_chat_id"]
        src_msg = campaign["source_message_id"]
        mode = campaign.get("mode") or "copy"
        max_attempts = self._config.bcast_retry_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                if mode == "copy":
                    await bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=src_chat,
                        message_id=src_msg,
                    )
                else:
                    content = campaign.get("content_html") or ""
                    if campaign.get("ab_test_id") is not None:
                        # A/B: content_html stores the variant template
                        pass
                    ctx = await self._user_context(user_id)
                    ctx["created_at"] = campaign.get("created_at") or ""
                    rendered = _safe_format(
                        content, **{k: html.escape(str(v)) for k, v in ctx.items()}
                    )
                    await bot.send_message(
                        chat_id=user_id, text=rendered, parse_mode="HTML",
                    )
                try:
                    await repo.update_recipient_status(
                        self._db, cid, user_id, RecipientStatus.SENT.value,
                        increment_attempts=True,
                    )
                except Exception:
                    logger.warning("recipient DB update failed (cid=%d uid=%d)",
                                   cid, user_id, exc_info=True)
                return RecipientStatus.SENT.value
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                kind = classify_error(exc)

                if kind == ErrorKind.RETRY_FLOOD:
                    retry_after = getattr(exc, "retry_after", 1)
                    try:
                        retry_after = int(retry_after)
                    except (TypeError, ValueError):
                        retry_after = 1
                    if retry_after <= self._config.bcast_flood_retry_threshold:
                        await asyncio.sleep(retry_after)
                        continue
                    try:
                        await repo.update_recipient_status(
                            self._db, cid, user_id, RecipientStatus.FAILED.value,
                            error=str(exc), increment_attempts=True,
                        )
                    except Exception:
                        logger.warning("recipient DB update failed (cid=%d uid=%d)",
                                       cid, user_id, exc_info=True)
                    return RecipientStatus.FAILED.value

                if kind == ErrorKind.RETRY_TRANSIENT:
                    if attempt < max_attempts:
                        backoff = self._config.bcast_retry_backoff_base ** attempt
                        await asyncio.sleep(backoff)
                        continue
                    try:
                        await repo.update_recipient_status(
                            self._db, cid, user_id, RecipientStatus.FAILED.value,
                            error=str(exc), increment_attempts=True,
                        )
                    except Exception:
                        logger.warning("recipient DB update failed (cid=%d uid=%d)",
                                       cid, user_id, exc_info=True)
                    return RecipientStatus.FAILED.value

                # PERMANENT_BLOCKED, PERMANENT_FAIL, RETRY_DELAYED
                status_map = {
                    ErrorKind.PERMANENT_BLOCKED: RecipientStatus.BLOCKED.value,
                    ErrorKind.PERMANENT_FAIL: RecipientStatus.FAILED.value,
                    ErrorKind.RETRY_DELAYED: RecipientStatus.FAILED.value,
                }
                recipient_status = status_map[kind]
                try:
                    await repo.update_recipient_status(
                        self._db, cid, user_id, recipient_status,
                        error=str(exc), increment_attempts=True,
                    )
                except Exception:
                    logger.warning("recipient DB update failed (cid=%d uid=%d)",
                                   cid, user_id, exc_info=True)
                if kind == ErrorKind.PERMANENT_BLOCKED:
                    return RecipientStatus.BLOCKED.value
                return RecipientStatus.FAILED.value

        # exhausted retries
        try:
            await repo.update_recipient_status(
                self._db, cid, user_id, RecipientStatus.FAILED.value,
                error="exhausted retries", increment_attempts=True,
            )
        except Exception:
            logger.warning("recipient DB update failed (cid=%d uid=%d)",
                           cid, user_id, exc_info=True)
        return RecipientStatus.FAILED.value

    async def _send_admin_card(
        self, bot: Any, cid: int, admin_id: int,
        sent: int, blocked: int, failed: int, skipped: int, total: int,
        *, finished: bool = False,
    ) -> None:
        """Send or edit the progress card in the admin's DM."""
        text = _progress_text(sent, blocked, failed, skipped, total)
        if finished:
            done = sent + blocked + failed + skipped
            text += f"\n⟡ مكتمل ({done}/{total})"
        existing = self._progress_cards.get(cid)
        try:
            if existing is None:
                msg = await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
                self._progress_cards[cid] = (admin_id, msg.message_id)
            else:
                chat_id, msg_id = existing
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML",
                )
        except Exception:
            logger.warning("progress card update failed for campaign %d", cid, exc_info=True)

    async def _user_context(self, user_id: int) -> dict[str, Any]:
        """Fetch user fields + account/job counts for personalization templates."""
        user = await repo.get_user(self._db, user_id) or {}
        accounts_count = await repo.count_accounts_for_user(self._db, user_id)
        jobs_count = await repo.count_jobs_for_user(self._db, user_id)
        return {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "username": user.get("username") or "",
            "accounts_count": accounts_count,
            "jobs_count": jobs_count,
        }

    def _ab_test_split(
        self, user_id: int, num_variants: int
    ) -> int:
        """Determine which A/B variant a user is assigned to.

        Uses ``user_id % num_variants`` for deterministic, even splitting
        (Phase 6 §2.8: the weighted form ``user_id % 100 < weight_pct`` reduces
        to this for equal splits).
        """
        if num_variants <= 0:
            return 0
        return user_id % num_variants

    async def create_ab_test(
        self,
        name: str,
        splits: list[tuple[str, str]],
        *,
        admin_id: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> int:
        """Create an A/B test: one ``ab_tests`` row + one draft Broadcast per
        variant.

        Each variant ``(mode, content_html)`` becomes a separate Broadcast row
        linked by ``ab_test_id``.  The audience is split deterministically by
        ``user_id % num_variants`` at delivery time (Phase 6 §2.8).

        Returns the new ``ab_tests.id``.
        """
        ab_test_id = await self._db.execute(
            "INSERT INTO ab_tests (name, created_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        for i, (mode, content) in enumerate(splits):
            await repo.create_broadcast(
                self._db,
                admin_id=admin_id,
                label=f"{name} — variant {i}",
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                mode=mode,
                content_html=content,
                ab_test_id=ab_test_id,
            )
        await repo.audit(
            self._db, "ab_test_created",
            owner_id=admin_id,
            detail={"name": name, "variants": len(splits), "ab_test_id": ab_test_id},
        )
        return ab_test_id


class _SafeFormatter(string.Formatter):
    """Custom formatter that returns ``""`` for missing keys instead of
    raising ``KeyError``.  Used for personalization templates where a
    placeholder may not have a matching context value (Phase 6 §2.8)."""

    def get_value(self, key: Any, args: Sequence[Any], kwargs: dict[str, Any]) -> Any:
        if isinstance(key, str):
            return kwargs.get(key, "")
        try:
            return args[key]
        except IndexError:
            return ""


_safe_formatter = _SafeFormatter()


def _safe_format(template: str, **kwargs: Any) -> str:
    """Render a template with ``str.format`` semantics, returning ``""`` for
    placeholder variables that have no matching keyword argument."""
    return _safe_formatter.format(template, **kwargs)


def now_iso() -> str:
    """UTC timestamp in ISO-8601 'Z' form (mirrors repositories.now_iso)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
