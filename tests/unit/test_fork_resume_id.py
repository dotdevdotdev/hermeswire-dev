"""Tests for ``_resolve_fork_resume_id`` — fork's session-id resolver (#36).

``cmd_fork`` used to read the source session id from ``~/.claude/history.jsonl``
and ``~/.claude/projects/<encoded>/`` — the OLD Claude Code store. Under Hermes
those reads miss, so a fork started FRESH instead of resuming the source. The
resolver must consult Hermes sources of truth first (the session record's
``conversation_ids`` chain, then ``~/.hermes/state.db``), and touch ``~/.claude``
only as a legacy fallback for pre-conversion sessions.
"""

import types
from pathlib import Path

import pytest

from hermeswire import session_cli

FORK_PATH = Path("/tmp/fork")


@pytest.fixture
def hermes_db(monkeypatch):
    """A fake Hermes session store: a mutable set of 'present' session ids.

    ``history.locate_conversation`` reads ``_db().get_session(id)``; a present
    id is 'in the store' (resumable), an absent id is 'gone'.
    """
    present = set()

    class FakeDB:
        @staticmethod
        def get_session(sid):
            return {"id": sid} if sid in present else None

    monkeypatch.setattr("hermeswire.history._db", lambda: FakeDB())
    return present


def _patch(monkeypatch, *, ids=(), capture=None, legacy=None):
    """Stub the resolver's three inputs; return a call recorder.

    ``load_session_metadata`` returns a record holding ``conversation_ids``.
    ``_capture_live_source_id`` (the state.db poll) and ``_legacy_claude_resume_id``
    (the ~/.claude read) are stubbed so the test can assert which were hit.
    """
    calls = types.SimpleNamespace(capture_calls=[], legacy_calls=[])

    monkeypatch.setattr(
        session_cli,
        "load_session_metadata",
        lambda name: {"conversation_ids": list(ids)},
    )

    def fake_capture(path):
        calls.capture_calls.append(path)
        return capture

    def fake_legacy(source_session, fork_path):
        calls.legacy_calls.append((source_session, fork_path))
        return legacy

    monkeypatch.setattr(session_cli, "_capture_live_source_id", fake_capture)
    monkeypatch.setattr(session_cli, "_legacy_claude_resume_id", fake_legacy)
    return calls


class TestResolveForkResumeId:
    def test_reads_conversation_ids_before_touching_claude(self, hermes_db, monkeypatch):
        """A resumable recorded id wins; neither the poll nor ~/.claude runs."""
        hermes_db.add("hermes-123")
        calls = _patch(monkeypatch, ids=["hermes-123"])

        assert session_cli._resolve_fork_resume_id("source", FORK_PATH) == "hermes-123"
        assert calls.capture_calls == []   # no state.db poll
        assert calls.legacy_calls == []    # ~/.claude never touched

    def test_falls_back_down_the_chain_for_a_never_prompted_launch(
        self, hermes_db, monkeypatch
    ):
        """The newest id has no state.db row until its first turn, so the id it
        resumed FROM still holds the conversation and must win (#36)."""
        hermes_db.add("older")
        calls = _patch(monkeypatch, ids=["older", "never-prompted"])

        assert session_cli._resolve_fork_resume_id("source", FORK_PATH) == "older"
        assert calls.legacy_calls == []

    def test_empty_chain_falls_to_state_db_capture(self, hermes_db, monkeypatch):
        calls = _patch(monkeypatch, ids=[], capture="captured-id")

        assert session_cli._resolve_fork_resume_id("source", FORK_PATH) == "captured-id"
        assert calls.capture_calls == [FORK_PATH]
        assert calls.legacy_calls == []

    def test_capture_miss_falls_to_legacy_claude(self, hermes_db, monkeypatch):
        calls = _patch(monkeypatch, ids=[], capture=None, legacy="legacy-id")

        assert session_cli._resolve_fork_resume_id("source", FORK_PATH) == "legacy-id"
        assert calls.legacy_calls == [("source", FORK_PATH)]

    def test_chain_all_gone_returns_newest_recorded_id(self, hermes_db, monkeypatch):
        """Nothing resumable: still hand back the newest recorded id so --resume
        surfaces its absence cleanly instead of a silent fresh session."""
        calls = _patch(monkeypatch, ids=["old", "new"], capture=None)

        assert session_cli._resolve_fork_resume_id("source", FORK_PATH) == "new"
        assert calls.capture_calls == [FORK_PATH]
        assert calls.legacy_calls == []   # chain non-empty → never legacy
