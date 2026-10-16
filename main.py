"""Dashboard-only management routes for the native AstrBot plugin page."""

from __future__ import annotations

import functools
import hmac
import secrets
import time
from pathlib import Path

import jwt
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import (
    error_response,
    json_response as response_json,
    request,
)
from astrbot.core.utils.astrbot_path import get_astrbot_root
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from .files import SyncError, check_keys
from .service import SyncService

PLUGIN_NAME = "astrbot_plugin_rclone"


def json_response(data):
    return response_json(data, headers={"Cache-Control": "no-store"})


class RclonePlugin(Star):
    """Provide a native page and authenticated four-mode transfer management."""

    def __init__(self, context: Context):
        super().__init__(context)
        self.sync = SyncService(
            Path(get_astrbot_root()), StarTools.get_data_dir(PLUGIN_NAME)
        )
        self.handlers = []
        self.nonces = {}
        self.closed = False

    async def initialize(self) -> None:
        await self.sync.initialize()
        for action, method in {
            "state": "GET",
            "connection": "POST",
            "connect": "POST",
            "disconnect": "POST",
            "save-task": "POST",
            "delete-task": "POST",
            "preview": "POST",
            "run": "POST",
            "cancel": "POST",
            "providers": "GET",
            "remotes": "GET",
            "config-fields": "POST",
            "config-begin": "POST",
            "config-answer": "POST",
            "config-finish": "POST",
            "config-reconnect": "POST",
            "install-rclone": "POST",
            "gui-start": "POST",
        }.items():
            handler = functools.partial(self.dispatch, action)
            self.handlers.append(handler)
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{action}",
                handler,
                [method],
                "Rclone sync management",
            )
        self.logger.info("Rclone sync page is ready")

    def authorize_request(self) -> str:
        """Require a Dashboard login JWT; asset tokens and API keys are rejected."""
        if self.closed:
            raise PermissionError("插件页面正在关闭。")
        username = request.username
        authorization = request.headers.get("Authorization", "")
        secret = self.context.get_config().get("dashboard", {}).get("jwt_secret")
        if (
            not isinstance(username, str)
            or not username
            or username.startswith(API_KEY_USERNAME_PREFIX)
            or not authorization.startswith("Bearer ")
            or not isinstance(secret, str)
            or not secret
        ):
            raise PermissionError("请从已登录的 AstrBot WebUI 插件页面访问。")
        try:
            payload = jwt.decode(
                authorization.removeprefix("Bearer ").strip(),
                secret,
                algorithms=["HS256"],
                options={"require": ["exp", "username"]},
            )
        except jwt.InvalidTokenError:
            raise PermissionError("登录凭据无效或已过期，请重新登录 WebUI。") from None
        if payload["username"] != username or payload.get("token_type") is not None:
            raise PermissionError("页面资源凭据或身份不匹配。")
        return username

    def nonce(self, username: str) -> str:
        value = self.nonces.get(username)
        if not value or value[1] <= time.time():
            value = (secrets.token_urlsafe(32), time.time() + 3600)
            self.nonces[username] = value
        return value[0]

    async def dispatch(self, action: str):
        """Validate each request independently of the frontend bridge."""
        try:
            username = self.authorize_request()
            if request.method == "POST":
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise SyncError("请求体必须是 JSON 对象。")
                nonce = self.nonces.get(username)
                submitted = payload.get("csrf")
                if (
                    not nonce
                    or nonce[1] <= time.time()
                    or not isinstance(submitted, str)
                    or not hmac.compare_digest(nonce[0], submitted)
                ):
                    raise PermissionError("操作凭据过期或无效，请刷新页面。")
                payload = {
                    key: value for key, value in payload.items() if key != "csrf"
                }
            else:
                payload = {}
            if action == "state":
                return json_response(
                    {
                        **self.sync.state(),
                        "configuration": self.sync.configuration.status(username),
                        "csrf": self.nonce(username),
                    }
                )
            if action == "providers":
                return json_response(
                    {"providers": await self.sync.configuration.providers()}
                )
            if action == "remotes":
                return json_response(
                    {"remotes": await self.sync.configuration.remotes()}
                )
            if action == "config-fields":
                check_keys(payload, {"backend", "name"}, {"backend", "name"})
                return json_response(
                    await self.sync.configuration.fields(
                        payload["backend"], payload["name"]
                    )
                )
            if action in {"config-begin", "gui-start", "config-reconnect"}:
                if action == "config-reconnect":
                    check_keys(payload, set(), set())
                return json_response(
                    await self.sync.begin_configuration(
                        username,
                        payload,
                        gui=action == "gui-start",
                        reconnect=action == "config-reconnect",
                    )
                )
            if action == "config-answer":
                return json_response(
                    await self.sync.configuration.answer(username, payload)
                )
            if action == "config-finish":
                return json_response(
                    await self.sync.configuration.finish(username, payload)
                )
            if action == "install-rclone":
                check_keys(payload, set(), set())
                self.sync.install_rclone()
            elif action == "connection":
                await self.sync.save_connection(payload)
            elif action in {"connect", "disconnect"}:
                check_keys(payload, set(), set())
                if action == "connect":
                    return json_response(await self.sync.connect())
                else:
                    await self.sync.disconnect()
            elif action == "save-task":
                self.sync.save_task(payload)
            elif action in {"delete-task", "run", "cancel"}:
                check_keys(payload, {"task_id"}, {"task_id"})
                if action == "run":
                    self.sync.enqueue(payload["task_id"])
                elif action == "cancel":
                    await self.sync.cancel(payload["task_id"])
                else:
                    self.sync.delete_task(payload["task_id"])
            elif action == "preview":
                return json_response(await self.sync.preview(payload))
            else:
                raise SyncError("未知页面操作。")
            return json_response({"saved": True})
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (SyncError, OSError) as exc:
            return error_response(str(exc), status_code=400)
        except (ValueError, KeyError, TypeError):
            return error_response(
                "数据格式无效，请检查状态文件或请求参数。", status_code=400
            )

    async def terminate(self) -> None:
        self.closed = True
        self.context.registered_web_apis[:] = [
            entry
            for entry in self.context.registered_web_apis
            if not any(entry[1] is handler for handler in self.handlers)
        ]
        await self.sync.close()
        self.logger.info("Rclone sync page is closed")
