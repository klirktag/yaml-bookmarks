# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Import bookmarks from other tools' export files.

Currently supports the Raindrop.io CSV export, whose columns are:
``id,title,note,excerpt,url,folder,tags,created,cover,highlights,favorite``.

Folders in that export are nested with `` / `` (space-slash-space), the special
folder ``Unsorted`` is the root, and a literal ``/`` inside a name (e.g.
``Båt/kanot``) is part of a single collection name (sanitised to ``-``).
"""

from __future__ import annotations

import csv
import datetime as _dt
import io

from .storage import Bookmark, BookmarkStore, EncryptionRequired, VaultLocked

# Characters that can't appear in a folder segment (superset of storage's rule,
# also excluding "/" since that is our path separator).
_ILLEGAL = set('<>:"|?*\\/') | {chr(c) for c in range(32)}
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


def _safe_segment(segment: str) -> str:
    cleaned = "".join("-" if ch in _ILLEGAL else ch for ch in segment).strip()
    cleaned = cleaned.rstrip(" .")  # Windows dislikes trailing dots/spaces
    if not cleaned:
        cleaned = "imported"
    if cleaned.lower() in _RESERVED:
        cleaned += "_"
    return cleaned


def _raindrop_folder(raw: str | None) -> str:
    raw = (raw or "").strip()
    if not raw or raw.lower() == "unsorted":
        return ""  # Raindrop's "Unsorted" maps to our root
    segments = [_safe_segment(s) for s in raw.split(" / ") if s.strip()]
    return "/".join(segments)


def _split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _parse_created_unix(raw: str | None) -> int | None:
    """Raindrop's ISO timestamp (e.g. 2026-03-30T11:36:02.712Z) → unix seconds."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def parse_raindrop_csv(text: str) -> list[Bookmark]:
    """Parse Raindrop CSV text into a list of (unsaved) Bookmarks."""
    reader = csv.DictReader(io.StringIO(text))
    bookmarks: list[Bookmark] = []
    for row in reader:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        description = (row.get("note") or "").strip() or (row.get("excerpt") or "").strip()
        bookmarks.append(
            Bookmark(
                url=url,
                title=(row.get("title") or "").strip(),
                description=description,
                tags=_split_tags(row.get("tags")),
                folder=_raindrop_folder(row.get("folder")),
                created=_parse_created_unix(row.get("created")),
            )
        )
    return bookmarks


def import_raindrop(store: BookmarkStore, text: str, *, encrypt: bool = False) -> dict:
    """Import Raindrop CSV *text* into *store* (upsert). Returns a summary."""
    bookmarks = parse_raindrop_csv(text)
    added = 0
    failed = 0
    for bookmark in bookmarks:
        try:
            store.save(bookmark, encrypt=encrypt)
            added += 1
        except (VaultLocked, EncryptionRequired, ValueError, OSError):
            failed += 1
    return {
        "format": "raindrop",
        "total": len(bookmarks),
        "added": added,
        "failed": failed,
        "encrypted": encrypt,
    }
