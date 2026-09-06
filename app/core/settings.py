"""In-process user settings store for transfer parameters.

Provides per-user overrides for the configurable TransferParams defaults
defined in :class:`Config`.  When no override exists the JobManager falls
back to the global Config values, so behavior is unchanged from prior
versions when settings are never touched.
"""

from __future__ import annotations

from app.config import Config

__all__ = ["UserSettings"]

#: The set of Config keys that can be overridden per-user.
OVERRIDABLE_KEYS = (
    "max_members_per_job",
    "invite_delay_seconds",
    "flood_wait_max_seconds",
    "job_timeout_seconds",
)


class UserSettings:
    """Thread-safe-by-asyncio single-taskstore for user transfer settings.

    Stores overrides keyed by ``owner_id``. Each entry maps a config-key
    to the user-supplied numeric value.  Lookups are O(1).
    """

    def __init__(self, defaults: Config) -> None:
        self._defaults = defaults
        self._overrides: dict[int, dict[str, int]] = {}

    def get(self, owner_id: int, key: str) -> int:
        """Return the effective value for *key* (override or default)."""
        if override := self._overrides.get(owner_id, {}).get(key):
            return override
        return int(getattr(self._defaults, key))

    def set(self, owner_id: int, key: str, value: int) -> None:
        """Store an override for *key*."""
        self._overrides.setdefault(owner_id, {})[key] = value

    def reset(self, owner_id: int, key: str | None = None) -> None:
        """Clear a single override or all overrides for *owner_id*."""
        if key is None:
            self._overrides.pop(owner_id, None)
        else:
            self._overrides.get(owner_id, {}).pop(key, None)

    def all_values(self, owner_id: int) -> dict[str, int]:
        """Return all effective values for *owner_id* (defaults merged with overrides)."""
        return {key: self.get(owner_id, key) for key in OVERRIDABLE_KEYS}
