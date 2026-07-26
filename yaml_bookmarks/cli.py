# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Command-line interface for yaml-bookmarks."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .settings import ensure_settings_file
from .storage import (
    BOOKMARKS_SUBDIR,
    DEFAULT_BASE_DIR,
    BookmarkStore,
    EncryptionRequired,
    VaultLocked,
    normalize_folder,
)


def _split_tags(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [t.strip() for t in value.split(",") if t.strip()]


def _print_bookmark(b, verbose: bool = False) -> None:
    prefix = f"[{b.folder}] " if b.folder else ""
    lock = " 🔒" if b.encrypted else ""
    if b.title:
        print(f"{prefix}{b.title}{lock}\n  {b.url}")
    else:
        print(f"{prefix}{b.url}{lock}")
    if verbose:
        if b.description:
            print(f"  {b.description}")
        if b.tags:
            print(f"  tags: {', '.join(b.tags)}")
        print(f"  folder: {b.folder or '(root)'}")
        if b.created:
            print(f"  created: {b.created}")


def cmd_add(store: BookmarkStore, args) -> int:
    if args.encrypt and not store.is_unlocked:
        # Need a password to encrypt; use --password or prompt for one.
        store.unlock(args.password or getpass.getpass("Encryption password: "))
    try:
        b = store.add(
            args.url,
            folder=args.folder or "",
            encrypt=args.encrypt,
            title=args.title or "",
            description=args.description or "",
            tags=_split_tags(args.tags) or [],
            created=args.created,
        )
    except (FileExistsError, ValueError, VaultLocked, EncryptionRequired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, FileExistsError):
            print("hint: use 'update' to change an existing bookmark.", file=sys.stderr)
        elif isinstance(exc, EncryptionRequired):
            print("hint: add it encrypted with -e (and -p PASSWORD).", file=sys.stderr)
        return 1
    lock = " 🔒" if b.encrypted else ""
    print(f"added: {b.url}{lock}" + (f"  → {b.folder}" if b.folder else ""))
    return 0


def cmd_update(store: BookmarkStore, args) -> int:
    try:
        b = store.update(
            args.url,
            folder=args.folder or "",
            title=args.title,
            description=args.description,
            tags=_split_tags(args.tags),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, FileNotFoundError):
            print("hint: use 'add' to create it first.", file=sys.stderr)
        return 1
    print(f"updated: {b.url}")
    return 0


def cmd_move(store: BookmarkStore, args) -> int:
    try:
        b = store.move(args.url, args.from_folder or "", args.to)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"moved: {b.url}  → {b.folder or '(root)'}")
    return 0


def cmd_remove(store: BookmarkStore, args) -> int:
    try:
        removed = store.remove(args.url, folder=args.folder or "")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(f"removed: {args.url}")
        return 0
    print(f"error: no bookmark found for {args.url!r}", file=sys.stderr)
    return 1


def cmd_list(store: BookmarkStore, args) -> int:
    bookmarks = store.list()
    if args.folder is not None:
        wanted = normalize_folder(args.folder)
        bookmarks = [b for b in bookmarks if b.folder == wanted]
    if args.tag:
        tags = {t.strip() for t in args.tag}
        bookmarks = [b for b in bookmarks if tags & set(b.tags)]
    if not bookmarks:
        print("no bookmarks found.")
        return 0
    for b in bookmarks:
        _print_bookmark(b, verbose=args.verbose)
    return 0


def cmd_folders(store: BookmarkStore, args) -> int:
    folders = store.folders()
    if not folders:
        print("no folders yet.")
        return 0
    counts: dict[str, int] = {}
    for b in store.list():
        counts[b.folder] = counts.get(b.folder, 0) + 1
    for f in folders:
        depth = f.count("/")
        name = f.split("/")[-1]
        print(f"{'  ' * depth}{name}  ({counts.get(f, 0)})")
    return 0


def cmd_mkdir(store: BookmarkStore, args) -> int:
    try:
        folder = store.create_folder(args.folder)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created folder: {folder or '(root)'}")
    return 0


