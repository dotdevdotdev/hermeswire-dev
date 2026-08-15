# Hermes integration strategy (HermesWire → HermesWire)

Status: decided. Phase 1 (runtime swap) landed on `hermes-conversion`.

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
- **Session ids** are `{YYYYMMDD_HHMMSS}_{6 hex}` (minted in cli.py); store is
  SQLite `~/.hermes/state.db` (`sessions` table: id, source, title, cwd,
  parent_session_id). `--source tool` tags + hides automation sessions.
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
session_context, completion) must switch to a stronger signal: `#{pane_pid}` +
`ps -p <pid> -o command=` (the cmdline contains `hermes`), or the session's
recorded launch metadata. Do NOT add `python` to the pane-command regex —
that misclassifies daemons (see `test_daemon_skipped_gracefully`,
`TestIsAgentPane::test_command_classification`).

## Flag mapping (landed in Phase 1)

| Claude Code | Hermes Agent |
|---|---|
| `claude` | `hermes chat --cli` |
| `--dangerously-skip-permissions` / `--enable-auto-mode --permission-mode auto` | `--yolo` |
| `--model <m>` | `-m <m>` |
| `--session-id` / `--fork-session` / `--resume` | (removed) / `--resume <id>` |
| `--allowedTools` / `--tools` / `--disallowedTools` | `-t TOOLSETS` / `approvals.deny` (fidelity loss) |
| `--append-system-prompt "$(<file)"` | context files / `-s SKILLS` (issue #15) |

## Known gaps (deferred to later phases)

- Role-instruction injection (`--append-system-prompt`) — issue #15.
- `--allowedTools` core allowlist has no equivalent — replaced by
  damage-control hooks + `approvals.deny` (issues #3/#11).
- `conversation_id` is still a locally-minted UUID, not a Hermes session id —
  issue #4.
