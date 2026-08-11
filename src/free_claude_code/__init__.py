"""Compatibility shim: ``free_claude_code`` re-exports ``my_claude_code``.

The renamed canonical package is ``my_claude_code``. This shim lets any
third-party code or straggler import that still references the legacy
``free_claude_code`` namespace resolve to the same implementation without
maintaining a duplicate copy.
"""

import my_claude_code  # compat shim: legacy namespace resolves to the canonical package


def __getattr__(name: str) -> object:
    return getattr(my_claude_code, name)