def cmd_import(store: BookmarkStore, args) -> int:
    from .importers import import_raindrop

    try:
        text = open(args.file, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.encrypt and not store.is_unlocked:
        store.unlock(args.password or getpass.getpass("Encryption password: "))
    encrypt = store.is_unlocked
    if not encrypt and not store.allow_unencrypted:
        print("error: encryption is required; unlock with -p PASSWORD", file=sys.stderr)
        return 1
    summary = import_raindrop(store, text, encrypt=encrypt)
    enc = " (encrypted)" if summary["encrypted"] else ""
    failed = f", {summary['failed']} failed" if summary["failed"] else ""
    print(f"imported {summary['added']} of {summary['total']} bookmarks{enc}{failed}")
    return 0


def cmd_web(store: BookmarkStore, args) -> int:
    from .web import run_server

    # Explicit --port wins; otherwise use settings.yaml (default 22222).
    port = args.port if args.port is not None else args.settings.port
    run_server(store, host=args.host, port=port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yaml-bookmarks",
        description="A personal, YAML-backed bookmark manager.",
    )
    parser.add_argument(
        "--dir",
        help="base directory (default: $HOME/.yaml-bookmarks or $YAML_BOOKMARKS_DIR); "
        "bookmarks live in <dir>/bookmarks and settings in <dir>/settings.yaml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared --password option: unlocks encrypted bookmarks for the command.
    pw = argparse.ArgumentParser(add_help=False)
    pw.add_argument(
        "-p",
        "--password",
        nargs="?",
        const="",
        help="unlock encrypted bookmarks (prompts if given with no value)",
    )

    p_add = sub.add_parser("add", parents=[pw], help="add a new bookmark")
    p_add.add_argument("url")
    p_add.add_argument("-f", "--folder", help="folder to file it under, e.g. work/ideas")
    p_add.add_argument("-t", "--title")
    p_add.add_argument("-d", "--description")
    p_add.add_argument("--tags", help="comma-separated tags")
    p_add.add_argument(
        "--created", type=int, help="creation time as a unix timestamp (default: now)"
    )
    p_add.add_argument(
        "-e", "--encrypt", action="store_true", help="store this bookmark encrypted"
    )
    p_add.set_defaults(func=cmd_add)

    p_upd = sub.add_parser("update", parents=[pw], help="update an existing bookmark")
    p_upd.add_argument("url")
    p_upd.add_argument("-f", "--folder", help="folder the bookmark is in")
    p_upd.add_argument("-t", "--title")
    p_upd.add_argument("-d", "--description")
    p_upd.add_argument("--tags", help="comma-separated tags (replaces existing)")
    p_upd.set_defaults(func=cmd_update)

    p_mv = sub.add_parser(
        "move", parents=[pw], aliases=["mv"], help="move a bookmark to another folder"
    )
    p_mv.add_argument("url")
    p_mv.add_argument("--to", required=True, help="destination folder (use '' for root)")
    p_mv.add_argument("--from", dest="from_folder", default="", help="source folder")
    p_mv.set_defaults(func=cmd_move)

    p_rm = sub.add_parser("remove", parents=[pw], aliases=["rm"], help="remove a bookmark")
    p_rm.add_argument("url")
    p_rm.add_argument("-f", "--folder", help="folder the bookmark is in")
    p_rm.set_defaults(func=cmd_remove)

    p_ls = sub.add_parser("list", parents=[pw], aliases=["ls"], help="list bookmarks")
    p_ls.add_argument("-v", "--verbose", action="store_true")
    p_ls.add_argument("-f", "--folder", help="only show bookmarks in this folder")
    p_ls.add_argument(
        "--tag", action="append", help="only show bookmarks with this tag (repeatable)"
    )
    p_ls.set_defaults(func=cmd_list)

    p_fold = sub.add_parser("folders", parents=[pw], help="list folders as a tree")
    p_fold.set_defaults(func=cmd_folders)

    p_mkdir = sub.add_parser("mkdir", help="create an (empty) folder")
    p_mkdir.add_argument("folder")
    p_mkdir.set_defaults(func=cmd_mkdir)

    p_imp = sub.add_parser(
        "import", parents=[pw], help="import bookmarks from a Raindrop.io CSV export"
    )
    p_imp.add_argument("file", help="path to the CSV file")
    p_imp.add_argument("--format", default="raindrop", choices=["raindrop"])
    p_imp.add_argument(
        "-e", "--encrypt", action="store_true", help="import the bookmarks encrypted"
    )
    p_imp.set_defaults(func=cmd_import)

    p_web = sub.add_parser("web", help="launch the localhost-only web UI")
    p_web.add_argument("--host", default="127.0.0.1", help="default: 127.0.0.1")
    p_web.add_argument(
        "--port", type=int, default=None, help="override the port from settings.yaml (22222)"
    )
    p_web.add_argument(
        "--no-browser", action="store_true", help="do not open a browser automatically"
    )
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base = Path(args.dir).expanduser() if args.dir else DEFAULT_BASE_DIR
    store = BookmarkStore(base / BOOKMARKS_SUBDIR)
    # Global settings live in <base>/settings.yaml (created on first run);
    # bookmarks live in <base>/bookmarks/.
    args.settings = ensure_settings_file(base)
    store.allow_unencrypted = args.settings.allow_unencrypted
    # A bare "--password" (const "") prompts; "--password PW" unlocks directly.
    password = getattr(args, "password", None)
    if password is not None:
        password = password or getpass.getpass("Password: ")
        if password:
            store.unlock(password)
    return args.func(store, args)


if __name__ == "__main__":
    raise SystemExit(main())
