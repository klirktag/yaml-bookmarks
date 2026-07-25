# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Global application settings, stored in ``<store>/settings.yaml``.

The settings file always lives in the root of the bookmark directory (next to the
top-level bookmarks). It is created with documented defaults on first run and is
never treated as a bookmark by :class:`~yaml_bookmarks.storage.BookmarkStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SETTINGS_FILENAME = "settings.yaml"
DEFAULT_PORT = 22222


@dataclass
class Settings:
    port: int = DEFAULT_PORT
    allow_unencrypted: bool = True


_TEMPLATE = """\
# yaml-bookmarks — global settings.
# Edit these values and restart the web server for changes to take effect.

# Port the web UI (`yaml-bookmarks web`) listens on.
port: 22222

# If false, every new bookmark must be encrypted: adding an unencrypted bookmark
# is rejected. Existing unencrypted bookmarks are left untouched.
allow_unencrypted: true
"""


def settings_path(directory: Path | str) -> Path:
    return Path(directory).expanduser() / SETTINGS_FILENAME


def _coerce_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def load_settings(directory: Path | str) -> Settings:
    """Read settings from ``<directory>/settings.yaml`` (defaults if missing/bad)."""
    path = settings_path(directory)
    data: dict = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, yaml.YAMLError):
            data = {}
    return Settings(
        port=_coerce_port(data.get("port", DEFAULT_PORT)),
        allow_unencrypted=_coerce_bool(data.get("allow_unencrypted"), True),
    )


def ensure_settings_file(directory: Path | str) -> Settings:
    """Create the settings file with documented defaults if absent, then load it."""
    path = settings_path(directory)
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
    return load_settings(directory)
