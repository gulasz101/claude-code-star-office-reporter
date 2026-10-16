"""D4/D5 — packaging: stdlib only, and the wiring + docs that make it usable.

The ticket asks for a Python 3 stdlib-only script, a ``settings.example.json``
that wires every mapped event, and a README covering install, env vars and the
event table. Those are contracts, so they are tested, not promised.
"""

from __future__ import annotations

import ast
import json
import sys

import pytest

from conftest import HOOK_SCRIPT, README_PATH, SETTINGS_PATH

HOOK_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "Notification", "SessionEnd", "SubagentStart", "SubagentStop",
]
EVENT_TYPES = [
    "session_start", "state_change", "tool_call", "file_edit",
    "command_run", "code_completion", "session_end", "background_start", "background_end",
]


def test_script_imports_the_standard_library_only():
    """No pip install for a hook: only modules in sys.stdlib_module_names."""
    tree = ast.parse(HOOK_SCRIPT.read_text(), filename=str(HOOK_SCRIPT))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: a package inside reporter/
                imported.append("~local")
            elif node.module:
                imported.append(node.module.split(".")[0])
    assert imported, "the script should import something"
    non_std = sorted({name for name in imported if name not in sys.stdlib_module_names})
    assert non_std == [], f"third-party imports: {non_std}"


def test_script_declares_python3_and_is_executable():
    text = HOOK_SCRIPT.read_text()
    first = text.splitlines()[0] if text.splitlines() else ""
    assert first.startswith("#!") and "python" in first.lower(), first
    assert HOOK_SCRIPT.stat().st_mode & 0o111, "chmod +x: the docs' own requirement"


def test_script_needs_no_arguments_to_report():
    """Claude Code passes the payload on stdin; the hook must not need argv."""
    from conftest import StubServer, run_hook, fixture_bytes

    with StubServer() as server:
        result = run_hook(fixture_bytes("session_start.json"), url=server.url)
    assert result.returncode == 0, result.stderr[:400]
    assert len(server.requests) == 1


def test_hooks_json_is_valid_and_wires_every_event():
    assert SETTINGS_PATH.exists(), "the plugin wires its hooks in hooks/hooks.json"
    config = json.loads(SETTINGS_PATH.read_text())
    assert "hooks" in config, list(config)
    hooks = config["hooks"]
    for event in HOOK_EVENTS:
        assert event in hooks, f"{event} is not wired"
        groups = hooks[event]
        assert isinstance(groups, list) and groups, event
        for group in groups:
            handlers = group.get("hooks")
            assert isinstance(handlers, list) and handlers, f"{event} has no handler"
            for handler in handlers:
                assert handler.get("type") == "command", handler
                line = " ".join([str(handler.get("command", ""))]
                                + [str(a) for a in handler.get("args", [])])
                assert "star_office_hook.py" in line, (event, line)


def test_hooks_json_timeouts_leave_room_for_the_network_cap():
    """The script caps the POST at 2 s; Claude Code must not kill it mid-flight.

    The hooks reference gives ``SessionEnd`` a 1.5 s shared budget unless a
    per-hook ``timeout`` raises it, so the example must set one there too.
    """
    config = json.loads(SETTINGS_PATH.read_text())
    for event, groups in config["hooks"].items():
        for group in groups:
            for handler in group.get("hooks", []):
                timeout = handler.get("timeout")
                assert isinstance(timeout, int) and 3 <= timeout <= 60, (event, timeout)
    assert config["hooks"]["SessionEnd"], "SessionEnd needs the raised budget"


def test_hooks_json_never_hardcodes_a_person():
    """An example must be copy-pasteable: no home directory, no username."""
    text = SETTINGS_PATH.read_text()
    assert "/home/" not in text and "/Users/" not in text, text[:200]
    assert "${CLAUDE_PROJECT_DIR}" in text or "CLAUDE_PLUGIN_ROOT" in text


def test_readme_documents_install_env_vars_and_the_event_table():
    assert README_PATH.exists(), "README.md documents the plugin"
    text = README_PATH.read_text()
    for token in [
        "STAR_OFFICE_URL", "STAR_OFFICE_AGENT", "STAR_OFFICE_TIMEOUT",
        "http://127.0.0.1:19000", "claude-code",
        "/plugin marketplace add", "/plugin install",
        "pytest", "event_v1.json",
    ]:
        assert token in text, f"README must mention {token!r}"
    for event in HOOK_EVENTS + EVENT_TYPES:
        assert event in text, f"README event table must list {event}"


def test_readme_states_the_two_guarantees_the_owner_cares_about():
    text = README_PATH.read_text().lower()
    assert "exit 0" in text, "the fail-open promise must be written down"
    assert "prompt" in text and "never" in text, "the privacy promise must be written down"
    for forbidden in ["prompt text", "file content", "tool output", "full command", "absolute path"]:
        assert forbidden in text, forbidden


def test_plugin_manifests_name_the_plugin_and_the_marketplace_lists_it():
    root = SETTINGS_PATH.parent.parent
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["name"] == "star-office-reporter"
    assert [p["name"] for p in market["plugins"]] == [plugin["name"]]
    assert market["plugins"][0]["source"] == "./"


def test_hooks_json_runs_the_script_from_the_plugin_root():
    text = SETTINGS_PATH.read_text()
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/star_office_hook.py" in text
