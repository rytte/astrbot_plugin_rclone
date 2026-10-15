"""Backend-independent transfers through a private rclone configuration."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from .files import SyncError, check_keys

ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
CONNECTION_FIELDS = {"binary", "remote", "folder"}
DEFAULT_CONNECTION = {
    "binary": "rclone",
    "remote": "backup",
    "folder": "AstrBotSync",
}
UPLOAD_MODES = {"upload", "upload_mirror"}
MIRROR_MODES = {"upload_mirror", "download_mirror"}
MODES = UPLOAD_MODES | {"download", "download_mirror"}


def opaque_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise SyncError("无效的任务或执行记录 ID。")
    return value


def relative_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value != value.strip()
        or any(c in value for c in '\\:*?"<>|')
        or any(ord(c) < 32 for c in value)
        or any(
            part in ("", ".", "..") or part.endswith((" ", "."))
            for part in value.split("/")
        )
    ):
        raise SyncError(
            "云端目录必须是非空相对路径，使用 / 分隔，不能包含 ..、冒号或特殊字符。"
        )
    return value


def validate_connection(value: dict) -> dict:
    check_keys(value, CONNECTION_FIELDS, CONNECTION_FIELDS)
    binary = value["binary"]
    if not isinstance(binary, str) or not binary.strip() or len(binary) > 4096:
        raise SyncError("请填写 rclone 或 rclone 可执行文件的绝对路径。")
    binary = binary.strip()
    if binary != "rclone" and not Path(binary).is_absolute():
        raise SyncError("rclone 路径必须为 rclone 或绝对路径，不能附带命令参数。")
    remote = value["remote"]
    if not isinstance(remote, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", remote
    ):
        raise SyncError(
            "remote 名称须以字母、数字或下划线开头，仅包含字母、数字、下划线和连字符，最多 64 个字符。"
        )
    return {
        "binary": binary,
        "remote": remote,
        "folder": relative_path(value["folder"]),
    }


class Rclone:
    """Execute rclone without a shell and never expose credential-bearing output."""

    def __init__(self, config_path: Path, settings: dict | None = None):
        self.config_path = config_path
        self.settings = validate_connection(settings or dict(DEFAULT_CONNECTION))
        self.connected = False
        self.error = ""
        self.version = ""
        self.processes: set = set()
        self.closed = False

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "error": self.error,
            "version": self.version,
        }

    def configure(self, settings: dict) -> None:
        self.settings = validate_connection(settings)
        self.connected = False
        self.error = ""
        self.version = ""

    def executable(self) -> str:
        binary = self.settings["binary"]
        found = shutil.which("rclone") if binary == "rclone" else binary
        if not found or not Path(found).is_file():
            raise SyncError(
                "找不到 rclone。请在 AstrBot 所在机器或容器安装 rclone，或填写其绝对路径。"
            )
        path = Path(found).resolve()
        if os.name == "nt" and path.suffix.lower() != ".exe":
            raise SyncError("Windows 上必须选择 rclone.exe，不能使用批处理或命令脚本。")
        return str(path)

    async def _run(
        self,
        *args: str,
        timeout: float = 120,
        limit: int = 8 * 1024 * 1024,
        progress: Callable[[dict], None] | None = None,
    ) -> bytes:
        if self.closed:
            raise SyncError("rclone 连接正在关闭。")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("RCLONE_")
        }
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable(),
                *args,
                "--config",
                str(self.config_path),
                "--ask-password=false",
                "--use-json-log",
                "--log-level",
                "INFO" if progress else "ERROR",
                "--stats",
                "1s" if progress else "0",
                "--retries",
                "3",
                "--low-level-retries",
                "5",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                limit=1024 * 1024,
                **options,
            )
        except OSError:
            raise SyncError("无法启动 rclone，请检查可执行文件及运行权限。") from None
        self.processes.add(process)

        async def output():
            chunks, total = [], 0
            while chunk := await process.stdout.read(64 * 1024):
                total += len(chunk)
                if total > limit:
                    raise SyncError("rclone 输出过大，请缩小任务范围。")
                chunks.append(chunk)
            return b"".join(chunks)

        async def diagnostics():
            while line := await process.stderr.readline():
                if not progress:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                stats = record.get("stats") if isinstance(record, dict) else None
                if not isinstance(stats, dict):
                    continue
                safe = {}
                for key in (
                    "bytes",
                    "totalBytes",
                    "transfers",
                    "totalTransfers",
                    "checks",
                    "totalChecks",
                    "deletes",
                    "deletedDirs",
                    "speed",
                    "elapsedTime",
                    "eta",
                    "errors",
                ):
                    value = stats.get(key)
                    if (
                        type(value) in (int, float)
                        and math.isfinite(value)
                        and value >= 0
                    ):
                        safe[key] = value
                progress(safe)

        readers = [asyncio.create_task(output()), asyncio.create_task(diagnostics())]
        try:
            async with asyncio.timeout(timeout):
                result, _ = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode != 0:
                raise SyncError(
                    f"rclone 操作失败（退出码 {process.returncode}）。请检查源文件读取权限、网络、存储空间及连接授权。"
                )
            return result
        except TimeoutError:
            raise SyncError(
                "rclone 操作超时，请检查网络或缩小任务范围后重试。"
            ) from None
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            self.processes.discard(process)

    @staticmethod
    def decode(output: bytes) -> Any:
        try:
            return json.loads(output)
        except (ValueError, UnicodeError):
            raise SyncError("rclone 返回了无效的 JSON 数据。") from None

    async def check_version(self) -> str:
        try:
            version = (
                (await self._run("version", timeout=15)).decode("utf-8").splitlines()[0]
            )
        except (UnicodeError, IndexError):
            raise SyncError("无法识别 rclone 版本，请检查所选可执行文件。") from None
        match = re.fullmatch(r"rclone v(\d+)\.(\d+)(?:\..*)?", version)
        if not match or (int(match[1]), int(match[2])) < (1, 74):
            raise SyncError(
                "需要 rclone 1.74 或更新版本（包含官方 GUI），请升级 rclone。"
            )
        self.version = version
        return version

    async def check_connection(self) -> dict:
        self.connected = False
        try:
            await self.check_version()
            if (
                not self.config_path.is_file()
                or self.config_path.is_symlink()
                or self.config_path.is_junction()
            ):
                raise SyncError(
                    "尚未建立私有 rclone 配置。请按页面中的首次登录步骤创建 rclone.conf。"
                )
            with self.config_path.open("rb") as stream:
                if stream.read(128).startswith(b"RCLONE_ENCRYPT_V0"):
                    raise SyncError(
                        "当前不支持加密的 rclone 配置，请用 rclone 配置工具明确移除配置加密，并保护文件权限。"
                    )
            self.config_path.chmod(0o600)
            config = self.decode(await self._run("config", "dump", timeout=15))
            entry = (
                config.get(self.settings["remote"])
                if isinstance(config, dict)
                else None
            )
            if not isinstance(entry, dict) or not entry.get("type"):
                raise SyncError("指定 remote 不存在，请先在存储连接中完成配置。")
            features = self.decode(
                await self._run("backend", "features", self.settings["remote"] + ":")
            )
            if not isinstance(features, dict) or not isinstance(
                features.get("Features"), dict
            ):
                raise SyncError("rclone 返回了无效的后端能力信息。")
            base = self.settings["remote"] + ":"
            if features["Features"].get("BucketBased"):
                # S3-like credentials may only allow access to one bucket.
                parts = self.settings["folder"].split("/")
                if len(parts) < 2:
                    raise SyncError(
                        "对象存储的同步根目录请填写 桶名/同步前缀，不能只填写桶名。"
                    )
                base += parts[0]
            # Detection never creates a bucket or backup directory.
            await self._run(
                "lsjson",
                base,
                "--dirs-only",
                "--max-depth",
                "1",
                timeout=60,
            )
            self.connected, self.error = True, ""
            return self.status()
        except SyncError as exc:
            self.error = str(exc)
            raise
        except OSError:
            self.error = (
                "无法读取或保护 rclone.conf，请检查插件私有目录和配置文件的访问权限。"
            )
            raise SyncError(self.error) from None

    def remote_path(self, path: str) -> str:
        path = relative_path(path)
        root = self.settings["folder"]
        if path != root and not path.startswith(root + "/"):
            raise SyncError("云端路径超出配置的同步根目录，拒绝操作。")
        return self.settings["remote"] + ":" + path

    def require_connected(self) -> None:
        if not self.connected:
            raise SyncError("请先在插件页面检测并连接存储。")

    async def require_remote_source(self, target: str) -> None:
        full = self.settings["folder"] + "/" + relative_path(target)
        parent, name = full.rsplit("/", 1)
        entries = self.decode(
            await self._run(
                "lsjson", self.remote_path(parent), "--dirs-only", "--max-depth", "1"
            )
        )
        if not isinstance(entries, list):
            raise SyncError("云端来源目录检查返回了无效的数据，已停止下载。")
        if not any(
            isinstance(entry, dict)
            and entry.get("Name") == name
            and entry.get("IsDir") is True
            for entry in entries
        ):
            raise SyncError("云端来源目录不存在，已停止下载，未将其视为空目录。")

    async def transfer(
        self,
        local: Path,
        remote: str,
        mode: str,
        excludes: list[str],
        *,
        dry_run: bool = False,
        progress: Callable[[dict], None] | None = None,
    ) -> dict:
        self.require_connected()
        if mode not in MODES:
            raise SyncError("不支持的同步模式，请明确选择四种模式之一。")
        cloud_path = self.remote_path(
            self.settings["folder"] + "/" + relative_path(remote)
        )
        if mode in UPLOAD_MODES:
            source, destination = str(local), cloud_path
        else:
            await self.require_remote_source(remote)
            source, destination = cloud_path, str(local)
        args = [
            "sync" if mode in MIRROR_MODES else "copy",
            source,
            destination,
            "--create-empty-src-dirs",
        ]
        if mode in MIRROR_MODES:
            args.append("--delete-after")
        for pattern in excludes:
            args.extend(("--exclude", pattern))
        if dry_run:
            args.extend(("--dry-run", "--combined", "-"))
        output = await self._run(*args, timeout=1800, progress=progress)
        if not dry_run:
            return {}
        counts = {"add": 0, "update": 0, "delete": 0, "unchanged": 0}
        changes = []
        actions = {"+": "add", "*": "update", "-": "delete", "=": "unchanged"}
        for line in output.decode("utf-8").splitlines():
            if len(line) < 2 or line[0] not in actions:
                if line.startswith("!"):
                    raise SyncError("预览发现无法读取或比较的文件，已停止同步。")
                continue
            if line[0] == "-" and mode not in MIRROR_MODES:
                continue
            action = actions[line[0]]
            counts[action] += 1
            if action != "unchanged" and len(changes) < 200:
                changes.append({"action": action, "path": line[2:]})
        return {
            "counts": counts,
            "changes": changes,
            "truncated": sum(counts[key] for key in ("add", "update", "delete"))
            > len(changes),
        }

    async def disconnect(self) -> None:
        # The configuration is user-managed; disconnecting only pauses this plugin.
        self.connected = False
        self.error = ""

    async def close(self) -> None:
        self.closed = True
        self.connected = False
        processes = list(self.processes)
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
        await asyncio.gather(
            *(process.wait() for process in processes), return_exceptions=True
        )
