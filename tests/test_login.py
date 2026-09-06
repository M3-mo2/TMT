"""Offline tests for app.tg.login — full flow, 2FA, failures, TTL sweeper."""

from __future__ import annotations

import asyncio
import logging

import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
)

from app.tg.errors import LoginFailure
from app.tg.login import LoginFlowManager, LoginResult
from tests.fakes import FakeTelegramClient

SECRET_SESSION = "1BQANOTEuMTA0LXN1cGVyLXNlY3JldC1mYWtlLXNlc3Npb24tc3RyaW5n"


def make_manager(
    fake: FakeTelegramClient, ttl_seconds: int = 600
) -> LoginFlowManager:
    return LoginFlowManager(
        12345,
        "api-hash",
        ttl_seconds=ttl_seconds,
        client_factory=lambda: fake,
    )


@pytest.fixture
def fake() -> FakeTelegramClient:
    return FakeTelegramClient(session_string=SECRET_SESSION)


class TestFullFlow:
    async def test_phone_code_session(self, fake: FakeTelegramClient) -> None:
        manager = make_manager(fake)
        await manager.start(101, "+905001234567")
        assert manager.has_flow(101)
        assert ("send_code_request", ("+905001234567",)) in fake.calls

        session_string = await manager.submit_code(101, "1 2 3 4 5")
        assert session_string == SECRET_SESSION
        assert fake.session.save_count == 1
        assert manager.has_flow(101) is False
        assert fake.disconnect_count == 1  # temp client torn down

    async def test_phone_normalized_to_e164(self, fake: FakeTelegramClient) -> None:
        manager = make_manager(fake)
        await manager.start(101, "905001234567")
        assert ("send_code_request", ("+905001234567",)) in fake.calls

    async def test_result_not_available_via_manager_state(
        self, fake: FakeTelegramClient
    ) -> None:
        # Only the caller receives the session string; the manager keeps
        # no residual per-owner state after success.
        manager = make_manager(fake)
        await manager.start(101, "+905001234567")
        await manager.submit_code(101, "12345")
        assert manager._flows == {}


class TestTwoFactor:
    async def test_password_branch(self, fake: FakeTelegramClient) -> None:
        fake.code_flow["sign_in"] = "password"
        manager = make_manager(fake)
        await manager.start(101, "+905001234567")

        assert await manager.submit_code(101, "12345") == "password"
        assert manager.has_flow(101)  # flow alive, awaiting the password

        result = await manager.submit_password(101, "s3cret")
        assert isinstance(result, LoginResult)
        assert result.session_string == SECRET_SESSION
        assert result.tg_user_id == 900001
        assert result.username == "fake_user"
        assert result.display_name == "Fake"
        assert manager.has_flow(101) is False

    async def test_wrong_password_keeps_flow_for_retry(
        self, fake: FakeTelegramClient
    ) -> None:
        fake.code_flow["sign_in"] = "password"
        fake.raises["sign_in_password"] = PasswordHashInvalidError(request=None)
        manager = make_manager(fake)
        await manager.start(101, "+905001234567")
        await manager.submit_code(101, "12345")

        with pytest.raises(LoginFailure) as exc_info:
            await manager.submit_password(101, "wrong")
        assert exc_info.value.key == "PASSWORD_INVALID"
        assert manager.has_flow(101)

        fake.raises.clear()
        result = await manager.submit_password(101, "right")
        assert result.session_string == SECRET_SESSION

    async def test_password_without_password_stage_fails(
        self, fake: FakeTelegramClient
    ) -> None:
        manager = make_manager(fake)
        with pytest.raises(LoginFailure):
            await manager.submit_password(101, "s3cret")


