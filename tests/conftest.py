"""Test harness for the Claude Code Star Office reporter plugin.

Every test drives the real thing: ``scripts/star_office_hook.py``
is launched as a subprocess with a recorded Claude Code hook JSON document on
stdin (``fixtures/``, field names taken from
https://docs.claude.com/en/docs/claude-code/hooks) and a local stub HTTP
server in place of the dashboard. The assertions therefore run against the
bytes that actually left the script, not against an import of its internals —
which is the only way to prove both the mapping and the fail-open rule.

Canary policy: every value the reporter must never transmit carries a
``CANARY-`` token in the fixture (prompt text, file content, tool output,
command arguments, absolute directory prefixes). The privacy sweep asserts no
``CANARY`` substring survives in any envelope. Filenames that ARE allowed to
travel (``app.py``, ``activity.js``) stay realistic and canary-free.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
HOOK_SCRIPT = ROOT / "scripts" / "star_office_hook.py"
FIXTURE_DIR = TESTS_DIR / "fixtures"
# A copy of the dashboard's backend/schemas/event_v1.json (gulasz101/star-office-ui-fork).
SCHEMA_PATH = TESTS_DIR / "schema" / "event_v1.json"
SETTINGS_PATH = ROOT / "hooks" / "hooks.json"
README_PATH = ROOT / "README.md"

SCHEMA = json.loads(SCHEMA_PATH.read_text())

# The dashboard's backend/event_schema.py is the authority on what it accepts; point
# SOU_BACKEND_DIR at a star-office-ui-fork checkout's backend/ to run those checks too.
if os.environ.get("SOU_BACKEND_DIR"):
    sys.path.insert(0, os.environ["SOU_BACKEND_DIR"])

# Every fixture that the mapping table says must produce an envelope.
EMITTING_FIXTURES = [
    "session_start.json",
    "session_resume.json",
    "user_prompt_submit.json",
    "pre_tool_use_bash.json",
    "pre_tool_use_edit.json",
    "post_tool_use_edit.json",
    "post_tool_use_write.json",
    "post_tool_use_multi_edit.json",
    "post_tool_use_notebook_edit.json",
    "post_tool_use_bash.json",
    "post_tool_use_powershell.json",
    "post_tool_use_read.json",
    "post_tool_use_mcp.json",
    "stop.json",
    "notification.json",
    "notification_idle.json",
    "session_end.json",
    "no_session_id.json",
    "relative_cwd.json",
    "tool_no_path.json",
    "windows_paths.json",
    "huge.json",
]

# Fixtures that must produce nothing at all (unknown event, undecodable input).
SILENT_FIXTURES = ["unknown_event.json", "malformed.txt", "array.json", "empty.json"]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def free_port() -> int:
    """A port nothing is listening on: the dashboard-down case."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class StubServer:
    """Minimal stand-in for ``POST /events`` that records what it received.

    ``delay`` reproduces a dashboard that accepts the connection and then
    hangs — the case the 2 s network cap has to survive. ``status`` reproduces
    an error answer (a schema rejection, a 500), which must still leave the
    hook silent and exiting 0.
    """

    def __init__(self, *, delay: float = 0.0, status: int = 201, port: int = 0):
        self.delay = delay
        self.status = status
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _collect(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "content_type": self.headers.get("Content-Type"),
                        "raw": raw,
                    }
                )
                if outer.delay:
                    time.sleep(outer.delay)
                body = json.dumps({"ok": outer.status < 400}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = _collect

            def log_message(self, *args):  # keep pytest output clean
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "StubServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    @property
    def envelopes(self) -> list[dict]:
        """Parsed JSON bodies, failing the test if anything was not JSON."""
        out = []
        for req in self.requests:
            out.append(json.loads(req["raw"].decode("utf-8")))
        return out


import tempfile as _tempfile

_STATE_DIR = _tempfile.mkdtemp(prefix="sou-reporter-test-")


def hook_env(url: str, overrides: dict | None = None) -> dict:
    """A clean environment: ambient SOU/STAR variables must not leak in."""
    env = copy.deepcopy(dict(os.environ))
    for key in list(env):
        if key.startswith("STAR_OFFICE") or key.startswith("SOU_"):
            env.pop(key, None)
    env["STAR_OFFICE_URL"] = url
    # background-task state goes to a throwaway dir, never the real ~/.local/state
    env["STAR_OFFICE_STATE_DIR"] = _STATE_DIR
    env["STAR_OFFICE_TASKS_GLOB"] = os.path.join(_STATE_DIR, "no-such-tasks", "{session}", "{task}.output")
    if overrides:
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return env


def run_hook(stdin_bytes: bytes, *, url: str, env: dict | None = None,
             timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Launch the hook for real and time it (the fail-open bound is a timing bound)."""
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=hook_env(url, env),
        cwd=str(ROOT),
        timeout=timeout,
    )
    result.elapsed = time.monotonic() - started
    return result


class Harness:
    """One stub dashboard, many hook invocations."""

    def __init__(self, server: StubServer):
        self.server = server
        self.results: list[subprocess.CompletedProcess] = []

    def send(self, fixture_name: str, **env) -> subprocess.CompletedProcess:
        return self.send_raw(fixture_bytes(fixture_name), **env)

    def send_raw(self, payload: bytes, **env) -> subprocess.CompletedProcess:
        result = run_hook(payload, url=self.server.url, env=env or None)
        self.results.append(result)
        return result

    @property
    def envelopes(self) -> list[dict]:
        return self.server.envelopes

    @property
    def paths(self) -> list[str]:
        return self.server.paths


@pytest.fixture
def stub() -> StubServer:
    with StubServer() as server:
        yield server


@pytest.fixture
def harness(stub: StubServer) -> Harness:
    return Harness(stub)


@pytest.fixture(scope="session")
def emitted() -> dict[str, dict]:
    """Run every emitting fixture exactly once, for the sweep tests.

    Returns ``{fixture_name: {"envelope": <first POST>, "result": ...}}``. A
    fixture that sends nothing (or more than one event) fails here, so the
    sweeps below can assume one envelope each.
    """
    out: dict[str, dict] = {}
    with StubServer() as server:
        hub = Harness(server)
        for name in EMITTING_FIXTURES:
            before = len(server.requests)
            result = hub.send(name)
            captured = server.requests[before:]
            out[name] = {"result": result, "requests": captured}
    for name, record in out.items():
        assert record["result"].returncode == 0, f"{name}: hook exited non-zero"
        assert len(record["requests"]) == 1, (
            f"{name}: expected exactly one POST, got {len(record['requests'])}"
        )
        record["envelope"] = json.loads(record["requests"][0]["raw"].decode("utf-8"))
    return out


@pytest.fixture(scope="session")
def all_envelopes(emitted: dict[str, dict]) -> dict[str, dict]:
    return {name: rec["envelope"] for name, rec in emitted.items()}
