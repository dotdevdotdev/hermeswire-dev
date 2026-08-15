"""Hermes Agent conversation history utilities (#9).

Reads conversation data from Hermes's SQLite session store at
``~/.hermes/state.db`` (``hermes_state.SessionDB`` / the ``hermes sessions``
CLI surface), which replaced Claude Code's ``~/.claude/history.jsonl`` +
``~/.claude/projects/<encoded-cwd>/<id>.jsonl`` tree.

Key semantic differences from the Claude store, all of which fall out of
Hermes sessions being keyed by a plain ``id`` with ``cwd`` as a *data column*:

- There is no ``encode_project_path`` equivalent: a moved directory can never
  orphan a transcript, because the cwd is not part of the storage key.
- There are no ``type:"summary"`` rows: Hermes stores the full message history
  and auto-titles sessions, so "summary" maps to the session ``title`` (or the
  last assistant message).
- Store timestamps are epoch **seconds**; this module converts them to the
  **milliseconds** the historical ``history.jsonl`` shape used, so the
  ``get_history``/``get_session_detail`` dict shapes stay unchanged for the
  ``cmd_history_list``/``routes/history``/``mcp_history`` consumers.
- Resumability is binary: ``get_session(id) is not None``. The old
  ``resumable/orphaned/gone`` trichotomy collapses to "present or not" — a
  Hermes session is either in the store (resumable) or absent (gone); there is
  no "orphaned under a different cwd key" state to detect or repair.

The module lazily imports ``hermes_state.SessionDB`` (a heavy import, and the
AgentWire wheel must not hard-depend on a specific Hermes version). When that
import is unavailable — e.g. the AgentWire interpreter is not the one Hermes is
installed into — the functions degrade to empty results rather than reading a
non-existent ``~/.claude`` tree. Remote (``machine``) listing is not
implemented for the Hermes store; callers pass ``machine="local"`` and any
non-local ``machine`` yields no results.
"""

import re
from dataclasses import dataclass
from pathlib import Path

#: Claude's data directory. Retained only because ``auth_expired`` (#13) still
#: reads ``PROJECTS_DIR``/``encode_project_path``; the history functions below
#: no longer touch the Claude tree.
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

#: Hermes's session store. ``ConversationLocation.expected_dir`` points here:
#: a session's history lives in this one DB, not under a per-cwd directory.
HERMES_DB_PATH = Path.home() / ".hermes" / "state.db"


