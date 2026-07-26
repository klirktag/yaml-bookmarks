# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Load, save, list and delete bookmarks stored as YAML files.

Bookmark files live in the ``bookmarks/`` subfolder of the base directory
(``settings.yaml`` and any future top-level files sit in the base dir itself).
A bookmark's folder is simply *where its file lives* under the store directory —
e.g. a bookmark in folder ``work/projects`` is the file
``~/.yaml-bookmarks/bookmarks/work/projects/<escaped-url>.yaml``.  The filesystem
is the single source of truth for folders, so the folder is derived from the path
and is **not** written into the YAML itself.

Bookmarks may also be **encrypted** (see ``crypto.py`` and
``docs/encryption.md``).  An encrypted bookmark is a file with ``crypt: true``, a
random ``<uuid>.yaml`` name, and its whole record stored as one ciphertext blob.
Encrypted bookmarks are invisible until the store is unlocked with the right
password; once unlocked they behave like any other bookmark.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from . import crypto
from .crypto import CryptoSession, is_encrypted, new_filename
from .escaping import filename_for_url

# The base directory holds settings.yaml (and any future top-level files); the
# bookmarks themselves live in its "bookmarks/" subfolder.
BOOKMARKS_SUBDIR = "bookmarks"
DEFAULT_BASE_DIR = Path(
    os.environ.get("YAML_BOOKMARKS_DIR", Path.home() / ".yaml-bookmarks")
)
DEFAULT_STORE_DIR = DEFAULT_BASE_DIR / BOOKMARKS_SUBDIR

# Characters/names that are unsafe as directory names across Windows/macOS/Linux.
_ILLEGAL_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


ORPHANED_FOLDER = "orphaned"


class VaultLocked(Exception):
    """Raised when an encrypted operation is attempted without an unlocked vault."""


class EncryptionRequired(Exception):
    """Raised when adding an unencrypted bookmark while ``allow_unencrypted`` is off."""


class _LockedBookmark(Exception):
    """Internal: a file is encrypted and cannot be read in the current state."""


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


# The always-present fields of a bookmark's persisted record. ``created`` (an
# optional unix timestamp) is persisted only when set; folder/encrypted/path are
# runtime-only and never written.
_PAYLOAD_FIELDS = ("url", "title", "description", "tags")


@dataclass
class Bookmark:
    """A single bookmark.

    ``created`` is an optional unix timestamp (whole seconds since the epoch) —
    the moment the bookmark was created. It may be absent on older bookmarks that
    predate the field. ``folder``/``encrypted``/``path`` are runtime-derived and
    never persisted.
    """

    url: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    folder: str = ""
    created: int | None = None
    encrypted: bool = False
    path: str = ""

    def payload(self) -> dict:
        """The persisted record: core fields, plus ``created`` when set."""
        d = {k: getattr(self, k) for k in _PAYLOAD_FIELDS}
        if self.created is not None:
            d["created"] = int(self.created)
        return d

    def to_dict(self) -> dict:
        """For the JSON API: payload plus ``folder``, ``encrypted`` and ``created``."""
        d = self.payload()
        d["folder"] = self.folder
        d["encrypted"] = self.encrypted
        d["created"] = self.created
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Bookmark":
        if not data or "url" not in data:
            raise ValueError("bookmark file is missing a 'url' field")
        fields = {k: v for k, v in data.items() if k in _PAYLOAD_FIELDS}
        created = data.get("created")
        if created is not None:
            try:
                fields["created"] = int(created)
            except (TypeError, ValueError):
                pass
        return cls(**fields)


