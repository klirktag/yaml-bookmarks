# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
import base64

from yaml_bookmarks.crypto import (
    CryptoSession,
    is_encrypted,
    new_filename,
    new_salt,
)


def test_roundtrip():
    salt = new_salt()
    enc = CryptoSession("pw").encrypt({"url": "https://x", "title": "T"}, aad=b"f.yaml", salt=salt)
    assert is_encrypted(enc)
    out = CryptoSession("pw").decrypt(enc, aad=b"f.yaml")
    assert out == {"url": "https://x", "title": "T"}


def test_wrong_password_fails():
    salt = new_salt()
    enc = CryptoSession("right").encrypt({"url": "u"}, aad=b"f.yaml", salt=salt)
    assert CryptoSession("wrong").decrypt(enc, aad=b"f.yaml") is None


def test_aad_binding():
    # A ciphertext bound to one filename must not verify under another.
    salt = new_salt()
    enc = CryptoSession("pw").encrypt({"url": "u"}, aad=b"a.yaml", salt=salt)
    assert CryptoSession("pw").decrypt(enc, aad=b"b.yaml") is None
    assert CryptoSession("pw").decrypt(enc, aad=b"a.yaml") == {"url": "u"}


def test_tampering_detected():
    salt = new_salt()
    enc = CryptoSession("pw").encrypt({"url": "u"}, aad=b"f.yaml", salt=salt)
    ct = bytearray(base64.b64decode(enc["ciphertext"]))
    ct[0] ^= 0x01
    enc["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
    assert CryptoSession("pw").decrypt(enc, aad=b"f.yaml") is None


def test_same_password_same_salt_reuses_key():
    salt = new_salt()
    s = CryptoSession("pw")
    s.encrypt({"url": "a"}, aad=b"1.yaml", salt=salt)
    # second use of the same salt must hit the cache, not re-derive
    assert len(s._keys) == 1
    s.encrypt({"url": "b"}, aad=b"2.yaml", salt=salt)
    assert len(s._keys) == 1


def test_new_filename_is_uuid_yaml():
    name = new_filename()
    assert name.endswith(".yaml") and len(name) == len("0" * 32 + ".yaml")


def test_is_encrypted():
    assert is_encrypted({"crypt": True})
    assert not is_encrypted({"url": "u"})
    assert not is_encrypted(None)
