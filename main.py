"""Dashboard routes, chat commands, and model tools for rclone task management."""

from __future__ import annotations

import base64
import functools
import hmac
import json
import secrets
import time
from pathlib import Path

import jwt
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import (
    error_response,
    json_response as response_json,
    request,
)
from astrbot.core.provider.register import llm_tools as registered_llm_tools
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_root
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from .files import SyncError, check_keys
from .rclone import MIRROR_MODES
from .service import TASK_FIELDS, SyncService

PLUGIN_NAME = "astrbot_plugin_rclone"
LOGOS_DIR = Path(__file__).resolve().parent / "logos"
LOGO_NAMES = ("logo.png", "logo2.png", "logo3.png", "logo4.png")
MODEL_TOOL_NAMES = (
    "rclone_list_tasks",
    "rclone_get_task",
    "rclone_get_status",
    "rclone_create_task",
    "rclone_update_task",
    "rclone_delete_task",
    "rclone_preview_task",
    "rclone_run_task",
    "rclone_cancel_task",
)


def load_logo(name):
    if not isinstance(name, str) or name not in LOGO_NAMES:
        raise SyncError(
            "logo 必须是 logos 目录中的以下文件名之一：" + "、".join(LOGO_NAMES)
        )
    try:
        data = (LOGOS_DIR / name).read_bytes()
    except OSError:
        raise SyncError(f"无法读取 logos/{name}，请检查图片文件及读取权限。") from None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SyncError(f"logos/{name} 不是有效的 PNG 图片。")
    return {
        "name": name,
        "src": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
    }


def json_response(data):
    return response_json(data, headers={"Cache-Control": "no-store"})


