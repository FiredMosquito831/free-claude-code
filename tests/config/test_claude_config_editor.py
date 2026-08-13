"""The editor's job is to make a settings file say what the user meant.

Most of these tests exist because the naive implementation of each one is
wrong in a way that silently does the opposite of what was asked -- writing
``"0"`` to a presence-read variable, leaving an empty ``permissions`` object
behind, or writing a plan the file has since moved out from under.
"""

import json
from collections import Counter
from pathlib import Path
from typing import ClassVar

import pytest

from my_claude_code.config.claude_code_catalog import load_catalog
from my_claude_code.config.claude_config_editor import (
    SECRET_MASK,
    ChangeRequest,
    apply_plan,
    load_document,
    plan_changes,
    read_values,
)
from my_claude_code.config.claude_settings import ClaudeSettingsError


def _write(path: Path, document: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _plan(path: Path, *requests: ChangeRequest):
    return plan_changes(load_document(path), list(requests))


class TestLoadDocument:
    def test_missing_file_parses_as_empty(self, tmp_path: Path) -> None:
        document = load_document(tmp_path / "settings.json")
        assert document.exists is False
        assert document.parsed is True
        assert document.data == {}

    def test_broken_json_reports_rather_than_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("{not json", encoding="utf-8")
        document = load_document(path)
        assert document.parsed is False
        assert document.error

    def test_top_level_array_is_not_a_settings_file(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("[]", encoding="utf-8")
        document = load_document(path)
        assert document.parsed is False
        assert "not an object" in (document.error or "")


class TestReadValues:
    def test_env_block_is_flattened_with_an_env_prefix(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "settings.json",
            {"model": "claude-opus-5", "env": {"API_TIMEOUT_MS": "1200000"}},
        )
        values = read_values(load_document(path))
        assert values["model"] == "claude-opus-5"
        assert values["env.API_TIMEOUT_MS"] == "1200000"

    def test_secrets_are_masked(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "settings.json",
            {"env": {"ANTHROPIC_API_KEY": "sk-ant-real-value"}},
        )
        values = read_values(load_document(path))
        assert values["env.ANTHROPIC_API_KEY"] == SECRET_MASK
        assert "sk-ant-real-value" not in json.dumps(values)

    def test_unknown_keys_survive(self, tmp_path: Path) -> None:
        """Claude Code adds settings weekly; hiding one would misreport the file."""

        path = _write(tmp_path / "settings.json", {"somethingBrandNew": True})
        assert read_values(load_document(path))["somethingBrandNew"] is True


class TestPlanChanges:
    def test_plan_writes_nothing(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {"model": "sonnet"})
        before = path.read_text(encoding="utf-8")
        _plan(path, ChangeRequest("model", "set", "claude-opus-5"))
        assert path.read_text(encoding="utf-8") == before

    def test_before_and_after_describe_the_edit(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {"model": "sonnet"})
        plan = _plan(path, ChangeRequest("model", "set", "claude-opus-5"))
        change = plan.changes[0]
        assert change.before == "sonnet"
        assert change.after == "claude-opus-5"
        assert change.is_noop is False

    def test_setting_a_value_to_what_it_already_is_is_a_noop(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "settings.json", {"model": "sonnet"})
        plan = _plan(path, ChangeRequest("model", "set", "sonnet"))
        assert plan.changes[0].is_noop is True
        assert plan.effective == []

    def test_duplicate_change_for_one_key_is_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(
            path,
            ChangeRequest("model", "set", "a"),
            ChangeRequest("model", "set", "b"),
        )
        assert len(plan.changes) == 1
        assert plan.rejected[0]["name"] == "model"

    def test_unreadable_file_refuses_a_plan(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(ClaudeSettingsError):
            _plan(path, ChangeRequest("model", "set", "sonnet"))


class TestValueTraps:
    """The three shapes that make a naive editor do the opposite of what is asked."""

    def test_turning_off_a_presence_read_variable_removes_the_key(
        self, tmp_path: Path
    ) -> None:
        """Writing "0" to DISABLE_TELEMETRY would keep telemetry OFF, not on."""

        path = _write(tmp_path / "settings.json", {"env": {"DISABLE_TELEMETRY": "1"}})
        plan = _plan(path, ChangeRequest("env.DISABLE_TELEMETRY", "set", "0"))
        change = plan.changes[0]
        assert change.op == "unset"
        assert change.after is None
        assert any("presence" in warning for warning in change.warnings)

        apply_plan(plan)
        assert "DISABLE_TELEMETRY" not in json.loads(
            path.read_text(encoding="utf-8")
        ).get("env", {})

    def test_turning_on_a_presence_read_variable_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.DISABLE_TELEMETRY", "set", "1"))
        assert plan.changes[0].op == "set"
        assert plan.changes[0].after == "1"

    def test_force_hyperlink_rejects_a_word_that_would_enable_it(
        self, tmp_path: Path
    ) -> None:
        """FORCE_HYPERLINK is parsed as a number, so "false" enables hyperlinks."""

        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.FORCE_HYPERLINK", "set", "false"))
        assert plan.changes == []
        assert "number" in plan.rejected[0]["reason"]

    def test_force_hyperlink_accepts_zero(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.FORCE_HYPERLINK", "set", "0"))
        assert plan.changes[0].after == "0"

    def test_read_only_variables_are_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.CLAUDE_CODE_SESSION_ID", "set", "x"))
        assert plan.changes == []
        assert plan.rejected

    def test_env_values_are_coerced_to_strings(self, tmp_path: Path) -> None:
        """settings.json env values are strings; a raw number is silently ignored."""

        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.API_TIMEOUT_MS", "set", 1200000))
        assert plan.changes[0].after == "1200000"

    def test_boolean_true_becomes_the_string_one(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.CLAUDE_CODE_NO_FLICKER", "set", True))
        assert plan.changes[0].after == "1"

    def test_off_catalog_key_warns_but_is_allowed(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("someFutureKey", "set", "yes"))
        assert plan.changes[0].after == "yes"
        assert plan.changes[0].warnings

    def test_a_value_outside_a_documented_enum_warns(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("effortLevel", "set", "turbo"))
        assert plan.changes[0].warnings

    def test_deprecated_keys_warn(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        plan = _plan(path, ChangeRequest("env.ANTHROPIC_SMALL_FAST_MODEL", "set", "x"))
        assert any("deprecated" in w for w in plan.changes[0].warnings)


class TestApplyPlan:
    def test_nested_keys_are_created(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        apply_plan(_plan(path, ChangeRequest("permissions.defaultMode", "set", "plan")))
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "permissions": {"defaultMode": "plan"}
        }

    def test_unsetting_the_last_nested_key_removes_the_empty_parent(
        self, tmp_path: Path
    ) -> None:
        """A leftover {"permissions": {}} makes the file look configured."""

        path = _write(
            tmp_path / "settings.json", {"permissions": {"defaultMode": "plan"}}
        )
        apply_plan(_plan(path, ChangeRequest("permissions.defaultMode", "unset")))
        assert json.loads(path.read_text(encoding="utf-8")) == {}

    def test_a_sibling_key_keeps_its_parent(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "settings.json",
            {"permissions": {"defaultMode": "plan", "allow": ["Bash(ls *)"]}},
        )
        apply_plan(_plan(path, ChangeRequest("permissions.defaultMode", "unset")))
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "permissions": {"allow": ["Bash(ls *)"]}
        }

    def test_unrelated_keys_are_preserved(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "settings.json",
            {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8082"}, "theme": "dark"},
        )
        apply_plan(_plan(path, ChangeRequest("model", "set", "claude-opus-5")))
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8082"
        assert written["theme"] == "dark"

    def test_a_backup_is_taken_before_the_first_write(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {"theme": "dark"})
        apply_plan(_plan(path, ChangeRequest("theme", "set", "light")))
        backup = path.with_name(path.name + ".fcc-backup")
        assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}

    def test_the_backup_is_not_overwritten_by_later_writes(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "settings.json", {"theme": "dark"})
        apply_plan(_plan(path, ChangeRequest("theme", "set", "light")))
        apply_plan(_plan(path, ChangeRequest("theme", "set", "paper")))
        backup = path.with_name(path.name + ".fcc-backup")
        assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}

    def test_creating_the_file_when_it_does_not_exist(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / ".claude" / "settings.json"
        apply_plan(_plan(path, ChangeRequest("theme", "set", "light")))
        assert json.loads(path.read_text(encoding="utf-8")) == {"theme": "light"}

    def test_apply_refuses_when_the_file_broke_after_planning(
        self, tmp_path: Path
    ) -> None:
        """A reviewed plan must not clobber a file that changed underneath it."""

        path = _write(tmp_path / "settings.json", {"theme": "dark"})
        plan = _plan(path, ChangeRequest("theme", "set", "light"))
        path.write_text("{corrupted", encoding="utf-8")
        with pytest.raises(ClaudeSettingsError):
            apply_plan(plan)

    def test_apply_rereads_rather_than_using_the_planned_document(
        self, tmp_path: Path
    ) -> None:
        """An edit made between plan and apply survives."""

        path = _write(tmp_path / "settings.json", {"theme": "dark"})
        plan = _plan(path, ChangeRequest("theme", "set", "light"))
        _write(tmp_path / "settings.json", {"theme": "dark", "verbose": True})

        apply_plan(plan)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written == {"theme": "light", "verbose": True}


class TestCatalogContract:
    def test_the_packaged_catalog_loads(self) -> None:
        catalog = load_catalog()
        assert len(catalog.entries) > 400

    def test_the_six_presence_read_variables_are_marked(self) -> None:
        """If this set changes upstream, the editor's off-switch changes with it."""

        assert load_catalog().set_or_unset() == {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
            "CLAUDE_CODE_TMUX_TRUECOLOR",
            "DISABLE_ERROR_REPORTING",
            "DISABLE_TELEMETRY",
            "FALLBACK_FOR_ALL_PRIMARY_MODELS",
            "IS_DEMO",
        }

    def test_every_credential_variable_is_marked_secret(self) -> None:
        secrets = load_catalog().secrets()
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MCP_CLIENT_SECRET"):
            assert name in secrets

    def test_a_refresh_interval_is_not_mistaken_for_a_credential(self) -> None:
        """CLAUDE_CODE_API_KEY_HELPER_TTL_MS is a number, not a key."""

        catalog = load_catalog()
        assert "CLAUDE_CODE_API_KEY_HELPER_TTL_MS" not in catalog.secrets()
        entry = catalog.get("env", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS")
        assert entry is not None
        assert entry.control == "number"

    def test_enum_entries_carry_their_documented_values(self) -> None:
        entry = load_catalog().get("env", "CLAUDE_CODE_EFFORT_LEVEL")
        assert entry is not None
        assert entry.values == ["low", "medium", "high", "xhigh", "max", "auto"]

    def test_the_common_set_is_a_usable_default_view(self) -> None:
        common = [entry for entry in load_catalog().entries if entry.common]
        assert 80 <= len(common) <= 140
        names = {entry.name for entry in common}
        assert {"model", "permissions.allow", "ANTHROPIC_BASE_URL"} <= names


class TestNestedValuesRoundTrip:
    """A value the editor writes must read back under the key it wrote.

    Found in a real browser, not by the unit tests: after writing
    ``permissions.defaultMode`` the page showed it as unset, because
    ``read_values`` returned only the ``permissions`` object and the UI
    addresses the dotted leaf.
    """

    def test_a_nested_value_is_readable_by_its_dotted_key(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "settings.json",
            {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(ls *)"]}},
        )
        values = read_values(load_document(path))
        assert values["permissions.defaultMode"] == "acceptEdits"
        assert values["permissions.allow"] == ["Bash(ls *)"]

    def test_the_parent_object_is_still_returned(self, tmp_path: Path) -> None:
        """The `permissions` row itself is an object control and needs it."""

        path = _write(
            tmp_path / "settings.json", {"permissions": {"defaultMode": "plan"}}
        )
        values = read_values(load_document(path))
        assert values["permissions"] == {"defaultMode": "plan"}

    def test_two_levels_deep_is_reachable(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "settings.json",
            {"sandbox": {"filesystem": {"allowWrite": ["/tmp/build"]}}},
        )
        values = read_values(load_document(path))
        assert values["sandbox.filesystem.allowWrite"] == ["/tmp/build"]

    def test_writing_then_reading_agrees(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "settings.json", {})
        apply_plan(
            _plan(path, ChangeRequest("permissions.defaultMode", "set", "acceptEdits"))
        )
        values = read_values(load_document(path))
        assert values["permissions.defaultMode"] == "acceptEdits"


class TestGrouping:
    """The page draws its sections from the catalog's `group`.

    Grouping lives in the generator rather than the browser so the page and
    docs/CLAUDE-CODE-CONFIG.md cannot disagree about where a setting is. A
    missing group silently collapsed all 101 default rows into one section.
    """

    GROUPS: ClassVar[set[str]] = {
        "model",
        "context",
        "permissions",
        "tools",
        "agents",
        "mcp",
        "connection",
        "interface",
        "privacy",
    }

    def test_every_entry_has_a_known_group(self) -> None:
        for entry in load_catalog().entries:
            assert entry.group in self.GROUPS, (entry.name, entry.group)

    def test_no_group_swallows_the_catalog(self) -> None:
        """One giant section is the failure mode this replaced."""

        entries = load_catalog().entries
        counts = Counter(entry.group for entry in entries)
        assert len(counts) == len(self.GROUPS)
        assert max(counts.values()) < len(entries) / 3

    def test_permission_and_sandbox_keys_group_together(self) -> None:
        catalog = load_catalog()
        for entry in catalog.entries:
            if entry.name.startswith(("permissions.", "sandbox.")):
                assert entry.group == "permissions", entry.name

    def test_the_rule_editor_keys_are_arrays(self) -> None:
        """The page renders a rule builder for these three, so they must be lists."""

        catalog = load_catalog()
        for name in ("permissions.allow", "permissions.ask", "permissions.deny"):
            entry = next(e for e in catalog.entries if e.name == name)
            assert entry.control == "array", (name, entry.control)

    def test_the_default_view_stays_scannable(self) -> None:
        """Every group should be reachable without turning on Show all."""

        common = [entry for entry in load_catalog().entries if entry.common]
        assert {entry.group for entry in common} >= self.GROUPS
