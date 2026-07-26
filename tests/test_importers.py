# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
import pytest

from yaml_bookmarks.importers import (
    _raindrop_folder,
    import_raindrop,
    parse_raindrop_csv,
)
from yaml_bookmarks.storage import BookmarkStore

CSV = (
    "id,title,note,excerpt,url,folder,tags,created,cover,highlights,favorite\n"
    "1,Root Item,,,https://root.example,Unsorted,,2026-03-30T11:36:02.712Z,,,false\n"
    "2,Nested,a note,,https://nested.example,Acting / Voice acting,\"a, b\","
    "2024-04-13T14:17:33.000Z,,,false\n"
    "3,Slashy,,an excerpt,https://slash.example,Båt/kanot,,2024-01-01T00:00:00Z,,,false\n"
    "4,No URL,,,,SomeFolder,,,,,\n"
)


@pytest.fixture
def store(tmp_path):
    return BookmarkStore(tmp_path)


def test_folder_mapping():
    assert _raindrop_folder("Unsorted") == ""
    assert _raindrop_folder("Acting / Voice acting") == "Acting/Voice acting"
    assert _raindrop_folder("Båt/kanot") == "Båt-kanot"        # literal slash sanitised
    assert _raindrop_folder("Bad:Name / Ok?") == "Bad-Name/Ok-"  # illegal chars sanitised


def test_parse_fields():
    bms = {b.url: b for b in parse_raindrop_csv(CSV)}
    assert set(bms) == {
        "https://root.example",
        "https://nested.example",
        "https://slash.example",
    }  # the row without a URL is skipped
    import datetime

    def _unix(iso):
        return int(datetime.datetime.fromisoformat(iso).timestamp())

    root = bms["https://root.example"]
    assert root.folder == "" and root.title == "Root Item"
    assert root.created == _unix("2026-03-30T11:36:02+00:00")   # unix seconds
    nested = bms["https://nested.example"]
    assert nested.folder == "Acting/Voice acting"
    assert nested.tags == ["a", "b"]
    assert nested.description == "a note"                       # note preferred
    assert bms["https://slash.example"].description == "an excerpt"  # falls back to excerpt


def test_import_plaintext(store):
    summary = import_raindrop(store, CSV, encrypt=False)
    assert summary == {
        "format": "raindrop",
        "total": 3,
        "added": 3,
        "failed": 0,
        "encrypted": False,
    }
    assert len(store.list()) == 3
    assert not any(b.encrypted for b in store.list())


def test_import_encrypted_hidden_until_unlock(store):
    store.unlock("pw")
    summary = import_raindrop(store, CSV, encrypt=True)
    assert summary["added"] == 3 and summary["encrypted"] is True
    assert all(b.encrypted for b in store.list())
    store.lock()
    assert store.list() == []               # hidden while locked
    store.unlock("pw")
    assert len(store.list()) == 3


def test_reimport_is_idempotent(store):
    import_raindrop(store, CSV, encrypt=False)
    import_raindrop(store, CSV, encrypt=False)   # same rows again
    assert len(store.list()) == 3                # upsert, not duplicated
