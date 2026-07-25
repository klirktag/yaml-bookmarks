# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
import pytest

from yaml_bookmarks.storage import BookmarkStore, Bookmark, normalize_folder


@pytest.fixture
def store(tmp_path):
    return BookmarkStore(tmp_path)


def test_add_and_get(store):
    store.add("https://example.com", title="Example", tags=["a", "b"])
    b = store.get("https://example.com")
    assert b is not None
    assert b.title == "Example"
    assert b.tags == ["a", "b"]
    assert b.created_at and b.updated_at


def test_add_duplicate_raises(store):
    store.add("https://example.com")
    with pytest.raises(FileExistsError):
        store.add("https://example.com")


def test_file_lives_under_dir(store, tmp_path):
    store.add("https://example.com/path")
    files = list(tmp_path.glob("*.yaml"))
    assert len(files) == 1


def test_update(store):
    store.add("https://example.com", title="Old")
    created = store.get("https://example.com").created_at
    updated = store.update("https://example.com", title="New", tags=["x"])
    assert updated.title == "New"
    assert updated.tags == ["x"]
    assert updated.created_at == created  # created_at preserved


def test_update_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.update("https://missing.com", title="x")


def test_remove(store):
    store.add("https://example.com")
    assert store.remove("https://example.com") is True
    assert store.get("https://example.com") is None
    assert store.remove("https://example.com") is False


def test_list_sorted_recent_first(store):
    store.add("https://a.com")
    store.add("https://b.com")
    store.update("https://a.com", title="touched")  # bumps updated_at
    urls = [b.url for b in store.list()]
    assert urls[0] == "https://a.com"
    assert set(urls) == {"https://a.com", "https://b.com"}


def test_save_upsert(store):
    store.save(Bookmark(url="https://example.com", title="v1"))
    store.save(Bookmark(url="https://example.com", title="v2"))
    assert store.get("https://example.com").title == "v2"
    assert len(store.list()) == 1


def test_bad_file_is_skipped(store, tmp_path):
    store.add("https://example.com")
    (tmp_path / "junk.yaml").write_text("not: a bookmark\n", encoding="utf-8")
    (tmp_path / "broken.yaml").write_text(":\n  - [unclosed", encoding="utf-8")
    assert len(store.list()) == 1


# -- folders -----------------------------------------------------------------

def test_add_in_folder_creates_nested_dir(store, tmp_path):
    store.add("https://example.com", folder="work/projects", title="P")
    f = tmp_path / "work" / "projects"
    assert f.is_dir()
    assert len(list(f.glob("*.yaml"))) == 1
    b = store.get("https://example.com", folder="work/projects")
    assert b.folder == "work/projects" and b.title == "P"


def test_folder_not_stored_in_yaml(store, tmp_path):
    store.add("https://example.com", folder="work")
    text = next((tmp_path / "work").glob("*.yaml")).read_text(encoding="utf-8")
    assert "folder:" not in text  # location is the source of truth


def test_same_url_in_different_folders(store):
    store.add("https://example.com", folder="a", title="A")
    store.add("https://example.com", folder="b", title="B")
    assert store.get("https://example.com", folder="a").title == "A"
    assert store.get("https://example.com", folder="b").title == "B"
    assert len(store.list()) == 2


def test_list_carries_folder(store):
    store.add("https://root.com")
    store.add("https://nested.com", folder="x/y")
    by_url = {b.url: b.folder for b in store.list()}
    assert by_url["https://root.com"] == ""
    assert by_url["https://nested.com"] == "x/y"


def test_move_preserves_created_at(store):
    store.add("https://example.com", folder="a", title="T", tags=["k"])
    created = store.get("https://example.com", folder="a").created_at
    moved = store.move("https://example.com", "a", "b/c")
    assert moved.folder == "b/c"
    assert store.get("https://example.com", folder="a") is None
    dst = store.get("https://example.com", folder="b/c")
    assert dst.title == "T" and dst.tags == ["k"]
    assert dst.created_at == created


def test_folders_lists_tree_including_empty(store):
    store.add("https://example.com", folder="a/b")
    store.create_folder("empty/child")
    assert set(store.folders()) == {"a", "a/b", "empty", "empty/child"}


def test_normalize_folder_rejects_traversal():
    with pytest.raises(ValueError):
        normalize_folder("../etc")


def test_normalize_folder_rejects_illegal_chars():
    with pytest.raises(ValueError):
        normalize_folder("bad:name")


def test_normalize_folder_cleans_slashes():
    assert normalize_folder("/work//projects/") == "work/projects"
    assert normalize_folder("") == ""
    assert normalize_folder(None) == ""


def test_remove_in_folder(store):
    store.add("https://example.com", folder="a")
    assert store.remove("https://example.com", folder="a") is True
    assert store.get("https://example.com", folder="a") is None
