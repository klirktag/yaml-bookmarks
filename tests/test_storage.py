# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
import pytest

from yaml_bookmarks.storage import (
    Bookmark,
    BookmarkStore,
    EncryptionRequired,
    VaultLocked,
    normalize_folder,
)


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


# -- encryption --------------------------------------------------------------

def test_encrypted_add_requires_unlock(store):
    with pytest.raises(VaultLocked):
        store.add("https://s.example", encrypt=True)


def test_encrypted_hidden_until_unlock(store):
    store.unlock("pw")
    store.add("https://s.example", encrypt=True, title="Secret")
    store.lock()
    assert store.list() == []                       # hidden while locked
    assert store.get("https://s.example") is None
    store.unlock("pw")
    got = store.get("https://s.example")
    assert got is not None and got.encrypted and got.title == "Secret"


def test_encrypted_file_is_opaque_on_disk(store, tmp_path):
    store.unlock("pw")
    store.add("https://secret.example/path", encrypt=True, title="T", tags=["k"])
    files = list(tmp_path.rglob("*.yaml"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "crypt: true" in text
    assert "secret.example" not in text     # url not leaked
    assert "url:" not in text and "title:" not in text


def test_wrong_password_reveals_nothing(store):
    store.unlock("right")
    store.add("https://s.example", encrypt=True)
    store.lock()
    store.unlock("wrong")
    assert store.list() == []


def test_encrypted_update_move_remove(store):
    store.unlock("pw")
    store.add("https://s.example", encrypt=True, folder="a", title="v1")
    created = store.get("https://s.example", "a").created_at
    up = store.update("https://s.example", folder="a", title="v2")
    assert up.title == "v2" and up.created_at == created
    mv = store.move("https://s.example", "a", "b")
    assert mv.folder == "b" and mv.encrypted
    assert store.get("https://s.example", "a") is None
    assert store.get("https://s.example", "b").title == "v2"
    assert store.remove("https://s.example", "b") is True


def test_plaintext_and_encrypted_coexist(store):
    store.add("https://plain.example", title="P")
    store.unlock("pw")
    store.add("https://enc.example", encrypt=True, title="E")
    kinds = {b.url: b.encrypted for b in store.list()}
    assert kinds == {"https://plain.example": False, "https://enc.example": True}


def test_folder_ops_preserve_encrypted(store):
    store.unlock("pw")
    store.add("https://s.example", folder="work", encrypt=True, title="S")
    store.rename_folder("work", "w2")
    got = store.get("https://s.example", "w2")
    assert got is not None and got.encrypted and got.title == "S"


# -- folder management -------------------------------------------------------

def test_rename_folder(store):
    store.add("https://a.example", folder="work/ideas", title="A")
    assert store.rename_folder("work/ideas", "concepts") == "work/concepts"
    assert store.get("https://a.example", "work/concepts").folder == "work/concepts"
    assert store.get("https://a.example", "work/ideas") is None


def test_move_folder(store):
    store.add("https://a.example", folder="work/ideas")
    assert store.move_folder("work/ideas", "personal") == "personal/ideas"
    assert store.get("https://a.example", "personal/ideas") is not None


def test_move_folder_into_itself_rejected(store):
    store.add("https://a.example", folder="work")
    with pytest.raises(ValueError):
        store.move_folder("work", "work")


def test_rename_to_existing_folder_rejected(store):
    store.add("https://a.example", folder="work/ideas")
    store.create_folder("work/concepts")
    with pytest.raises(ValueError):
        store.rename_folder("work/ideas", "concepts")


def test_delete_folder_orphans_bookmarks(store):
    store.add("https://a.example", folder="work", title="A")
    store.add("https://b.example", folder="work/sub", title="B")
    assert store.delete_folder("work") == "orphaned"
    assert store.get("https://a.example", "orphaned").title == "A"
    # sub-path is preserved under orphaned/
    assert store.get("https://b.example", "orphaned/sub").title == "B"
    assert "work" not in store.folders()


def test_delete_orphaned_folder_rejected(store):
    store.create_folder("orphaned")
    with pytest.raises(ValueError):
        store.delete_folder("orphaned")


# -- settings / allow_unencrypted --------------------------------------------

def test_settings_file_is_not_a_bookmark(store, tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "port: 22222\nallow_unencrypted: true\n", encoding="utf-8"
    )
    store.add("https://a.example")
    assert [b.url for b in store.list()] == ["https://a.example"]


def test_allow_unencrypted_false_blocks_plaintext_add(store):
    store.allow_unencrypted = False
    with pytest.raises(EncryptionRequired):
        store.add("https://a.example")
    with pytest.raises(EncryptionRequired):
        store.save(Bookmark(url="https://b.example"))
    # encrypted still works
    store.unlock("pw")
    store.add("https://c.example", encrypt=True)
    assert store.get("https://c.example") is not None


def test_allow_unencrypted_false_allows_editing_existing_plaintext(store):
    store.add("https://a.example", title="v1")   # created while allowed
    store.allow_unencrypted = False
    up = store.save(Bookmark(url="https://a.example", title="v2"))  # editing, not adding
    assert up.title == "v2" and not up.encrypted


def test_new_bookmarks_join_existing_vault_salt(store, tmp_path):
    store.unlock("pw")
    store.add("https://a.example", encrypt=True)
    store.lock()
    store.unlock("pw")  # re-unlock: should adopt the existing file's salt
    store.add("https://b.example", encrypt=True)
    import yaml
    salts = {
        yaml.safe_load(p.read_text())["salt"]
        for p in tmp_path.rglob("*.yaml")
    }
    assert len(salts) == 1  # both encrypted files share one vault salt
