"""Interactive-prompt routing — notify a parent session when a child blocks.

Hermes Agent has NO screen-scrapable dialog: its TUI is prompt_toolkit + rich
(no horizontal-rule input box, no ``☐``/``❯`` option menus, no Claude-style
permission / plan-mode / resume dialogs). Prompt routing therefore runs
exclusively on Hermes's *structured* integration surfaces instead of pane
parsing:

  - ``pre_tool_call`` shell hook — fires with ``tool_name`` + ``tool_input`` on
    stdin; the portal POSTs it to :func:`notify_permission_request`.
  - the terminal-tool approval callback (``set_approval_callback``) — carries
    the command + description for dangerous-command approvals.

Both carry structured payloads, so the old glyph-soup ``detect_prompt`` sweep is
gone: there is nothing to scan. :func:`tick` is now a pure marker GC + renotify
pass, and :func:`safe_deliver` / :func:`answer` no longer re-detect anything
from the pane.

Routing never auto-answers and never blocks the prompt itself: no parent
(or an unsafe delivery target) degrades to the existing human-only behavior.

State:
  ~/.agentwire/prompt-router/{session}.{pane}.json   active-prompt markers
  ~/.agentwire/prompt-router-events.jsonl            audit log
"""

import fcntl
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .core import load_session_metadata
from .usage_limit import (
    _atomic_write,
    _capture,
    _normalize,
    _now,
    _session_exists,
    _tmux,
)
from .utils.event_log import append_event

STATE_DIR = Path.home() / ".agentwire" / "prompt-router"
EVENTS_FILE = Path.home() / ".agentwire" / "prompt-router-events.jsonl"

# A prompt the parent never answered re-notifies after this long.
RENOTIFY_TTL = timedelta(minutes=10)
# Markers whose pane vanished are garbage-collected after this long.
MARKER_GC_TTL = timedelta(minutes=30)

# A root session's prompt has nowhere to route, so the owner is the only
# recipient left. Longer than RENOTIFY_TTL on purpose: this is an out-of-band
# email, not a paste into a session that is already watching.
NO_PARENT_ESCALATE_TTL = timedelta(hours=1)

# How long a pane may sit on a detected prompt before `doctor` calls it stuck.
# Comfortably longer than RENOTIFY_TTL: one re-notification going unanswered
# is a busy parent, a second is a parent that isn't coming.
STUCK_PROMPT_AFTER = timedelta(minutes=25)

# Every routed message starts with this; the detector treats its presence on
# a screen as poison so a delivered notification can never be re-detected.
MESSAGE_PREFIX = "[PROMPT from "

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m|\x1b\].*?\x07")

# AskUserQuestion UI blocks (moved from server.py — single source of truth).
# RETAINED for server.py's web-dashboard question detection, which still keys
# on these Claude-era glyphs. The pane-sweep detector that shared them is gone;
# see the module docstring. (Server migration is a separate issue.)
# Format: ☐ Header\n\nQuestion?\n\n❯ 1. Label\n     Description\n  2. Label...
# Multi-tab format: ←  ☐ Tab1  ☐ Tab2  ✔ Submit  →\n\nQuestion?...
ASK_PATTERN = re.compile(
    r"☐\s+(\S+)"  # ☐ followed by first word only (active tab name)
    r".*?\n\s*\n"  # Rest of header line + blank line
    r"((?:.+\n)+?)"  # Question text (one or more lines, non-greedy)
    r"\s*\n"  # Blank line before options
    r"((?:[❯\s]+\d+\.\s+.+\n(?:\s{3,}.+\n)?)+)",  # Options block
    re.MULTILINE | re.DOTALL,
)

# Simple format without ☐ header (e.g. "Ready to submit?\n\n❯ 1. Submit")
ASK_PATTERN_SIMPLE = re.compile(
    r"\n([^\n☐❯]+\?)\s*\n"  # Question ending with ? (no ☐ or ❯)
    r"\s*\n"  # Blank line
    r"((?:[❯\s]+\d+\.\s+.+\n(?:\s{3,}.+\n)?)+)",  # Options block
    re.MULTILINE,
)


