# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in HermesWire, please report it privately.

**Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

Email: security@agentwire.dev

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fixes (optional)

### What to Expect

- **Acknowledgment:** Within 48 hours
- **Initial Assessment:** Within 1 week
- **Resolution Timeline:** Depends on severity, typically 30-90 days

### Scope

This security policy applies to:
- The HermesWire CLI (`agentwire` command)
- The HermesWire portal (web interface)
- Official HermesWire packages on PyPI

### Out of Scope

- Third-party dependencies (report to their maintainers)
- Self-hosted TTS/STT servers
- User misconfiguration

## Security Features

HermesWire includes built-in security features, layered on top of Hermes Agent's own safety model:

- **Damage Control Hooks:** Block 300+ dangerous command patterns via `pre_tool_call` hooks registered in `~/.hermes/config.yaml` (installed with `agentwire hooks install`). Commands are matched after shell-aware normalization (quote/escape stripping, simple `$VAR` resolution); constructs that can't be statically verified (command substitution, `eval`, `base64 -d | sh`) fail closed.
- **Path Protection:** Prevent access to sensitive files (`.env`, SSH keys, credentials) across shell, file edits/writes, **and native content reads** (`read_file`/`search_files`). Committed env templates (`.env.example`, `.sample`, `.template`, `.dist`) are treated as secret-free and remain readable.
- **Hermes approval gate + HARDLINE floor:** Hermes Agent's own dangerous-command approval gate (`tools/approval.py`, configured via `approvals.mode` / a permanent allowlist) plus its HARDLINE blocklist (`rm -rf /`, `mkfs`, block-device writes, shutdown) fire regardless of `--yolo`.
- **Checkpoints:** `--checkpoints` records a rollback point before destructive file operations.
- **Audit Logging:** All blocked operations are logged

See `docs/wiki/internals/damage-control.md` for details, `docs/wiki/security/damage-control-hardening.md` for the 2026-06 matcher hardening (control-plane path coverage, tilde/`$HOME` canonicalization, `.env` whole-component matching, shell-aware fail-closed matching, read-surface policing) and the new fail-closed-when-unattended behavior, and `docs/wiki/sessions/hermes-safety-posture.md` for the full safety posture.

### Dangerous-command approval bypass (`--yolo`)

By default, HermesWire launches Hermes Agent with `--yolo --accept-hooks`. This is safe because the damage-control hooks (installed via `agentwire hooks install`, registered as `pre_tool_call` in `~/.hermes/config.yaml`) and Hermes's own HARDLINE blocklist enforce safety at the tool layer. `--yolo` bypasses the interactive dangerous-command prompts; it never bypasses the HARDLINE floor (`rm -rf /`, `mkfs`, block-device writes, shutdown) or the damage-control hard blocks. Add `--checkpoints` on top for rollback.

## Trust Model

The portal enforces two security layers in-process:

1. **Origin validation (always on).** Every state-changing request (POST/PUT/DELETE/PATCH) and WebSocket upgrade with an `Origin` header must match the portal's own origin, a localhost equivalent, or an entry in `server.allowed_origins` (exact `scheme://host[:port]` strings — needed when fronting with Cloudflare Tunnel, where the browser's origin is the tunnel domain). Mismatches get a 403 and a log line. Requests without an Origin header (curl, CLI, scripts) pass — CSRF is a browser vector. This protects loopback-only users from malicious pages firing cross-site requests at `localhost:8765`.

2. **Bearer-token auth (required for non-loopback binds).** The portal binds `127.0.0.1:8765` by default — local only. Binding anything else (`0.0.0.0`, a LAN IP) auto-generates a token at `~/.agentwire/portal.token` (mode 0600) and requires it on every request outside the public bootstrap surface (`GET /`, `/health`, `/static/*`): `Authorization: Bearer <token>` on HTTP, `agentwire.bearer.<token>` WebSocket subprotocol. The portal **refuses to start** on a non-loopback bind with auth explicitly disabled. Print the token with `agentwire portal token`; rotate with `--rotate`. Browsers prompt once per device and store it in localStorage.

Token configuration (`server.auth_token` in `~/.agentwire/config.yaml`): unset = use the token file (auto-generated); any string = explicit override; `""` = auth disabled, allowed only on loopback binds. Tokens are compared constant-time and redacted from the config served to the portal's config editor.

What this means in practice:

- **LAN exposure (`server.host: 0.0.0.0`) is protected by the token.** Someone on your network who can reach the port gets 401s until they present it. Treat the token like a password; rotate it if a device is lost.
- **Do not expose the portal directly to the public internet.** Token auth raises the bar on a trusted LAN; it is not a substitute for identity, MFA, audit, and revocation. For anything internet-facing, front it with **Cloudflare Tunnel + Zero Trust** (see `docs/wiki/deployment/remote-access.md`) and add your tunnel domain to `server.allowed_origins`.
- **Project deletion via `/api/projects/delete`** validates the path is absolute, contains no `..`, contains no shell metacharacters, and is not in a protected list. Local execution uses argv form (no shell); SSH execution uses `shlex.quote` per argument. These mitigations reduce blast radius if the perimeter fails.
- **Self-hosted TTS/STT servers are a separate trust domain** — they have no auth of their own and are out of scope here.
