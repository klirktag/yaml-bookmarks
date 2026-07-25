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
import sys

from .storage import BookmarkStore, normalize_folder


def _split_tags(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [t.strip() for t in value.split(",") if t.strip()]


def _print_bookmark(b, verbose: bool = False) -> None:
    prefix = f"[{b.folder}] " if b.folder else ""
    if b.title:
        print(f"{prefix}{b.title}\n  {b.url}")
    else:
        print(f"{prefix}{b.url}")
    if verbose:
        if b.description:
            print(f"  {b.description}")
        if b.tags:
            print(f"  tags: {', '.join(b.tags)}")
        print(f"  folder: {b.folder or '(root)'}")
        if b.updated_at:
            print(f"  updated: {b.updated_at}")


def cmd_add(store: BookmarkStore, args) -> int:
    try:
        b = store.add(
            args.url,
            folder=args.folder or "",
            title=args.title or "",
            description=args.description or "",
            tags=_split_tags(args.tags) or [],
        )
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, FileExistsError):
            print("hint: use 'update' to change an existing bookmark.", file=sys.stderr)
        return 1
    print(f"added: {b.url}" + (f"  → {b.folder}" if b.folder else ""))
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


def cmd_web(store: BookmarkStore, args) -> int:
    from .web import run_server

    run_server(store, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yaml-bookmarks",
        description="A personal, YAML-backed bookmark manager.",
    )
    parser.add_argument(
        "--dir",
        help="bookmark directory (default: $HOME/.yaml-bookmarks or $YAML_BOOKMARKS_DIR)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a new bookmark")
    p_add.add_argument("url")
    p_add.add_argument("-f", "--folder", help="folder to file it under, e.g. work/ideas")
    p_add.add_argument("-t", "--title")
    p_add.add_argument("-d", "--description")
    p_add.add_argument("--tags", help="comma-separated tags")
    p_add.set_defaults(func=cmd_add)

    p_upd = sub.add_parser("update", help="update an existing bookmark")
    p_upd.add_argument("url")
    p_upd.add_argument("-f", "--folder", help="folder the bookmark is in")
    p_upd.add_argument("-t", "--title")
    p_upd.add_argument("-d", "--description")
    p_upd.add_argument("--tags", help="comma-separated tags (replaces existing)")
    p_upd.set_defaults(func=cmd_update)

    p_mv = sub.add_parser("move", aliases=["mv"], help="move a bookmark to another folder")
    p_mv.add_argument("url")
    p_mv.add_argument("--to", required=True, help="destination folder (use '' for root)")
    p_mv.add_argument("--from", dest="from_folder", default="", help="source folder")
    p_mv.set_defaults(func=cmd_move)

    p_rm = sub.add_parser("remove", aliases=["rm"], help="remove a bookmark")
    p_rm.add_argument("url")
    p_rm.add_argument("-f", "--folder", help="folder the bookmark is in")
    p_rm.set_defaults(func=cmd_remove)

    p_ls = sub.add_parser("list", aliases=["ls"], help="list bookmarks")
    p_ls.add_argument("-v", "--verbose", action="store_true")
    p_ls.add_argument("-f", "--folder", help="only show bookmarks in this folder")
    p_ls.add_argument(
        "--tag", action="append", help="only show bookmarks with this tag (repeatable)"
    )
    p_ls.set_defaults(func=cmd_list)

    p_fold = sub.add_parser("folders", help="list folders as a tree")
    p_fold.set_defaults(func=cmd_folders)

    p_mkdir = sub.add_parser("mkdir", help="create an (empty) folder")
    p_mkdir.add_argument("folder")
    p_mkdir.set_defaults(func=cmd_mkdir)

    p_web = sub.add_parser("web", help="launch the localhost-only web UI")
    p_web.add_argument("--host", default="127.0.0.1", help="default: 127.0.0.1")
    p_web.add_argument("--port", type=int, default=22222, help="default: 22222")
    p_web.add_argument(
        "--no-browser", action="store_true", help="do not open a browser automatically"
    )
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = BookmarkStore(args.dir) if args.dir else BookmarkStore()
    return args.func(store, args)


if __name__ == "__main__":
    raise SystemExit(main())
