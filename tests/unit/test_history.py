"""Tests for agentwire/history.py — Hermes session-store reads (#9).

The Claude transcript store (``~/.claude/history.jsonl`` +
``~/.claude/projects/<encoded-cwd>/<id>.jsonl``) is gone. These pin the Hermes
mapping: sessions are keyed by id in ``~/.hermes/state.db``, resumability is
"present in the store" (there is no orphaned state), and the historical dict
shapes (``sessionId``/``firstMessage``/``lastSummary``/``timestamp``-ms/
``messageCount``) are preserved so the CLI/routes/MCP consumers keep working
unchanged.
"""

import pytest

from agentwire import history


class FakeDB:
    """A stand-in for ``hermes_state.SessionDB`` with the surface history.py uses."""

    def __init__(self, sessions=None, messages=None):
        self.sessions = {s["id"]: dict(s) for s in (sessions or [])}
        self.messages = {sid: list(ms) for sid, ms in (messages or {}).items()}

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def resolve_session_id(self, prefix):
        if prefix in self.sessions:
            return prefix
        matches = [sid for sid in self.sessions if sid.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def list_sessions_rich(self, cwd_prefix=None, limit=20,
                           order_by_last_active=False, **kwargs):
        rows = []
        for s in self.sessions.values():
            if cwd_prefix and cwd_prefix not in (s.get("cwd") or ""):
                continue
            rows.append(s)
        rows.sort(key=lambda r: r.get("last_active", 0), reverse=True)
        return rows[:limit]

    def export_session(self, session_id):
        s = self.sessions.get(session_id)
        if s is None:
            return None
        return {**s, "messages": self.messages.get(session_id, [])}


@pytest.fixture
def db(monkeypatch):
    def make(sessions=None, messages=None):
        fake = FakeDB(sessions, messages)
        monkeypatch.setattr(history, "_db", lambda: fake)
        return fake

    return make


SESS = {
    "id": "abc123",
    "title": "Fix the thing",
    "preview": "hello there",
    "started_at": 1_700_000_000,
    "ended_at": 1_700_000_100,
    "last_active": 1_700_000_100,
    "message_count": 7,
    "cwd": "/proj/x",
    "git_branch": "feat",
}


class TestResolveSessionId:
    def test_exact_match(self, db):
        db(sessions=[SESS])
        assert history.resolve_session_id("abc123") == "abc123"

    def test_unique_prefix(self, db):
        db(sessions=[SESS])
        assert history.resolve_session_id("abc") == "abc123"

    def test_ambiguous_prefix(self, db):
        db(sessions=[SESS, {**SESS, "id": "abc456"}])
        assert history.resolve_session_id("abc") is None

    def test_unknown(self, db):
        db(sessions=[SESS])
        assert history.resolve_session_id("nope") is None

    def test_remote_is_unsupported(self, db):
        db(sessions=[SESS])
        assert history.resolve_session_id("abc123", machine="other") is None


class TestGetHistory:
    def test_lists_sessions_for_a_cwd(self, db):
        db(sessions=[SESS, {**SESS, "id": "other", "cwd": "/proj/y"}])
        rows = history.get_history("/proj/x")
        assert [r["sessionId"] for r in rows] == ["abc123"]

    def test_maps_to_the_old_shape(self, db):
        db(sessions=[SESS])
        [r] = history.get_history("/proj/x")
        assert r["sessionId"] == "abc123"
        assert r["firstMessage"] == "hello there"
        assert r["lastSummary"] == "Fix the thing"
        assert r["timestamp"] == 1_700_000_100 * 1000  # ms, the old shape
        assert r["messageCount"] == 7

    def test_empty_when_no_store(self, monkeypatch):
        monkeypatch.setattr(history, "_db", lambda: None)
        assert history.get_history("/proj/x") == []

    def test_remote_is_unsupported(self, db):
        db(sessions=[SESS])
        assert history.get_history("/proj/x", machine="other") == []


class TestGetSessionDetail:
    def test_detail_shape(self, db):
        db(sessions=[SESS], messages={
            "abc123": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        })
        d = history.get_session_detail("abc123")
        assert d["sessionId"] == "abc123"
        assert d["firstMessage"] == "hello"
        assert d["summaries"] == ["hi there"]  # assistant turns, no summary type
        assert d["gitBranch"] == "feat"
        assert d["messageCount"] == 7
        assert d["timestamps"]["start"] == 1_700_000_000 * 1000
        assert d["timestamps"]["end"] == 1_700_000_100 * 1000

    def test_resolves_prefix(self, db):
        db(sessions=[SESS], messages={"abc123": []})
        d = history.get_session_detail("abc")
        assert d is not None
        assert d["sessionId"] == "abc123"

    def test_none_when_absent(self, db):
        db(sessions=[SESS])
        assert history.get_session_detail("nope") is None


class TestLocateConversation:
    def test_present_is_resumable(self, db):
        db(sessions=[SESS])
        loc = history.locate_conversation("abc123", "/proj/x")
        assert loc.status == "resumable" and loc.resumable
        assert loc.found_at is not None
        assert loc.elsewhere == ()

    def test_absent_is_gone(self, db):
        db(sessions=[SESS])
        loc = history.locate_conversation("nope", "/proj/x")
        assert loc.status == "gone"
        assert not loc.resumable
        assert loc.found_at is None and loc.elsewhere == ()

    def test_there_is_no_orphaned_state(self, db):
        """A moved directory cannot strand a session: id lookup ignores cwd."""
        db(sessions=[SESS])
        loc = history.locate_conversation("abc123", "/somewhere/else")
        assert loc.status == "resumable"

    def test_holds_a_conversation_matches_presence(self, db):
        db(sessions=[SESS])
        assert history.holds_a_conversation("abc123") is True
        assert history.holds_a_conversation("nope") is False

    def test_resumable_predicate(self, db):
        db(sessions=[SESS])
        assert history.resumable("abc123") is True
        assert history.resumable("nope") is False


class TestRetainedEncoding:
    def test_encode_project_path_is_still_exported_for_importers(self):
        """``encode_project_path`` survives for auth_expired/handoff/session_cli."""
        assert history.encode_project_path("/Users/dev/my-app") == "-Users-dev-my-app"
