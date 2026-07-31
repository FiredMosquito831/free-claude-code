"""Report the running version and upgrade to the latest published release.

The upgrade path deliberately mirrors ``scripts/install.sh``: fetch the wheel
published for the latest tag, verify its SHA-256, then hand it to
``uv tool install --force``. Reusing that shape keeps a dashboard-triggered
upgrade byte-identical to one done from the command line, checksum
verification included.

Upgrading never restarts the server. A running process keeps serving the code
it already imported, so the caller is told a restart is required and chooses
when to take the downtime.

Windows cannot replace the environment underneath a running process: the
interpreter and its loaded DLLs are held open, so ``uv tool install --force``
fails partway through removing the old environment and leaves it broken. There
the install is therefore *deferred* -- the verified wheel is staged and a
detached helper installs it once this process exits. POSIX unlinks open files
happily, so it keeps installing in place.
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from free_claude_code.config.paths import config_dir_path

PACKAGE_NAME = "free-claude-code"
# Kept in step with the URLs in scripts/install.sh and scripts/install.ps1.
RELEASE_REPO = "FiredMosquito831/free-claude-code"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
_CACHE_TTL_SECONDS = 6 * 3600.0
_HTTP_TIMEOUT_SECONDS = 10.0
_UPGRADE_TIMEOUT_SECONDS = 900.0
_WHEEL_SUFFIX = ".whl"
_WINDOWS = os.name == "nt"
_STAGE_DIRNAME = "updates"
_PENDING_RESULT_FILENAME = "pending-upgrade.json"
# Bound on how long the helper waits for this process to exit before giving up,
# so a server left running forever does not leave a helper resident forever.
_HELPER_WAIT_SECONDS = 3600


def current_version() -> str:
    """Version of the running package, or ``unknown`` outside an install."""
    try:
        return installed_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def parse_version(text: str | None) -> tuple[int, ...]:
    """Parse ``4.14.2`` or ``v4.14.2`` into a comparable tuple.

    Compares numerically rather than lexically so 4.14.10 correctly sorts
    above 4.14.9. Unparseable input sorts lowest so it never looks newer.
    """
    if not text:
        return ()
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """Whether ``candidate`` is a strictly newer release than ``baseline``."""
    parsed_candidate = parse_version(candidate)
    parsed_baseline = parse_version(baseline)
    if not parsed_candidate or not parsed_baseline:
        return False
    return parsed_candidate > parsed_baseline


@dataclass(slots=True)
class ReleaseStatus:
    """What the dashboard needs to render the version and update banner."""

    current: str
    latest: str | None = None
    update_available: bool = False
    release_url: str | None = None
    release_name: str | None = None
    release_notes: str | None = None
    published_at: str | None = None
    checked_at: float | None = None
    restart_required: bool = False
    staged_install: bool = False
    # Outcome of a deferred (Windows) install that ran after a shutdown.
    pending_upgrade: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "release_name": self.release_name,
            "release_notes": self.release_notes,
            "published_at": self.published_at,
            "checked_at": self.checked_at,
            "restart_required": self.restart_required,
            "staged_install": self.staged_install,
            "pending_upgrade": self.pending_upgrade,
            "error": self.error,
        }


@dataclass(slots=True)
class UpgradeResult:
    """Outcome of one upgrade attempt."""

    ok: bool
    message: str
    installed_version: str | None = None
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "installed_version": self.installed_version,
            "log": self.log,
        }


class _ReleaseCache:
    """Cache the release lookup and collapse concurrent checks into one call."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._payload: dict[str, Any] | None = None
        self._checked_at: float | None = None
        self._error: str | None = None
        self.restart_required = False
        # True when the install was staged for shutdown rather than applied.
        self.staged_install = False

    async def get(
        self, *, force: bool
    ) -> tuple[dict[str, Any] | None, float | None, str | None]:
        async with self._lock:
            fresh = (
                self._checked_at is not None
                and time.time() - self._checked_at < _CACHE_TTL_SECONDS
            )
            if fresh and not force:
                return self._payload, self._checked_at, self._error
            payload, error = await _fetch_latest_release()
            if payload is not None or not fresh:
                # Keep the last good payload when a refresh fails, so a
                # transient network problem does not blank the version panel.
                self._payload = payload if payload is not None else self._payload
                self._checked_at = time.time()
                self._error = error
            return self._payload, self._checked_at, self._error


