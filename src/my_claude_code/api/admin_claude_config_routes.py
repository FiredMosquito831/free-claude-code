"""Admin API for the Configure Claude Code settings editor.

Four routes, matching how the page works:

``GET  /admin/api/claude-config/catalog``  what Claude Code can be configured to do
``GET  /admin/api/claude-config/document`` what this machine's settings file says
``POST /admin/api/claude-config/plan``     what a set of edits would change
``POST /admin/api/claude-config/apply``    make those edits

Plan and apply are separate so the page can show a real diff before writing.
Apply re-plans server-side rather than trusting the diff the browser sends
back: the browser's copy is a rendering, not an authority, and a plan the user
looked at for a minute must not clobber a change made in the meantime.

Writes are confined to Claude Code settings files. Nothing here accepts an
arbitrary path, because an admin API that writes JSON anywhere on the box is a
different and much larger thing than a settings editor.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from my_claude_code.config.claude_code_catalog import (
    ClaudeCatalogError,
    load_catalog,
)
from my_claude_code.config.claude_config_editor import (
    ChangePlan,
    ChangeRequest,
    PlannedChange,
    apply_plan,
    load_document,
    managed_overrides,
    plan_changes,
    read_values,
)
from my_claude_code.config.claude_discovery import discover_settings_files
from my_claude_code.config.claude_settings import ClaudeSettingsError
from my_claude_code.config.paths import claude_settings_path

from .admin_routes import require_loopback_admin

router = APIRouter()

CLAUDE_SETTINGS_FILENAMES = ("settings.json", "settings.local.json")
CLAUDE_DIRNAME = ".claude"


class ChangePayload(BaseModel):
    """One requested edit, as the page sends it."""

    name: str = Field(min_length=1, max_length=200)
    op: str = Field(pattern="^(set|unset)$")
    value: Any = None


class PlanPayload(BaseModel):
    """A set of edits against one settings file."""

    path: str | None = None
    changes: list[ChangePayload] = Field(default_factory=list, max_length=200)


def _resolve_target(raw_path: str | None) -> Path:
    """Return the settings file to act on, refusing anything that is not one.

    The page offers the user file by default and a typed path for a project's
    ``.claude/settings.json``. Both shapes are checked here rather than trusted,
    so a crafted request cannot turn this into a general file writer.
    """

    if raw_path is None or not raw_path.strip():
        return claude_settings_path()

    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")

    if candidate.name not in CLAUDE_SETTINGS_FILENAMES:
        raise HTTPException(
            status_code=400,
            detail=(
                "path must name a Claude Code settings file "
                f"({' or '.join(CLAUDE_SETTINGS_FILENAMES)})"
            ),
        )

    if candidate.parent.name != CLAUDE_DIRNAME:
        raise HTTPException(
            status_code=400,
            detail=f"path must sit inside a {CLAUDE_DIRNAME}/ directory",
        )

    return candidate.absolute()


def _plan_for(payload: PlanPayload) -> tuple[Path, ChangePlan]:
    target = _resolve_target(payload.path)
    document = load_document(target)

    requests = [
        ChangeRequest(
            name=change.name,
            op="unset" if change.op == "unset" else "set",
            value=change.value,
        )
        for change in payload.changes
    ]

    try:
        plan = plan_changes(document, requests)
    except ClaudeSettingsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return target, plan


def _plan_response(plan: ChangePlan) -> dict[str, Any]:
    payload = plan.as_dict()
    payload["managed_overrides"] = managed_overrides(
        [change.name for change in plan.effective]
    )
    return payload


@router.get("/admin/api/claude-config/catalog")
async def get_claude_config_catalog(
    _: None = Depends(require_loopback_admin),
) -> dict[str, Any]:
    """Return every configurable Claude Code value and the control it wants."""

    try:
        catalog = load_catalog()
    except ClaudeCatalogError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = catalog.as_dict()
    payload["categories"] = list(catalog.categories())
    return payload


@router.get("/admin/api/claude-config/document")
async def get_claude_config_document(
    path: str | None = Query(default=None),
    _: None = Depends(require_loopback_admin),
) -> dict[str, Any]:
    """Return the current contents of one settings file, secrets masked."""

    target = _resolve_target(path)
    document = load_document(target)
    values = read_values(document) if document.parsed else {}

    return {
        "path": document.path,
        "exists": document.exists,
        "parsed": document.parsed,
        "error": document.error,
        "is_default": target == claude_settings_path(),
        "default_path": str(claude_settings_path()),
        "candidates": [entry.path for entry in discover_settings_files()],
        "values": values,
        "managed_overrides": managed_overrides(sorted(values)),
    }


@router.post("/admin/api/claude-config/plan")
async def plan_claude_config(
    payload: PlanPayload,
    _: None = Depends(require_loopback_admin),
) -> dict[str, Any]:
    """Resolve edits into a reviewable diff without touching disk."""

    _target, plan = _plan_for(payload)
    return _plan_response(plan)


@router.post("/admin/api/claude-config/apply")
async def apply_claude_config(
    payload: PlanPayload,
    _: None = Depends(require_loopback_admin),
) -> dict[str, Any]:
    """Re-plan the edits server-side, then write them."""

    target, plan = _plan_for(payload)

    if not plan.effective:
        document = load_document(target)
        return {
            "applied": [],
            "plan": _plan_response(plan),
            "values": read_values(document),
            "path": document.path,
        }

    try:
        document = apply_plan(plan)
    except ClaudeSettingsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "applied": [_applied(change) for change in plan.effective],
        "plan": _plan_response(plan),
        "values": read_values(document),
        "path": document.path,
    }


def _applied(change: PlannedChange) -> dict[str, Any]:
    return {"name": change.name, "op": change.op}
