"""Read, plan, and apply edits to a Claude Code settings.json.

``claude_settings`` owns the proxy link -- the two keys that point Claude Code
at this server. This module owns everything else: reading the whole document,
saying which scope actually supplies each value, turning a set of requested
changes into a reviewable diff, and writing that diff atomically.

The write path is deliberately two-stage. ``plan_changes`` never touches disk;
it resolves every requested change against the catalog, records what the value
is now and what it would become, and attaches a warning for anything the user
would otherwise get wrong. Only ``apply_plan`` writes, and only what the plan
said it would.

The rule that most needs enforcing here is the "set or unset" family: six
variables that Claude Code reads for *presence*, so writing ``"0"`` turns them
on. A UI toggle set to off must delete the key, and this module rewrites such a
request rather than trusting the caller to have known.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from my_claude_code.config.claude_code_catalog import (
    CatalogEntry,
    ClaudeCodeCatalog,
    load_catalog,
)
from my_claude_code.config.claude_settings import (
    CLAUDE_SETTINGS_BACKUP_SUFFIX,
    ClaudeSettingsError,
)
from my_claude_code.config.paths import claude_managed_settings_paths

SECRET_MASK = "********"
FALSEY_TEXT = frozenset({"0", "false", "no", "off", ""})

type ChangeOp = Literal["set", "unset"]


@dataclass(frozen=True)
class SettingsDocument:
    """A parsed settings.json, or the reason it could not be parsed."""

    path: str
    exists: bool
    parsed: bool
    error: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class ChangeRequest:
    """One requested edit, addressed by the catalog's dotted key name."""

    name: str
    op: ChangeOp
    value: Any = None


