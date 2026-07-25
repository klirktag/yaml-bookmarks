# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Turn a URL into a filesystem-safe file name (and back again).

The goal is a name that is safe on Windows, Linux and macOS and that maps the
*same* URL to the *same* file every time (so we can find a bookmark again).

Rules
-----
* A small set of characters is kept as-is: lowercase letters, digits, ``-``
  and ``.``.  These are safe everywhere and keep names readable.
* Everything else — including uppercase letters — is written as ``_XX`` where
  ``XX`` is the uppercase hex of each UTF-8 byte.

Uppercase letters are escaped on purpose: Windows and macOS file systems are
case-insensitive, so ``Foo`` and ``foo`` would otherwise collide.  Because the
escape marker ``_`` is itself escaped (to ``_5F``), the encoding is fully
reversible.

Very long URLs would blow past the 255-character file-name limit, so names
longer than :data:`MAX_NAME_LEN` are truncated and a short hash of the full URL
is appended to keep them unique.  Such names are not reversible, but the URL is
also stored inside the YAML file, so nothing is lost.
"""

from __future__ import annotations

import hashlib
import string

# Characters that are safe to leave untouched in a file name everywhere.
_SAFE = frozenset(string.ascii_lowercase + string.digits + "-.")

# Keep well under the common 255-byte per-component limit, leaving room for the
# ".yaml" suffix and the hash we may append.
MAX_NAME_LEN = 200

EXTENSION = ".yaml"


def escape_url(url: str) -> str:
    """Escape *url* into a reversible, filesystem-safe string."""
    out = []
    for byte in url.encode("utf-8"):
        ch = chr(byte)
        if ch in _SAFE:
            out.append(ch)
        else:
            out.append(f"_{byte:02X}")
    return "".join(out)


def unescape_url(name: str) -> str:
    """Reverse :func:`escape_url`.

    Raises :class:`ValueError` if *name* was truncated (contains a hash suffix)
    or is otherwise not a valid escaping.
    """
    raw = bytearray()
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == "_":
            hexpart = name[i + 1 : i + 3]
            if len(hexpart) != 2:
                raise ValueError(f"truncated escape sequence in {name!r}")
            try:
                raw.append(int(hexpart, 16))
            except ValueError as exc:
                raise ValueError(f"invalid escape sequence in {name!r}") from exc
            i += 3
        else:
            raw.append(ord(ch))
            i += 1
    return raw.decode("utf-8")


def filename_for_url(url: str) -> str:
    """Return the ``*.yaml`` file name that *url* is stored under.

    Deterministic: the same URL always yields the same file name, which is how
    add / update / remove find the right file.
    """
    escaped = escape_url(url)
    if len(escaped) > MAX_NAME_LEN:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        escaped = f"{escaped[: MAX_NAME_LEN - len(digest) - 1]}_{digest}"
    return escaped + EXTENSION
