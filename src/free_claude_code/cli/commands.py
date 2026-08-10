"""Implementations for installed Free Claude Code commands."""

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from enum import Enum
from pathlib import Path

import uvicorn
from loguru import logger

from free_claude_code.cli.launchers.common import preflight_proxy
from free_claude_code.cli.process_registry import kill_all_best_effort
from free_claude_code.config.env_migrations import (
    explicit_env_file_migration_warning,
    migrate_owned_env_files,
)
from free_claude_code.config.env_template import load_env_template
from free_claude_code.config.paths import (
    config_dir_path,
    legacy_env_paths,
    managed_env_path,
)
from free_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from free_claude_code.config.settings import Settings, get_settings
from free_claude_code.core.process_handoff import external_upgrade_helper_pending
from free_claude_code.runtime.bootstrap import build_asgi_app

SERVER_GRACEFUL_SHUTDOWN_SECONDS = 5
_WINDOWS = os.name == "nt"


class ServerExitAction(Enum):
    """What the supervisor does after one fully closed server generation."""

    STOP = "stop"
    RELOAD = "reload"
    REPLACE_PROCESS = "replace_process"


def _server_launcher() -> str | None:
    """Return the stable launcher outside the uv-managed tool environment."""
    bin_dir = _uv_tool_bin_dir()
    if bin_dir is not None:
        candidate = bin_dir / ("fcc-server.exe" if os.name == "nt" else "fcc-server")
        if candidate.is_file():
            return str(candidate)
    return shutil.which("fcc-server")


def _uv_tool_bin_dir() -> Path | None:
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


def _replace_server_process() -> None:
    """Hand off to the updated server after the old runtime fully closes."""
    # Windows cannot replace the environment until this interpreter exits. Its
    # external PowerShell helper is already waiting and will launch the stable
    # shim after a successful install, so the only safe action here is to flush
    # and return from serve().
    if _WINDOWS or external_upgrade_helper_pending():
        logger.info("Server closed; the update helper will install and restart it.")
        logger.complete()
        kill_all_best_effort()
        return

    launcher = _server_launcher()
    if launcher is None:
        logger.error("Updated successfully, but fcc-server could not be found on PATH.")
        return
    # ``enqueue=True`` logging uses a background queue. exec() destroys its
    # writer thread, so wait until every queued record reaches its sink first.
    logger.info("Restarting with the updated server...")
    logger.complete()
    kill_all_best_effort()
    os.execv(launcher, [launcher, *sys.argv[1:]])


def serve() -> None:
    """Start and supervise the FastAPI server."""
    opened_admin_browser = False
    try:
        try:
            while True:
                _migrate_legacy_env_if_missing()
                _migrate_config_env_keys()
                settings = get_settings()
                should_open_admin = (
                    settings.open_admin_browser and not opened_admin_browser
                )
                action = _run_supervised_server(
                    settings, open_admin_browser=should_open_admin
                )
                if action is ServerExitAction.STOP:
                    return
                if action is ServerExitAction.REPLACE_PROCESS:
                    _replace_server_process()
                    return
                opened_admin_browser = opened_admin_browser or should_open_admin
                get_settings.cache_clear()
        except KeyboardInterrupt:
            return
    finally:
        kill_all_best_effort()


def _schedule_open_admin_browser(settings: Settings) -> None:
    """After /health succeeds, open the admin UI in the default browser (daemon thread)."""

    admin_url = local_admin_url(settings)
    proxy_root_url = local_proxy_root_url(settings)

    def open_when_ready() -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if preflight_proxy(proxy_root_url) is None:
                webbrowser.open(admin_url)
                return
            time.sleep(0.15)

    threading.Thread(
        target=open_when_ready, name="fcc-open-admin-browser", daemon=True
    ).start()


