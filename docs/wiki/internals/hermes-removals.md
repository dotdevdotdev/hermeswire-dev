# Hermes removals — dropped Claude Code features and their replacements

Status: decided. The conversion from Claude Code to Hermes Agent (issue #20, PR #21)
landed. Some Claude Code features have **no direct Hermes equivalent** and were
dropped, replaced by a different mechanism, or removed outright. This page is the
authoritative record of each removal — what went away, why, and what (if anything)
took its place.

Companion page: [hermes-integration.md](hermes-integration.md) (the conversion
strategy and final state).

## 1. Per-tool allow/deny (`--allowedTools` / `--disallowedTools`)

**Claude Code.** `claude --allowedTools Read,Write` and
`--disallowedTools Bash(*)` accepted tool-name globs, letting a caller compose a
precise per-session tool surface at launch time.

**Hermes.** No tool-name-glob flag exists. The closest surface is:

- `-t TOOLSETS` — selects **coarse toolset groups** (e.g. `web`, `terminal`,
  `file`), not individual tool names.
- `approvals.deny` patterns in `~/.hermes/config.yaml` — deny rules matched
  against tool calls, evaluated by Hermes's approval layer.
- **Damage-control hooks** (`agentwire/safety/`) — agentwire's own
  `pre_tool_call` shell-hook layer screens every tool invocation (Bash, file
  edits, MCP tools) against rule files, with `unattended_allow` grants,
  path-scoped entries, and the wrapped-command re-scan. This is the primary
  safety layer under Hermes, not a per-launch allowlist.

`build_agent_command` (`agentwire/core.py`) maps role `tools` to `-t TOOLSETS`
(coarse fidelity — acknowledged as a fidelity loss in #3) and **ignores**
`disallowed_tools` with a warning, because there is no Hermes flag to honor them
(#24 is the open follow-up wiring `disallowed_tools` to a Hermes denial
mechanism).

**Net:** coarse toolsets at launch + damage-control hooks as the real guard,
instead of fine-grained per-tool allow/deny. See
[damage-control.md](damage-control.md) for the rule/hook model.

## 2. `auto` approval classifier -> `--yolo` + damage-control hooks

**Claude Code.** `--enable-auto-mode --permission-mode auto` ran an on-device
classifier that decided whether each tool call was safe enough to auto-approve.

**Hermes.** No classifier. `approvals.mode` ∈ `manual | smart | off` (there is
no `auto`). `--yolo` (`HERMES_YOLO_MODE=1`) bypasses Hermes's approval gate
entirely.

AgentWire maps **both** `bypass` and `auto` postures to `--yolo`
(`agentwire/core.py`, #3): the reasoning is that agentwire's own damage-control
hooks are the safety layer, not a classifier. The `auto` posture loses its
distinct semantics — #25 is the open follow-up re-examining whether `auto`
should map to `approvals.mode: smart` (Hermes's manual alternative) instead of
`--yolo`.

**Net:** no classifier; damage-control hooks + `--yolo` replace it. The `auto`
posture currently behaves identically to `bypass`.

## 3. `restricted` / `readonly` tool-locking postures -> dropped

**Claude Code.** A permission mode could lock the agent into a restricted or
readonly tool surface.

**Hermes.** Dropped. `agentwire/project_config.py` documents this directly:

> Tool-locking postures (restricted/readonly) were dropped — every agent runs
> with damage-control hooks as the guard, not tool allowlists.

`POSTURES` is now `("bypass", "prompted", "auto")` plus the `bare` sentinel (no
agent). There is no restricted/readonly axis; the safety boundary is the
damage-control hook layer, not a frozen toolset.

## 4. Slash commands -> skills

**Claude Code.** Slash commands (`/handoff`, `/memory`, `/skills`, `/compact`,
`/branch`, …) lived in `.claude/commands/` and were sent to the running REPL.

**Hermes.** Hermes has its own slash commands (`/clear`, `/compress`,
`/statusbar`, …) and loads role/feature instructions as **skills** via `-s
SKILLS` (the `~/.hermes/skills/` SKILL.md format). The conversion (#14, #15)
moved agentwire's slash-command-backed features to skills:

- `/handoff` -> the `handoff` feature, rendered by `agentwire/handoff_cli.py` +
  the `/handoff` slash command is still sent to the REPL, but the distillation
  is done in-context by the agent (free), with CLI/MCP rendering the output
  deterministically.
- Role instructions (formerly `--append-system-prompt` text) ride
  `-s agentwire-<role>` skills (`agentwire/roles/__init__.py`,
  `role_skill_name`). Prefixing `agentwire-` avoids shadowing Hermes built-in
  skills.
- `/compact` -> `/compress` (Hermes's name for context compression); `/clear`
  is unchanged (`agentwire/session_context.py`).
- `/branch` (Claude's session forking) -> removed (see §5).

`session_context.py` carries the slash-command mapping table for the context
policies agentwire drives (`/clear`, `/compress`).

## 5. `--fork-session` -> removed (`--resume` continues the same session)

**Claude Code.** `--resume <old> --fork-session --session-id <new>` branched a
conversation: a new conversation id was minted, seeded with the old one's
history.

**Hermes.** No fork. `--resume <id>` **continues the same session** — no new id
is minted, so the resumed conversation IS the original. `--session-id` (caller
chooses the id) is also gone: Hermes mints its own id
(`{YYYYMMDD_HHMMSS}_{6hex}`, captured post-launch via
`core.extract_hermes_session_id`).

Branching still exists at the **git** level (`agentwire worktree`, `agentwire
fork`) and at the **session** level (`agentwire new -s project/branch`), but
there is no conversation-level fork. `history_cli.py` notes this directly:

> there is no `--fork-session` under Hermes (branching is `/branch`)

…where `/branch` refers to the now-removed Claude slash command.

**Net:** `conversation_ids` in session metadata is a chain of Hermes session
ids (each a resume of the prior), not a fork tree. See
[Conversation identity](../sessions/conversation-identity.md).

## 6. `[Pasted text]` chip / queued-message placeholder -> removed

**Claude Code.** Pasting multi-line or long text into the Claude Code REPL
rendered a `[Pasted text #N +M lines]` chip — a collapsed banner that required
an extra Enter to dismiss before the prompt could be submitted. This chip was a
load-bearing hazard for programmatic delivery: `flush_session`'s `stuck`
substring test found nothing behind the chip, so messages could wedge
permanently — never healed, never dead-lettered, never emailed
(`agentwire/voice_layer/confirm.py`).

**Hermes.** Hermes's prompt_toolkit REPL has **no** `[Pasted text]` placeholder.
A paste lands as editable text in the input box; no chip, no extra Enter to
dismiss a banner. `session_ready.py` keys verified delivery on the prompt line
instead of Claude's horizontal-rule box, and the old `allow_chip` guard branch
is gone (the `allow_chip` parameter is kept only for signature stability).

The `queued_placeholder` state (the "Press up to edit queued messages" box
state, distinct from the paste chip) is still detected by `inbox.py` as a
non-penalty defer reason — it's a Hermes prompt_toolkit box state, not the
Claude chip.

**Net:** the chip and its wedge class are gone. Multi-line paste handling in
`pane_manager.send_to_target` still sends a second Enter for long prompts
(defensive, for any REPL that might re-introduce a banner), but under Hermes it
is a no-op.

## Summary table

| Claude Code feature | Hermes replacement | Tracking |
|---|---|---|
| `--allowedTools` / `--disallowedTools` | `-t TOOLSETS` (coarse) + damage-control hooks + `approvals.deny` | #3 (landed); #24 (disallowed follow-up) |
| `auto` approval classifier | `--yolo` + damage-control hooks (no classifier) | #3 (landed); #25 (re-examine mapping) |
| `restricted` / `readonly` postures | dropped (damage-control hooks are the guard) | #3 (landed) |
| slash commands (`/handoff`, `/compact`, …) | skills (`-s`) + Hermes slash commands (`/compress`, `/clear`) | #14, #15 (landed) |
| `--fork-session` | removed (`--resume` continues the same session) | #4 (landed) |
| `[Pasted text]` chip / placeholder | removed (Hermes prompt_toolkit has no chip) | #5 (landed) |
