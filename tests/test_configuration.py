"""Exercise real rclone configuration, isolated RC services and local transfers."""

import asyncio
import json
import shutil
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from astrbot_plugin_rclone.configuration import Configuration
from astrbot_plugin_rclone.files import SyncError
from astrbot_plugin_rclone.rclone import Rclone
from astrbot_plugin_rclone.service import SyncService


@pytest.fixture
def binary():
    bundled = Path(__file__).parent / "tools" / "rclone.exe"
    value = str(bundled.resolve()) if bundled.is_file() else shutil.which("rclone")
    if not value:
        pytest.skip("Real rclone binary required for integration checks")
    return value


@pytest.fixture
async def configuration(tmp_path, binary):
    private = tmp_path / "private"
    private.mkdir()
    cloud = Rclone(
        private / "rclone.conf",
        {"binary": binary, "remote": "backup", "folder": "AstrBotBackups"},
    )
    config = Configuration(cloud, private)
    yield config
    await config.close()
    await cloud.close()


async def test_real_dynamic_metadata_and_transactional_save_cancel(
    configuration, tmp_path
):
    config = configuration
    assert {p["Name"] for p in await config.providers()} >= {
        "onedrive",
        "s3",
        "webdav",
        "alias",
    }
    assert not config.cloud.config_path.exists()
    created = await config.begin(
        "admin",
        {
            "name": "backup",
            "backend": "alias",
            "parameters": {"remote": str(tmp_path / "cloud")},
            "edit": False,
        },
    )
    assert created["complete"]
    assert not config.cloud.config_path.exists()
    assert config.status("another").get("session_id") is None
    with pytest.raises(SyncError, match="其他用户"):
        await config.finish(
            "another", {"session_id": created["session_id"], "save": True}
        )
    await config.finish("admin", {"session_id": created["session_id"], "save": True})
    before = config.cloud.config_path.read_bytes()
    edited = await config.begin(
        "admin",
        {
            "name": "backup",
            "backend": "alias",
            "parameters": {"remote": str(tmp_path / "elsewhere")},
            "edit": True,
        },
    )
    await config.finish("admin", {"session_id": edited["session_id"], "save": False})
    assert config.cloud.config_path.read_bytes() == before
    assert not config.staging.exists()
    assert await config.remotes() == [{"name": "backup", "type": "alias"}]


async def test_passwords_never_reappear_in_fields_or_command_line(
    configuration, monkeypatch
):
    config = configuration
    calls = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs.get("env", {})))
        return await original(*args, **kwargs)

    monkeypatch.setenv("RCLONE_RC_NO_AUTH", "true")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await config.begin(
        "admin",
        {
            "name": "dav",
            "backend": "webdav",
            "parameters": {
                "url": "https://example.invalid",
                "vendor": "other",
                "user": "private-user",
                "pass": "private-password",
            },
            "edit": False,
        },
    )
    await config.finish("admin", {"session_id": result["session_id"], "save": True})
    fields = await config.fields("webdav", "dav")
    assert "private-password" not in json.dumps(fields)
    assert "private-user" not in json.dumps(fields)
    assert {"user", "pass"} <= set(fields["configured"])
    assert "private-password" not in config.cloud.config_path.read_text()
    assert all("private-password" not in str(args) for args, _ in calls)
    assert all(not env.get("RCLONE_RC_NO_AUTH") for args, env in calls if "rcd" in args)


async def test_onedrive_questions_are_driven_by_rclone_and_cancel_reaps_process(
    configuration,
):
    config = configuration
    result = await config.begin(
        "admin", {"name": "ms", "backend": "onedrive", "parameters": {}, "edit": False}
    )
    assert not result["complete"]
    assert result["option"]["Name"] == "config_token"
    assert result["option"]["IsPassword"] and not result["option"]["Default"]
    assert 'rclone authorize "onedrive"' in result["option"]["Help"]
    process = config.process.process
    with pytest.raises(SyncError, match="所有配置步骤"):
        await config.finish("admin", {"session_id": result["session_id"], "save": True})
    await config.close()
    assert process.returncode is not None
    assert not config.cloud.config_path.exists() and not config.staging.exists()