@dataclass(frozen=True)
class PlannedChange:
    """A resolved edit: what it does, and anything the user should know first."""

    name: str
    op: ChangeOp
    before: Any
    after: Any
    warnings: list[str] = field(default_factory=list)
    secret: bool = False

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the browser. A credential is masked on the way out.

        Masking lives here rather than in the API so the diff a caller renders
        cannot leak the value the caller is setting: the apply response echoes
        the plan, and an earlier version of this returned the raw key in
        ``after`` while dutifully masking it everywhere else.
        """

        return {
            "name": self.name,
            "op": self.op,
            "before": _masked(self.before) if self.secret else self.before,
            "after": _masked(self.after) if self.secret else self.after,
            "warnings": self.warnings,
            "noop": self.is_noop,
        }


@dataclass(frozen=True)
class ChangePlan:
    """Every requested edit, resolved. ``rejected`` never reaches disk."""

    path: str
    changes: list[PlannedChange]
    rejected: list[dict[str, str]]

    @property
    def effective(self) -> list[PlannedChange]:
        return [change for change in self.changes if not change.is_noop]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "changes": [change.as_dict() for change in self.changes],
            "rejected": self.rejected,
            "effective_count": len(self.effective),
        }


def _split(name: str) -> list[str]:
    return name.split(".")


def _read_pointer(data: dict[str, Any], name: str) -> Any:
    """Return the value at a dotted key, or None when any segment is missing."""

    cursor: Any = data
    for segment in _split(name):
        if not isinstance(cursor, dict) or segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


def _write_pointer(data: dict[str, Any], name: str, value: Any) -> None:
    segments = _split(name)
    cursor = data
    for segment in segments[:-1]:
        existing = cursor.get(segment)
        if not isinstance(existing, dict):
            existing = {}
            cursor[segment] = existing
        cursor = existing
    cursor[segments[-1]] = value


def _clear_pointer(data: dict[str, Any], name: str) -> None:
    """Remove a dotted key, and any parent objects it leaves empty.

    Leaving ``{"permissions": {}}`` behind after removing the last rule would
    make the file look configured when it is not, and Claude Code treats an
    empty object and a missing one identically.
    """

    segments = _split(name)
    chain: list[dict[str, Any]] = [data]
    cursor: Any = data
    for segment in segments[:-1]:
        cursor = cursor.get(segment) if isinstance(cursor, dict) else None
        if not isinstance(cursor, dict):
            return
        chain.append(cursor)

    chain[-1].pop(segments[-1], None)

    for depth in range(len(chain) - 1, 0, -1):
        if chain[depth]:
            break
        chain[depth - 1].pop(segments[depth - 1], None)


def load_document(path: Path) -> SettingsDocument:
    """Read a settings.json without judging its contents."""

    path = path.absolute()
    if not path.exists():
        return SettingsDocument(str(path), False, True, None, {})

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SettingsDocument(str(path), True, False, str(exc), {})

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return SettingsDocument(str(path), True, False, str(exc), {})

    if not isinstance(data, dict):
        return SettingsDocument(
            str(path), True, False, "top-level JSON value is not an object", {}
        )

    return SettingsDocument(str(path), True, True, None, data)


def _entry_for(catalog: ClaudeCodeCatalog, name: str) -> CatalogEntry | None:
    """Find a catalog entry by dotted name across every section.

    Environment variables live under the ``env.`` prefix in a settings file but
    are catalogued by their bare name, so both spellings resolve.
    """

    bare = name.removeprefix("env.")
    for entry in catalog.entries:
        if entry.name == name or (entry.is_env and entry.name == bare):
            return entry
    return None


def _masked(value: Any) -> Any:
    return SECRET_MASK if isinstance(value, str) and value else value


def read_values(
    document: SettingsDocument, *, catalog: ClaudeCodeCatalog | None = None
) -> dict[str, Any]:
    """Return every catalogued value the document sets, secrets masked.

    Keys the catalog does not know are returned too: Claude Code adds settings
    weekly, and silently dropping one from the editor would make it look like
    the file does not contain it.
    """

    catalog = catalog or load_catalog()
    secrets = catalog.secrets()
    values: dict[str, Any] = {}

    for key, value in document.data.items():
        if key == "env" and isinstance(value, dict):
            for env_name, env_value in value.items():
                values[f"env.{env_name}"] = (
                    _masked(env_value) if env_name in secrets else env_value
                )
            continue
        values[key] = value

    return values


def _validate(
    entry: CatalogEntry | None, request: ChangeRequest
) -> tuple[ChangeRequest, list[str], str | None]:
    """Return the request to actually plan, its warnings, and any rejection."""

    warnings: list[str] = []

    if "[]" in request.name:
        return request, warnings, "structured list entries are not editable by key"

    if entry is None:
        warnings.append(
            "not in the catalog for this Claude Code version -- it will be "
            "written verbatim"
        )
        return request, warnings, None

    if entry.read_only:
        return request, warnings, "Claude Code sets this itself; it cannot be written"

    if entry.deprecated:
        warnings.append("deprecated upstream: setting it has no effect")

    if entry.control == "set_or_unset" and request.op == "set":
        text = "" if request.value is None else str(request.value).strip().lower()
        if text in FALSEY_TEXT:
            warnings.append(
                "this variable is read for presence, so a falsey value would "
                "still enable it -- removing the key instead"
            )
            return ChangeRequest(request.name, "unset"), warnings, None

    if entry.control == "numeric_boolean" and request.op == "set":
        text = "" if request.value is None else str(request.value).strip().lower()
        if text in FALSEY_TEXT - {"0"}:
            return (
                request,
                warnings,
                f"{entry.name} is parsed as a number, so {request.value!r} would "
                "enable it; use 0 to turn it off",
            )

    if entry.control == "enum" and request.op == "set" and entry.values:
        literal = [
            value for value in entry.values if "<" not in value and value != "auto:N"
        ]
        if literal and str(request.value) not in literal:
            warnings.append(f"not one of the documented values ({', '.join(literal)})")

    if entry.is_env and request.op == "set" and not isinstance(request.value, str):
        warnings.append("env values are strings in settings.json; coerced")
        return (
            ChangeRequest(request.name, "set", _as_env_text(request.value)),
            warnings,
            None,
        )

    return request, warnings, None


def _as_env_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    return str(value)


def plan_changes(
    document: SettingsDocument,
    requests: list[ChangeRequest],
    *,
    catalog: ClaudeCodeCatalog | None = None,
) -> ChangePlan:
    """Resolve requested edits into a reviewable diff. Touches no disk."""

    if not document.parsed:
        raise ClaudeSettingsError(
            f"cannot plan changes against an unreadable settings file: {document.error}"
        )

    catalog = catalog or load_catalog()
    changes: list[PlannedChange] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()

    for request in requests:
        if request.name in seen:
            rejected.append({"name": request.name, "reason": "duplicate change"})
            continue
        seen.add(request.name)

        entry = _entry_for(catalog, request.name)
        resolved, warnings, rejection = _validate(entry, request)
        if rejection is not None:
            rejected.append({"name": request.name, "reason": rejection})
            continue

        before = _read_pointer(document.data, resolved.name)
        after = None if resolved.op == "unset" else resolved.value
        changes.append(
            PlannedChange(
                name=resolved.name,
                op=resolved.op,
                before=before,
                after=after,
                warnings=warnings,
                secret=entry is not None and entry.is_secret,
            )
        )

    return ChangePlan(path=document.path, changes=changes, rejected=rejected)


def _backup_once(path: Path) -> str | None:
    backup_path = path.with_name(path.name + CLAUDE_SETTINGS_BACKUP_SUFFIX)
    if path.exists() and not backup_path.exists():
        shutil.copyfile(path, backup_path)
        return str(backup_path)
    return str(backup_path) if backup_path.exists() else None


def apply_plan(plan: ChangePlan) -> SettingsDocument:
    """Write the planned changes atomically, after backing the file up once.

    Re-reads the document rather than trusting the one the plan was built
    against: a plan the user reviewed for a while must not silently clobber an
    edit Claude Code or another tool made in the meantime.
    """

    path = Path(plan.path).absolute()
    document = load_document(path)
    if not document.parsed:
        raise ClaudeSettingsError(
            f"cannot write {path}: the file changed and no longer parses "
            f"({document.error})"
        )

    data = json.loads(json.dumps(document.data))

    for change in plan.effective:
        if change.op == "unset":
            _clear_pointer(data, change.name)
        else:
            _write_pointer(data, change.name, change.after)

    try:
        _backup_once(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".fcc-tmp")
        tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        raise ClaudeSettingsError(f"cannot write {path}: {exc}") from exc

    return load_document(path)


def managed_overrides(names: list[str]) -> list[dict[str, Any]]:
    """Return the managed settings files that also set any of ``names``.

    Managed settings outrank every other scope, so editing a user file that a
    policy already pins changes nothing. The page needs to say so rather than
    let the user write a value that will never apply.
    """

    overrides: list[dict[str, Any]] = []
    wanted = set(names)

    for managed_path in claude_managed_settings_paths():
        document = load_document(managed_path)
        if not document.parsed:
            continue
        pinned = sorted(
            name for name in wanted if _read_pointer(document.data, name) is not None
        )
        if pinned:
            overrides.append({"path": document.path, "keys": pinned})

    return overrides