class TestFailures:
    async def test_wrong_code(self, fake: FakeTelegramClient) -> None:
        fake.raises["sign_in"] = PhoneCodeInvalidError(request=None)
        manager = make_manager(fake)
        await manager.start(101, "+905001234567")
        with pytest.raises(LoginFailure) as exc_info:
            await manager.submit_code(101, "00000")
        assert exc_info.value.key == "CODE_INVALID"
        assert manager.has_flow(101)  # wrong code is retryable

    async def test_flood_on_send_code(self, fake: FakeTelegramClient) -> None:
        fake.raises["send_code_request"] = FloodWaitError(request=None, capture=30)
        manager = make_manager(fake)
        with pytest.raises(LoginFailure) as exc_info:
            await manager.start(101, "+905001234567")
        assert exc_info.value.key == "PHONE_FLOOD"
        assert exc_info.value.wait_seconds == 30
        assert manager.has_flow(101) is False
        assert fake.disconnect_count == 1  # temp client cleaned up

    @pytest.mark.parametrize("bad", ["123", "90500abc1234", "+", "123456789012345678"])
    async def test_invalid_phone(
        self, fake: FakeTelegramClient, bad: str
    ) -> None:
        manager = make_manager(fake)
        with pytest.raises(LoginFailure) as exc_info:
            await manager.start(101, bad)
        assert exc_info.value.key == "PHONE_INVALID"
        assert manager.has_flow(101) is False
        assert fake.calls == []  # nothing sent to Telegram

    async def test_code_without_flow(self, fake: FakeTelegramClient) -> None:
        manager = make_manager(fake)
        with pytest.raises(LoginFailure):
            await manager.submit_code(101, "12345")

    async def test_disconnect_failure_does_not_mask_login_failure(
        self, fake: FakeTelegramClient
    ) -> None:
        # Regression: teardown errors on the temp client must not replace
        # the classified LoginFailure the caller has to render.
        async def broken_disconnect() -> None:
            raise ConnectionError("disconnect exploded")

        fake.disconnect = broken_disconnect  # type: ignore[method-assign]
        fake.raises["send_code_request"] = FloodWaitError(request=None, capture=5)
        manager = make_manager(fake)
        with pytest.raises(LoginFailure) as exc_info:
            await manager.start(101, "+905001234567")
        assert exc_info.value.key == "PHONE_FLOOD"
        assert manager.has_flow(101) is False

    async def test_restart_replaces_existing_flow(
        self, fake: FakeTelegramClient
    ) -> None:
        manager = make_manager(fake)
        await manager.start(101, "+905001234567")
        await manager.start(101, "+905009999999")
        assert fake.disconnect_count == 1  # the first temp client was dropped
        assert ("send_code_request", ("+905009999999",)) in fake.calls


class TestCancelAndSweeper:
    async def test_cancel_is_idempotent(self, fake: FakeTelegramClient) -> None:
        manager = make_manager(fake)
        await manager.cancel(101)  # no flow: no-op
        await manager.start(101, "+905001234567")
        await manager.cancel(101)
        assert manager.has_flow(101) is False
        assert fake.disconnect_count == 1
        await manager.cancel(101)  # still fine

    async def test_sweeper_drops_expired_flow(self, fake: FakeTelegramClient) -> None:
        manager = make_manager(fake, ttl_seconds=0)
        await manager.start(101, "+905001234567")
        manager.start_sweeper()
        try:
            await asyncio.sleep(0.3)
            assert manager.has_flow(101) is False
            assert fake.disconnect_count == 1
        finally:
            await manager.stop_sweeper()

    async def test_stop_sweeper_without_start_is_noop(
        self, fake: FakeTelegramClient
    ) -> None:
        manager = make_manager(fake)
        await manager.stop_sweeper()

    async def test_alive_flow_survives_sweep(self, fake: FakeTelegramClient) -> None:
        manager = make_manager(fake, ttl_seconds=600)
        await manager.start(101, "+905001234567")
        manager.start_sweeper()
        try:
            await asyncio.sleep(0.2)
            assert manager.has_flow(101)
        finally:
            await manager.stop_sweeper()


class TestSecretsNeverLogged:
    async def test_no_secrets_in_logs(
        self,
        fake: FakeTelegramClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake.code_flow["sign_in"] = "password"
        manager = make_manager(fake)
        with caplog.at_level(logging.DEBUG, logger="app.tg"):
            await manager.start(101, "+905001234567")
            assert await manager.submit_code(101, "99999") == "password"
            await manager.submit_password(101, "s3cret")
            await manager.start(102, "+905009999999")
            await manager.cancel(102)
        log_text = caplog.text
        assert SECRET_SESSION not in log_text
        assert "s3cret" not in log_text
        assert "99999" not in log_text
        assert "phone_code_hash" not in log_text
