"""Provider-limit recovery — deterministic, zero-LLM.

Claude Code parked on an interactive usage-limit dialog (``/rate-limit-options``)
that blocked forever. Hermes has **no such dialog**: provider rate-limit /
quota / credit failures surface as a structured ``AuthError`` (provider, code,
``relogin_required``) on the failed turn's stderr or the session's last
message (see #13). This module detects that error, records a per-session park
state, emails the owner, and nudges the session back to work after the limit
window passes.

Every step is plain code: at the moment this fires, usage is exhausted by
definition — no agent can run to orchestrate the recovery, and a recovery
mechanism must be more reliable than the thing it recovers.

Detection runs in two places:

- ``hermeswire limits tick`` — a stateless launchd watchdog (every 60s)
  sweeping local tmux sessions for provider-limit errors. Also the resume
  timer: a tick that finds a parked session past its reset time sends the
  resume nudge.
- ensure's completion poll (``completion.wait_for_completion_signal``) —
  fast path (≤10s) for scheduler-dispatched tasks.

A limit-parked Hermes session needs **no keystroke** — there is no menu to
answer. Parking is just: write the park state, notify the owner, and arm the
resume nudge. The transient class (``codex_rate_limited`` /
``temporarily_unavailable``) clears on its own and gets a short resume window;
the hard/credit class (``insufficient_credits``, ``no_usable_credits``,
``subscription_expired``, ``account_missing``) is #13's outage gate, but is
still parked here so the session is not re-dispatched into the same refusal.

State: one JSON file per parked session under ``~/.hermeswire/usage-limit/``
(worktree session names contain ``/`` and nest one directory down, same as
the tasks dir). File presence in the active dir == "parked" — that is the
guard ensure, the scheduler, and the idle hook all check so a
parked session is never prompted, re-dispatched, or reaped. Files are
archived to ``usage-limit/done/`` on resume.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .utils.event_log import append_event

STATE_DIR = Path.home() / ".hermeswire" / "usage-limit"
DONE_DIR = STATE_DIR / "done"
EVENTS_FILE = Path.home() / ".hermeswire" / "usage-limit-events.jsonl"

# Owner-specified fixed resume message — the only agent involvement in the
# whole recovery story is the parked session acting on this nudge.
RESUME_NUDGE = (
    "You were interrupted by a usage limit; the limit has reset. "
    "Continue your task from where you stopped and complete it fully."
)

# A provider limit error has no parseable reset time (unlike Claude's dialog).
# Transient rate limits (codex_rate_limited / plain 429s) clear on their own
# within a short window; use that as the resume nudge target.
TRANSIENT_RESET_WINDOW = timedelta(minutes=5)
# Nudge this long after the stated reset so we're safely past it.
RESUME_GRACE = timedelta(minutes=1)
# Give up nudging after this many failed attempts and archive as failed.
MAX_RESUME_ATTEMPTS = 5


# =============================================================================
# Small utilities
# =============================================================================


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    """Collapse all whitespace so narrow-pane line wraps can't break matches."""
    return " ".join(text.split())


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def log_event(event: str, **fields) -> None:
    """Append an event to the usage-limit events log (best-effort)."""
    record = {"ts": _now().isoformat(), "event": event, **fields}
    append_event(EVENTS_FILE, record)


def _tmux(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout
    )


def _capture(target: str, scrollback: int | None = None, escapes: bool = False) -> str:
    """Capture pane text. Visible screen only unless ``scrollback`` lines given.

    ``escapes=True`` adds ``-e`` (preserve SGR escape sequences) so callers can
    distinguish dim-rendered ghost/autosuggest text from real typed text.
    """
    cmd = ["capture-pane", "-t", target, "-p"]
    if escapes:
        cmd.append("-e")
    if scrollback:
        cmd += ["-S", f"-{scrollback}"]
    try:
        result = _tmux(cmd)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _session_exists(session: str) -> bool:
    try:
        return _tmux(["has-session", "-t", f"={session}"]).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _list_sessions() -> list[str]:
    """All local tmux session names (empty on any error)."""
    try:
        result = _tmux(["list-sessions", "-F", "#{session_name}"])
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().splitlines() if s]


# =============================================================================
# State files
# =============================================================================


def state_path(session: str) -> Path:
    return STATE_DIR / f"{session}.json"


def is_parked(session: str) -> bool:
    """True iff this session is currently parked on a provider limit."""
    return state_path(session).exists()