@dataclass
class PromptInfo:
    kind: str  # "permission" (approval) | "question" (clarify)
    question: str
    options: list[dict] = field(default_factory=list)  # {number,label,description}
    summary: str = ""  # one-line context (tool/command for permissions)

    def content_hash(self) -> str:
        labels = " ".join(o.get("label", "") for o in self.options)
        raw = _normalize(f"{self.kind} {self.question} {labels}")
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_ask_options(options_block: str) -> list[dict]:
    """Parse numbered options from a dialog's options block."""
    options = []
    current_option = None

    for line in options_block.split("\n"):
        line = ANSI_PATTERN.sub("", line)
        option_match = re.match(r"[❯\s]*(\d+)\.\s+(.+)", line)
        if option_match:
            if current_option:
                options.append(current_option)
            current_option = {
                "number": int(option_match.group(1)),
                "label": option_match.group(2).strip(),
                "description": "",
            }
        elif current_option and line.strip():
            current_option["description"] = line.strip()

    if current_option:
        options.append(current_option)

    return options


# =============================================================================
# Parent resolution
# =============================================================================

# pane_current_command values that indicate an agent runs in a pane.
# Legacy Claude panes report the node binary or a bare version string (e.g.
# "2.1.170"). Hermes panes report `hermes` / `uv` / `python3*` — but `python3*`
# is ALSO what AgentWire's own daemons (portal/tts/scheduler) report, so a bare
# command match is NOT enough for those; :func:`is_agent_pane` disambiguates via
# the pane's process cmdline.
_AGENT_COMMAND_RE = re.compile(
    r"^(node|claude|hermes|uv|\d+\.\d+\.\d+\S*|python3?(?:\.\d+)*)$"
)
# Commands that unambiguously name an agent binary (legacy Claude or the hermes
# console script) — no cmdline disambiguation needed.
_UNAMBIGUOUS_AGENT_RE = re.compile(r"^(node|claude|hermes|\d+\.\d+\.\d+\S*)$")


def _read_creator(session: str) -> "str | None":
    """The session recorded as creator at `agentwire new` time, if any."""
    creator = load_session_metadata(session).get("created_by")
    return creator if isinstance(creator, str) and creator else None


def pane_command(session: str, pane_index: int) -> str:
    """The pane's current command ('' on any error)."""
    try:
        result = _tmux([
            "display", "-t", f"{session}.{pane_index}",
            "-p", "#{pane_current_command}",
        ])
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _pane_pid(session: str, pane_index: int) -> str:
    """The pane's foreground-process PID ('' on any error)."""
    try:
        result = _tmux([
            "display", "-t", f"{session}.{pane_index}", "-p", "#{pane_pid}",
        ])
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _pane_cmdline(session: str, pane_index: int) -> str:
    """The pane's foreground-process command line ('' on any error)."""
    pid = _pane_pid(session, pane_index)
    if not pid or not pid.isdigit():
        return ""
    try:
        out = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout.strip()


def _pane_runs_hermes(session: str, pane_index: int) -> bool:
    """True if the pane's foreground process is a real Hermes invocation.

    ``pane_current_command`` for a Hermes REPL is ``python3*``/``uv`` — the same
    as AgentWire's own daemons (portal/tts/scheduler), so the command name alone
    cannot tell them apart. The process argv is the only reliable discriminator:
    a Hermes REPL's argv mentions ``hermes`` (console script or ``python -m
    hermes``), while a daemon's mentions ``agentwire``.
    """
    cmd = _pane_cmdline(session, pane_index).lower()
    if not cmd:
        return False
    return "hermes" in cmd and "agentwire" not in cmd


def is_agent_pane(session: str, pane_index: int) -> bool:
    """Is an interactive agent (Hermes REPL or legacy Claude) in this pane?

    Daemon panes (portal/tts/scheduler run ``python3*``) must resolve to False —
    pasting into them would execute message text as input. The command name is
    trusted only when it unambiguously names an agent binary; ``uv``/``python3*``
    are disambiguated by inspecting the pane's process cmdline.
    """
    command = pane_command(session, pane_index).strip()
    if not command or not _AGENT_COMMAND_RE.match(command):
        return False  # shell/editor/etc.
    if _UNAMBIGUOUS_AGENT_RE.match(command):
        return True
    return _pane_runs_hermes(session, pane_index)


