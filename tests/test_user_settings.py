"""Tests for the in-process UserSettings override store."""

from __future__ import annotations

import pytest

from app.config import Config
from app.core.settings import OVERRIDABLE_KEYS, UserSettings


def _config() -> Config:
    return Config(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
    )


class TestUserSettings:
    def test_defaults_match_config(self) -> None:
        cfg = _config()
        s = UserSettings(cfg)
        for key in OVERRIDABLE_KEYS:
            assert s.get(1, key) == int(getattr(cfg, key))

    def test_set_override(self) -> None:
        cfg = _config()
        s = UserSettings(cfg)
        s.set(1, "max_members_per_job", 500)
        assert s.get(1, "max_members_per_job") == 500
        # other users still see the default
        assert s.get(2, "max_members_per_job") == cfg.max_members_per_job

    def test_all_values_merges_overrides(self) -> None:
        cfg = _config()
        s = UserSettings(cfg)
        s.set(1, "max_members_per_job", 300)
        s.set(1, "invite_delay_seconds", 5)
        vals = s.all_values(1)
        assert vals["max_members_per_job"] == 300
        assert vals["invite_delay_seconds"] == 5
        # untouched keys fall back to config defaults
        assert vals["flood_wait_max_seconds"] == cfg.flood_wait_max_seconds
        assert vals["job_timeout_seconds"] == cfg.job_timeout_seconds

    def test_all_values_empty_for_new_user(self) -> None:
        cfg = _config()
        s = UserSettings(cfg)
        vals = s.all_values(42)
        for key in OVERRIDABLE_KEYS:
            assert vals[key] == int(getattr(cfg, key))

    def test_reset_single_key(self) -> None:
        cfg = _config()
        s = UserSettings(cfg)
        s.set(1, "max_members_per_job", 500)
        s.reset(1, "max_members_per_job")
        assert s.get(1, "max_members_per_job") == cfg.max_members_per_job

    def test_reset_all_keys(self) -> None:
        cfg = _config()
        s = UserSettings(cfg)
        s.set(1, "max_members_per_job", 500)
        s.set(1, "invite_delay_seconds", 5)
        s.reset(1)
        assert s.all_values(1) == {k: int(getattr(cfg, k)) for k in OVERRIDABLE_KEYS}

    @pytest.mark.parametrize("key", OVERRIDABLE_KEYS)
    def test_all_overridable_keys_exist_on_config(self, key: str) -> None:
        cfg = _config()
        assert hasattr(cfg, key)
