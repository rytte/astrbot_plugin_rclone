import asyncio
import json
from uuid import uuid4

import pytest

from astrbot_plugin_rclone.files import SyncError, write_json
from astrbot_plugin_rclone.rclone import Rclone
from astrbot_plugin_rclone.service import SyncService


class FakeCloud(Rclone):
    def __init__(self, config):
        super().__init__(config)
        self.connected = True
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def transfer(self, source, target, mode, excludes, **kwargs):
        self.calls.append((source, target, mode, excludes))
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise SyncError("模拟同步失败")
        if kwargs.get("progress"):
            kwargs["progress"](
                {
                    "bytes": 10,
                    "totalBytes": 10,
                    "transfers": 1,
                    "totalTransfers": 1,
                    "deletes": 2,
                }
            )
        return {}


def create_task(service, source, **overrides):
    return service.save_task(
        {
            "name": "test",
            "mode": "upload_mirror",
            "local_path": str(source),
            "remote_path": "files",
            "exclude": [],
            "enabled": True,
            "interval_minutes": 0,
            **overrides,
        }
    )


@pytest.fixture
async def service(tmp_path):
    cloud = FakeCloud(tmp_path / "private" / "rclone.conf")
    service = SyncService(tmp_path, tmp_path / "private", cloud)
    await service.initialize()
    yield service
    await service.close()


