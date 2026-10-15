"""Run real rclone against an isolated alias backend, including destructive mirroring."""

import asyncio
import shutil
from pathlib import Path

import pytest

from astrbot_plugin_rclone.service import SyncService


@pytest.fixture
async def mirror(tmp_path):
    bundled = Path(__file__).parent / "tools" / "rclone.exe"
    binary = str(bundled.resolve()) if bundled.is_file() else shutil.which("rclone")
    if not binary:
        pytest.skip("Real rclone required")
    local, remote = tmp_path / "local", tmp_path / "cloud"
    local.mkdir()
    remote.mkdir()
    private = local / "data" / "plugin_data" / "astrbot_plugin_rclone"
    private.mkdir(parents=True)
    service = SyncService(local, private)
    service.config_path.write_text(
        f"[backup]\ntype = alias\nremote = {remote.as_posix()}\n", encoding="utf-8"
    )
    await service.save_connection(
        {"binary": binary, "remote": "backup", "folder": "Mirrors"}
    )
    await service.initialize()
    await service.connect()
    yield service, local, remote
    await service.close()


def task(service, source, **overrides):
    return service.save_task(
        {
            "name": "files",
            "mode": "upload_mirror",
            "local_path": str(source),
            "remote_path": "files",
            "exclude": [],
            "enabled": True,
            "interval_minutes": 0,
            **overrides,
        }
    )


async def execute(service, value):
    run = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 20)
    assert run["status"] == "completed", run
    return run


async def test_mirror_adds_updates_deletes_and_keeps_original_structure(mirror):
    service, local, remote = mirror
    source = local / "documents"
    source.mkdir()
    (source / "keep.txt").write_text("first")
    (source / "gone.txt").write_text("remove later")
    (source / "empty").mkdir()
    value = task(service, source)
    first = await execute(service, value)
    target = remote / "Mirrors" / "files"
    assert (target / "keep.txt").read_text() == "first"
    assert (target / "empty").is_dir()
    (remote / "unrelated.txt").write_text("keep unrelated")
    (target / "extra.txt").write_text("cloud only")
    (source / "keep.txt").write_text("updated content")
    (source / "gone.txt").unlink()
    (source / "new.txt").write_text("new")
    second = await execute(service, value)
    assert (target / "keep.txt").read_text() == "updated content"
    assert (target / "new.txt").read_text() == "new"
    assert not (target / "gone.txt").exists() and not (target / "extra.txt").exists()
    assert (remote / "unrelated.txt").read_text() == "keep unrelated"
    assert second["progress"]["deletes"] == 2
    assert first["progress"]["transfers"] == 2
    assert not any(
        path.name in {"backup.zip", "manifest.json", value["id"]}
        for path in target.rglob("*")
    )
    assert (source / "keep.txt").read_text() == "updated content"
    unchanged = await execute(service, value)
    assert unchanged["progress"]["transfers"] == 0


async def test_empty_directory_clears_target_but_missing_source_never_does(mirror):
    service, local, remote = mirror
    source = local / "documents"
    source.mkdir()
    (source / "file.txt").write_text("important")
    value = task(service, source)
    await execute(service, value)
    target = remote / "Mirrors" / "files" / "file.txt"
    source.rename(local / "renamed")
    failed = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 5)
    assert failed["status"] == "failed" and target.read_text() == "important"
    source.mkdir()
    completed = await execute(service, value)
    assert not target.exists() and completed["progress"]["deletes"] == 1


@pytest.mark.parametrize("mode", ["upload", "upload_mirror"])
async def test_single_file_mirrors_only_that_file_and_preserves_unchanged_timestamp(
    mirror,
    mode,
):
    service, local, remote = mirror
    source = local / "export.zip"
    source.write_bytes(b"exported data")
    (local / "unrelated.txt").write_text("not selected")
    value = task(service, source, mode=mode)
    await execute(service, value)
    target = remote / "Mirrors" / "files"
    assert [path.name for path in target.iterdir()] == ["export.zip"]
    unchanged = await execute(service, value)
    assert unchanged["progress"]["transfers"] == 0
    source.unlink()
    failed = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 5)
    assert failed["status"] == "failed"
    assert (target / "export.zip").read_bytes() == b"exported data"
    assert not list(service.work.iterdir())


