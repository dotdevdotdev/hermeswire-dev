# Hermes dev config (`~/.hermes/config.yaml`)

Status: dev-only reference. Replaces the retired `.claude/settings.local.json`
(Claude Code's local permission allowlist — dev-only, never tracked, gone after
the Hermes conversion).

## What this replaces

Claude's `settings.local.json` held a `permissions.allow` list for local,
dev-only convenience (bypass interactive permission prompts). Hermes has no
single-file equivalent; the same intent decomposes into several mechanisms in
`~/.hermes/config.yaml`:

| Claude Code | Hermes Agent (v0.19.0) |
|---|---|
| `permissions.allow` (per-tool allowlist) | `command_allowlist` + `approvals.deny` (fnmatch globs) |
| `--dangerously-skip-permissions` | `--yolo` (flag) or `approvals.mode: off` (config) |
| `--allowedTools` / `--tools` / `--disallowedTools` | `-t TOOLSETS` (coarse; `terminal,web,filesystem`) |
| accept all shell hooks non-interactively | `hooks_auto_accept: true` |
| (no Claude analog) | `hooks:` block (pre/post-tool shell hooks) |

> **Correction to issue #17:** `approvals.mode: yolo` is not a valid value.
> `approvals.mode` accepts `manual | smart | off` (default `smart`), where
> `off` = skip all approval prompts (the `--yolo` equivalent). `--yolo` itself
> is a CLI flag / env (`HERMES_YOLO_MODE=1`), not a config value. Verified
> against the installed v0.19.0 source (`hermes_cli/config.py`).

## Dev-only fragment

```yaml
# ~/.hermes/config.yaml  (dev-only; NOT committed)
approvals:
  mode: "off"          # manual | smart | off — off skips all approval prompts (= --yolo)
  timeout: 60
  cron_mode: deny      # deny | approve — what a cron hit on a dangerous command does
  # Deny-list globs survive --yolo / mode:off: a match blocks the command
  # unconditionally (BEFORE the bypass). Case-insensitive; quote patterns that
  # start with * or contain {}/!: sequences. Example:
  #   deny:
  #     - "git push --force*"
  #     - "*rm -rf /*"
  deny: []

# Permanent "always allow" decisions end up here — Hermes writes this when you
# answer [a]lways to a dangerous-command prompt.
command_allowlist: []

# Accept shell-hook registrations without a TTY prompt (needed for headless /
# cron / gateway runs). Equivalent of accepting every hook non-interactively.
hooks_auto_accept: true

# Damage-control hooks (installed/healed by the agentwire safety path — see
# internals/damage-control.md; `agentwire doctor` reports missing matchers).
# Each entry is {matcher, command, timeout}. `matcher` is the Hermes tool name
# as a regex (mcp__<server>__<tool> for MCP tools).
hooks:
  pre_tool_call:
    - matcher: "terminal"
      command: "~/.agentwire/hooks/damage-control/bash-tool-damage-control.py"
      timeout: 60
    - matcher: "write_file"
      command: "~/.agentwire/hooks/damage-control/write-tool-damage-control.py"
      timeout: 60
    - matcher: "patch"
      command: "~/.agentwire/hooks/damage-control/edit-tool-damage-control.py"
      timeout: 60
    - matcher: "read_file"
      command: "~/.agentwire/hooks/damage-control/read-tool-damage-control.py"
      timeout: 60
    - matcher: "search_files"
      command: "~/.agentwire/hooks/damage-control/read-tool-damage-control.py"
      timeout: 60
    - matcher: "mcp__.*"
      command: "~/.agentwire/hooks/damage-control/mcp-tool-damage-control.py"
      timeout: 60
```

## Fidelity notes

- **Tool gating is at toolSET granularity, not per-tool.** `-t terminal`
  enables the whole terminal/process toolset. There is no per-tool
  `--allowedTools` / `--disallowedTools` equivalent; AgentWire's fine-grained
  role tool lists degrade to toolsets + `approvals.deny` (issues #3/#11).
- **`approvals.deny` outranks yolo.** A deny glob blocks the command even under
  `--yolo` / `mode: off` — the user-editable hard stop that replaces the
  "never" half of Claude's allowlist.
- **Quote `mode: "off"`.** YAML 1.1 parses unquoted `off` as boolean `False`;
  Hermes normalizes `False` → `"off"` at load (`tools/approval.py:
  _normalize_approval_mode`), but quoting is unambiguous and matches what
  `hermes config set approvals.mode off` writes (string-typed settings are
  preserved, never coerced to booleans).
- **Shell-hook wire contract:** hooks are PEP 723 scripts invoked with a JSON
  payload on stdin (`hook_event_name`, `tool_name`, `tool_input`, `session_id`,
  `cwd`, `extra`) and may return `{"decision": "block", "reason": …}` (or
  `{"action": "block", "message": …}`) on stdout to block. See
  `agent/shell_hooks.py` in the installed Hermes and
  `internals/hermes-integration.md`.

## Retired Claude Code artifacts (this conversion)

- `scripts/repro_889_paste_delivery.py` — deleted. It was a harness for the
  Claude-paste delivery bug (#889/#867), which is confirmed negative (the real
  root cause was an expired login, #906) and specific to Claude Code's input
  box; Hermes has no analogous input box to probe.
- `agentwire/templates/statusline.sh` — deleted. It consumed Claude Code's
  `statusLine` JSON-on-stdin protocol (`~/.claude/settings.json`); Hermes
  renders its own status bar (toggle `/statusbar`) and exposes no external
  statusline protocol to point the script at. `agentwire/templates/tmux.conf`
  keeps an inline `status-right` for the tmux-visible fields (dir/branch/CPU/RAM).
