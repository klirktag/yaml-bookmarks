# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Load, save, list and delete bookmarks stored as YAML files.

Storage is **flat**: every object is a ``<uuid>.yaml`` file directly in the
``bookmarks/`` subfolder of the base dir. The folder a bookmark belongs to is
**not** its location on disk — it is a ``path`` field *inside* the file. This
decouples the collection layout from the filesystem, and (crucially) means an
encrypted bookmark's folder lives *inside its ciphertext*: on disk it is just an
opaque ``<uuid>.yaml`` with no folder names anywhere, so an encrypted vault can be
committed to a public repo without leaking structure.

Object kinds (told apart when read):

* **Bookmark** — has a ``url`` (plus ``title``/``description``/``tags``/
  ``created``) and a ``path`` (its folder, ``""`` = root).
* **Folder** — ``type: folder`` plus a ``path``. Only needed to record an
  *empty* folder; non-empty folders are inferred from the bookmarks' ``path``.

Either kind can be encrypted: the file is then a ``crypt: true`` blob whose
decrypted payload is the record above.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import crypto
from .crypto import CryptoSession, is_encrypted, new_filename

# The base directory holds settings.yaml (and any future top-level files); the
# bookmarks themselves live in its "bookmarks/" subfolder.
BOOKMARKS_SUBDIR = "bookmarks"
DEFAULT_BASE_DIR = Path(
    os.environ.get("YAML_BOOKMARKS_DIR", Path.home() / ".yaml-bookmarks")
)
DEFAULT_STORE_DIR = DEFAULT_BASE_DIR / BOOKMARKS_SUBDIR

ORPHANED_FOLDER = "orphaned"
FOLDER_TYPE = "folder"

# Characters/names that are unsafe as folder-path segments across OSes.
_ILLEGAL_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


class VaultLocked(Exception):
    """Raised when an encrypted operation is attempted without an unlocked vault."""


class EncryptionRequired(Exception):
    """Raised when adding an unencrypted bookmark while ``allow_unencrypted`` is off."""


class _Hidden(Exception):
    """Internal: an encrypted object that can't be read in the current state."""


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


def _now_unix() -> int:
    """Current time as a unix timestamp (whole seconds since the epoch)."""
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


@dataclass
class Bookmark:
    """A single bookmark.

    ``folder`` is the collection it lives in (persisted as the ``path`` field).
    ``created`` is an optional unix timestamp. ``encrypted``/``file`` are runtime
    only (whether it's stored encrypted, and its file on disk).
    """

    url: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    folder: str = ""
    created: int | None = None
    encrypted: bool = False
    file: str = ""

    def payload(self) -> dict:
        """The record persisted to disk (or encrypted into the blob)."""
        d = {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "path": self.folder,
        }
        if self.created is not None:
            d["created"] = int(self.created)
        return d

    def to_dict(self) -> dict:
        """For the JSON API (keeps the user-facing ``folder`` name)."""
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "folder": self.folder,
            "created": self.created,
            "encrypted": self.encrypted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bookmark":
        if not data or "url" not in data:
            raise ValueError("not a bookmark (no 'url')")
        bm = cls(
            url=data["url"],
            title=data.get("title", "") or "",
            description=data.get("description", "") or "",
            tags=list(data.get("tags") or []),
            folder=normalize_folder(data.get("path")),
        )
        created = data.get("created")
        if created is not None:
            try:
                bm.created = int(created)
            except (TypeError, ValueError):
                pass
        return bm


@dataclass
class Folder:
    """A folder marker — only needed to record an *empty* folder."""

    folder: str = ""
    encrypted: bool = False
    file: str = ""

    def payload(self) -> dict:
        return {"type": FOLDER_TYPE, "path": self.folder}

    @classmethod
    def from_dict(cls, data: dict) -> "Folder":
        return cls(folder=normalize_folder(data.get("path")))