def resolve_parent(
    session: str, pane_index: int, project_path: "str | None" = None
) -> "tuple[str, int] | None":
    """The (session, pane) that should be told about this pane's prompt.

    Precedence:
      1. Worker pane (index > 0) -> pane 0 of the same session.
      2. Creator recorded at `agentwire new` time (session metadata).
      3. `.agentwire.yml` `parent:` field (project_path's config).
      4. None -> human-only behavior, unchanged.

    Depth-1 and local-machine only. Never returns the source pane itself or
    a dead session. (Whether the target pane is safe to paste into is the
    delivery layer's job — see safe_deliver.)
    """
    if pane_index > 0:
        return (session, 0)

    bare = session.split("@")[0]
    creator = _read_creator(bare)
    if creator and creator != bare and _session_exists(creator):
        return (creator, 0)

    parent = _parent_from_config(project_path)
    if parent and parent != bare and _session_exists(parent):
        return (parent, 0)

    return None


def _parent_from_config(project_path: "str | None") -> "str | None":
    try:
        from .project_config import get_parent_from_config

        return get_parent_from_config(Path(project_path) if project_path else None)
    except Exception:
        return None


# =============================================================================
# Delivery
# =============================================================================


def screen_shows_live_menu(visible: str) -> bool:
    """True if a live select-menu/dialog appears to be on screen.

    Hermes has no screen-scrapable menu: dangerous-command approvals and
    ``clarify`` prompts are callback- or stdin-driven, not TUI widgets, so there
    is nothing to detect from a ``capture-pane``. Kept for API stability —
    callers that used this as a pre-paste safety gate now always proceed.
    """
    return False


# The Hermes REPL prompt glyph (prompt_toolkit, ``hermes chat --cli``). The
# prompt is a single line: the glyph (``❯``) optionally preceded by a state icon
# (``⚕ `` while the agent runs, ``⚠ `` while an approval is pending, a spinner
# while a command runs) and followed by the in-progress draft text.
PROMPT_GLYPH = "❯"


def input_box_content(visible: str) -> "str | None":
    """The text in a Hermes REPL's prompt line, or None if no prompt renders.

    Hermes has no horizontal-rule input box: the prompt_toolkit line is the last
    line containing the glyph (it sits at the bottom, above the status bar), so
    arrows in scrollback never claim it. Returns ``""`` for an idle empty prompt
    (including the ``⚕ ❯`` agent-running state), the draft otherwise, and None
    when no prompt line is visible (busy render / non-REPL pane) — the
    conservative "defer" signal.
    """
    clean = ANSI_PATTERN.sub("", visible)
    for line in reversed(clean.split("\n")):
        s = line.strip()
        idx = s.find(PROMPT_GLYPH)
        if idx == -1:
            continue
        return s[idx + len(PROMPT_GLYPH):].strip()
    return None


def input_box_content_sgr(visible: str) -> "str | None":
    """SGR-aware ``input_box_content``.

    Hermes prompt_toolkit renders no dim ghost/autosuggest text in the prompt
    line (autosuggestion is off by default), so this is the plain parse.
    """
    return input_box_content(visible)


def capture(target_session: str, target_pane: int = 0, escapes: bool = False) -> str:
    """Capture the live screen text of a pane (``escapes=True`` keeps SGR)."""
    return _capture(f"{target_session}.{target_pane}", escapes=escapes)


def prompt_is_empty(target_session: str, target_pane: int = 0) -> bool:
    """True iff the target's prompt line holds no uncommitted text.

    The polite-messaging building block: detects whether a human is mid-typing.
    Conservative by design — any non-empty draft and any screen we can't parse
    as a clean prompt returns False (defer). A delayed message is fine; a
    clobbered human draft is not.
    """
    content = input_box_content_sgr(_capture(f"{target_session}.{target_pane}"))
    return content == ""


def is_queued_placeholder(content: str) -> bool:
    """True if *content* is a busy-state placeholder rather than a draft.

    Hermes has no queued-message placeholder (that was Claude Code's "Press up
    to edit queued messages"), so there is never one. Kept for API stability —
    the inbox drain calls it to decide the dead-letter penalty.
    """
    return False


