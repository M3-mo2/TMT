"""Offline tests for app.tg.preflight — PASS/WARN/FAIL/UNKNOWN paths."""

from __future__ import annotations

import pytest
from telethon import utils
from telethon.errors import (
    ChannelPrivateError,
    UserNotParticipantError,
)
from telethon.tl.types import (
    PeerChannel,
    RestrictionReason,
    User,
)

from app.core.models import CheckStatus, ResolvedEntity
from app.tg.preflight import run_preflight
from tests.fakes import FakeTelegramClient, admin_permissions, member_permissions

SOURCE_ID = utils.get_peer_id(PeerChannel(555))
DEST_ID = utils.get_peer_id(PeerChannel(777))
HIDDEN_NOTE = "قائمة الأعضاء غير متاحة قد تكون مخفية"


def resolved(
    id: int = SOURCE_ID,
    kind: str = "supergroup",
    title: str = "Source",
    *,
    members_count: int | None = None,
    is_member: bool | None = None,
    via_invite_link: bool = False,
    is_broadcast: bool = False,
    is_megagroup: bool = True,
) -> ResolvedEntity:
    return ResolvedEntity(
        id=id,
        kind=kind,
        title=title,
        members_count=members_count,
        is_broadcast=is_broadcast,
        is_megagroup=is_megagroup,
        is_member=is_member,
        via_invite_link=via_invite_link,
    )


def chat_resolved(id: int = 7, title: str = "Basic Chat") -> ResolvedEntity:
    return ResolvedEntity(id=id, kind="chat", title=title, members_count=4)


def users(*ids: int) -> list[User]:
    return [User(id=i, first_name=f"u{i}", access_hash=1) for i in ids]


def make_fake(**overrides: object) -> FakeTelegramClient:
    defaults: dict[str, object] = {
        "permissions": {
            SOURCE_ID: member_permissions(),
            DEST_ID: admin_permissions(invite_users=True),
        },
        "participants_by_peer": {SOURCE_ID: users(1, 2, 3), DEST_ID: users(9)},
        "me": User(id=900001, is_self=True, first_name="Fake"),
    }
    defaults.update(overrides)
    return FakeTelegramClient(**defaults)  # type: ignore[arg-type]


async def run(
    fake: FakeTelegramClient,
    source: ResolvedEntity | None = None,
    dest: ResolvedEntity | None = None,
    *,
    max_members: int = 100,
) -> dict[str, object]:
    source = source or resolved()
    dest = dest or resolved(id=DEST_ID, title="Dest")
    checks = await run_preflight(fake, source, dest, max_members=max_members)
    assert [c.key for c in checks] == [
        "dest_diff",
        "source_type",
        "dest_type",
        "source_member",
        "dest_member",
        "source_participants",
        "dest_invite_rights",
        "account_restriction",
        "flood_note",
    ]
    return {c.key: c.status for c in checks}


class TestHappyPath:
    async def test_all_pass(self) -> None:
        fake = make_fake()
        source = resolved(members_count=3, is_member=True)
        dest = resolved(id=DEST_ID, title="Dest", members_count=1, is_member=True)
        statuses = await run(fake, source, dest)
        assert statuses["dest_diff"] is CheckStatus.PASS
        assert statuses["source_type"] is CheckStatus.PASS
        assert statuses["dest_type"] is CheckStatus.PASS
        assert statuses["source_member"] is CheckStatus.PASS
        assert statuses["dest_member"] is CheckStatus.PASS
        assert statuses["source_participants"] is CheckStatus.PASS
        assert statuses["dest_invite_rights"] is CheckStatus.PASS
        assert statuses["account_restriction"] is CheckStatus.PASS
        assert statuses["flood_note"] is CheckStatus.INFO


class TestStructuralFailures:
    async def test_same_id_fails(self) -> None:
        statuses = await run(make_fake(), dest=resolved(id=SOURCE_ID, title="Same"))
        assert statuses["dest_diff"] is CheckStatus.FAIL

    async def test_broadcast_dest_fails(self) -> None:
        dest = resolved(
            id=DEST_ID,
            kind="channel",
            title="News",
            is_broadcast=True,
            is_megagroup=False,
        )
        statuses = await run(make_fake(), dest=dest)
        assert statuses["dest_type"] is CheckStatus.FAIL

    async def test_user_dest_fails(self) -> None:
        dest = resolved(id=42, kind="user", title="Someone")
        statuses = await run(make_fake(), dest=dest)
        assert statuses["dest_type"] is CheckStatus.FAIL