async def test_exclusions_and_plugin_private_directory_are_protected(mirror):
    service, local, remote = mirror
    (local / "important.txt").write_text("important")
    (local / "ignored.tmp").write_text("ignore")
    (local / "cache").mkdir()
    (local / "cache" / "cache.db").write_text("ignore")
    value = task(service, local, exclude=["*.tmp", "cache/**"])
    await execute(service, value)
    target = remote / "Mirrors" / "files"
    assert (target / "important.txt").read_text() == "important"
    assert not (target / "data" / "plugin_data" / "astrbot_plugin_rclone").exists()
    assert not (target / "ignored.tmp").exists() and not (target / "cache").exists()
    (target / "ignored.tmp").write_text("ignored at destination too")
    await execute(service, value)
    assert (target / "ignored.tmp").exists()


async def test_dry_run_reports_changes_without_modifying_cloud(mirror):
    service, local, remote = mirror
    source = local / "documents"
    source.mkdir()
    for name in ("same", "update", "delete"):
        (source / name).write_text(name)
    value = task(service, source)
    await execute(service, value)
    (source / "upload").write_text("new")
    (source / "update").write_text("updated content")
    (source / "delete").unlink()
    target = remote / "Mirrors" / "files"
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    preview = await service.preview(
        {
            "id": value["id"],
            "mode": "upload_mirror",
            "local_path": str(source),
            "remote_path": "files",
            "exclude": [],
        }
    )
    assert preview["counts"] == {"add": 1, "update": 1, "delete": 1, "unchanged": 1}
    assert {item["action"] for item in preview["changes"]} == {
        "add",
        "update",
        "delete",
    }
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    assert not service.previewing and not list(service.work.iterdir())


async def test_transfer_error_prevents_destination_deletions(mirror, monkeypatch):
    service, local, remote = mirror
    source = local / "documents"
    source.mkdir()
    (source / "keep").write_text("important")
    value = task(service, source)
    await execute(service, value)
    target = remote / "Mirrors" / "files"
    (target / "cloud-only").write_text("retain on failure")
    (source / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    original = service.cloud._run

    async def restricted(*args, **kwargs):
        if args[0] == "sync":
            args += ("--max-transfer", "1B", "--cutoff-mode", "HARD")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service.cloud, "_run", restricted)
    run = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 20)
    assert run["status"] == "failed"
    assert (target / "cloud-only").read_text() == "retain on failure"


MODES = ("upload", "upload_mirror", "download", "download_mirror")


def snapshot(directory):
    return (
        {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }
        if directory.exists()
        else None
    )


@pytest.mark.parametrize("mode", MODES)
async def test_all_modes_preview_and_execute_in_the_selected_direction(mirror, mode):
    import os
    import time

    service, local, remote = mirror
    local_path = local / "files"
    cloud_path = remote / "Mirrors" / "files"
    local_path.mkdir()
    cloud_path.mkdir(parents=True)
    uploading = mode.startswith("upload")
    origin, destination = (
        (local_path, cloud_path) if uploading else (cloud_path, local_path)
    )
    (origin / "new.txt").write_text("new file")
    (origin / "changed.txt").write_text("source content wins, even over a newer target")
    (origin / "empty").mkdir()
    (destination / "changed.txt").write_text("newer target")
    future = time.time() + 3600
    os.utime(destination / "changed.txt", (future, future))
    (destination / "extra.txt").write_text("destination only")
    (destination.parent / "unrelated.txt").write_text("outside destination")
    value = task(service, local_path, mode=mode)
    before_local, before_cloud = snapshot(local_path), snapshot(cloud_path)
    preview = await service.preview(
        {
            "id": value["id"],
            "mode": mode,
            "local_path": str(local_path),
            "remote_path": "files",
            "exclude": [],
        }
    )
    assert preview["counts"]["add"] == 1, preview
    assert preview["counts"]["update"] == 1, preview
    assert preview["counts"]["delete"] == (1 if "mirror" in mode else 0), preview
    assert snapshot(local_path) == before_local and snapshot(cloud_path) == before_cloud
    original = snapshot(origin)
    run = await execute(service, value)
    assert run["mode"] == mode and run["remote_path"] == "backup:Mirrors/files"
    assert (destination / "new.txt").read_text() == "new file"
    assert (destination / "changed.txt").read_bytes() == (
        origin / "changed.txt"
    ).read_bytes()
    assert (destination / "empty").is_dir()
    assert (destination / "extra.txt").exists() == ("mirror" not in mode)
    assert (destination.parent / "unrelated.txt").read_text() == "outside destination"
    assert snapshot(origin) == original
    unchanged = await execute(service, value)
    assert unchanged["progress"]["transfers"] == 0