_CACHE = _ReleaseCache()


async def _fetch_latest_release() -> tuple[dict[str, Any] | None, str | None]:
    """Read the latest release, returning ``(payload, error)``; never raises."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                _LATEST_RELEASE_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Offline or rate-limited is an expected, non-fatal condition: the
        # dashboard still renders the running version.
        logger.debug("Release check failed: {}", type(exc).__name__)
        return None, f"Could not reach the release feed ({type(exc).__name__})."
    if not isinstance(payload, dict):
        return None, "Unexpected release feed response."
    return payload, None


async def get_release_status(*, force: bool = False) -> ReleaseStatus:
    """Current version plus the latest published release, best effort."""
    running = current_version()
    payload, checked_at, error = await _CACHE.get(force=force)
    status = ReleaseStatus(
        current=running,
        checked_at=checked_at,
        error=error,
        restart_required=_CACHE.restart_required,
        staged_install=_CACHE.staged_install,
        pending_upgrade=pending_upgrade_result(),
    )
    if payload is None:
        return status
    latest = str(payload.get("tag_name") or "").lstrip("vV") or None
    status.latest = latest
    status.release_url = payload.get("html_url")
    status.release_name = payload.get("name")
    status.release_notes = _release_notes(payload.get("body"))
    status.published_at = payload.get("published_at")
    status.update_available = is_newer(latest, running)
    return status


def _receipt_path() -> Path:
    return (
        Path.home()
        / ".local"
        / "share"
        / "uv"
        / "tools"
        / PACKAGE_NAME
        / "uv-receipt.toml"
    )


def _installed_extras_and_python() -> tuple[list[str], str]:
    """Recover the extras and Python pin uv recorded for this install.

    Reinstalling without them would silently drop optional features such as
    voice support, so they are carried across the upgrade.
    """
    default_python = ".".join(str(part) for part in sys.version_info[:3])
    receipt = _receipt_path()
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return [], default_python
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return [], default_python
    python = str(tool.get("python") or default_python)
    extras: list[str] = []
    requirements = tool.get("requirements")
    if isinstance(requirements, list):
        for requirement in requirements:
            if (
                isinstance(requirement, dict)
                and requirement.get("name") == PACKAGE_NAME
                and isinstance(requirement.get("extras"), list)
            ):
                extras = [str(extra) for extra in requirement["extras"]]
                break
    return extras, python


def _select_wheel_asset(payload: dict[str, Any]) -> dict[str, Any] | None:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if isinstance(asset, dict) and str(asset.get("name", "")).endswith(
            _WHEEL_SUFFIX
        ):
            return asset
    return None


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage_dir() -> Path:
    return config_dir_path() / _STAGE_DIRNAME


def pending_upgrade_result() -> dict[str, Any] | None:
    """Outcome written by a deferred (Windows) upgrade, if one has finished.

    Read on status so a deferred install that failed after this process exited
    is still reported instead of vanishing.
    """

    path = _stage_dir() / _PENDING_RESULT_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_pending_upgrade_result() -> None:
    """Drop a consumed deferred-upgrade outcome."""

    with suppress(OSError):
        (_stage_dir() / _PENDING_RESULT_FILENAME).unlink()


def _powershell_literal(value: str) -> str:
    """Quote a value for a PowerShell single-quoted string."""

    return "'" + value.replace("'", "''") + "'"


def _deferred_helper_script(
    *,
    uv_executable: str,
    command: list[str],
    result_path: Path,
    stage_dir: Path,
) -> str:
    """PowerShell that waits for this process to exit, then installs.

    Written as PowerShell rather than Python because the only interpreter we
    can rely on is the one inside the environment being replaced -- using it
    would hold the very directory uv needs to delete.
    """

    quoted_args = ", ".join(_powershell_literal(arg) for arg in command[1:])
    return f"""$ErrorActionPreference = 'Stop'
