"""D2 — the reporter must never block or break Claude Code.

The rules the ticket sets: a 2 s network timeout, every error swallowed, always
``exit 0``, nothing printed to stdout. The docs matter here: exit code 2 blocks
the action, and on ``UserPromptSubmit``/``SessionStart`` plain-text stdout is
added to Claude's context — so a chatty or non-zero hook is a broken session.

Timing is asserted as an upper bound, because "does not stall the turn" is a
statement about wall-clock, not about exit codes.
"""

from __future__ import annotations

import json

import pytest

from conftest import StubServer, free_port, run_hook, fixture_bytes
from conftest import ROOT, HOOK_SCRIPT

SESSION_START = fixture_bytes("session_start.json")


def test_dashboard_down_exits_zero_quickly():
    """The AC case: no dashboard listening at all -> exit 0 within 3 s."""
    port = free_port()
    result = run_hook(SESSION_START, url=f"http://127.0.0.1:{port}")
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.elapsed < 3.0, f"took {result.elapsed:.2f}s"


def test_hanging_dashboard_is_abandoned_at_the_timeout():
    """A server that accepts and then stalls must not hold the turn open."""
    with StubServer(delay=5.0) as server:
        result = run_hook(SESSION_START, url=server.url)
    assert result.returncode == 0
    assert result.elapsed < 4.0, f"took {result.elapsed:.2f}s"


def test_timeout_is_configurable_and_honoured():
    with StubServer(delay=3.0) as server:
        result = run_hook(SESSION_START, url=server.url,
                           env={"STAR_OFFICE_TIMEOUT": "0.5"})
    assert result.returncode == 0
    assert result.elapsed < 2.0, f"took {result.elapsed:.2f}s"


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_any_http_answer_still_exits_zero(status):
    """A rejected event (bad schema, rate limit, crash) is not our caller's problem."""
    with StubServer(status=status) as server:
        result = run_hook(SESSION_START, url=server.url)
    assert result.returncode == 0
    assert result.stdout == b""
    assert server.requests, "the request should at least have been attempted"


def test_unroutable_host_exits_zero():
    """A URL that can never connect must fail fast and silent, not hang."""
    result = run_hook(SESSION_START, url="http://127.0.0.1:1/events")
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.elapsed < 3.0


@pytest.mark.parametrize("url", ["not a url", "http://", "ftp://example.invalid/", "127.0.0.1:9000"])
def test_nonsense_url_is_swallowed(url):
    result = run_hook(SESSION_START, url=url)
    assert result.returncode == 0
    assert result.stdout == b""


def test_explicitly_empty_url_switches_reporting_off():
    """``STAR_OFFICE_URL=""`` is the operator's kill switch, not a fall-back.

    Tested against the compiled-in default on purpose: the bug this pins down
    was found by running the suite while a real dashboard listened on 19000 —
    an empty URL fell back to the default and wrote a live ``session_start``
    row into that office. If 19000 is taken (a real dashboard running) the test
    skips rather than post into someone's session.
    """
    try:
        server = StubServer(port=19000)
    except OSError:
        pytest.skip("port 19000 is in use by a real dashboard")
    with server:
        result = run_hook(SESSION_START, url="http://127.0.0.1:19000",
                          env={"STAR_OFFICE_URL": ""})
    assert result.returncode == 0
    assert result.stdout == b""
    assert server.requests == [], (
        f"an empty STAR_OFFICE_URL still reported to the default: {server.requests}"
    )


CASES = {
    "empty stdin": b"",
    "not json": b"definitely not json\n",
    "truncated json": b'{"session_id": "abc", "hook_event_name": "Session',
    "json array": b'[{"hook_event_name": "SessionStart"}]',
    "json string": b'"SessionStart"',
    "json null": b"null",
    "json number": b"42",
    "invalid utf-8": b"\xff\xfe\x00binary",
    "empty object": b"{}",
    "no hook_event_name": b'{"session_id": "abc"}',
    "numeric event name": b'{"hook_event_name": 7}',
    "tool_event_without_tool_name": b'{"hook_event_name": "PostToolUse"}',
    "numeric tool_name": b'{"hook_event_name":"PostToolUse","tool_name":42}',
    "unknown event": b'{"hook_event_name":"PreCompact","trigger":"auto"}',
}

# Wrong-typed fields on a mapped event: the event is still real, so it is still
# reported - with only the fields that could be trusted.
DEGRADED = {
    b'{"hook_event_name":"PostToolUse","tool_name":"Edit","tool_input":null}':
        ("tool_call", {"tool": "Edit", "status": "ok"}),
    b'{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":"ls"}':
        ("command_run", {"tool": "Bash", "command": "shell", "status": "ok"}),
    b'{"hook_event_name":"SessionStart","cwd":7,"source":"startup"}':
        ("session_start", {"source": "startup"}),
    b'{"hook_event_name":"SessionStart","session_id":"s"}':
        ("session_start", {"session_id": "s"}),
    b'{"hook_event_name":"PreToolUse","session_id":"s","tool_name":"Grep","tool_input":{}}':
        ("tool_call", {"tool": "Grep", "status": "started", "session_id": "s"}),
}


@pytest.mark.parametrize("payload, expected", sorted(DEGRADED.items(), key=lambda kv: kv[0]))
def test_wrong_typed_fields_degrade_to_the_minimal_event(payload, expected, harness):
    event_type, payload_part = expected
    result = harness.send_raw(payload)
    assert result.returncode == 0, result.stderr[:400]
    assert result.stdout == b""
    assert len(harness.envelopes) == 1, harness.envelopes
    body = harness.envelopes[0]
    assert body["event_type"] == event_type
    assert body["payload"] == payload_part


@pytest.mark.parametrize("label, payload", sorted(CASES.items()))
def test_undecodable_or_unmapped_input_is_silent_and_clean(label, payload, harness):
    """No traceback, no stdout, exit 0: Claude Code sees a hook that did nothing."""
    result = harness.send_raw(payload)
    assert result.returncode == 0, f"{label}: rc={result.returncode} err={result.stderr[:400]!r}"
    assert result.stdout == b"", f"{label}: stdout would reach Claude as context"
    assert b"Traceback" not in result.stderr, label
    assert harness.envelopes == [], f"{label}: must not invent an event"


def test_missing_script_is_not_the_case_here():
    """Guard the suite itself: the script must exist and be runnable at all."""
    assert HOOK_SCRIPT.exists(), "S2 owns reporter/claude-code/star_office_hook.py"
    assert HOOK_SCRIPT.stat().st_mode & 0o111, "the hooks docs require an executable script"


def test_no_temporary_or_state_files_are_written(harness):
    """A hook that leaves files behind is a leak of a different kind."""
    before = {p.name for p in ROOT.rglob("*.sou-hook-tmp*")}
    harness.send("post_tool_use_edit.json")
    after = {p.name for p in ROOT.rglob("*.sou-hook-tmp*")}
    assert before == after


def test_environment_override_of_the_dashboard_url_is_used(harness):
    """The URL is the operator's, not the developer's: /events is appended, not replaced."""
    result = harness.send("session_start.json")
    assert result.returncode == 0
    assert harness.paths == ["/events"]
