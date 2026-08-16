# Hermes integration strategy (HermesWire → Hermes)

Status: the conversion is **complete**. All six phases landed
(`hermes-conversion` merge through the package rename); every issue the
original version of this page listed as deferred (#15, #4, #3, #11) is
CLOSED. See [Removed Claude features](hermes-removals.md) for the features
that were dropped rather than mapped.

## Decision: hybrid

HermesWire drives the coding agent two different ways, and Hermes offers a
different surface for each. We use **both**, by path:

| HermesWire path | Strategy | Hermes invocation |
|---|---|---|
| `new` / `spawn` (human-watched cockpit pane) | interactive REPL | `hermes chat --cli` |
| `ensure` / scheduler dispatch | headless one-shot | `hermes -z "$PROMPT"` or `hermes chat -q "$PROMPT" -Q` |
| council / handoff / fan-out / voice automation | headless | `hermes chat -q ... -Q --source tool` |
| `restart` / resume | headless resume | `hermes ... --resume <id>` |

Rationale: unattended paths should never depend on parsing a terminal UI. The
cockpit keeps the REPL so a human can watch the agent work in tmux.

## Verified Hermes facts (v0.19.0, read from installed source)

- **Interactive REPL is prompt_toolkit, not curses.** `hermes chat --cli`
  ("classic prompt_toolkit REPL"); `hermes --tui` is the Ink TUI. Default
  controlled by `display.interface` in `~/.hermes/config.yaml`. `curses_ui.py`
  is only for the `hermes console` safe-command subcommand.
- **Prompt glyph** defaults to `❯ ` (`get_active_prompt_symbol("❯ ")`).
- **Status bar** (`_build_status_bar_text`): `⚕ {model} │ {used}/{total} │ {percent}% │ ⏱ …`
  plus a `[█…░…]` context bar (`_build_context_bar`); toggle via `/statusbar`.
- **`hermes -z "prompt"`** — prints only the final answer; no session-id on stdout.
- **`hermes chat -q "prompt"`** — prints answer + exit summary
  (`Session: {id}` / `Resume this session with: hermes --resume {id}`).
- **`hermes chat -Q`** — answer on stdout, `session_id: {id}` on **stderr**; exit 0/1.
- **Session ids** are `{YYYYMMDD_HHMMSS}_{6 hex}` (minted by Hermes in cli.py);
  store is SQLite `~/.hermes/state.db` (`sessions` table: id, source, title,
  cwd, parent_session_id). `--source tool` tags + hides automation sessions.
- **Approvals**: `approvals.mode` ∈ `manual | smart | off` (no "auto").
  `--yolo` = `HERMES_YOLO_MODE=1` (import-frozen). `-t TOOLSETS` selects
  coarse toolsets, NOT tool-name globs (no `--allowedTools` equivalent).
- **Hooks** (`VALID_HOOKS`): `on_session_start`, `on_session_end` (from
  turn_finalizer.py with `completed`/`interrupted`), `on_session_finalize`,
  `on_session_reset`, plus pre/post-tool hooks. Shell-hook wire contract:
  stdin JSON `{hook_event_name, session_id, cwd, extra}` → stdout JSON
  `{block, context}`.
- **Context files** auto-injected: `.hermes.md`, `AGENTS.md`, `CLAUDE.md`,
  `SOUL.md`, `.cursorrules`. Skills use the same SKILL.md format under
  `~/.hermes/skills/`.

## The hard problem: agent-vs-daemon detection

`pane_current_command` for a running `hermes` is a **python process**
(`python3.13`) — the same as HermesWire's own daemons (portal/tts/scheduler).
Claude's was `node`/`claude`/a version string, trivially distinguishable.
Every place that classifies a pane as "agent" vs "daemon" (prompt_router,
session_context, completion) switched to a stronger signal: `#{pane_pid}` +
`ps -p <pid> -o command=` (the cmdline contains `hermes`), or the session's
recorded launch metadata. Do NOT add `python` to the pane-command regex —
that misclassifies daemons (see `test_daemon_skipped_gracefully`,
`TestIsAgentPane::test_command_classification`).

## Flag mapping (final state)

| Claude Code | Hermes Agent |
|---|---|
| `claude` | `hermes chat --cli` |
| `--dangerously-skip-permissions` / `--enable-auto-mode --permission-mode auto` | `--yolo` |
| `--model <m>` | `-m <m>` |
| `--session-id` | (removed — Hermes mints its own id; see [Conversation identity](../sessions/conversation-identity.md)) |
| `--fork-session` | (removed — `--resume` continues the same session; see [Removed features](hermes-removals.md)) |
| `--resume <id>` | `--resume <id>` |
| `--allowedTools` / `--tools` | `-t TOOLSETS` (coarse; per-tool fidelity lost — see [Removed features](hermes-removals.md)) |
| `--disallowedTools` | (no equivalent; `approvals.deny` + damage-control hooks) |
| `--append-system-prompt "$(cat file)"` | `-s SKILLS` + context files (`.hermes.md`, `SOUL.md`); see [#15](https://github.com/dotdevdotdev/hermeswire-dev/issues/15) |

### `build_agent_command` — the one flag-builder (#729)

Fresh sessions AND history resume both route through `core.build_agent_command`,
so a posture always launches with the same flags — no create-vs-resume drift.
The base command is:

```
hermes chat --cli --source tool --accept-hooks
```

- `--source tool` tags every launch so Hermes hides these automation sessions
  from user session lists (#4).
- `--accept-hooks` acknowledges the damage-control hook contract (added after
  the original Phase-1 landing — interactive launches need it so Hermes
  does not block on the hook registration prompt).
- Permission postures: `bypass` and `auto` both map to `--yolo` because
  HermesWire's own damage-control hooks are the safety layer. `prompted`
  relies on `approvals.mode: smart` (no `--yolo`).
- Session resume: `--resume <id>` continues the SAME Hermes session. No new
  id is minted; the `conversation_id` IS `resume_session_id`.
- Role instructions ride `-s hermeswire-<role>` skills (Hermes loads them on
  demand; no `--append-system-prompt`, no temp prompt file, #15). `soul` is
  the `SOUL.md` identity slot, never a `-s` skill.

## Conversation identity is Hermes-owned (#4)

This is the change the stale version of this page got most wrong: it claimed
`conversation_id` was "still a locally-minted UUID." It is not.

- **Hermes mints the id** (`<timestamp>_<hex>`). HermesWire no longer mints a
  UUID or passes `--session-id`. A fresh launch records
  `conversation_id: None` and captures the real id post-launch
  (`core.extract_hermes_session_id()` reads `-Q`/`-q` stderr; the SQLite
  store at `~/.hermes/state.db` is the fallback).
- **`--resume <id>` continues the same session** — no fork, no new id. The
  `conversation_ids` chain in `~/.hermeswire/sessions/<name>/metadata.json`
  only grows when *Hermes* forks/compresses a session into a new id
  (`parent_session_id`), not on every resume.
- **`record_session_launch`** is the one writer and `load_session_metadata`
  the one reader; every session-launch path calls it exactly once. The
  record carries `conversation_ids` (Hermes session ids), `source: "tool"`,
  `resumed_from`, cwd/repo/branch, posture, and roles — enough to
  REGENERATE the launch flags, not merely reference them.

Full detail: [Conversation identity](../sessions/conversation-identity.md).

## What was dropped (not mapped)

The features below have no Hermes equivalent and were removed rather than
shimmed. Full rationale and replacements: [Removed Claude features](hermes-removals.md).

- **Per-tool allow/deny** (`--allowedTools` / `--disallowedTools`) → damage-control
  hooks + `approvals.deny` patterns.
- **`auto` approval classifier** (`--enable-auto-mode`) → `--yolo` + the
  four-layer posture (hooks + HARDLINE + checkpoints + `--yolo`). No
  classifier exists on Hermes; see [Hermes safety posture](../sessions/hermes-safety-posture.md).
- **`restricted` / `readonly` tool-locking postures** → dropped
  (`project_config.py:20`); damage-control hooks are the guard, not tool
  allowlists.
- **Slash commands** → skills (`/handoff` etc. are Hermes built-in skills;
  role instructions ride `hermeswire-<role>` skills, #15).
- **`--fork-session`** → removed (`--resume` continues the same session).
- **`[Pasted text]` chip / queued-message placeholder** → removed (Hermes
  has no chip; the defensive second-Enter in `pane_manager.send_to_target`
  is harmless on Hermes).

## See also

- [Removed Claude features](hermes-removals.md) — the dropped features and
  their replacements.
- [Hermes safety posture](../sessions/hermes-safety-posture.md) — the
  four-layer posture that replaces the Auto Mode classifier.
- [Damage control](damage-control.md) — the `pre_tool_call` hook layer.
- [Conversation identity](../sessions/conversation-identity.md) — the
  session record and the `conversation_ids` chain under Hermes.