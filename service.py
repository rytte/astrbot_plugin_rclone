"""Persistent four-mode transfer tasks, serial execution, and live transfer statistics."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shlex
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .configuration import Configuration
from .files import (
    SyncError,
    check_keys,
    destination_path,
    source_path,
    stage_file,
    write_json,
)
from .installer import RcloneInstaller
from .rclone import (
    DEFAULT_CONNECTION,
    MODES,
    UPLOAD_MODES,
    Rclone,
    opaque_id,
    relative_path,
    validate_connection,
)

TASK_FIELDS = {
    "name",
    "mode",
    "local_path",
    "remote_path",
    "exclude",
    "enabled",
    "interval_minutes",
}
ACTIVE = {"queued", "checking", "syncing"}
STATUSES = ACTIVE | {"completed", "failed", "cancelled", "interrupted"}


def validate_task(
    value: dict, root: Path, private: Path, *, check_local: bool = True
) -> dict:
    check_keys(value, TASK_FIELDS, TASK_FIELDS)
    name = value["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise SyncError("任务名称必须是 1～100 个字符。")
    if type(value["enabled"]) is not bool:
        raise SyncError("enabled 必须是布尔值。")
    interval = value["interval_minutes"]
    if type(interval) is not int or not 0 <= interval <= 525600:
        raise SyncError("执行间隔必须是 0～525600 的整数分钟。")
    excludes = value["exclude"]
    if (
        not isinstance(excludes, list)
        or len(excludes) > 100
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 1024
            or any(ord(c) < 32 for c in item)
            for item in excludes
        )
    ):
        raise SyncError("排除规则必须是最多 100 条非空 rclone glob 表达式。")
    mode = value["mode"]
    if not isinstance(mode, str) or mode not in MODES:
        raise SyncError(
            "同步模式必须是 upload、upload_mirror、download 或 download_mirror。"
        )
    source = value["local_path"]
    if check_local:
        validator = source_path if mode in UPLOAD_MODES else destination_path
        source = str(validator(source, root, private))
    elif (
        not isinstance(source, str)
        or len(source) > 4096
        or not Path(source).is_absolute()
    ):
        raise SyncError("状态中的本机路径必须是绝对路径。")
    return {
        **value,
        "name": name.strip(),
        "local_path": source,
        "remote_path": relative_path(value["remote_path"]),
        "exclude": [item.strip() for item in excludes],
    }


class SyncService:
    def __init__(self, root: Path, directory: Path, cloud: Rclone | None = None):
        self.root, self.directory = root.resolve(), directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"
        self.data = {
            "version": 1,
            "mode": "transfer",
            "connection": {**DEFAULT_CONNECTION, "enabled": False},
            "tasks": {},
            "runs": [],
        }
        if self.state_path.exists():
            try:
                self.data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise SyncError(
                    "state.json 无法解析，请修复或另存后移出私有目录。"
                ) from None
            self.validate_state()
        self.work = self.directory / "work"
        self.work.mkdir(exist_ok=True)
        if (
            self.work.is_symlink()
            or self.work.is_junction()
            or not self.work.resolve().is_relative_to(self.directory)
        ):
            raise SyncError("暂存目录必须位于本插件私有目录内，且不能是链接。")
        self.config_path = self.directory / "rclone.conf"
        self.cloud = cloud or Rclone(
            self.config_path,
            {key: self.data["connection"][key] for key in DEFAULT_CONNECTION},
        )
        self.configuration = Configuration(self.cloud, self.directory)
        self.installer = RcloneInstaller(self.directory)
        self.install_task = None
        self.queue = asyncio.Queue()
        self.worker = self.scheduler = self.current = None
        self.current_run = None
        self.cloud_lock = asyncio.Lock()
        self.closed = self.previewing = False

    def validate_state(self):
        data = self.data
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise SyncError("状态文件版本不支持，当前仅支持版本 1。")
        if data.get("mode") != "transfer":
            raise SyncError(
                "state.json 不是当前四模式同步格式。请将旧状态文件另存后移出私有目录，保留 rclone.conf 并重新创建同步任务；旧云端文件不会自动转换或删除。"
            )
        check_keys(
            data,
            {"version", "mode", "connection", "tasks", "runs"},
            {"version", "mode", "connection", "tasks", "runs"},
        )
        connection = data["connection"]
        check_keys(
            connection,
            set(DEFAULT_CONNECTION) | {"enabled"},
            set(DEFAULT_CONNECTION) | {"enabled"},
        )
        validate_connection({key: connection[key] for key in DEFAULT_CONNECTION})
        if (
            type(connection["enabled"]) is not bool
            or not isinstance(data["tasks"], dict)
            or not isinstance(data["runs"], list)
        ):
            raise SyncError("状态文件格式损坏。")
        validated_tasks = []
        for task_id, task in data["tasks"].items():
            opaque_id(task_id)
            check_keys(
                task, TASK_FIELDS | {"id", "next_run"}, TASK_FIELDS | {"id", "next_run"}
            )
            if task["id"] != task_id or not self.valid_time(
                task["next_run"], optional=True
            ):
                raise SyncError("任务身份或执行时间损坏。")
            validated = validate_task(
                {key: task[key] for key in TASK_FIELDS},
                self.root,
                self.directory,
                check_local=False,
            )
            for previous in validated_tasks:
                self.validate_pair(validated, previous)
            validated_tasks.append(validated)
        fields = {
            "id",
            "task_id",
            "task_name",
            "mode",
            "local_path",
            "remote_path",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "scheduled",
            "error",
            "warning",
            "progress",
        }
        for run in data["runs"]:
            check_keys(run, fields, fields)
            opaque_id(run["id"])
            opaque_id(run["task_id"])
            if (
                not isinstance(run["mode"], str)
                or run["mode"] not in MODES
                or run["status"] not in STATUSES
                or not self.valid_time(run["created_at"])
                or any(
                    not self.valid_time(run[key], optional=True)
                    for key in ("started_at", "finished_at")
                )
                or not isinstance(run["progress"], dict)
                or type(run["scheduled"]) is not bool
                or any(
                    not isinstance(run[key], str)
                    for key in (
                        "task_name",
                        "local_path",
                        "remote_path",
                        "error",
                        "warning",
                    )
                )
            ):
                raise SyncError("执行记录格式损坏。")
            if any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in run["progress"].values()
            ):
                raise SyncError("执行进度格式损坏。")
            if run["status"] in ACTIVE:
                run.update(
                    status="interrupted",
                    error="AstrBot 在同步完成前停止，请重新执行。",
                    finished_at=time.time(),
                )

    @staticmethod
    def valid_time(value, optional=False):
        return (
            optional
            and value is None
            or type(value) in (int, float)
            and math.isfinite(value)
            and value >= 0
        )

    @staticmethod
    def overlap(first, second):
        return (
            first == second
            or first.startswith(second + "/")
            or second.startswith(first + "/")
        )

    def persist(self):
        active = [run for run in self.data["runs"] if run["status"] in ACTIVE]
        finished = [run for run in self.data["runs"] if run["status"] not in ACTIVE]
        self.data["runs"] = sorted(
            active + finished[-200:], key=lambda run: run["created_at"]
        )
        write_json(self.state_path, self.data)

    async def initialize(self):
        for child in self.work.iterdir():
            self.remove_workspace(child)
        self.persist()
        if self.data["connection"]["enabled"]:
            try:
                await self.cloud.check_connection()
            except SyncError:
                pass
        self.worker = asyncio.create_task(self._worker())
        self.scheduler = asyncio.create_task(self._schedule())

    def remove_workspace(self, path: Path):
        if not path.exists():
            return
        if (
            path.parent != self.work
            or not re.fullmatch(r"[0-9a-f]{32}", path.name)
            or self.work.is_symlink()
            or self.work.is_junction()
            or path.is_symlink()
            or path.is_junction()
            or not path.is_dir()
            or not self.work.resolve().is_relative_to(self.directory)
            or not path.resolve().is_relative_to(self.work.resolve())
        ):
            raise SyncError("暂存路径不属于本插件，拒绝递归清理。")
        shutil.rmtree(path)

    def state(self):
        connection = self.data["connection"]

        def command(*args):
            values = [connection["binary"], *args, "--config", str(self.config_path)]
            if os.name == "nt":
                return "& " + " ".join(
                    "'" + value.replace("'", "''") + "'" for value in values
                )
            return shlex.join(values)

        return {
            "connection": {key: connection[key] for key in DEFAULT_CONNECTION},
            "config_path": str(self.config_path),
            "commands": {
                "setup": command("config"),
                "reconnect": command("config", "reconnect", connection["remote"] + ":"),
            },
            "cloud": self.cloud.status(),
            "configuration": self.configuration.status(),
            "installation": dict(self.installer.state),
            "root": str(self.root),
            "examples": {
                "plugins": str(self.root / "data" / "plugin_data"),
                "backups": str(self.root / "data" / "backups"),
            },
            "tasks": list(self.data["tasks"].values()),
            "runs": list(reversed(self.data["runs"])),
            "busy": self.busy,
        }

    @property
    def busy(self):
        return (
            self.installer.active
            or self.previewing
            or any(run["status"] in ACTIVE for run in self.data["runs"])
        )

    def require_idle(self):
        if self.installer.active:
            raise SyncError("正在安装 rclone，请等待安装完成后再操作。")
        if self.configuration.active:
            raise SyncError("正在配置存储连接，请先保存或结束配置。")
        if self.closed:
            raise SyncError("插件正在关闭。")
        if self.busy or self.cloud_lock.locked():
            raise SyncError("有同步或预览正在执行，请完成后再操作。")

    def task(self, task_id):
        task = self.data["tasks"].get(opaque_id(task_id))
        if task is None:
            raise SyncError("任务不存在。")
        return task

    async def save_connection(self, payload: dict) -> None:
        self.require_idle()
        settings = validate_connection(payload)
        async with self.cloud_lock:
            await self.cloud.disconnect()
            self.cloud.configure(settings)
            self.data["connection"] = {**settings, "enabled": False}
            self.persist()

    async def connect(self) -> dict:
        self.require_idle()
        async with self.cloud_lock:
            self.data["connection"]["enabled"] = False
            self.persist()
            result = await self.cloud.check_connection()
            self.data["connection"]["enabled"] = True
            self.persist()
            return result

    async def disconnect(self) -> None:
        self.require_idle()
        async with self.cloud_lock:
            await self.cloud.disconnect()
            self.data["connection"]["enabled"] = False
            self.persist()

    async def begin_configuration(
        self, owner: str, payload: dict, *, gui: bool = False, reconnect: bool = False
    ) -> dict:
        if reconnect:
            check_keys(payload, set(), set())
        self.require_idle()
        async with self.cloud_lock:
            await self.cloud.disconnect()
            self.data["connection"]["enabled"] = False
            self.persist()
            if reconnect:
                return await self.configuration.reconnect(
                    owner, self.cloud.settings["remote"]
                )
            if gui:
                return await self.configuration.gui(owner, payload)
            return await self.configuration.begin(owner, payload)

    def install_rclone(self) -> None:
        self.require_idle()
        self.installer.state = {
            "status": "checking",
            "received": 0,
            "total": None,
            "error": "",
        }
        self.install_task = asyncio.create_task(self._install_rclone())

    async def _install_rclone(self) -> None:
        try:
            async with self.cloud_lock:
                await self.cloud.disconnect()
                self.data["connection"]["enabled"] = False
                self.persist()
                async with asyncio.timeout(600):
                    binary, version = await self.installer.install()
                settings = {**self.cloud.settings, "binary": binary}
                self.cloud.configure(settings)
                self.data["connection"] = {**settings, "enabled": False}
                self.persist()
                self.cloud.version = version
                self.installer.state.update(status="completed", binary=binary)
        except asyncio.CancelledError:
            self.installer.state.update(
                status="cancelled", error="插件停止，安装已取消。"
            )
            raise
        except TimeoutError:
            self.installer.state.update(
                status="failed", error="下载或安装超时，请重试。"
            )
        except httpx.HTTPError:
            self.installer.state.update(
                status="failed",
                error="无法从 rclone 官方下载站获取安装文件，请检查网络后重试。",
            )
        except (SyncError, OSError) as exc:
            self.installer.state.update(status="failed", error=str(exc))
        except Exception:
            self.installer.state.update(
                status="failed", error="rclone 安装失败，请重试或手动安装。"
            )

    @classmethod
    def validate_pair(cls, first, second):
        first_upload = first["mode"] in UPLOAD_MODES
        second_upload = second["mode"] in UPLOAD_MODES
        if first_upload and second_upload:
            if cls.overlap(
                first["remote_path"].casefold(), second["remote_path"].casefold()
            ):
                raise SyncError(
                    "上传任务必须使用独立的云端子目录，不能重复或互相包含。"
                )
        if not first_upload or not second_upload:
            left, right = (
                Path(first["local_path"]).resolve(),
                Path(second["local_path"]).resolve(),
            )
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise SyncError("下载接收目录不能与其他任务的本机路径重复或互相包含。")

    def validate_locations(self, task, task_id=None):
        for existing in self.data["tasks"].values():
            if existing["id"] != task_id:
                self.validate_pair(task, existing)

    def save_task(self, payload):
        check_keys(payload, TASK_FIELDS | {"id"}, TASK_FIELDS)
        task_id = payload.get("id")
        if task_id is not None:
            self.task(task_id)
            if any(
                run["task_id"] == task_id and run["status"] in ACTIVE
                for run in self.data["runs"]
            ):
                raise SyncError("该任务正在执行，暂时不能修改。")
        else:
            task_id = uuid4().hex
        value = validate_task(
            {key: payload[key] for key in TASK_FIELDS}, self.root, self.directory
        )
        self.validate_locations(value, task_id)
        interval = value["interval_minutes"]
        task = {
            **value,
            "id": task_id,
            "next_run": time.time() + interval * 60
            if interval and value["enabled"]
            else None,
        }
        self.data["tasks"][task_id] = task
        self.persist()
        return task

    def delete_task(self, task_id):
        self.task(task_id)
        if any(
            run["task_id"] == task_id and run["status"] in ACTIVE
            for run in self.data["runs"]
        ):
            raise SyncError("该任务正在执行，暂时不能删除。")
        del self.data["tasks"][task_id]
        self.persist()

    async def prepare_local(self, task, workspace):
        uploading = task["mode"] in UPLOAD_MODES
        validator = source_path if uploading else destination_path
        source = await asyncio.to_thread(
            validator, task["local_path"], self.root, self.directory
        )
        excludes = list(task["exclude"])
        if uploading and source.is_file():
            cancelled = threading.Event()
            prepare = asyncio.create_task(
                asyncio.to_thread(stage_file, source, workspace, cancelled)
            )
            try:
                source = await asyncio.shield(prepare)
            except asyncio.CancelledError:
                cancelled.set()
                await asyncio.gather(prepare, return_exceptions=True)
                raise
        elif self.directory.is_relative_to(source):
            relative = self.directory.relative_to(source).as_posix()
            # Escape glob metacharacters in an otherwise literal local path.
            relative = re.sub(r"([*?\[\]{}])", r"\\\1", relative)
            excludes.insert(0, "/" + relative + "/**")
        return source, excludes

    async def preview(self, payload):
        check_keys(
            payload,
            {"mode", "local_path", "remote_path", "exclude", "id"},
            {"mode", "local_path", "remote_path", "exclude"},
        )
        self.require_idle()
        self.cloud.require_connected()
        if "id" in payload:
            self.task(payload["id"])
        task = validate_task(
            {
                "name": "preview",
                "enabled": False,
                "interval_minutes": 0,
                **{
                    key: payload[key]
                    for key in ("mode", "local_path", "remote_path", "exclude")
                },
            },
            self.root,
            self.directory,
        )
        self.validate_locations(task, payload.get("id"))
        workspace = self.work / uuid4().hex
        self.previewing = True
        try:
            async with self.cloud_lock:
                source, excludes = await self.prepare_local(task, workspace)
                return await self.cloud.transfer(
                    source, task["remote_path"], task["mode"], excludes, dry_run=True
                )
        finally:
            try:
                self.remove_workspace(workspace)
            finally:
                self.previewing = False

    def enqueue(self, task_id, *, scheduled=False):
        if self.installer.active:
            raise SyncError("正在安装 rclone，请等待安装完成后重新检测连接。")
        if self.configuration.active:
            raise SyncError("配置期间暂停同步，请先结束配置并重新检测连接。")
        if self.closed or self.previewing:
            raise SyncError("插件正在关闭或执行预览，请稍后重试。")
        task = self.task(task_id)
        self.cloud.require_connected()
        if any(
            run["task_id"] == task_id and run["status"] in ACTIVE
            for run in self.data["runs"]
        ):
            raise SyncError("该任务已经排队或正在执行。")
        now = time.time()
        run = {
            "id": uuid4().hex,
            "task_id": task_id,
            "task_name": task["name"],
            "mode": task["mode"],
            "local_path": task["local_path"],
            "remote_path": self.cloud.settings["remote"]
            + ":"
            + self.cloud.settings["folder"]
            + "/"
            + task["remote_path"],
            "status": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "scheduled": scheduled,
            "error": "",
            "warning": "",
            "progress": {},
        }
        if task["enabled"] and task["interval_minutes"]:
            task["next_run"] = now + task["interval_minutes"] * 60
        self.data["runs"].append(run)
        self.persist()
        self.queue.put_nowait((dict(task), run))
        return run

    async def cancel(self, task_id):
        self.task(task_id)
        run = next(
            (
                run
                for run in self.data["runs"]
                if run["task_id"] == task_id and run["status"] in ACTIVE
            ),
            None,
        )
        if run is None:
            raise SyncError("该任务没有正在执行或排队的同步。")
        if self.current is not None and self.current_run is run:
            self.current.cancel()
            await asyncio.gather(self.current, return_exceptions=True)
        elif run["status"] == "queued":
            run.update(status="cancelled", finished_at=time.time())
            self.persist()

    async def _schedule(self):
        while True:
            if (
                self.cloud.status()["connected"]
                and not self.configuration.active
                and not self.installer.active
                and not self.previewing
            ):
                for task in list(self.data["tasks"].values()):
                    if (
                        task["enabled"]
                        and task["next_run"] is not None
                        and task["next_run"] <= time.time()
                    ):
                        if not any(
                            run["task_id"] == task["id"] and run["status"] in ACTIVE
                            for run in self.data["runs"]
                        ):
                            self.enqueue(task["id"], scheduled=True)
            await asyncio.sleep(15)

    async def _worker(self):
        while True:
            task, run = await self.queue.get()
            try:
                if run["status"] != "queued":
                    continue
                self.current_run = run
                self.current = asyncio.create_task(self._execute(task, run))
                try:
                    await self.current
                except asyncio.CancelledError:
                    if run["status"] in ACTIVE:
                        run.update(
                            status="interrupted" if self.closed else "cancelled",
                            finished_at=time.time(),
                            error="插件停止，同步未完成。" if self.closed else "",
                        )
                        self.persist()
                    if self.closed:
                        raise
            finally:
                self.current = self.current_run = None
                self.queue.task_done()

    async def _execute(self, task, run):
        workspace = self.work / run["id"]
        try:
            run.update(status="checking", started_at=time.time())
            self.persist()
            source, excludes = await self.prepare_local(task, workspace)
            run["status"] = "syncing"
            self.persist()
            last_persist = time.monotonic()

            def progress(stats):
                nonlocal last_persist
                run["progress"].update(stats)
                if time.monotonic() - last_persist >= 1:
                    self.persist()
                    last_persist = time.monotonic()

            async with self.cloud_lock:
                await self.cloud.transfer(
                    source,
                    task["remote_path"],
                    task["mode"],
                    excludes,
                    progress=progress,
                )
            run["status"] = "completed"
        except asyncio.CancelledError:
            run.update(
                status="interrupted" if self.closed else "cancelled",
                error="插件停止，同步未完成。" if self.closed else "",
            )
            raise
        except Exception as exc:
            run.update(
                status="failed",
                error=str(exc)
                if isinstance(exc, (SyncError, OSError))
                else "同步失败，请检查配置和存储连接。",
            )
        finally:
            run["finished_at"] = time.time()
            try:
                self.remove_workspace(workspace)
            except (OSError, SyncError):
                run["warning"] = "暂存目录清理失败，请检查本机文件权限。"
            self.persist()

    async def close(self):
        self.closed = True
        if self.install_task:
            self.install_task.cancel()
            await asyncio.gather(self.install_task, return_exceptions=True)
        for task in (self.scheduler, self.worker):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.scheduler, self.worker) if task),
            return_exceptions=True,
        )
        for run in self.data["runs"]:
            if run["status"] in ACTIVE:
                run.update(
                    status="interrupted",
                    error="插件停止，同步未完成。",
                    finished_at=time.time(),
                )
        self.persist()
        await self.configuration.close()
        await self.cloud.close()
