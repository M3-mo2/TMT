"""Session-string encryption at rest (PRD §7).

The master key comes from config (env) or a generated key file with 0600
permissions. Nothing else in the project performs cryptography — RULES §3.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("app.security.crypto")


class CryptoError(Exception):
    """Raised when decryption fails (wrong key / corrupted ciphertext)."""


def load_or_create_key(env_key: str | None, key_file: Path) -> bytes:
    """Resolve the Fernet master key.

    Priority: SESSIONS_MASTER_KEY env var, then the key file (created on first
    run with 0600 perms inside a 0700 directory).
    """
    if env_key:
        env_key = env_key.strip()
        # Validate by constructing a Fernet (raises ValueError on bad keys).
        Fernet(env_key.encode())
        return env_key.encode()

    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_file.parent, stat.S_IRWXU)  # 0700, best effort
    except OSError:
        logger.warning("Could not tighten permissions on %s", key_file.parent)
    if key_file.exists():
        key = key_file.read_bytes().strip()
        Fernet(key)  # validate
        return key
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    logger.info("Generated new session master key at %s", key_file)
    return key


class SessionCrypto:
    """Encrypts/decrypts Telethon StringSession payloads."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            raise CryptoError(
                "Session decryption failed — wrong master key or corrupted data."
            ) from exc

    def reencrypt(self, ciphertext: str, new_key: bytes) -> str:
        """Rotate: decrypt with the current key, encrypt with a new one."""
        return Fernet(new_key).encrypt(self.decrypt(ciphertext).encode()).decode()
