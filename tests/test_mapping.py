"""D1 — the mapping table is the contract (SOU-157).

One case per hook event the ticket wires, asserting the exact ``event_type``
and the exact payload the reporter may emit. The expected payloads are written
out in full rather than as subsets: a payload key that was not agreed here is
a leak waiting to happen, so an extra field must fail this test.

Field names the renderer already reads (SOU-156 ``frontend/v3/activity.js``):
``tool``, ``status``, ``file``, ``command``, ``state``.
"""

from __future__ import annotations

import json

import pytest

SID = "0f0f9c3a-7d21-4b0e-9b3a-5d1c2a7e8811"

# fixture -> (event_type, expected payload)
MAPPING = {
    # SessionStart -> session_start (session_id, cwd basename, source)
    "session_start.json": (
        "session_start",
        {"session_id": SID, "cwd": "star-office-ui", "source": "startup"},
    ),
    "session_resume.json": (
        "session_start",
        {"session_id": SID, "cwd": "star-office-ui", "source": "resume"},
    ),
    # UserPromptSubmit -> state_change thinking, never the prompt text
    "user_prompt_submit.json": (
        "state_change",
        {"session_id": SID, "state": "thinking"},
    ),
    # PreToolUse -> tool_call started, whatever the tool is
    "pre_tool_use_bash.json": (
        "tool_call",
        {"tool": "Bash", "status": "started", "session_id": SID},
    ),
    "pre_tool_use_edit.json": (
        "tool_call",
        {"tool": "Edit", "status": "started", "session_id": SID},
    ),
    # PostToolUse -> file_edit for the edit tools, file = basename
    "post_tool_use_edit.json": (
        "file_edit",
        {"tool": "Edit", "file": "app.py", "status": "ok", "session_id": SID,
         "duration_ms": 12},
    ),
    "post_tool_use_write.json": (
        "file_edit",
        {"tool": "Write", "file": "ingest_helpers.py", "status": "ok",
         "session_id": SID, "duration_ms": 4},
    ),
    "post_tool_use_multi_edit.json": (
        "file_edit",
        {"tool": "MultiEdit", "file": "activity.js", "status": "ok",
         "session_id": SID, "duration_ms": 9},
    ),
    "post_tool_use_notebook_edit.json": (
        "file_edit",
        {"tool": "NotebookEdit", "file": "analysis.ipynb", "status": "ok",
         "session_id": SID, "duration_ms": 7},
    ),
    "windows_paths.json": (
        "file_edit",
        {"tool": "Write", "file": "index.ts", "status": "ok",
         "session_id": SID, "duration_ms": 6},
    ),
    # PostToolUse -> command_run for the shell tools: program plus, for an
    # allow-listed CLI, one bare subcommand token (SOU-173). Arguments of the
    # fixture command (-m 'CANARY-COMMIT-MSG') must still never appear.
    "post_tool_use_bash.json": (
        "command_run",
        {"tool": "Bash", "command": "git commit", "status": "ok",
         "session_id": SID, "duration_ms": 4187},
    ),
    "post_tool_use_powershell.json": (
        "command_run",
        {"tool": "PowerShell", "command": "pytest", "status": "ok",
         "session_id": SID, "duration_ms": 900},
    ),
    # PostToolUse -> plain tool_call for everything else
    "post_tool_use_read.json": (
        "tool_call",
        {"tool": "Read", "status": "ok", "session_id": SID, "duration_ms": 3},
    ),
    "post_tool_use_mcp.json": (
        "tool_call",
        {"tool": "mcp__itsaplan__add_comment", "status": "ok", "session_id": SID,
         "duration_ms": 120},
    ),
    # An edit tool with no usable path: fall back to tool_call, never guess
    "tool_no_path.json": (
        "tool_call",
        {"tool": "Edit", "status": "ok", "session_id": SID, "duration_ms": 5},
    ),
    # Stop -> code_completion
    "stop.json": ("code_completion", {"session_id": SID}),
    # Notification -> state_change waiting_for_user (type kept, text dropped)
    "notification.json": (
        "state_change",
        {"session_id": SID, "state": "waiting_for_user",
         "notification_type": "permission_prompt"},
    ),
    "notification_idle.json": (
        "state_change",
        {"session_id": SID, "state": "waiting_for_user",
         "notification_type": "idle_prompt"},
    ),
    # SessionEnd -> session_end
    "session_end.json": ("session_end", {"session_id": SID, "reason": "other"}),
    # Degenerate but real inputs
    "no_session_id.json": (
        "session_start",
        {"cwd": "tools", "source": "startup"},
    ),
    "relative_cwd.json": (
        "session_start",
        {"session_id": SID, "cwd": "relproj", "source": "startup"},
    ),
    "huge.json": (
        "tool_call",
        {"tool": "Bash", "status": "started", "session_id": SID},
    ),
}

ALL_FIXTURES = sorted(MAPPING)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_event_type_and_payload_are_exactly_as_mapped(harness, name):
    """The ticket's mapping table, fixture by fixture, with no extra fields."""
    harness.send(name)
    envelopes = harness.envelopes
    assert len(envelopes) == 1, f"{name}: expected one event, got {len(envelopes)}"
    event_type, payload = MAPPING[name]
    body = envelopes[0]
    assert body["event_type"] == event_type, f"{name}: {body['event_type']!r}"
    assert body["payload"] == payload, f"{name}: payload {body['payload']!r}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_posted_to_events_with_json_content_type(harness, name):
    harness.send(name)
    assert harness.paths == ["/events"], f"{name}: posted to {harness.paths}"
    assert harness.server.requests[0]["content_type"] == "application/json"


def test_envelope_top_level_shape(harness):
    """version=1, agent string, ISO-8601 Z instant, object payload."""
    harness.send("post_tool_use_edit.json")
    body = harness.envelopes[0]
    assert set(body) >= {"version", "agent", "event_type", "timestamp", "payload"}
    assert body["version"] == 1
    assert body["agent"] == "claude-code"
    assert body["timestamp"].endswith("Z")
    assert isinstance(body["payload"], dict)


def test_agent_name_is_configurable(harness):
    result = harness.send("session_start.json", STAR_OFFICE_AGENT="claude-office-01")
    assert result.returncode == 0
    assert harness.envelopes[0]["agent"] == "claude-office-01"


def test_default_agent_is_claude_code(harness):
    harness.send("session_start.json", STAR_OFFICE_AGENT=None)
    assert harness.envelopes[0]["agent"] == "claude-code"


def test_unknown_hook_events_send_nothing(harness):
    """Claude Code documents 30+ events; the ticket wires 7. The rest stay quiet."""
    for name in ["unknown_event.json"]:
        result = harness.send(name)
        assert result.returncode == 0
    assert harness.envelopes == []


def test_agent_name_survives_odd_characters(harness):
    """An agent name with spaces would be refused by the schema; keep it sane."""
    harness.send("session_start.json", STAR_OFFICE_AGENT="  claude-code  ")
    assert harness.envelopes[0]["agent"] == "claude-code"


def test_payload_is_json_serialisable_and_small(harness):
    harness.send("huge.json")
    body = harness.envelopes[0]
    assert len(json.dumps(body["payload"]).encode()) < 65536
