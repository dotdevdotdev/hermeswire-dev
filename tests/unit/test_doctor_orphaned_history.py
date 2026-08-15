"""Doctor's conversation-history check under Hermes (#9).

Hermes keys sessions by id in ``~/.hermes/state.db`` with cwd as a data
column, so a moved directory can no longer orphan a transcript. The old
``resumable/orphaned/gone`` trichotomy collapses to "present or not": a
recorded conversation id is either in the store (silent) or absent (reported
only for LIVE sessions, as information, never scored). These tests pin both
the silences and the single report.
"""

import types

import pytest

from hermeswire import doctor_cli, history


class FakeStore:
    """A mutable stand-in for ``hermes_state.SessionDB``."""

    def __init__(self, ids=()):
        self.ids = set(ids)

    def add(self, *ids):
        self.ids.update(ids)

    def get_session(self, session_id):
        return {} if session_id in self.ids else None


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("hermeswire.core.CONFIG_DIR", tmp_path / "hermeswire")
    fake = FakeStore()
    monkeypatch.setattr(history, "_db", lambda: fake)
    # Default: nothing is live. Each test opts in.
    monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: False)
    monkeypatch.setattr("hermeswire.core.tmux_session_cwd", lambda n: None)
    return types.SimpleNamespace(root=tmp_path, store=fake)


def record(session, *, cwd, ids, **extra):
    from hermeswire.core import store_session_metadata

    store_session_metadata(session, {
        "cwd_at_launch": str(cwd),
        "posture": "bypass",
        "conversation_ids": list(ids),
        **extra,
    })


class TestScanOrphanedHistory:
    def test_healthy_session_is_silent(self, store):
        store.store.add("cid")
        record("sess", cwd=store.root / "wt", ids=["cid"])
        assert doctor_cli.scan_orphaned_history() == []

    def test_moved_directory_no_longer_orphans(self, store):
        """The failure mode this check existed for cannot happen under Hermes.

        The id is in the store regardless of the recorded cwd, so a session
        whose directory moved is simply resumable — silent, nothing to fix.
        """
        store.store.add("cid")
        record("sess", cwd=store.root / "new", ids=["cid"])
        assert doctor_cli.scan_orphaned_history() == []

    def test_dead_session_with_no_history_is_silent(self, store):
        """Absent ids on a dead session are not a fault — and there is no
        "orphaned" state to report either."""
        record("resumed", cwd=store.root, ids=[f"fake-{i}" for i in range(36)])
        assert doctor_cli.scan_orphaned_history() == []

    def test_live_session_with_no_history_is_reported_but_not_scored(self, store, monkeypatch):
        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: True)
        record("sess", cwd=store.root, ids=["cid"])

        [f] = doctor_cli.scan_orphaned_history()
        assert f["status"] == "gone"
        assert doctor_cli._render_orphaned_history_section() == 0  # stated, not counted

    def test_pre_871_records_are_skipped(self, store):
        from hermeswire.core import store_session_metadata

        store_session_metadata("old-shape", {"created_by": "orch"})
        store_session_metadata("no-ids", {"cwd_at_launch": str(store.root)})
        assert doctor_cli.scan_orphaned_history() == []

    def test_explicit_session_list_bypasses_the_sweep(self, store):
        store.store.add("cid")
        record("sess", cwd=store.root / "wt", ids=["cid"])
        assert doctor_cli.scan_orphaned_history(sessions=[]) == []
        assert doctor_cli.scan_orphaned_history(sessions=["sess"]) == []


class TestRenderSection:
    def test_clean_store(self, store, capsys):
        assert doctor_cli._render_orphaned_history_section() == 0
        assert "[ok]" in capsys.readouterr().out

    def test_gone_lines_are_stated_not_counted(self, store, monkeypatch, capsys):
        monkeypatch.setattr(doctor_cli, "tmux_session_exists", lambda n: True)
        record("sess", cwd=store.root, ids=["cid"])

        assert doctor_cli._render_orphaned_history_section() == 0
        out = capsys.readouterr().out
        assert "[..]" in out and "sess" in out