def encode_project_path(path: str) -> str:
    """Encode a cwd to the ``~/.claude/projects/<dir>`` name Claude Code used.

    Retained for backward compatibility: ``auth_expired`` (#13),
    ``handoff.instructions`` and ``session_cli`` still import it for their own
    file layout. The Hermes-backed history functions above do NOT use it —
    Hermes has no cwd-keyed transcript directory. The rule is unchanged:
    every character outside ASCII ``[A-Za-z0-9]`` becomes ``-``, one for one.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


#: Lazily-opened Hermes ``SessionDB``. ``None`` both before first use and when
#: ``hermes_state`` cannot be imported (see module docstring).
_db_instance = None


def _db():
    """Open the Hermes session store once, or return ``None`` if unavailable.

    ``hermes_state`` is imported lazily (heavy, and the wheel must not depend
    on a specific Hermes version). Callers that get ``None`` degrade to empty
    results. Tests monkeypatch this to inject a fake store.
    """
    global _db_instance
    if _db_instance is None:
        try:
            from hermes_state import DEFAULT_DB_PATH, SessionDB
        except ImportError:
            return None
        _db_instance = SessionDB(DEFAULT_DB_PATH)
    return _db_instance


def resolve_session_id(prefix: str, machine: str = "local") -> str | None:
    """Resolve a session id prefix to the full Hermes session id.

    Hermes ids are opaque strings (not 36-char UUIDs), so there is no UUID
    shape fast-path. Resolution is exact-or-unique-prefix, delegating to
    ``SessionDB.resolve_session_id``. ``None`` for no match or an ambiguous
    prefix. Non-local ``machine`` is unsupported and returns ``None``.
    """
    if machine != "local":
        return None
    db = _db()
    if db is None:
        return None
    return db.resolve_session_id(prefix)


def resumable(session_id: str, machine: str = "local") -> bool:
    """Whether ``hermes --resume <session_id>`` would find the session.

    The one predicate resumability reduces to under Hermes: the session is
    present in the store. ``get_session(id) is not None``.
    """
    if machine != "local":
        return False
    db = _db()
    if db is None:
        return False
    return db.get_session(session_id) is not None


def holds_a_conversation(session_id: str) -> bool:
    """Whether *session_id* holds a real conversation.

    Hermes has no metadata-stub transcripts: a session either exists in the
    store (with its full message history) or it does not. So the answer is
    exactly "present in the store" — the old stub-vs-turns distinction cannot
    arise, and a dead id is simply one ``get_session`` misses.
    """
    return resumable(session_id)


@dataclass(frozen=True)
class ConversationLocation:
    """Where a recorded conversation's history is — present or not (#9).

    Under Hermes there is no per-cwd directory to look in, so the answer is
    binary: the id is in ``~/.hermes/state.db`` (``resumable``) or it is not
    (``gone``). The ``orphaned`` state — intact but keyed under a *different*
    cwd after a directory move — cannot occur, because cwd is a data column
    rather than part of the storage key. ``elsewhere`` is therefore always
    empty and ``status`` never reports ``orphaned``.
    """

    conversation_id: str
    cwd: str
    expected_dir: Path
    found_at: Path | None
    elsewhere: tuple[Path, ...]

    @property
    def status(self) -> str:
        if self.found_at is not None:
            return "resumable"
        return "gone"

    @property
    def resumable(self) -> bool:
        return self.found_at is not None


def locate_conversation(
    conversation_id: str, cwd, projects_dir: Path | None = None
) -> ConversationLocation:
    """Locate *conversation_id* in the Hermes session store.

    ``projects_dir`` is accepted for backward compatibility with the Claude
    store's callers but is ignored: Hermes keeps every session in one DB, keyed
    by id, so a moved directory can never strand a transcript.
    """
    db = _db()
    present = db is not None and db.get_session(conversation_id) is not None
    return ConversationLocation(
        conversation_id=conversation_id,
        cwd=str(cwd),
        expected_dir=HERMES_DB_PATH,
        found_at=HERMES_DB_PATH if present else None,
        elsewhere=(),
    )


def get_history(project_path: str, machine: str = "local", limit: int = 20) -> list[dict]:
    """Get conversation history for a project from the Hermes session store.

    Lists sessions whose ``cwd`` is under (or matches) *project_path*, newest
    activity first, mapped onto the historical dict shape so the CLI/routes/MCP
    consumers stay unchanged:

    ``{sessionId, firstMessage, lastSummary, timestamp, messageCount}``

    where ``timestamp`` is epoch **milliseconds** (the old ``history.jsonl``
    unit) and ``lastSummary`` is the Hermes session ``title`` (there is no
    separate ``summary`` record type).
    """
    if machine != "local":
        return []
    db = _db()
    if db is None:
        return []

    rows = db.list_sessions_rich(
        cwd_prefix=project_path, limit=limit, order_by_last_active=True
    )

    sessions: list[dict] = []
    for row in rows:
        ts = row.get("last_active") or row.get("started_at") or 0
        sessions.append({
            "sessionId": row.get("id"),
            "firstMessage": row.get("preview") or row.get("title") or "",
            "lastSummary": row.get("title"),
            "timestamp": int(ts * 1000),
            "messageCount": row.get("message_count", 0),
        })

    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    return sessions[:limit]


def get_session_detail(session_id: str, machine: str = "local") -> dict | None:
    """Get full details for a specific Hermes session.

    Resolves a unique prefix, then exports the session (row + messages). Maps
    onto the historical shape:

    ``{sessionId, summaries, firstMessage, timestamps: {start, end}, gitBranch, messageCount}``

    where ``summaries`` are the assistant-message contents (no ``summary`` type
    exists) and timestamps are epoch **milliseconds**. ``None`` if absent.
    """
    if machine != "local":
        return None
    db = _db()
    if db is None:
        return None

    resolved = resolve_session_id(session_id, machine)
    if resolved:
        session_id = resolved

    exported = db.export_session(session_id)
    if not exported:
        return None

    messages = exported.get("messages") or []
    summaries = [
        m.get("content") for m in messages
        if m.get("role") == "assistant" and m.get("content")
    ]
    first_message = next(
        (m.get("content") for m in messages
         if m.get("role") == "user" and m.get("content")),
        "",
    )

    start = exported.get("started_at")
    end = exported.get("ended_at") or exported.get("started_at")

    return {
        "sessionId": session_id,
        "summaries": summaries,
        "firstMessage": first_message,
        "timestamps": {
            "start": int(start * 1000) if start else None,
            "end": int(end * 1000) if end else None,
        },
        "gitBranch": exported.get("git_branch"),
        "messageCount": exported.get("message_count", len(messages)),
    }
