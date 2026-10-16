"""SOU-173 — the command label: program plus ONE allow-listed subcommand.

``program()`` used to send the bare program name, so ``git push``,
``docker build`` and ``npm run build`` all arrived as ``git`` / ``docker`` /
``npm`` and the v3 activity map (``frontend/v3/activity.js``, SOU-156) sent
every build to the rack instead of the garage. These tests pin the fix:

* allow-list of programs that may carry a subcommand, mirroring activity.js;
* subcommand = the first argument that is a bare lowercase identifier
  (``^[a-z][a-z0-9-]{0,20}$``) — flags and value-ish tokens (``key=value``,
  paths, URLs, uppercase) are skipped and NEVER echoed;
* npm/pnpm/yarn send ``run <script>`` as two tokens, because activity.js
  computes the runner verb from the token AFTER ``run`` — a lone ``npm run``
  would classify ``npm run build`` as ops, not build;
* everything outside the allow-list keeps the old program-name-only behaviour.

The zone side is proven separately by feeding these exact labels through
``mapEvent`` (see the evidence on SOU-173/SOU-180).
"""

from __future__ import annotations

import json

import pytest

SID = "0f0f9c3a-7d21-4b0e-9b3a-5d1c2a7e8811"


def bash_event(command: str) -> bytes:
    return json.dumps({
        "session_id": SID,
        "hook_event_name": "PostToolUse",
        "cwd": "/home/owner/CANARY-DIR/proj",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }).encode()


def label(harness, command: str) -> str:
    harness.server.requests.clear()
    result = harness.send_raw(bash_event(command))
    assert result.returncode == 0, command
    body = harness.envelopes[0]
    assert body["event_type"] == "command_run", command
    return body["payload"]["command"]


# Issue's own cases + the allow-list mirroring activity.js.
ALLOWED = [
    ("git push origin main", "git push"),
    ("git status", "git status"),
    ("git commit -m 'note to self'", "git commit"),
    ("git switch --detach", "git switch"),
    ("docker build -t x .", "docker build"),
    ("docker compose up -d", "docker compose"),
    ("kubectl apply -f manifest.yaml", "kubectl apply"),
    ("cargo build --release", "cargo build"),
    ("go test ./...", "go test"),
    ("helm upgrade my-release ./chart", "helm upgrade"),
    ("flux reconcile kustomization web", "flux reconcile"),
    ("make clean", "make clean"),
    ("uv run pytest", "uv run"),
    ("pip install requests", "pip install"),
    ("poetry add rich", "poetry add"),
    ("npm install lodash", "npm install"),
    ("npm i -g pnpm", "npm i"),
    ("yarn add left-pad", "yarn add"),
    ("git rev-parse HEAD", "git rev-parse"),
]

# Runner programs keep the script verb as a second token (activity.js needs it).
RUNNERS = [
    ("npm run build", "npm run build"),
    ("npm run test --watch", "npm run test"),
    ("npm run", "npm run"),
    ("pnpm run lint", "pnpm run lint"),
    ("yarn run build", "yarn run build"),
    ("npm --silent run build", "npm run build"),
]

# Flags, key=value pairs, paths, URLs and uppercase words are skipped and
# never echoed — the privacy case from the issue.
PRIVACY = [
    ("git -c user.password=SECRET push", "git push"),
    ("git -c credential.helper=store push", "git push"),
    ("git push https://user:TOKEN@host/repo.git", "git push"),
    ("docker build --build-arg TOKEN=abc -t x .", "docker build"),
    ("git checkout feature/login", "git checkout"),
    ("git -c color.ui=never log", "git log"),
    ("git -c a=b -c c=d", "git"),  # no bare-identifier token at all -> program only
]

# Env-prefix and wrapper stripping as today, now with the subcommand attached.
# ("timeout" is NOT stripped today and activity.js does not strip it either;
# that parity is pinned in NOT_ALLOWED below.)
WRAPPERS = [
    ("sudo git push", "git push"),
    ("sudo -n git push", "git push"),
    ("FOO=bar git push", "git push"),
    ("  /usr/bin/env FOO=bar npm run build ", "npm run build"),
]

# Outside the allow-list: program name only, exactly as before.
NOT_ALLOWED = [
    ("rm -rf /tmp/x", "rm"),
    ("pytest tests -q", "pytest"),
    ("ls -la", "ls"),
    ("cat notes.txt | grep CANARY", "cat"),
    ("ssh deploy@host", "ssh"),
    ("bash deploy.sh", "bash"),
    ("./deploy.sh", "deploy.sh"),
    ("timeout 30 git status", "git status"),  # timeout + its duration are a wrapper (SOU-173 addendum)
    ("git", "git"),
]

# Only the first real segment gets the label, so the scan never wanders into later
# segments. The one exception (SOU-173 addendum): leading `cd`/`pushd` segments are
# skipped, because Claude Code prefixes most commands with `cd <dir> &&`.
SEGMENTS = [
    ("cd build && make test", "make test"),
    ("echo hi && git push", "echo"),
]

ALL_CASES = ALLOWED + RUNNERS + PRIVACY + WRAPPERS + NOT_ALLOWED + SEGMENTS


@pytest.mark.parametrize("command, expected", ALL_CASES)
def test_command_label(harness, command, expected):
    assert label(harness, command) == expected, command


def test_secret_value_never_echoed(harness):
    """The -c value from the privacy case must not be anywhere in the envelope."""
    harness.server.requests.clear()
    harness.send_raw(bash_event("git -c user.password=SUPERSECRET push"))
    body = json.dumps(harness.envelopes[0])
    assert "SUPERSECRET" not in body, body


def test_label_stays_single_line_and_small(harness):
    for command, _expected in ALL_CASES:
        value = label(harness, command)
        assert "\n" not in value
        assert len(value) <= 120, (command, value)



# Claude Code commands usually start with `cd <dir> &&` (SOU-173 addendum, Claude Code).
CD_AND_WRAPPERS = [
    ("cd /home/me/repo && git push origin main", "git push"),
    ("cd /tmp; docker build -t x .", "docker build"),
    ("cd a && cd b && npm run build", "npm run build"),
    ("pushd /x && make test", "make test"),
    ("cd /only/here", "cd"),
    ("timeout 300 .venv/bin/python -m pytest -q", "pytest"),
    ("timeout 5m git pull", "git pull"),
    ("python3 -m pip install x", "pip install"),
    ("uv run pytest backend/tests", "uv run"),
    ("cd /x && PGPASSWORD=s3cr3t psql -h db", "psql"),
]


@pytest.mark.parametrize("command,expected", CD_AND_WRAPPERS)
def test_cd_prefix_and_wrappers(harness, command, expected):
    got = label(harness, command)
    assert got == expected
    for secret in ("/home/me/repo", "/tmp", "s3cr3t", "/only/here", ".venv"):
        assert secret not in got
