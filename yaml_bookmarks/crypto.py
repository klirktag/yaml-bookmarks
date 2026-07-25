# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Password-based encryption for individual bookmarks.

See ``docs/encryption.md`` for the full design. In short: the whole bookmark
record is encrypted with AES-256-GCM under a key derived from the user's
password with scrypt. A per-vault salt is embedded in each file, each file has a
unique nonce, and the file name is bound as associated data (AAD).

A :class:`CryptoSession` holds one password in memory and derives/caches keys
(one scrypt run per distinct salt), so listing a whole vault costs one KDF run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CRYPT_VERSION = 1
KDF_NAME = "scrypt"

# scrypt work factors. n=2**15,r=8 ≈ 150 ms and ~32 MiB per derivation — enough
# to make offline password guessing expensive without being noticeable on unlock.
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32       # 256-bit key
_SALT_LEN = 16
_NONCE_LEN = 12     # AES-GCM standard nonce size

DEFAULT_KDF_PARAMS = {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P}


def new_salt() -> bytes:
    return os.urandom(_SALT_LEN)


def new_filename() -> str:
    """A random ``<uuid4>.yaml`` name for an encrypted bookmark (leaks nothing)."""
    return uuid.uuid4().hex + ".yaml"


def is_encrypted(data) -> bool:
    """True if a loaded YAML mapping is an encrypted bookmark (`crypt: true`)."""
    return bool(isinstance(data, dict) and data.get("crypt"))


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text)


def _derive(password: str, salt: bytes, params: dict) -> bytes:
    n = int(params.get("n", _SCRYPT_N))
    r = int(params.get("r", _SCRYPT_R))
    p = int(params.get("p", _SCRYPT_P))
    # scrypt uses ~128*n*r bytes; give the cap comfortable head-room.
    maxmem = 128 * n * r * 2 + (1 << 20)
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_LEN, maxmem=maxmem
    )


class CryptoSession:
    """One engaged password. Derives keys lazily and caches them per salt."""

    def __init__(self, password: str):
        self._password = password
        self._keys: dict[tuple, bytes] = {}
        # Salt used when encrypting *new* bookmarks (the vault the user is in).
        self.active_salt: bytes = b""

    def _key(self, salt: bytes, params: dict) -> bytes:
        cache_key = (salt, params.get("n"), params.get("r"), params.get("p"))
        if cache_key not in self._keys:
            self._keys[cache_key] = _derive(self._password, salt, params)
        return self._keys[cache_key]

    def decrypt(self, data: dict, aad: bytes) -> dict | None:
        """Return the decrypted record, or ``None`` if this password can't open it."""
        try:
            salt = _b64d(data["salt"])
            nonce = _b64d(data["nonce"])
            ciphertext = _b64d(data["ciphertext"])
            params = data.get("kdf_params") or DEFAULT_KDF_PARAMS
            key = self._key(salt, params)
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
            payload = json.loads(plaintext.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (InvalidTag, KeyError, ValueError, TypeError):
            return None

    def encrypt(self, payload: dict, aad: bytes, salt: bytes | None = None) -> dict:
        """Encrypt *payload* into the on-disk mapping for an encrypted bookmark."""
        if not salt:
            salt = self.active_salt or new_salt()
        params = dict(DEFAULT_KDF_PARAMS)
        key = self._key(salt, params)
        nonce = os.urandom(_NONCE_LEN)
        plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        return {
            "crypt": True,
            "version": CRYPT_VERSION,
            "kdf": KDF_NAME,
            "kdf_params": params,
            "salt": _b64e(salt),
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(ciphertext),
        }
