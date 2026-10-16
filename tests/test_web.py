import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from starlette.requests import Request

SECRET = "onedrive-test-signing-secret-32-bytes"


@pytest.fixture
async def page_plugin(tmp_path):
    from astrbot.core.star.context import Context
    from astrbot_plugin_rclone.main import RclonePlugin
    from astrbot_plugin_rclone.service import SyncService

    context = Context.__new__(Context)
    context.registered_web_apis = []
    context.get_config = lambda: {"dashboard": {"jwt_secret": SECRET}}
    plugin = RclonePlugin.__new__(RclonePlugin)
    plugin.context = context
    plugin.sync = SyncService(tmp_path, tmp_path / "private")
    plugin.handlers = []
    plugin.nonces = {}
    plugin.closed = False
    plugin.logger = logging.getLogger("onedrive-tests")
    await plugin.initialize()
    yield plugin
    await plugin.terminate()


async def call(plugin, action, *, username="admin", payload=None, token_fields=None):
    from astrbot.api.web import PluginRequest, bind_request_context

    token = jwt.encode(
        {"exp": time.time() + 600, "username": username, **(token_fields or {})},
        SECRET,
        algorithm="HS256",
    )
    body = json.dumps(payload).encode() if payload is not None else b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST" if payload is not None else "GET",
            "path": "/api/v1/plugins/extensions/astrbot_plugin_rclone/" + action,
            "headers": [
                (b"authorization", ("Bearer " + token).encode()),
                (b"content-type", b"application/json"),
            ],
            "query_string": b"",
            "server": ("test", 80),
            "scheme": "http",
        },
        receive,
    )
    with bind_request_context(
        PluginRequest(request, username=username, plugin_name="astrbot_plugin_rclone")
    ):
        response = await plugin.dispatch(action)
    return response.status_code, json.loads(response.body)


async def test_native_routes_require_dashboard_jwt_and_csrf(page_plugin):
    code, state = await call(page_plugin, "state")
    assert code == 200 and state["csrf"]
    assert "tokens" not in state
    assert len(page_plugin.context.registered_web_apis) == 18
    code, result = await call(
        page_plugin,
        "connection",
        payload={"binary": "rclone", "remote": "onedrive", "folder": "AstrBotBackups"},
    )
    assert code == 403
    code, result = await call(
        page_plugin,
        "connection",
        payload={
            "csrf": state["csrf"],
            "binary": "rclone",
            "remote": "onedrive",
            "folder": "AstrBotBackups",
        },
    )
    assert code == 200
    assert page_plugin.sync.data["connection"]["remote"] == "onedrive"


async def test_asset_tokens_api_keys_and_wrong_user_are_rejected(page_plugin):
    from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

    code, _ = await call(
        page_plugin, "state", token_fields={"token_type": "plugin_page_asset"}
    )
    assert code == 403
    code, _ = await call(
        page_plugin, "state", username=API_KEY_USERNAME_PREFIX + "test"
    )
    assert code == 403
    code, _ = await call(page_plugin, "state", token_fields={"username": "different"})
    assert code == 403


async def test_unknown_fields_are_not_ignored(page_plugin):
    _, state = await call(page_plugin, "state")
    code, result = await call(
        page_plugin,
        "connection",
        payload={
            "csrf": state["csrf"],
            "binary": "rclone",
            "remote": "onedrive",
            "folder": "AstrBotBackups",
            "client_id": "unsupported",
        },
    )
    assert code == 400 and "未知字段" in result["message"]


async def test_page_is_discovered_by_real_astrbot_page_service():
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.dashboard.services.plugin_page_service import PluginPageService

    directory = Path(__file__).resolve().parents[1]
    metadata = PluginManager._load_plugin_metadata(str(directory))
    metadata.root_dir_name = directory.name
    service = PluginPageService(
        SimpleNamespace(plugin_store_path=str(directory.parent))
    )
    pages = await service.discover_plugin_pages(metadata)
    assert [page.name for page in pages] == ["sync"]
    assert metadata.i18n["zh-CN"]["pages"]["sync"]["title"] == "云端同步"


@pytest.mark.parametrize(
    "action",
    [
        "config-fields",
        "config-begin",
        "config-answer",
        "config-finish",
        "gui-start",
        "config-reconnect",
        "install-rclone",
    ],
)
async def test_configuration_mutations_require_csrf_before_starting_processes(
    page_plugin, action
):
    await call(page_plugin, "state")
    code, _ = await call(page_plugin, action, payload={})
    assert code == 403
    assert not page_plugin.sync.configuration.active
    assert not page_plugin.sync.installer.active


@pytest.mark.parametrize("action", ["config-reconnect", "install-rclone"])
async def test_setup_actions_reject_arbitrary_commands(page_plugin, action):
    _, state = await call(page_plugin, "state")
    code, result = await call(
        page_plugin, action, payload={"csrf": state["csrf"], "command": "whoami"}
    )
    assert code == 400 and "未知字段" in result["message"]
    assert not page_plugin.sync.installer.active
    assert not page_plugin.sync.configuration.active


@pytest.mark.parametrize("action", ["providers", "remotes"])
async def test_configuration_reads_reject_asset_tokens(page_plugin, action):
    code, _ = await call(
        page_plugin, action, token_fields={"token_type": "plugin_page_asset"}
    )
    assert code == 403


@pytest.mark.parametrize(
    "mode", ["upload", "upload_mirror", "download", "download_mirror"]
)
async def test_task_api_requires_and_returns_explicit_modes(page_plugin, mode):
    source = page_plugin.sync.root / "files"
    source.mkdir()
    _, state = await call(page_plugin, "state")
    payload = {
        "csrf": state["csrf"],
        "name": "files",
        "mode": mode,
        "local_path": str(source),
        "remote_path": "files",
        "exclude": [],
        "enabled": False,
        "interval_minutes": 0,
    }
    code, result = await call(page_plugin, "save-task", payload=payload)
    assert code == 200, result
    _, saved = await call(page_plugin, "state")
    assert saved["tasks"][0]["mode"] == mode
    assert saved["tasks"][0]["local_path"] == str(source)
    assert "source" not in saved["tasks"][0]
    del payload["mode"]
    code, result = await call(page_plugin, "save-task", payload=payload)
    assert code == 400 and "缺少字段 mode" in result["message"]
    assert len(page_plugin.sync.data["tasks"]) == 1
