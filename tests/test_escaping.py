# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
from yaml_bookmarks.escaping import escape_url, unescape_url, filename_for_url, MAX_NAME_LEN


ROUNDTRIP_URLS = [
    "https://example.com",
    "https://example.com/path?a=1&b=2#frag",
    "http://пример.рф/страница",
    "https://example.com/Path/With/CAPS",
    "ftp://user:pass@host:21/dir/file name.txt",
    "https://example.com/emoji/🔖",
    "a",
]


def test_roundtrip():
    for url in ROUNDTRIP_URLS:
        assert unescape_url(escape_url(url)) == url


def test_only_safe_chars_in_output():
    # lowercase letters, digits, - . kept raw; _ marks an escape; A-F are hex.
    safe = set("abcdefghijklmnopqrstuvwxyz0123456789-._ABCDEF")
    for url in ROUNDTRIP_URLS:
        assert set(escape_url(url)) <= safe


def test_case_does_not_collide():
    # case-insensitive filesystems must not merge these two
    assert filename_for_url("https://X.com") != filename_for_url("https://x.com")


def test_deterministic():
    url = "https://example.com/deterministic"
    assert filename_for_url(url) == filename_for_url(url)


def test_filename_has_yaml_extension():
    assert filename_for_url("https://example.com").endswith(".yaml")


def test_long_url_is_truncated_but_stable():
    url = "https://example.com/" + "x" * 5000
    name = filename_for_url(url)
    assert len(name) <= MAX_NAME_LEN + len(".yaml")
    assert name == filename_for_url(url)  # still deterministic
    # distinct long URLs get distinct names via the hash suffix
    assert name != filename_for_url(url + "y")
