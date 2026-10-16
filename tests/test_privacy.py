"""D3 — privacy. Nothing the ticket forbids may leave the machine.

The ticket bans prompt text, file contents, tool output, full commands and
full paths. Each forbidden value in the fixtures carries a ``CANARY-`` token,
so a single sweep over every captured envelope proves all five at once; the
structural tests then prove it for inputs that carry no canary (a path with an
innocent-looking name must still be reduced to its basename).
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import EMITTING_FIXTURES, SILENT_FIXTURES, fixture_bytes

ALL_FIXTURES = sorted(EMITTING_FIXTURES)

FORBIDDEN_KEYS = {
    "prompt", "content", "old_string", "new_string", "new_source", "edits",
    "tool_response", "message", "title", "error", "transcript_path",
    "last_assistant_message", "description", "stdout", "stderr",
    "scratchpad_dir", "cell_id", "background_tasks", "session_crons",
}

# Every forbidden value in every fixture starts with this token.
CANARY = "CANARY"

PATHISH = re.compile(r"[/\\]|\.\.[/\\]|[A-Za-z]:[/\\]")


def walk(node, path="payload"):
    """Yield (path, key, value) for every scalar in a payload tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_canary_leaves_the_script(all_envelopes, name):
    body = json.dumps(all_envelopes[name], sort_keys=True)
    assert CANARY not in body, f"{name}: canary leaked -> {body[:400]}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_path_separators_in_any_value(all_envelopes, name):
    """Basename-only rule: no absolute path, no directory, no drive letter."""
    payload = all_envelopes[name]["payload"]
    for path, key, value in walk(payload):
        if isinstance(value, str):
            assert not PATHISH.search(value), f"{name}: {path}={value!r} looks like a path"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_forbidden_payload_keys(all_envelopes, name):
    payload = all_envelopes[name]["payload"]
    for path, key, _value in walk(payload):
        assert key.lower() not in FORBIDDEN_KEYS, f"{name}: key {path}.{key} is forbidden"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_raw_newlines_or_giant_values(all_envelopes, name):
    """A one-line signal, not a blob: the office wants the fact, not the text."""
    for _path, _key, value in walk(all_envelopes[name]["payload"]):
        if isinstance(value, str):
            assert "\n" not in value, name
            assert len(value) <= 120, (name, value[:120])


def test_shell_command_is_program_plus_allow_listed_subcommand_only(harness):
    """Full commands are forbidden: the program name plus, for the allow-listed
    CLI families (SOU-173), at most a bare subcommand — never a value, path or
    flag. See test_subcommands.py for the full table."""
    cases = [
        ("git commit -m 'note to self'", "git commit"),
        ("  /usr/bin/env FOO=bar npm run build ", "npm run build"),
        ("sudo -n rm -rf /tmp/x", "rm"),
        ("pytest reporter/claude-code/tests -q", "pytest"),
        ("docker compose up -d", "docker compose"),
        ("cat notes.txt | grep CANARY", "cat"),
    ]
    for command, expected in cases:
        harness.server.requests.clear()
        raw = json.dumps({
            "session_id": "s1", "hook_event_name": "PostToolUse", "cwd": "/x/CANARY-DIR/proj",
            "tool_name": "Bash", "tool_input": {"command": command},
        }).encode()
        harness.send_raw(raw)
        body = harness.envelopes[0]
        assert body["event_type"] == "command_run", command
        assert body["payload"]["command"] == expected, command
        assert CANARY not in json.dumps(body), command


def test_empty_and_whitespace_command_still_exits_zero(harness):
    raw = json.dumps({
        "session_id": "s1", "hook_event_name": "PostToolUse",
        "tool_name": "Bash", "tool_input": {"command": "   "},
    }).encode()
    result = harness.send_raw(raw)
    assert result.returncode == 0
    body = harness.envelopes[0]
    assert body["event_type"] == "command_run"
    assert body["payload"]["command"] == "shell"


@pytest.mark.parametrize("name", sorted(SILENT_FIXTURES))
def test_refused_inputs_emit_nothing_and_leak_nothing(harness, name):
    """Undecodable or unmapped stdin: no POST, and no text on stdout either."""
    result = harness.send(name)
    assert result.returncode == 0, name
    assert harness.envelopes == [], name
    assert result.stdout == b"", name
    assert fixture_bytes(name), "a fixture must have content to test against"
