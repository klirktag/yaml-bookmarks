# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 The yaml-bookmarks authors
from yaml_bookmarks.settings import (
    SETTINGS_FILENAME,
    ensure_settings_file,
    load_settings,
    settings_path,
)


def test_defaults_when_missing(tmp_path):
    s = load_settings(tmp_path)
    assert s.port == 22222
    assert s.allow_unencrypted is False  # secure default: encryption required


def test_ensure_creates_documented_file(tmp_path):
    assert not settings_path(tmp_path).exists()
    s = ensure_settings_file(tmp_path)
    path = settings_path(tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "port:" in text and "allow_unencrypted:" in text
    assert "#" in text  # comments present
    assert s.port == 22222 and s.allow_unencrypted is False


def test_reads_configured_values(tmp_path):
    (tmp_path / SETTINGS_FILENAME).write_text(
        "port: 9000\nallow_unencrypted: false\n", encoding="utf-8"
    )
    s = load_settings(tmp_path)
    assert s.port == 9000
    assert s.allow_unencrypted is False


def test_bad_port_falls_back(tmp_path):
    (tmp_path / SETTINGS_FILENAME).write_text("port: not-a-port\n", encoding="utf-8")
    assert load_settings(tmp_path).port == 22222


def test_out_of_range_port_falls_back(tmp_path):
    (tmp_path / SETTINGS_FILENAME).write_text("port: 99999\n", encoding="utf-8")
    assert load_settings(tmp_path).port == 22222


def test_string_bool_is_coerced(tmp_path):
    (tmp_path / SETTINGS_FILENAME).write_text('allow_unencrypted: "no"\n', encoding="utf-8")
    assert load_settings(tmp_path).allow_unencrypted is False
