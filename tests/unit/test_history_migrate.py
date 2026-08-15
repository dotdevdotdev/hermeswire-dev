"""Tests for hermeswire/history_migrate.py — now a deprecated no-op (#9).

Hermes sessions are keyed by id in ``~/.hermes/state.db`` with cwd as a data
column, so a moved directory can no longer orphan a transcript. The migrate
machinery's entire premise is gone; these tests pin that every operation is a
safe no-op that says so.
"""


from hermeswire import history_migrate as hm


def test_plan_reports_obsolete():
    result = hm.plan("/old/place", "/new/place")
    assert result["status"] == hm.OBSOLETE
    assert "state.db" in result["detail"]
    assert result["old_cwd"] == "/old/place"


def test_apply_is_a_no_op_that_reports_obsolete():
    result = hm.apply("/old/place", "/new/place")
    assert result["status"] == hm.OBSOLETE


def test_resolve_session_reports_obsolete():
    result = hm.resolve_session("sess")
    assert result["session"] == "sess"
    assert result["status"] == hm.OBSOLETE


def test_scan_finds_nothing():
    assert hm.scan() == []


def test_outcome_constants_are_still_exported():
    for name in ("ALIGNED", "READY", "MIGRATED", "SOURCE_ABSENT",
                 "TARGET_EXISTS", "UNDETERMINED", "ERROR", "OBSOLETE"):
        assert hasattr(hm, name)


class _Fake:
    def __init__(self, ids):
        self.ids = set(ids)

    def get_session(self, session_id):
        return {} if session_id in self.ids else None


def test_resumable_delegates_to_the_store(monkeypatch):
    from hermeswire import history

    monkeypatch.setattr(history, "_db", lambda: _Fake({"abc"}))
    assert hm.resumable("abc", "/place") is True
    assert hm.resumable("nope", "/place") is False


def test_known_sessions_delegates_to_core(monkeypatch, tmp_path):
    from hermeswire import core

    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    for name in ("alpha", "beta"):
        (tmp_path / "sessions" / name).mkdir(parents=True)
        (tmp_path / "sessions" / name / "metadata.json").write_text("{}")
    assert hm.known_sessions() == ["alpha", "beta"]
