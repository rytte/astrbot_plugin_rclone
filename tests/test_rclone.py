"""Backend-independent rclone transfers and process isolation."""

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from astrbot_plugin_rclone.files import SyncError, write_json
from astrbot_plugin_rclone.rclone import (
    DEFAULT_CONNECTION,
    Rclone,
    validate_connection,
)
from astrbot_plugin_rclone.service import SyncService


async def test_old_direct_oauth_state_is_rejected_without_rewriting_it(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    old = {"version": 1, "client_id": "old", "tasks": {}, "cloud_tasks": {}, "runs": []}
    path = private / "state.json"
    write_json(path, old)
    with pytest.raises(SyncError, match="四模式同步格式"):
        SyncService(tmp_path, private)
    assert json.loads(path.read_text(encoding="utf-8")) == old


@pytest.fixture
def cloud_disk(tmp_path, monkeypatch):
    config = tmp_path / "private" / "rclone.conf"
    config.parent.mkdir()
    config.write_text("private-token", encoding="utf-8")
    disk = tmp_path / "cloud"
    disk.mkdir()
    cloud = Rclone(config)
    calls = []
    remote = {
        "type": "onedrive",
        "drive_type": "personal",
        "drive_id": "drive",
        "token": "private-token",
    }

    def path(value):
        assert value.startswith(cloud.settings["remote"] + ":")
        result = disk / value.split(":", 1)[1]
        assert result.resolve().is_relative_to(disk.resolve())
        return result

    def item(value):
        return {
            "Name": value.name,
            "IsDir": value.is_dir(),
            "Size": -1 if value.is_dir() else value.stat().st_size,
        }

    async def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "version":
            return b"rclone v1.75.2\nos/version: test\n"
        if args[:2] == ("config", "dump"):
            return json.dumps({cloud.settings["remote"]: remote}).encode()
        if args[:2] == ("backend", "features"):
            return json.dumps(
                {"Features": {"BucketBased": remote["type"] == "s3"}}
            ).encode()
        if args[0] == "lsjson":
            target = path(args[1])
            if not target.exists():
                if kwargs.get("missing"):
                    return None
                raise SyncError("directory missing")
            if "--stat" in args:
                return json.dumps(item(target)).encode()
            return json.dumps([item(child) for child in target.iterdir()]).encode()
        if args[0] == "mkdir":
            path(args[1]).mkdir(parents=True, exist_ok=True)
        elif args[0] == "copyto":
            source = (
                path(args[1])
                if ":" in args[1] and args[1].startswith(cloud.settings["remote"] + ":")
                else Path(args[1])
            )
            target = (
                path(args[2])
                if args[2].startswith(cloud.settings["remote"] + ":")
                else Path(args[2])
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        elif args[0] == "cat":
            return path(args[1]).read_bytes()
        elif args[0] == "purge":
            shutil.rmtree(path(args[1]))
        else:
            raise AssertionError(args)
        return b""

    monkeypatch.setattr(cloud, "_run", run)
    return cloud, disk, remote, calls


async def test_connection_is_read_only_and_does_not_expose_config_tokens(cloud_disk):
    cloud, disk, _, calls = cloud_disk
    result = await cloud.check_connection()
    assert result["connected"] and "private-token" not in str(result)
    assert not list(disk.iterdir())
    assert [call[0] for call in calls] == ["version", "config", "backend", "lsjson"]


async def test_connection_and_disconnect_persist_across_restart(tmp_path, cloud_disk):
    cloud, _, _, _ = cloud_disk
    service = SyncService(tmp_path, tmp_path / "private", cloud)
    await service.save_connection(DEFAULT_CONNECTION)
    await service.connect()
    assert json.loads(service.state_path.read_text(encoding="utf-8"))["version"] == 1
    assert service.data["connection"]["enabled"]
    await service.close()
    # Simulated remote reuses the configured backend, as a new rclone process would.
    cloud.closed = False
    restarted = SyncService(tmp_path, tmp_path / "private", cloud)
    await restarted.initialize()
    try:
        assert restarted.data["version"] == 1
        assert restarted.state()["cloud"]["connected"]
        await restarted.disconnect()
        assert not restarted.data["connection"]["enabled"]
        assert restarted.config_path.read_text(encoding="utf-8") == "private-token"
        assert not restarted.state()["cloud"]["connected"]
    finally:
        await restarted.close()
    paused = SyncService(tmp_path, tmp_path / "private", cloud)
    assert not paused.data["connection"]["enabled"]


async def test_failed_connection_keeps_page_available_and_stops_scheduling(
    tmp_path, cloud_disk
):
    cloud, _, remote, _ = cloud_disk
    remote["type"] = ""
    service = SyncService(tmp_path, tmp_path / "private", cloud)
    service.data["connection"]["enabled"] = True
    await service.initialize()
    try:
        assert not service.state()["cloud"]["connected"]
        assert "不存在" in service.state()["cloud"]["error"]
        assert service.queue.empty()
    finally:
        await service.close()


@pytest.mark.parametrize("backend", ["onedrive", "webdav", "local", "sftp"])
async def test_configured_backends_are_accepted_without_provider_specific_rules(
    cloud_disk, backend
):
    cloud, _, remote, calls = cloud_disk
    remote.clear()
    remote["type"] = backend
    await cloud.check_connection()
    assert cloud.status()["connected"]
    assert all(call[0] not in ("mkdir", "copyto", "purge") for call in calls)


async def test_s3_checks_only_selected_bucket_and_requires_a_prefix(cloud_disk):
    cloud, disk, remote, calls = cloud_disk
    remote["type"] = "s3"
    (disk / "mybucket").mkdir()
    cloud.configure({**DEFAULT_CONNECTION, "folder": "mybucket/AstrBotBackups"})
    await cloud.check_connection()
    assert calls[-1][1] == "backup:mybucket"
    assert not (disk / "mybucket" / "AstrBotBackups").exists()
    cloud.configure({**DEFAULT_CONNECTION, "folder": "mybucket"})
    with pytest.raises(SyncError, match="桶名/同步前缀"):
        await cloud.check_connection()


@pytest.mark.parametrize(
    "folder",
    [
        "",
        "/",
        "../other",
        "backup/../other",
        "other:root",
        "backup//run",
        "backup\\run",
        "backup/.",
        "backup/..",
        "backup/space ",
    ],
)
def test_remote_roots_reject_traversal_and_remote_injection(folder):
    with pytest.raises(SyncError):
        validate_connection({**DEFAULT_CONNECTION, "folder": folder})


def test_paths_cannot_escape_the_selected_root(tmp_path):
    cloud = Rclone(tmp_path / "rclone.conf")
    with pytest.raises(SyncError, match="超出"):
        cloud.remote_path("AstrBotBackups-other/file")
    with pytest.raises(SyncError):
        cloud.remote_path("AstrBotBackups/../elsewhere")
    with pytest.raises(SyncError, match="未知字段"):
        validate_connection({**DEFAULT_CONNECTION, "client_id": "old"})


async def test_subprocess_uses_explicit_config_and_ignores_rclone_environment(
    tmp_path, monkeypatch
):
    cloud = Rclone(tmp_path / "private config.conf")
    monkeypatch.setattr(cloud, "executable", lambda: sys.executable)
    monkeypatch.setenv("RCLONE_CONFIG_ONEDRIVE_TYPE", "local")
    code = "import sys,json,os;print(json.dumps({'args':sys.argv[1:],'env':[k for k in os.environ if k.startswith('RCLONE_')]}))"
    result = json.loads(await cloud._run("-c", code, "file;$(not-a-command)"))
    assert result["args"][0] == "file;$(not-a-command)"
    assert result["args"][result["args"].index("--config") + 1] == str(
        cloud.config_path
    )
    assert not result["env"]
    assert not cloud.processes


async def test_subprocess_errors_do_not_return_tokens_or_raw_stderr(
    tmp_path, monkeypatch
):
    cloud = Rclone(tmp_path / "rclone.conf")
    monkeypatch.setattr(cloud, "executable", lambda: sys.executable)
    with pytest.raises(SyncError) as caught:
        await cloud._run(
            "-c",
            "import sys;sys.stderr.write('refresh_token=private-secret');sys.exit(1)",
        )
    assert "private-secret" not in str(caught.value)


async def test_cancelled_subprocess_is_terminated_and_reaped(tmp_path, monkeypatch):
    cloud = Rclone(tmp_path / "rclone.conf")
    monkeypatch.setattr(cloud, "executable", lambda: sys.executable)
    task = asyncio.create_task(cloud._run("-c", "import time;time.sleep(30)"))
    async with asyncio.timeout(3):
        while not cloud.processes:
            await asyncio.sleep(0.01)
    process = next(iter(cloud.processes))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None and not cloud.processes


async def test_subprocess_timeout_is_terminated(tmp_path, monkeypatch):
    cloud = Rclone(tmp_path / "rclone.conf")
    monkeypatch.setattr(cloud, "executable", lambda: sys.executable)
    with pytest.raises(SyncError, match="超时"):
        await cloud._run("-c", "import time;time.sleep(30)", timeout=0.1)
    assert not cloud.processes
