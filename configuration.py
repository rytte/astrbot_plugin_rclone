"""Metadata-driven configuration and an on-demand official rclone GUI."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from .files import SyncError, check_keys
from .rclone import Rclone, validate_connection

SESSION_TIMEOUT = 15 * 60


def remote_name(value: str) -> str:
    return validate_connection(
        {"binary": "rclone", "remote": value, "folder": "backup"}
    )["remote"]


def public_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(c) < 32 for c in value)
    ):
        raise SyncError("GUI 和 API 访问地址必须是完整的 http:// 或 https:// 地址。")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SyncError("GUI 和 API 地址必须是无路径、账号及查询参数的 HTTP(S) 地址。")
    return value.rstrip("/")


class ConfigProcess:
    """One authenticated loopback RC process, optionally serving the official GUI."""

    def __init__(self, cloud: Rclone):
        self.cloud = cloud
        self.process = None
        self.reader = None
        self.endpoint = ""
        self.user = "astrbot"
        self.password = secrets.token_urlsafe(32)

    async def start(self, path: Path, gui: dict | None = None) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("RCLONE_")
        }
        environment.update(RCLONE_RC_USER=self.user, RCLONE_RC_PASS=self.password)
        args = ["rcd", "--rc-addr", "127.0.0.1:0"]
        if gui:
            args = [
                "gui",
                "--no-open-browser",
                "--addr",
                f"{gui['listen']}:{gui['gui_port']}",
                "--api-addr",
                f"{gui['listen']}:{gui['api_port']}",
            ]
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.cloud.executable(),
                *args,
                "--config",
                str(path),
                "--ask-password=false",
                "--log-level",
                "NOTICE",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                **options,
            )
        except OSError:
            raise SyncError(
                "无法启动 rclone 配置服务，请检查程序路径与权限。"
            ) from None
        ready = asyncio.get_running_loop().create_future()

        async def drain():
            total = 0
            try:
                while line := await self.process.stderr.readline():
                    total += len(line)
                    if total > 4 * 1024 * 1024:
                        break
                    # Do not retain or return logs: the GUI logs a login password.
                    text = line.decode("utf-8", errors="replace")
                    match = re.search(
                        r"Serving remote control on (http://[^\s]+)", text
                    )
                    if match:
                        self.endpoint = match[1].rstrip("/")
                    if (
                        self.endpoint
                        and (not gui or "GUI available at " in text)
                        and not ready.done()
                    ):
                        ready.set_result(None)
            finally:
                if not ready.done():
                    ready.set_exception(
                        SyncError(
                            "rclone 配置服务启动失败。请确认版本 ≥ 1.74、GUI 已打包且端口未被占用。"
                        )
                    )

        self.reader = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(ready, 20)
        except TimeoutError:
            await self.close()
            raise SyncError(
                "rclone 配置服务启动超时，请检查程序版本及监听端口。"
            ) from None
        except BaseException:
            await self.close()
            raise

    async def rpc(self, action: str, payload: dict) -> dict:
        if not self.process or self.process.returncode is not None:
            raise SyncError("rclone 配置服务已退出，请重新开始配置。")
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=90, auth=(self.user, self.password)
            ) as client:
                async with client.stream(
                    "POST", self.endpoint + "/" + action, json=payload
                ) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8 * 1024 * 1024:
                            raise SyncError("rclone 配置响应过大，已停止读取。")
                    if response.status_code != 200:
                        raise SyncError(
                            "rclone 未能完成配置步骤，请检查参数、网络或授权后重试。"
                        )
            result = Rclone.decode(bytes(data))
            if not isinstance(result, dict):
                raise SyncError("rclone 配置响应格式无效。")
            return result
        except httpx.HTTPError:
            raise SyncError("rclone 配置服务连接失败或超时，请重试。") from None

    async def close(self) -> None:
        if self.process:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.kill()
            await self.process.wait()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
        self.password = ""


class Configuration:
    def __init__(self, cloud: Rclone, directory: Path):
        self.cloud = cloud
        self.directory = directory
        self.staging = directory / "rclone.edit.conf"
        self.lock = asyncio.Lock()
        self.session = None
        self.process = None
        self.timer = None
        self.catalog = None
        self.catalog_binary = None

    @property
    def active(self) -> bool:
        return self.session is not None

    def status(self, owner: str | None = None) -> dict:
        if not self.session:
            return {"active": False}
        return {
            "active": True,
            "mode": self.session["mode"],
            "remote": self.session.get("remote", ""),
            "expires_at": self.session["touched"] + SESSION_TIMEOUT,
            **(
                {"session_id": self.session["id"]}
                if owner == self.session["owner"]
                else {}
            ),
        }

    def protect_config(self, path: Path) -> None:
        if path.is_symlink() or path.is_junction():
            raise SyncError("rclone 配置文件不能是符号链接或目录联接。")
        if path.exists():
            if not path.is_file():
                raise SyncError("rclone 配置路径必须是文件。")
            with path.open("rb") as stream:
                encrypted = stream.read(128).startswith(b"RCLONE_ENCRYPT_V0")
            if encrypted:
                raise SyncError("请先通过 rclone 移除配置文件加密，再使用页面配置。")
            path.chmod(0o600)

    async def providers(self) -> list[dict]:
        binary = self.cloud.executable()
        if self.catalog is None or binary != self.catalog_binary:
            await self.cloud.check_version()
            data = self.cloud.decode(
                await self.cloud._run("config", "providers", limit=16 * 1024 * 1024)
            )
            if not isinstance(data, list):
                raise SyncError("rclone 后端列表格式无效。")
            self.catalog, self.catalog_binary = data, binary
        return self.catalog

    async def remotes(self) -> list[dict]:
        self.protect_config(self.cloud.config_path)
        if not self.cloud.config_path.exists():
            return []
        data = self.cloud.decode(await self.cloud._run("config", "dump"))
        if not isinstance(data, dict):
            raise SyncError("rclone 连接列表格式无效。")
        return [
            {"name": name, "type": value.get("type", "")}
            for name, value in sorted(data.items())
            if isinstance(value, dict)
        ]

    async def fields(self, backend: str, name: str | None = None) -> dict:
        if not isinstance(backend, str) or (
            name is not None and not isinstance(name, str)
        ):
            raise SyncError("后端必须是文本，连接名称必须是文本或 null。")
        provider = next(
            (item for item in await self.providers() if item.get("Name") == backend),
            None,
        )
        if provider is None:
            raise SyncError("未知存储后端，请重新加载 rclone 后端列表。")
        values, configured = {}, []
        if name is not None:
            remote_name(name)
            self.protect_config(self.cloud.config_path)
            config = self.cloud.decode(await self.cloud._run("config", "dump"))
            entry = config.get(name)
            if not isinstance(entry, dict) or entry.get("type") != backend:
                raise SyncError("连接不存在或后端不匹配。")
            for option in provider.get("Options", []):
                key = option["Name"]
                if key in entry:
                    if option.get("IsPassword") or option.get("Sensitive"):
                        configured.append(key)
                    else:
                        values[key] = entry[key]
        return {"provider": provider, "values": values, "configured": configured}

    def require_owner(self, owner: str, session_id: str) -> dict:
        if (
            not self.session
            or self.session["owner"] != owner
            or not secrets.compare_digest(self.session["id"], session_id)
        ):
            raise SyncError("配置会话不存在或属于其他用户，请重新开始。")
        if time.time() - self.session["touched"] > SESSION_TIMEOUT:
            raise SyncError("配置会话已过期，请取消后重新开始。")
        self.session["touched"] = time.time()
        return self.session

    async def _open(self, owner: str, mode: str, path: Path, gui: dict | None = None):
        if self.active:
            raise SyncError("已有配置会话，请先完成或取消当前配置。")
        self.session = {
            "id": secrets.token_urlsafe(24),
            "owner": owner,
            "mode": mode,
            "touched": time.time(),
        }
        self.process = ConfigProcess(self.cloud)
        try:
            await self.process.start(path, gui)
        except BaseException:
            await self._close()
            raise
        self.timer = asyncio.create_task(self._expire())

    async def _expire(self):
        while self.active:
            await asyncio.sleep(10)
            async with self.lock:
                if self.session and (
                    time.time() - self.session["touched"] > SESSION_TIMEOUT
                    or self.process.process.returncode is not None
                ):
                    await self._close()

    async def reconnect(self, owner: str, name: str) -> dict:
        name = remote_name(name)
        remote = next(
            (item for item in await self.remotes() if item["name"] == name), None
        )
        if remote is None:
            raise SyncError("当前连接尚未配置，请先添加存储连接。")
        return await self.begin(
            owner,
            {"name": name, "backend": remote["type"], "parameters": {}, "edit": True},
            reconnect=True,
        )

    async def begin(
        self, owner: str, payload: dict, *, reconnect: bool = False
    ) -> dict:
        check_keys(
            payload,
            {"name", "backend", "parameters", "edit"},
            {"name", "backend", "parameters", "edit"},
        )
        name = remote_name(payload["name"])
        if type(payload["edit"]) is not bool:
            raise SyncError("edit 必须是布尔值。")
        field_data = await self.fields(
            payload["backend"], name if payload["edit"] else None
        )
        options = {
            item["Name"]: item for item in field_data["provider"].get("Options", [])
        }
        check_keys(payload["parameters"], set(options), set())
        parameters = payload["parameters"]
        if any(
            not isinstance(value, str) or len(value) > 256 * 1024 or "\x00" in value
            for value in parameters.values()
        ):
            raise SyncError("连接参数必须是有效的文本。")
        async with self.lock:
            if self.active:
                raise SyncError("已有配置会话，请先完成或取消。")
            existing = await self.remotes()
            if not payload["edit"] and name in {item["name"] for item in existing}:
                raise SyncError("此连接名称已存在，请使用编辑入口或更换名称。")
            self.protect_config(self.staging)
            self.staging.write_bytes(
                self.cloud.config_path.read_bytes()
                if self.cloud.config_path.exists()
                else b""
            )
            self.staging.chmod(0o600)
            await self._open(owner, "form", self.staging)
            self.session["remote"] = name
            # Keep OAuth interactive questions in this page; never open a browser on the server.
            parameters = {**parameters, "config_is_local": "false"}
            if payload["edit"]:
                parameters["config_refresh_token"] = "true" if reconnect else "false"
            try:
                result = await self.process.rpc(
                    "config/update" if payload["edit"] else "config/create",
                    {
                        "name": name,
                        **({} if payload["edit"] else {"type": payload["backend"]}),
                        "parameters": parameters,
                        "opt": {"nonInteractive": True, "obscure": True},
                    },
                )
                return await self._question(result)
            except BaseException:
                await self._close()
                raise

    async def _question(self, result: dict) -> dict:
        state = result.get("State", "")
        if not isinstance(state, str):
            raise SyncError("rclone 配置状态无效。")
        self.session["state"] = state
        if not state:
            self.session["complete"] = True
            self.session["option"] = None
            return {
                "session_id": self.session["id"],
                "complete": True,
                "name": self.session["remote"],
            }
        option = result.get("Option")
        # rclone returns ConfigError without repeating the current question.
        if result.get("Error") and option is None:
            option = self.session.get("option")
        if not isinstance(option, dict) or not isinstance(option.get("Name"), str):
            raise SyncError("此配置步骤无法在页面展示，请取消并使用官方 GUI。")
        self.session["option"] = option
        safe = {
            key: option[key]
            for key in (
                "Name",
                "Help",
                "Default",
                "Examples",
                "Required",
                "IsPassword",
                "Sensitive",
                "Type",
                "Exclusive",
            )
            if key in option
        }
        if (
            option.get("IsPassword")
            or option.get("Sensitive")
            or option["Name"] == "config_token"
        ):
            safe.update(Default="", IsPassword=True, Examples=[])
        return {
            "session_id": self.session["id"],
            "complete": False,
            "option": safe,
            "error": "rclone 未接受此答案，请检查后重试。"
            if result.get("Error")
            else "",
        }

    async def answer(self, owner: str, payload: dict) -> dict:
        check_keys(payload, {"session_id", "answer"}, {"session_id", "answer"})
        if (
            not isinstance(payload["session_id"], str)
            or not isinstance(payload["answer"], str)
            or len(payload["answer"]) > 256 * 1024
        ):
            raise SyncError("配置答案格式无效。")
        async with self.lock:
            session = self.require_owner(owner, payload["session_id"])
            if session["mode"] != "form" or session.get("complete"):
                raise SyncError("当前配置会话无需回答。")
            result = await self.process.rpc(
                "config/update",
                {
                    "name": session["remote"],
                    "parameters": {"config_is_local": "false"},
                    "opt": {
                        "nonInteractive": True,
                        "continue": True,
                        "state": session["state"],
                        "result": payload["answer"],
                    },
                },
            )
            return await self._question(result)

    async def finish(self, owner: str, payload: dict) -> dict:
        check_keys(payload, {"session_id", "save"}, {"session_id", "save"})
        if (
            not isinstance(payload["session_id"], str)
            or type(payload["save"]) is not bool
        ):
            raise SyncError("结束配置参数无效。")
        async with self.lock:
            session = self.require_owner(owner, payload["session_id"])
            if payload["save"] and session["mode"] == "form":
                if not session.get("complete"):
                    raise SyncError("请先完成所有配置步骤。")
                await self.process.close()
                self.protect_config(self.staging)
                self.protect_config(self.cloud.config_path)
                os.replace(self.staging, self.cloud.config_path)
            await self._close()
            self.protect_config(self.cloud.config_path)
            return {"saved": payload["save"], "remotes": await self.remotes()}

    async def gui(self, owner: str, payload: dict) -> dict:
        required = {"listen", "gui_port", "api_port", "gui_url", "api_url"}
        check_keys(payload, required, required)
        if payload["listen"] not in {"127.0.0.1", "0.0.0.0"}:
            raise SyncError("监听地址只能是 127.0.0.1 或 0.0.0.0。")
        for field in ("gui_port", "api_port"):
            if type(payload[field]) is not int or not 1024 <= payload[field] <= 65535:
                raise SyncError("GUI 和 API 端口必须是 1024～65535 的整数。")
        if payload["gui_port"] == payload["api_port"]:
            raise SyncError("GUI 与 API 必须使用不同端口。")
        gui_url, api_url = (
            public_url(payload["gui_url"]),
            public_url(payload["api_url"]),
        )
        await self.cloud.check_version()
        async with self.lock:
            self.protect_config(self.cloud.config_path)
            if not self.cloud.config_path.exists():
                self.cloud.config_path.write_text("", encoding="utf-8")
                self.cloud.config_path.chmod(0o600)
            await self._open(owner, "gui", self.cloud.config_path, payload)
            query = urlencode(
                {
                    "url": api_url + "/",
                    "user": self.process.user,
                    "pass": self.process.password,
                }
            )
            return {
                "session_id": self.session["id"],
                "login_url": gui_url + "/login?" + query,
                "expires_at": self.session["touched"] + SESSION_TIMEOUT,
            }

    async def _close(self) -> None:
        if self.process:
            await self.process.close()
        self.process = None
        self.session = None
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        self.timer = None
        if self.staging.is_file() and not self.staging.is_symlink():
            self.staging.unlink()

    async def close(self) -> None:
        async with self.lock:
            await self._close()
