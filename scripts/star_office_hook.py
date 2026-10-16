#!/usr/bin/env python3
"""Claude Code hook -> Star Office UI events.

Claude Code runs this as a *command hook* (wired by the plugin's
``hooks/hooks.json``): the event's JSON arrives on stdin and the script turns it
into ADR-035 schema-v1 envelopes (``tests/schema/event_v1.json``) POSTed to
``$STAR_OFFICE_URL/events``.

Two promises, both load-bearing:

* **Never block or break Claude Code.** Exit code 2 blocks the tool call or the
  prompt, and plain-text stdout is added to Claude's context on
  ``UserPromptSubmit``/``SessionStart``. So this script always exits 0, always
  prints nothing on stdout, swallows every error, and caps the POST at 2 s.
* **Never leak the session.** Prompt text, file contents, tool output, command
  arguments, error text and absolute paths are all dropped: what leaves the
  machine is a tool name, a status, a file *basename*, a program name plus —
  for the allow-listed CLI families (SOU-173) — at most one or two bare
  subcommand tokens, and a couple of documented enum values. Flags, values,
  paths and URLs in a command are never echoed.

The mapping (hook event -> ``event_type``) is the ticket's table, and the
payload keys are the ones the v3 activity map already reads:

    SessionStart      -> session_start    {session_id, cwd, source}
    UserPromptSubmit  -> state_change     {state: "thinking", session_id}
    PreToolUse        -> tool_call        {tool, status: "started", session_id}
    PostToolUse       -> file_edit        Edit/Write/MultiEdit/NotebookEdit
                                          {tool, file, status, session_id, duration_ms}
                      -> command_run      Bash/PowerShell
                                          {tool, command, status, session_id, duration_ms}
                      -> tool_call        anything else {tool, status, ...}
    Stop              -> code_completion  {session_id}
    Notification      -> state_change     {state: "waiting_for_user", notification_type}
    SessionEnd        -> session_end      {session_id, reason}

Background work (a ``Bash`` call with ``run_in_background``, and subagents) is
tracked across hook invocations in a small per-session state file:

    PostToolUse Bash + run_in_background -> background_start {task_id, kind: "shell", label}
    SubagentStart                        -> background_start {task_id, kind: "agent", label}
    task output gains "[exited with code N]",
    SubagentStop, SessionEnd or a 6 h TTL -> background_end   {task_id, kind, label,
                                                               status, exit_code, duration_ms}

The finished check runs on every hook call of the session, so an end is
reported with the next thing Claude does. Only the exit-code line at the very
end of the task's output file is read; the output itself never is.

Anything else (Claude Code documents 30+ events) sends nothing.

Stdlib only: a hook must not need an install step.

Configuration (all optional):

    STAR_OFFICE_URL      dashboard base URL   (default http://127.0.0.1:19000;
                                             set it to an empty value to switch
                                             reporting off without uninstalling)
    STAR_OFFICE_AGENT    feed identity        (default claude-code)
    STAR_OFFICE_TIMEOUT  network cap, seconds (default 2.0, clamped 0.25-10)
    STAR_OFFICE_DEBUG    1 = echo the envelope on stderr, never on stdout
    STAR_OFFICE_STATE_DIR  where background-task state lives
                           (default $XDG_STATE_HOME/star-office-reporter)
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_URL = "http://127.0.0.1:19000"
DEFAULT_AGENT = "claude-code"
DEFAULT_TIMEOUT = 2.0
TIMEOUT_FLOOR = 0.25
TIMEOUT_CEIL = 10.0

# Mirrors backend/event_schema.py PAYLOAD_MAX_BYTES: the ingest guard, so an
# envelope is never rejected for size. Our payloads are ~120 bytes by
# construction; the cap is a belt, not the suspender.
PAYLOAD_MAX_BYTES = 65536

# A single value in a payload is a label for a pixel character, not a document.
MAX_VALUE_CHARS = 120
MAX_STDIN_BYTES = 2_000_000

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
SHELL_TOOLS = {"bash", "powershell"}

# Keys a file-editing tool may put the path under. The hooks reference documents
# `file_path` for Write/Edit/Read; MultiEdit and NotebookEdit have no documented
# table, so `notebook_path` and the generic `path` are read defensively and an
# unusable value falls back to a plain tool_call rather than a guessed one.
PATH_KEYS = ("file_path", "notebook_path", "path", "target_file", "absolute_path")

# Wrappers that are not the program the agent actually ran.
COMMAND_WRAPPERS = {"sudo", "doas", "env", "time", "nohup", "command", "nice", "timeout"}
# `timeout 300 cmd` / `timeout 5m cmd`: the duration is the wrapper's argument, not the program.
DURATION = re.compile(r"^[0-9]+(\.[0-9]+)?[smhd]?$")
# Directory changes chained in front of the real work (`cd /repo && git push`) are skipped;
# a command that is only a `cd` still reports as `cd`. The directory is never sent.
DIR_CHANGERS = {"cd", "pushd"}
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|]")
PYTHONS = re.compile(r"^python[0-9.]*$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PROGRAM = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")
TOKEN_SPLIT = re.compile(r"\s+")

# Programs allowed to carry one subcommand token in the label (SOU-173). The
# list mirrors frontend/v3/activity.js: these are the CLI families whose verb
# decides the office zone (garage vs rack), and the renderer re-parses the
# same label with the same bare-identifier discipline.
SUBCOMMAND_PROGRAMS = {
    "git", "npm", "pnpm", "yarn", "docker", "kubectl", "cargo", "go",
    "helm", "flux", "make", "uv", "pip", "poetry",
}
# npm/pnpm/yarn need `run <script>` as TWO tokens: activity.js computes the
# runner verb from the token after "run", so a lone "npm run" would classify
# `npm run build` as ops instead of the garage.
RUNNER_PROGRAMS = {"npm", "pnpm", "yarn"}
# A subcommand is a bare lowercase identifier, never a flag, path, URL,
# key=value pair or value. Anything that does not match is skipped and not
# echoed — this regex is the whole privacy boundary of the feature.
SUBCOMMAND = re.compile(r"^[a-z][a-z0-9-]{0,20}$")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------- sanitising --
def text_of(value, limit: int = MAX_VALUE_CHARS) -> str:
    """A trimmed, control-character-free string, or "" for anything else."""
    if isinstance(value, str):
        out = _CONTROL_CHARS.sub(" ", value).strip()
        return out[:limit]
    return ""


def basename(value) -> str:
    """Last path segment only — the whole path is the thing we must not send."""
    raw = text_of(value, 1024)
    if not raw:
        return ""
    normalised = raw.replace("\\", "/")
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    last = parts[-1]
    if last in {"..", "/"} or "\x00" in last:
        return ""
    # A drive letter or a leftover separator means we did not get a basename.
    if ":" in last or "/" in last or "\\" in last:
        return ""
    return last[:MAX_VALUE_CHARS]


def enum(value) -> str:
    """A documented enum value kept as-is when it is only letters/_/-."""
    raw = text_of(value, 64)
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw or ""):
        return raw
    return ""


def command_parts(command):
    """First real command -> ``(program, [arguments after it])``.

    Leading ``cd``/``pushd`` segments are skipped when something follows them. Env
    prefixes (``FOO=bar``), wrapper tokens (``sudo``, ``env``, ``timeout 300`` …) and
    flags are skipped while finding the program; ``python -m <module>`` reports the
    module. The arguments keep their original tokens so the caller can pick a
    subcommand out of them. Nothing usable -> ``("shell", [])``.
    """
    raw = text_of(command, 4096)
    if not raw:
        return "", []
    segments = [seg for seg in SEGMENT_SPLIT.split(raw) if seg.strip()]
    parsed = None
    for seg in segments:
        parsed = _segment_parts(seg)
        if parsed[0] not in DIR_CHANGERS:
            return parsed
    return parsed or ("shell", [])


def _segment_parts(segment):
    tokens = [token for token in TOKEN_SPLIT.split(segment.strip()) if token]
    skip_duration = False
    for index, token in enumerate(tokens):
        if skip_duration and DURATION.match(token):
            skip_duration = False
            continue
        if ASSIGNMENT.match(token) or token.startswith("-"):
            continue  # env assignment or a flag of the wrapper
        name = basename(token.replace("\\", "/")) or token
        name = name.split("/")[-1]
        if name in COMMAND_WRAPPERS:
            skip_duration = name == "timeout"
            continue
        if not PROGRAM.match(name) or ":" in name:
            return "shell", []
        rest = tokens[index + 1:]
        if PYTHONS.match(name) and len(rest) >= 2 and rest[0] == "-m" and PROGRAM.match(rest[1]):
            return rest[1], rest[2:]
        return name, rest
    return "shell", []


def program(command) -> str:
    """Program name of a shell command: first segment, first real token.

    Never arguments, never a path. `sudo -u x npm i` -> `npm`;
    `FOO=bar ./bin/run.sh -v` -> `run.sh`; nothing usable -> `shell`.
    """
    return command_parts(command)[0]


def command_label(command) -> str:
    """Privacy-safe label: the program plus ONE allow-listed subcommand (SOU-173).

    ``git push origin main`` -> ``git push``, ``docker build -t x .`` ->
    ``docker build``, ``npm run build`` -> ``npm run build`` (runner programs
    keep the script verb, two tokens). The subcommand is the first argument
    that is a bare lowercase identifier; flags and value-ish tokens
    (``key=value``, paths, URLs, uppercase) are skipped and never echoed.
    Programs outside ``SUBCOMMAND_PROGRAMS`` keep the old program-name-only
    behaviour, so ``git -c user.password=SECRET push`` -> ``git push`` and
    nothing else about that command leaves the machine.
    """
    prog, args = command_parts(command)
    if not prog or prog.lower() not in SUBCOMMAND_PROGRAMS:
        return prog
    picked = []
    for token in args:
        if token.startswith("-") or not SUBCOMMAND.match(token):
            continue  # a flag or a value: never a subcommand, never echoed
        picked.append(token)
        want = 2 if prog.lower() in RUNNER_PROGRAMS and picked[0] == "run" else 1
        if len(picked) >= want:
            break
    return " ".join([prog] + picked)


def tool_name(data) -> str:
    """The tool identifier, only when it is a usable string (MCP names included)."""
    return text_of(data.get("tool_name"), 100)


def duration_ms(data) -> int | None:
    value = data.get("duration_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > 86_400_000:
        return None
    return value


# ------------------------------------------------------------------ mapping --
def build_event(data: dict):
    """One hook input -> ``(event_type, payload)`` or ``None`` to stay silent."""
    name = text_of(data.get("hook_event_name"), 60)
    if not name:
        return None

    session_id = enumish_id(data.get("session_id"))
    tool = tool_name(data)
    timing = duration_ms(data)

    if name == "SessionStart":
        payload = {}
        put(payload, "session_id", session_id)
        put(payload, "cwd", basename(data.get("cwd")))
        put(payload, "source", enum(data.get("source")))
        return "session_start", payload

    if name == "UserPromptSubmit":
        # The prompt itself is never read, let alone sent.
        payload = {"state": "thinking"}
        put(payload, "session_id", session_id)
        return "state_change", payload

    if name == "PreToolUse":
        if not tool:
            return None
        payload = {"tool": tool, "status": "started"}
        put(payload, "session_id", session_id)
        return "tool_call", payload

    if name == "PostToolUse":
        if not tool:
            return None
        low = tool.lower()
        tool_input = data.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if low in EDIT_TOOLS:
            file_name = first_path(tool_input)
            if file_name:
                payload = {"tool": tool, "file": file_name, "status": "ok"}
                put(payload, "session_id", session_id)
                put_number(payload, "duration_ms", timing)
                return "file_edit", payload
        elif low in SHELL_TOOLS:
            command = command_label(tool_input.get("command"))
            payload = {"tool": tool, "command": command or "shell", "status": "ok"}
            put(payload, "session_id", session_id)
            put_number(payload, "duration_ms", timing)
            return "command_run", payload
        payload = {"tool": tool, "status": "ok"}
        put(payload, "session_id", session_id)
        put_number(payload, "duration_ms", timing)
        return "tool_call", payload

    if name == "Stop":
        payload = {}
        put(payload, "session_id", session_id)
        return "code_completion", payload

    if name == "Notification":
        # message/title are notification text: dropped. The type is one of a
        # dozen documented enum values and carries no user content.
        payload = {"state": "waiting_for_user"}
        put(payload, "session_id", session_id)
        put(payload, "notification_type", enum(data.get("notification_type")))
        return "state_change", payload

    if name == "SessionEnd":
        payload = {}
        put(payload, "session_id", session_id)
        put(payload, "reason", enum(data.get("reason")))
        return "session_end", payload

    return None


def put(payload: dict, key: str, value) -> None:
    if isinstance(value, str) and value:
        payload[key] = value


def put_number(payload: dict, key: str, value) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        payload[key] = value


def enumish_id(value) -> str:
    """A session id is an opaque token: keep it, but keep it single-token."""
    raw = text_of(value, 128)
    if not raw or re.search(r"\s", raw):
        return ""
    return raw


def first_path(tool_input: dict) -> str:
    for key in PATH_KEYS:
        name = basename(tool_input.get(key))
        if name:
            return name
    return ""


# ---------------------------------------------------------------- transport --
def resolve_config(env) -> tuple[str | None, str, float]:
    """URL, agent and network cap.

    An *unset* ``STAR_OFFICE_URL`` means the local dashboard. An explicitly
    empty one means "do not report" — the off switch that does not need the
    hook uninstalled, and the reason a blank value must not fall back to the
    default (SOU-157: it used to, and the suite then wrote into any live
    office on 19000).
    """
    raw_url = env.get("STAR_OFFICE_URL")
    url = DEFAULT_URL if raw_url is None else raw_url.strip()
    agent = (env.get("STAR_OFFICE_AGENT") or "").strip()
    if not agent or re.search(r"\s", agent):
        agent = DEFAULT_AGENT
    try:
        timeout = float(env.get("STAR_OFFICE_TIMEOUT") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = min(max(timeout, TIMEOUT_FLOOR), TIMEOUT_CEIL)
    return url or None, agent, timeout


def make_envelope(agent: str, event_type: str, payload: dict) -> dict:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if len(json.dumps(payload).encode("utf-8")) > PAYLOAD_MAX_BYTES:
        trimmed = {}
        put(trimmed, "session_id", payload.get("session_id", ""))
        payload = trimmed
    return {
        "version": 1,
        "agent": agent,
        "event_type": event_type,
        "timestamp": stamp,
        "payload": payload,
    }


def post_event(url: str, envelope: dict, timeout: float) -> None:
    """Best-effort POST. Every failure is a no-op; the office is not the job."""
    target = url.rstrip("/") + "/events"
    body = json.dumps(envelope).encode("utf-8")
    request = urllib.request.Request(
        target,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "claude-code-star-office-reporter/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read(2048)


# ---------------------------------------------------------- background work --
BACKGROUND_TTL_S = 6 * 3600
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXIT_LINE = re.compile(rb"\[exited with code (-?[0-9]{1,3})\]\s*$")
TAIL_BYTES = 256


def state_file(env, session_id: str):
    base = (env.get("STAR_OFFICE_STATE_DIR") or "").strip()
    if not base:
        xdg = (env.get("XDG_STATE_HOME") or "").strip() or os.path.join(os.path.expanduser("~"), ".local", "state")
        base = os.path.join(xdg, "star-office-reporter")
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
    return os.path.join(base, f"bg-{digest}.json")


def task_output_path(env, session_id: str, task_id: str) -> str:
    """Where Claude Code writes a background task's output: <tmp>/claude-<uid>/<project>/<session>/tasks/<id>.output."""
    pattern = (env.get("STAR_OFFICE_TASKS_GLOB") or "").strip() or os.path.join(
        tempfile.gettempdir(), "claude-*", "*", "{session}", "tasks", "{task}.output")
    hits = glob.glob(pattern.replace("{session}", glob.escape(session_id)).replace("{task}", glob.escape(task_id)))
    return hits[0] if hits else ""


