# Hermes integration strategy (HermesWire → HermesWire)

Status: decided. The conversion landed (PR #21, epic #20). This page describes
the **final state**; for features that were dropped or replaced, see
[hermes-removals.md](hermes-removals.md).

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
- **Session ids** are `{YYYYMMDD_HHMMSS}_{6 hex}` (minted by Hermes in `cli.py`);
  the store is SQLite `~/.hermes/state.db` (`sessions` table: id, source, title,
  cwd, parent_session_id). `--source tool` tags + hides automation sessions.
  AgentWire no longer mints a UUID or passes `--session-id`; a fresh launch
  captures the Hermes-minted id post-launch (#4).
- **Approvals**: `approvals.mode` ∈ `manual | smart | off` (no `auto` classifier).
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
session_context, completion) must switch to a stronger signal: `#{pane_pid}` +
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
| `--session-id` / `--fork-session` / `--resume` | (removed) / (removed) / `--resume <id>` |
| `--allowedTools` / `--tools` / `--disallowedTools` | `-t TOOLSETS` / damage-control hooks + `approvals.deny` (fidelity loss) |
| `--append-system-prompt "$(<file)"` | context files (`.hermes.md`, `AGENTS.md`) + `-s SKILLS` (role instructions ride `agentwire-<role>` skills) |

All entries in this table are **landed** (PR #21 / epic #20). The
`--session-id` / `--fork-session` columns are marked removed rather than mapped:
Hermes mints its own id and `--resume` continues the same session (no fork). See
[hermes-removals.md](hermes-removals.md) for the rationale on each dropped
feature and its replacement.

## Conversation identity (final state)

`conversation_id` is **no longer a locally-minted UUID**. Under Hermes it is the
session id Hermes itself minted (`{YYYYMMDD_HHMMSS}_{6hex}`), captured
post-launch by `core.extract_hermes_session_id()` and recorded by the one writer,
`core.record_session_launch()` (#4). `--resume <id>` continues the SAME session
(no new id is minted), so a resumed session's `conversation_id` IS
`resume_session_id`. A fresh launch records nothing at launch time and captures
the id post-launch.

Resumability is binary under Hermes: `present` in `~/.hermes/state.db`
(resumable) or `absent` (gone) — there is no orphan state, because cwd is a data
column, not part of the storage key (#9). `agentwire restart` walks the
`conversation_ids` chain newest-first and, when nothing resolves, starts FRESH
with the role intact — and says so. See
[Conversation identity](../sessions/conversation-identity.md) for the full
record/restart story.

## Open follow-ups

The conversion landed; these issues track refinements to the mappings above
(not blockers on the conversion itself):

- #22 — wire `extract_hermes_session_id` into the live launch path.
- #23 — install role skills so `-s agentwire-<role>` resolves.
- #24 — map role `disallowed_tools` to a Hermes denial mechanism.
- #25 — re-examine the `auto` -> `--yolo` posture mapping.
