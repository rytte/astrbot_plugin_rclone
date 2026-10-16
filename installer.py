"""Download and verify official rclone builds into the plugin's private directory."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import httpx

from .configuration import ConfigProcess
from .files import SyncError
from .rclone import Rclone

DOWNLOAD_ROOT = "https://downloads.rclone.org"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024
INSTALLING = {"checking", "downloading", "verifying"}


def build_target() -> tuple[str, str]:
    system, machine = platform.system().lower(), platform.machine().lower()
    architectures = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "x86": "386",
        "i386": "386",
        "i686": "386",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "arm-v7",
        "armv6l": "arm-v6",
    }
    arch = architectures.get(machine)
    supported = {
        "windows": {"amd64", "386", "arm64"},
        "linux": {"amd64", "386", "arm64", "arm-v7", "arm-v6"},
        "darwin": {"amd64", "arm64"},
    }
    if arch not in supported.get(system, set()):
        raise SyncError(
            f"暂不支持自动安装此平台（{system}/{machine}），请从 rclone 官网手动安装。"
        )
    return system, arch


class RcloneInstaller:
    def __init__(self, directory: Path):
        self.directory = directory / "bin"
        self.state = {"status": "idle", "received": 0, "total": None, "error": ""}

    @property
    def active(self) -> bool:
        return self.state["status"] in INSTALLING

    async def _metadata(self, client: httpx.AsyncClient, url: str) -> str:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 64 * 1024:
                    raise SyncError("rclone 官方版本或校验信息过大，已停止安装。")
        try:
            return data.decode("utf-8")
        except UnicodeError:
            raise SyncError("rclone 官方版本或校验信息格式无效。") from None

    async def _check_binary(self, binary: Path, version: str) -> str:
        candidate = Rclone(
            binary.parent / "check.conf",
            {"binary": str(binary), "remote": "backup", "folder": "backup"},
        )
        process = ConfigProcess(candidate)
        try:
            result = await candidate.check_version()
            if result != f"rclone {version}":
                raise SyncError("下载的 rclone 程序版本与官方版本信息不一致。")
            await process.start(
                candidate.config_path,
                {"listen": "127.0.0.1", "gui_port": 0, "api_port": 0},
            )
            return result
        finally:
            await process.close()
            await candidate.close()

    async def install(self) -> tuple[str, str]:
        system, arch = build_target()
        if self.directory.is_symlink() or self.directory.is_junction():
            raise SyncError("rclone 安装目录不能是符号链接或目录联接。")
        self.directory.mkdir(exist_ok=True)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15), follow_redirects=False
        ) as client:
            text = (
                await self._metadata(client, DOWNLOAD_ROOT + "/version.txt")
            ).strip()
            match = re.fullmatch(r"rclone (v(\d+)\.(\d+)\.\d+)", text)
            if not match or (int(match[2]), int(match[3])) < (1, 74):
                raise SyncError("官方稳定版信息无效或版本低于 1.74，已停止安装。")
            version = match[1]
            stem = f"rclone-{version}-{system}-{arch}"
            filename = stem + ".zip"
            base = f"{DOWNLOAD_ROOT}/{version}"
            checksums = await self._metadata(client, base + "/SHA256SUMS")
            hashes = re.findall(
                r"^([0-9a-fA-F]{64}) [ *]" + re.escape(filename) + r"\r?$",
                checksums,
                re.MULTILINE,
            )
            if len(hashes) != 1:
                raise SyncError("官方校验列表中没有唯一匹配的安装包，已停止安装。")
            self.state.update(status="downloading", version=version)
            with tempfile.TemporaryDirectory(
                prefix=".download-", dir=self.directory
            ) as temporary:
                work = Path(temporary).resolve()
                if not work.is_relative_to(self.directory.resolve()):
                    raise SyncError("rclone 下载暂存目录超出安装目录。")
                archive = work / filename
                digest = hashlib.sha256()
                async with client.stream("GET", base + "/" + filename) as response:
                    response.raise_for_status()
                    length = response.headers.get("Content-Length", "")
                    total = (
                        int(length) if length.isascii() and length.isdigit() else None
                    )
                    if total is not None and total > MAX_ARCHIVE:
                        raise SyncError("rclone 安装包过大，已停止下载。")
                    self.state["total"] = total
                    with archive.open("wb") as stream:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            self.state["received"] += len(chunk)
                            if self.state["received"] > MAX_ARCHIVE:
                                raise SyncError("rclone 安装包过大，已停止下载。")
                            digest.update(chunk)
                            stream.write(chunk)
                if digest.hexdigest() != hashes[0].lower():
                    raise SyncError(
                        "rclone 安装包 SHA-256 校验失败，已停止安装，请重试。"
                    )
                self.state["status"] = "verifying"
                executable = "rclone.exe" if system == "windows" else "rclone"
                binary = work / executable
                try:
                    with zipfile.ZipFile(archive) as package:
                        entries = [
                            item
                            for item in package.infolist()
                            if item.filename == f"{stem}/{executable}"
                        ]
                        if (
                            len(entries) != 1
                            or not 0 < entries[0].file_size <= MAX_BINARY
                        ):
                            raise SyncError("安装包中的 rclone 程序缺失或大小无效。")
                        with (
                            package.open(entries[0]) as source,
                            binary.open("wb") as out,
                        ):
                            shutil.copyfileobj(source, out, length=64 * 1024)
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                    raise SyncError("rclone 安装包无法解压，请重新下载。") from None
                binary.chmod(0o700)
                checked = await self._check_binary(binary, version)
                target = self.directory / (
                    stem + (".exe" if system == "windows" else "")
                )
                if target.is_symlink() or target.is_junction():
                    raise SyncError("rclone 程序目标不能是符号链接或目录联接。")
                os.replace(binary, target)
                return str(target.resolve()), checked
