"""Tests for app.security.crypto (RULES §3: all crypto lives here)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.security.crypto import CryptoError, SessionCrypto, load_or_create_key


def test_encrypt_decrypt_roundtrip(crypto: SessionCrypto) -> None:
    session = "1BAAANk0MDgt0LongSessionStringPayloadForTesting"
    ciphertext = crypto.encrypt(session)
    assert ciphertext != session
    assert crypto.decrypt(ciphertext) == session


def test_decrypt_with_wrong_key_raises() -> None:
    first = SessionCrypto(Fernet.generate_key())
    second = SessionCrypto(Fernet.generate_key())
    ciphertext = first.encrypt("secret-session")
    with pytest.raises(CryptoError):
        second.decrypt(ciphertext)


def test_decrypt_corrupted_data_raises(crypto: SessionCrypto) -> None:
    with pytest.raises(CryptoError):
        crypto.decrypt("definitely-not-a-fernet-token")


def test_env_key_wins(tmp_path: Path) -> None:
    env_key = Fernet.generate_key().decode()
    key_file = tmp_path / "keys" / "master.key"
    assert load_or_create_key(env_key, key_file) == env_key.encode()
    assert not key_file.exists()


def test_env_key_wins_even_if_file_exists(tmp_path: Path) -> None:
    key_file = tmp_path / "master.key"
    file_key = Fernet.generate_key()
    key_file.write_bytes(file_key)
    env_key = Fernet.generate_key().decode()
    assert load_or_create_key(env_key, key_file) == env_key.encode()
    assert key_file.read_bytes() == file_key


def test_invalid_env_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_or_create_key("not-a-valid-fernet-key", tmp_path / "master.key")


def test_key_file_created_with_0600_and_reused(tmp_path: Path) -> None:
    key_file = tmp_path / "keys" / "master.key"
    first = load_or_create_key(None, key_file)
    assert key_file.exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    second = load_or_create_key(None, key_file)
    assert second == first


def test_reencrypt(crypto: SessionCrypto) -> None:
    new_key = Fernet.generate_key()
    ciphertext = crypto.encrypt("session-payload")
    rotated = crypto.reencrypt(ciphertext, new_key)
    assert SessionCrypto(new_key).decrypt(rotated) == "session-payload"
    with pytest.raises(CryptoError):
        crypto.decrypt(rotated)
