# CLAUDE.md

Guidance for working in this repository.

## What this is

`yaml-bookmarks` is a personal bookmark manager. Every bookmark is a single YAML
file on disk under `$HOME/.yaml-bookmarks/`, and bookmarks can be organised into
folders (collections) that are **real directories** under that path. It ships
two front-ends over the same storage layer:

- a **CLI** (`yaml-bookmarks`) for add / update / remove / list / move / folders
- a **web UI** (`yaml-bookmarks web`) — a localhost-only, installable PWA

### Design principles (do not break these)

- **Personal & offline.** It is meant to be run by one person on their own
  machine. There is **no authentication** and there must not be — the web server
  binds to `127.0.0.1` only and is never intended to be reachable over a network.
- **The filesystem is the single source of truth.** A bookmark's folder is
  *where its file lives*, not a field. Folder membership is derived from the path
  and is deliberately **not** written into the YAML. Anything that changes a
  bookmark's folder must move the file.
- **No binary assets in the repo.** The PWA icons are generated at runtime (see
  `web.py`), and the HTML/CSS/JS is inlined in `web.py`. Keep it that way so the
  package stays a pure-Python, easy-to-audit tree.
- **Keep it simple.** Only two runtime dependencies (PyYAML, Flask). Prefer the
  standard library over adding packages.

## Layout

```
yaml_bookmarks/
├── __init__.py      public exports + version
├── escaping.py      URL  <->  filesystem-safe file name
├── storage.py       Bookmark dataclass + BookmarkStore (all disk I/O) + folder rules
├── cli.py           argparse CLI, thin wrapper over BookmarkStore
└── web.py           Flask app: JSON API, metadata fetch, PWA assets, inlined UI
tests/
├── test_escaping.py
└── test_storage.py
pyproject.toml       console_script `yaml-bookmarks = yaml_bookmarks.cli:main`
```

Data flow: **CLI / web → `BookmarkStore` → YAML files**. Both front-ends are thin;
all persistence logic lives in `storage.py`. If you add a capability, add it to
`BookmarkStore` first, then surface it in the CLI and/or web layer.

## URL → file name (`escaping.py`)

A bookmark's file name is a deterministic, filesystem-safe escaping of its URL,
so the same URL always maps to the same file on Windows, macOS and Linux.

- `escape_url(url)` keeps lowercase letters, digits, `-` and `.` as-is; every
  other byte (including **uppercase letters**) becomes `_XX` (uppercase hex of
  each UTF-8 byte). Uppercase is escaped on purpose so `Foo` and `foo` don't
  collide on case-insensitive file systems. The escape marker `_` is itself
  escaped (`_5F`), so the transform is fully reversible via `unescape_url`.
- `filename_for_url(url)` returns the `*.yaml` name. URLs whose escaping exceeds
  `MAX_NAME_LEN` (200) are truncated with a short SHA-256 suffix — still
  deterministic, but no longer reversible (the URL is also stored in the file, so
  nothing is lost).

Example: `https://example.com/Path` → `https_3A_2F_2Fexample.com_2F_50ath.yaml`
(note the capital `P` → `_50`).

## Storage model (`storage.py`)

`Bookmark` is a dataclass: `url`, `title`, `description`, `tags`, `folder`,
`created_at`, `updated_at` (ISO-8601 UTC, seconds precision).

- `folder` is a runtime attribute **derived from the file's location**;
  `to_yaml_dict()` drops it before writing, `to_dict()` keeps it (for the JSON
  API), and `_read()` sets it from the path so location always wins.
- The store directory is `$HOME/.yaml-bookmarks`, overridable via the
  `YAML_BOOKMARKS_DIR` env var or `BookmarkStore(directory)`.

`BookmarkStore` methods:

| Method | Notes |
|---|---|
| `add(url, folder=, title=, description=, tags=)` | raises `FileExistsError` if it already exists at that folder |
| `update(url, folder=, ...)` | only the fields you pass change; raises `FileNotFoundError` if missing |
| `save(bookmark)` | upsert at `bookmark.folder`; preserves `created_at` |
| `move(url, src_folder, dst_folder)` | relocates the file, preserving content & `created_at` |
| `remove(url, folder=)` | returns `True` if a file was deleted |
| `get(url, folder=)` / `exists(...)` | single lookup at an exact folder |
| `list()` | all bookmarks, recursive, most-recently-updated first |
| `folders()` | every folder path (incl. empty ones), from directory tree |
| `create_folder(folder)` | `mkdir -p` an empty collection |

Because folder is part of the on-disk location, the identity of a bookmark is
**(folder, url)** — the same URL may exist in more than one folder, and `move`
needs both the source and destination folder.

Writes are atomic (`*.yaml.tmp` + `os.replace`). `list()`/`_iter()` skip files
that don't parse as bookmarks rather than crashing.

### Folder validation — `normalize_folder(folder)`

All folder input funnels through this. It returns a clean relative POSIX path
(`""` = root) and **raises `ValueError`** for anything unsafe:

- `..` path traversal
- illegal characters `< > : " | ? * \` and control chars
- names ending in a space or dot (Windows-hostile)
- reserved device names (`con`, `prn`, `aux`, `nul`, `com1..9`, `lpt1..9`)

It also normalises separators/whitespace (`/work//ideas/` → `work/ideas`). Never
build a path from raw user folder input without going through it.

## CLI (`cli.py`)

`argparse` with subcommands; each `cmd_*` calls into `BookmarkStore` and returns
an exit code. Global `--dir` overrides the store directory.

