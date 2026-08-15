"""Tests for `agentwire history migrate` — obsolete under Hermes (#9).

Claude keyed transcripts by cwd, so a directory move orphaned them and this verb
re-keyed them. Hermes keys sessions by id in ~/.hermes/state.db (cwd is a data
column), so nothing can be orphaned and the verb is a reporting no-op.
"""

import json

from agentwire import history_cli
from agentwire import history_migrate as hm


class Args:
    def __init__(self, **kw):
        self.json = kw.get("json", False)


class TestMigrateIsObsolete:
    def test_plain_reports_obsolete_and_succeeds(self, capsys):
        assert history_cli.cmd_history_migrate(Args()) == 0
        assert "obsolete" in capsys.readouterr().out.lower()

    def test_json_reports_obsolete(self, capsys):
        assert history_cli.cmd_history_migrate(Args(json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["obsolete"] is True
        assert "message" in payload


class TestMigrateModule:
    def test_plan_is_obsolete(self):
        assert hm.plan("/a", "/b")["status"] == hm.OBSOLETE

    def test_apply_is_a_noop(self):
        assert hm.apply("/a", "/b")["status"] == hm.OBSOLETE

    def test_resolve_session_is_obsolete(self):
        assert hm.resolve_session("s")["status"] == hm.OBSOLETE

    def test_scan_is_empty(self):
        assert hm.scan() == []

    def test_resumable_delegates_to_history(self, monkeypatch):
        monkeypatch.setattr(
            "agentwire.history.resumable",
            lambda sid, machine="local": sid == "present",
        )
        assert hm.resumable("present") is True
        assert hm.resumable("absent") is False