class BookmarkStore:
    """A flat directory of bookmark/folder objects (plaintext or encrypted)."""

    def __init__(self, directory: Path | str = DEFAULT_STORE_DIR):
        self.directory = Path(directory).expanduser()
        self._session: CryptoSession | None = None
        self.allow_unencrypted: bool = True
        self._entries: list | None = None  # in-memory cache of loaded objects

    # -- crypto session ------------------------------------------------------

    @property
    def is_unlocked(self) -> bool:
        return self._session is not None

    def unlock(self, password: str) -> int:
        """Engage *password*. Returns how many encrypted objects it reveals."""
        session = CryptoSession(password)
        count = 0
        active: bytes | None = None
        for path in self._files():
            data = self._load_yaml(path)
            if not is_encrypted(data):
                continue
            payload = session.decrypt(data, aad=path.name.encode("utf-8"))
            if payload is not None:
                count += 1
                if active is None:
                    try:
                        active = crypto._b64d(data["salt"])
                    except (KeyError, ValueError, TypeError):
                        pass
        session.active_salt = active if active is not None else crypto.new_salt()
        self._session = session
        self._invalidate()
        return count

    def lock(self) -> None:
        self._session = None
        self._invalidate()

    def encrypted_file_count(self) -> int:
        return sum(1 for p in self._files() if is_encrypted(self._load_yaml(p)))

    # -- reading / cache -----------------------------------------------------

    def _files(self):
        if self.directory.exists():
            yield from self.directory.glob("*.yaml")  # flat

    @staticmethod
    def _load_yaml(path: Path):
        try:
            with path.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return None

    def _read_obj(self, path: Path):
        data = self._load_yaml(path)
        if not isinstance(data, dict):
            raise ValueError(f"not an object: {path.name}")
        if is_encrypted(data):
            if self._session is None:
                raise _Hidden
            payload = self._session.decrypt(data, aad=path.name.encode("utf-8"))
            if payload is None:
                raise _Hidden  # wrong password for this file
            encrypted = True
        else:
            payload = data
            encrypted = False
        if not isinstance(payload, dict):
            raise ValueError("bad payload")
        if payload.get("type") == FOLDER_TYPE:
            obj = Folder.from_dict(payload)
        elif "url" in payload:
            obj = Bookmark.from_dict(payload)
        else:
            raise ValueError("unknown object kind")
        obj.encrypted = encrypted
        obj.file = str(path)
        return obj

    def _all(self) -> list:
        if self._entries is None:
            entries = []
            for path in self._files():
                try:
                    entries.append(self._read_obj(path))
                except (_Hidden, ValueError, yaml.YAMLError, OSError):
                    continue  # encrypted+locked, or not a valid object
            self._entries = entries
        return self._entries

    def _invalidate(self) -> None:
        self._entries = None

    def _bookmarks(self):
        return [e for e in self._all() if isinstance(e, Bookmark)]

    # -- queries -------------------------------------------------------------

    def exists(self, url: str, folder: str = "") -> bool:
        return self.get(url, folder) is not None

    def get(self, url: str, folder: str = "") -> Bookmark | None:
        folder = normalize_folder(folder)
        for b in self._bookmarks():
            if b.url == url and b.folder == folder:
                return b
        return None

    def list(self) -> list[Bookmark]:
        """All *visible* bookmarks, newest first (by ``created``; unknown last)."""
        return sorted(self._bookmarks(), key=lambda b: b.created or 0, reverse=True)

    def folders(self) -> list[str]:
        """Every folder path, including ancestors and empty (Folder-object) ones."""
        result: set[str] = set()
        for e in self._all():
            if not e.folder:
                continue
            parts = e.folder.split("/")
            for i in range(1, len(parts) + 1):
                result.add("/".join(parts[:i]))
        return sorted(result)

    # -- writing -------------------------------------------------------------

    def _require_session(self) -> CryptoSession:
        if self._session is None:
            raise VaultLocked("the vault is locked; unlock with a password first")
        return self._session

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def _dump(self, data: dict) -> str:
        return yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    def _write_object(self, obj) -> None:
        """Write a Bookmark/Folder to its file (new UUID file if it has none)."""
        if obj.file:
            path = Path(obj.file)
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / new_filename()
            while path.exists():
                path = self.directory / new_filename()
        if obj.encrypted:
            session = self._require_session()
            data = session.encrypt(
                obj.payload(), aad=path.name.encode("utf-8"), salt=session.active_salt
            )
        else:
            data = obj.payload()
        self._atomic_write(path, self._dump(data))
        obj.file = str(path)

    def _cache_append(self, obj) -> None:
        if self._entries is not None:
            self._entries.append(obj)

    def _cache_drop(self, files: set) -> None:
        if self._entries is not None:
            self._entries = [e for e in self._entries if e.file not in files]

    # -- mutations -----------------------------------------------------------

    def add(
        self,
        url: str,
        *,
        folder: str = "",
        encrypt: bool = False,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
        created: int | None = None,
    ) -> Bookmark:
        """Create a new bookmark. Raises if one already exists at that folder."""
        folder = normalize_folder(folder)
        if self.get(url, folder) is not None:
            where = f"{folder}/" if folder else "the root folder"
            raise FileExistsError(f"a bookmark already exists for {url!r} in {where}")
        if not encrypt and not self.allow_unencrypted:
            raise EncryptionRequired(
                "unencrypted bookmarks are not allowed (allow_unencrypted is false)"
            )
        if encrypt:
            self._require_session()
        bm = Bookmark(
            url=url,
            title=title,
            description=description,
            tags=list(tags or []),
            folder=folder,
            created=created if created is not None else _now_unix(),
            encrypted=encrypt,
        )
        self._write_object(bm)
        self._cache_append(bm)
        return bm

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
        bm = self.get(url, normalize_folder(folder))
        if bm is None:
            raise FileNotFoundError(f"no bookmark found for {url!r} in folder {folder!r}")
        if title is not None:
            bm.title = title
        if description is not None:
            bm.description = description
        if tags is not None:
            bm.tags = list(tags)
        self._write_object(bm)  # same file (cached object mutated in place)
        return bm

    def save(self, bookmark: Bookmark, *, encrypt: bool = False) -> Bookmark:
        """Create or overwrite a bookmark (upsert on ``(folder, url)``)."""
        bookmark.folder = normalize_folder(bookmark.folder)
        existing = self.get(bookmark.url, bookmark.folder)
        if existing is not None:
            existing.title = bookmark.title
            existing.description = bookmark.description
            existing.tags = list(bookmark.tags)
            if bookmark.created is not None:
                existing.created = bookmark.created
            self._write_object(existing)
            return existing
        if not encrypt and not self.allow_unencrypted:
            raise EncryptionRequired(
                "unencrypted bookmarks are not allowed (allow_unencrypted is false)"
            )
        if encrypt:
            self._require_session()
        bookmark.encrypted = encrypt
        bookmark.file = ""
        if bookmark.created is None:
            bookmark.created = _now_unix()
        self._write_object(bookmark)
        self._cache_append(bookmark)
        return bookmark

    def move(self, url: str, src_folder: str, dst_folder: str) -> Bookmark:
        """Move a bookmark to another folder (rewrites its ``path`` field)."""
        bm = self.get(url, normalize_folder(src_folder))
        if bm is None:
            raise FileNotFoundError(f"no bookmark found for {url!r} in folder {src_folder!r}")
        if bm.encrypted:
            self._require_session()
        bm.folder = normalize_folder(dst_folder)
        self._write_object(bm)  # same file, re-encrypted with a fresh nonce
        return bm

    def remove(self, url: str, folder: str = "") -> bool:
        """Delete a bookmark. Returns True if one was removed."""
        bm = self.get(url, normalize_folder(folder))
        if bm is None:
            return False
        Path(bm.file).unlink(missing_ok=True)
        self._cache_drop({bm.file})
        return True

    # -- folder management ---------------------------------------------------

    def create_folder(self, folder: str) -> str:
        """Record an (empty) folder via a Folder object. No-op if it exists."""
        folder = normalize_folder(folder)
        if not folder or folder in set(self.folders()):
            return folder
        encrypt = self._session is not None
        if not encrypt and not self.allow_unencrypted:
            raise EncryptionRequired(
                "unencrypted folders are not allowed (allow_unencrypted is false)"
            )
        fo = Folder(folder=folder, encrypted=encrypt)
        self._write_object(fo)
        self._cache_append(fo)
        return folder

    def _reprefix(self, src: str, dst: str) -> str:
        src = normalize_folder(src)
        dst = normalize_folder(dst)
        if not src:
            raise ValueError("cannot move or rename the root folder")
        if not dst:
            raise ValueError("a destination folder is required")
        if dst == src:
            return dst
        if dst.startswith(src + "/"):
            raise ValueError("cannot move a folder into itself")
        if dst in set(self.folders()):
            raise ValueError(f"a folder {dst!r} already exists")
        for e in self._all():
            if e.folder == src or e.folder.startswith(src + "/"):
                e.folder = dst + e.folder[len(src):]
                if e.encrypted:
                    self._require_session()
                self._write_object(e)
        return dst

    def rename_folder(self, folder: str, new_name: str) -> str:
        folder = normalize_folder(folder)
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("a folder name is required")
        parent = "/".join(folder.split("/")[:-1])
        target = f"{parent}/{new_name}" if parent else new_name
        return self._reprefix(folder, target)

    def move_folder(self, folder: str, new_parent: str) -> str:
        folder = normalize_folder(folder)
        leaf = folder.split("/")[-1]
        new_parent = normalize_folder(new_parent)
        target = f"{new_parent}/{leaf}" if new_parent else leaf
        return self._reprefix(folder, target)

    def delete_folder(self, folder: str) -> str:
        """Delete a folder: bookmarks inside move to ``orphaned/``; markers go."""
        folder = normalize_folder(folder)
        if not folder:
            raise ValueError("cannot delete the root folder")
        if folder == ORPHANED_FOLDER or folder.startswith(ORPHANED_FOLDER + "/"):
            raise ValueError(f"cannot delete the {ORPHANED_FOLDER!r} folder")
        dropped: set = set()
        for e in list(self._all()):
            if not (e.folder == folder or e.folder.startswith(folder + "/")):
                continue
            if isinstance(e, Bookmark):
                e.folder = ORPHANED_FOLDER + e.folder[len(folder):]
                if e.encrypted:
                    self._require_session()
                self._write_object(e)
            else:  # a Folder marker under the deleted folder — remove it
                Path(e.file).unlink(missing_ok=True)
                dropped.add(e.file)
        self._cache_drop(dropped)
        return ORPHANED_FOLDER
