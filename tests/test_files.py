import threading
from pathlib import Path

import pytest

from astrbot_plugin_rclone.files import (
    SyncError,
    destination_path,
    source_path,
    stage_file,
)


def test_source_resolves_relative_paths_and_rejects_missing_or_private(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    assert source_path("source", tmp_path, tmp_path / "private") == source
    with pytest.raises(SyncError, match="不存在或无法读取"):
        source_path("missing", tmp_path, tmp_path / "private")
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(SyncError, match="自己的状态"):
        source_path(str(private), tmp_path, private)


def test_unreadable_file_fails_instead_of_becoming_empty_source(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("important")
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(SyncError, match="未将其视为空目录"):
        source_path(str(source), tmp_path, tmp_path / "private")


def test_staged_file_preserves_name_content_and_modification_time(tmp_path):
    source = tmp_path / "export.zip"
    source.write_bytes(b"original export")
    staged = stage_file(source, tmp_path / "staged", threading.Event())
    artifact = staged / source.name
    assert artifact.read_bytes() == source.read_bytes()
    assert artifact.stat().st_mtime_ns == source.stat().st_mtime_ns
    assert source.read_bytes() == b"original export"


def test_staging_detects_source_changes(tmp_path, monkeypatch):
    source = tmp_path / "export.zip"
    source.write_text("first")
    original = Path.stat
    reads = 0

    def modified(path, *args, **kwargs):
        nonlocal reads
        if path == source:
            reads += 1
            if reads == 2:
                source.write_text("changed while reading")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", modified)
    with pytest.raises(SyncError, match="发生变化"):
        stage_file(source, tmp_path / "staged", threading.Event())


def test_staging_honors_cancellation(tmp_path):
    source = tmp_path / "file"
    source.write_bytes(b"important")
    event = threading.Event()
    event.set()
    with pytest.raises(SyncError, match="已取消"):
        stage_file(source, tmp_path / "staged", event)


def test_download_destination_validation_does_not_create_directories(tmp_path):
    destination = tmp_path / "new" / "downloads"
    assert (
        destination_path(str(destination), tmp_path, tmp_path / "private")
        == destination
    )
    assert not destination.parent.exists()
    file = tmp_path / "file"
    file.write_text("not a directory")
    for value, error in (
        (file, "必须是目录"),
        (tmp_path / "private", "自己的状态"),
        (Path(tmp_path.anchor), "根目录"),
    ):
        with pytest.raises(SyncError, match=error):
            destination_path(str(value), tmp_path, tmp_path / "private")


def test_download_destination_rejects_existing_directory_links(tmp_path):
    import os

    outside, destination = tmp_path / "outside", tmp_path / "destination"
    outside.mkdir()
    destination.mkdir()
    link = destination / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SyncError, match="内部存在链接"):
        destination_path(str(destination), tmp_path, tmp_path / "private")
    with pytest.raises(SyncError, match="父目录不能"):
        destination_path(str(link / "new"), tmp_path, tmp_path / "private")