class RclonePlugin(Star):
    """Provide a native page and authenticated four-mode transfer management."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.page_logo = load_logo(config.get("logo"))
        model_tools_enabled = config.get("enable_model_tools", False)
        if type(model_tools_enabled) is not bool:
            raise SyncError("enable_model_tools 必须是布尔值。")
        self.model_tools_enabled = model_tools_enabled
        self.sync = SyncService(
            Path(get_astrbot_root()), StarTools.get_data_dir(PLUGIN_NAME)
        )
        self.handlers = []
        self.nonces = {}
        self.closed = False

    async def initialize(self) -> None:
        await self.sync.initialize()
        manager_getter = getattr(self.context, "get_llm_tool_manager", None)
        if manager_getter is not None:
            try:
                manager = manager_getter()
            except AttributeError:
                manager = registered_llm_tools
        else:
            manager = registered_llm_tools
        if manager is None:
            manager = registered_llm_tools
        for tool in manager.func_list:
            if tool.name in MODEL_TOOL_NAMES:
                tool.active = self.model_tools_enabled
        for action, method in {
            "state": "GET",
            "logo": "GET",
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

    def _resolve_task(self, task_ref: str) -> dict:
        """Resolve a task ID or an exact task name.

        Args:
            task_ref: Task ID or exact task name supplied by a command or tool.

        Returns:
            The persisted task dictionary.

        Raises:
            SyncError: If the reference is empty, missing, or ambiguous.
        """
        if not isinstance(task_ref, str) or not task_ref.strip():
            raise SyncError("请提供任务 ID 或任务名称。")
        task_ref = task_ref.strip()
        task = self.sync.data["tasks"].get(task_ref)
        if task is not None:
            return task
        matches = [
            value
            for value in self.sync.data["tasks"].values()
            if value["name"] == task_ref
        ]
        if not matches:
            raise SyncError("任务不存在。")
        if len(matches) > 1:
            raise SyncError("任务名称不唯一，请改用任务 ID。")
        return matches[0]

    @staticmethod
    def _task_view(task: dict) -> dict:
        """Return the public fields of a persisted task.

        Args:
            task: Persisted task dictionary.

        Returns:
            A copy containing task configuration and its next scheduled time.
        """
        return {
            key: task[key] for key in TASK_FIELDS | {"id", "next_run"} if key in task
        }

    @staticmethod
    def _tool_result(result=None, error: str | None = None) -> str:
        """Encode a model-tool result as stable JSON text.

        Args:
            result: Successful operation result.
            error: User-facing failure message.

        Returns:
            JSON text for the model.
        """
        payload = {"ok": error is None}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)

    def _require_model_tools(self) -> None:
        """Ensure model tools are enabled in plugin configuration.

        Raises:
            SyncError: If the model-tool switch is disabled.
        """
        if not self.model_tools_enabled:
            raise SyncError("模型工具已关闭，请在插件配置中启用 enable_model_tools。")

    @staticmethod
    def _parse_object(payload: str, operation: str) -> dict:
        """Parse one JSON object from a command payload.

        Args:
            payload: Greedy command payload.
            operation: Operation name used in the error message.

        Returns:
            Parsed JSON object.

        Raises:
            SyncError: If the payload is not an object.
        """
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            raise SyncError(f"{operation} 的参数必须是 JSON 对象。") from None
        if not isinstance(value, dict):
            raise SyncError(f"{operation} 的参数必须是 JSON 对象。")
        return value

    def _parse_task_patch(self, payload: str) -> tuple[str, dict]:
        """Parse an exact task reference followed by a JSON patch.

        Args:
            payload: Text containing the task reference and JSON object.

        Returns:
            A task reference and a partial task update.

        Raises:
            SyncError: If no unambiguous JSON suffix is found.
        """
        text = payload.strip()
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            task_ref = text[:index].strip()
            try:
                patch, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if text[index + end :].strip() or not task_ref:
                continue
            if not isinstance(patch, dict):
                raise SyncError("edit 的更新内容必须是 JSON 对象。")
            return task_ref, patch
        raise SyncError("edit 格式应为：/rclone edit <task> {JSON 更新内容}")

    def _merge_task_patch(self, task_ref: str, patch: dict) -> dict:
        """Validate and persist a partial task update.

        Args:
            task_ref: Task ID or exact task name.
            patch: Partial task fields.

        Returns:
            The saved task dictionary.

        Raises:
            SyncError: If the task or patch is invalid.
        """
        task = self._resolve_task(task_ref)
        check_keys(patch, TASK_FIELDS, set())
        value = {key: task[key] for key in TASK_FIELDS}
        value.update(patch)
        return self.sync.save_task({**value, "id": task["id"]})

    def _human_task(self, task: dict) -> str:
        """Format one task for a chat response.

        Args:
            task: Persisted task dictionary.

        Returns:
            Human-readable task summary.
        """
        return (
            f"{task['name']}\n"
            f"id: {task['id']}\n"
            f"mode: {task['mode']}\n"
            f"local_path: {task['local_path']}\n"
            f"remote_path: {task['remote_path']}\n"
            f"enabled: {str(task['enabled']).lower()}\n"
            f"interval_minutes: {task['interval_minutes']}\n"
            f"exclude: {json.dumps(task['exclude'], ensure_ascii=False)}"
        )

    @filter.command("rclone")
    @filter.regex(r"^rclone\s*$")
    async def rclone_help(self, event: AstrMessageEvent):
        """Show the English rclone task command help."""
        yield event.plain_result(
            "rclone commands:\n"
            "/rclone list\n"
            "/rclone show <task>\n"
            "/rclone status [task]\n"
            "/rclone create <JSON>\n"
            "/rclone edit <task> <JSON>\n"
            "/rclone delete <task> confirm\n"
            "/rclone preview <task>\n"
            "/rclone run <task> [confirm]\n"
            "/rclone cancel <task>"
        )

    @filter.command("rclone list")
    async def rclone_list(self, event: AstrMessageEvent):
        """List all configured synchronization tasks."""
        try:
            tasks = list(self.sync.data["tasks"].values())
            if not tasks:
                yield event.plain_result("当前没有同步任务。")
                return
            yield event.plain_result(
                "\n\n".join(
                    f"{task['name']} | {task['id']} | {task['mode']} | "
                    f"enabled={str(task['enabled']).lower()}"
                    for task in tasks
                )
            )
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone show")
    async def rclone_show(self, event: AstrMessageEvent, task_ref: GreedyStr):
        """Show one task configuration."""
        try:
            yield event.plain_result(self._human_task(self._resolve_task(task_ref)))
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone status")
    async def rclone_status(self, event: AstrMessageEvent, task_ref: GreedyStr = ""):
        """Show global or task-specific execution status."""
        try:
            if task_ref.strip():
                task = self._resolve_task(task_ref)
                runs = [
                    run
                    for run in self.sync.data["runs"]
                    if run["task_id"] == task["id"]
                ]
            else:
                runs = list(self.sync.data["runs"])
            if not runs:
                yield event.plain_result("没有执行记录。")
                return
            lines = [
                f"{run['task_name']} | {run['id']} | {run['status']}"
                + (f" | error: {run['error']}" if run["error"] else "")
                for run in reversed(runs[-20:])
            ]
            yield event.plain_result("\n".join(lines))
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone create")
    async def rclone_create(self, event: AstrMessageEvent, payload: GreedyStr):
        """Create a synchronization task from a JSON object."""
        try:
            value = self._parse_object(payload, "create")
            check_keys(value, TASK_FIELDS, TASK_FIELDS)
            task = self.sync.save_task(value)
            yield event.plain_result(f"已创建同步任务：\n{self._human_task(task)}")
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone edit")
    async def rclone_edit(self, event: AstrMessageEvent, payload: GreedyStr):
        """Update one synchronization task with a JSON patch."""
        try:
            task_ref, patch = self._parse_task_patch(payload)
            task = self._merge_task_patch(task_ref, patch)
            yield event.plain_result(f"已更新同步任务：\n{self._human_task(task)}")
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone delete")
    async def rclone_delete(self, event: AstrMessageEvent, payload: GreedyStr):
        """Delete a task configuration after an explicit confirmation token."""
        try:
            text = payload.strip()
            if not text.lower().endswith(" confirm"):
                raise SyncError("删除任务请在末尾添加 confirm。")
            task = self._resolve_task(text[: -len(" confirm")])
            self.sync.delete_task(task["id"])
            yield event.plain_result(
                f"已删除同步任务：{task['name']}。本机和云端文件未删除。"
            )
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone preview")
    async def rclone_preview(self, event: AstrMessageEvent, task_ref: GreedyStr):
        """Preview changes without modifying either side."""
        try:
            task = self._resolve_task(task_ref)
            result = await self.sync.preview(
                {
                    key: task[key]
                    for key in ("id", "mode", "local_path", "remote_path", "exclude")
                }
            )
            yield event.plain_result(json.dumps(result, ensure_ascii=False, indent=2))
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone run")
    async def rclone_run(self, event: AstrMessageEvent, payload: GreedyStr):
        """Queue a task for execution, requiring confirmation for mirror modes."""
        try:
            text = payload.strip()
            confirmed = text.lower().endswith(" confirm")
            task_ref = text[: -len(" confirm")] if confirmed else text
            task = self._resolve_task(task_ref)
            if task["mode"] in MIRROR_MODES and not confirmed:
                raise SyncError("镜像任务会删除目标端多余文件，请在末尾添加 confirm。")
            run = self.sync.enqueue(task["id"])
            yield event.plain_result(f"已排队执行：{task['name']}。run_id: {run['id']}")
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("rclone cancel")
    async def rclone_cancel(self, event: AstrMessageEvent, task_ref: GreedyStr):
        """Cancel a queued or running task."""
        try:
            task = self._resolve_task(task_ref)
            await self.sync.cancel(task["id"])
            yield event.plain_result(f"已取消同步：{task['name']}。")
        except (SyncError, OSError, ValueError, TypeError) as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="rclone_list_tasks")
    async def rclone_list_tasks(self, event: AstrMessageEvent) -> str:
        """列出所有同步任务。

        Returns:
            JSON text containing every configured task.
        """
        try:
            self._require_model_tools()
            return self._tool_result(
                [self._task_view(task) for task in self.sync.data["tasks"].values()]
            )
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_get_task")
    async def rclone_get_task(self, event: AstrMessageEvent, task_ref: str) -> str:
        """查看一个同步任务的完整配置。

        Args:
            task_ref(string): 任务 ID 或精确任务名称。

        Returns:
            JSON text containing the task.
        """
        try:
            self._require_model_tools()
            return self._tool_result(self._task_view(self._resolve_task(task_ref)))
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_get_status")
    async def rclone_get_status(self, event: AstrMessageEvent) -> str:
        """查看同步连接、队列和最近执行记录。

        Returns:
            JSON text containing service state and recent runs.
        """
        try:
            self._require_model_tools()
            state = self.sync.state()
            return self._tool_result(
                {
                    "cloud": state["cloud"],
                    "busy": state["busy"],
                    "runs": state["runs"][:20],
                }
            )
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_create_task")
    async def rclone_create_task(self, event: AstrMessageEvent, task: dict) -> str:
        """创建同步任务。

        Args:
            task(object): 包含 name、mode、local_path、remote_path、exclude、enabled 和 interval_minutes；mode 必须是 upload、upload_mirror、download 或 download_mirror。

        Returns:
            JSON text containing the created task.
        """
        try:
            self._require_model_tools()
            check_keys(task, TASK_FIELDS, TASK_FIELDS)
            return self._tool_result(self._task_view(self.sync.save_task(task)))
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_update_task")
    async def rclone_update_task(
        self, event: AstrMessageEvent, task_ref: str, changes: dict
    ) -> str:
        """部分更新一个同步任务。

        Args:
            task_ref(string): 任务 ID 或精确任务名称。
            changes(object): 要更新的任务字段，只能使用 name、mode、local_path、remote_path、exclude、enabled 或 interval_minutes。

        Returns:
            JSON text containing the updated task.
        """
        try:
            self._require_model_tools()
            return self._tool_result(
                self._task_view(self._merge_task_patch(task_ref, changes))
            )
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_delete_task")
    async def rclone_delete_task(
        self, event: AstrMessageEvent, task_ref: str, confirm: bool
    ) -> str:
        """删除同步任务配置，不删除本机或云端文件。

        Args:
            task_ref(string): 任务 ID 或精确任务名称。
            confirm(boolean): 必须为 true 才允许删除。

        Returns:
            JSON text describing the deletion.
        """
        try:
            self._require_model_tools()
            if confirm is not True:
                raise SyncError("删除任务必须明确提供 confirm=true。")
            task = self._resolve_task(task_ref)
            self.sync.delete_task(task["id"])
            return self._tool_result({"deleted": task["id"], "files_preserved": True})
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_preview_task")
    async def rclone_preview_task(self, event: AstrMessageEvent, task_ref: str) -> str:
        """预览同步变更，不修改本机或云端文件。

        Args:
            task_ref(string): 任务 ID 或精确任务名称。

        Returns:
            JSON text containing the dry-run result.
        """
        try:
            self._require_model_tools()
            task = self._resolve_task(task_ref)
            result = await self.sync.preview(
                {
                    key: task[key]
                    for key in ("id", "mode", "local_path", "remote_path", "exclude")
                }
            )
            return self._tool_result(result)
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_run_task")
    async def rclone_run_task(
        self, event: AstrMessageEvent, task_ref: str, confirm: bool = False
    ) -> str:
        """排队执行同步任务；镜像模式必须显式确认。

        Args:
            task_ref(string): 任务 ID 或精确任务名称。
            confirm(boolean): 镜像模式必须为 true；普通更新模式可为 false。

        Returns:
            JSON text containing the queued run.
        """
        try:
            self._require_model_tools()
            task = self._resolve_task(task_ref)
            if task["mode"] in MIRROR_MODES and confirm is not True:
                raise SyncError("镜像任务必须明确提供 confirm=true。")
            return self._tool_result(self.sync.enqueue(task["id"]))
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

    @filter.llm_tool(name="rclone_cancel_task")
    async def rclone_cancel_task(self, event: AstrMessageEvent, task_ref: str) -> str:
        """取消排队或正在执行的同步任务。

        Args:
            task_ref(string): 任务 ID 或精确任务名称。

        Returns:
            JSON text describing the cancellation.
        """
        try:
            self._require_model_tools()
            task = self._resolve_task(task_ref)
            await self.sync.cancel(task["id"])
            return self._tool_result({"cancelled": task["id"]})
        except (SyncError, OSError, ValueError, TypeError) as exc:
            return self._tool_result(error=str(exc))

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
            if action == "logo":
                return json_response(self.page_logo)
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
