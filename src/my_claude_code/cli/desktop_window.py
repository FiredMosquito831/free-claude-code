"""Window providers for the MCC desktop shell.

The desktop app shows the admin dashboard in a window. There is no single
"native window" on three operating systems -- a native webview is WebView2 on
Windows, WKWebView on macOS and WebKitGTK on Linux -- so this module models the
window as a *seam* with an ordered fallback chain instead of a single engine.

The preferred provider is Chromium app-mode, because it is a real Chrome
profile: ``window.open(url, "_blank")`` (the ChatGPT and Anthropic OAuth
flows), ``<a download>`` (the analytics export window) and
``navigator.clipboard.writeText`` (the copy buttons) all behave exactly as they
do in the browser the dashboard was written against. Embedded webviews break
all three to varying degrees per engine.

Every provider loads ``http://127.0.0.1:<port>/admin`` over real HTTP. Nothing
here ever presents a ``file://`` origin, because the admin API's
``require_loopback_admin`` rejects one with a 403.
"""

import logging
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from my_claude_code.config.desktop import (
    WINDOW_PREFERENCES,
    WindowPreference,
    chromium_binary,
)
from my_claude_code.config.paths import config_dir_path

logger = logging.getLogger(__name__)

WINDOW_SIZE = "1400,900"
LINUX_WM_CLASS = "MyClaudeCode"
PROFILE_DIRNAME = "desktop-profile"

#: Provider identifiers in default preference order. ``auto`` walks this list.
PROVIDER_CHAIN: tuple[str, ...] = ("app-mode", "pywebview", "browser")


@runtime_checkable
class DesktopWindow(Protocol):
    """A showable dashboard window owned by the desktop app."""

    def open(self, url: str) -> None:
        """Show the window on ``url``."""

    def focus(self) -> bool:
        """Raise an existing window; return False when that is not possible."""

    def close(self) -> None:
        """Close the window without touching the server."""

    @property
    def is_open(self) -> bool:
        """Whether a window is currently believed to be showing."""


# ------------------------------------------------------------------- providers
#
# Chromium-family binary discovery (``chromium_binary``) lives in
# ``config.desktop`` -- it is also needed there to report what an ``auto``
# preference resolves to, without ``config`` importing this module or ``api``
# crossing into ``cli`` (see ``tests/contracts/test_import_boundaries.py``).


