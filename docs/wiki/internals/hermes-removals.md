# Removed Claude Features and Their Replacements

During the conversion from Claude Code to Hermes Agent (issues #2/#3), several
Claude-specific features had no Hermes equivalent and were dropped. This page
documents each removal, the rationale, and what (if anything) replaces it.

## Per-tool allow/deny (`--allowedTools` / `--disallowedTools`)

**Removed.** Hermes has no `--allowedTools` or `--disallowedTools` flag.

### `disallowed_tools` (tool-name blacklist) — issue #24

Role `disallowed_tools` (e.g. `Bash`, `Edit`, `AskUserQuestion`) are **tool
names**. Hermes's only per-tool denial mechanism is `approvals.deny` in
`~/.hermes/config.yaml` — a list of **fnmatch globs matched against shell
commands**, not tool names (`tools/approval.py` → `_match_user_deny_rule`).
These are fundamentally different axes:

| Claude `disallowedTools` | Hermes `approvals.deny` |
|---|---|
| Tool names (`Bash`, `Edit`) | fnmatch command globs (`git push --force*`) |
| Blocks the tool entirely | Blocks specific command patterns |
| Fires before the tool runs | Fires before the command runs (below `--yolo`) |

No clean mapping exists: a tool name is not a command pattern. `Bash` would need
to match *every possible shell command* (a `*` glob, which is useless), and
`Edit` isn't even a shell command. A deny-pattern approximation would either be
too broad (blocking everything) or too narrow (missing the point).

**Decision:** `disallowed_tools` is dropped deliberately, not warn-and-dropped.
The old code (`core.py:435`) logged a misleading "no Hermes equivalent yet"
warning that implied a future fix was coming. The warning is removed;
`disallowed_tools` is parsed and merged (for `roles list` / `role show`
display) but has no effect at launch time. Damage-control hooks are the safety
layer for tool access.

### `allowed_tools` (tool-name whitelist) — issue #3

Role `tools` map to Hermes **toolsets** via `-t` (coarse fidelity: a toolset is
a bundle, not a single tool). This is a lossy mapping, not a removal — see
`build_agent_command` in `core.py`.

## `auto` approval posture → `--yolo` — issue #25

Claude's `auto` posture used a classifier to auto-approve safe commands and
prompt for dangerous ones. Hermes's closest analog is `approvals.mode: smart`
(auxiliary-LLM risk assessment: `APPROVE` / `DENY` / `ESCALATE`).

**Decision:** `auto` maps to `--yolo` (full bypass), not `approvals.mode:
smart`.

### Why `smart` was rejected for `auto`

`smart` mode is unsuitable for **unattended** sessions (scheduler dispatch,
`hermeswire ensure`) — the paths where `auto` matters most:

1. **ESCALATE stalls.** When the aux LLM is uncertain, `smart` escalates to an
   interactive approval prompt. An unattended session has no human watching —
   it hangs indefinitely on a prompt no one will answer.
2. **DENY blocks legitimate work.** A `DENY` verdict blocks the command and
   tells the agent not to retry. In unattended automation, a false-positive
   deny can halt a task that has no human to override it.
3. **Aux-LLM dependency.** `smart` makes an extra LLM call per flagged command.
   If the aux LLM fails (no provider configured, rate limit, network error),
   it returns `escalate` → same stall as (1).

`--yolo` bypasses Hermes approvals entirely, but HermesWire's **damage-control
hooks** (`hermeswire-permission.sh` + `bash-tool-damage-control.py`) remain the
safety layer — they fire before `--yolo` and block genuinely dangerous
operations (`rm -rf`, `git push --force`, etc.) regardless of approval mode.
This is the same guard `bypass` already trusts, so `auto` inherits identical
safety properties.

### When `smart` is still useful

`smart` is a good choice for **interactive** sessions (a human at the terminal
who can answer escalation prompts). Set it in `~/.hermes/config.yaml`:

```yaml
approvals:
  mode: smart
```

HermesWire's `prompted` posture (no `--yolo`) uses Hermes's default approvals,
which is `smart` unless the user changed it — so `prompted` already gets
`smart` for free when the user hasn't overridden the mode.

## `restricted` / `readonly` tool-locking postures

**Removed.** Claude's `restricted` and `readonly` postures locked the tool set
to a safe subset. HermesWire has no tool-locking postures — every agent runs
with damage-control hooks as the guard, not tool allowlists. `resolve_posture`
in `project_config.py` raises `ValueError` for these values.

## Slash commands → skills

**Replaced.** Claude slash commands (e.g. `/handoff`) map to Hermes **skills**
loaded via `-s` (e.g. `hermeswire-handoff`). Skills are loadable on demand and
don't require pre-injection into the system prompt.

## `--fork-session`

**Removed.** Claude's `--fork-session` minted a new session id per fork.
Hermes mints its own session id (`<timestamp>_<hex>`), and `--resume <id>`
continues the **same** session (no fork, no new id). There is no fork concept
under Hermes — see `docs/wiki/sessions/conversation-identity.md`.

## `[Pasted text]` chip / queued-message placeholder

**Removed.** Claude's `[Pasted text]` chip and queued-message placeholder have
no Hermes equivalent. HermesWire's polite messaging (`hermeswire msg`) uses a
file inbox + watchdog injection instead of a paste-queue UI affordance.