class TestMembership:
    async def test_not_member_of_dest_fails(self) -> None:
        fake = make_fake(
            permissions={SOURCE_ID: member_permissions(), DEST_ID: UserNotParticipantError(request=None)}
        )
        statuses = await run(fake)
        assert statuses["dest_member"] is CheckStatus.FAIL
        assert statuses["source_member"] is CheckStatus.PASS

    async def test_rpc_error_is_unknown_not_fail(self) -> None:
        fake = make_fake(
            permissions={
                SOURCE_ID: ChannelPrivateError(request=None),
                DEST_ID: admin_permissions(invite_users=True),
            }
        )
        statuses = await run(fake)
        assert statuses["source_member"] is CheckStatus.UNKNOWN

    async def test_invite_preview_dest_not_member_fails_without_rpc(self) -> None:
        # Invite previews carry id=0: the membership branch must fire on the
        # ResolvedEntity flags before any peer is addressed.
        fake = make_fake()
        dest = resolved(id=0, title="Preview", is_member=False, via_invite_link=True)
        statuses = await run(fake, dest=dest)
        assert statuses["dest_member"] is CheckStatus.FAIL
        assert statuses["dest_invite_rights"] is CheckStatus.UNKNOWN
        # Only the source membership probe ran; the preview dest was never
        # addressed (id=0).
        assert (
            sum(1 for name, _ in fake.calls if name == "get_permissions") == 1
        )


class TestSourceParticipants:
    @pytest.mark.parametrize("members_count", [3, None])
    async def test_hidden_list_is_unknown(self, members_count: int | None) -> None:
        fake = make_fake(
            participants_by_peer={SOURCE_ID: [], DEST_ID: users(9)}
        )
        source = resolved(members_count=members_count)
        statuses = await run(fake, source)
        assert statuses["source_participants"] is CheckStatus.UNKNOWN

    async def test_hidden_message_is_verbatim(self) -> None:
        fake = make_fake(participants_by_peer={SOURCE_ID: [], DEST_ID: users(9)})
        checks = await run_preflight(
            fake, resolved(members_count=3), resolved(id=DEST_ID, title="Dest"),
            max_members=100,
        )
        by_key = {c.key: c for c in checks}
        assert by_key["source_participants"].message == HIDDEN_NOTE

    async def test_empty_list_with_zero_count_passes(self) -> None:
        fake = make_fake(participants_by_peer={SOURCE_ID: [], DEST_ID: users(9)})
        statuses = await run(fake, resolved(members_count=0))
        assert statuses["source_participants"] is CheckStatus.PASS

    async def test_over_cap_from_members_count_warns(self) -> None:
        statuses = await run(make_fake(), resolved(members_count=150), max_members=100)
        assert statuses["source_participants"] is CheckStatus.WARN

    async def test_over_cap_from_page_warns(self) -> None:
        # members_count unavailable, but the first page alone exceeds the cap.
        fake = make_fake(
            participants_by_peer={SOURCE_ID: users(1, 2, 3), DEST_ID: users(9)}
        )
        statuses = await run(fake, max_members=2)
        assert statuses["source_participants"] is CheckStatus.WARN

    async def test_fetch_error_is_unknown(self) -> None:
        fake = make_fake(
            participants_by_peer={
                SOURCE_ID: ChannelPrivateError(request=None),
                DEST_ID: users(9),
            }
        )
        statuses = await run(fake)
        assert statuses["source_participants"] is CheckStatus.UNKNOWN


class TestDestInviteRights:
    async def test_admin_without_invite_right_fails(self) -> None:
        fake = make_fake(permissions={
            SOURCE_ID: member_permissions(),
            DEST_ID: admin_permissions(invite_users=False),
        })
        statuses = await run(fake)
        assert statuses["dest_invite_rights"] is CheckStatus.FAIL

    async def test_plain_member_is_unknown(self) -> None:
        fake = make_fake(permissions={
            SOURCE_ID: member_permissions(),
            DEST_ID: member_permissions(),
        })
        statuses = await run(fake)
        assert statuses["dest_invite_rights"] is CheckStatus.UNKNOWN

    async def test_basic_chat_is_unknown(self) -> None:
        fake = make_fake(permissions={
            SOURCE_ID: member_permissions(),
            7: member_permissions(),
        })
        statuses = await run(
            fake,
            dest=chat_resolved(),
        )
        assert statuses["dest_invite_rights"] is CheckStatus.UNKNOWN
        assert statuses["dest_type"] is CheckStatus.PASS

    async def test_rights_read_error_is_unknown(self) -> None:
        fake = make_fake(permissions={
            SOURCE_ID: member_permissions(),
            DEST_ID: ChannelPrivateError(request=None),
        })
        statuses = await run(fake)
        assert statuses["dest_invite_rights"] is CheckStatus.UNKNOWN


class TestAccountRestriction:
    async def test_restricted_account_warns(self) -> None:
        me = User(
            id=900001,
            is_self=True,
            first_name="Fake",
            restricted=True,
            restriction_reason=[
                RestrictionReason(platform="android", reason="pike", text="")
            ],
        )
        statuses = await run(make_fake(me=me))
        assert statuses["account_restriction"] is CheckStatus.WARN

    async def test_unrestricted_account_passes(self) -> None:
        statuses = await run(make_fake())
        assert statuses["account_restriction"] is CheckStatus.PASS
