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
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.cli.desktop_window import DesktopWindow, create_window
from my_claude_code.cli.launchers.common import preflight_proxy
from my_claude_code.cli.port_diagnostics import (
    PortOwner,
    diagnose_port_owner,
    probe_port_available,
)
from my_claude_code.config.claude_discovery import native_origin
from my_claude_code.config.desktop import load_desktop_state
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import get_settings
from my_claude_code.core.interprocess_lock import InterprocessFileLock

_HEALTH_CHECK_INTERVAL_SECONDS = 0.25
_START_WAIT_SECONDS = 15.0
_ADMIN_REQUEST_TIMEOUT_SECONDS = 5.0

_SERVER_MODULE = "my_claude_code.cli.entrypoints"

LOCK_FILENAME = "desktop.lock"
ACTIVATION_FILENAME = "desktop.activate"
ACTIVATION_POLL_SECONDS = 1.0
HEALTH_POLL_INTERVAL_SECONDS = 5.0
HEALTH_FAILURE_THRESHOLD = 3

#: Three distinguishable states of the configured host:port.
type ServerPresence = Literal["healthy", "foreign", "free"]

SERVER_DOWN_NOTIFICATION = (
    "The MCC server stopped answering. Choose Restart Server in this menu, "
    "or run mcc-server in a terminal to see why it exited."
)
SERVER_RECOVERED_NOTIFICATION = "The MCC server is answering again."


def probe_server_presence(settings: Any) -> ServerPresence:
    """Tell a healthy MCC apart from a stranger holding the port.

    ``preflight_proxy`` answers only one question -- does a healthy MCC reply
    on this port -- and its "no" covers two very different worlds: nothing is
    listening, or something that is not MCC is. Spawning into the second case
    produces a bind failure with no explanation, so the port is probed
    read-only to separate them.
    """

    if preflight_proxy(local_proxy_root_url(settings)) is None:
        return "healthy"
    host = (settings.host or "127.0.0.1").strip()
    if probe_port_available(host, settings.port):
        return "free"
    return "foreign"


def port_conflict_message(settings: Any) -> str:
    """Name the process holding the port, not just the fact that it is held."""

    host = (settings.host or "127.0.0.1").strip()
    owner: PortOwner | None = diagnose_port_owner(host, settings.port)
    if owner is None:
        holder = "another program"
    elif owner.name and owner.pid:
        holder = f"{owner.name} (pid {owner.pid})"
    elif owner.pid:
        holder = f"pid {owner.pid}"
    else:
        holder = owner.name or "another program"
    return (
        f"Port {settings.port} is held by {holder}, which is not the MCC "
        f"server. Stop it, or change the port in the admin dashboard, then "
        f"start the desktop app again."
    )


class HealthTracker:
    """Debounce health probes so a self-update restart is not read as death.

    The server can replace its own process during an update, and the dashboard
    already has its own reconnect state machine. A single failed probe
    therefore means nothing: only ``threshold`` consecutive failures raise a
    notification, and only one notification is raised per outage.
    """

    def __init__(self, threshold: int = HEALTH_FAILURE_THRESHOLD) -> None:
        self._threshold = max(1, threshold)
        self._failures = 0
        self._notified = False

    def record(self, healthy: bool) -> bool:
        """Record one probe; return True exactly when an outage begins."""

        if healthy:
            self._failures = 0
            self._notified = False
            return False
        self._failures += 1
        if self._failures < self._threshold or self._notified:
            return False
        self._notified = True
        return True

    def record_recovery(self, healthy: bool) -> bool:
        """Return True when a probe ends an outage we already reported."""

        return healthy and self._notified