class AppModeWindow:
    """A dedicated Chrome/Edge/Brave window launched with ``--app``.

    App-mode is a chrome-less browser window backed by a private profile
    directory. It is a real browser, so the dashboard's OAuth popups, blob
    downloads and clipboard writes keep working unchanged.
    """

    def __init__(self, binary: str, *, profile_dir: Path | None = None) -> None:
        self._binary = binary
        self._profile_dir = profile_dir or (config_dir_path() / PROFILE_DIRNAME)
        self._process: subprocess.Popen[bytes] | None = None
        self._last_url: str | None = None

    @staticmethod
    def available() -> bool:
        return chromium_binary() is not None

    @classmethod
    def create(cls) -> AppModeWindow | None:
        binary = chromium_binary()
        return None if binary is None else cls(binary)

    def command(self, url: str) -> list[str]:
        command = [
            self._binary,
            f"--app={url}",
            f"--user-data-dir={self._profile_dir}",
            f"--window-size={WINDOW_SIZE}",
        ]
        if sys.platform not in {"win32", "darwin"}:
            # Without an explicit WM class the window groups under "Chromium".
            command.append(f"--class={LINUX_WM_CLASS}")
        return command

    def open(self, url: str) -> None:
        self._last_url = url
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen(self.command(url))
        except OSError:
            logger.warning("Could not launch %s in app mode.", self._binary)
            self._process = None
            webbrowser.open(url)

    def focus(self) -> bool:
        """Re-invoke the same app command so the profile raises its window.

        Chromium routes a second launch through the running profile process,
        which brings the existing app window forward instead of creating a
        duplicate. Best effort: if we never launched, there is nothing to
        raise and the caller should open instead.
        """

        process = self._process
        if process is None or process.poll() is not None:
            return False
        url = self._last_url
        if url is None:
            return False
        try:
            subprocess.Popen(self.command(url))
        except OSError:
            return False
        return True

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @property
    def is_open(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None


class PywebviewWindow:
    """Embedded webview, used only when app-mode is unavailable.

    ``pywebview`` wraps three different engines, and the dashboard depends on
    three browser behaviours that embedded engines handle inconsistently, so
    this provider installs shims before the page can use them:

    * ``window.open`` is replaced with a bridge that hands the URL to the
      system browser, because the ChatGPT and Anthropic OAuth flows call it and
      a silent no-op makes login unreachable.
    * downloads require ``webview.settings["ALLOW_DOWNLOADS"]``; when that
      setting is absent the provider declares itself unavailable rather than
      shipping an export window that silently drops files.
    * the clipboard falls back to ``document.execCommand('copy')`` when
      ``navigator.clipboard`` is missing.

    It is also unavailable on macOS, where the GUI loop must own the main
    thread and the tray already does.
    """

    _SHIM_JS = """
    (function () {
      var api = window.pywebview && window.pywebview.api;
      if (api && api.open_external) {
        window.open = function (url) {
          if (url) { api.open_external(String(url)); }
          return null;
        };
      }
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        navigator.clipboard = {
          writeText: function (text) {
            var area = document.createElement('textarea');
            area.value = text;
            document.body.appendChild(area);
            area.select();
            try { document.execCommand('copy'); } finally { area.remove(); }
            return Promise.resolve();
          }
        };
      }
    })();
    """

    def __init__(self, module: Any) -> None:
        self._webview: Any = module
        self._window: Any = None

    @staticmethod
    def _module() -> Any:
        if sys.platform == "darwin":
            # pywebview's run loop requires the main thread, which pystray owns.
            return None
        try:
            import webview
        except ImportError, OSError:
            return None
        settings = getattr(webview, "settings", None)
        if not isinstance(settings, dict) or "ALLOW_DOWNLOADS" not in settings:
            return None
        return webview

    @staticmethod
    def available() -> bool:
        return PywebviewWindow._module() is not None

    @classmethod
    def create(cls) -> PywebviewWindow | None:
        module = cls._module()
        return None if module is None else cls(module)

    def open(self, url: str) -> None:
        import threading

        webview = self._webview
        settings = webview.settings
        settings["ALLOW_DOWNLOADS"] = True
        settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        window = webview.create_window(
            "My Claude Code",
            url,
            js_api=_PywebviewApi(),
            width=1400,
            height=900,
        )
        self._window = window
        loaded = getattr(getattr(window, "events", None), "loaded", None)
        if loaded is not None:
            loaded += self._install_shims
        threading.Thread(
            target=webview.start,
            name="mcc-desktop-webview",
            daemon=True,
        ).start()

    def _install_shims(self) -> None:
        window = self._window
        if window is None:
            return
        window.evaluate_js(self._SHIM_JS)

    def focus(self) -> bool:
        window = self._window
        if window is None:
            return False
        restore = getattr(window, "restore", None)
        if callable(restore):
            restore()
        show = getattr(window, "show", None)
        if callable(show):
            show()
        return True

    def close(self) -> None:
        window = self._window
        self._window = None
        if window is None:
            return
        destroy = getattr(window, "destroy", None)
        if callable(destroy):
            destroy()

    @property
    def is_open(self) -> bool:
        return self._window is not None


class _PywebviewApi:
    """JS-callable bridge exposed to the embedded page."""

    def open_external(self, url: str) -> None:
        webbrowser.open(url)


class BrowserTabWindow:
    """The default browser, in a normal tab. Always available, never focusable."""

    def __init__(self) -> None:
        self._opened = False

    @staticmethod
    def available() -> bool:
        return True

    @classmethod
    def create(cls) -> BrowserTabWindow:
        return cls()

    def open(self, url: str) -> None:
        webbrowser.open(url)
        self._opened = True

    def focus(self) -> bool:
        """A browser tab we do not own cannot be raised."""

        return False

    def close(self) -> None:
        self._opened = False

    @property
    def is_open(self) -> bool:
        return self._opened


_PROVIDERS: dict[str, Callable[[], DesktopWindow | None]] = {
    "app-mode": AppModeWindow.create,
    "pywebview": PywebviewWindow.create,
    "browser": BrowserTabWindow.create,
}


def create_window(preference: str) -> DesktopWindow:
    """Return the first usable window provider for ``preference``.

    ``auto`` walks :data:`PROVIDER_CHAIN` in order. An explicit pin is tried
    first and then *degrades* through the remaining chain with a logged
    warning: an unavailable preference is a misconfiguration, and a guardrail
    must degrade rather than become an outage.
    """

    normalized: WindowPreference | str = preference
    if preference not in WINDOW_PREFERENCES:
        logger.warning(
            "Unknown window preference %r; falling back to 'auto'.", preference
        )
        normalized = "auto"

    if normalized == "auto":
        order = list(PROVIDER_CHAIN)
    else:
        order = [normalized, *(name for name in PROVIDER_CHAIN if name != normalized)]

    for name in order:
        window = _PROVIDERS[name]()
        if window is None:
            continue
        if normalized != "auto" and name != normalized:
            logger.warning(
                "Window provider %r is unavailable; using %r instead.",
                normalized,
                name,
            )
        return window
    return BrowserTabWindow()
