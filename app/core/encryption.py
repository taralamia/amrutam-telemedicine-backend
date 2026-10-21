import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.types import Text, TypeDecorator

from app.core.config import get_settings


def _key() -> bytes:
    configured = get_settings().data_encryption_key.get_secret_value().encode()
    return hashlib.sha256(configured).digest()


def encrypt_text(value: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, value.encode(), None)
    return "v1:" + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_text(value: str) -> str:
    version, encoded = value.split(":", 1)
    if version != "v1":
        raise ValueError(f"Unsupported encrypted data version {version}")
    payload = base64.urlsafe_b64decode(encoded.encode())
    return AESGCM(_key()).decrypt(payload[:12], payload[12:], None).decode()


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return encrypt_text(value) if value is not None else None

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return decrypt_text(value) if value is not None else None


class EncryptedJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect) -> str | None:
        if value is None:
            return None
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return encrypt_text(serialized)

    def process_result_value(self, value: str | None, dialect) -> dict[str, Any] | None:
        if value is None:
            return None
        decoded = json.loads(decrypt_text(value))
        if not isinstance(decoded, dict):
            raise ValueError("Encrypted JSON value must contain a JSON object")
        return decoded