```
yaml-bookmarks add URL [-f FOLDER] [-t TITLE] [-d DESC] [--tags a,b]
yaml-bookmarks update URL [-f FOLDER] [-t ...] [-d ...] [--tags ...]
yaml-bookmarks move URL --to DEST [--from SRC]        # alias: mv  (default SRC = root)
yaml-bookmarks remove URL [-f FOLDER]                 # alias: rm
yaml-bookmarks list [-v] [-f FOLDER] [--tag T ...]    # alias: ls
yaml-bookmarks folders                                # folder tree with counts
yaml-bookmarks mkdir FOLDER                           # create an empty folder
yaml-bookmarks web [--host H] [--port P] [--no-browser]
```

Tags are comma-separated on input. `--tags` on `update` replaces the whole list.

## Web app (`web.py`)

Flask app created by `create_app(store)`; `run_server(...)` binds it (default
`127.0.0.1:22222`, opens a browser). Everything the browser needs is served by
this one module — no `static/` or `templates/` dirs.

### Security

- Binds to `127.0.0.1` only.
- A `before_request` guard rejects any request whose `Host` header host isn't in
  `{localhost, 127.0.0.1, [::1], ::1}` → `403`. This blocks DNS-rebinding. Keep
  this guard if you touch routing.

### Routes

- `GET /` — the inlined single-page UI (`_INDEX_HTML`).
- `GET /api/bookmarks` — list as JSON (each item includes `folder`).
- `POST /api/bookmarks` — upsert. Body: `url`, `folder`, `title`, `description`,
  `tags`, and optional `original_folder`. If `original_folder` is present and
  differs from `folder`, the bookmark is **moved** first (so edits that change
  the collection relocate the file and keep the created date), then saved.
  Folder validation errors return `400`.
- `DELETE /api/bookmarks?url=&folder=` — remove one (folder also accepted in body).
- `GET /api/folders` — the folder tree as a flat list of paths.
- `POST /api/folders` — `{folder}` create an empty collection.
- `GET /api/fetch-meta?url=` — **server-side** fetch of a page's `<head>` to pull
  `title` / `description` / `keywords` (the browser can't, due to CORS). Uses
  `urllib` + `html.parser` (`_MetaExtractor`), http(s) only, 10s timeout, reads
  at most 1 MB, ignores non-HTML. Errors return `400`/`502` with a message.
- `GET /manifest.webmanifest`, `GET /sw.js`, `GET /icon-192.png`,
  `GET /icon-512.png`, `GET /icon-512-maskable.png`, `GET /favicon.ico` — PWA.

The page footer (`_footer_html()`) shows the license and, if the
`YAML_BOOKMARKS_SOURCE_URL` env var is set, a courtesy "Source code" link. It's
injected into `_INDEX_HTML` via the `__FOOTER__` placeholder at serve time.

### PWA / generated icons

- `_MANIFEST` + a service worker string (`_SERVICE_WORKER`). The SW caches the
  app shell cache-first (so it launches offline) and network-first for `/api/`.
  **When you change the inlined HTML/CSS/JS, bump the `CACHE = 'yaml-bookmarks-vN'`
  constant** or a cached client won't see the update.
- Icons are drawn in pure Python: `_png()` writes a PNG (zlib + CRC, no Pillow)
  and `_icon_png(size, maskable=)` renders a flat indigo square with a white
  bookmark-ribbon glyph. Results are `lru_cache`d.

### UI structure (inside `_INDEX_HTML`)

Three panes on wide screens (CSS grid `240px | 1fr | 340px`): a **collections
sidebar** (left) with a collapsible folder tree, per-folder counts, "All
bookmarks" / "Unsorted" and a "+" new-collection button; the **bookmark list**
(centre); the **add/edit form** (right). Below 1000px it collapses to one column
ordered form → collections → list (so the form is on top). Theme-aware
(light/dark via `prefers-color-scheme`).

Front-end state (vanilla JS, no framework): `all` (bookmarks), `folders`,
`filter` (`'*ALL*'` sentinel / `''` root / a folder path), `collapsed` set, and
`shown` (the currently rendered slice — list actions reference it by index).
`load()` fetches bookmarks + folders in parallel and re-renders. Selecting a
collection filters the list and pre-fills the form's Collection field so new adds
land there. Editing sends `original_folder` so a changed collection moves the
file.

## Data format

```yaml
url: https://example.com
title: Example
description: A description
tags:
- web
- reference
created_at: '2026-07-25T20:41:28+00:00'
updated_at: '2026-07-25T20:41:28+00:00'
```

There is intentionally **no `folder:` key** — the folder is the directory the
file sits in.

## Running, installing, testing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .           # installs the `yaml-bookmarks` command + PyYAML + Flask
yaml-bookmarks web           # http://127.0.0.1:22222

pip install pytest && pytest    # unit tests for escaping + storage
```

## License

**LGPL-3.0-or-later** — `COPYING.LESSER` (the LGPL) on top of `COPYING`
(GPLv3). Proprietary software may import/call this package; only modifications
to *this* code, when distributed, must be released under the LGPL. Every source
file carries an SPDX header and `pyproject.toml` declares the SPDX id and
classifier. When adding new source files, include the same
`SPDX-License-Identifier: LGPL-3.0-or-later` header.

## Conventions when extending

- Put new persistence logic in `BookmarkStore`; keep CLI and web thin.
- Route all folder input through `normalize_folder`; never trust raw paths.
- Don't add a `folder` field to the YAML — keep the filesystem authoritative.
- Don't add binary assets; generate them or inline them.
- Don't add network exposure or auth; preserve the localhost-only guard.
- Bump the service-worker cache version when the inlined UI changes.
- Add/adjust tests in `tests/` for storage and escaping behaviour.
