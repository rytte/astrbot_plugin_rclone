"""Validate local paths and stage individual files for directory transfers."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class SyncError(ValueError):
    """An actionable configuration or synchronization error."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def check_keys(value: Any, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise SyncError("配置必须是 JSON 对象。")
    unknown, missing = set(value) - allowed, required - set(value)
    if unknown or missing:
        raise SyncError(
            "配置字段不正确："
            + ("未知字段 " + ", ".join(sorted(unknown)) + "；" if unknown else "")
            + ("缺少字段 " + ", ".join(sorted(missing)) if missing else "")
        )
    return value


def source_path(value: Any, root: Path, private: Path) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise SyncError("请填写一个本机文件或目录路径，长度不超过 4096。")
    path = Path(value.strip()).expanduser()
    path = path if path.is_absolute() else root / path
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise SyncError("同步来源不能是符号链接或目录联接。")
    try:
        path = path.resolve(strict=True)
        if path.is_relative_to(private):
            raise SyncError("不能同步本插件自己的状态、令牌或暂存文件。")
        if path.is_dir():
            with os.scandir(path) as entries:
                next(entries, None)
        elif path.is_file():
            with path.open("rb") as stream:
                stream.read(1)
        else:
            raise SyncError("同步来源必须是普通文件或目录。")
    except OSError:
        raise SyncError(
            "本机来源不存在或无法读取，已停止同步，未将其视为空目录。"
        ) from None
    return path


def destination_path(value: Any, root: Path, private: Path) -> Path:
    """Validate a download directory without creating it, including for dry runs."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise SyncError("请填写一个本机接收目录，长度不超过 4096。")
    path = Path(value.strip()).expanduser()
    path = path if path.is_absolute() else root / path
    try:
        for component in (path, *path.parents):
            if component.is_symlink() or component.is_junction():
                raise SyncError("本机接收目录及父目录不能是符号链接或目录联接。")
        path = path.resolve()
        if path == Path(path.anchor):
            raise SyncError("不能将磁盘或文件系统根目录作为下载目标，请使用专用目录。")
        if path.is_relative_to(private):
            raise SyncError("不能下载到本插件自己的状态、令牌或暂存目录。")
        if path.exists():
            if not path.is_dir():
                raise SyncError("下载模式的本机路径必须是目录，不能是文件。")

            def unreadable(error):
                raise error

            for parent, directories, files in os.walk(path, onerror=unreadable):
                for name in directories + files:
                    child = Path(parent) / name
                    if child.is_symlink() or child.is_junction():
                        raise SyncError(
                            "本机接收目录内部存在链接，请使用不含链接的专用目录。"
                        )
    except OSError:
        raise SyncError("本机接收目录无法检查，请确认路径和访问权限。") from None
    return path


def stage_file(source: Path, destination: Path, cancelled: threading.Event) -> Path:
    """Copy one file without exposing its parent directory to rclone sync."""
    destination.mkdir(parents=True)
    before = source.stat()
    artifact = destination / source.name
    with source.open("rb") as original, artifact.open("wb") as output:
        while not cancelled.is_set():
            chunk = original.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    if cancelled.is_set():
        raise SyncError("同步已取消。")
    after = source.stat()

    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    if identity(before) != identity(after):
        raise SyncError("源文件在读取期间发生变化，请停止写入或使用已导出的文件。")
    os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns))
    return destination
