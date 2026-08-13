"""The Claude Code configuration surface, as data the admin UI can render.

The catalog is generated from the official Claude Code documentation by
``scripts/gen_claude_config_reference.py`` and shipped as packaged data. It
describes every environment variable and ``settings.json`` key Claude Code
reads, and -- the part the UI needs -- which control each value wants.

Three value shapes decide whether an editor helps or actively misleads:

``SET_OR_UNSET``
    Six variables read only whether they are set at all, so writing ``"0"``
    turns the behaviour ON. The only way to disable them is to remove the key.
``NUMERIC_BOOLEAN``
    ``FORCE_HYPERLINK`` is parsed as a number, so ``"false"`` enables it.
``SECRET``
    Credentials, which must never be sent back to the browser in the clear.

Those are carried as explicit control kinds rather than comments so the API and
the page cannot forget them.
"""

import json
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any, Literal

CATALOG_RESOURCE = "claude_code_config_catalog.json"
CATALOG_PACKAGE = "my_claude_code.config.data"

type ControlKind = Literal[
    "toggle",
    "set_or_unset",
    "numeric_boolean",
    "enum",
    "number",
    "string",
    "path",
    "list",
    "json",
    "secret",
    "array",
    "object",
]

# The generator's vocabulary, mapped onto the control kinds the UI renders.
_CONTROL_ALIASES: dict[str, ControlKind] = {
    "boolean": "toggle",
    "set_or_unset": "set_or_unset",
    "numeric_boolean": "numeric_boolean",
    "enum": "enum",
    "number": "number",
    "string": "string",
    "path": "path",
    "list": "list",
    "json": "json",
    "secret": "secret",
    "array": "array",
    "object": "object",
}

# Sections whose entries live under a parent key in settings.json rather than
# at the top level, mapped to that parent.
_NESTED_SECTIONS = {
    "permission_settings": "permissions",
    "sandbox_settings": "sandbox",
    "attribution_settings": "attribution",
}


class ClaudeCatalogError(Exception):
    """Raised when the packaged catalog is missing or malformed."""


@dataclass(frozen=True)
class CatalogEntry:
    """One configurable value: what it is, what it accepts, and how to draw it."""

    name: str
    kind: str
    control: ControlKind
    category: str
    group: str
    purpose: str
    common: bool
    default: str | None = None
    example: str | None = None
    values: list[str] = field(default_factory=list)
    deprecated: bool = False
    read_only: bool = False
    managed_only: bool = False

    @property
    def is_env(self) -> bool:
        return self.kind == "env"

    @property
    def is_secret(self) -> bool:
        return self.control == "secret"

    @property
    def editable(self) -> bool:
        """False for values Claude Code owns or that no longer do anything.

        ``managed_only`` stays editable: an admin reading their own machine's
        managed-settings.json is a legitimate use, and the API refuses the write
        by path rather than by flag.
        """

        return not (self.read_only or self.deprecated)

    @property
    def parent_key(self) -> str | None:
        """The settings.json object this entry nests under, if any."""

        return _NESTED_SECTIONS.get(self.kind)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "control": self.control,
            "category": self.category,
            "group": self.group,
            "purpose": self.purpose,
            "common": self.common,
            "editable": self.editable,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.example:
            payload["example"] = self.example
        if self.values:
            payload["values"] = self.values
        if self.parent_key is not None:
            payload["parent_key"] = self.parent_key
        for flag in ("deprecated", "read_only", "managed_only"):
            if getattr(self, flag):
                payload[flag] = True
        return payload


@dataclass(frozen=True)
class ClaudeCodeCatalog:
    """Every entry, indexed the ways the API and the page need to look them up."""

    entries: tuple[CatalogEntry, ...]

    @property
    def by_name(self) -> dict[tuple[str, str], CatalogEntry]:
        return {(entry.kind, entry.name): entry for entry in self.entries}

    def get(self, kind: str, name: str) -> CatalogEntry | None:
        return self.by_name.get((kind, name))

    def env(self) -> tuple[CatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_env)

    def secrets(self) -> frozenset[str]:
        return frozenset(entry.name for entry in self.entries if entry.is_secret)

    def set_or_unset(self) -> frozenset[str]:
        return frozenset(
            entry.name for entry in self.entries if entry.control == "set_or_unset"
        )

    def categories(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for entry in self.entries:
            seen.setdefault(entry.category, None)
        return tuple(seen)

    def as_dict(self) -> dict[str, Any]:
        return {"entries": [entry.as_dict() for entry in self.entries]}


def _coerce_entry(raw: dict[str, Any], *, kind: str) -> CatalogEntry:
    control_raw = str(raw.get("control", "string"))
    control = _CONTROL_ALIASES.get(control_raw)
    if control is None:
        raise ClaudeCatalogError(
            f"unknown control {control_raw!r} for {raw.get('name')}"
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ClaudeCatalogError(f"catalog entry without a name in section {kind}")

    values = raw.get("values", [])
    if not isinstance(values, list):
        raise ClaudeCatalogError(f"{name}: values must be a list")

    return CatalogEntry(
        name=name,
        kind=kind,
        control=control,
        category=str(raw.get("category", "settings")),
        group=str(raw.get("group", "interface")),
        purpose=str(raw.get("purpose", "")),
        common=bool(raw.get("common", False)),
        default=raw.get("default") if isinstance(raw.get("default"), str) else None,
        example=raw.get("example") if isinstance(raw.get("example"), str) else None,
        values=[str(value) for value in values],
        deprecated=bool(raw.get("deprecated", False)),
        read_only=bool(raw.get("read_only", False)),
        managed_only=bool(raw.get("managed_only", False)),
    )


@cache
def load_catalog() -> ClaudeCodeCatalog:
    """Load and validate the packaged catalog, once per process."""

    try:
        raw_text = (
            resources.files(CATALOG_PACKAGE)
            .joinpath(CATALOG_RESOURCE)
            .read_text("utf-8")
        )
    except (OSError, ModuleNotFoundError) as exc:
        raise ClaudeCatalogError(f"cannot read packaged catalog: {exc}") from exc

    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ClaudeCatalogError(f"packaged catalog is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise ClaudeCatalogError("packaged catalog is not a JSON object")

    entries: list[CatalogEntry] = []
    for section, rows in document.items():
        if not isinstance(rows, list):
            continue
        entries.extend(
            _coerce_entry(raw, kind=section) for raw in rows if isinstance(raw, dict)
        )

    if not entries:
        raise ClaudeCatalogError("packaged catalog contains no entries")

    return ClaudeCodeCatalog(entries=tuple(entries))
