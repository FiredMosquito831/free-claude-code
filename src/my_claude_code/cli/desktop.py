"""Thin controller between the tray UI and the MCC server child process.

The fork has no in-process ``ServerSupervisor``: ``mcc-server`` is a blocking
``serve()`` loop, so the tray runs it as a *child process* and drives it over
the loopback admin API. This controller owns that child -- spawn, health check,
restart, stop -- while the tray adapter owns the visible menu.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.cli.launchers.common import preflight_proxy
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import get_settings
from my_claude_code.core.interprocess_lock import InterprocessFileLock

_HEALTH_CHECK_INTERVAL_SECONDS = 0.25
_START_WAIT_SECONDS = 15.0
_ADMIN_REQUEST_TIMEOUT_SECONDS = 5.0

_SERVER_MODULE = "my_claude_code.cli.entrypoints"


class DesktopController:
    """Spawn and supervise the MCC server from the desktop tray.

    The server is always a separate process; this controller never imports or
    embeds the blocking ``serve()`` loop. Every operation goes through the
    loopback admin API, which requires no token.
    """

    def __init__(self, *, lock: InterprocessFileLock) -> None:
        self._lock = lock
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def status(self) -> str:
        error = preflight_proxy(local_proxy_root_url(get_settings()))
        return "running" if error is None else "stopped"

    def server_mode(self) -> str:
        """Return the persisted server-ownership mode (``spawn|attach|off``)."""

        from my_claude_code.config.desktop import load_desktop_state

        return load_desktop_state().server_mode

    # -- process management ------------------------------------------------

    def ensure_server(self) -> None:
        """Spawn ``mcc-server`` only when the tray owns it (``spawn``).

        In ``attach`` and ``off`` the desktop app never starts the server:
        ``attach`` health-checks and reports an existing server, ``off`` does
        not touch the server at all.
        """

        from my_claude_code.config.desktop import load_desktop_state

        if load_desktop_state().server_mode != "spawn":
            return
        settings = get_settings()
        if preflight_proxy(local_proxy_root_url(settings)) is None:
            return
        self._spawn_server(settings)

    def _spawn_server(self, settings: Any) -> None:
        command = self._server_command()
        if command is None:
            raise DesktopError(
                "Could not resolve mcc-server. Reinstall My Claude Code so the "
                "server command is on PATH, or start it manually."
            )
        try:
            self._process = subprocess.Popen(command)
        except OSError as exc:
            raise DesktopError(f"Could not start the MCC server: {exc}") from exc

        deadline = time.monotonic() + _START_WAIT_SECONDS
        root_url = local_proxy_root_url(settings)
        while time.monotonic() < deadline:
            if preflight_proxy(root_url) is None:
                return
            time.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)
        raise DesktopError(
            "The MCC server did not become healthy in time. It may be starting "
            "still, or it failed to bind its port."
        )

    def _server_command(self) -> list[str] | None:
        binary = self._server_binary()
        if binary is not None:
            return [binary]
        return [sys.executable, "-m", _SERVER_MODULE]

    def _server_binary(self) -> str | None:
        bin_dir = self._uv_tool_bin_dir()
        if bin_dir is not None:
            candidate = bin_dir / (
                "mcc-server.exe" if os.name == "nt" else "mcc-server"
            )
            if candidate.is_file():
                return str(candidate)
        return shutil.which("mcc-server")

    def _uv_tool_bin_dir(self) -> Path | None:
        uv = shutil.which("uv")
        if uv is None:
            return None
        try:
            completed = subprocess.run(
                [uv, "tool", "dir", "--bin"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return None
        path = completed.stdout.strip()
        return Path(path) if completed.returncode == 0 and path else None

    def _stop_child(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    # -- admin API ---------------------------------------------------------

    def _admin_json(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        root = local_proxy_root_url(get_settings())
        url = f"{root.rstrip('/')}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            method=method,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urlopen(request, timeout=_ADMIN_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise DesktopError(
                f"Admin API returned HTTP {exc.code}: {detail.strip() or exc.reason}"
            ) from exc
        except URLError as exc:
            raise DesktopError(f"Admin API is unreachable: {exc.reason}") from exc
        except OSError as exc:
            raise DesktopError(f"Admin API request failed: {exc}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    # -- menu actions ------------------------------------------------------

    def open_admin(self) -> None:
        webbrowser.open(local_admin_url(get_settings()))

    def check_status(self) -> str:
        return self.status

    def restart_server(self) -> None:
        """Restart the running server; if it is down, spawn it fresh.

        In ``attach``/``off`` mode the tray never owns a server, so a restart
        raises :class:`DesktopError` instead of silently spawning one. Prefer
        the loopback ``POST /admin/api/config/apply`` no-op so the server's own
        graceful-drain machinery performs the reload. Fall back to a hard kill
        + respawn when the API is unreachable but the health probe said the
        server was up (a race), or when the caller holds a child.
        """

        if self.server_mode() != "spawn":
            raise DesktopError(
                "Server is managed by the deployment mode; restart is only "
                "available when Server mode is set to 'spawn'."
            )

        settings = get_settings()
        root_url = local_proxy_root_url(settings)
        if preflight_proxy(root_url) is not None:
            self.ensure_server()
            return

        try:
            self._admin_json("POST", "/admin/api/config/apply", {"values": {}})
        except DesktopError:
            # The config apply is unreachable even though the health probe just
            # passed. Kill any child we own and let ensure_server respawn; if we
            # do not own a child, leave the running server alone.
            self._stop_child()
            if preflight_proxy(root_url) is not None:
                self.ensure_server()

    def stop(self) -> None:
        """Stop the server child we own and release the singleton lock.

        Only the child is stopped; a server the user started outside the tray
        is left running, because the tray is a controller, not an owner.
        """

        self._stop_child()
        self._lock.release()

    def quit(self) -> None:
        """Stop the child server and release the lock; the tray loop ends itself."""

        self.stop()


class DesktopError(Exception):
    """Raised when the desktop controller cannot complete an operation."""


def launch_desktop(tray_factory: Any) -> None:
    """Start the singleton desktop host or hand off to an already-running tray.

    ``tray_factory`` is a callable that receives the controller and returns an
    object exposing ``run()`` / ``stop()`` -- the pystray adapter.
    """

    from my_claude_code.config.paths import config_dir_path

    settings = get_settings()
    instance_lock = InterprocessFileLock(config_dir_path() / "desktop.lock")
    if not instance_lock.acquire():
        # Another tray already owns the server lifecycle; just surface the UI.
        _open_admin_when_ready(settings)
        return

    controller = DesktopController(lock=instance_lock)
    tray = tray_factory(controller)
    try:
        controller.ensure_server()
        tray.run()
    finally:
        controller.stop()


def _open_admin_when_ready(settings: Any) -> None:
    """Open the admin UI once the server answers its health probe."""

    root_url = local_proxy_root_url(settings)
    deadline = time.monotonic() + _START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if preflight_proxy(root_url) is None:
            webbrowser.open(local_admin_url(settings))
            return
        time.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)