async def test_reconnect_replaces_existing_oauth_token_only_after_save(configuration):
    config = configuration
    original = (
        '[ms]\ntype = onedrive\ntoken = {"access_token":"private-old-token"}\n'
        "drive_id = existing-drive\ndrive_type = personal\n"
        "[other]\ntype = alias\nremote = /unchanged\n"
    ).encode()
    config.cloud.config_path.write_bytes(original)
    result = await config.reconnect("admin", "ms")
    assert not result["complete"]
    assert result["option"]["Name"] == "config_token"
    assert "private-old-token" not in json.dumps(result)
    assert 'rclone authorize "onedrive"' in result["option"]["Help"]
    with pytest.raises(SyncError, match="其他用户"):
        await config.answer(
            "another", {"session_id": result["session_id"], "answer": ""}
        )
    invalid = await config.answer(
        "admin", {"session_id": result["session_id"], "answer": "invalid-token"}
    )
    assert invalid["error"] and invalid["option"]["Name"] == "config_token"
    process = config.process.process
    await config.finish("admin", {"session_id": result["session_id"], "save": False})
    assert config.cloud.config_path.read_bytes() == original
    assert process.returncode is not None and not config.staging.exists()

    # A backend without an OAuth step still uses the same transactional save.
    result = await config.reconnect("admin", "other")
    assert result["complete"]
    await config.process.rpc(
        "config/update",
        {
            "name": "other",
            "parameters": {"remote": "/changed"},
            "opt": {"nonInteractive": True},
        },
    )
    assert config.cloud.config_path.read_bytes() == original
    await config.finish("admin", {"session_id": result["session_id"], "save": True})
    saved = config.cloud.config_path.read_text()
    assert "/changed" in saved and "private-old-token" in saved


async def test_reconnect_missing_remote_does_not_start_a_session(configuration):
    with pytest.raises(SyncError, match="尚未配置"):
        await configuration.reconnect("admin", "missing")
    assert not configuration.active and not configuration.staging.exists()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_official_gui_authenticates_uses_shared_config_and_stops(configuration):
    config = configuration
    gui_port, api_port = free_port(), free_port()
    while gui_port == api_port:
        api_port = free_port()
    result = await config.gui(
        "admin",
        {
            "listen": "127.0.0.1",
            "gui_port": gui_port,
            "api_port": api_port,
            "gui_url": f"http://127.0.0.1:{gui_port}",
            "api_url": f"http://127.0.0.1:{api_port}",
        },
    )
    query = parse_qs(urlsplit(result["login_url"]).query)
    process = config.process.process
    assert query["pass"][0] == config.process.password
    assert "login_url" not in config.status("admin") and "pass" not in str(
        config.status("another")
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.get(f"http://127.0.0.1:{gui_port}/")).status_code == 200
        assert (
            await client.post(query["url"][0] + "config/listremotes", json={})
        ).status_code == 401
    await config.process.rpc(
        "config/create",
        {
            "name": "fromgui",
            "type": "local",
            "parameters": {},
            "opt": {"nonInteractive": True},
        },
    )
    await config.finish("admin", {"session_id": result["session_id"], "save": True})
    assert process.returncode is not None
    assert await config.remotes() == [{"name": "fromgui", "type": "local"}]


async def test_real_full_backup_and_configuration_exclude_each_other(tmp_path, binary):
    service = SyncService(tmp_path, tmp_path / "private")
    service.cloud.configure(
        {"binary": binary, "remote": "backup", "folder": "AstrBotBackups"}
    )
    service.data["connection"].update(binary=binary)
    result = await service.begin_configuration(
        "admin",
        {
            "name": "backup",
            "backend": "alias",
            "parameters": {"remote": str(tmp_path / "cloud")},
            "edit": False,
        },
    )
    source = tmp_path / "source.txt"
    source.write_text("important data", encoding="utf-8")
    task = service.save_task(
        {
            "name": "files",
            "mode": "upload_mirror",
            "local_path": str(source),
            "remote_path": "files",
            "exclude": [],
            "enabled": True,
            "interval_minutes": 0,
        }
    )
    with pytest.raises(SyncError, match="配置期间"):
        service.enqueue(task["id"])
    with pytest.raises(SyncError, match="正在配置"):
        await service.connect()
    await service.configuration.finish(
        "admin", {"session_id": result["session_id"], "save": True}
    )
    (tmp_path / "cloud").mkdir()
    await service.initialize()
    try:
        await service.connect()
        run = service.enqueue(task["id"])
        with pytest.raises(SyncError, match="同步或预览"):
            await service.begin_configuration(
                "admin",
                {
                    "name": "elsewhere",
                    "backend": "local",
                    "parameters": {},
                    "edit": False,
                },
            )
        await asyncio.wait_for(service.queue.join(), 30)
        assert run["status"] == "completed", run
        assert (
            tmp_path / "cloud" / "AstrBotBackups" / "files" / source.name
        ).read_bytes() == source.read_bytes()
    finally:
        await service.close()


@pytest.mark.parametrize("parameters", [{"legacy_key": "old"}, {"remote": 123}])
async def test_unknown_or_invalid_config_parameters_fail_before_changes(
    configuration, parameters
):
    with pytest.raises(SyncError):
        await configuration.begin(
            "admin",
            {
                "name": "backup",
                "backend": "alias",
                "parameters": parameters,
                "edit": False,
            },
        )
    assert not configuration.active
    assert not configuration.cloud.config_path.exists()
