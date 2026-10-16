# claude-code-star-office-reporter

A [Claude Code](https://claude.com/claude-code) **plugin** that reports your coding agent's
activity to a [Star Office UI](https://github.com/gulasz101/star-office-ui-fork) dashboard
as it happens: tool calls, file edits, commands, sessions, **background tasks and
subagents**. One stdlib-only Python script, zero dependencies, wired through Claude Code
hooks. Your agent keeps working; the office feed lights up and a little character in an
orange hoodie walks between the desk, the rack and the garage.

Sibling of [`hermes-star-office-reporter`](https://github.com/gulasz101/hermes-star-office-reporter)
and [`openchamber-star-office-reporter`](https://github.com/gulasz101/openchamber-star-office-reporter):
same event envelope, same dashboard.

## What it reports

Every event is one
[ADR-035 schema-v1 envelope](https://github.com/gulasz101/star-office-ui-fork/blob/master/backend/schemas/event_v1.json)
(`{version: 1, agent, event_type, timestamp, payload}`, schema copy in
`tests/schema/event_v1.json`) `POST`ed to the dashboard's `/events` endpoint:

| Claude Code hook   | `event_type`       | `payload` fields                                                    |
|--------------------|--------------------|---------------------------------------------------------------------|
| `SessionStart`     | `session_start`    | `session_id`, `cwd` (basename only), `source`                       |
| `UserPromptSubmit` | `state_change`     | `state: "thinking"`, `session_id` (the prompt is never read)        |
| `PreToolUse`       | `tool_call`        | `tool`, `status: "started"`, `session_id`                           |
| `PostToolUse`      | `file_edit`        | Edit/Write/MultiEdit/NotebookEdit: `tool`, `file` (basename), `status`, `duration_ms` |
|                    | `command_run`      | Bash/PowerShell: `tool`, `command` (program + one safe subcommand), `status`, `duration_ms` |
|                    | `tool_call`        | anything else: `tool`, `status`, `duration_ms`                      |
| `Stop`             | `code_completion`  | `session_id`                                                        |
| `Notification`     | `state_change`     | `state: "waiting_for_user"`, `notification_type`                    |
| `SessionEnd`       | `session_end`      | `session_id`, `reason`                                              |
| `PostToolUse` (Bash, `run_in_background`) / `SubagentStart` | `background_start` | `task_id`, `kind` (`shell` / `agent`), `label` |
| task exits / `SubagentStop` / `SessionEnd` | `background_end` | `task_id`, `kind`, `label`, `status` (`ok`/`error`/`session_end`/`stale`), `exit_code`, `duration_ms` |

### Background work

Claude Code can run shell commands in the background and spawn subagents. The reporter
keeps a tiny per-session state file (`$XDG_STATE_HOME/star-office-reporter/`) so the
dashboard can show them as helpers working next to the main character:

- a background `Bash` call starts a `shell` task under the task id Claude Code gives it;
  it ends when Claude Code appends `[exited with code N]` to the task's output file. Only
  that trailing line is read, to get the exit code; **the output itself is never read or sent**.
- `SubagentStart` / `SubagentStop` start and end an `agent` task labelled with the
  subagent type (`Explore`, `general-purpose`, …).
- whatever is still open when the session ends is closed with `status: "session_end"`;
  anything older than 6 h is closed as `stale`.

Ends are noticed on the next hook call of the session, i.e. with the next thing Claude does.

## Install

**Requires:** Claude Code with plugin support, `python3` on `PATH`, and a reachable Star
Office UI dashboard (`backend/app.py`, default port `19000`).

```text
/plugin marketplace add gulasz101/claude-code-star-office-reporter
/plugin install star-office-reporter@gulasz101-star-office
```

Then restart Claude Code (or `/reload-plugins`). From a shell:

```bash
claude plugin marketplace add gulasz101/claude-code-star-office-reporter
claude plugin install star-office-reporter@gulasz101-star-office
```

If you wired the older hook by hand (the `reporter/claude-code` hooks block in
`~/.claude/settings.json` from the dashboard repo), remove that block, or every event is
sent twice.

## Configure

All optional; set them in your shell profile or in the `env` block of
`~/.claude/settings.json`.

| Env var                 | Default                    |                                                        |
|-------------------------|----------------------------|--------------------------------------------------------|
| `STAR_OFFICE_URL`       | `http://127.0.0.1:19000`   | Dashboard base URL. Set it to an empty value to switch reporting off without uninstalling. |
| `STAR_OFFICE_AGENT`     | `claude-code`              | Feed identity: the name your character and room get on the dashboard. |
| `STAR_OFFICE_TIMEOUT`   | `2.0`                      | Network cap per event in seconds, clamped 0.25–10.     |
| `STAR_OFFICE_DEBUG`     | off                        | `1` echoes every envelope on stderr (never stdout).    |
| `STAR_OFFICE_STATE_DIR` | `$XDG_STATE_HOME/star-office-reporter` | Where background-task bookkeeping lives. |

## Guarantees

- **Never blocks or breaks Claude Code.** Exit code 2 from a hook blocks the tool call and
  stdout can be injected into Claude's context, so the script always does exit 0, never writes
  to stdout, swallows every error and caps each POST at 2 s. A dead dashboard costs
  nothing.
- **Never leaks the session.** Prompt text, file content, tool output, the full command
  line, error text and absolute paths are never sent. What leaves the machine is a tool
  name, a status, a file basename, a program name plus at most one or two bare
  subcommand tokens for well-known CLIs (`git push`, `npm run build`; flags, values,
  paths and URLs are skipped), opaque session/task ids and a few documented enum values.

## Development

```bash
python3 -m pytest tests          # stdlib + pytest; jsonschema optional
claude plugin validate --strict .
```

The suite runs the real script as a subprocess against a stub dashboard and sweeps every
envelope for canary strings planted in the fixtures. Set `SOU_BACKEND_DIR` to a
star-office-ui-fork `backend/` directory to also validate against the dashboard's own
`event_schema.py`.

## License

MIT
