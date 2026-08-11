"""Compatibility shim: ``free_claude_code`` re-exports ``my_claude_code``.

The renamed canonical package is ``my_claude_code``. This shim lets any
third-party code or straggler import that still references the legacy
``free_claude_code`` namespace resolve to the same implementation without
maintaining a duplicate copy.
"""

from my_claude_code import __all__ as __all__  # explicit re-export
from my_claude_code import identity, version  # noqa: F401 — core public API