class ActivationSignal:
    """Cross-process "show the window" doorbell backed by one small file.

    A file is chosen over a loopback socket deliberately. Binding a listener
    raises the Windows Defender firewall prompt on first run; a port can be
    taken by exactly the unrelated program the desktop app is already fighting
    over; and the config directory already hosts ``desktop.lock``, so the
    singleton and its doorbell live together. The cost is polling, which at a
    one-second cadence is free.

    Staleness is handled by the reader, not the writer: the instance that owns
    the lock clears the file before it starts watching, so a signal left behind
    by an instance that died without cleanup is discarded instead of causing a
    spurious activation on the next launch.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._seen = ""

    def clear(self) -> None:
        with suppress(OSError):
            self._path.unlink(missing_ok=True)
        self._seen = ""

    def signal(self) -> None:
        token = f"{time.time():.6f}"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
            tmp_path.write_text(token, encoding="utf-8")
            os.replace(tmp_path, self._path)
        except OSError:
            return

    def poll(self) -> bool:
        """Return True once per new signal written by another process."""

        try:
            token = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not token or token == self._seen:
            return False
        self._seen = token
        return True


class DesktopController:
    """Spawn and supervise the MCC server from the desktop tray.

    The server is always a separate process; this controller never imports or
    embeds the blocking ``serve()`` loop. Every operation goes through the
    loopback admin API, which requires no token.
    """

    def __init__(
        self, *, lock: InterprocessFileLock, window: DesktopWindow | None = None
    ) -> None:
        self._lock = lock
        self._process: subprocess.Popen[bytes] | None = None
        self._window = window
        self._health_stop: threading.Event | None = None

    @property
    def status(self) -> str:
        error = preflight_proxy(local_proxy_root_url(get_settings()))
        return "running" if error is None else "stopped"

    def server_mode(self) -> str:
        """Return the persisted server-ownership mode (``spawn|attach|off``)."""

        return load_desktop_state().server_mode

    # -- process management ------------------------------------------------

    def ensure_server(self) -> None:
        """Spawn ``mcc-server`` only when the tray owns it (``spawn``).

        In ``attach`` and ``off`` the desktop app never starts the server:
        ``attach`` health-checks and reports an existing server, ``off`` does
        not touch the server at all.
        """

        if load_desktop_state().server_mode != "spawn":
            return
        settings = get_settings()
        presence = probe_server_presence(settings)
        if presence == "healthy":
            return
        if presence == "foreign":
            raise DesktopError(port_conflict_message(settings))
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

    def attach_window(self, window: DesktopWindow) -> None:
        """Give the controller the window provider chosen at launch."""

        self._window = window

    @property
    def window(self) -> DesktopWindow | None:
        return self._window

    def show_window(self) -> None:
        """Show the dashboard window, raising an existing one when possible."""

        window = self._window
        if window is None:
            return
        if window.is_open and window.focus():
            return
        window.open(local_admin_url(get_settings()))

    def close_window(self) -> None:
        """Close the window only.

        Closing a window and quitting the app are different actions: the server
        keeps running either way, and in ``attach``/``off`` mode it was never
        ours to stop.
        """

        window = self._window
        if window is not None:
            window.close()

    def handle_window_closed(self) -> bool:
        """Return True when the app should keep running after a window close.

        ``minimize_to_tray`` is the user's answer to "did closing the window
        mean quit?". With a tray to fall back to, closing hides the window;
        with no tray, closing is the only way out, so it ends the app.
        """

        state = load_desktop_state()
        return state.minimize_to_tray and state.tray_enabled

    # -- health monitor ----------------------------------------------------

    def start_health_monitor(
        self,
        on_unhealthy: Callable[[], None],
        *,
        on_recovered: Callable[[], None] | None = None,
        interval: float = HEALTH_POLL_INTERVAL_SECONDS,
        threshold: int = HEALTH_FAILURE_THRESHOLD,
    ) -> None:
        """Poll the server on a daemon thread and report an outage once.

        This only *reports*. It never respawns: in ``attach``/``off`` mode the
        server is not ours, and in ``spawn`` mode a silent respawn would race
        the server's own self-update restart.
        """

        if self._health_stop is not None:
            return
        stop = threading.Event()
        self._health_stop = stop
        threading.Thread(
            target=self._health_loop,
            args=(on_unhealthy, on_recovered, interval, threshold, stop),
            name="mcc-desktop-health",
            daemon=True,
        ).start()

    def stop_health_monitor(self) -> None:
        stop = self._health_stop
        self._health_stop = None
        if stop is not None:
            stop.set()

    def _health_loop(
        self,
        on_unhealthy: Callable[[], None],
        on_recovered: Callable[[], None] | None,
        interval: float,
        threshold: int,
        stop: threading.Event,
    ) -> None:
        tracker = HealthTracker(threshold)
        root_url = local_proxy_root_url(get_settings())
        while not stop.wait(interval):
            healthy = preflight_proxy(root_url) is None
            recovered = tracker.record_recovery(healthy)
            if tracker.record(healthy):
                with suppress(Exception):
                    on_unhealthy()
            elif recovered and on_recovered is not None:
                with suppress(Exception):
                    on_recovered()

    # -- menu actions ------------------------------------------------------

    def open_admin(self) -> None:
        self.show_window()

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

        self.stop_health_monitor()
        self.close_window()
        self._stop_child()
        self._lock.release()

    def quit(self) -> None:
        """Stop the child server and release the lock; the tray loop ends itself."""

        self.stop()


class DesktopError(Exception):
    """Raised when the desktop controller cannot complete an operation."""


def headless_refusal_reason() -> str | None:
    """Return why a desktop window cannot be shown here, or ``None``.

    Reuses the discovery module's platform detection rather than re-sniffing
    ``/proc``. A refusal names the working alternative instead of reporting the
    missing display.
    """

    origin = native_origin()
    if origin in {"windows", "macos"}:
        return None
    # Quote the configured address, never the default: a message that names
    # :8082 on a machine serving :9000 sends the reader somewhere empty.
    admin_url = local_admin_url(get_settings())
    if origin == "wsl":
        return (
            "mcc-desktop needs a desktop session, and WSL does not have one. "
            "Run mcc-server inside WSL instead -- it is headless -- and open "
            f"the dashboard at {admin_url} in a Windows browser, which "
            "reaches the WSL port directly."
        )
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    return (
        "mcc-desktop needs a desktop session, and neither DISPLAY nor "
        "WAYLAND_DISPLAY is set. Run mcc-server instead -- it is headless -- "
        f"and open {admin_url} from a browser on any machine that can reach "
        "this one."
    )


def launch_desktop(
    tray_factory: Any,
    *,
    window_factory: Callable[[str], DesktopWindow] = create_window,
) -> None:
    """Start the singleton desktop host, or activate the one already running.

    ``tray_factory`` is a callable that receives the controller and returns an
    object exposing ``run()`` / ``stop()`` -- the pystray adapter.

    A second launch does not open anything of its own. It rings the running
    instance's doorbell and exits, so "launch again" means "show me the window
    I already have" rather than "make me a second one".
    """

    state = load_desktop_state()
    signal = ActivationSignal(config_dir_path() / ACTIVATION_FILENAME)
    instance_lock = InterprocessFileLock(config_dir_path() / LOCK_FILENAME)
    if not instance_lock.acquire():
        signal.signal()
        return

    signal.clear()
    controller = DesktopController(
        lock=instance_lock, window=window_factory(state.window)
    )
    tray = tray_factory(controller)
    notify = _tray_notifier(tray)
    stop_watching = threading.Event()
    watcher = threading.Thread(
        target=_watch_activation,
        args=(signal, controller, stop_watching),
        name="mcc-desktop-activation",
        daemon=True,
    )
    try:
        controller.ensure_server()
        controller.show_window()
        controller.start_health_monitor(
            lambda: notify(SERVER_DOWN_NOTIFICATION),
            on_recovered=lambda: notify(SERVER_RECOVERED_NOTIFICATION),
        )
        watcher.start()
        tray.run()
    finally:
        stop_watching.set()
        controller.stop()
        signal.clear()


def _tray_notifier(tray: Any) -> Callable[[str], None]:
    """Return a notifier that tolerates a tray adapter without notifications."""

    notify = getattr(tray, "notify", None)
    if not callable(notify):
        return lambda _message: None

    def _notify(message: str) -> None:
        with suppress(Exception):
            notify(message)

    return _notify


def _watch_activation(
    signal: ActivationSignal,
    controller: DesktopController,
    stop: threading.Event,
) -> None:
    while not stop.wait(ACTIVATION_POLL_SECONDS):
        if signal.poll():
            with suppress(Exception):
                controller.show_window()
