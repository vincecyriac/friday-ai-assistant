"""Secrets at rest: AES-GCM under a key derived from FRIDAY_MASTER_KEY.

The setting key is bound in as associated data, so a ciphertext copied into
another setting's row cannot be decrypted there. Tokens are versioned
(``v1:``) so a future key rotation or algorithm change can coexist with old
rows during a migration.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from friday.core.config import ConfigError

MIN_MASTER_KEY_BYTES = 32
_HKDF_INFO = b"friday-vault-v1"


class MasterKeyError(ConfigError):
    """FRIDAY_MASTER_KEY is missing, malformed, or too short."""


class VaultError(ValueError):
    """A token could not be decrypted: wrong key, tampered, or malformed."""


def _b64decode_any(text: str) -> bytes:
    text = text.strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as e:
        raise ValueError(str(e)) from e


class Vault:
    VERSION = "v1"

    def __init__(self, derived_key: bytes):
        self._aead = AESGCM(derived_key)
        self._fingerprint = hashlib.sha256(derived_key).hexdigest()[:8]

    @classmethod
    def from_master_key(cls, encoded: str) -> "Vault":
        if not encoded or not encoded.strip():
            raise MasterKeyError(
                "FRIDAY_MASTER_KEY is not set — run 'friday-sentinel keygen' and add the printed line to .env")
        try:
            raw = _b64decode_any(encoded)
        except ValueError:
            raise MasterKeyError("FRIDAY_MASTER_KEY is not valid base64") from None
        if len(raw) < MIN_MASTER_KEY_BYTES:
            raise MasterKeyError(
                f"FRIDAY_MASTER_KEY must decode to at least {MIN_MASTER_KEY_BYTES} bytes, got {len(raw)}")
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(raw)
        return cls(derived)

    @staticmethod
    def generate_master_key() -> str:
        return base64.urlsafe_b64encode(os.urandom(MIN_MASTER_KEY_BYTES)).decode()

    def fingerprint(self) -> str:
        return self._fingerprint

    def encrypt(self, key: str, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), key.encode("utf-8"))
        return f"{self.VERSION}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"

    def decrypt(self, key: str, token: str) -> str:
        parts = token.split(":")
        if len(parts) != 3 or parts[0] != self.VERSION:
            raise VaultError("unrecognised vault token format")
        try:
            nonce = base64.b64decode(parts[1], validate=True)
            ct = base64.b64decode(parts[2], validate=True)
        except (binascii.Error, ValueError) as e:
            raise VaultError(f"malformed vault token: {e}") from e
        if len(nonce) != 12:
            raise VaultError("malformed vault token: bad nonce length")
        try:
            return self._aead.decrypt(nonce, ct, key.encode("utf-8")).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as e:
            raise VaultError("cannot decrypt: wrong master key, wrong setting key, or tampered data") from e
