import json
from pathlib import Path

import pytest


class Event:
    def plain_result(self, text):
        return text


def make_plugin(tmp_path, *, model_tools=False):
    from astrbot_plugin_rclone.main import RclonePlugin
    from astrbot_plugin_rclone.service import SyncService

    root = Path(tmp_path)
    private = root / "private"
    source = root / "source"
    source.mkdir()
    plugin = object.__new__(RclonePlugin)
    plugin.sync = SyncService(root, private)
    plugin.model_tools_enabled = model_tools
    return plugin, source


async def collect(handler, event, *args):
    return [item async for item in handler(event, *args)]


@pytest.mark.asyncio
async def test_chat_commands_create_edit_and_delete(tmp_path):
    plugin, source = make_plugin(tmp_path)
    event = Event()
    task_payload = {
        "name": "command task",
        "mode": "upload",
        "local_path": str(source),
        "remote_path": "command-task",
        "exclude": [],
        "enabled": False,
        "interval_minutes": 0,
    }

    created = await collect(
        plugin.rclone_create, event, json.dumps(task_payload, ensure_ascii=False)
    )
    assert "已创建同步任务" in created[0]
    task_id = next(iter(plugin.sync.data["tasks"]))

    edited = await collect(
        plugin.rclone_edit,
        event,
        f"{task_id} {json.dumps({'interval_minutes': 10})}",
    )
    assert "已更新同步任务" in edited[0]
    assert plugin.sync.data["tasks"][task_id]["interval_minutes"] == 10

    rejected = await collect(plugin.rclone_delete, event, task_id)
    assert "confirm" in rejected[0]
    deleted = await collect(plugin.rclone_delete, event, task_id + " confirm")
    assert "已删除同步任务" in deleted[0]
    assert not plugin.sync.data["tasks"]


@pytest.mark.asyncio
async def test_mirror_run_requires_confirmation(tmp_path):
    plugin, source = make_plugin(tmp_path)
    event = Event()
    task = plugin.sync.save_task(
        {
            "name": "mirror task",
            "mode": "upload_mirror",
            "local_path": str(source),
            "remote_path": "mirror-task",
            "exclude": [],
            "enabled": False,
            "interval_minutes": 0,
        }
    )
    plugin.sync.cloud.require_connected = lambda: None

    rejected = await collect(plugin.rclone_run, event, task["id"])
    assert "confirm" in rejected[0]
    queued = await collect(plugin.rclone_run, event, task["id"] + " confirm")
    assert "已排队执行" in queued[0]


@pytest.mark.asyncio
async def test_model_tools_are_guarded_and_manage_tasks(tmp_path):
    plugin, source = make_plugin(tmp_path, model_tools=False)
    event = Event()
    disabled = json.loads(await plugin.rclone_list_tasks(event))
    assert disabled == {
        "ok": False,
        "error": "模型工具已关闭，请在插件配置中启用 enable_model_tools。",
    }

    plugin.model_tools_enabled = True
    task = {
        "name": "model task",
        "mode": "upload",
        "local_path": str(source),
        "remote_path": "model-task",
        "exclude": [],
        "enabled": False,
        "interval_minutes": 0,
    }
    created = json.loads(await plugin.rclone_create_task(event, task))
    assert created["ok"]
    task_id = created["result"]["id"]
    updated = json.loads(
        await plugin.rclone_update_task(event, task_id, {"interval_minutes": 15})
    )
    assert updated["ok"] and updated["result"]["interval_minutes"] == 15
    rejected = json.loads(await plugin.rclone_delete_task(event, task_id, False))
    assert not rejected["ok"] and "confirm=true" in rejected["error"]
    deleted = json.loads(await plugin.rclone_delete_task(event, task_id, True))
    assert deleted["ok"] and deleted["result"]["files_preserved"] is True
