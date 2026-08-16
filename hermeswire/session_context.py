"""Session context observability + auto-management (issue #442, Hermes rewrite #8).

Phase 0 made context bloat *visible* and queryable (parse the bar, flag low
sessions, expose via CLI/MCP). Phase 1 adds the *deterministic, zero-LLM*
auto-action: opted-in sessions whose remaining context crosses the warn
threshold get ``/clear`` (stateless service sessions) or ``/compress`` while
they sit idle. See :func:`tick` and :func:`resolve_policy`.

**Opt-in only.** A session is auto-managed solely when it carries an explicit
``context_policy`` (``clear`` | ``compact``); the default everywhere is
``none``. Bundled stateless service sessions are default-on via their
service-registry entry. The watchdog never touches a session without a policy.

Where the headroom comes from (Hermes, v0.19.0)
------------------------------------------------
Hermes has **no** Claude-style (pre-conversion) ``NN%`` context-remaining footer and no
model-name meta line in ``tmux capture-pane`` — the old Claude-Code bar scraper
is gone. Headroom is instead read from Hermes's SQLite session store
(``~/.hermes/state.db``, ``hermes_state.SessionDB``): the ``sessions`` table
carries cumulative ``input_tokens`` / ``output_tokens`` per session plus the
``model`` column, so

    headroom = 1 - (input_tokens + output_tokens) / get_model_context_length(model)

``get_model_context_length`` lives in ``agent/model_metadata.py`` (Hermes's
own package; imported lazily, ``MINIMUM_CONTEXT_LENGTH`` as fallback). The
pane is matched to its store row by the pane's ``#{pane_current_path}`` —
``list_sessions_rich(cwd_prefix=...)`` returns the most-recent session in that
cwd, then ``get_session(id)`` supplies the token columns.

**Advisory, never a live gauge.** Token columns are cumulative session totals,
not a resettable bar: after a ``/compress`` they may not jump back the way
the old Claude Code's bar did, and Hermes auto-compresses on its own
(``agent/context_compressor.py``). So a low headroom must persist across two
watchdog ticks before an auto-``/clear`` fires, and a pane whose cwd has no
store row reads as "unknown / skip" — never "0% → /clear".

Daemon sessions (scheduler / portal / tts / stt / kokoro) run plain processes,
not Hermes conversations — they have no store row and nothing to bloat. They
are detected via the pane's current command and skipped gracefully.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .usage_limit import _tmux
from .utils.event_log import append_event

# pane_current_command values that mean an interactive agent runs in the pane.
# Hermes REPL panes report `hermes` / `uv` / `python3*` (prompt_toolkit); the
# legacy Claude binary names (node / bare version string) are gone. Mirrors the
# Hermes half of prompt_router._AGENT_COMMAND_RE (#7) — kept local to avoid an
# import cycle.
_AGENT_COMMAND_RE = re.compile(r"^(hermes|uv|python3(?:\.\d+)?)$")

DEFAULT_WARN_REMAINING_PCT = 20

# Lazily-opened Hermes SessionDB, mirroring history._db() (#9). ``None`` both
# before first use and when ``hermes_state`` cannot be imported (the wheel must
# not hard-depend on a Hermes version). Tests monkeypatch this to inject a fake
# store.
_db_instance = None


def _db():
    """Open the Hermes session store once, or return ``None`` if unavailable."""
    global _db_instance
    if _db_instance is None:
        try:
            from hermes_state import DEFAULT_DB_PATH, SessionDB
        except ImportError:
            return None
        _db_instance = SessionDB(DEFAULT_DB_PATH)
    return _db_instance


@dataclass
class SessionContext:
    """Context state of a single session's pane."""

    session: str
    pane: int
    is_agent: bool  # interactive Hermes session (vs daemon / bare shell)
    remaining_pct: int | None  # % context HEADROOM left; None when unknown
    model: str | None
    flagged: bool  # remaining_pct <= warn threshold (agents only)
    note: str  # human-readable one-liner

    def to_dict(self) -> dict:
        return asdict(self)