$parent = {os.getpid()}
$deadline = (Get-Date).AddSeconds({_HELPER_WAIT_SECONDS})
while ((Get-Date) -lt $deadline) {{
    if (-not (Get-Process -Id $parent -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 500
}}
if (Get-Process -Id $parent -ErrorAction SilentlyContinue) {{
    $result = @{{ ok = $false; message = 'Timed out waiting for the server to stop.' }}
    $result | ConvertTo-Json | Set-Content -Path {_powershell_literal(str(result_path))} -Encoding utf8
    exit 1
}}
# Give Windows a moment to release the handles the exiting process held.
Start-Sleep -Seconds 2
# uv writes progress to stderr. Under ErrorActionPreference='Stop' a native
# command's stderr becomes a *terminating* NativeCommandError, which would kill
# this script before it installs anything, so drop back to Continue for the
# call itself and judge the result by exit code alone.
$ErrorActionPreference = 'Continue'
$output = & {_powershell_literal(uv_executable)} {quoted_args} 2>&1 | Out-String
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$ok = $code -eq 0
$result = @{{
    ok = $ok
    exit_code = $code
    message = if ($ok) {{ 'Deferred install completed.' }} else {{ 'Deferred install failed.' }}
    output = $output
}}
$result | ConvertTo-Json | Set-Content -Path {_powershell_literal(str(result_path))} -Encoding utf8
if ($ok) {{ Remove-Item -Path {_powershell_literal(str(stage_dir / "wheel"))} -Recurse -Force -ErrorAction SilentlyContinue }}
"""


def _spawn_deferred_upgrade(
    *,
    uv_executable: str,
    command: list[str],
    tag: str,
    log: list[str],
) -> UpgradeResult:
    """Hand the install to a detached helper that runs after we exit."""

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        return UpgradeResult(
            ok=False,
            message=(
                "PowerShell was not found, so the update cannot be staged. "
                "Stop the server and re-run the install command instead."
            ),
            log=log,
        )
    stage_dir = _stage_dir()
    result_path = stage_dir / _PENDING_RESULT_FILENAME
    with suppress(OSError):
        result_path.unlink()
    script_path = stage_dir / "apply-upgrade.ps1"
    try:
        script_path.write_text(
            _deferred_helper_script(
                uv_executable=uv_executable,
                command=command,
                result_path=result_path,
                stage_dir=stage_dir,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return UpgradeResult(
            ok=False, message=f"Could not stage the update: {exc!s}", log=log
        )

    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the helper must outlive us,
    # and must not die with the console this server was started from.
    creation_flags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        return UpgradeResult(
            ok=False, message=f"Could not start the update helper: {exc!s}", log=log
        )

    log.append("staged for install after shutdown (Windows)")
    _CACHE.restart_required = True
    _CACHE.staged_install = True
    return UpgradeResult(
        ok=True,
        message=(
            f"{tag or 'The latest release'} is verified and staged. Windows cannot "
            "replace the environment while the server is running, so stop "
            "fcc-server to finish installing, then start it again."
        ),
        installed_version=tag or None,
        log=log,
    )


def upgrade_to_latest(payload: dict[str, Any]) -> UpgradeResult:
    """Download, verify, and install the wheel from ``payload``.

    Synchronous and slow (a full dependency resolve): callers must run this in
    a worker thread so it never blocks the event loop.
    """
    log: list[str] = []
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        return UpgradeResult(
            ok=False,
            message="uv was not found on PATH; re-run the install script instead.",
        )

    asset = _select_wheel_asset(payload)
    if asset is None:
        return UpgradeResult(ok=False, message="That release publishes no wheel.")
    download_url = asset.get("browser_download_url")
    if not download_url:
        return UpgradeResult(ok=False, message="Release wheel has no download URL.")

    expected_digest = str(asset.get("digest") or "").removeprefix("sha256:").lower()
    tag = str(payload.get("tag_name") or "").lstrip("vV")

    with ExitStack() as stack:
        if _WINDOWS:
            wheel_dir = _stage_dir() / "wheel"
            with suppress(OSError):
                shutil.rmtree(wheel_dir)
            wheel_dir.mkdir(parents=True, exist_ok=True)
        else:
            wheel_dir = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="fcc-upgrade-"))
            )
        wheel_path = wheel_dir / str(asset.get("name"))
        try:
            with httpx.stream(
                "GET",
                download_url,
                timeout=_HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with wheel_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            return UpgradeResult(
                ok=False, message=f"Could not download the release wheel: {exc!s}"
            )
        log.append(f"downloaded {wheel_path.name}")

        actual_digest = _sha256_of(wheel_path)
        if expected_digest and actual_digest != expected_digest:
            # Same refusal the install scripts make: never install a wheel
            # whose checksum does not match what the release advertises.
            return UpgradeResult(
                ok=False,
                message="Release wheel checksum mismatch; refusing to install.",
                log=log,
            )
        log.append(
            f"verified sha256 {actual_digest[:16]}…"
            if expected_digest
            else "release published no digest; skipped checksum verification"
        )

        extras, python = _installed_extras_and_python()
        spec = wheel_path.as_uri()
        if extras:
            spec = f"{spec}[{','.join(extras)}]"
            log.append(f"preserving extras: {', '.join(extras)}")
        command = [
            uv_executable,
            "tool",
            "install",
            "--force",
            "--refresh-package",
            PACKAGE_NAME,
            "--python",
            python,
            spec,
        ]
        if _WINDOWS:
            return _spawn_deferred_upgrade(
                uv_executable=uv_executable,
                command=command,
                tag=tag,
                log=log,
            )
        try:
            # Fixed argv, never a shell string, so the release metadata cannot
            # inject arguments.
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_UPGRADE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return UpgradeResult(
                ok=False, message=f"Upgrade command failed: {exc!s}", log=log
            )

    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    log.extend(tail[-8:])
    if completed.returncode != 0:
        return UpgradeResult(
            ok=False,
            message=f"uv tool install exited with code {completed.returncode}.",
            log=log,
        )

    _CACHE.restart_required = True
    return UpgradeResult(
        ok=True,
        message=(
            f"Installed {tag or 'the latest release'}. Restart the server to run it."
        ),
        installed_version=tag or None,
        log=log,
    )


async def perform_upgrade() -> UpgradeResult:
    """Fetch the latest release and install it off the event loop."""
    payload, _checked_at, error = await _CACHE.get(force=True)
    if payload is None:
        return UpgradeResult(ok=False, message=error or "No release information.")
    if not is_newer(str(payload.get("tag_name") or ""), current_version()):
        return UpgradeResult(ok=False, message="Already on the latest release.")
    return await asyncio.to_thread(upgrade_to_latest, payload)


def reset_cache_for_tests() -> None:
    """Clear cached release state so tests start from a known point."""
    global _CACHE
    _CACHE = _ReleaseCache()


_RELEASE_NOTES_MAX_CHARS = 4000


def _release_notes(body: object) -> str | None:
    """Trim the release body for the dashboard banner.

    Bounded because the feed is remote: the banner shows an excerpt and links
    out for the rest rather than rendering an unbounded blob.
    """

    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text:
        return None
    if len(text) <= _RELEASE_NOTES_MAX_CHARS:
        return text
    return text[:_RELEASE_NOTES_MAX_CHARS].rstrip() + "\n\n…"
