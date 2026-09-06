"""Offline tests for app.tg.resolver — input forms, invite previews, failures."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashInvalidError,
)
from telethon.tl.types import Chat, ChatInvite, ChatInviteAlready, Channel

from app.tg.resolver import ResolutionError, resolve_entity
from tests.fakes import FakeTelegramClient


def make_channel(
    id: int = 555,
    title: str = "My Channel",
    username: str | None = "mych",
    *,
    broadcast: bool = False,
    megagroup: bool = True,
) -> Channel:
    return Channel(
        id=id,
        title=title,
        username=username,
        photo=None,
        date=None,
        broadcast=broadcast,
        megagroup=megagroup,
    )


def make_basic_chat(id: int = 7, title: str = "Basic Chat") -> Chat:
    return Chat(
        id=id,
        title=title,
        photo=None,
        participants_count=10,
        date=None,
        version=0,
    )


@pytest.fixture
def fake() -> FakeTelegramClient:
    channel = make_channel()
    return FakeTelegramClient(
        entities={"@mych": channel, -1001234567890: make_channel(id=1234567890)},
        participants=SimpleNamespace(
            full_chat=SimpleNamespace(participants_count=1234)
        ),
    )


class TestUsernameForms:
    async def test_at_sign(self, fake: FakeTelegramClient) -> None:
        resolved = await resolve_entity(fake, "@mych")
        assert resolved.id == -1000000000555
        assert resolved.kind == "supergroup"
        assert resolved.title == "My Channel"
        assert resolved.username == "mych"
        assert resolved.is_megagroup is True
        assert resolved.is_broadcast is False
        assert resolved.members_count == 1234  # from GetFullChannelRequest
        assert resolved.raw_ref == "@mych"
        assert resolved.is_member is None
        assert resolved.via_invite_link is False

    async def test_bare_name(self, fake: FakeTelegramClient) -> None:
        resolved = await resolve_entity(fake, "mych")
        assert resolved.id == -1000000000555

    async def test_tme_link(self, fake: FakeTelegramClient) -> None:
        resolved = await resolve_entity(fake, "https://t.me/mych")
        assert resolved.id == -1000000000555
        assert resolved.raw_ref == "https://t.me/mych"

    async def test_tme_link_no_scheme_and_trailing_slash(
        self, fake: FakeTelegramClient
    ) -> None:
        resolved = await resolve_entity(fake, "t.me/mych/")
        assert resolved.id == -1000000000555

    async def test_telegram_me_link(self, fake: FakeTelegramClient) -> None:
        resolved = await resolve_entity(fake, "https://telegram.me/mych")
        assert resolved.id == -1000000000555

    async def test_broadcast_channel_kind(self) -> None:
        fake = FakeTelegramClient(
            entities={"@news": make_channel(title="News", broadcast=True, megagroup=False)},
            participants=SimpleNamespace(
                full_chat=SimpleNamespace(participants_count=99)
            ),
        )
        resolved = await resolve_entity(fake, "@news")
        assert resolved.kind == "channel"
        assert resolved.is_broadcast is True
        assert resolved.is_megagroup is False
        assert resolved.members_count == 99

    async def test_basic_chat_kind(self) -> None:
        fake = FakeTelegramClient(entities={"@old": make_basic_chat()})
        resolved = await resolve_entity(fake, "@old")
        assert resolved.kind == "chat"
        assert resolved.members_count == 10  # from the entity itself
        assert fake.calls == [
            ("get_entity", ("@old",),)  # no full-channel request needed
        ]

    async def test_input_trimmed(self, fake: FakeTelegramClient) -> None:
        resolved = await resolve_entity(fake, "  @mych  ")
        assert resolved.id == -1000000000555


class TestInviteLinks:
    async def test_plus_hash_already_member(self) -> None:
        chat = make_basic_chat(id=999, title="Known Group")
        fake = FakeTelegramClient(invite_results=ChatInviteAlready(chat=chat))
        resolved = await resolve_entity(fake, "https://t.me/+AbCdEf123")
        assert resolved.id == -999
        assert resolved.kind == "chat"
        assert resolved.title == "Known Group"
        assert resolved.members_count == 10
        assert resolved.is_member is True
        assert resolved.via_invite_link is True

    async def test_joinchat_hash_not_member_preview(self) -> None:
        invite = ChatInvite(
            title="Private Group",
            photo=None,
            color=0,
            broadcast=False,
            megagroup=True,
            participants_count=42,
        )
        fake = FakeTelegramClient(invite_results=invite)
        resolved = await resolve_entity(fake, "https://t.me/joinchat/AbCdEf123")
        assert resolved.id == 0  # preview carries no id
        assert resolved.kind == "supergroup"
        assert resolved.title == "Private Group"
        assert resolved.members_count == 42
        assert resolved.is_member is False
        assert resolved.via_invite_link is True
        assert resolved.is_broadcast is False
        assert resolved.is_megagroup is True

    async def test_broadcast_invite_preview_kind(self) -> None:
        invite = ChatInvite(
            title="Announcements",
            photo=None,
            color=0,
            broadcast=True,
            megagroup=False,
            participants_count=1000,
        )
        fake = FakeTelegramClient(invite_results=invite)
        resolved = await resolve_entity(fake, "t.me/+Hash01")
        assert resolved.kind == "channel"
        assert resolved.is_broadcast is True

    async def test_invalid_invite_hash(self) -> None:
        fake = FakeTelegramClient(
            invite_results=InviteHashInvalidError(request=None)
        )
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, "t.me/+dead")
        assert exc_info.value.reason == "invalid"


class TestNumeric:
    async def test_known_numeric_id(self, fake: FakeTelegramClient) -> None:
        resolved = await resolve_entity(fake, "-1001234567890")
        assert resolved.id == -1001234567890

    async def test_unknown_numeric_id_unresolvable(
        self, fake: FakeTelegramClient
    ) -> None:
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, "-1001111111111")
        assert exc_info.value.reason == "unresolvable"

    async def test_numeric_private_still_unresolvable(self) -> None:
        fake = FakeTelegramClient(
            raises={"get_entity": ChannelPrivateError(request=None)}
        )
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, "-1001234567890")
        assert exc_info.value.reason == "unresolvable"


class TestFailures:
    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "hello world!", "user$$$", "https://evil.com/mych", "t.me/"],
    )
    async def test_invalid_input(self, fake: FakeTelegramClient, raw: str) -> None:
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, raw)
        assert exc_info.value.reason == "invalid"

    async def test_unknown_username_not_found(self) -> None:
        fake = FakeTelegramClient(entities={})
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, "@ghost")
        assert exc_info.value.reason == "not_found"

    async def test_private_channel_no_access(self) -> None:
        fake = FakeTelegramClient(
            raises={"get_entity": ChannelPrivateError(request=None)}
        )
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, "@secret")
        assert exc_info.value.reason == "no_access"

    async def test_floodwait_is_not_swallowed_as_resolution_reason(self) -> None:
        # Network-level trouble maps to "unresolvable", not to a bogus
        # not_found — the caller can distinguish and retry.
        fake = FakeTelegramClient(raises={"get_entity": FloodWaitError(request=None, capture=5)})
        with pytest.raises(ResolutionError) as exc_info:
            await resolve_entity(fake, "@busy")
        assert exc_info.value.reason == "unresolvable"
