"""pystray adapter for the Windows tray and macOS menu bar."""

from io import BytesIO

from PIL import Image
from pystray import Icon, Menu, MenuItem

from my_claude_code.cli.desktop import DesktopController
from my_claude_code.cli.desktop_assets import app_icon_bytes
from my_claude_code.config.desktop import (
    load_desktop_state,
    set_start_at_login,
    set_tray_enabled,
)

_APP_NAME = "My Claude Code"


class PystrayDesktopTray:
    """Render desktop lifecycle actions through the native status area."""

    def __init__(self, controller: DesktopController) -> None:
        self._controller = controller
        state = load_desktop_state()
        self._start_at_login = state.start_at_login
        self._tray_enabled = state.tray_enabled
        self._icon = Icon(
            "my-claude-code",
            _create_icon(),
            _APP_NAME,
            self._menu(),
        )

    def _menu(self) -> Menu:
        return Menu(
            MenuItem("Open Admin", self._open_admin, default=True),
            MenuItem("Check Server Status", self._check_status),
            MenuItem("Restart Server", self._restart_server),
            Menu.SEPARATOR,
            MenuItem(
                "Start at Login",
                self._toggle_start_at_login,
                checked=lambda item: self._start_at_login,
            ),
            MenuItem(
                "Tray Enabled",
                self._toggle_tray_enabled,
                checked=lambda item: self._tray_enabled,
            ),
            Menu.SEPARATOR,
            MenuItem("Quit", self._quit),
        )

    def run(self) -> None:
        self._icon.run()

    def stop(self) -> None:
        self._icon.stop()

    def _open_admin(self, _icon: Icon, _item: MenuItem) -> None:
        self._controller.open_admin()

    def _check_status(self, _icon: Icon, _item: MenuItem) -> None:
        self._icon.notify(
            f"Server is {self._controller.status}.",
            _APP_NAME,
        )

    def _restart_server(self, _icon: Icon, _item: MenuItem) -> None:
        try:
            self._controller.restart_server()
            self._icon.notify("Server restarted.", _APP_NAME)
        except Exception as exc:
            self._icon.notify(f"Restart failed: {exc}", _APP_NAME)

    def _toggle_start_at_login(self, _icon: Icon, _item: MenuItem) -> None:
        self._start_at_login = not self._start_at_login
        set_start_at_login(self._start_at_login)

    def _toggle_tray_enabled(self, _icon: Icon, _item: MenuItem) -> None:
        self._tray_enabled = not self._tray_enabled
        set_tray_enabled(self._tray_enabled)

    def _quit(self, _icon: Icon, _item: MenuItem) -> None:
        self._controller.quit()
        self._icon.stop()


def _create_icon() -> Image.Image:
    """Load the same branded artwork used by native desktop launchers."""

    with Image.open(BytesIO(app_icon_bytes(".png"))) as image:
        return image.convert("RGBA")


def launch() -> None:
    """Launch the pystray tray adapter around a desktop controller."""

    from my_claude_code.cli.desktop import launch_desktop

    launch_desktop(PystrayDesktopTray)