def read_park_state(session: str) -> dict | None:
    try:
        return json.loads(state_path(session).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_park_state(state: dict) -> None:
    _atomic_write(state_path(state["session"]), state)


def list_parked() -> list[dict]:
    """All active park states (excludes the done/ archive)."""
    if not STATE_DIR.exists():
        return []
    states = []
    for path in sorted(STATE_DIR.rglob("*.json")):
        if DONE_DIR in path.parents:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("session"):
            states.append(data)
    return states


def archive_state(state: dict, status: str) -> None:
    """Move a park state into done/ with a final status."""
    state["status"] = status
    state["archived_at"] = _now().isoformat()
    flat = state["session"].replace("/", "_")
    ts = _now().strftime("%Y%m%dT%H%M%S")
    _atomic_write(DONE_DIR / f"{flat}-{ts}.json", state)
    try:
        state_path(state["session"]).unlink(missing_ok=True)
    except OSError:
        pass


# =============================================================================
# Detection
# =============================================================================


def detect_limit(
    session: str, pane_index: int = 0, stderr: str | None = None,
    provider: str | None = None,
) -> dict | None:
    """Detect a provider limit/credit error for *session* (transient or hard).

    Returns a detail dict (never a bare bool — the caller must be able to say
    WHICH surface proved it) or None. Surfaces, most-specific first:

    1. ``stderr`` — the failed ``hermes -z``/``-q`` output of the turn. Proof
       that *this* turn hit the limit, keyed on the ``AuthError`` class.
       Detects BOTH the transient class (``codex_rate_limited`` /
       ``temporarily_unavailable`` — self-resolving, this subsystem's job) and
       the hard/credit class (``insufficient_credits``, ``no_usable_credits``,
       ``subscription_expired``, ``account_missing`` — #13's gate, but still
       parked here so the session isn't re-dispatched into the refusal).
    2. session store — the session's last assistant message (stub until the #9
       store-message surface is wired; mirrors auth_expired).
    3. ``hermes auth status <provider>`` — pre-flight, hard auth only, and only
       when the caller names the provider (the polling loop does not subprocess
       per tick).

    A session that merely *reports* another session's quota error (an
    orchestrator reviewing output) is never parked: only this session's own
    structured ``AuthError`` counts — exactly #13's transcript-vs-pane rule.
    """
    from . import auth_expired

    if stderr:
        err = auth_expired.parse_auth_error(stderr)
        if err:
            code = err.get("code")
            transient = bool(code in auth_expired.TRANSIENT_CODES)
            hard = auth_expired.auth_error_is_hard(err)
            if transient or hard:
                return {
                    "session": session,
                    "provider": err.get("provider"),
                    "code": code,
                    "transient": transient,
                    "hard": hard,
                    "source": "stderr",
                    "evidence": stderr[:500],
                }

    msg = _session_last_limit_error(session)
    if msg:
        return {"session": session, "source": "message", **msg}

    if provider:
        pre = auth_expired.probe_provider_auth(provider)
        if pre:
            return {
                "session": session,
                "provider": pre.get("provider") or provider,
                "code": pre.get("code"),
                "transient": False,
                "hard": True,
                "source": "preflight",
                "evidence": "",
            }
    return None


def _session_last_limit_error(session: str) -> dict | None:
    """Last assistant message's provider limit error, via the Hermes store.

    Mirrors ``auth_expired._session_last_auth_error``: the #9 store-message
    surface is not wired yet, so this returns None and the detector degrades
    gracefully to stderr + pre-flight — never a crash, never a false park.
    """
    return None


def _recovery_config() -> tuple[bool, set[str]]:
    """(enabled, excluded session names) from config.yaml.

    Gates NEW parks only — resume always drains existing park states, even
    for sessions excluded (or the feature disabled) after they were parked.
    """
    try:
        from .config import get_config

        cfg = get_config().usage_limit
        return bool(cfg.enabled), set(cfg.exclude_sessions)
    except Exception:
        return True, set()


def check_and_park(session: str, pane_index: int = 0, source: str = "ensure") -> bool:
    """True iff the session is (or just became) parked on a provider limit.

    The fast-path probe for polling loops (ensure's completion wait): cheap
    when nothing is wrong, parks deterministically when a provider limit error
    is detected, and honors a park the watchdog already performed.
    """
    if is_parked(session):
        return True
    enabled, excluded = _recovery_config()
    if not enabled or session in excluded:
        return False
    limit = detect_limit(session, pane_index)
    if limit:
        park(session, pane_index, source=source, limit=limit)
        return is_parked(session)
    return False


# =============================================================================
# Park
# =============================================================================


def _task_info(session: str) -> dict:
    """Best-effort context for notifications: task name + project path."""
    info: dict = {}
    task_file = Path.home() / ".hermeswire" / "tasks" / f"{session}.json"
    try:
        ctx = json.loads(task_file.read_text())
        if isinstance(ctx, dict) and ctx.get("task"):
            info["task"] = ctx["task"]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        result = _tmux(["display-message", "-p", "-t", session, "#{pane_current_path}"])
        if result.returncode == 0 and result.stdout.strip():
            info["project_path"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return info


def park(
    session: str, pane_index: int = 0, source: str = "watchdog",
    limit: dict | None = None,
) -> dict | None:
    """Park a session that hit a provider limit error.

    A limit-parked Hermes session needs no keystroke — there is no menu to
    answer. Just write the park state, notify the owner, and arm the resume
    nudge. Idempotent: an already-parked session is a no-op.
    """
    if is_parked(session):
        return None

    now = _now()
    limit = limit or {}
    reset_at = now + TRANSIENT_RESET_WINDOW

    state = {
        "session": session,
        "pane": pane_index,
        "status": "parked",
        "source": source,
        "detected_at": now.isoformat(),
        "parked_at": now.isoformat(),
        "reset_at": reset_at.isoformat(),
        "resume_at": (reset_at + RESUME_GRACE).isoformat(),
        "provider": limit.get("provider"),
        "code": limit.get("code"),
        "transient": bool(limit.get("transient")),
        "notified": False,
        "resume_attempts": 0,
        "excerpt": limit.get("evidence") or "",
        **_task_info(session),
    }
    write_park_state(state)
    log_event(
        "session_parked", session=session, pane=pane_index, source=source,
        code=state["code"], provider=state["provider"],
        reset_at=state["reset_at"], resume_at=state["resume_at"],
        task=state.get("task"),
    )
    _notify_parked(state)
    return state


def _fmt_local(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso)
            .astimezone()
            .strftime("%Y-%m-%d %I:%M%p %Z")
        )
    except ValueError:
        return iso


def _notify_parked(state: dict) -> bool:
    """Email the owner that a session is parked. Plain Resend call, no agent.

    Called from ``park`` AND from :func:`resume_due` — the latter on every tick
    until ``notified`` sticks, which on a keyless machine is never. Anything
    added here must carry its own idempotence rather than assuming one call.
    """
    session = state["session"]
    code = state.get("code")
    provider = state.get("provider")
    lines = [
        f"Session **{session}** on `{socket.gethostname()}` hit a provider "
        f"limit (code `{code}`) and was parked.",
        "",
        f"- **Task:** {state.get('task') or '(none — not a tracked task)'}",
        f"- **Project:** {state.get('project_path') or 'unknown'}",
        f"- **Provider:** {provider or '(unknown)'}",
        f"- **Detected:** {_fmt_local(state['detected_at'])}",
        f"- **Limit clears (est.):** {_fmt_local(state['reset_at'])}",
        f"- **Auto-resume:** {_fmt_local(state['resume_at'])}",
        "",
        "The session will be nudged automatically after the limit window — "
        "no action needed.",
        "",
    ]
    if state.get("excerpt"):
        lines += ["```", state["excerpt"], "```"]
    _alert_fleet(state)
    return _send_notification(
        state,
        subject=f"[hermeswire] provider limit: {session} parked until {_fmt_local(state['reset_at'])}",
        body="\n".join(lines),
        mark_notified=True,
    )


def _alert_fleet(state: dict) -> None:
    """Tell subscribed sessions a session parked (#982). A NOTE, deliberately.

    A park is self-healing: the resume nudge is armed, and the owner's own
    email ends "no action needed". There is nothing for anyone to do with it
    in the next thirty seconds, so it does not earn the kind that may be acted
    on out of turn — it is exactly the fleet news that a gap. Demoting it is
    what keeps `escalation` worth acting on.

    **Its own stamp, on the park record.** ``fleet_alerted`` is stamped on
    successful ENQUEUE — a local write, which cannot fail the way the email
    does — and lives on the park state, so it clears with the park: a session
    that parks again genuinely is new news.
    """
    if state.get("fleet_alerted"):
        return
    try:
        from . import fleet_alerts

        reached = fleet_alerts.emit_for(
            "usage_limit_park",
            f"Session {state['session']} hit a provider limit and was parked"
            f"{' on task ' + state['task'] if state.get('task') else ''}. "
            f"Limit clears ~{_fmt_local(state['reset_at'])}; it will be nudged "
            f"to continue at {_fmt_local(state['resume_at'])}. No action needed.",
        )
        if reached:
            state["fleet_alerted"] = True
            if is_parked(state["session"]):
                write_park_state(state)
    except Exception as e:  # best-effort; the park is what matters
        log_event("fleet_alert_failed", session=state.get("session"), error=str(e))


def _notify_resumed(state: dict) -> None:
    session = state["session"]
    _send_notification(
        state,
        subject=f"[hermeswire] provider limit reset: {session} resumed",
        body=(
            f"Session **{session}** on `{socket.gethostname()}` was nudged to "
            f"continue after its provider limit cleared.\n\n"
            f"- **Task:** {state.get('task') or '(none)'}\n"
            f"- **Parked:** {_fmt_local(state['parked_at'])}\n"
            f"- **Resumed:** {_fmt_local(_now().isoformat())}"
        ),
        mark_notified=False,
    )


def _send_notification(state: dict, subject: str, body: str, mark_notified: bool) -> bool:
    try:
        from .channels.email import send_email

        result = send_email(subject=subject, body=body)
        if result.success:
            if mark_notified:
                state["notified"] = True
                if is_parked(state["session"]):
                    write_park_state(state)
            log_event("notify_sent", session=state["session"], subject=subject)
            return True
        log_event("notify_failed", session=state["session"], error=result.error)
    except Exception as e:
        log_event("notify_failed", session=state["session"], error=str(e))
    return False


# =============================================================================
# Sweep (detection backstop across every local tmux session)
# =============================================================================


def sweep() -> list[dict]:
    """Scan local tmux sessions for provider limit errors; park what's found.

    Respects the ``usage_limit:`` config knobs: ``enabled: false`` disables
    new parks entirely; ``exclude_sessions`` names are never auto-parked.
    """
    enabled, excluded = _recovery_config()
    if not enabled:
        return []
    parked = []
    for session in _list_sessions():
        if session in excluded:
            continue
        if is_parked(session):
            continue
        limit = detect_limit(session, 0)
        if limit:
            state = park(session, 0, source="watchdog", limit=limit)
            if state:
                parked.append(state)
    return parked


# =============================================================================
# Resume
# =============================================================================


def _nudge_visible(target: str) -> bool:
    norm = _normalize(_capture(target))
    return "interrupted by a usage limit" in norm or "[Pasted text" in norm


def resume_session(state: dict, force: bool = False) -> bool:
    """Send the resume nudge to a parked session; archive state on success."""
    session = state["session"]
    pane = state.get("pane", 0)
    target = f"{session}.{pane}"

    if not _session_exists(session):
        archive_state(state, "orphaned")
        log_event("park_orphaned", session=session)
        return False

    from . import pane_manager

    attempts = state.get("resume_attempts", 0)
    try:
        pane_manager.send_to_target(target, RESUME_NUDGE, enter=True)
        time.sleep(2.0)
        delivered = _nudge_visible(target)
    except Exception as e:
        log_event("resume_send_error", session=session, error=str(e))
        delivered = False

    if delivered or force:
        state["resumed_at"] = _now().isoformat()
        archive_state(state, "resumed")
        log_event("session_resumed", session=session, attempts=attempts + 1,
                  forced=bool(force and not delivered))
        _notify_resumed(state)
        return True

    state["resume_attempts"] = attempts + 1
    if state["resume_attempts"] >= MAX_RESUME_ATTEMPTS:
        archive_state(state, "resume_failed")
        log_event("resume_failed", session=session, attempts=state["resume_attempts"])
        _send_notification(
            state,
            subject=f"[hermeswire] provider limit: FAILED to resume {session}",
            body=(
                f"Session **{session}** could not be nudged after "
                f"{state['resume_attempts']} attempts — it needs a human look."
            ),
            mark_notified=False,
        )
    else:
        write_park_state(state)
        log_event("resume_retry", session=session, attempts=state["resume_attempts"])
    return False


def resume_due(now: datetime | None = None) -> list[str]:
    """Resume every parked session whose reset (+ grace) has passed."""
    now = now or _now()
    resumed = []
    for state in list_parked():
        session = state["session"]
        if not _session_exists(session):
            archive_state(state, "orphaned")
            log_event("park_orphaned", session=session)
            continue
        if not state.get("notified"):
            _notify_parked(state)
        try:
            due = datetime.fromisoformat(state["resume_at"]) <= now
        except (KeyError, ValueError):
            due = True
        if due and resume_session(state):
            resumed.append(session)
    return resumed


# =============================================================================
# Tick (the stateless watchdog entry point)
# =============================================================================


def tick() -> dict:
    """One watchdog pass: sweep for provider limits, resume what's due.

    Stateless and self-contained — safe to run from launchd every minute.
    A non-blocking lock skips overlapping ticks.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = open(STATE_DIR / ".tick.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return {"skipped": "tick already running"}

    try:
        parked = sweep()
        resumed = resume_due()
        return {
            "parked": [s["session"] for s in parked],
            "resumed": resumed,
            "waiting": [s["session"] for s in list_parked()],
        }
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
