> Living document. Update this, don't create new versions.

# Hermes Safety Posture

Hermes Agent has **no "Auto Mode" classifier.** The old AgentWire docs described
Claude Code's Auto Mode (a Sonnet 4.6 classifier reviewing every tool call) as the
"safest default for autonomous work." That classifier does not exist on Hermes, and
there is **no flag that reproduces it** — `--enable-auto-mode` / `--permission-mode auto`
have no Hermes analog and should not be mapped to anything. The honest equivalent is a
**posture assembled from four layers**, each of which is a real, configurable Hermes or
AgentWire mechanism.

---

## The four layers

1. **Damage-control `pre_tool_call` hooks** — ported from AgentWire's 300+ rules.
   Installed via `agentwire hooks install` and registered as `pre_tool_call` in
   `~/.hermes/config.yaml` (see [Damage control](../internals/damage-control.md)).
   These classify every tool call (`terminal`, `write_file`, `patch`, `read_file`,
   `search_files`, outbound MCP) as allow / ask / block at the tool boundary — the
   same shell-aware matcher and path protections AgentWire always enforced, now firing
   through Hermes's hook contract instead of Claude's PreToolUse hooks.

2. **Hermes's native dangerous-command approval gate** — `tools/approval.py` ships a
   HARDLINE blocklist (catastrophic, never-approvable commands: `rm -rf /`, `mkfs`,
   block-device writes, shutdown) plus a DANGEROUS pattern set that normally prompts
   for approval. Configured through `approvals.mode` (`manual | smart | off`) and a
   permanent allowlist in `~/.hermes/config.yaml`.

3. **`--checkpoints`** — Hermes's rollback mechanism: before destructive file
   operations, the agent records a checkpoint so a bad edit can be reverted instead of
   lived with.

4. **`--yolo`** — bypasses the interactive dangerous-command approval prompts for
   fully-trusted runs. It is what AgentWire's `bypass` and `auto` postures map to
   (see [Hermes integration](../internals/hermes-integration.md)). Critically, `--yolo`
   does **not** bypass the HARDLINE floor or the damage-control hard blocks — those fire
   regardless of approval mode.

Assembled, these layers give an unattended run the same "don't let it delete the world
at 3am" guarantee the classifier used to promise, but from deterministic hooks and a
blocklist rather than an AI reviewing its own transcript.

---

## CLI flags

The `auto` posture in AgentWire maps to `--yolo`, not to any classifier:

```bash
# AgentWire posture: auto  →  Hermes launch (built by agentwire)
hermes chat --cli --source tool --yolo
```

There is no `--enable-auto-mode` and no `--permission-mode`. Approval behavior is
configured in `~/.hermes/config.yaml`, not on the command line:

```yaml
approvals:
  mode: smart           # manual | smart | off
  allow: []             # permanent allowlist of commands that never prompt
```

Add `--checkpoints` (or set the equivalent in config) on top of `--yolo` when a run
needs rollback before destructive file ops. `--yolo` only silences the DANGEROUS
prompts; it never silences HARDLINE.

---

## What HARDLINE still blocks (even with `--yolo`)

- `rm -rf /`, `rm -rf ~` and other root/home recursive deletion
- `mkfs.*`, block-device writes (`dd of=/dev/…`), disk wipes
- `shutdown`, `reboot`, `poweroff`
- Whatever else `tools/approval.py`'s HARDLINE set matches — consult the installed
  Hermes source for the authoritative list; the point is that these are
  **never silently allowed**.

The damage-control hard blocks (`block`-tier rules in `rules/*.yaml`) sit in front of
all of it and fire for every session regardless of posture, `--yolo`, or approval mode.

---

## Comparison to the old postures

| | `bypass` (old) | `auto` (old, classifier) | **Hermes (`--yolo` + hooks)** |
|-|----------------|--------------------------|-------------------------------|
| Approval prompts | None | None (classifier decides) | None (`--yolo`), except HARDLINE which never prompts — it blocks |
| Safety checks | None | AI classifier | Damage-control hooks + HARDLINE blocklist |
| Mass file deletion | Allowed | Blocked | Blocked (hooks + HARDLINE) |
| Credential exfiltration | Allowed | Blocked | Blocked (zeroAccessPaths read hook) |
| Force push to main | Allowed | Blocked | Blocked (`git push --force` block rule) |
| Normal file edits | Allowed | Allowed | Allowed |
| Token overhead | None | ~20% | None (deterministic, no LLM in the loop) |
| Headless stall on block | N/A | idle_timeout catches it | Block returns to the model immediately; idle_timeout still catches a stalled agent |

**Bottom line:** the classifier-free posture does what `auto` was meant to do for
unattended work — prevent catastrophic failures when nobody's watching — using hooks
and a blocklist that are deterministic, auditable, and free of the classifier's token
overhead and false-negative rate. It is **not** the same mechanism, and operators
should not assume classifier-grade judgment: an action the hooks don't classify is
allowed.

---

## Using the posture in AgentWire

The posture is wired through every entry point — `.agentwire.yml`, the CLI, and MCP —
and `build_agent_command()` in `agentwire/core.py` maps `posture:` to Hermes flags at
launch (`auto` and `bypass` both become `--yolo`; `prompted` relies on
`approvals.mode: smart`).

**`.agentwire.yml`:**
```yaml
posture: auto
roles:
  - task-runner
```

**CLI:**
```bash
agentwire new myproject --posture auto
```

**MCP:**
```python
session_create(name="myproject", posture="auto")
```

There is no core-allowlist injection: Hermes has no `--allowedTools` equivalent
(`-t TOOLSETS` selects coarse toolsets, not tool names). Anything not matched by the
damage-control hooks or the HARDLINE/DANGEROUS approval sets runs without friction —
which is the point of `--yolo`.

---

## Edge cases

**An unattended run hits a `block`-tier hook match:** the hook returns a block
directive, the model sees the reason, and — if it can't proceed — goes idle.
AgentWire's `idle_timeout` reaps the session; the scheduler reports it and (for
`ask`-tier rules that fail closed unattended) the owner is emailed. See
[Damage control § Unattended guardrail](../internals/damage-control.md#unattended-no-human-present-guardrail).

**Mixed postures:** some tasks use `auto` (production repos), others `bypass`
(sandboxed experiments). Both map to `--yolo` today, so the real difference between
them is **nothing at the flag level** — safety comes from the hooks and the rule set,
which are identical for both. Keep the distinction in `.agentwire.yml` for clarity and
future fidelity; it currently changes nothing about enforcement.

**`approvals.mode: smart` for a prompted session:** interactive non-`--yolo` sessions
get Hermes's DANGEROUS-command prompts routed through AgentWire's portal
(`agentwire-permission.sh`), and damage-control `ask`-tier matches escalate to the same
gate. That is the `prompted` posture, not this one.

---

## References

- [Hermes integration strategy](../internals/hermes-integration.md) — the flag mapping
  (`--dangerously-skip-permissions` / `--enable-auto-mode` → `--yolo`) and verified
  Hermes facts (v0.19.0).
- [Damage control](../internals/damage-control.md) — the `pre_tool_call` hook layer
  that is the primary safety mechanism for this posture.
- [SECURITY.md](../../../SECURITY.md) — the safety model end-to-end, including the
  HARDLINE floor and `--yolo` tradeoff.
