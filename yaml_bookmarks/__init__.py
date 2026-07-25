# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""yaml-bookmarks — a personal, YAML-backed bookmark manager.

Bookmarks are stored as individual YAML files under ``$HOME/.yaml-bookmarks/``.
Each file is named after a filesystem-safe escaping of the bookmark URL so the
same URL always maps to the same file on Windows, Linux and macOS.
"""

from .escaping import escape_url, unescape_url, filename_for_url
from .storage import Bookmark, BookmarkStore, DEFAULT_STORE_DIR, VaultLocked

__all__ = [
    "Bookmark",
    "BookmarkStore",
    "DEFAULT_STORE_DIR",
    "VaultLocked",
    "escape_url",
    "unescape_url",
    "filename_for_url",
]

__version__ = "0.1.0"