async def test_failed_sync_is_reported_and_worker_continues(service, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("important")
    task = create_task(service, source)
    service.cloud.fail = True
    run = service.enqueue(task["id"])
    with pytest.raises(SyncError, match="已经排队"):
        service.enqueue(task["id"])
    await asyncio.wait_for(service.queue.join(), 5)
    assert run["status"] == "failed" and "模拟同步失败" in run["error"]
    assert source.read_text() == "important" and not list(service.work.iterdir())
    service.cloud.fail = False
    following = service.enqueue(task["id"])
    await asyncio.wait_for(service.queue.join(), 5)
    assert following["status"] == "completed" and following["progress"]["deletes"] == 2


async def test_installation_excludes_other_operations_and_saves_program_path(
    service, tmp_path, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()
    binary = str(tmp_path / "private" / "bin" / "rclone.exe")

    async def install():
        started.set()
        await release.wait()
        return binary, "rclone v1.75.2"

    monkeypatch.setattr(service.installer, "install", install)
    source = tmp_path / "files"
    source.mkdir()
    task = create_task(service, source)
    service.install_rclone()
    with pytest.raises(SyncError, match="正在安装"):
        service.install_rclone()
    with pytest.raises(SyncError, match="正在安装"):
        service.enqueue(task["id"])
    await started.wait()
    assert service.busy and not service.cloud.connected
    with pytest.raises(SyncError, match="正在安装"):
        await service.begin_configuration("admin", {}, reconnect=True)
    release.set()
    await service.install_task
    assert service.state()["installation"]["status"] == "completed"
    assert service.cloud.settings["binary"] == binary
    assert not service.busy and not service.cloud.connected
    saved = json.loads(service.state_path.read_text())
    assert saved["connection"]["binary"] == binary
    assert not saved["connection"]["enabled"]


async def test_stopping_plugin_cancels_installation(service, monkeypatch):
    started = asyncio.Event()

    async def install():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.installer, "install", install)
    service.install_rclone()
    await started.wait()
    await service.close()
    assert service.install_task.done()
    assert service.installer.state["status"] == "cancelled"


async def test_failed_download_does_not_change_the_configured_program(
    service, monkeypatch
):
    before = dict(service.cloud.settings)

    async def install():
        raise SyncError("SHA-256 校验失败")

    monkeypatch.setattr(service.installer, "install", install)
    service.install_rclone()
    await service.install_task
    assert service.installer.state["status"] == "failed"
    assert "SHA-256" in service.installer.state["error"]
    assert service.cloud.settings == before
    assert not service.busy and not service.cloud.connected


async def test_running_and_queued_cancellation_keep_worker_available(service, tmp_path):
    source = tmp_path / "local_path"
    source.mkdir()
    first = create_task(service, source)
    second = create_task(service, source, remote_path="other")
    service.cloud.release.clear()
    first_run = service.enqueue(first["id"])
    await asyncio.wait_for(service.cloud.started.wait(), 3)
    second_run = service.enqueue(second["id"])
    await service.cancel(second["id"])
    await service.cancel(first["id"])
    await asyncio.wait_for(service.queue.join(), 3)
    assert first_run["status"] == second_run["status"] == "cancelled"
    assert len(service.cloud.calls) == 1
    service.cloud.release.set()
    following = service.enqueue(first["id"])
    await asyncio.wait_for(service.queue.join(), 3)
    assert following["status"] == "completed"


async def test_missing_source_does_not_invoke_mirror(service, tmp_path):
    source = tmp_path / "local_path"
    source.mkdir()
    task = create_task(service, source)
    source.rmdir()
    run = service.enqueue(task["id"])
    await asyncio.wait_for(service.queue.join(), 3)
    assert run["status"] == "failed" and "不存在或无法读取" in run["error"]
    assert not service.cloud.calls


async def test_cancellation_before_execution_starts_does_not_run_mirror(
    service, tmp_path
):
    source = tmp_path / "local_path"
    source.mkdir()
    task = create_task(service, source)
    run = service.enqueue(task["id"])
    await asyncio.sleep(0)
    await service.cancel(task["id"])
    await asyncio.wait_for(service.queue.join(), 3)
    assert run["status"] == "cancelled" and not service.cloud.calls


@pytest.mark.parametrize("remote_path", ["files", "FILES", "files/child"])
async def test_tasks_cannot_share_or_nest_cloud_targets(service, tmp_path, remote_path):
    source = tmp_path / "local_path"
    source.mkdir()
    create_task(service, source)
    with pytest.raises(SyncError, match="独立的云端子目录"):
        create_task(service, source, remote_path=remote_path)
    assert len(service.data["tasks"]) == 1


async def test_schedule_and_run_state_survive_restart(service, tmp_path):
    source = tmp_path / "local_path"
    source.mkdir()
    task = create_task(service, source, interval_minutes=1440)
    service.enqueue(task["id"])
    restarted = SyncService(tmp_path, service.directory, service.cloud)
    assert restarted.data["version"] == 1 and restarted.data["mode"] == "transfer"
    assert (
        restarted.task(task["id"])["next_run"] == service.task(task["id"])["next_run"]
    )
    assert restarted.data["runs"][0]["status"] == "interrupted"


async def test_old_versioned_state_fails_without_modification(tmp_path):
    private = tmp_path / "private"
    old = {"version": 1, "connection": {}, "tasks": {}, "cloud_tasks": {}, "runs": []}
    write_json(private / "state.json", old)
    with pytest.raises(SyncError, match="四模式同步格式"):
        SyncService(tmp_path, private)
    assert json.loads((private / "state.json").read_text()) == old


async def test_workspace_cleanup_rejects_unrelated_user_directories(service, tmp_path):
    source = tmp_path / uuid4().hex
    source.mkdir()
    (source / "data").write_text("keep")
    with pytest.raises(SyncError, match="拒绝递归"):
        service.remove_workspace(source)
    assert (source / "data").read_text() == "keep"


async def test_active_sync_blocks_connection_configuration(service, tmp_path):
    source = tmp_path / "local_path"
    source.mkdir()
    task = create_task(service, source)
    service.cloud.release.clear()
    service.enqueue(task["id"])
    await asyncio.wait_for(service.cloud.started.wait(), 3)
    with pytest.raises(SyncError, match="同步或预览"):
        await service.begin_configuration("admin", {})
    await service.close()
    assert service.data["runs"][-1]["status"] == "interrupted"


@pytest.mark.parametrize(
    "mode", ["upload", "upload_mirror", "download", "download_mirror"]
)
async def test_all_modes_are_persisted_dispatched_and_cancellable(
    service, tmp_path, mode
):
    path = tmp_path / "files"
    path.mkdir()
    value = create_task(service, path, mode=mode)
    service.cloud.release.clear()
    run = service.enqueue(value["id"])
    await asyncio.wait_for(service.cloud.started.wait(), 3)
    assert service.cloud.calls[0][2] == mode and run["mode"] == mode
    await service.cancel(value["id"])
    await asyncio.wait_for(service.queue.join(), 3)
    assert run["status"] == "cancelled"
    restarted = SyncService(tmp_path, service.directory, service.cloud)
    assert restarted.task(value["id"])["mode"] == mode
    service.cloud.release.set()
    following = service.enqueue(value["id"])
    await asyncio.wait_for(service.queue.join(), 3)
    assert following["status"] == "completed"


async def test_download_tasks_can_read_same_cloud_directory_but_not_share_local_targets(
    service, tmp_path
):
    first = create_task(service, tmp_path / "one", mode="download")
    create_task(service, tmp_path / "two", mode="download_mirror")
    for path in (tmp_path / "one", tmp_path / "one" / "child", tmp_path):
        with pytest.raises(SyncError, match="接收目录不能"):
            create_task(service, path, mode="download", remote_path="another")
    first_local = tmp_path / "upload"
    first_local.mkdir()
    create_task(service, first_local, mode="upload")
    with pytest.raises(SyncError, match="接收目录不能"):
        create_task(service, first_local / "child", mode="download")
    assert service.task(first["id"])["mode"] == "download"


async def test_previous_mirror_state_fails_without_modification(tmp_path):
    private = tmp_path / "private"
    old = {"version": 1, "mode": "mirror", "connection": {}, "tasks": {}, "runs": []}
    write_json(private / "state.json", old)
    with pytest.raises(SyncError, match="四模式同步格式"):
        SyncService(tmp_path, private)
    assert json.loads((private / "state.json").read_text()) == old