def safe_deliver(target_session: str, target_pane: int, text: str) -> "tuple[bool, str]":
    """Deliver *text* to the target pane, refusing unsafe targets.

    Refusals (returned as (False, reason), retried by the next tick):
      target_gone       session no longer exists
      target_not_agent  pane runs a shell/daemon — pasted text could EXECUTE

    There is no parked / live-dialog refusal anymore: Hermes has no
    screen-scrapable menu to answer, and usage-limit parking is usage_limit's
    concern (#8). Delivery itself is ``session_ready.send_verified``, keyed on
    the FULL whitespace-normalized message (#667) so a silent tmux paste failure
    reports as not-delivered instead of being assumed sent.
    """
    if not _session_exists(target_session):
        return False, "target_gone"
    if not is_agent_pane(target_session, target_pane):
        return False, "target_not_agent"

    from .session_ready import send_verified

    ok = send_verified(target_session, text, pane_index=target_pane)
    return (ok, "delivered" if ok else "delivery_unverified")


# =============================================================================
# Markers (presence-based dedupe) + events
# =============================================================================


def _log_event(event: str, **fields) -> None:
    record = {"ts": _now().isoformat(), "event": event, **fields}
    append_event(EVENTS_FILE, record)


def marker_path(session: str, pane_index: int) -> Path:
    # Worktree session names contain "/" and nest a directory level, same as
    # usage_limit.state_path. The bash idle-handler guard tests the literal
    # "$HOME/.agentwire/prompt-router/${session}.${pane}.json" string — keep
    # these in lockstep.
    return STATE_DIR / f"{session}.{pane_index}.json"


