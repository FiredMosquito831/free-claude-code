"""Shared credential-value parsing helpers."""


def parse_credential_keys(credential: str) -> tuple[str, ...]:
    """Split a comma-separated credential value into individual keys."""
    return tuple(key for key in (part.strip() for part in credential.split(",")) if key)
