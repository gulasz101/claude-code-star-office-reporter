"""Background work: background Bash tasks and subagents become start/end events.

A background task's end is read from the trailing "[exited with code N]" line Claude
Code appends to the task's output file; the output itself must never leave the machine.
"""

from __future__ import annotations

import json
import os
import tempfile

from conftest import StubServer, run_hook

SESSION = "5e55a1b2-0000-4000-8000-00000000cafe"


def hook(server, doc, env):
    result = run_hook(json.dumps(doc).encode(), url=server.url, env=env)
    assert result.returncode == 0 and result.stdout == b"", result.stderr[:300]


def kinds(server):
    return [(e["event_type"], e["payload"].get("task_id")) for e in server.envelopes]


def setup_env():
    tmp = tempfile.mkdtemp(prefix="sou-bg-")
    tasks = os.path.join(tmp, "tasks")
    os.makedirs(os.path.join(tasks, SESSION))
    return tmp, tasks, {
        "STAR_OFFICE_STATE_DIR": os.path.join(tmp, "state"),
        "STAR_OFFICE_TASKS_GLOB": os.path.join(tasks, "{session}", "{task}.output"),
    }


def bash_bg(task_id, command="git push origin CANARY-BRANCH"):
    return {
        "session_id": SESSION, "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": command, "run_in_background": True, "description": "CANARY-DESC"},
        "tool_response": {"stdout": "", "stderr": "", "backgroundTaskId": task_id},
    }


def tool(name="Read"):
    return {"session_id": SESSION, "hook_event_name": "PreToolUse", "tool_name": name,
            "tool_input": {"file_path": "/home/x/CANARY-DIR/a.py"}}


def test_background_bash_starts_then_ends_with_the_exit_code():
    _, tasks, env = setup_env()
    out = os.path.join(tasks, SESSION, "b7abc123.output")
    with StubServer() as server:
        hook(server, bash_bg("b7abc123"), env)
        start = [e for e in server.envelopes if e["event_type"] == "background_start"]
        assert len(start) == 1
        assert start[0]["payload"] == {"task_id": "b7abc123", "kind": "shell", "label": "git push", "session_id": SESSION}

        with open(out, "w") as handle:
            handle.write("CANARY-OUTPUT still working\n")
        hook(server, tool(), env)
        assert ("background_end", "b7abc123") not in kinds(server), "still running: no end yet"

        with open(out, "a") as handle:
            handle.write("CANARY-OUTPUT done\n\n[exited with code 3]\n")
        hook(server, tool(), env)
        end = [e for e in server.envelopes if e["event_type"] == "background_end"]
        assert len(end) == 1
        p = end[0]["payload"]
        assert (p["task_id"], p["kind"], p["label"], p["status"], p["exit_code"]) == ("b7abc123", "shell", "git push", "error", 3)
        assert isinstance(p["duration_ms"], int)

        hook(server, tool(), env)
        assert len([e for e in server.envelopes if e["event_type"] == "background_end"]) == 1, "reported once"
        assert "CANARY" not in json.dumps(server.envelopes)


def test_subagents_start_and_stop_by_agent_id():
    _, _, env = setup_env()
    with StubServer() as server:
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "Explore"}, env)
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStart", "agent_id": "agent-2", "agent_type": "general-purpose"}, env)
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStop", "agent_id": "agent-2"}, env)
        assert kinds(server) == [("background_start", "agent-1"), ("background_start", "agent-2"), ("background_end", "agent-2")]
        assert server.envelopes[0]["payload"]["label"] == "Explore"
        assert server.envelopes[2]["payload"]["status"] == "ok"


def test_subagent_stop_without_an_id_ends_the_oldest_one():
    _, _, env = setup_env()
    with StubServer() as server:
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStart", "agent_id": "a-old", "agent_type": "Plan"}, env)
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStart", "agent_id": "a-new", "agent_type": "Plan"}, env)
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStop"}, env)
        assert kinds(server)[-1] == ("background_end", "a-old")


def test_session_end_closes_everything_still_open():
    _, _, env = setup_env()
    with StubServer() as server:
        hook(server, bash_bg("bAAA"), env)
        hook(server, {"session_id": SESSION, "hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "Explore"}, env)
        hook(server, {"session_id": SESSION, "hook_event_name": "SessionEnd", "reason": "other"}, env)
        ends = {e["payload"]["task_id"]: e["payload"]["status"] for e in server.envelopes if e["event_type"] == "background_end"}
        assert ends == {"bAAA": "session_end", "a1": "session_end"}
        assert server.envelopes[-3]["event_type"] == "session_end", "the main event goes first"


def test_foreground_bash_and_unsafe_task_ids_start_nothing():
    _, _, env = setup_env()
    with StubServer() as server:
        fg = bash_bg("bX")
        fg["tool_input"]["run_in_background"] = False
        hook(server, fg, env)
        hook(server, bash_bg("../../etc/passwd"), env)
        hook(server, bash_bg("has space"), env)
        assert not [e for e in server.envelopes if e["event_type"].startswith("background")]


def test_a_broken_state_file_never_costs_the_main_event():
    tmp, _, env = setup_env()
    os.makedirs(env["STAR_OFFICE_STATE_DIR"])
    for name in os.listdir(env["STAR_OFFICE_STATE_DIR"]):
        os.remove(os.path.join(env["STAR_OFFICE_STATE_DIR"], name))
    with StubServer() as server:
        hook(server, bash_bg("bOK"), env)
        state = [os.path.join(env["STAR_OFFICE_STATE_DIR"], f) for f in os.listdir(env["STAR_OFFICE_STATE_DIR"])]
        with open(state[0], "w") as handle:
            handle.write("{not json")
        hook(server, tool("Edit"), env)
        assert server.envelopes[-1]["event_type"] == "tool_call"