@pytest.mark.parametrize("mode", ["download", "download_mirror"])
async def test_download_preview_never_creates_missing_local_directory(mirror, mode):
    service, local, remote = mirror
    cloud_path = remote / "Mirrors" / "files"
    cloud_path.mkdir(parents=True)
    (cloud_path / "data.txt").write_text("cloud data")
    destination = local / "not-created" / "downloads"
    value = task(service, destination, mode=mode)
    assert not destination.parent.exists()
    preview = await service.preview(
        {
            "mode": mode,
            "id": value["id"],
            "local_path": str(destination),
            "remote_path": "files",
            "exclude": [],
        }
    )
    assert preview["counts"]["add"] == 1
    assert not destination.parent.exists()
    await execute(service, value)
    assert (destination / "data.txt").read_text() == "cloud data"


@pytest.mark.parametrize("mode", ["download", "download_mirror"])
async def test_missing_cloud_source_fails_but_existing_empty_source_is_explicit(
    mirror, mode
):
    from astrbot_plugin_rclone.files import SyncError

    service, local, remote = mirror
    (remote / "Mirrors").mkdir()
    destination = local / "downloads"
    destination.mkdir()
    (destination / "important.txt").write_text("important local data")
    value = task(service, destination, mode=mode)
    run = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 10)
    assert run["status"] == "failed" and "云端来源目录不存在" in run["error"]
    assert (destination / "important.txt").read_text() == "important local data"
    with pytest.raises(SyncError, match="云端来源目录不存在"):
        await service.preview(
            {
                "mode": mode,
                "id": value["id"],
                "local_path": str(destination),
                "remote_path": "files",
                "exclude": [],
            }
        )
    (remote / "Mirrors" / "files").mkdir()
    await execute(service, value)
    assert (destination / "important.txt").exists() == (mode == "download")


@pytest.mark.parametrize("mode", ["download", "download_mirror"])
async def test_download_exclusions_protect_private_config_and_local_extras(
    mirror, mode
):
    service, local, remote = mirror
    cloud_path = remote / "Mirrors" / "files"
    cloud_path.mkdir(parents=True)
    private_relative = service.directory.relative_to(local)
    malicious = cloud_path / private_relative
    malicious.mkdir(parents=True)
    (malicious / "rclone.conf").write_text("do not overwrite private config")
    (cloud_path / "visible.txt").write_text("download this")
    (cloud_path / "ignored.tmp").write_text("cloud ignored")
    (local / "ignored.tmp").write_text("keep local ignored")
    secret = service.config_path.read_bytes()
    value = task(service, local, mode=mode, exclude=["*.tmp"])
    await execute(service, value)
    assert service.config_path.read_bytes() == secret
    assert (local / "visible.txt").read_text() == "download this"
    assert (local / "ignored.tmp").read_text() == "keep local ignored"
    assert (cloud_path / "ignored.tmp").read_text() == "cloud ignored"


async def test_failed_download_transfer_does_not_delete_local_extras(
    mirror, monkeypatch
):
    service, local, remote = mirror
    cloud_path = remote / "Mirrors" / "files"
    cloud_path.mkdir(parents=True)
    (cloud_path / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    destination = local / "downloads"
    destination.mkdir()
    (destination / "extra.txt").write_text("retain on transfer failure")
    value = task(service, destination, mode="download_mirror")
    original = service.cloud._run

    async def restricted(*args, **kwargs):
        if args[0] == "sync":
            args += ("--max-transfer", "1B", "--cutoff-mode", "HARD")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service.cloud, "_run", restricted)
    run = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 20)
    assert run["status"] == "failed"
    assert (destination / "extra.txt").read_text() == "retain on transfer failure"
