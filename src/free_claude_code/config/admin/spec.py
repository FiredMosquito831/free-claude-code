"""Neutral Admin manifest spec types shared by manifest generators."""

from dataclasses import dataclass
from typing import Literal

FieldType = Literal[
    "text",
    "secret",
    "number",
    "boolean",
    "model",
    "optional_model",
    "model_chain",
    "select",
    "textarea",
    "oauth_login",
]


@dataclass(frozen=True, slots=True)
class ConfigSectionSpec:
    """A group of config fields rendered together in the admin UI."""

    section_id: str
    label: str
    description: str
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class ConfigOptionSpec:
    """A persisted option value and its user-facing label."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ConfigFieldSpec:
    """Typed metadata for one env-backed admin setting."""

    key: str
    label: str
    section_id: str
    field_type: FieldType = "text"
    settings_attr: str | None = None
    default: str = ""
    options: tuple[str | ConfigOptionSpec, ...] = ()
    secret: bool = False
    advanced: bool = False
    restart_required: bool = False
    session_sensitive: bool = False
    description: str = ""
