# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
#
# yaml-bookmarks is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See the COPYING.LESSER file for details.
"""Localhost-only web UI for yaml-bookmarks, installable as a PWA.

The server binds to 127.0.0.1 by default and rejects requests whose ``Host``
header is not local, which guards against DNS-rebinding.  No authentication is
needed: the app is meant to be run by one user on their own machine and is not
reachable over the network.
"""

from __future__ import annotations

import functools
import html
import os
import struct
import threading
import urllib.error
import urllib.request
import webbrowser
import zlib
from html.parser import HTMLParser

from flask import Flask, Response, jsonify, request

from .storage import Bookmark, BookmarkStore, normalize_folder

APP_NAME = "YAML Bookmarks"

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


# --------------------------------------------------------------------------- #
# Icons (generated so we ship no binary assets)
# --------------------------------------------------------------------------- #

_BG = (79, 70, 229)     # indigo
_FG = (255, 255, 255)   # white bookmark ribbon


def _png(size: int, rgba: bytes) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 for each scanline
        raw += rgba[y * stride : (y + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


@functools.lru_cache(maxsize=None)
def _icon_png(size: int, maskable: bool = False) -> bytes:
    """A flat indigo square with a white bookmark ribbon."""
    # Shrink the ribbon for maskable icons so it survives platform cropping.
    pad = 0.30 if maskable else 0.22
    left, right = pad, 1 - pad
    top = pad
    notch = 1 - pad - 0.12          # where the bottom V begins
    bottom = 1 - pad
    center = (left + right) / 2
    half = (right - left) / 2

    buf = bytearray(size * size * 4)
    for y in range(size):
        fy = y / size
        for x in range(size):
            fx = x / size
            fg = False
            if left <= fx <= right and top <= fy <= bottom:
                # bottom edge dips toward the centre to form the ribbon notch
                edge = notch + (abs(fx - center) / half) * (bottom - notch)
                fg = fy <= edge
            r, g, b = _FG if fg else _BG
            i = (y * size + x) * 4
            buf[i : i + 4] = bytes((r, g, b, 255))
    return _png(size, bytes(buf))


# --------------------------------------------------------------------------- #
# Fetch title / description / keywords from a page (server-side, to dodge CORS)
# --------------------------------------------------------------------------- #

_MAX_FETCH_BYTES = 1_000_000
_FETCH_TIMEOUT = 10


class _StopParsing(Exception):
    """Raised to stop the parser once we've seen </head>."""


class _MetaExtractor(HTMLParser):
    """Pull <title> and relevant <meta> tags out of a page's <head>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.meta: dict[str, str] = {}
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            content = a.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key, content)

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self.title = "".join(self._title_parts).strip()
            self._in_title = False
        elif tag == "head":
            raise _StopParsing


def _extract_meta(html: str) -> dict:
    parser = _MetaExtractor()
    try:
        parser.feed(html)
    except _StopParsing:
        pass
    m = parser.meta
    title = (parser.title or m.get("og:title") or m.get("twitter:title") or "").strip()
    description = (
        m.get("description")
        or m.get("og:description")
        or m.get("twitter:description")
        or ""
    ).strip()
    keywords = [t.strip() for t in m.get("keywords", "").split(",") if t.strip()]
    return {"title": title, "description": description, "tags": keywords}


def _fetch_meta(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "yaml-bookmarks/0.1 (+http://localhost)"}
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        if resp.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
            return {"title": "", "description": "", "tags": []}
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read(_MAX_FETCH_BYTES)
    return _extract_meta(raw.decode(charset, errors="replace"))


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def _footer_html() -> str:
    """Footer notice: license + (optional) link to the app's source.

    Set ``YAML_BOOKMARKS_SOURCE_URL`` to your repository URL to show a courtesy
    "Source code" link (handy since the LGPL requires you to make your modified
    source available to anyone you distribute the software to).
    """
    lic = (
        '<a href="https://www.gnu.org/licenses/lgpl-3.0.html" '
        'target="_blank" rel="noopener">LGPL-3.0-or-later</a>'
    )
    source_url = os.environ.get("YAML_BOOKMARKS_SOURCE_URL", "").strip()
    src = ""
    if source_url:
        src = (
            f' · <a href="{html.escape(source_url, quote=True)}" '
            'target="_blank" rel="noopener">Source code</a>'
        )
    return f"yaml-bookmarks · licensed under {lic}{src}"


def create_app(store: BookmarkStore) -> Flask:
    app = Flask(__name__)

    @app.before_request
    def _only_local():  # guard against DNS-rebinding
        host = (request.host or "").split(":")[0]
        if host not in _ALLOWED_HOSTS:
            return Response("This app is available on localhost only.\n", status=403)

    # -- pages -------------------------------------------------------------- #

    @app.get("/")
    def index() -> Response:
        return Response(_INDEX_HTML.replace("__FOOTER__", _footer_html()), mimetype="text/html")

    @app.get("/manifest.webmanifest")
    def manifest() -> Response:
        return jsonify(_MANIFEST)

    @app.get("/sw.js")
    def service_worker() -> Response:
        # Served from the root so its scope covers the whole app.
        return Response(_SERVICE_WORKER, mimetype="application/javascript")

    @app.get("/icon-<int:size>.png")
    def icon(size: int) -> Response:
        if size not in (192, 512):
            return Response(status=404)
        return Response(_icon_png(size), mimetype="image/png")

    @app.get("/icon-512-maskable.png")
    def icon_maskable() -> Response:
        return Response(_icon_png(512, maskable=True), mimetype="image/png")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(_icon_png(192), mimetype="image/png")

    # -- JSON API ----------------------------------------------------------- #

    @app.get("/api/bookmarks")
    def api_list() -> Response:
        return jsonify([b.to_dict() for b in store.list()])

    @app.post("/api/bookmarks")
    def api_save() -> Response:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        try:
            folder = normalize_folder(data.get("folder"))
            # If editing moved the bookmark to another folder, relocate it first
            # so its file (and created date) follow along.
            original = data.get("original_folder")
            if original is not None:
                original = normalize_folder(original)
                if original != folder and store.exists(url, original):
                    store.move(url, original, folder)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # While a password is engaged, a *new* bookmark is encrypted with it;
        # editing an existing bookmark keeps whatever kind it already is.
        b = store.save(
            Bookmark(
                url=url,
                folder=folder,
                title=(data.get("title") or "").strip(),
                description=(data.get("description") or "").strip(),
                tags=_clean_tags(data.get("tags")),
            ),
            encrypt=store.is_unlocked,
        )
        return jsonify(b.to_dict())

    @app.delete("/api/bookmarks")
    def api_delete() -> Response:
        body = request.get_json(silent=True) or {}
        url = request.args.get("url") or body.get("url")
        folder = request.args.get("folder")
        if folder is None:
            folder = body.get("folder", "")
        if not url:
            return jsonify({"error": "url is required"}), 400
        try:
            removed = store.remove(url, folder=folder or "")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"removed": removed})

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify({"unlocked": store.is_unlocked})

    @app.post("/api/unlock")
    def api_unlock() -> Response:
        data = request.get_json(silent=True) or {}
        password = data.get("password") or ""
        if not password:
            return jsonify({"error": "password is required"}), 400
        count = store.unlock(password)
        return jsonify(
            {
                "unlocked": True,
                "count": count,
                "encrypted_total": store.encrypted_file_count(),
            }
        )

    @app.post("/api/lock")
    def api_lock() -> Response:
        store.lock()
        return jsonify({"unlocked": False})

    @app.get("/api/folders")
    def api_folders() -> Response:
        return jsonify(store.folders())

    @app.post("/api/folders")
    def api_create_folder() -> Response:
        data = request.get_json(silent=True) or {}
        try:
            folder = store.create_folder(data.get("folder") or "")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"folder": folder})

    @app.post("/api/folders/rename")
    def api_folder_rename() -> Response:
        data = request.get_json(silent=True) or {}
        try:
            folder = store.rename_folder(data.get("folder") or "", data.get("name") or "")
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"folder": folder})

    @app.post("/api/folders/move")
    def api_folder_move() -> Response:
        data = request.get_json(silent=True) or {}
        try:
            folder = store.move_folder(data.get("folder") or "", data.get("parent") or "")
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"folder": folder})

    @app.post("/api/folders/delete")
    def api_folder_delete() -> Response:
        data = request.get_json(silent=True) or {}
        try:
            moved_to = store.delete_folder(data.get("folder") or "")
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"moved_to": moved_to})

    @app.get("/api/fetch-meta")
    def api_fetch_meta():
        url = (request.args.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        if not url.lower().startswith(("http://", "https://")):
            return jsonify({"error": "only http(s) URLs are supported"}), 400
        try:
            return jsonify(_fetch_meta(url))
        except urllib.error.HTTPError as exc:
            return jsonify({"error": f"page returned HTTP {exc.code}"}), 502
        except (urllib.error.URLError, ValueError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return jsonify({"error": f"could not fetch page: {reason}"}), 502

    return app


def _clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    if not tags:
        return []
    return [t.strip() for t in tags if str(t).strip()]


def run_server(
    store: BookmarkStore,
    host: str = "127.0.0.1",
    port: int = 22222,
    open_browser: bool = True,
) -> None:
    app = create_app(store)
    url = f"http://{host}:{port}/"
    print(f"{APP_NAME} running at {url}")
    print(f"Bookmarks: {store.directory}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True)


# --------------------------------------------------------------------------- #
# Static assets (kept inline so the package has no template/static files)
# --------------------------------------------------------------------------- #

_MANIFEST = {
    "name": APP_NAME,
    "short_name": "Bookmarks",
    "description": "A personal, YAML-backed bookmark manager.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0f172a",
    "theme_color": "#4f46e5",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {
            "src": "/icon-512-maskable.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ],
}

_SERVICE_WORKER = """
const CACHE = 'yaml-bookmarks-v10';
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) {
    // Always try the network for live data; the app still works offline for GET.
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }
  // App shell: cache-first so it launches offline.
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
""".strip()

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YAML Bookmarks</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#4f46e5">
<link rel="icon" href="/favicon.ico">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Bookmarks">
<style>
  :root {
    --bg: #0f172a; --panel: #1e293b; --panel2: #172033; --text: #e2e8f0;
    --muted: #94a3b8; --accent: #4f46e5; --accent2: #818cf8; --border: #334155;
    --danger: #ef4444; --accent-soft: rgba(99,102,241,.22);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f5f7; --panel: #ffffff; --panel2: #f1f3f5; --text: #1a1a2e;
      --muted: #6b7280; --accent: #4f46e5; --accent2: #4f46e5; --border: #e5e7eb;
      --accent-soft: rgba(79,70,229,.12);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
  }
  header {
    position: sticky; top: 0; background: var(--accent); color: white;
    padding: 12px 20px; display: flex; align-items: center; gap: 12px;
    box-shadow: 0 1px 6px rgba(0,0,0,.18); z-index: 20;
  }
  header h1 { font-size: 1.1rem; margin: 0; font-weight: 700; letter-spacing: .2px; }
  header .sub { color: rgba(255,255,255,.75); font-size: .82rem; margin-left: auto; }
  .lockbtn {
    background: rgba(255,255,255,.16); color: #fff; border: none; border-radius: 9px;
    padding: 6px 11px; font-size: 1.15rem; line-height: 1; cursor: pointer;
  }
  .lockbtn:hover { background: rgba(255,255,255,.30); }
  .lockbtn.engaged { background: #fff; }
  .enc-hint { font-size: .8rem; color: var(--accent2); font-weight: 600; }
  .lock-badge { margin-left: 6px; }
  /* Password modal */
  .modal {
    position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 50;
    display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .modal-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 20px; width: 100%; max-width: 360px; box-shadow: 0 12px 44px rgba(0,0,0,.35);
  }
  .modal-title { font-weight: 700; margin-bottom: 12px; }
  .pw-row { display: flex; gap: 8px; }
  .pw-row input { flex: 1; min-width: 0; }
  /* Mask a plain text input (so the browser never treats it as a saveable password). */
  .masked { -webkit-text-security: disc; text-security: disc; }
  .eye {
    flex: 0 0 auto; background: var(--panel2); color: var(--text);
    border: 1px solid var(--border); font-size: 1.05rem; padding: 8px 11px;
  }
  .eye:hover { background: var(--border); }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 20px; }
  .app { display: grid; gap: 20px; }
  @media (min-width: 1000px) {
    /* Three panes: collections sidebar | list | compose form */
    .app { grid-template-columns: 240px minmax(0,1fr) 340px; align-items: start; }
    .sidebar { grid-column: 1; grid-row: 1; position: sticky; top: 76px;
               max-height: calc(100vh - 96px); overflow: auto; }
    .main { grid-column: 2; grid-row: 1; }
    .compose { grid-column: 3; grid-row: 1; position: sticky; top: 76px; }
  }

  /* Sidebar / collections */
  .sidebar {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 10px;
  }
  .side-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 10px 10px; font-size: .76rem; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted); font-weight: 700;
  }
  .mini { background: transparent; color: var(--muted); border: 1px solid var(--border);
    border-radius: 8px; padding: 1px 9px; font-size: 1rem; line-height: 1.3; }
  .mini:hover { color: var(--text); background: var(--panel2); }
  .coll {
    display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 9px;
    cursor: pointer; color: var(--text); user-select: none; font-size: .95rem;
  }
  .coll:hover { background: var(--panel2); }
  .coll.active { background: var(--accent-soft); color: var(--accent2); font-weight: 600; }
  .coll .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .coll .cnt { margin-left: auto; font-size: .76rem; color: var(--muted);
    background: var(--panel2); border-radius: 999px; padding: 1px 8px; min-width: 22px; text-align: center; }
  .coll.active .cnt { background: var(--accent); color: #fff; }
  .chev, .chev-spacer { width: 14px; display: inline-flex; justify-content: center;
    color: var(--muted); font-size: .7rem; }
  .chev { cursor: pointer; }
  .sep { height: 1px; background: var(--border); margin: 8px 4px; }

  /* Compose form */
  form.add {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 16px; display: grid; gap: 10px;
  }
  .form-title { font-weight: 700; font-size: .95rem; }
  .folder-edit {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 16px; display: grid; gap: 10px; margin-top: 16px;
  }
  .folder-edit #feName { word-break: break-all; }
  .fe-row { display: flex; gap: 8px; }
  .fe-row input, .fe-row select { flex: 1; min-width: 0; }
  .fe-row button { flex: 0 0 auto; }
  .fe-delete {
    background: transparent; color: var(--danger); border: 1px solid var(--border);
    width: 100%; margin-top: 2px;
  }
  .fe-delete:hover { background: var(--danger); color: #fff; }
  input, textarea, select {
    width: 100%; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
    background: var(--panel2); color: var(--text); font: inherit;
  }
  textarea { resize: vertical; min-height: 44px; }
  label.fld { display: grid; gap: 4px; font-size: .78rem; color: var(--muted); font-weight: 600; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 130px; }
  .url-row { display: flex; gap: 8px; }
  .url-row input { flex: 1; min-width: 0; }
  #fetchBtn { flex: 0 0 auto; white-space: nowrap; background: var(--panel2);
    color: var(--text); border: 1px solid var(--border); }
  #fetchBtn:hover:not(:disabled) { background: var(--border); }
  #fetchBtn:disabled { opacity: .6; cursor: default; }
  button {
    font: inherit; font-weight: 600; border: none; border-radius: 9px; cursor: pointer;
    padding: 10px 16px; background: var(--accent); color: white;
  }
  button:hover { background: var(--accent2); }
  button.ghost { background: transparent; color: var(--muted); padding: 6px 10px; }
  button.ghost:hover { color: var(--text); background: var(--panel2); }
  button.danger { color: var(--danger); }

  /* Main list */
  .main { min-width: 0; }
  .search { margin-bottom: 12px; }
  .crumb { display: flex; align-items: baseline; gap: 10px; }
  .crumb h2 { font-size: 1.25rem; margin: 0; font-weight: 700; word-break: break-all; }
  .count { color: var(--muted); font-size: .88rem; margin: 2px 0 12px; }
  ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
  li.card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
  }
  li .title { font-weight: 600; font-size: 1.02rem; }
  li a.url { color: var(--accent2); text-decoration: none; word-break: break-all; font-size: .9rem; }
  li a.url:hover { text-decoration: underline; }
  li .desc { color: var(--muted); margin: 6px 0; }
  .meta { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; align-items: center; }
  .tag, .fbadge { font-size: .78rem; padding: 2px 9px; border-radius: 999px; border: 1px solid var(--border); }
  .tag { background: var(--panel2); color: var(--muted); }
  .fbadge { background: var(--accent-soft); color: var(--accent2); cursor: pointer; border-color: transparent; }
  .fbadge:hover { text-decoration: underline; }
  .actions { display: flex; gap: 4px; margin-top: 10px; }
  .empty { color: var(--muted); text-align: center; padding: 50px 20px; }
  footer.foot { text-align: center; color: var(--muted); font-size: .8rem; padding: 26px 20px 6px; }
  footer.foot a { color: var(--muted); }
  footer.foot a:hover { color: var(--text); }
</style>
</head>
<body>
<header>
  <h1>🔖 YAML Bookmarks</h1>
  <span class="sub">personal · localhost</span>
  <button id="lockBtn" class="lockbtn" type="button"
          title="Show encrypted bookmarks">🔓</button>
</header>
<div class="wrap">
  <div class="app">
    <aside class="compose">
      <form class="add" id="addForm">
        <div class="form-title" id="formTitle">Add a bookmark</div>
        <div class="enc-hint" id="encHint" style="display:none">🔒 New bookmarks will be encrypted</div>
        <div class="url-row">
          <input id="f-url" type="url" placeholder="https://example.com" required autocomplete="off">
          <button type="button" id="fetchBtn"
                  title="Fetch the title, description and keywords from this URL">⬇ Fetch</button>
        </div>
        <input id="f-title" type="text" placeholder="Title (optional)" autocomplete="off">
        <label class="fld">Collection
          <input id="f-folder" list="folderOptions" autocomplete="off"
                 placeholder="e.g. work/ideas (blank = Unsorted)">
        </label>
        <datalist id="folderOptions"></datalist>
        <input id="f-tags" type="text" placeholder="tags, comma, separated" autocomplete="off">
        <textarea id="f-desc" placeholder="Description (optional)"></textarea>
        <div class="row">
          <button type="submit" id="submitBtn">Add bookmark</button>
          <button type="button" class="ghost" id="cancelEdit" style="display:none">Cancel</button>
        </div>
      </form>

      <div class="folder-edit" id="folderEdit" style="display:none">
        <div class="form-title">📁 Folder: <span id="feName"></span></div>
        <label class="fld">Rename to
          <div class="fe-row">
            <input id="feRename" autocomplete="off" placeholder="new name">
            <button type="button" id="feRenameBtn">Rename</button>
          </div>
        </label>
        <label class="fld">Move under
          <div class="fe-row">
            <select id="feMove"></select>
            <button type="button" id="feMoveBtn">Move</button>
          </div>
        </label>
        <button type="button" class="fe-delete" id="feDeleteBtn">Delete folder</button>
      </div>
    </aside>

    <nav class="sidebar">
      <div class="side-head"><span>Collections</span>
        <button class="mini" id="newFolderBtn" title="New collection">＋</button></div>
      <div id="tree"></div>
    </nav>

    <section class="main">
      <input class="search" id="search" type="search" placeholder="Search bookmarks…">
      <div class="crumb"><h2 id="crumbTitle">All bookmarks</h2></div>
      <div class="count" id="count"></div>
      <ul id="list"></ul>
      <div class="empty" id="empty" style="display:none">Nothing here yet. Add a bookmark using the form.</div>
    </section>
  </div>
  <footer class="foot">__FOOTER__</footer>
</div>

<div id="pwModal" class="modal" style="display:none">
  <div class="modal-card">
    <div class="modal-title">Unlock encrypted bookmarks</div>
    <div class="pw-row">
      <input id="pwInput" class="masked" type="text" inputmode="text"
             autocomplete="off" autocapitalize="off" autocorrect="off"
             spellcheck="false" placeholder="Password">
      <button type="button" id="pwEye" class="eye" title="Show password">👁</button>
    </div>
    <div class="modal-actions">
      <button type="button" class="ghost" id="pwCancel">Cancel</button>
      <button type="button" id="pwOk">Unlock</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const ALL = '*ALL*';           // sentinel: '*' is illegal in folder names, so no clash
let all = [];
let folders = [];
let filter = ALL;              // ALL, '' (Unsorted/root), or a folder path
let collapsed = new Set();
let shown = [];                // items currently rendered (index-based actions)
let editingUrl = null;
let editingFolder = null;
let unlocked = false;

async function load() {
  const [b, f] = await Promise.all([
    fetch('/api/bookmarks').then((r) => r.json()),
    fetch('/api/folders').then((r) => r.json()),
  ]);
  all = b; folders = f;
  renderSidebar(); renderDatalist(); renderList();
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function countFor(path) { return all.filter((b) => b.folder === path).length; }

/* ---- Sidebar / collections tree ---- */
function buildTree(list) {
  const root = { children: {} };
  for (const f of list) {
    let node = root; const acc = [];
    for (const part of f.split('/')) {
      acc.push(part); const p = acc.join('/');
      node.children[part] = node.children[part] || { name: part, path: p, children: {} };
      node = node.children[part];
    }
  }
  return root;
}
function renderNode(node, depth, out) {
  for (const key of Object.keys(node.children).sort((a, b) => a.localeCompare(b))) {
    const c = node.children[key];
    const kids = Object.keys(c.children).length > 0;
    const isCol = collapsed.has(c.path);
    const active = filter === c.path ? ' active' : '';
    const chev = kids
      ? `<span class="chev" data-toggle="${esc(c.path)}">${isCol ? '▸' : '▾'}</span>`
      : '<span class="chev-spacer"></span>';
    out.push(
      `<div class="coll${active}" data-filter="${esc(c.path)}" style="padding-left:${8 + depth * 14}px">`
      + `${chev}<span class="ic">📁</span><span class="nm">${esc(c.name)}</span>`
      + `<span class="cnt">${countFor(c.path)}</span></div>`);
    if (kids && !isCol) renderNode(c, depth + 1, out);
  }
}
function topRow(f, ic, name, cnt) {
  const active = filter === f ? ' active' : '';
  return `<div class="coll${active}" data-filter="${esc(f)}">`
    + `<span class="chev-spacer"></span><span class="ic">${ic}</span>`
    + `<span class="nm">${esc(name)}</span><span class="cnt">${cnt}</span></div>`;
}
function renderSidebar() {
  const out = [];
  out.push(topRow(ALL, '🔖', 'All bookmarks', all.length));
  out.push(topRow('', '📄', 'Unsorted', countFor('')));
  out.push('<div class="sep"></div>');
  renderNode(buildTree(folders), 0, out);
  $('tree').innerHTML = out.join('');
}
function renderDatalist() {
  $('folderOptions').innerHTML = folders.map((f) => `<option value="${esc(f)}">`).join('');
}

/* ---- Main list ---- */
function crumbLabel() {
  if (filter === ALL) return 'All bookmarks';
  if (filter === '') return 'Unsorted';
  return filter;
}
function renderList() {
  const q = $('search').value.trim().toLowerCase();
  shown = all
    .filter((b) => filter === ALL || b.folder === filter)
    .filter((b) => !q || [b.url, b.title, b.description, b.folder, (b.tags||[]).join(' ')]
      .join(' ').toLowerCase().includes(q));
  $('crumbTitle').textContent = crumbLabel();
  $('count').textContent = shown.length
    ? `${shown.length} bookmark${shown.length === 1 ? '' : 's'}` : '';
  $('empty').style.display = shown.length === 0 ? 'block' : 'none';
  $('list').innerHTML = shown.map((b, i) => {
    const tags = (b.tags||[]).map((t) => `<span class="tag">${esc(t)}</span>`).join('');
    const fb = b.folder
      ? `<span class="fbadge" data-goto="${esc(b.folder)}">📁 ${esc(b.folder)}</span>` : '';
    const lock = b.encrypted ? '<span class="lock-badge" title="Encrypted">🔒</span>' : '';
    return `<li class="card">
      <div class="title">${esc(b.title || b.url)}${lock}</div>
      <a class="url" href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.url)}</a>
      ${b.description ? `<div class="desc">${esc(b.description)}</div>` : ''}
      ${(fb || tags) ? `<div class="meta">${fb}${tags}</div>` : ''}
      <div class="actions">
        <button class="ghost" data-edit="${i}">Edit</button>
        <button class="ghost danger" data-del="${i}">Delete</button>
      </div></li>`;
  }).join('');
  updateFolderEdit();
}

/* ---- Events ---- */
$('tree').addEventListener('click', (e) => {
  const tog = e.target.closest('.chev');
  if (tog) {
    const p = tog.getAttribute('data-toggle');
    collapsed.has(p) ? collapsed.delete(p) : collapsed.add(p);
    renderSidebar();
    return;
  }
  const coll = e.target.closest('.coll');
  if (!coll) return;
  filter = coll.getAttribute('data-filter');
  if (!editingUrl && filter !== ALL) $('f-folder').value = filter; // new adds land here
  renderSidebar(); renderList();
});

$('list').addEventListener('click', async (e) => {
  const goto = e.target.getAttribute('data-goto');
  if (goto !== null) { filter = goto; renderSidebar(); renderList(); return; }
  const delI = e.target.getAttribute('data-del');
  const editI = e.target.getAttribute('data-edit');
  if (delI !== null) {
    const b = shown[+delI];
    if (!b || !confirm('Delete this bookmark?')) return;
    await fetch('/api/bookmarks?url=' + encodeURIComponent(b.url)
      + '&folder=' + encodeURIComponent(b.folder), { method: 'DELETE' });
    if (editingUrl === b.url) resetForm();
    await load();
  } else if (editI !== null) {
    const b = shown[+editI];
    if (!b) return;
    editingUrl = b.url; editingFolder = b.folder;
    $('f-url').value = b.url; $('f-url').readOnly = true;
    $('f-title').value = b.title || '';
    $('f-folder').value = b.folder || '';
    $('f-desc').value = b.description || '';
    $('f-tags').value = (b.tags || []).join(', ');
    $('formTitle').textContent = 'Edit bookmark';
    $('submitBtn').textContent = 'Save changes';
    $('cancelEdit').style.display = 'inline-block';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
});

$('addForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('f-url').value.trim();
  if (!url) return;
  const payload = {
    url,
    title: $('f-title').value.trim(),
    description: $('f-desc').value.trim(),
    tags: $('f-tags').value,
    folder: $('f-folder').value.trim(),
  };
  if (editingUrl) payload.original_folder = editingFolder;
  const res = await fetch('/api/bookmarks', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!res.ok) { const d = await res.json().catch(() => ({})); alert(d.error || 'Could not save.'); return; }
  resetForm();
  await load();
});

$('newFolderBtn').addEventListener('click', async () => {
  const base = (filter !== ALL && filter !== '') ? filter + '/' : '';
  const name = prompt('New collection' + (base ? ' inside ' + base : '') + ':');
  if (!name) return;
  const res = await fetch('/api/folders', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ folder: base + name }),
  });
  const d = await res.json();
  if (!res.ok) { alert(d.error || 'Could not create collection.'); return; }
  await load();
  filter = d.folder; $('f-folder').value = d.folder;
  renderSidebar(); renderList();
});

/* ---- Folder management panel (shown when a folder is selected) ---- */
function updateFolderEdit() {
  const isFolder = filter !== ALL && filter !== '';
  $('folderEdit').style.display = isFolder ? 'grid' : 'none';
  if (!isFolder) return;
  $('feName').textContent = filter;
  $('feRename').value = filter.split('/').pop();
  // Move-target dropdown: root is always offered, plus every folder that is a
  // valid destination (not this folder itself and not one of its descendants).
  const parent = filter.split('/').slice(0, -1).join('/');
  const opts = ['<option value="">(root)</option>'];
  for (const f of folders) {
    if (f === filter || f.startsWith(filter + '/')) continue;
    opts.push(`<option value="${esc(f)}">${esc(f)}</option>`);
  }
  const sel = $('feMove');
  sel.innerHTML = opts.join('');
  sel.value = parent;   // preselect the current location ("" = root)
}

async function folderOp(url, body) {
  const res = await fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const d = await res.json();
  if (!res.ok) { alert(d.error || 'Folder operation failed.'); return null; }
  return d;
}

$('feRenameBtn').addEventListener('click', async () => {
  const name = $('feRename').value.trim();
  if (!name || name === filter.split('/').pop()) return;
  const d = await folderOp('/api/folders/rename', { folder: filter, name });
  if (!d) return;
  filter = d.folder;
  if (!editingUrl) $('f-folder').value = filter;
  await load();
});

$('feMoveBtn').addEventListener('click', async () => {
  const parent = $('feMove').value;   // "" means root
  const d = await folderOp('/api/folders/move', { folder: filter, parent });
  if (!d) return;
  filter = d.folder;
  if (!editingUrl) $('f-folder').value = filter;
  await load();
});

$('feDeleteBtn').addEventListener('click', async () => {
  if (!confirm(`Delete folder "${filter}"?\\nBookmarks inside will be moved to "orphaned".`)) return;
  const d = await folderOp('/api/folders/delete', { folder: filter });
  if (!d) return;
  filter = ALL;
  await load();
});

$('fetchBtn').addEventListener('click', async () => {
  const url = $('f-url').value.trim();
  if (!url) { $('f-url').focus(); return; }
  const btn = $('fetchBtn'); const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  try {
    const res = await fetch('/api/fetch-meta?url=' + encodeURIComponent(url));
    const data = await res.json();
    if (!res.ok) { alert(data.error || 'Could not fetch page info.'); return; }
    // Only fill empty fields so we never clobber what you've typed.
    if (data.title && !$('f-title').value.trim()) $('f-title').value = data.title;
    if (data.description && !$('f-desc').value.trim()) $('f-desc').value = data.description;
    if ((data.tags || []).length && !$('f-tags').value.trim()) $('f-tags').value = data.tags.join(', ');
    if (!data.title && !data.description && !(data.tags || []).length)
      alert('No title, description or keywords found on that page.');
  } catch (e) {
    alert('Could not reach that URL.');
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
});

/* ---- Encryption padlock ---- */
function updateLock() {
  const btn = $('lockBtn');
  btn.textContent = unlocked ? '🔒' : '🔓';
  btn.classList.toggle('engaged', unlocked);
  btn.title = unlocked
    ? 'Password engaged — click to lock and hide encrypted bookmarks'
    : 'Show encrypted bookmarks (enter a password)';
  $('encHint').style.display = unlocked ? 'block' : 'none';
}

function openPwModal() {
  $('pwInput').value = '';
  $('pwInput').classList.add('masked');   // masked by default
  $('pwEye').textContent = '👁';
  $('pwEye').title = 'Show password';
  $('pwModal').style.display = 'flex';
  setTimeout(() => $('pwInput').focus(), 40);
}
function closePwModal() {
  $('pwModal').style.display = 'none';
  $('pwInput').value = '';                 // don't leave the password lying around
}

async function doUnlock(password) {
  const res = await fetch('/api/unlock', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ password }),
  });
  const d = await res.json();
  if (!res.ok) { alert(d.error || 'Could not unlock.'); return; }
  unlocked = true;
  updateLock();
  await load();
  if (d.count === 0 && d.encrypted_total > 0) {
    alert('No encrypted bookmarks match that password. New bookmarks you add will be encrypted with it.');
  }
}

$('lockBtn').addEventListener('click', async () => {
  if (unlocked) {
    await fetch('/api/lock', { method: 'POST' });
    unlocked = false;
    updateLock();
    await load();
  } else {
    openPwModal();
  }
});

$('pwEye').addEventListener('click', () => {
  const inp = $('pwInput');
  const masked = inp.classList.toggle('masked');
  $('pwEye').textContent = masked ? '👁' : '🙈';
  $('pwEye').title = masked ? 'Show password' : 'Hide password';
  inp.focus();
});
$('pwCancel').addEventListener('click', closePwModal);
$('pwOk').addEventListener('click', async () => {
  const pw = $('pwInput').value;
  if (!pw) { $('pwInput').focus(); return; }
  closePwModal();
  await doUnlock(pw);
});
$('pwInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('pwOk').click(); }
  else if (e.key === 'Escape') { e.preventDefault(); closePwModal(); }
});
$('pwModal').addEventListener('click', (e) => {
  if (e.target === $('pwModal')) closePwModal();   // click backdrop to dismiss
});

$('cancelEdit').addEventListener('click', resetForm);
$('search').addEventListener('input', renderList);

function resetForm() {
  editingUrl = null; editingFolder = null;
  $('addForm').reset();
  $('f-url').readOnly = false;
  // keep the current collection prefilled for the next add
  $('f-folder').value = (filter !== ALL) ? filter : '';
  $('formTitle').textContent = 'Add a bookmark';
  $('submitBtn').textContent = 'Add bookmark';
  $('cancelEdit').style.display = 'none';
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js'));
}

async function init() {
  try {
    const s = await fetch('/api/status').then((r) => r.json());
    unlocked = !!s.unlocked;
  } catch (e) { unlocked = false; }
  updateLock();
  await load();
}
init();
</script>
</body>
</html>
"""
