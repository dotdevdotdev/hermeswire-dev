# Removed Claude features and their Hermes replacements

> Living document. Update this, don't create new versions.

The conversion from Claude Code to Hermes Agent dropped several Claude Code
features that have no direct Hermes equivalent. This page records each one,
why it was dropped, and what (if anything) replaces it. It is the companion
to the [Hermes integration strategy](hermes-integration.md), which covers the
flag mapping that *did* survive the conversion.

The common theme: Claude Code had a bespoke per-tool permission system and a
heuristic "Auto Mode" classifier layered on top of it. Hermes has neither —
and HermesWire's own damage-control hooks already do the safety work those
features were nominally for. So the features were dropped rather than
shimmed, and the hooks became the single safety layer. See
[Hermes safety posture](../sessions/hermes-safety-posture.md) for the
four-layer posture that replaces the classifier.

---

## Per-tool allow / deny (`--allowedTools` / `--disallowedTools`)

**Claude Code** let the caller pass tool-name globs at launch:
`--allowedTools "Read,Write"` to restrict the agent to a subset of tools,
`--disallowedTools` to exclude specific ones. HermesWire's role configs
mirrored this with `tools` / `disallowed_tools` lists.

**Hermes** has no tool-name globs. The closest flag, `-t TOOLSETS`, selects
coarse *toolsets* (`web`, `terminal`, `file`, …), not individual tool names —
there is no `-t Read,Write` equivalent. `build_agent_command` in
`hermeswire/core.py` maps role `tools` to `-t TOOLSETS` and logs a warning
that role `disallowed_tools` have no Hermes equivalent.

**Replacement:** the **damage-control `pre_tool_call` hooks** classify every
tool call (`terminal`, `write_file`, `patch`, `read_file`, outbound MCP)
as allow / ask / block at the tool boundary. The hook layer is the real
safety mechanism, not an allowlist of tool names. `approvals.deny`
patterns in `~/.hermes/config.yaml` add per-pattern blocks on top. Together
they cover the practical use case ("don't let the agent run `rm -rf`") without
the per-name fidelity loss of trying to express it as a tool glob.

The fidelity gap is real: a tool Hermes exposes that Claude did not (or vice
versa) is invisible to a role's `tools` list. The hooks catch dangerous
*commands*, not dangerous *tools*, so a benign tool that can run a dangerous
command is still covered by the command matcher.

---

## `auto` approval classifier (`--enable-auto-mode` / `--permission-mode auto`)

**Claude Code** Auto Mode ran a Sonnet 4.6 classifier over every tool call to
decide whether to allow it. The old docs described it as "the safest default
for autonomous work."

**Hermes** has **no Auto Mode classifier** and no flag that reproduces it.
`--enable-auto-mode` / `--permission-mode auto` have no Hermes analog.

**Replacement:** a **four-layer posture** assembled from real Hermes +
HermesWire mechanisms:

1. **Damage-control `pre_tool_call` hooks** — the 300+ rules ported from
   HermesWire, firing through Hermes's hook contract instead of Claude's
   PreToolUse hooks.
2. **Hermes's native dangerous-command approval gate** (`tools/approval.py`)
   — a HARDLINE blocklist that never approves (`rm -rf /`, `mkfs`,
   block-device writes, shutdown) plus a DANGEROUS set that normally prompts.
3. **`--checkpoints`** — rollback before destructive file ops.
4. **`--yolo`** — bypasses the DANGEROUS *prompts* (not the HARDLINE floor or
   the hook blocks) for fully-trusted unattended runs.

Both `bypass` and `auto` postures map to `--yolo`; `prompted` relies on
`approvals.mode: smart`. See [Hermes safety posture](../sessions/hermes-safety-posture.md)
for the full comparison and the edge cases.

---

## `restricted` / `readonly` tool-locking postures

**Claude Code** had permission modes (`restricted`, `readonly`) that locked
the agent to a read-only or limited tool set. HermesWire's early posture
table listed them.

**Hermes** has no equivalent — `-t TOOLSETS` is coarse, not a lock, and there
is no `--permission-mode readonly`.

