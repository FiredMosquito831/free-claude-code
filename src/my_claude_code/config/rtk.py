"""Persisted RTK token-optimizer state and machine reconciliation."""

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .paths import config_dir_path

RTK_STATE_FILENAME = "rtk.json"
RTK_VERSION = "0.44.2"
RTK_RELEASE_BASE_URL = f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}"
RTK_TELEMETRY_ENV = "RTK_TELEMETRY_DISABLED"

_RELEASES: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        "rtk-x86_64-unknown-linux-musl.tar.gz",
        "d94cc2a3e57fa534892b5235a726e7eeb7523f205a5f8f48f853bfcae7be7e33",
    ),
    ("linux", "aarch64"): (
        "rtk-aarch64-unknown-linux-gnu.tar.gz",
        "5cd3f7fa2697faf9e5b77a10ce4e699006e02d4752d792f06550697eb4b8e8a9",
    ),
    ("darwin", "x86_64"): (
        "rtk-x86_64-apple-darwin.tar.gz",
        "636f808db86b2cefab7db7dd9393da8b6e4721bb2ffaa0644e3ffa52d3420d81",
    ),
    ("darwin", "aarch64"): (
        "rtk-aarch64-apple-darwin.tar.gz",
        "b7c2218eca538b54e63fa594a8ce58bd3716851b01b3b0dc026515323baf6393",
    ),
    ("win32", "x86_64"): (
        "rtk-x86_64-pc-windows-msvc.zip",
        "3a1e114edce9080f8a10663e9c87488363a82f14a5ca8aab2ad416817f89d47c",
    ),
}

_ENABLE_COMMANDS: dict[str, tuple[str, ...]] = {
    "claude": ("init", "-g", "--auto-patch"),
    "codex": ("init", "-g", "--codex"),
    "pi": ("init", "-g", "--agent", "pi"),
}
_UNINSTALL_COMMANDS: dict[str, tuple[str, ...]] = {
    "claude": ("init", "-g", "--uninstall"),
    "codex": ("init", "--uninstall", "-g", "--codex"),
    "pi": ("init", "--uninstall", "-g", "--agent", "pi"),
}


class RtkError(Exception):
    """Raised when RTK state cannot be persisted or reconciled."""


@dataclass(frozen=True)
class RtkState:
    """Desired RTK integration state for each supported coding agent."""

    claude: bool = False
    codex: bool = False
    pi: bool = False


def rtk_state_path() -> Path:
    """Return the persisted desired-state path."""

    return config_dir_path() / RTK_STATE_FILENAME


def load_rtk_state() -> RtkState:
    """Load desired RTK state, returning defaults for missing or corrupt data."""

    try:
        data = json.loads(rtk_state_path().read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return RtkState()
    if not isinstance(data, dict):
        return RtkState()

    values = asdict(RtkState())
    for name in values:
        if isinstance(data.get(name), bool):
            values[name] = data[name]
    return RtkState(**values)


def save_rtk_state(state: RtkState) -> None:
    """Persist desired RTK state atomically."""

    path = rtk_state_path()
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(asdict(state)), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise RtkError(f"Failed to save RTK state: {exc}") from exc


def _normalized_architecture(machine: str) -> str:
    architecture = machine.strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(architecture, architecture)


def _release_for_current_platform() -> tuple[str, str]:
    key = (sys.platform, _normalized_architecture(platform.machine()))
    release = _RELEASES.get(key)
    if release is None:
        raise RtkError(
            f"RTK {RTK_VERSION} has no release for {sys.platform} "
            f"{platform.machine() or 'unknown architecture'}."
        )
    return release


def _managed_binary_path() -> Path:
    filename = "rtk.exe" if sys.platform == "win32" else "rtk"
    return Path.home() / ".local" / "bin" / filename


def _verify_rtk(binary: str | Path) -> str | None:
    env = os.environ.copy()
    env[RTK_TELEMETRY_ENV] = "1"
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RtkError(f"Could not run RTK at {binary}: {exc}") from exc
    version = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        raise RtkError(
            f"RTK verification failed at {binary} with exit code "
            f"{completed.returncode}."
        )
    return version or None


def _extract_binary(archive_path: Path, asset_name: str, destination: Path) -> None:
    executable_name = "rtk.exe" if asset_name.endswith(".zip") else "rtk"
    try:
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                members = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and PurePosixPath(info.filename).name == executable_name
                ]
                if len(members) != 1:
                    raise RtkError(
                        f"Verified RTK archive must contain exactly one {executable_name}."
                    )
                with (
                    archive.open(members[0]) as source,
                    destination.open("wb") as target,
                ):
                    shutil.copyfileobj(source, target)
        else:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = [
                    member
                    for member in archive.getmembers()
                    if member.isfile()
                    and PurePosixPath(member.name).name == executable_name
                ]
                if len(members) != 1:
                    raise RtkError(
                        f"Verified RTK archive must contain exactly one {executable_name}."
                    )
                source = archive.extractfile(members[0])
                if source is None:
                    raise RtkError("Verified RTK executable could not be extracted.")
                with source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
    except RtkError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RtkError(f"Could not extract verified RTK archive: {exc}") from exc

    if destination.stat().st_size == 0:
        raise RtkError("Verified RTK executable was empty.")


