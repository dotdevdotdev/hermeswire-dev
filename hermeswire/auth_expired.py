"""Detect Hermes provider auth failure and stop dispatching into it (#13).

The failure this exists for, measured on 2026-08-04 (#867): a scheduled
dispatch did everything right — session created, agent launched, a large
prompt submitted — and the turn was rejected because the agent's provider
credentials were no longer accepted. Zero tokens, no model call, the run
failed in milliseconds. Nothing noticed; the task reported
``incomplete — Timeout waiting for task completion``, which describes the
symptom and actively misleads about the cause. Six hours of dispatch time and
three investigation passes chasing a guardrail, a dispatcher, a timeout and a
paste race.

Hermes auth is provider-based (``hermes auth``, ``~/.hermes/auth.json``,
``hermes model``, provider routing/fallback). There is no Claude-style login
command. The detection signal is Hermes's structured ``AuthError``
(``provider``, ``code``, ``relogin_required``), which surfaces in two places:

* **turn-level** — a ``hermes -z``/``-q`` run that hits auth failure exits 1
  with ``agent failed: AuthError(...)`` on stderr, OR records the failure on
  the session's last assistant message (the #9 store surface);
* **pre-flight** — ``hermes auth status <provider>`` returns
  ``{"logged_in": bool, "error": ...}``, a single cheap subprocess that
  replaces the "no cheap pre-flight" problem that forced Claude onto
  transcript tailing.

"Auth expired" splits into two provider-level classes that must NOT be
conflated:

1. **Hard auth failure (→ outage-gate + email).** ``relogin_required=True``,
   or ``code`` in :data:`HARD_AUTH_CODES`. A human must act (``hermes auth
   add <provider>`` / ``hermes model``, or top up a subscription) — the
   exact analog of Claude's login command, and it inherits the whole
   outage-gate / throttled-email pattern unchanged.
2. **Transient limit (→ retry, do NOT gate).** ``code == "codex_rate_limited"``
   / ``temporarily_unavailable`` / plain 429s. These resolve on their own;
   gating dispatch on them is the false-alarm class this module exists to
   refuse. That class belongs to the park/resume subsystem, not this gate.

Recovery is a property of the same signal, not a separate mechanism: only the
**last** turn decides, so a session that auth-failed and then took a real turn
reads as healthy with nothing to reset.

The machine-wide part matters as much as the detection. An expired credential
is not per-task — every subsequent dispatch hits it — so one detection records
a single outage state, emails the owner ONCE (throttled), and later dispatches
fail fast instead of each burning its own timeout. The state carries
:data:`OUTAGE_TTL` so it cannot wedge the scheduler indefinitely: after it, one
dispatch is let through as a probe, which now fails in seconds rather than
hours.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Provider error codes that mean "a human must act" — keyed-on-auth, permanent
# until re-auth / subscription top-up. Never widened to "any api error":
# a rate-limit or an overloaded upstream is transient and retryable.
HARD_AUTH_CODES = frozenset({
    "subscription_expired",
    "no_usable_credits",
    "account_missing",
    "insufficient_credits",
    "subscription_required",
})

# The transient class: resolves on its own, must NOT gate dispatch. Shared with
# the park/resume subsystem (#8) — see usage_limit for the retry side.
TRANSIENT_CODES = frozenset({"codex_rate_limited", "temporarily_unavailable"})

# Human-readable per-provider fixup, quoted to the operator in email/summary.
# Never used as a detection signal — only as recognizable copy.
RENDERED = {
    "nous": "Nous login expired — run `hermes auth add nous`",
    "openrouter": "OpenRouter key rejected — run `hermes model`",
    "anthropic": "Anthropic key rejected — run `hermes auth add anthropic`",
    "openai": "OpenAI key rejected — run `hermes auth add openai`",
    "zai": "Z.AI credential exhausted — run `hermes auth add zai`",
}

# Fallback for a provider we don't have tailored copy for.
DEFAULT_RENDERED = "provider login expired — run `hermes auth` / `hermes model`"

# How long a recorded outage keeps gating dispatch before one probe is allowed
# through. Bounded on purpose: a stale flag that never cleared would halt every
# scheduled task on the machine, which is a worse failure than the hang. Each
# fresh detection refreshes it, so a real outage keeps gating; a resolved one
# costs at most one fast failure to notice.
OUTAGE_TTL = timedelta(minutes=30)

# Owner-escalation throttle: an out-of-band email, sent on the first sighting
# and then at most once an hour while the outage persists.
ESCALATE_TTL = timedelta(hours=1)

# `AuthError(...)` on stderr. Keyed on the class name + the ``code`` /
# ``relogin_required`` fields, never on a fixed rendered phrase (the rendered
# text has already changed once in this codebase's history).
_AUTH_ERROR_RE = re.compile(r"AuthError\((.*)\)", re.DOTALL)
_KV_RE = re.compile(r"(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^,)\s]+))")


def render_provider(provider: str | None) -> str:
    """The human-readable fixup line for *provider* (or a generic one)."""
    if provider and provider in RENDERED:
        return RENDERED[provider]
    return DEFAULT_RENDERED


def _config_dir() -> Path:
    """Read through the MODULE, not a from-import (#902).

    ``from .core import CONFIG_DIR`` binds the value at import time, so a test
    (or anything else) that patches ``core.CONFIG_DIR`` is silently ignored and
    the code writes to the real ``~/.hermeswire``. Same trap ``core.role_prompts_dir()``
    exists to avoid.
    """
    from . import core

    return Path(core.CONFIG_DIR)


def state_path() -> Path:
    """The ONE outage record. Machine-wide, not per-session or per-task."""
    return _config_dir() / "auth-expired" / "state.json"


def events_path() -> Path:
    return _config_dir() / "auth-expired-events.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log_event(event: str, **fields) -> None:
    """Append an event. Best-effort — telemetry must never break a dispatch."""
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"ts": _now().isoformat(), "event": event, **fields}) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Detection — classification
# ---------------------------------------------------------------------------


def _field(err, name: str):
    """Read a field off either a dict or an attribute-carrying object."""
    if isinstance(err, dict):
        return err.get(name)
    return getattr(err, name, None)


def auth_error_is_hard(err) -> bool:
    """Is *err* a hard auth failure (relogin required or a keyed-on code)?

    Transient limits (``codex_rate_limited``, ``temporarily_unavailable``) and
    plain overloads return False — gating on them would halt the whole
    scheduler over a five-minute window. ``relogin_required`` alone is hard
    even with an unrecognized code.
    """
    if err is None:
        return False
    if _field(err, "relogin_required"):
        return True
    code = _field(err, "code")
    return code in HARD_AUTH_CODES


def parse_auth_error(text: str | None) -> dict | None:
    """Extract an ``AuthError(...)`` from *text*, or None.

    Hermes surfaces auth failure as ``hermes -z: agent failed: AuthError(...)``
    on stderr (exit 1). Parse ``provider`` / ``code`` / ``relogin_required``.
    Returns None when no AuthError is present — the caller then treats the
    failure as non-auth. Does NOT classify hard vs transient; pair with
    :func:`auth_error_is_hard`.
    """
    if not text:
        return None
    m = _AUTH_ERROR_RE.search(text)
    if not m:
        return None
    err: dict = {}
    for key, s1, s2, s3 in _KV_RE.findall(m.group(1)):
        err[key] = s1 if s1 else (s2 if s2 else s3)
    if not any(k in err for k in ("provider", "code", "relogin_required")):
        return None
    if err.get("relogin_required") in ("True", "true"):
        err["relogin_required"] = True
    else:
        err.pop("relogin_required", None)
    return err


def _active_provider() -> str | None:
    """The provider a session routes through (env override, else auth.json).

    Callers that need a named provider for the pre-flight derive it here. Not
    auto-invoked inside :func:`detect` because the polling path must not
    subprocess (or even read auth.json) on every scheduler tick.
    """
    env = os.environ.get("HERMESWIRE_PROVIDER") or os.environ.get("HERMES_ACTIVE_PROVIDER")
    if env:
        return env
    path = Path.home() / ".hermes" / "auth.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data.get("active_provider")
    return None


# ---------------------------------------------------------------------------
# Detection — surfaces
# ---------------------------------------------------------------------------


def probe_provider_auth(provider: str) -> dict | None:
    """Pre-flight: ``hermes auth status <provider>`` → hard-auth outage or None.

    A single subprocess, no transcript. Returns
    ``{"provider", "code", "relogin_required"}`` when the provider reports
    ``logged_in: false`` with a hard auth error; None otherwise (healthy,
    transient, or unparseable — the last reads as "nothing to see", never a
    crash).
    """
    if not provider:
        return None
    try:
        result = subprocess.run(
            ["hermes", "auth", "status", provider],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("logged_in"):
        return None
    err = data.get("error")
    if isinstance(err, str):
        err = parse_auth_error(err) or {"code": err}
    if not isinstance(err, dict) or not auth_error_is_hard(err):
        return None
    return {
        "provider": err.get("provider") or provider,
        "code": err.get("code"),
        "relogin_required": bool(err.get("relogin_required")),
    }


def _session_last_auth_error(session: str) -> dict | None:
    """Last assistant message's hard auth error, via the Hermes session store.

    Wired to ``SessionDB.get_messages`` in #9. Until then this returns None,
    so the detector degrades gracefully to pre-flight + stderr — never a crash
    and never a false negative that blocks a dispatch (the store is an
    *additional* surface, not the only one).
    """
    return None


def detect(
    session: str,
    project_path=None,
    since: float | None = None,
    stderr: str | None = None,
    provider: str | None = None,
) -> dict | None:
    """Is *session*'s last turn a hard Hermes auth failure?

    Returns a detail dict (never a bare bool — the caller has to be able to
    say WHICH surface proved it) or None. Surfaces, most-specific first:

    1. ``stderr`` — the ``hermes -z``/``-q`` output of the failed turn. Proof
       that *this* turn failed, keyed on the ``AuthError`` class.
    2. pre-flight — ``hermes auth status <provider>``, only when the caller
       names the effective provider (the polling loop does not subprocess per
       tick).
    3. message store — the session's last assistant message (#9).
    """
    if stderr:
        err = parse_auth_error(stderr)
        if err and auth_error_is_hard(err):
            return {
                "session": session,
                "provider": err.get("provider"),
                "code": err.get("code"),
                "source": "stderr",
                "evidence": stderr[:500],
            }
    if provider:
        pre = probe_provider_auth(provider)
        if pre:
            return {"session": session, "source": "preflight", **pre}
    msg = _session_last_auth_error(session)
    if msg:
        return {"session": session, "source": "message", **msg}
    return None


# ---------------------------------------------------------------------------
# Machine-wide outage state
# ---------------------------------------------------------------------------


def read_state() -> dict | None:
    try:
        return json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state: dict) -> None:
    """Atomic — a torn write must not leave an unparseable gate behind."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def clear_state() -> bool:
    """Drop the outage record. True iff one was there.

    Called from ``completion.wait_for_completion_signal``'s success path: a
    written task summary is proof a turn ran, which is proof the credentials
    work. ``OUTAGE_TTL`` remains the backstop for a fleet that isn't
    completing anything.
    """
    try:
        state_path().unlink()
        log_event("outage_cleared")
        return True
    except OSError:
        return False


def outage_active(now: datetime | None = None) -> dict | None:
    """The current outage, or None if there isn't a fresh one.

    Freshness is the safety property: a recorded outage gates dispatch only
    while it has been seen within :data:`OUTAGE_TTL`. Past that the gate opens
    and the next dispatch acts as the probe.
    """
    state = read_state()
    if not state:
        return None
    try:
        seen = datetime.fromisoformat(state["last_seen"])
    except (KeyError, TypeError, ValueError):
        return None
    if (now or _now()) - seen > OUTAGE_TTL:
        return None
    return state


def record_outage(detail: dict, source: str = "ensure") -> dict:
    """Record (or refresh) the machine-wide outage and escalate once.

    ``detected_at`` is carried forward across refreshes so the operator can see
    how long the outage has run; refreshing it each sighting would make a
    four-hour outage read as seconds old.
    """
    prior = read_state() or {}
    now = _now()
    sessions = list(prior.get("sessions") or [])
    if detail.get("session") and detail["session"] not in sessions:
        sessions.append(detail["session"])
    state = {
        "detected_at": prior.get("detected_at") or now.isoformat(),
        "last_seen": now.isoformat(),
        "sessions": sessions,
        "provider": detail.get("provider") or prior.get("provider"),
        "code": detail.get("code") or prior.get("code"),
        "evidence": detail.get("evidence"),
        "source": source,
        "host": socket.gethostname(),
        "escalated_at": prior.get("escalated_at"),
        "alerted_at": prior.get("alerted_at"),
    }
    state["escalated_at"] = _escalate(state, prior)
    state["alerted_at"] = _alert_fleet(state, prior)
    write_state(state)
    log_event("outage_detected", session=detail.get("session"),
              provider=state.get("provider"), code=state.get("code"),
              source=source)
    return state


def _escalate(state: dict, prior: dict) -> str | None:
    """Email the owner once per :data:`ESCALATE_TTL` while the outage persists.

    Best-effort in the strong sense: a missing key or a provider failure must
    never turn "we detected the outage and failed the task fast" into an
    exception that fails it slowly instead. The outage state is written either
    way, so the gate works with or without the email. Only a SUCCESSFUL send
    stamps ``escalated_at``, so a persistently broken sender is retried once
    per detection rather than once per TTL — the intended trade.
    """
    previous = prior.get("escalated_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass
    try:
        from .channels.email import send_email

        sessions = ", ".join(state.get("sessions") or []) or "(none recorded)"
        provider = state.get("provider")
        code = state.get("code")
        rendered = render_provider(provider)
        body = "\n".join([
            f"Hermes provider **{provider or '(unknown)'}** on "
            f"`{state.get('host')}` is refusing every turn with "
            f"**{rendered}** (code: `{code}`).",
            "",
            f"- **First seen:** {state.get('detected_at')}",
            f"- **Sessions affected so far:** {sessions}",
            f"- **Evidence:** {state.get('evidence') or state.get('source')}",
            "",
            "Scheduled dispatches are being skipped rather than each burning its "
            "own timeout. Run `hermes auth add <provider>` or `hermes model` to "
            "clear it; the gate re-probes automatically and reopens on the first "
            "successful turn.",
        ])
        result = send_email(
            subject=f"[hermeswire] Hermes {provider} auth expired on "
                    f"{state.get('host')} — dispatch gated",
            body=body,
        )
        if getattr(result, "success", False):
            log_event("escalated", sessions=state.get("sessions"))
            return _now().isoformat()
        log_event("escalate_failed", error=getattr(result, "error", None))
    except Exception as exc:  # never break the caller
        log_event("escalate_failed", error=str(exc))
    return previous


def _alert_fleet(state: dict, prior: dict) -> "str | None":
    """Tell subscribed sessions, on the same clock the owner email rides (#982).

    The outage is machine-wide and nothing but a human re-auth can clear it.
    Stamped separately from ``escalated_at`` while sharing that field's state
    record and its TTL: the email retries per detection when the provider is
    down, and an alert inheriting that retry would turn a broken Resend key
    into one interrupt per dispatch. Enqueueing is a local write, so this
    stamp lands whenever the alert actually did.
    """
    previous = prior.get("alerted_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass
    try:
        from . import fleet_alerts

        sessions = ", ".join(state.get("sessions") or []) or "(none recorded)"
        provider = state.get("provider")
        rendered = render_provider(provider)
        reached = fleet_alerts.emit_for(
            "auth_expired",
            f"Hermes provider {provider or '(unknown)'} login expired on "
            f"{state.get('host')} — every turn is being refused ({rendered}). "
            f"Sessions hit so far: {sessions}. Scheduled dispatch is gated "
            f"until someone runs `hermes auth add <provider>` / `hermes model`; "
            f"nothing recovers on its own. First seen {state.get('detected_at')}.",
        )
    except Exception as exc:  # alerting is best-effort; never break the gate
        log_event("alert_failed", error=str(exc))
        return previous
    if not reached:
        return previous
    log_event("alerted", sessions=state.get("sessions"), to=reached)
    return _now().isoformat()


def check_and_flag(
    session: str,
    project_path=None,
    since: float | None = None,
    source: str = "ensure",
    stderr: str | None = None,
    provider: str | None = None,
) -> dict | None:
    """The fast-path probe for polling loops. Mirrors ``usage_limit.check_and_park``.

    Cheap when nothing is wrong, records the machine-wide outage and escalates
    when it is. Returns the detail dict so the caller can name the provider in
    its own failure message. ``stderr`` carries the failed ``hermes -z``/``-q``
    output; ``provider`` enables the pre-flight subprocess.
    """
    detail = detect(session, project_path=project_path, since=since,
                    stderr=stderr, provider=provider)
    if detail is None:
        return None
    record_outage(detail, source=source)
    return detail


def summary_line(detail: dict | None = None) -> str:
    """The operator-facing reason string. Names the cause, not the symptom."""
    detail = detail or {}
    provider = detail.get("provider")
    code = detail.get("code")
    rendered = render_provider(provider)
    code_part = f" (code: {code})" if code else ""
    where = f" (detected via {detail['source']})" if detail.get("source") else ""
    return (
        f"Hermes provider login expired — {rendered}{code_part}; no completion "
        f"can run until re-auth{where}"
    )