**Replacement: dropped.** `hermeswire/project_config.py` defines
`POSTURES = ("bypass", "prompted", "auto")` plus the `bare` sentinel (no
agent). The `restricted`/`readonly` postures are gone; a comment in
`project_config.py:20` records the decision:

> Tool-locking postures (restricted/readonly) were dropped — every agent
> runs with damage-control hooks as the guard, not tool allowlists.

If you need read-only behaviour today, scope the role's `tools` to
read-only toolsets (`-t file`) and rely on the hooks to block write-shaped
tool calls. There is no first-class "read-only session" mode.

---

## Slash commands → skills

**Claude Code** shipped built-in slash commands: `/handoff`, `/memory`,
`/skills`, `/clear`, etc. HermesWire called some of these by sending the
literal slash command to the REPL.

**Hermes** has no built-in slash-command system. The `/handoff`, `/memory`,
`/skills` names are Hermes built-in *skills* under `~/.hermes/skills/`,
loaded on demand via `-s`. `hermeswire roles/__init__.py:184` records the
collision-avoidance rule: role instructions ride `hermeswire-<role>` skills
(prefixed to avoid shadowing the Hermes built-ins), and `soul` is the
`SOUL.md` identity slot, never a `-s` skill (#15).

**Replacement:** role instructions are loadable skills, not pre-injected
prompt text. `build_agent_command` emits `-s hermeswire-<role>` for each
role; Hermes loads them on demand. The bundled `/handoff` command still
works — `hermeswire/handoff_cli.py` renders the handoff bundle, and the
distillation happens in-context inside the agent session.

---

## `--fork-session` → removed (`--resume` continues the same session)

**Claude Code** had `--fork-session`: `--resume <old> --fork-session
--session-id <new>` landed a fork at a caller-chosen id, which is what let
`conversation_ids` be a *chain* (each resume mints a new id) rather than a
scalar that goes stale on the first resume.

**Hermes** has no `--fork-session`. `--resume <id>` continues the **same**
session — no new id is minted, no fork occurs. Hermes itself may fork or
compress a session into a new id (`parent_session_id`), captured
post-launch, but the caller never controls it.

**Replacement:** the `conversation_ids` chain still exists in the session
record, but it only grows when *Hermes* forks — not on every resume. A
resume launch's id is already in the chain, so nothing is appended. See
[Conversation identity](../sessions/conversation-identity.md) for the full
record shape and the re-entry predicate.

`--session-id` is also gone: Hermes mints its own id
(`<timestamp>_<hex>`), captured post-launch via
`core.extract_hermes_session_id()` or read from `~/.hermes/state.db`. A
fresh launch records `conversation_id: None` until the id is captured (#4).

---

## `[Pasted text]` chip / queued-message placeholder

**Claude Code** rendered multi-line pastes as a `[Pasted text +N lines]`
chip that required an extra Enter to dismiss and submit. HermesWire's
`send_to_target` (`hermeswire/pane_manager.py`) still sends a second Enter
for long/multi-line pastes to handle this, but the code now also handles
the Hermes REPL (prompt_toolkit), which does not show the chip.

**Hermes** has **no `[Pasted text]` chip** and no queued-message
placeholder. The prompt_toolkit REPL accepts multi-line pastes directly.

**Replacement: removed.** The `send` / `send-keys` paste path in
`hermeswire/send_cli.py` and `pane_manager.py` still sends the extra Enter
defensively (harmless on Hermes, necessary if a Claude Code session were
ever targeted again), but no Hermes-specific handling is needed. The
`queued_placeholder` state in `hermeswire/inbox.py` exists for HermesWire's
own msg-inbox delivery gating (a box holding queued messages), not because
Hermes shows a chip.

---

## See also

- [Hermes integration strategy](hermes-integration.md) — the flag mapping
  that survived the conversion, the agent-vs-daemon detection problem, and
  the final post-conversion state.
- [Hermes safety posture](../sessions/hermes-safety-posture.md) — the
  four-layer posture (hooks + HARDLINE + checkpoints + `--yolo`) that
  replaces the Auto Mode classifier.
- [Damage control](damage-control.md) — the `pre_tool_call` hook layer that
  is the primary safety mechanism.
- [Conversation identity](../sessions/conversation-identity.md) — the
  session record and the `conversation_ids` chain under Hermes.