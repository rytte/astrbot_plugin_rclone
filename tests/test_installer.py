"""Verify platform selection, trusted downloads and installation failure isolation."""

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from astrbot_plugin_rclone import installer as module
from astrbot_plugin_rclone.files import SyncError
from astrbot_plugin_rclone.installer import RcloneInstaller, build_target


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Windows", "AMD64", ("windows", "amd64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Linux", "armv7l", ("linux", "arm-v7")),
        ("Darwin", "arm64", ("darwin", "arm64")),
    ],
)
def test_platform_uses_the_server_architecture(monkeypatch, system, machine, expected):
    monkeypatch.setattr(module.platform, "system", lambda: system)
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    assert build_target() == expected


def test_unsupported_platform_fails_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "unknown-cpu")
    with pytest.raises(SyncError, match="暂不支持"):
        build_target()


def package_bytes(member="rclone-v1.75.2-windows-amd64/rclone.exe"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as package:
        package.writestr(member, b"candidate-binary")
        package.writestr("../outside.exe", b"must-not-extract")
    return data.getvalue()


def serve_download(monkeypatch, archive, *, checksum=None, version="rclone v1.75.2"):
    digest = hashlib.sha256(archive).hexdigest() if checksum is None else checksum
    filename = "rclone-v1.75.2-windows-amd64.zip"
    requests = []

    def respond(request):
        requests.append(str(request.url))
        assert (
            request.url.scheme == "https" and request.url.host == "downloads.rclone.org"
        )
        bodies = {
            "/version.txt": version.encode(),
            "/v1.75.2/SHA256SUMS": (
                "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA1\n\n"
                + digest
                + "  "
                + filename
                + "\n-----BEGIN PGP SIGNATURE-----\n"
            ).encode(),
            "/v1.75.2/" + filename: archive,
        }
        return httpx.Response(200, content=bodies[request.url.path])

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    monkeypatch.setattr(module, "build_target", lambda: ("windows", "amd64"))
    return requests


async def test_install_publishes_only_the_validated_binary(tmp_path, monkeypatch):
    requests = serve_download(monkeypatch, package_bytes())
    install = RcloneInstaller(tmp_path)
    install.state["status"] = "checking"
    validated = []

    async def check(binary, version):
        assert binary.read_bytes() == b"candidate-binary"
        assert version == "v1.75.2"
        assert not (tmp_path / "outside.exe").exists()
        validated.append(binary)
        return "rclone v1.75.2"

    monkeypatch.setattr(install, "_check_binary", check)
    binary, version = await install.install()
    assert (
        Path(binary).read_bytes() == b"candidate-binary" and version == "rclone v1.75.2"
    )
    assert len(requests) == 3 and validated
    assert list(install.directory.iterdir()) == [Path(binary)]


@pytest.mark.parametrize("failure", ["checksum", "member", "validation", "version"])
async def test_failed_install_keeps_existing_program_and_cleans_downloads(
    tmp_path, monkeypatch, failure
):
    archive = (
        package_bytes("wrong/rclone.exe") if failure == "member" else package_bytes()
    )
    requests = serve_download(
        monkeypatch,
        archive,
        checksum="0" * 64 if failure == "checksum" else None,
        version="rclone v1.73.0" if failure == "version" else "rclone v1.75.2",
    )
    install = RcloneInstaller(tmp_path)
    install.directory.mkdir()
    existing = install.directory / "rclone-v1.75.2-windows-amd64.exe"
    existing.write_bytes(b"original-program")

    async def reject(*args):
        raise SyncError("GUI 检查失败")

    monkeypatch.setattr(install, "_check_binary", reject)
    with pytest.raises(SyncError):
        await install.install()
    assert existing.read_bytes() == b"original-program"
    assert list(install.directory.iterdir()) == [existing]
    assert len(requests) == (1 if failure == "version" else 3)


async def test_official_package_passes_real_version_and_gui_checks(
    tmp_path, monkeypatch
):
    archive = Path(__file__).parent / "tools" / "rclone-v1.75.2-windows-amd64.zip"
    if module.platform.system() != "Windows" or not archive.is_file():
        pytest.skip("Requires the cached official Windows rclone package")
    serve_download(monkeypatch, archive.read_bytes())
    install = RcloneInstaller(tmp_path)
    binary, version = await install.install()
    assert Path(binary).is_file() and version == "rclone v1.75.2"
    assert list(install.directory.iterdir()) == [Path(binary)]