def _ensure_rtk_binary() -> Path:
    """Return a verified RTK binary, installing the pinned release if absent."""

    existing = shutil.which("rtk")
    if existing is not None:
        _verify_rtk(existing)
        return Path(existing)

    asset_name, expected_sha256 = _release_for_current_platform()
    url = f"{RTK_RELEASE_BASE_URL}/{asset_name}"
    destination = _managed_binary_path()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mcc-rtk-") as temporary_directory:
        archive_path = Path(temporary_directory) / asset_name
        temporary_binary = destination.with_name(f".{destination.name}.tmp")
        try:
            try:
                with (
                    urllib.request.urlopen(url, timeout=60) as response,
                    archive_path.open("wb") as archive_file,
                ):
                    shutil.copyfileobj(response, archive_file)
            except OSError as exc:
                raise RtkError(f"Could not download RTK {RTK_VERSION}: {exc}") from exc

            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if digest != expected_sha256:
                raise RtkError(
                    f"RTK checksum verification failed for {asset_name}: "
                    f"expected {expected_sha256}, got {digest}."
                )

            _extract_binary(archive_path, asset_name, temporary_binary)
            temporary_binary.chmod(
                temporary_binary.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
            os.replace(temporary_binary, destination)
        except RtkError:
            with suppress(OSError):
                temporary_binary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            with suppress(OSError):
                temporary_binary.unlink(missing_ok=True)
            raise RtkError(f"Could not install RTK at {destination}: {exc}") from exc

    _verify_rtk(destination)
    return destination


def _available_binary() -> Path | None:
    discovered = shutil.which("rtk")
    if discovered is not None:
        return Path(discovered)
    managed = _managed_binary_path()
    return managed if managed.is_file() else None


def _run_rtk(binary: Path, arguments: tuple[str, ...]) -> None:
    env = os.environ.copy()
    env[RTK_TELEMETRY_ENV] = "1"
    try:
        subprocess.run(
            [str(binary), *arguments],
            check=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RtkError(f"RTK command failed: {' '.join(arguments)}: {exc}") from exc


def _ensure_claude_config_directory() -> None:
    """Create the Claude Code config directory RTK writes its hooks into.

    ``rtk init --auto-patch`` writes RTK.md next to the Claude settings file, but
    it does not create the directory first, so an RTK enable on a machine that
    has never run Claude Code fails. Mirror the upstream installer's pre-step.
    """

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(configured) if configured else Path.home() / ".claude"
    path.mkdir(parents=True, exist_ok=True)


def apply_rtk_state(state: RtkState, *, uninstall: bool = False) -> None:
    """Reconcile installed RTK hooks and optionally remove its managed binary."""

    any_enabled = state.claude or state.codex or state.pi
    binary = _ensure_rtk_binary() if any_enabled else _available_binary()

    if binary is not None:
        if state.claude:
            _ensure_claude_config_directory()
        for agent in ("claude", "codex", "pi"):
            enabled = getattr(state, agent)
            command = _ENABLE_COMMANDS[agent] if enabled else _UNINSTALL_COMMANDS[agent]
            _run_rtk(binary, command)

    if uninstall:
        managed = _managed_binary_path()
        try:
            managed.unlink(missing_ok=True)
        except OSError as exc:
            raise RtkError(f"Could not remove RTK binary at {managed}: {exc}") from exc


def rtk_status() -> dict[str, bool | str | None]:
    """Return desired agent state and verified binary metadata."""

    state = load_rtk_state()
    binary = _available_binary()
    version: str | None = None
    installed = False
    if binary is not None:
        try:
            version = _verify_rtk(binary)
            installed = True
        except RtkError:
            pass
    return {
        "installed": installed,
        "claude": state.claude,
        "codex": state.codex,
        "pi": state.pi,
        "binary_path": str(binary) if binary is not None else None,
        "version": version,
    }