def read_marker(session: str, pane_index: int) -> "dict | None":
    try:
        return json.loads(marker_path(session, pane_index).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_marker(session: str, pane_index: int, **fields) -> dict:
    marker = {"session": session, "pane": pane_index, **fields}
    _atomic_write(marker_path(session, pane_index), marker)
    return marker


def clear_marker(session: str, pane_index: int) -> None:
    try:
        marker_path(session, pane_index).unlink(missing_ok=True)
    except OSError:
        pass


def _marker_age(marker: dict, field_name: str = "detected_at") -> "timedelta | None":
    try:
        return _now() - datetime.fromisoformat(marker[field_name])
    except (KeyError, TypeError, ValueError):
        return None


def list_markers() -> list[dict]:
    if not STATE_DIR.exists():
        return []
    markers = []
    for path in sorted(STATE_DIR.rglob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("session") is not None:
            markers.append(data)
    return markers


# =============================================================================
# Routing
# =============================================================================


def build_message(session: str, pane_index: int, info: PromptInfo) -> str:
    """The notification a parent receives. Deliberately paraphrased: no `❯`,
    no menu-style option block, no dialog footer text — a delivered message
    must never look like a live dialog to the sweep (see MESSAGE_PREFIX
    poison + screen_shows_live_menu)."""
    labels = ", ".join(
        f"{o['number']}={o['label']}" for o in info.options if o.get("label")
    )
    deadline = (
        "~5 minutes (portal permission timeout)"
        if info.kind == "permission"
        else "none — blocks until answered"
    )
    summary = f" Context: {info.summary}" if info.summary else ""
    return (
        f"{MESSAGE_PREFIX}{session} pane {pane_index}] kind={info.kind} — "
        f"a session you are responsible for is blocked on an interactive prompt. "
        f"Question: {info.question}{summary} "
        f"Option keys: {labels}. Deadline: {deadline}. "
        f"Inspect FIRST: agentwire output -s '{session}' (MCP: pane_output/session_output). "
        f"Answer ONLY via: agentwire prompts answer -s '{session}' --pane {pane_index} "
        f"--expect {info.content_hash()} <key> — it verifies the same prompt is still "
        f"live before sending the key. Do not blanket-approve; if unsure, do nothing "
        f"(the human was also notified)."
    )


def _alert_no_parent(
    session: str, pane_index: int, info: PromptInfo, prior: "dict | None"
) -> "str | None":
    """Mirror a no-parent prompt to subscribed sessions (#982). Escalation kind.

    Earns the interrupt on both halves of the test: nothing but a human can
    answer a prompt with no parent to route to, and the session is stalled —
    burning wall-clock, and in the plan/permission cases holding a tool call
    open — for as long as it waits.

    **Its own stamp, and this is the whole bug.** The first version rode
    ``escalated_at``, described as "inheriting" the email's throttle. It does
    not inherit it, because that gate never closes on a machine without
    ``RESEND_API_KEY``: ``send_email`` RAISES ``EmailConfigError`` rather than
    returning a failed result, and :func:`_escalate_no_parent`'s handler returns
    the previous (absent) stamp. So the marker was rewritten every 60s sweep
    with ``escalated_at=None`` and the alert re-fired every tick — measured at
    5 escalations for 5 sweeps of ONE prompt, which over a 12h lease is ~720.
    That is precisely the failure mode this tier cannot survive: the
    over-production does not merely annoy, it retires the tier.

    ``alerted_at`` therefore stamps on successful ENQUEUE, which is a local
    write and cannot fail for the reason the email does. Same marker, same
    ``NO_PARENT_ESCALATE_TTL`` window, keyed on the same prompt hash (the
    caller only passes a *prior* whose hash matches), so a redraw is suppressed
    while a genuinely different question still alerts.

    Sent before the email for the same reason it needed its own stamp: on the
    common keyless machine the email is not a channel at all.
    """
    previous = (prior or {}).get("alerted_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < NO_PARENT_ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass
    try:
        from . import fleet_alerts

        waiting = _marker_age(prior, "detected_at") if prior else None
        waited = (
            f" It has been waiting {int(waiting.total_seconds() // 60)} minutes."
            if waiting else ""
        )
        reached = fleet_alerts.emit_for(
            "blocked_pane_no_parent",
            f"{session} (pane {pane_index}) is blocked on a {info.kind} prompt "
            f"and is a root session — there is no parent to route it to, so "
            f"nothing will answer it automatically.{waited} Question: "
            f"{info.question} Answer with: agentwire prompts answer -s "
            f"'{session}' --pane {pane_index} --expect {info.content_hash()} <key>",
        )
    except Exception as exc:  # best-effort; never break the sweep
        _log_event(
            "no_parent_alert_failed", session=session, pane=pane_index, error=str(exc)
        )
        return previous
    if not reached:
        return previous
    _log_event("no_parent_alerted", session=session, pane=pane_index, to=reached)
    return _now().isoformat()


def _escalate_no_parent(
    session: str, pane_index: int, info: PromptInfo, prior: "dict | None"
) -> "str | None":
    """Email the owner about a ROOT session blocked with nowhere to route (#905).

    A root session has no parent by design, so ``status=no_parent`` was a
    terminal state: the marker sat there forever and no surface said anything.
    That is fine for a prompt a human is sitting in front of and wrong for an
    unattended one — a root orchestrator blocked on a product question is
    stalled until somebody happens to look at the pane.

    Follows the dead-letter escalation precedent rather than inventing a
    channel: shared Resend wiring, best-effort, never raises. Rate-limited by
    ``escalated_at`` on the marker — the first sighting emails, then at most
    once per :data:`NO_PARENT_ESCALATE_TTL` while the SAME prompt stays up
    (the sweep re-routes a no-parent prompt every 60s, so an unthrottled
    escalation would be 60 emails an hour). Returns the timestamp to record.
    """
    previous = (prior or {}).get("escalated_at")
    if previous:
        try:
            if _now() - datetime.fromisoformat(previous) < NO_PARENT_ESCALATE_TTL:
                return previous
        except (TypeError, ValueError):
            pass

    waiting = _marker_age(prior, "detected_at") if prior else None
    try:
        import socket

        from .channels.email import send_email

        options = ", ".join(
            f"{o['number']}={o['label']}" for o in info.options if o.get("label")
        )
        summary = f"\n**Context:** {info.summary}" if info.summary else ""
        waited = (
            f" It has been waiting {int(waiting.total_seconds() // 60)} minutes."
            if waiting else ""
        )
        body = (
            f"`{session}` (pane {pane_index}) on `{socket.gethostname()}` is blocked "
            f"on a **{info.kind}** prompt and has no parent session to route it to, "
            f"so nothing can answer it automatically.{waited}\n"
            f"\n**Question:** {info.question}{summary}\n"
            f"\n**Options:** {options}\n"
            f"\nInspect:\n```\nagentwire output -s '{session}'\n```\n"
            f"\nAnswer (never raw send-keys — it verifies the same prompt is still "
            f"live first):\n```\nagentwire prompts answer -s '{session}' "
            f"--pane {pane_index} --expect {info.content_hash()} <key>\n```\n"
        )
        result = send_email(
            subject=f"[agentwire] {session} is blocked on a {info.kind} prompt "
                    f"with no parent",
            body=body,
        )
        ok = bool(getattr(result, "success", False))
    except Exception as exc:  # escalation is best-effort; never break the sweep
        _log_event(
            "no_parent_escalate_failed",
            session=session, pane=pane_index, error=str(exc),
        )
        return previous
    _log_event(
        "no_parent_escalated",
        session=session, pane=pane_index, kind=info.kind, ok=ok,
    )
    return _now().isoformat()


def route_prompt(
    session: str,
    pane_index: int,
    info: PromptInfo,
    source: str = "sweep",
    project_path: "str | None" = None,
) -> "str | None":
    """Resolve the parent and deliver the notification. Never raises.

    Writes a marker either way: delivered markers dedupe future sweeps,
    deferred/no-parent markers make retries cheap and keep the idle-handler
    reap guard active while the pane is blocked. Returns the parent session
    name when delivery succeeded, else None.
    """
    try:
        content_hash = info.content_hash()
        parent = resolve_parent(session, pane_index, project_path)
        if parent is None:
            # Same prompt as last sweep -> keep the ORIGINAL detected_at. A
            # no-parent marker is rewritten every tick (nothing sets
            # notified_at, so the sweep never short-circuits), and refreshing
            # the timestamp each pass would make a pane blocked for four hours
            # read as four seconds old to anything measuring the wait.
            prior = read_marker(session, pane_index)
            if not prior or prior.get("hash") != content_hash:
                prior = None
            escalated_at = _escalate_no_parent(session, pane_index, info, prior)
            # Two channels, two stamps, deliberately (#982). They cannot share
            # one: the email's gate only closes on a SUCCESSFUL send, and on a
            # machine with no RESEND_API_KEY there is never one — which left the
            # fleet alert re-firing every 60s sweep when it rode that stamp.
            alerted_at = _alert_no_parent(session, pane_index, info, prior)
            write_marker(
                session, pane_index,
                kind=info.kind, question=info.question,
                hash=content_hash, source=source,
                parent=None, status="no_parent",
                options=info.options, summary=info.summary,
                detected_at=(prior or {}).get("detected_at") or _now().isoformat(),
                notified_at=None,
                escalated_at=escalated_at,
                alerted_at=alerted_at,
            )
            _log_event("no_parent", session=session, pane=pane_index, kind=info.kind)
            return None

        target_session, target_pane = parent
        delivered, reason = safe_deliver(
            target_session, target_pane, build_message(session, pane_index, info)
        )
        write_marker(
            session, pane_index,
            kind=info.kind, question=info.question,
            hash=content_hash, source=source,
            parent=target_session, status=reason,
            options=info.options, summary=info.summary,
            detected_at=_now().isoformat(),
            notified_at=_now().isoformat() if delivered else None,
        )
        _log_event(
            "prompt_routed" if delivered else "route_deferred",
            session=session, pane=pane_index, kind=info.kind,
            parent=target_session, status=reason,
        )
        return target_session if delivered else None
    except Exception as exc:  # routing must never break a caller
        _log_event("route_failed", session=session, pane=pane_index, error=str(exc))
        return None


def notify_permission_request(session: str, pane_index: int, data: dict) -> "str | None":
    """Hook-path entry: the portal received a Hermes ``pre_tool_call`` POST.

    Builds the PromptInfo from the structured hook payload (no pane parsing
    needed) and routes it. ``data`` is either the ``pre_tool_call`` hook JSON
    (``tool_name`` + ``tool_input``) or the terminal-tool approval callback's
    args (``command`` + ``description``). Sync + best-effort; the server calls
    this in an executor.
    """
    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input") or {}

    # Hermes's `clarify` tool → a question the parent can answer. The payload
    # carries the question + choices (structured, unlike Claude's glyph menu).
    if tool_name == "clarify":
        question = str(tool_input.get("question") or "clarify question")
        choices = tool_input.get("choices") or []
        options = [
            {"number": i + 1, "label": str(c), "description": ""}
            for i, c in enumerate(choices[:4])
            if str(c) != ""
        ]
        info = PromptInfo(kind="question", question=question, options=options)
        return route_prompt(session, pane_index, info, source="hook")

    # Dangerous-command approval (approval callback / pre_tool_call). The
    # approval gate returns once/session/always/deny — mirror that vocabulary.
    command = data.get("command") or tool_input.get("command") or ""
    description = str(data.get("description") or "")
    if command:
        summary = f"run: {command}"[:300]
    elif description:
        summary = description[:300]
    else:
        summary = f"use {tool_name}"
    info = PromptInfo(
        kind="permission",
        question=f"Hermes wants to {summary}",
        options=[
            {"number": 1, "label": "once", "description": ""},
            {"number": 2, "label": "session", "description": ""},
            {"number": 3, "label": "always", "description": ""},
            {"number": 4, "label": "deny", "description": ""},
        ],
        summary=summary,
    )
    return route_prompt(session, pane_index, info, source="hook")


# =============================================================================
# Guarded answer (compare-and-send)
# =============================================================================


def answer(
    session: str, pane_index: int, expect_hash: str, keys: list[str]
) -> "tuple[bool, str]":
    """Send *keys* to the pane only if the expected prompt is still recorded.

    This is the race guard: a human may have answered via the portal (or the
    prompt may have expired) between notification and the parent's answer —
    a stray keystroke would land in the child's prompt line. First answer wins;
    the loser no-ops here.

    The hash comes from the marker (written by ``route_prompt`` from the hook /
    callback payload) — Hermes has no screen to re-detect it from, so there is
    no capture here. A missing marker, or a hash mismatch, means the prompt is
    no longer the one we were told about.
    """
    marker = read_marker(session, pane_index)
    if marker is None:
        return False, "no live prompt recorded (already answered or gone)"
    if marker.get("hash") != expect_hash:
        return False, (
            f"a DIFFERENT prompt is recorded (hash={marker.get('hash')}) — "
            f"inspect before answering"
        )
    for key in keys:
        _tmux(["send-keys", "-t", f"{session}.{pane_index}", key])
    clear_marker(session, pane_index)
    _log_event(
        "prompt_answered", session=session, pane=pane_index,
        kind=marker.get("kind"), keys=keys,
    )
    return True, f"sent {' '.join(keys)} to {session}.{pane_index}"


# =============================================================================
# Sweep + tick
# =============================================================================


def _router_config() -> "tuple[bool, set[str]]":
    """(enabled, excluded session names) from config.yaml."""
    try:
        from .config import get_config

        cfg = get_config().prompt_router
        return bool(cfg.enabled), set(cfg.exclude_sessions)
    except Exception:
        return True, set()


@dataclass
class PaneRef:
    """One tmux pane, as the sweep and the doctor check both see it."""
    session: str
    pane: int
    command: str
    path: str

    @property
    def is_agent(self) -> bool:
        return is_agent_pane(self.session, self.pane)


def list_panes() -> "list[PaneRef] | None":
    """Every pane on the tmux server, or None if tmux couldn't be asked.

    ``list-panes -a`` deliberately, NOT ``-t <session>``: ``-a`` is server-wide
    and needs no per-session target, so it sidesteps the trap that bit the
    fleet's ad-hoc health checks — a bare ``list-panes -t <session>`` scopes to
    the ACTIVE WINDOW, making the first row the wrong pane whenever the agent
    isn't in it (``-s`` is what makes a targeted call session-wide). Pane
    indices are read from tmux, never assumed: base-index ships as 0 since
    #903, but windows created before that kept 1, so both are live at once.

    None (not ``[]``) when tmux is unreachable — "couldn't look" and "looked,
    found nothing" must not collapse, or a dead tmux server would read as a
    healthy fleet.
    """
    try:
        result = _tmux(
            ["list-panes", "-a", "-F",
             "#{session_name}\t#{pane_index}\t#{pane_current_command}\t#{pane_current_path}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None

    panes = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            pane_index = int(parts[1])
        except ValueError:
            continue
        panes.append(PaneRef(parts[0], pane_index, parts[2], parts[3]))
    return panes


def blocked_panes() -> "list[dict] | None":
    """Panes sitting on an unanswered prompt, read-only (marker-based).

    Hermes has no screen-scrapable dialog, so the only record of a blocked pane
    is the marker ``route_prompt`` wrote when the hook / approval callback
    fired. This reads those markers to say how long each has been waiting and
    whether anyone was told. ``status``:

    - ``no_parent`` — routed nowhere; a root session, owner emailed.
    - ``waiting`` — a parent was notified and hasn't answered yet.
    - ``deferred`` — the parent pane wasn't safe to paste into.

    ``stuck`` marks the ones worth acting on: waiting longer than
    :data:`STUCK_PROMPT_AFTER`. Never routes, never delivers, never writes a
    marker.
    """
    panes = list_panes()
    if panes is None:
        return None
    _, excluded = _router_config()

    blocked = []
    for marker in list_markers():
        session = marker.get("session")
        pane = marker.get("pane")
        if session is None or pane is None:
            continue
        age = _marker_age(marker)
        if marker.get("status") == "no_parent":
            status = "no_parent"
        elif marker.get("notified_at"):
            status = "waiting"
        else:
            status = "deferred"
        blocked.append({
            "session": session,
            "pane": pane,
            "kind": marker.get("kind"),
            "error": None,
            "question": marker.get("question", ""),
            "summary": marker.get("summary", ""),
            "status": status,
            "parent": marker.get("parent"),
            "excluded": session in excluded,
            "waiting_minutes": int(age.total_seconds() // 60) if age else None,
            "stuck": age is not None and age > STUCK_PROMPT_AFTER,
        })
    return blocked


def _info_from_marker(marker: dict) -> "PromptInfo | None":
    """Rebuild the PromptInfo a marker records (for renotification)."""
    kind = marker.get("kind")
    if not kind:
        return None
    options = []
    for o in marker.get("options") or []:
        if isinstance(o, dict):
            options.append({
                "number": o.get("number"),
                "label": str(o.get("label", "")),
                "description": str(o.get("description", "")),
            })
    return PromptInfo(
        kind=kind,
        question=marker.get("question", ""),
        options=options,
        summary=marker.get("summary", ""),
    )


def sweep() -> dict:
    """Marker GC + renotify pass over routed prompts (no pane scanning).

    Hermes prompts arrive via the hook / approval-callback path only, so the
    sweep no longer reads panes — the marker IS the record. Lifecycle per
    marker:

      pane still alive, freshly notified -> "active" (silent until RENOTIFY_TTL)
      pane still alive, notified past TTL -> re-route (renotify)
      pane still alive, never notified    -> retry route (deferred)
      pane gone                           -> GC after MARKER_GC_TTL
    """
    enabled, _ = _router_config()
    if not enabled:
        return {"routed": [], "deferred": [], "active": [], "failed": []}
    panes = list_panes()
    if panes is None:
        return {"routed": [], "deferred": [], "active": [], "failed": []}

    seen_panes = {(p.session, p.pane) for p in panes}
    buckets = {"routed": [], "deferred": [], "active": [], "failed": []}

    for marker in list_markers():
        session = marker.get("session")
        pane = marker.get("pane")
        if session is None or pane is None:
            continue
        key = (session, pane)
        if key not in seen_panes:
            age = _marker_age(marker)
            if age is None or age > MARKER_GC_TTL:
                clear_marker(session, pane)
            continue

        info = _info_from_marker(marker)
        if info is None:
            clear_marker(session, pane)
            continue

        if marker.get("notified_at"):
            age = _marker_age(marker, "notified_at")
            if age is not None and age < RENOTIFY_TTL:
                buckets["active"].append(
                    {"session": session, "pane": pane, "kind": info.kind})
                continue

        # Deferred (never notified) or TTL-expired -> re-route.
        try:
            parent = route_prompt(session, pane, info)
        except Exception as exc:  # one marker must never cost the fleet
            _log_event(
                "detect_failed",
                session=session, pane=pane,
                error=f"{type(exc).__name__}: {exc}",
            )
            buckets["failed"].append({
                "session": session, "pane": pane,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        entry = {"session": session, "pane": pane, "kind": info.kind,
                 "parent": parent}
        buckets["routed" if parent else "deferred"].append(entry)

    return {"routed": buckets["routed"], "deferred": buckets["deferred"],
            "active": buckets["active"], "failed": buckets["failed"]}


def tick() -> dict:
    """One watchdog pass; rides `agentwire limits tick` (after the
    usage-limit sweep, so its dialog is parked before we ever look)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = STATE_DIR / ".tick.lock"
    with open(lock_file, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"skipped": "tick already running"}
        return sweep()
