"""Shared-passphrase encryption for a relay network.

Every device in the same network (network name + passphrase) derives the same
Fernet key and can read each other's traffic. This is *group* encryption, not
per-peer end-to-end encryption: anyone who knows the passphrase can decrypt
everything on that network, including "direct" messages (those are routed by
topic, not cryptographically private from other members). Use a separate
network name/passphrase pair for a private channel between two devices.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

__all__ = ["Cipher", "InvalidToken", "topic_namespace"]

# The network *name* is only a salt for key derivation and a topic label.
# It is deliberately independent of the passphrase, so rotating the
# passphrase alone doesn't move everyone to a different topic namespace.
_KDF_ITERATIONS = 390_000


def _derive_key(passphrase: str, network_name: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=network_name.strip().lower().encode("utf-8"),
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class Cipher:
    def __init__(self, passphrase: str, network_name: str):
        self._fernet = Fernet(_derive_key(passphrase, network_name))

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        return self._fernet.decrypt(token)


def topic_namespace(network_name: str) -> str:
    """Human-readable, MQTT-topic-safe namespace derived from the network name."""
    slug = "".join(c if c.isalnum() or c in "-_." else "-" for c in network_name.strip().lower())
    slug = slug.strip("-") or "default"
    return f"omarchy-relay/v1/{slug}"