def _pane_command(session: str, pane: int) -> str:
    try:
        result = _tmux(
            ["display", "-t", f"{session}.{pane}", "-p", "#{pane_current_command}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _pane_cwd(session: str, pane: int) -> str:
    """The pane's current working directory ('' on any error)."""
    try:
        result = _tmux(
            ["display", "-t", f"{session}.{pane}", "-p", "#{pane_current_path}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_agent_command(command: str) -> bool:
    return bool(_AGENT_COMMAND_RE.match(command.strip()))


def _session_row(session: str, pane: int) -> dict | None:
    """The Hermes store row for this pane's cwd (most-recent session), or None.

    A pane that launched but was never prompted has no store row and reads as
    None — the "unknown / skip" fail-safe, never a "0% -> /clear".
    """
    db = _db()
    if db is None:
        return None
    cwd = _pane_cwd(session, pane)
    if not cwd:
        return None
    try:
        rows = db.list_sessions_rich(
            cwd_prefix=cwd, order_by_last_active=True, limit=1
        )
    except Exception:
        return None
    if not rows:
        return None
    sid = rows[0].get("id")
    if not sid:
        return None
    try:
        return db.get_session(sid)
    except Exception:
        return None


def _context_length(model: str) -> int | None:
    """The model's context window, or None when unknown/unavailable.

    ``agent.model_metadata`` is Hermes's own package (heavy import, and the
    HermesWire interpreter may not be Hermes's) — imported lazily, with
    ``MINIMUM_CONTEXT_LENGTH`` as the fallback.
    """
    try:
        from agent.model_metadata import get_model_context_length

        return get_model_context_length(model)
    except Exception:
        try:
            from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

            return MINIMUM_CONTEXT_LENGTH
        except Exception:
            return None


def _headroom_pct(row: dict) -> int | None:
    """int % context REMAINING (headroom), or None when unknowable.

    ``headroom = 1 - (input_tokens + output_tokens) / context_length``,
    clamped to 0..100. ``None`` when the row lacks a model or the context
    length can't be resolved — the advisory read must never masquerade as 0%.
    """
    model = row.get("model")
    if not model:
        return None
    ctx_len = _context_length(model)
    if not ctx_len or ctx_len <= 0:
        return None
    used = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
    pct = round((1 - used / ctx_len) * 100)
    return max(0, min(100, pct))


def session_context(
    session: str, pane: int = 0, warn_threshold: int | None = None
) -> SessionContext:
    """Read one session's context state from the Hermes session store.

    ``warn_threshold`` is the *remaining* % at/below which the session is
    flagged (default :data:`DEFAULT_WARN_REMAINING_PCT`). A daemon / non-agent
    pane is surfaced as ``is_agent=False`` and never flagged. An agent pane
    whose cwd has no store row reads as ``remaining_pct=None`` (unknown —
    fail safe, never 0%).
    """
    threshold = warn_threshold if warn_threshold is not None else _warn_threshold()
    command = _pane_command(session, pane)
    is_agent = _is_agent_command(command)

    if not is_agent:
        return SessionContext(
            session=session,
            pane=pane,
            is_agent=False,
            remaining_pct=None,
            model=None,
            flagged=False,
            note=f"daemon / non-agent ({command or 'unknown'}) — no session store",
        )

    row = _session_row(session, pane)
    if row is None:
        return SessionContext(
            session=session,
            pane=pane,
            is_agent=True,
            remaining_pct=None,
            model=None,
            flagged=False,
            note="agent pane but no store row (pre-first-turn or unmatched cwd)",
        )

    model = row.get("model")
    remaining = _headroom_pct(row)
    if remaining is None:
        return SessionContext(
            session=session,
            pane=pane,
            is_agent=True,
            remaining_pct=None,
            model=model,
            flagged=False,
            note="store row present but no token/model data (unknown headroom)",
        )

    flagged = remaining <= threshold
    note = f"{remaining}% context remaining" + (
        f" — LOW (<= {threshold}% warn threshold)" if flagged else ""
    )
    return SessionContext(
        session=session,
        pane=pane,
        is_agent=True,
        remaining_pct=remaining,
        model=model,
        flagged=flagged,
        note=note,
    )


def _list_local_sessions() -> list[str]:
    try:
        result = _tmux(["list-sessions", "-F", "#{session_name}"])
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().splitlines() if s]


def _warn_threshold() -> int:
    """The remaining-% warn threshold from config (best-effort default)."""
    try:
        from .config import get_config

        return int(get_config().session_context.warn_remaining_pct)
    except Exception:
        return DEFAULT_WARN_REMAINING_PCT


# =============================================================================
# Phase 1 — opt-in auto-management (clear / compress)
# =============================================================================

POLICY_NONE = "none"
POLICY_CLEAR = "clear"
POLICY_COMPACT = "compact"
VALID_POLICIES = (POLICY_NONE, POLICY_CLEAR, POLICY_COMPACT)

# The slash command each acting policy sends to the session. Hermes names:
# ``/clear`` (start a new session) and ``/compress`` (context compression —
# there is NO ``/compact``). Verified in hermes_cli/commands.py.
_POLICY_COMMAND = {POLICY_CLEAR: "/clear", POLICY_COMPACT: "/compress"}

EVENTS_FILE = Path.home() / ".hermeswire" / "session-context-events.jsonl"

# Two-tick low-headroom markers. Token columns are cumulative session totals,
# not a live gauge, so a low headroom must be observed on TWO consecutive
# watchdog ticks before an auto-action fires (mirror STUCK_BOX_SWEEPS's
# conservatism). One marker file per session under ~/.hermeswire.
_LOW_MARKER_DIR = Path.home() / ".hermeswire" / "session-context-low"


def _low_marker_path(session: str) -> Path:
    return _LOW_MARKER_DIR / f"{session}.json"


def _low_seen(session: str) -> bool:
    return _low_marker_path(session).exists()


def _mark_low(session: str) -> None:
    from datetime import datetime, timezone

    _LOW_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    _low_marker_path(session).write_text(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat()})
    )


def _clear_low(session: str) -> None:
    try:
        _low_marker_path(session).unlink(missing_ok=True)
    except OSError:
        pass


def _log_event(event: str, **fields) -> None:
    """Append one auto-action audit record (best-effort, never raises)."""
    from datetime import datetime, timezone

    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    append_event(EVENTS_FILE, record)


def resolve_policy(session: str, cfg=None) -> str:
    """The context-management policy for *session* — ``clear`` | ``compact`` | ``none``.

    Resolution, first match wins:
      1. Explicit per-session override in config ``session_context.policies``
         (a ``{name: policy}`` map — for arbitrary sessions like councils/anchors).
      2. The session's service-registry entry ``context_policy`` (bundled
         stateless services are default-on here — notifications => ``clear``).
      3. ``none`` — never auto-managed.

    An unknown/invalid value is treated as ``none`` (fail safe — never act).
    """
    if cfg is None:
        try:
            from .config import get_config

            cfg = get_config()
        except Exception:
            return POLICY_NONE

    policies = getattr(cfg.session_context, "policies", {}) or {}
    override = policies.get(session)
    if override in VALID_POLICIES:
        return override

    try:
        from . import services

        for svc in services.registry(cfg):
            if svc.name == session:
                pol = getattr(svc, "context_policy", POLICY_NONE)
                return pol if pol in VALID_POLICIES else POLICY_NONE
    except Exception:
        pass

    return POLICY_NONE


def act_on_session(session: str, policy: str, threshold: int | None = None) -> dict:
    """Evaluate one opted-in session and ``/clear`` | ``/compress`` if warranted.

    Returns a result dict (always; never raises). ``acted`` is True **only when
    the command was a verified delivery** — the paste is routed through
    :func:`prompt_router.safe_deliver`, so a guarded refusal or a silent paste
    failure is logged honestly as NOT acted (and retried next tick), never
    assumed sent.

    Delivery mirrors the sibling inbox drain (:func:`inbox.flush_session`), and
    the two conservatism guards replace the old Claude Code's ``prompt_is_empty``
    collision check (removed in #7 — Hermes has no scrapeable prompt box to
    gate on, and ``safe_deliver`` already refuses gone/non-agent targets):

    1. **Low-headroom must persist across two ticks.** The store token columns
       are cumulative totals, not a live gauge — the first low sighting is
       recorded (``first_low_sighting``) and only acted on the next tick.
    2. **safe_deliver** — adds the gone / non-agent refusals AND a verified
       paste (:func:`session_ready.send_verified`, marker = the command text).
    """
    if policy not in _POLICY_COMMAND:
        return {"session": session, "acted": False, "skipped": "no_policy"}

    threshold = threshold if threshold is not None else _warn_threshold()
    ctx = session_context(session, 0, threshold)

    if not ctx.is_agent or ctx.remaining_pct is None:
        return {"session": session, "acted": False, "skipped": "unknown"}
    if not ctx.flagged:
        _clear_low(session)
        return {
            "session": session, "acted": False, "skipped": "above_threshold",
            "remaining_pct": ctx.remaining_pct,
        }

    # Low headroom: require it to persist across two ticks before acting.
    if not _low_seen(session):
        _mark_low(session)
        _log_event(
            "first_low_sighting", session=session, policy=policy,
            remaining_pct=ctx.remaining_pct,
        )
        return {
            "session": session, "acted": False, "deferred": "first_low_sighting",
            "remaining_pct": ctx.remaining_pct,
        }

    from . import prompt_router

    command = _POLICY_COMMAND[policy]
    try:
        delivered, reason = prompt_router.safe_deliver(session, 0, command)
    except Exception as exc:  # delivery must never break the watchdog
        _log_event(
            "send_failed", session=session, policy=policy,
            remaining_pct=ctx.remaining_pct, error=str(exc),
        )
        return {"session": session, "acted": False, "deferred": "send_failed"}

    if not delivered:
        # Guarded refusal (gone / not-agent) or an unverified paste — logged
        # honestly as not acted, retried next tick.
        _log_event(
            "deferred", session=session, policy=policy,
            remaining_pct=ctx.remaining_pct, reason=reason,
        )
        return {
            "session": session, "acted": False, "deferred": reason,
            "remaining_pct": ctx.remaining_pct,
        }

    _clear_low(session)
    _log_event(
        "acted", session=session, policy=policy, command=command,
        remaining_pct=ctx.remaining_pct, threshold=threshold,
    )
    return {
        "session": session, "acted": True, "policy": policy, "command": command,
        "remaining_pct": ctx.remaining_pct,
    }


def tick() -> dict:
    """One auto-context-management pass over opted-in sessions.

    The 4th sweep on ``hermeswire limits tick`` (after usage-limit park, prompt
    routing, and the inbox drain). For every local session carrying a
    ``clear``/``compact`` policy whose store-derived headroom has crossed the
    warn threshold for two consecutive ticks, send ``/clear`` | ``/compress``.
    Deterministic, zero-LLM. Never raises.

    A session that defers (first sighting / busy / parked / mid-turn) is simply
    retried next tick; a session above threshold is left alone. A successful
    ``/clear`` starts a NEW Hermes session (new id), so the headroom read jumps
    back up and it won't be re-flagged; a silently-failed one *should* be
    retried.
    """
    try:
        from .config import get_config

        cfg = get_config()
    except Exception:
        return {"skipped": "no_config"}

    if not getattr(cfg.session_context, "auto_enabled", True):
        return {"skipped": "disabled"}

    threshold = int(getattr(cfg.session_context, "warn_remaining_pct", DEFAULT_WARN_REMAINING_PCT))

    acted, deferred = [], []
    for session in _list_local_sessions():
        policy = resolve_policy(session, cfg)
        if policy not in _POLICY_COMMAND:
            continue
        result = act_on_session(session, policy, threshold)
        if result.get("acted"):
            acted.append(result)
        elif result.get("deferred"):
            deferred.append(result)
    return {"acted": acted, "deferred": deferred}
