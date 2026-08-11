"""Canonical installed package version across both tool owners.

The migration bridge ships two uv-installable owners that share one
implementation: the legacy ``free-claude-code`` distribution and the native
``my-claude-code`` distribution. Either may own the running package, so the
lookup tries the native owner first and falls back to the legacy name.

Distribution names live here (rather than identity) so that ``identity`` can
import from ``version`` without creating a cycle.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

LEGACY_DISTRIBUTION = "free-claude-code"
NATIVE_DISTRIBUTION = "my-claude-code"

_UNKNOWN_VERSION = "0+unknown"


def package_version() -> str:
    """Return installed metadata, or an explicit source-only fallback."""
    for distribution in (NATIVE_DISTRIBUTION, LEGACY_DISTRIBUTION):
        try:
            return distribution_version(distribution)
        except PackageNotFoundError:
            continue
    return _UNKNOWN_VERSION


def distribution_name() -> str:
    """Return the distribution that owns the running package."""
    for distribution in (NATIVE_DISTRIBUTION, LEGACY_DISTRIBUTION):
        try:
            if distribution_version(distribution) != _UNKNOWN_VERSION:
                return distribution
        except PackageNotFoundError:
            continue
    return LEGACY_DISTRIBUTION
