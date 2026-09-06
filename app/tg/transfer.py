"""Transfer engine: fetch members and invite them into the destination.

Implements the PRD §15 loop with the PRD §16 error strategy: every failure is
classified through :func:`app.tg.errors.classify_error`, retries are bounded
and only for kinds the classification marks retryable, and cancellation is
cooperative (checked between invites and inside every wait).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from telethon import errors, utils
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import PeerUser

from app.core.models import (
    SKIP_ALREADY_MEMBER,
    SKIP_BOT,
    SKIP_DELETED,
    SKIP_KICKED,
    SKIP_NOT_MUTUAL,
    SKIP_OTHER,
    SKIP_PRIVACY,
    SKIP_TOO_MANY,
    ResolvedEntity,
)
from app.tg.errors import ErrorKind, classify_error, is_per_member

logger = logging.getLogger(__name__)

__all__ = ["ProgressSnapshot", "TransferEngine", "TransferParams", "TransferResult"]


@dataclass(frozen=True)
class TransferParams:
    source: ResolvedEntity
    dest: ResolvedEntity
    max_members: int
    invite_delay: float
    invite_jitter: float
    flood_wait_max: int
    deadline: float  # asyncio loop.time() absolute


@dataclass(frozen=True)
class ProgressSnapshot:
    phase: str  # "fetching_dest" | "fetching_source" | "inviting" | "waiting"
    done: int
    total: int
    invited: int
    skipped: int
    failed: int
    wait_left: int = 0  # seconds, when phase == "waiting"
    note: str = ""  # Arabic short note or ""


@dataclass(frozen=True)
class TransferResult:
    invited: int
    failed: int
    total_seen: int
    skip_reasons: dict[str, int]
    was_cancelled: bool
    abort_kind: ErrorKind | None  # job_fatal classification that aborted the run
    abort_message: str | None  # Arabic, user-facing


#: Per-member classified kinds map onto skip-reason keys (core.models).
_PER_MEMBER_SKIP = {
    ErrorKind.USER_PRIVACY: SKIP_PRIVACY,
    ErrorKind.USER_NOT_MUTUAL: SKIP_NOT_MUTUAL,
    ErrorKind.USER_TOO_MANY: SKIP_TOO_MANY,
    ErrorKind.USER_KICKED: SKIP_KICKED,
    ErrorKind.USER_DELETED: SKIP_DELETED,
}

#: Length of one cancel-aware sleep slice (seconds). Waits are cut into slices
#: so a cancel request is honored quickly without abandoning asyncio.sleep.
_SLEEP_SLICE = 0.2

#: Expected failure classes around fetches/invites: classified RPC errors,
#: unresolvable peers (ValueError) and network trouble. Anything else is
#: unexpected and propagates to the job runner.
_EXPECTED_ERRORS = (errors.RPCError, ValueError, ConnectionError, TimeoutError, OSError)


def _peer_of(resolved: ResolvedEntity) -> Any:
    """Peer reference addressable by the client, or None (invite previews
    carry ``id=0`` and cannot be addressed until the account joins)."""
    if resolved.id == 0:
        return None
    if resolved.kind not in ("chat", "supergroup", "channel"):
        return None
    # ResolvedEntity.id is Telethon's marked id; get_peer turns it back into
    # the correct PeerChat / PeerChannel reference.
    return utils.get_peer(resolved.id)


def _technical(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


async def _close_iterator(iterator: Any) -> None:
    """Close a participants iterator left suspended by an early break (the
    max_members cap) so no generator is abandoned un-awaited."""
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except _EXPECTED_ERRORS:
        logger.debug("participants iterator close failed", exc_info=True)


@dataclass(slots=True)
class _RunState:
    """Mutable per-run bookkeeping shared by the engine helpers."""

    client: Any
    params: TransferParams
    on_progress: Callable[[ProgressSnapshot], Awaitable[None]]
    cancel_event: asyncio.Event
    loop: asyncio.AbstractEventLoop
    invited: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    phase: str = "fetching_dest"
    was_cancelled: bool = False
    abort_kind: ErrorKind | None = None
    abort_message: str | None = None

    @property
    def done(self) -> int:
        return self.invited + self.skipped + self.failed

    def abort(self, kind: ErrorKind | None, message: str) -> None:
        """First abort wins; later ones never overwrite the user-facing cause."""
        if self.abort_message is None:
            self.abort_kind = kind
            self.abort_message = message

    def record_skip(self, reason: str) -> None:
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        self.skipped += 1

    async def emit(
        self, phase: str, *, wait_left: int = 0, note: str = ""
    ) -> None:
        self.phase = phase
        await self.on_progress(
            ProgressSnapshot(
                phase=phase,
                done=self.done,
                total=self.total,
                invited=self.invited,
                skipped=self.skipped,
                failed=self.failed,
                wait_left=wait_left,
                note=note,
            )
        )

    async def sleep(self, seconds: float) -> bool:
        """Cancel-aware sleep in small slices; False when cancel was observed."""
        remaining = float(seconds)
        while remaining > 0 and not self.cancel_event.is_set():
            step = min(_SLEEP_SLICE, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return not self.cancel_event.is_set()

    def result(self, total_seen: int) -> TransferResult:
        return TransferResult(
            invited=self.invited,
            failed=self.failed,
            total_seen=total_seen,
            skip_reasons=dict(self.skip_reasons),
            was_cancelled=self.was_cancelled,
            abort_kind=self.abort_kind,
            abort_message=self.abort_message,
        )


class TransferEngine:
    """Executes one member-transfer run against a connected client."""

    async def run(
        self,
        client: Any,
        params: TransferParams,
        on_progress: Callable[[ProgressSnapshot], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> TransferResult:
        state = _RunState(
            client=client,
            params=params,
            on_progress=on_progress,
            cancel_event=cancel_event,
            loop=asyncio.get_running_loop(),
        )
        try:
            dest_ids = await self._fetch_dest_ids(state)
            users: list[Any] | None = None
            if dest_ids is not None:
                users = await self._fetch_source_users(state)
                if users is not None:
                    await self._invite_loop(state, dest_ids, users)
        finally:
            # Exactly one final snapshot on every return path (PRD §15).
            await state.emit(state.phase)
        return state.result(total_seen=len(users) if users is not None else 0)

    # -- fetching ---------------------------------------------------------------

    async def _fetch_dest_ids(self, state: _RunState) -> set[int] | None:
        """Iterate destination participants into an id set; None = aborted."""
        dest = state.params.dest
        peer = _peer_of(dest)
        if peer is None:
            state.abort(
                ErrorKind.INPUT_INVALID,
                "تعذر تحديد المجموعة الهدف لجلب أعضائها الحاليين.",
            )
            return None
        if state.cancel_event.is_set():
            state.was_cancelled = True
            return None
        await state.emit("fetching_dest")

        ids: set[int] = set()
        for attempt in (1, 2):
            ids.clear()
            iterator = state.client.iter_participants(peer)
            try:
                async for user in iterator:
                    if state.cancel_event.is_set():
                        state.was_cancelled = True
                        return None
                    user_id = getattr(user, "id", None)
                    if user_id is not None:
                        ids.add(user_id)
            except _EXPECTED_ERRORS as exc:
                if await self._handle_fetch_error(state, exc, attempt):
                    continue  # bounded single retry for transient kinds
                return None
            finally:
                await _close_iterator(iterator)
            await state.emit("fetching_dest", note="تم جلب أعضاء الهدف.")
            logger.debug("dest %s: %d known member ids", dest.raw_ref, len(ids))
            return ids
        return None  # pragma: no cover - _handle_fetch_error always aborts first

    async def _fetch_source_users(self, state: _RunState) -> list[Any] | None:
        """Collect up to max_members user objects from the source; None = aborted."""
        source = state.params.source
        peer = _peer_of(source)
        if peer is None:
            state.abort(
                ErrorKind.INPUT_INVALID,
                "تعذر تحديد المجموعة المصدر لجلب أعضائها.",
            )
            return None
        if state.cancel_event.is_set():
            state.was_cancelled = True
            return None
        await state.emit("fetching_source")

        users: list[Any] = []
        for attempt in (1, 2):
            users.clear()
            iterator = state.client.iter_participants(peer)
            try:
                async for user in iterator:
                    if state.cancel_event.is_set():
                        state.was_cancelled = True
                        return None
                    users.append(user)
                    if len(users) >= state.params.max_members:
                        break
            except _EXPECTED_ERRORS as exc:
                if await self._handle_fetch_error(state, exc, attempt):
                    continue
                return None
            finally:
                await _close_iterator(iterator)
            state.total = len(users)
            await state.emit("fetching_source", note="تم جلب قائمة الأعضاء.")
            return users
        return None  # pragma: no cover - _handle_fetch_error always aborts first

    async def _handle_fetch_error(
        self, state: _RunState, exc: BaseException, attempt: int
    ) -> bool:
        """Classify a fetch failure: True = retry allowed (attempt < 2),
        False = run aborted. Unresolvable peers degrade to INPUT_INVALID."""
        if isinstance(exc, ValueError):
            state.abort(
                ErrorKind.INPUT_INVALID,
                "تعذر الوصول إلى إحدى المجموعتين لدى تيليجرام، افتح المحادثة معهما "
                "في الحساب ثم أعد المحاولة.",
            )
            logger.info("fetch failed: unresolvable peer (%s)", _technical(exc))
            return False
        classified = classify_error(exc)
        logger.info(
            "fetch failed: %s (%s)", classified.kind, classified.technical
        )
        if classified.retryable and attempt < 2:
            return True
        state.abort(classified.kind, classified.message)
        return False

    # -- inviting ---------------------------------------------------------------

    async def _invite_loop(
        self, state: _RunState, dest_ids: set[int], users: list[Any]
    ) -> None:
        dest = state.params.dest
        await state.emit("inviting")
        for user in users:
            if state.abort_message is not None:
                return  # a previous member's error aborted the whole run
            if state.cancel_event.is_set():
                state.was_cancelled = True
                return
            if state.loop.time() > state.params.deadline:
                state.abort(None, "انتهت المدة المسموحة للعملية")
                return
            user_id = getattr(user, "id", None)
            if user_id is None:
                # A participant without an id is unusable; count it, never skip
                # silently (RULES §5).
                state.record_skip(SKIP_OTHER)
                await state.emit("inviting")
                continue
            if getattr(user, "bot", False):
                state.record_skip(SKIP_BOT)
                await state.emit("inviting")
                continue
            if user_id in dest_ids:
                state.record_skip(SKIP_ALREADY_MEMBER)
                await state.emit("inviting")
                continue
            delay = state.params.invite_delay + random.uniform(
                0, max(state.params.invite_jitter, 0.0)
            )
            if not await state.sleep(delay):
                state.was_cancelled = True
                return
            await self._invite_one(state, dest, user, retry=False)
            if state.abort_message is not None:
                return

    async def _invite_one(
        self, state: _RunState, dest: ResolvedEntity, user: Any, *, retry: bool
    ) -> None:
        user_id = user.id
        try:
            await self._send_invite(state.client, dest, user_id)
            state.invited += 1
            logger.debug("invited user %s", user_id)
        except _EXPECTED_ERRORS as exc:
            await self._handle_invite_error(state, user, exc, retry=retry)
            return
        await state.emit("inviting")

    async def _send_invite(
        self, client: Any, dest: ResolvedEntity, user_id: int
    ) -> None:
        """One real invite request for the given user into ``dest``."""
        user_input = await client.get_input_entity(PeerUser(user_id=user_id))
        if dest.kind == "chat":
            await client(
                AddChatUserRequest(chat_id=dest.id, user_id=user_input, fwd_limit=0)
            )
            return
        dest_input = await client.get_input_entity(_peer_of(dest))
        await client(InviteToChannelRequest(channel=dest_input, users=[user_input]))

    async def _handle_invite_error(
        self, state: _RunState, user: Any, exc: BaseException, *, retry: bool
    ) -> None:
        """Classify one invite failure and take the PRD §16 action for it."""
        classified = classify_error(exc)
        kind = classified.kind
        logger.info(
            "invite user %s failed: %s (%s)", user.id, kind, classified.technical
        )
        if kind is ErrorKind.UNEXPECTED:
            raise exc  # truly unexpected: the job runner fails the job generically

        if is_per_member(kind):
            state.record_skip(_PER_MEMBER_SKIP[kind])
            await state.emit("inviting")
            return

        if kind is ErrorKind.FLOOD_WAIT:
            if retry:
                # Bounded: a second FloodWait after the single retry is a
                # counted failure, never another wait (PRD §16: wait once).
                state.failed += 1
                await state.emit("inviting")
                return
            wait = classified.wait_seconds or 0
            if wait > state.params.flood_wait_max:
                state.abort(
                    ErrorKind.FLOOD_WAIT,
                    f"تلقى الحساب طلب انتظار من تيليجرام لمدة {wait} ثانية، "
                    "وهي أطول من الحد المسموح، أوقف العملية وأعد المحاولة لاحقاً.",
                )
                return
            await state.emit(
                "waiting",
                wait_left=wait,
                note="بانتظار انتهاء حد الانتظار من تيليجرام.",
            )
            if not await state.sleep(wait):
                state.was_cancelled = True
                return
            await self._invite_one(state, state.params.dest, user, retry=True)
            return

        if kind is ErrorKind.TRANSIENT:
            if retry:
                state.failed += 1
                await state.emit("inviting")
                return
            await self._invite_one(state, state.params.dest, user, retry=True)
            return

        # Every remaining kind (PEER_FLOOD, ADMIN_REQUIRED, ENTITY_PRIVATE,
        # AUTH_REVOKED, ACCOUNT_RESTRICTED, INPUT_INVALID) is non-retryable:
        # continuing would hammer a restricted or unauthorized account.
        state.abort(kind, classified.message)