def _run_supervised_server(
    settings: Settings, *, open_admin_browser: bool
) -> ServerExitAction:
    """Run once; act only after the old ownership graph fully closes."""

    requested = ServerExitAction.STOP
    server_holder: dict[str, uvicorn.Server] = {}

    def request(action: ServerExitAction) -> None:
        nonlocal requested
        requested = action
        if server := server_holder.get("server"):
            server.should_exit = True

    def request_restart() -> None:
        request(ServerExitAction.RELOAD)

    def request_process_restart() -> None:
        request(ServerExitAction.REPLACE_PROCESS)

    asgi_app = build_asgi_app(
        settings,
        restart_callback=request_restart,
        process_restart_callback=request_process_restart,
    )
    config = uvicorn.Config(
        asgi_app,
        host=settings.host,
        port=settings.port,
        log_level="debug",
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    if open_admin_browser:
        _schedule_open_admin_browser(settings)
    server.run()
    if requested is ServerExitAction.STOP:
        return requested
    return requested if asgi_app.runtime.is_closed else ServerExitAction.STOP


def init() -> None:
    """Scaffold config at ~/.fcc/.env."""
    config_dir = config_dir_path()
    env_file = managed_env_path()

    migrated_from = _migrate_legacy_env_if_missing()
    _migrate_config_env_keys()
    if migrated_from is not None:
        print(f"Config migrated from {migrated_from} to {env_file}")
        print(
            "Edit it to set your API keys and model preferences, then run: fcc-server"
        )
        return

    if env_file.exists():
        print(f"Config already exists at {env_file}")
        print("Delete it first if you want to reset to defaults.")
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    template = load_env_template()
    env_file.write_text(template, encoding="utf-8")
    print(f"Config created at {env_file}")
    print("Edit it to set your API keys and model preferences, then run: fcc-server")


def _migrate_legacy_env_if_missing() -> Path | None:
    """Copy a legacy user env into the managed config path when absent."""

    env_file = managed_env_path()
    if env_file.exists():
        return None

    # TODO: Remove after the ~/.fcc/.env migration has had a release cycle.
    for legacy_env in legacy_env_paths():
        if not legacy_env.is_file():
            continue
        env_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_env, env_file)
        return legacy_env

    return None


def _migrate_config_env_keys() -> tuple[Path, ...]:
    """Apply dotenv key migrations before Settings loads config."""

    migrated = migrate_owned_env_files()
    if warning := explicit_env_file_migration_warning(os.environ):
        print(warning, file=sys.stderr)
    return migrated


def chatgpt_oauth_login() -> None:
    """Run the ChatGPT/Codex OAuth device-flow login."""
    from free_claude_code.providers.chatgpt_oauth import chatgpt_oauth_login_command

    chatgpt_oauth_login_command()


def compact_log() -> None:
    """Rewrite an existing request log into deduplicated compressed bodies."""
    from free_claude_code.core.request_log import (
        compact_request_log,
        default_request_log_path,
    )

    path = default_request_log_path()
    if not path.exists():
        print(f"No request log at {path}", file=sys.stderr)
        raise SystemExit(1)

    size = path.stat().st_size
    print(f"Compacting {path} ({size / 1e9:.2f} GB)")
    print("Stop the server first, or the final vacuum cannot reclaim space.\n")

    def report(done: int) -> None:
        print(f"\r  converted {done:,} requests", end="", flush=True)

    result = compact_request_log(path, progress=report)
    print()

    before = result["bytes_before"]
    after = result["bytes_after"]
    print(f"\nConverted   {result['converted']:,} requests")
    print(f"Before      {before / 1e9:.2f} GB")
    print(f"After       {after / 1e9:.2f} GB")
    if after:
        print(f"Reduction   {before / after:.1f}x")
    if not result["vacuumed"]:
        print(
            "\nThe vacuum could not run, so the file has not shrunk yet: something"
            " else has the database open. Stop the server and run this again --"
            " the conversion itself is already done and will not repeat.",
            file=sys.stderr,
        )