class BookmarkStore:
    """A directory tree full of bookmark YAML files (plaintext and encrypted)."""

    def __init__(self, directory: Path | str = DEFAULT_STORE_DIR):
        self.directory = Path(directory).expanduser()
        self._session: CryptoSession | None = None
        # When False, adding a new *unencrypted* bookmark is rejected. Set from
        # settings.yaml by the CLI / web entry points.
        self.allow_unencrypted: bool = True

    # -- crypto session ------------------------------------------------------

    @property
    def is_unlocked(self) -> bool:
        return self._session is not None

    def unlock(self, password: str) -> int:
        """Engage *password*. Returns how many encrypted bookmarks it reveals.

        The salt of the first matching file becomes the vault the user is in, so
        newly encrypted bookmarks join it. If nothing matches, a fresh salt is
        used for new bookmarks (i.e. starting a new vault).
        """
        session = CryptoSession(password)
        count = 0
        active: bytes | None = None
        for path in self._all_paths():
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
        return count

    def lock(self) -> None:
        self._session = None

    def encrypted_file_count(self) -> int:
        """Total number of encrypted files on disk, regardless of password."""
        return sum(1 for p in self._all_paths() if is_encrypted(self._load_yaml(p)))

    # -- paths ---------------------------------------------------------------

    def _path_for(self, url: str, folder: str = "") -> Path:
        """The plaintext file path for a URL (encrypted bookmarks use UUIDs)."""
        folder = normalize_folder(folder)
        base = self._folder_dir(folder)
        return base / filename_for_url(url)

    def _folder_dir(self, folder: str) -> Path:
        folder = normalize_folder(folder)
        return self.directory.joinpath(*folder.split("/")) if folder else self.directory

    def _folder_of(self, path: Path) -> str:
        rel = path.parent.relative_to(self.directory)
        return "" if rel == Path(".") else rel.as_posix()

    def _all_paths(self) -> Iterator[Path]:
        # settings.yaml lives in the base dir, outside this bookmarks dir, so
        # everything under here is a bookmark.
        if self.directory.exists():
            yield from self.directory.rglob("*.yaml")

    # -- reading -------------------------------------------------------------

    @staticmethod
    def _load_yaml(path: Path):
        try:
            with path.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return None

    def _read(self, path: Path) -> Bookmark:
        data = self._load_yaml(path)
        if data is None:
            raise ValueError(f"could not read {path}")
        if is_encrypted(data):
            if self._session is None:
                raise _LockedBookmark
            payload = self._session.decrypt(data, aad=path.name.encode("utf-8"))
            if payload is None:
                raise _LockedBookmark  # wrong password for this file
            bookmark = Bookmark.from_dict(payload)
            bookmark.encrypted = True
        else:
            bookmark = Bookmark.from_dict(data)
            bookmark.encrypted = False
        bookmark.folder = self._folder_of(path)
        bookmark.path = str(path)
        return bookmark

    def _iter(self) -> Iterator[Bookmark]:
        for path in self._all_paths():
            try:
                yield self._read(path)
            except _LockedBookmark:
                continue  # encrypted and locked (or wrong password): stay hidden
            except (ValueError, yaml.YAMLError, OSError):
                continue  # not a valid bookmark file

    # -- queries -------------------------------------------------------------

    def exists(self, url: str, folder: str = "") -> bool:
        return self.get(url, folder) is not None

    def get(self, url: str, folder: str = "") -> Bookmark | None:
        folder = normalize_folder(folder)
        plain = self._path_for(url, folder)
        if plain.exists():
            try:
                bookmark = self._read(plain)
                if not bookmark.encrypted:
                    return bookmark
            except (_LockedBookmark, ValueError, yaml.YAMLError, OSError):
                pass
        # Encrypted (UUID-named) bookmarks: scan the folder for a matching URL.
        if self._session is not None:
            path = self._find_encrypted_path(url, folder)
            if path is not None:
                return self._read(path)
        return None

    def _find_encrypted_path(self, url: str, folder: str) -> Path | None:
        """Path of the encrypted bookmark for *url* in *folder* (needs a session)."""
        if self._session is None:
            return None
        base = self._folder_dir(normalize_folder(folder))
        if not base.exists():
            return None
        for path in base.glob("*.yaml"):
            data = self._load_yaml(path)
            if not is_encrypted(data):
                continue
            payload = self._session.decrypt(data, aad=path.name.encode("utf-8"))
            if payload is not None and payload.get("url") == url:
                return path
        return None

    def list(self) -> list[Bookmark]:
        """All *visible* bookmarks, newest first (by ``created``; unknown last)."""
        return sorted(
            self._iter(),
            key=lambda b: b.created or 0,
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

    # -- folder management ---------------------------------------------------

    def create_folder(self, folder: str) -> str:
        folder = normalize_folder(folder)
        if folder:
            self._folder_dir(folder).mkdir(parents=True, exist_ok=True)
        return folder

    def _relocate_folder(self, src: str, dst: str) -> str:
        """Move the whole folder subtree from *src* to *dst* (keeps file names).

        Since file names are preserved, encrypted bookmarks (filename-bound AAD)
        keep working and nothing needs re-encrypting.
        """
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
        src_dir = self._folder_dir(src)
        if not src_dir.exists():
            raise FileNotFoundError(f"no such folder: {src!r}")
        dst_dir = self._folder_dir(dst)
        if dst_dir.exists():
            raise ValueError(f"a folder {dst!r} already exists")
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src_dir, dst_dir)
        return dst

    def rename_folder(self, folder: str, new_name: str) -> str:
        """Rename a folder's own name, keeping it under the same parent."""
        folder = normalize_folder(folder)
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("a folder name is required")
        parent = "/".join(folder.split("/")[:-1])
        target = f"{parent}/{new_name}" if parent else new_name
        return self._relocate_folder(folder, target)

    def move_folder(self, folder: str, new_parent: str) -> str:
        """Move a folder (keeping its own name) under *new_parent* ("" = root)."""
        folder = normalize_folder(folder)
        leaf = folder.split("/")[-1]
        new_parent = normalize_folder(new_parent)
        target = f"{new_parent}/{leaf}" if new_parent else leaf
        return self._relocate_folder(folder, target)

    def delete_folder(self, folder: str) -> str:
        """Delete a folder, moving any bookmarks inside it to ``orphaned/``.

        Bookmarks keep their sub-path relative to the deleted folder, so nothing
        collides and nothing is lost. Returns the folder they were moved to.
        """
        folder = normalize_folder(folder)
        if not folder:
            raise ValueError("cannot delete the root folder")
        if folder == ORPHANED_FOLDER or folder.startswith(ORPHANED_FOLDER + "/"):
            raise ValueError(f"cannot delete the {ORPHANED_FOLDER!r} folder")
        src_dir = self._folder_dir(folder)
        if not src_dir.exists():
            raise FileNotFoundError(f"no such folder: {folder!r}")
        orphan_dir = self._folder_dir(ORPHANED_FOLDER)
        for path in sorted(src_dir.rglob("*.yaml")):
            dest = orphan_dir / path.relative_to(src_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
        shutil.rmtree(src_dir)
        return ORPHANED_FOLDER

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

    def _write_plaintext(self, bookmark: Bookmark, path: Path) -> None:
        self._atomic_write(path, self._dump(bookmark.payload()))
        bookmark.path = str(path)

    def _write_encrypted(self, bookmark: Bookmark, path: Path | None = None) -> None:
        session = self._require_session()
        if path is None:
            base = self._folder_dir(bookmark.folder)
            base.mkdir(parents=True, exist_ok=True)
            path = base / new_filename()
            while path.exists():
                path = base / new_filename()
        data = session.encrypt(
            bookmark.payload(), aad=path.name.encode("utf-8"), salt=session.active_salt
        )
        self._atomic_write(path, self._dump(data))
        bookmark.path = str(path)

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
        """Create a new bookmark. Raises if one already exists at that folder.

        ``created`` defaults to the current time (unix seconds); pass a value to
        preserve an original creation time (e.g. when importing).
        """
        folder = normalize_folder(folder)
        bookmark = Bookmark(
            url=url,
            title=title,
            description=description,
            tags=list(tags or []),
            folder=folder,
            created=created if created is not None else _now_unix(),
            encrypted=encrypt,
        )
        if encrypt:
            self._require_session()
            if self._find_encrypted_path(url, folder) is not None:
                where = f"{folder}/" if folder else "the root folder"
                raise FileExistsError(f"a bookmark already exists for {url!r} in {where}")
            self._write_encrypted(bookmark)
        else:
            if not self.allow_unencrypted:
                raise EncryptionRequired(
                    "unencrypted bookmarks are not allowed (allow_unencrypted is false)"
                )
            path = self._path_for(url, folder)
            if path.exists():
                where = f"{folder}/" if folder else "the root folder"
                raise FileExistsError(f"a bookmark already exists for {url!r} in {where}")
            self._write_plaintext(bookmark, path)
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
        bookmark = self.get(url, normalize_folder(folder))
        if bookmark is None:
            raise FileNotFoundError(f"no bookmark found for {url!r} in folder {folder!r}")
        if title is not None:
            bookmark.title = title
        if description is not None:
            bookmark.description = description
        if tags is not None:
            bookmark.tags = list(tags)
        if bookmark.encrypted:
            self._write_encrypted(bookmark, path=Path(bookmark.path))
        else:
            self._write_plaintext(bookmark, Path(bookmark.path))
        return bookmark

    def save(self, bookmark: Bookmark, *, encrypt: bool = False) -> Bookmark:
        """Create or overwrite a bookmark (upsert).

        An existing bookmark keeps its kind (encrypted or plaintext); a brand-new
        one is encrypted iff *encrypt* is true.
        """
        bookmark.folder = normalize_folder(bookmark.folder)
        existing = self.get(bookmark.url, bookmark.folder)
        if bookmark.created is None:
            bookmark.created = existing.created if existing else _now_unix()
        if existing is not None:
            use_encrypt = existing.encrypted
            existing_path: Path | None = Path(existing.path)
        else:
            use_encrypt = encrypt
            existing_path = None
        if existing is None and not use_encrypt and not self.allow_unencrypted:
            raise EncryptionRequired(
                "unencrypted bookmarks are not allowed (allow_unencrypted is false)"
            )
        bookmark.encrypted = use_encrypt
        if use_encrypt:
            self._require_session()
            self._write_encrypted(bookmark, path=existing_path)
        else:
            self._write_plaintext(
                bookmark, existing_path or self._path_for(bookmark.url, bookmark.folder)
            )
        return bookmark

    def move(self, url: str, src_folder: str, dst_folder: str) -> Bookmark:
        """Move a bookmark from one folder to another, keeping its content."""
        src_folder = normalize_folder(src_folder)
        dst_folder = normalize_folder(dst_folder)
        bookmark = self.get(url, src_folder)
        if bookmark is None:
            raise FileNotFoundError(f"no bookmark found for {url!r} in folder {src_folder!r}")
        src_path = Path(bookmark.path)
        bookmark.folder = dst_folder
        if bookmark.encrypted:
            # Keep the same UUID file name so the filename-as-AAD stays valid.
            dst_dir = self._folder_dir(dst_folder)
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_path = dst_dir / src_path.name
            self._write_encrypted(bookmark, path=dst_path)
        else:
            dst_path = self._path_for(url, dst_folder)
            self._write_plaintext(bookmark, dst_path)
        if src_path.resolve() != Path(bookmark.path).resolve():
            src_path.unlink()
        return bookmark

    def remove(self, url: str, folder: str = "") -> bool:
        """Delete a bookmark (plaintext or encrypted). True if a file was removed."""
        folder = normalize_folder(folder)
        plain = self._path_for(url, folder)
        if plain.exists():
            plain.unlink()
            return True
        if self._session is not None:
            enc = self._find_encrypted_path(url, folder)
            if enc is not None:
                enc.unlink()
                return True
        return False
