"""Ingest token (dashboard SOU-219): sent as a Bearer header when configured, never leaked."""

from __future__ import annotations

from conftest import StubServer, fixture_bytes, run_hook

TOKEN = "tok_CANARY-abcdefghijklmnop"


def test_token_is_sent_as_bearer_header():
    with StubServer() as server:
        result = run_hook(fixture_bytes("session_start.json"), url=server.url, env={"STAR_OFFICE_TOKEN": TOKEN})
    assert result.returncode == 0
    assert [r["authorization"] for r in server.requests] == [f"Bearer {TOKEN}"]


def test_no_token_means_no_authorization_header():
    with StubServer() as server:
        run_hook(fixture_bytes("session_start.json"), url=server.url)
    assert [r["authorization"] for r in server.requests] == [None]


def test_token_never_reaches_stdout_stderr_or_the_body():
    with StubServer() as server:
        result = run_hook(fixture_bytes("post_tool_use_bash.json"), url=server.url,
                          env={"STAR_OFFICE_TOKEN": TOKEN, "STAR_OFFICE_DEBUG": "1"})
    assert result.stdout == b""
    assert TOKEN.encode() not in result.stderr
    assert TOKEN not in str(server.envelopes)


def test_a_token_with_spaces_or_newlines_is_ignored_not_sent():
    for bad in ("two words", "line\nbreak", "short"):
        with StubServer() as server:
            run_hook(fixture_bytes("session_start.json"), url=server.url, env={"STAR_OFFICE_TOKEN": bad})
        assert [r["authorization"] for r in server.requests] == [None], repr(bad)
