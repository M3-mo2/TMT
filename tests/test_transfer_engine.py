"""Offline tests for app.tg.transfer.TransferEngine — counters, cancel, errors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import utils
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    PeerFloodError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.types import InputPeerChannel, PeerChannel, User

from app.core.models import ResolvedEntity
from app.tg.errors import ErrorKind
from app.tg.transfer import ProgressSnapshot, TransferEngine, TransferParams
from tests.fakes import FakeTelegramClient

SOURCE_ID = utils.get_peer_id(PeerChannel(555))
DEST_ID = utils.get_peer_id(PeerChannel(777))
CHAT_DEST_ID = 7  # basic chat: ResolvedEntity keeps the raw chat id


def resolved(
    id: int = SOURCE_ID,
    kind: str = "supergroup",
    title: str = "Source",
    members_count: int | None = None,
) -> ResolvedEntity:
    return ResolvedEntity(id=id, kind=kind, title=title, members_count=members_count)


def users(*ids: int, bot: bool = False) -> list[User]:
    return [User(id=i, first_name=f"u{i}", access_hash=1, bot=bot) for i in ids]


def input_entities_for(user_ids: list[int], dest_id: int, dest_kind: str) -> dict[int, Any]:
    entities: dict[int, Any] = {
        uid: InputPeerChannel(channel_id=uid, access_hash=1) for uid in user_ids
    }
    if dest_kind == "supergroup":
        entities[dest_id] = InputPeerChannel(channel_id=777, access_hash=1)
    return entities


def make_fake(
    source_users: list[User],
    dest_users: list[User],
    dest: ResolvedEntity,
    invite_outcomes: list[Any] | None = None,
) -> FakeTelegramClient:
    dest_key = CHAT_DEST_ID if dest.kind == "chat" else DEST_ID
    return FakeTelegramClient(
        participants_by_peer={
            SOURCE_ID: source_users,
            dest_key: dest_users,
        },
        input_entities=input_entities_for(
            [u.id for u in source_users], dest_key, dest.kind
        ),
        invite_outcomes=list(invite_outcomes or []),
    )


def make_params(
    source: ResolvedEntity,
    dest: ResolvedEntity,
    *,
    max_members: int = 10,
    invite_delay: float = 0.0,
    invite_jitter: float = 0.0,
    flood_wait_max: int = 5,
    deadline_offset: float = 60.0,
) -> TransferParams:
    return TransferParams(
        source=source,
        dest=dest,
        max_members=max_members,
        invite_delay=invite_delay,
        invite_jitter=invite_jitter,
        flood_wait_max=flood_wait_max,
        deadline=asyncio.get_running_loop().time() + deadline_offset,
    )


class Collector:
    """Progress sink recording every snapshot in order."""

    def __init__(self) -> None:
        self.snaps: list[ProgressSnapshot] = []

    async def __call__(self, snapshot: ProgressSnapshot) -> None:
        self.snaps.append(snapshot)

    @property
    def phases(self) -> list[str]:
        return [s.phase for s in self.snaps]

    def assert_monotonic_done(self) -> None:
        done = [s.done for s in self.snaps]
        assert done == sorted(done)


def invite_call_count(fake: FakeTelegramClient) -> int:
    return sum(
        1
        for name, _ in fake.calls
        if name in ("AddChatUserRequest", "InviteToChannelRequest")
    )


async def run_engine(
    fake: FakeTelegramClient,
    params: TransferParams,
    on_progress: Callable[[ProgressSnapshot], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
):
    return await TransferEngine().run(
        fake,
        params,
        on_progress or (lambda _snap: _noop()),
        cancel_event or asyncio.Event(),
    )


async def _noop() -> None:
    return None


class TestNormalRun:
    async def test_mixed_outcomes_counters_exact(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        # user 1 already in dest, user 2 is a bot, user 3 privacy-restricted,
        # user 4 invites cleanly.
        source_users = users(1) + users(2, bot=True) + users(3, 4)
        fake = make_fake(
            source_users,
            users(1, 9),
            dest,
            invite_outcomes=[UserPrivacyRestrictedError(request=None)],
        )
        params = make_params(resolved(members_count=4), dest)

        result = await run_engine(fake, params)

        assert result.invited == 1
        assert sum(result.skip_reasons.values()) == 3
        assert result.failed == 0
        assert result.total_seen == 4
        assert result.skip_reasons == {
            "already_member": 1,
            "bot": 1,
            "privacy": 1,
        }
        assert result.was_cancelled is False
        assert result.abort_kind is None
        assert result.abort_message is None

    async def test_not_mutual_maps_to_skip_key(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(5), users(9), dest, invite_outcomes=[UserNotMutualContactError(request=None)]
        )
        params = make_params(resolved(members_count=1), dest)
        result = await run_engine(fake, params)
        assert result.invited == 0
        assert result.failed == 0
        assert result.skip_reasons == {"not_mutual": 1}

    async def test_basic_chat_dest_uses_add_chat_user(self) -> None:
        dest = resolved(id=CHAT_DEST_ID, kind="chat", title="Basic")
        fake = make_fake(users(6), users(9), dest)
        params = make_params(resolved(members_count=1), dest)
        result = await run_engine(fake, params)
        assert result.invited == 1
        names = [name for name, _ in fake.calls]
        assert "AddChatUserRequest" in names
        assert "InviteToChannelRequest" not in names

    async def test_max_members_caps_source(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(users(1, 2, 3, 4, 5), users(9), dest)
        params = make_params(resolved(members_count=5), dest, max_members=2)
        result = await run_engine(fake, params)
        assert result.total_seen == 2
        assert result.invited == 2


class TestProgress:
    async def test_phase_sequence_and_monotonic_done(self) -> None:
        collector = Collector()
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(users(1, 2), users(9), dest)
        params = make_params(resolved(members_count=2), dest)
        await run_engine(fake, params, on_progress=collector)

        assert collector.phases[0] == "fetching_dest"
        assert "fetching_source" in collector.phases
        assert collector.phases[-1] == "inviting"
        # total becomes known after the source fetch completes.
        totals = [s.total for s in collector.snaps if s.phase == "fetching_source"]
        assert 0 in totals and 2 in totals
        collector.assert_monotonic_done()

    async def test_final_snapshot_emitted_on_abort(self) -> None:
        collector = Collector()
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2), users(9), dest, invite_outcomes=[PeerFloodError(request=None)]
        )
        params = make_params(resolved(members_count=1), dest)
        result = await run_engine(fake, params, on_progress=collector)
        assert result.abort_kind is ErrorKind.PEER_FLOOD
        # The final snapshot reflects the abort (job-fatal errors are not
        # per-member failures, so failed stays 0).
        assert collector.phases[-1] == "inviting"
        assert collector.snaps[-1].failed == 0
        assert collector.snaps[-1].done == 0


class TestCancellation:
    async def test_cancel_between_invites_stops_the_loop(self) -> None:
        collector = Collector()
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(users(2, 3, 4), users(9), dest)
        params = make_params(resolved(members_count=3), dest, invite_delay=0.05)
        cancel_event = asyncio.Event()

        async def progress(snapshot: ProgressSnapshot) -> None:
            await collector(snapshot)
            if snapshot.phase == "inviting" and snapshot.done >= 2:
                cancel_event.set()

        result = await run_engine(fake, params, on_progress=progress, cancel_event=cancel_event)

        assert result.was_cancelled is True
        assert result.abort_kind is None
        assert result.abort_message is None
        assert result.invited == 2
        assert invite_call_count(fake) == 2
        collector.assert_monotonic_done()

    async def test_cancel_during_flood_wait(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2),
            users(9),
            dest,
            invite_outcomes=[FloodWaitError(request=None, capture=30)],
        )
        params = make_params(resolved(members_count=1), dest, flood_wait_max=60)
        cancel_event = asyncio.Event()

        async def progress(snapshot: ProgressSnapshot) -> None:
            if snapshot.phase == "waiting":
                cancel_event.set()

        result = await run_engine(fake, params, on_progress=progress, cancel_event=cancel_event)
        assert result.was_cancelled is True
        assert result.invited == 0


class TestFloodWait:
    async def test_small_flood_wait_sleeps_then_retries_once(self) -> None:
        collector = Collector()
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2),
            users(9),
            dest,
            invite_outcomes=[FloodWaitError(request=None, capture=1), None],
        )
        params = make_params(resolved(members_count=1), dest, flood_wait_max=5)

        result = await run_engine(fake, params, on_progress=collector)

        assert result.invited == 1
        assert result.failed == 0
        assert result.abort_kind is None
        waiting = [s for s in collector.snaps if s.phase == "waiting"]
        assert len(waiting) == 1
        assert waiting[0].wait_left == 1
        assert waiting[0].note != ""
        assert invite_call_count(fake) == 2

    async def test_second_flood_wait_after_retry_counts_failed(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2),
            users(9),
            dest,
            invite_outcomes=[
                FloodWaitError(request=None, capture=1),
                FloodWaitError(request=None, capture=1),
            ],
        )
        params = make_params(resolved(members_count=1), dest, flood_wait_max=5)
        result = await run_engine(fake, params)
        assert result.invited == 0
        assert result.failed == 1
        assert result.abort_kind is None

    async def test_flood_wait_over_cap_aborts(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2),
            users(9),
            dest,
            invite_outcomes=[FloodWaitError(request=None, capture=600)],
        )
        params = make_params(resolved(members_count=1), dest, flood_wait_max=5)
        result = await run_engine(fake, params)
        assert result.abort_kind is ErrorKind.FLOOD_WAIT
        assert result.abort_message is not None
        assert "600" in result.abort_message
        assert invite_call_count(fake) == 1


class TestAborts:
    async def test_peer_flood_aborts_without_retry(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2, 3),
            users(9),
            dest,
            invite_outcomes=[PeerFloodError(request=None)],
        )
        params = make_params(resolved(members_count=2), dest)
        result = await run_engine(fake, params)
        assert result.abort_kind is ErrorKind.PEER_FLOOD
        assert result.abort_message is not None
        assert invite_call_count(fake) == 1

    async def test_dest_iteration_failure_aborts(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = FakeTelegramClient(
            participants_by_peer={
                DEST_ID: ChannelPrivateError(request=None),
                SOURCE_ID: users(1, 2),
            },
            input_entities=input_entities_for([1, 2], DEST_ID, "supergroup"),
        )
        params = make_params(resolved(members_count=2), dest)
        result = await run_engine(fake, params)
        assert result.abort_kind is ErrorKind.ENTITY_PRIVATE
        assert result.abort_message is not None
        assert result.invited == 0
        assert result.total_seen == 0
        # The source was never iterated.
        assert sum(1 for name, _ in fake.calls if name == "iter_participants") == 1

    async def test_deadline_passing_aborts_with_message(self) -> None:
        collector = Collector()
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(users(2, 3), users(9), dest)
        params = make_params(resolved(members_count=2), dest, deadline_offset=-1.0)
        result = await run_engine(fake, params, on_progress=collector)
        assert result.abort_kind is None
        assert result.abort_message == "انتهت المدة المسموحة للعملية"
        assert result.was_cancelled is False
        assert result.invited == 0
        assert invite_call_count(fake) == 0
        assert collector.phases[-1] == "inviting"

    async def test_transient_retries_once_then_counts_failed(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2),
            users(9),
            dest,
            invite_outcomes=[ConnectionError("boom"), ConnectionError("boom again")],
        )
        params = make_params(resolved(members_count=1), dest)
        result = await run_engine(fake, params)
        assert result.failed == 1
        assert result.invited == 0
        assert result.abort_kind is None
        assert invite_call_count(fake) == 2

    async def test_transient_retry_can_still_succeed(self) -> None:
        dest = resolved(id=DEST_ID, title="Dest")
        fake = make_fake(
            users(2),
            users(9),
            dest,
            invite_outcomes=[ConnectionError("boom"), None],
        )
        params = make_params(resolved(members_count=1), dest)
        result = await run_engine(fake, params)
        assert result.invited == 1
        assert result.failed == 0
