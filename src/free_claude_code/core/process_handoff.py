"""Process-local handoff state shared by updater and server supervisor."""

import threading

_lock = threading.Lock()
_external_helper_pending = False


def set_external_upgrade_helper_pending(value: bool) -> None:
    """Record whether an external helper owns install and relaunch."""
    global _external_helper_pending
    with _lock:
        _external_helper_pending = value


def external_upgrade_helper_pending() -> bool:
    """Whether this process must exit instead of executing a launcher itself."""
    with _lock:
        return _external_helper_pending


def reset_process_handoff_for_tests() -> None:
    """Restore the process-local handoff state between tests."""
    set_external_upgrade_helper_pending(False)
