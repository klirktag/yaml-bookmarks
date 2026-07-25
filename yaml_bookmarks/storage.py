# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Load, save, list and delete bookmarks stored as YAML files.

Bookmarks may be organised into folders and subfolders.  A bookmark's folder is
simply *where its file lives* under the store directory — e.g. a bookmark in
folder ``work/projects`` is the file
``~/.yaml-bookmarks/work/projects/<escaped-url>.yaml``.  The filesystem is the
single source of truth for folders, so the folder is derived from the path and
is **not** written into the YAML itself.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from .escaping import filename_for_url

DEFAULT_STORE_DIR = Path(os.environ.get("YAML_BOOKMARKS_DIR", Path.home() / ".yaml-bookmarks"))

# Characters/names that are unsafe as directory names across Windows/macOS/Linux.
_ILLEGAL_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


def normalize_folder(folder: str | None) -> str:
    """Validate and normalise a folder path to a clean relative POSIX path.

    ``""`` (or ``None``) means the root.  Raises :class:`ValueError` for unsafe
    names or path traversal (``..``).
    """
    if not folder:
        return ""
    parts: list[str] = []
    for raw in str(folder).replace("\\", "/").split("/"):
        name = raw.strip()
        if name in ("", "."):
            continue
        if name == "..":
            raise ValueError("folder path may not contain '..'")
        if any(ch in _ILLEGAL_CHARS for ch in name):
            raise ValueError(f"folder name {name!r} contains an illegal character")
        if name.endswith(".") or name.endswith(" "):
            raise ValueError(f"folder name {name!r} may not end with a space or a dot")
        if name.lower() in _RESERVED_NAMES:
            raise ValueError(f"{name!r} is a reserved name")
        parts.append(name)
    return "/".join(parts)


def _now() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Bookmark:
    """A single bookmark. ``folder`` is derived from the file location."""

    url: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    folder: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        """Full dict including ``folder`` (used by the JSON API)."""
        return dataclasses.asdict(self)

    def to_yaml_dict(self) -> dict:
        """Persisted form: everything except ``folder`` (the path holds that)."""
        data = dataclasses.asdict(self)
        data.pop("folder", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Bookmark":
        if not data or "url" not in data:
            raise ValueError("bookmark file is missing a 'url' field")
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class BookmarkStore:
    """A directory tree full of bookmark YAML files."""

    def __init__(self, directory: Path | str = DEFAULT_STORE_DIR):
        self.directory = Path(directory).expanduser()

    # -- paths ---------------------------------------------------------------

    def _path_for(self, url: str, folder: str = "") -> Path:
        folder = normalize_folder(folder)
        base = self.directory
        if folder:
            base = base.joinpath(*folder.split("/"))
        return base / filename_for_url(url)

    def _folder_of(self, path: Path) -> str:
        rel = path.parent.relative_to(self.directory)
        return "" if rel == Path(".") else rel.as_posix()

    # -- queries -------------------------------------------------------------

    def exists(self, url: str, folder: str = "") -> bool:
        return self._path_for(url, folder).exists()

    def get(self, url: str, folder: str = "") -> Bookmark | None:
        path = self._path_for(url, folder)
        if not path.exists():
            return None
        return self._read(path)

    def list(self) -> list[Bookmark]:
        """All bookmarks (recursively), most recently updated first."""
        return sorted(
            self._iter(),
            key=lambda b: b.updated_at or b.created_at,
            reverse=True,
        )

    def folders(self) -> list[str]:
        """Every folder path under the store, including empty ones."""
        result: set[str] = set()
        if self.directory.exists():
            for p in self.directory.rglob("*"):
                if p.is_dir():
                    result.add(p.relative_to(self.directory).as_posix())
        return sorted(result)

    def _iter(self) -> Iterator[Bookmark]:
        if not self.directory.exists():
            return
        for path in self.directory.rglob("*.yaml"):
            try:
                yield self._read(path)
            except (ValueError, yaml.YAMLError, OSError):
                # Skip files that aren't valid bookmarks rather than crash.
                continue

    def _read(self, path: Path) -> Bookmark:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        bookmark = Bookmark.from_dict(data)
        bookmark.folder = self._folder_of(path)  # location wins over file content
        return bookmark

    # -- folder management ---------------------------------------------------

    def create_folder(self, folder: str) -> str:
        folder = normalize_folder(folder)
        if folder:
            self.directory.joinpath(*folder.split("/")).mkdir(parents=True, exist_ok=True)
        return folder

    # -- mutations -----------------------------------------------------------

    def add(
        self,
        url: str,
        *,
        folder: str = "",
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> Bookmark:
        """Create a new bookmark. Raises if one already exists at that folder."""
        folder = normalize_folder(folder)
        if self.exists(url, folder):
            where = f"{folder}/" if folder else "the root folder"
            raise FileExistsError(f"a bookmark already exists for {url!r} in {where}")
        now = _now()
        bookmark = Bookmark(
            url=url,
            title=title,
            description=description,
            tags=list(tags or []),
            folder=folder,
            created_at=now,
            updated_at=now,
        )
        self._write(bookmark)
        return bookmark

    def update(
        self,
        url: str,
        *,
        folder: str = "",
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> Bookmark:
        """Update fields of an existing bookmark. Only passed fields change."""
        folder = normalize_folder(folder)
        bookmark = self.get(url, folder)
        if bookmark is None:
            raise FileNotFoundError(f"no bookmark found for {url!r} in folder {folder!r}")
        if title is not None:
            bookmark.title = title
        if description is not None:
            bookmark.description = description
        if tags is not None:
            bookmark.tags = list(tags)
        bookmark.updated_at = _now()
        self._write(bookmark)
        return bookmark

    def save(self, bookmark: Bookmark) -> Bookmark:
        """Create or overwrite a bookmark at ``bookmark.folder`` (upsert)."""
        bookmark.folder = normalize_folder(bookmark.folder)
        now = _now()
        if not bookmark.created_at:
            existing = self.get(bookmark.url, bookmark.folder)
            bookmark.created_at = existing.created_at if existing else now
        bookmark.updated_at = now
        self._write(bookmark)
        return bookmark

    def move(self, url: str, src_folder: str, dst_folder: str) -> Bookmark:
        """Move a bookmark from one folder to another, keeping its content."""
        src = self._path_for(url, src_folder)
        if not src.exists():
            raise FileNotFoundError(f"no bookmark found for {url!r} in folder {src_folder!r}")
        bookmark = self._read(src)
        bookmark.folder = normalize_folder(dst_folder)
        bookmark.updated_at = _now()
        self._write(bookmark)  # writes to the destination
        dst = self._path_for(url, bookmark.folder)
        if src.resolve() != dst.resolve():
            src.unlink()
        return bookmark

    def remove(self, url: str, folder: str = "") -> bool:
        """Delete a bookmark. Returns True if a file was removed."""
        try:
            self._path_for(url, folder).unlink()
            return True
        except FileNotFoundError:
            return False

    def _write(self, bookmark: Bookmark) -> None:
        path = self._path_for(bookmark.url, bookmark.folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            bookmark.to_yaml_dict(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        # Write atomically so a crash mid-write can't corrupt an existing file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
