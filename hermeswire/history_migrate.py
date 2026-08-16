"""Deprecated — Hermes sessions cannot be orphaned by a directory move (#9).

The old Claude Code keyed a conversation's transcript by the directory it ran in
(``~/.claude/projects/<encoded-cwd>/<id>.jsonl``); moving that directory
orphaned the transcript, and this module re-keyed it onto the new path.

Hermes stores sessions in SQLite at ``~/.hermes/state.db``, keyed by a plain
``id`` with ``cwd`` as a *data column*. The cwd is not part of the storage
key, so a ``mv``/worktree move can never orphan a session — there is nothing
to re-key. Every operation here is therefore a no-op that says so. The module
is retained so ``hermeswire history migrate`` and ``known_sessions`` keep their
surface while reporting the new reality.
"""

from __future__ import annotations

# Outcome constants kept for importers that may still reference them. Only the
# new OBSOLETE status is ever produced now; the rest are historical.
ALIGNED = "aligned"
READY = "ready"
MIGRATED = "migrated"
SOURCE_ABSENT = "source_absent"
TARGET_EXISTS = "target_exists"
UNDETERMINED = "undetermined"
ERROR = "error"
OBSOLETE = "obsolete"

FAILURE_STATUSES = {TARGET_EXISTS, ERROR}

_OBSOLETE_DETAIL = (
    "Hermes sessions are keyed by id in ~/.hermes/state.db (cwd is a data "
    "column, not part of the storage key); no cwd re-keying exists"
)


def resumable(conversation_id: str, cwd=None) -> bool:
    """Whether the session is present in the Hermes store (present == resumable).

    *cwd* is accepted for signature compatibility but ignored: under Hermes,
    resumability depends only on the id, never on the directory.
    """
    from .history import resumable as _resumable

    return _resumable(conversation_id)


def plan(old_cwd=None, new_cwd=None) -> dict:
    """Migration plan: always obsolete, because there is nothing to move."""
    return {
        "old_cwd": str(old_cwd) if old_cwd else None,
        "new_cwd": str(new_cwd) if new_cwd else None,
        "status": OBSOLETE,
        "detail": _OBSOLETE_DETAIL,
    }


def apply(old_cwd=None, new_cwd=None, *, prune_source: bool = False) -> dict:
    """No-op: nothing is migrated, and nothing on disk is touched."""
    return plan(old_cwd, new_cwd)


def resolve_session(session_name: str) -> dict:
    """Reconciliation is obsolete: a session's cwd is not part of its key."""
    return {"session": session_name, "status": OBSOLETE, "detail": _OBSOLETE_DETAIL}


def scan() -> list:
    """Nothing can be orphaned, so there is nothing to report."""
    return []


def known_sessions() -> list[str]:
    """Session names that have a metadata record (delegates to ``core``)."""
    from .core import recorded_sessions

    return recorded_sessions()
