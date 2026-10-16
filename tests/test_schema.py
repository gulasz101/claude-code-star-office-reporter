"""AC2 — every emitted envelope validates against ``backend/schemas/event_v1.json``.

The schema is loaded from the repo, not copied, so the test tracks the contract
wherever it moves. Two independent gates run over the captured POSTs:

1. ``jsonschema`` (Draft 2020-12) straight against the schema document;
2. ``backend/event_schema.validate_event`` — the exact code ``POST /events``
   runs, which adds the two rules the schema alone cannot express (the ISO
   instant and the payload size cap). A reporter that passes this one cannot
   be answered a 400 by the dashboard.

A meta-test proves no captured request escaped validation.
"""

from __future__ import annotations

import json

import pytest

Draft202012Validator = pytest.importorskip("jsonschema").Draft202012Validator

from conftest import EMITTING_FIXTURES, SCHEMA  # noqa: E402

event_schema = pytest.importorskip("event_schema")

ALL_FIXTURES = sorted(EMITTING_FIXTURES)

_validator = Draft202012Validator(SCHEMA)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_envelope_validates_against_the_schema_document(all_envelopes, name):
    errors = sorted(_validator.iter_errors(all_envelopes[name]), key=lambda e: e.json_path)
    assert not errors, "\n".join(f"{e.json_path}: {e.message}" for e in errors)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_backend_validator_accepts_the_envelope(all_envelopes, name):
    envelope = all_envelopes[name]
    validated = event_schema.validate_event(envelope)
    assert validated.event_type == envelope["event_type"]
    assert validated.agent == envelope["agent"]
    assert validated.payload == envelope["payload"]


def test_every_captured_request_was_validated(emitted, all_envelopes):
    """No POST may go unexamined: captured count == validated count."""
    captured = sum(len(record["requests"]) for record in emitted.values())
    assert captured == len(all_envelopes) == len(EMITTING_FIXTURES)
    for name in ALL_FIXTURES:
        body = json.dumps(all_envelopes[name])
        assert body, f"{name}: empty envelope body"


def test_schema_version_is_always_one(all_envelopes):
    for name, envelope in all_envelopes.items():
        assert envelope["version"] == 1, name


def test_timestamp_is_utc_z_instant(all_envelopes):
    """The naive-UTC convention ADR-035 fixes; parseable by the backend."""
    for name, envelope in all_envelopes.items():
        parsed = event_schema.parse_timestamp(envelope["timestamp"])
        assert parsed.tzinfo is None, name
        assert envelope["timestamp"].endswith("Z"), name


def test_agent_is_one_token_within_length(all_envelopes):
    for name, envelope in all_envelopes.items():
        agent = envelope["agent"]
        assert isinstance(agent, str) and agent.strip() == agent and " " not in agent, name
        assert 1 <= len(agent) <= 255, name


def test_event_type_is_from_the_documented_registry(all_envelopes):
    """CLAUDE.md's non-normative registry: the reporter must not invent types."""
    allowed = {
        "session_start", "session_end", "tool_call", "file_edit",
        "command_run", "code_completion", "state_change",
    }
    for name, envelope in all_envelopes.items():
        assert envelope["event_type"] in allowed, (name, envelope["event_type"])


def test_payload_stays_under_the_backend_cap(all_envelopes):
    cap = event_schema.PAYLOAD_MAX_BYTES
    for name, envelope in all_envelopes.items():
        size = len(json.dumps(envelope["payload"]).encode("utf-8"))
        assert size <= cap, (name, size)
