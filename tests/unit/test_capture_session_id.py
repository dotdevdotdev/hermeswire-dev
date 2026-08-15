"""Tests for capture_session_id — the post-launch id capture half of #4/#22.

capture_session_id polls ~/.hermes/state.db for the Hermes session id a fresh
launch minted, since the interactive REPL has no stderr to parse. It must
degrade gracefully when the Hermes session store is unavailable (the common
case in test environments) and return the id when the store has a matching row.
"""


from hermeswire import core


class TestCaptureSessionId:
    """The store-unavailable path is the one every test environment hits —
    hermes_state is installed in the Hermes tool's own Python, not in the
    project venv. capture_session_id must return None immediately, not poll
    for the full timeout."""

    def test_returns_none_immediately_when_store_unavailable(self, monkeypatch, tmp_path):
        """No hermes_state import → no store → None now, not None after timeout."""
        monkeypatch.setattr(core, "_hermes_db_instance", None)
        # Force the lazy import to fail as if hermes_state is not installed.
        import sys
        monkeypatch.setitem(sys.modules, "hermes_state", None)

        import time
        start = time.time()
        result = core.capture_session_id(tmp_path, timeout=30.0)
        elapsed = time.time() - start

        assert result is None
        # Must not have polled — returns in well under the timeout.
        assert elapsed < 1.0

    def test_returns_id_when_store_has_matching_row(self, monkeypatch, tmp_path):
        """A row with source='tool' and matching cwd returns its id."""
        class _FakeDB:
            def list_sessions_rich(self, **kwargs):
                assert kwargs.get("source") == "tool"
                assert str(tmp_path) in str(kwargs.get("cwd_prefix", ""))
                return [{"id": "20260815_120000_abcdef", "started_at": 1000.0}]

        monkeypatch.setattr(core, "_hermes_session_db", lambda: _FakeDB())

        result = core.capture_session_id(tmp_path, timeout=5.0)
        assert result == "20260815_120000_abcdef"

    def test_source_none_queries_all_sources(self, monkeypatch, tmp_path):
        """source=None is used by the headless path (hermes -z defaults to cli)."""
        seen_sources = []

        class _FakeDB:
            def list_sessions_rich(self, **kwargs):
                seen_sources.append(kwargs.get("source"))
                return [{"id": "headless-id", "started_at": 1000.0}]

        monkeypatch.setattr(core, "_hermes_session_db", lambda: _FakeDB())

        result = core.capture_session_id(tmp_path, timeout=5.0, source=None)
        assert result == "headless-id"
        assert seen_sources == [None]  # no source filter passed

    def test_started_after_filters_stale_sessions(self, monkeypatch, tmp_path):
        """A session older than started_after is skipped, not returned."""
        class _FakeDB:
            def __init__(self):
                self.calls = 0

            def list_sessions_rich(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    # Stale session — started before our launch.
                    return [{"id": "stale-id", "started_at": 900.0}]
                return []  # no new session appears

        fake = _FakeDB()
        monkeypatch.setattr(core, "_hermes_session_db", lambda: fake)
        monkeypatch.setattr("time.sleep", lambda s: None)  # don't actually sleep

        result = core.capture_session_id(
            tmp_path, timeout=5.0, started_after=1000.0, poll_interval=0.01,
        )
        assert result is None
        assert fake.calls >= 2  # polled at least twice

    def test_returns_none_on_timeout_when_no_matching_row(self, monkeypatch, tmp_path):
        """No matching row in the store → None after timeout."""
        class _FakeDB:
            def list_sessions_rich(self, **kwargs):
                return []

        monkeypatch.setattr(core, "_hermes_session_db", lambda: _FakeDB())
        monkeypatch.setattr("time.sleep", lambda s: None)  # don't actually sleep

        result = core.capture_session_id(
            tmp_path, timeout=0.05, poll_interval=0.01,
        )
        assert result is None

    def test_returns_none_on_db_exception(self, monkeypatch, tmp_path):
        """A store that raises returns None, not a traceback."""
        class _FakeDB:
            def list_sessions_rich(self, **kwargs):
                raise RuntimeError("FTS5 unavailable")

        monkeypatch.setattr(core, "_hermes_session_db", lambda: _FakeDB())
        monkeypatch.setattr("time.sleep", lambda s: None)

        result = core.capture_session_id(
            tmp_path, timeout=0.05, poll_interval=0.01,
        )
        assert result is None


class TestExtractHermesSessionIdStillWorks:
    """The pure-function parser is unchanged by #22; pin it still works."""

    def test_parses_session_id_line(self):
        assert core.extract_hermes_session_id(
            "noise\nsession_id: 20260815_120000_abcdef\n") \
            == "20260815_120000_abcdef"

    def test_returns_none_for_no_id(self):
        assert core.extract_hermes_session_id("nothing here") is None