def exit_code_of(path: str):
    """The N of a trailing "[exited with code N]" line, or None while the task runs."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    match = EXIT_LINE.search(tail)
    return int(match.group(1)) if match else None


def _task_id(value) -> str:
    raw = text_of(value, 64)
    return raw if TASK_ID.match(raw or "") else ""


def _end(task_id: str, task: dict, status: str, now: float, session_id: str, exit_code=None):
    payload = {"task_id": task_id, "kind": task.get("kind", "shell"), "label": task.get("label", ""), "status": status}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    started = task.get("started")
    if isinstance(started, (int, float)):
        payload["duration_ms"] = max(0, int((now - started) * 1000))
    put(payload, "session_id", session_id)
    return "background_end", payload


def background_events(data: dict, env, now: float | None = None) -> list:
    """Starts and ends of background work for this hook call (see the module docstring)."""
    session_id = enumish_id(data.get("session_id"))
    if not session_id:
        return []
    now = time.time() if now is None else now
    name = text_of(data.get("hook_event_name"), 60)
    tool = tool_name(data).lower()
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    response = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}

    path = state_file(env, session_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tasks = json.load(handle).get("tasks", {})
        if not isinstance(tasks, dict):
            tasks = {}
    except (OSError, ValueError, AttributeError):
        tasks = {}
    before = json.dumps(tasks, sort_keys=True)
    out = []

    def start(task_id, kind, label):
        if task_id and task_id not in tasks:
            tasks[task_id] = {"kind": kind, "label": label, "started": now}
            payload = {"task_id": task_id, "kind": kind, "label": label}
            put(payload, "session_id", session_id)
            out.append(("background_start", payload))

    if name == "PostToolUse" and tool in SHELL_TOOLS and tool_input.get("run_in_background") is True:
        start(_task_id(response.get("backgroundTaskId")), "shell", command_label(tool_input.get("command")) or "shell")
    elif name == "SubagentStart":
        start(_task_id(data.get("agent_id")), "agent", enum(data.get("agent_type")) or "agent")
    elif name == "SubagentStop":
        agent_id = _task_id(data.get("agent_id"))
        agents = [tid for tid, t in tasks.items() if t.get("kind") == "agent"]
        if agent_id not in tasks:
            # no id to match on: the oldest running subagent is the best guess
            agent_id = min(agents, key=lambda tid: tasks[tid].get("started", 0), default="")
        if agent_id:
            out.append(_end(agent_id, tasks.pop(agent_id), "ok", now, session_id))

    for task_id in list(tasks):
        task = tasks[task_id]
        if name == "SessionEnd":
            out.append(_end(task_id, tasks.pop(task_id), "session_end", now, session_id))
            continue
        if task.get("kind") == "shell":
            output = task.get("output") or task_output_path(env, session_id, task_id)
            if output:
                task["output"] = output
                code = exit_code_of(output)
                if code is not None:
                    out.append(_end(task_id, tasks.pop(task_id), "ok" if code == 0 else "error", now, session_id, code))
                    continue
        if now - task.get("started", now) > BACKGROUND_TTL_S:
            out.append(_end(task_id, tasks.pop(task_id), "stale", now, session_id))

    if json.dumps(tasks, sort_keys=True) != before:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"tasks": tasks}, handle)
            os.replace(tmp, path)  # atomic: a concurrent hook sees the old or the new file
            if not tasks and name == "SessionEnd":
                os.remove(path)
        except OSError:
            pass
    return out


# --------------------------------------------------------------------- main --
def main(argv=None) -> int:
    env = os.environ
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES)
    if not raw:
        return 0
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    url, agent, timeout = resolve_config(env)
    if url is None:
        return 0  # STAR_OFFICE_URL="" -> reporting off, silently
    events = []
    built = build_event(data)
    if built is not None:
        events.append(built)
    try:
        events += background_events(data, env)
    except Exception:
        pass  # background bookkeeping is a bonus, never a reason to drop the main event

    debug = (env.get("STAR_OFFICE_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}
    for event_type, payload in events:
        envelope = make_envelope(agent, event_type, payload)
        if debug:
            # stderr only: stdout is Claude Code's decision channel and must stay empty.
            sys.stderr.write(json.dumps(envelope) + "\n")
        try:
            post_event(url, envelope, timeout)
        except BaseException:
            pass  # unreachable dashboard, refused event, TLS trouble: none of them ours
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BaseException:
        # A hook that fails open is a hook nobody notices. Never exit non-zero,
        # never write to stdout, whatever just happened.
        sys.exit(0)
