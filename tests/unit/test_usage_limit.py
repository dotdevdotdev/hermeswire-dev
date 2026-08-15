"""Tests for provider-limit recovery (hermeswire/usage_limit.py, issue #8).

Claude's usage-limit select-menu (park/reset dialog) is gone — Hermes surfaces
provider limits as structured ``AuthError`` codes on the failed turn's stderr
or ``hermes auth status``. These tests cover the error-code detection
(transient vs hard), the park state write (no keystrokes), the resume nudge,
and the config gates, all against the #13 detector's primitives.
"""

import json
from datetime import datetime, timezone

import pytest

from hermeswire import usage_limit

# Original reader, captured before the autouse fixture stubs it per-test.
_ORIG_RECOVERY_CONFIG = usage_limit._recovery_config


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point all state/event paths into tmp and never send real email."""
    state_dir = tmp_path / "usage-limit"
    monkeypatch.setattr(usage_limit, "STATE_DIR", state_dir)
    monkeypatch.setattr(usage_limit, "DONE_DIR", state_dir / "done")
    monkeypatch.setattr(usage_limit, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        usage_limit, "_send_notification", lambda *a, **k: False
    )
    monkeypatch.setattr(usage_limit.time, "sleep", lambda s: None)
    # Default knobs — individual tests override to exercise the config gate.
    monkeypatch.setattr(usage_limit, "_recovery_config", lambda: (True, set()))
    return state_dir


def events(tmp_path=None):
    path = usage_limit.EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# =============================================================================
# Detection (provider limit errors, not menus)
# =============================================================================


class TestDetectLimit:
    def test_transient_codex_rate_limited(self):
        stderr = (
            "hermes -z: agent failed: AuthError(provider='codex', "
            "code='codex_rate_limited', relogin_required=False)"
        )
        d = usage_limit.detect_limit("s1", stderr=stderr)
        assert d is not None
        assert d["code"] == "codex_rate_limited"
        assert d["transient"] is True
        assert d["hard"] is False
        assert d["source"] == "stderr"

    def test_hard_credit_error(self):
        stderr = "agent failed: AuthError(provider='anthropic', code='insufficient_credits')"
        d = usage_limit.detect_limit("s1", stderr=stderr)
        assert d["code"] == "insufficient_credits"
        assert d["transient"] is False
        assert d["hard"] is True

    def test_no_auth_error_returns_none(self):
        assert usage_limit.detect_limit("s1", stderr="all green") is None
        assert usage_limit.detect_limit("s1") is None
        assert usage_limit.detect_limit("s1", stderr="") is None

    def test_unrecognized_auth_code_is_not_a_limit(self):
        # An AuthError whose code is neither transient (codex_rate_limited /
        # temporarily_unavailable) nor hard/credit is not a park trigger.
        # detect_limit never reads pane text at all — it keys on the session's
        # OWN structured error (stderr / store / auth status), so an
        # orchestrator merely *displaying* another session's error can never
        # be parked (the #13 transcript-vs-pane rule).
        stderr = "agent failed: AuthError(provider='x', code='some_other_error')"
        assert usage_limit.detect_limit("s1", stderr=stderr) is None

    def test_preflight_hard_auth(self, monkeypatch):
        monkeypatch.setattr(
            "hermeswire.auth_expired.probe_provider_auth",
            lambda provider: {
                "provider": provider,
                "code": "no_usable_credits",
                "relogin_required": False,
            },
        )
        d = usage_limit.detect_limit("s1", provider="nous")
        assert d["source"] == "preflight"
        assert d["transient"] is False
        assert d["hard"] is True
        assert d["code"] == "no_usable_credits"

    def test_store_message_surface_is_stub(self):
        # Mirrors auth_expired._session_last_auth_error: not yet wired (#9).
        assert usage_limit._session_last_limit_error("s1") is None


# =============================================================================
# State files
# =============================================================================


class TestState:
    def test_roundtrip_and_is_parked(self):
        state = {"session": "mysession", "status": "parked"}
        usage_limit.write_park_state(state)
        assert usage_limit.is_parked("mysession") is True
        assert usage_limit.read_park_state("mysession") == state
        assert usage_limit.list_parked() == [state]

    def test_worktree_session_names_nest(self):
        state = {"session": "fragmentz/scheduler-leads-daily", "status": "parked"}
        usage_limit.write_park_state(state)
        assert usage_limit.is_parked("fragmentz/scheduler-leads-daily") is True
        assert usage_limit.list_parked() == [state]

    def test_archive_moves_out_of_active(self):
        state = {"session": "proj/branch", "status": "parked"}
        usage_limit.write_park_state(state)
        usage_limit.archive_state(state, "resumed")
        assert usage_limit.is_parked("proj/branch") is False
        assert usage_limit.list_parked() == []
        archived = list(usage_limit.DONE_DIR.glob("*.json"))
        assert len(archived) == 1
        data = json.loads(archived[0].read_text())
        assert data["status"] == "resumed"
        assert data["archived_at"]

    def test_not_parked_when_nothing_written(self):
        assert usage_limit.is_parked("ghost") is False
        assert usage_limit.list_parked() == []


# =============================================================================
# Park
# =============================================================================


class TestPark:
    def test_parks_and_writes_state_no_keystrokes(self, monkeypatch):
        # A limit-parked Hermes session needs NO keystroke — no tmux at all.
        def boom(*a, **k):
            raise AssertionError("park must not touch tmux (no menu to answer)")

        monkeypatch.setattr(usage_limit, "_tmux", boom)
        monkeypatch.setattr(usage_limit, "_task_info", lambda session: {})
        monkeypatch.setattr(usage_limit, "_notify_parked", lambda state: True)
        now = datetime(2026, 6, 11, 2, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(usage_limit, "_now", lambda: now)

        limit = {
            "provider": "codex",
            "code": "codex_rate_limited",
            "transient": True,
            "evidence": "AuthError(code='codex_rate_limited')",
        }
        state = usage_limit.park("fragmentz/leads", pane_index=0, limit=limit)

        assert state is not None
        assert state["status"] == "parked"
        assert state["code"] == "codex_rate_limited"
        assert state["provider"] == "codex"
        assert state["transient"] is True
        assert state["reset_at"] == "2026-06-11T02:35:00+00:00"
        assert state["resume_at"] == "2026-06-11T02:36:00+00:00"
        assert usage_limit.is_parked("fragmentz/leads")
        assert any(e["event"] == "session_parked" for e in events())

    def test_park_hard_credit_error(self, monkeypatch):
        monkeypatch.setattr(usage_limit, "_task_info", lambda session: {})
        monkeypatch.setattr(usage_limit, "_notify_parked", lambda state: True)
        now = datetime(2026, 6, 11, 2, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(usage_limit, "_now", lambda: now)

        state = usage_limit.park(
            "s1",
            limit={"provider": "anthropic", "code": "insufficient_credits",
                   "transient": False},
        )
        assert state["transient"] is False
        assert state["code"] == "insufficient_credits"

    def test_idempotent_when_already_parked(self, monkeypatch):
        usage_limit.write_park_state({"session": "s1", "status": "parked"})
        monkeypatch.setattr(usage_limit, "_task_info", lambda s: {})
        assert usage_limit.park("s1") is None


# =============================================================================
# Resume
# =============================================================================


class TestResume:
    def _parked(self, session="s1", resume_at=None, **extra):
        state = {
            "session": session,
            "pane": 0,
            "status": "parked",
            "detected_at": "2026-06-11T02:30:00+00:00",
            "parked_at": "2026-06-11T02:30:05+00:00",
            "reset_at": "2026-06-11T02:35:00+00:00",
            "resume_at": resume_at or "2026-06-11T02:36:00+00:00",
            "notified": True,
            "resume_attempts": 0,
            **extra,
        }
        usage_limit.write_park_state(state)
        return state

    def test_resume_sends_nudge_and_archives(self, monkeypatch):
        state = self._parked()
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        sent = []
        monkeypatch.setattr(
            usage_limit, "_capture",
            lambda target, scrollback=None: f"> {usage_limit.RESUME_NUDGE}\n",
        )
        import hermeswire.pane_manager as pm
        monkeypatch.setattr(
            pm, "send_to_target", lambda target, text, enter=True: sent.append((target, text))
        )

        assert usage_limit.resume_session(state) is True
        assert sent == [("s1.0", usage_limit.RESUME_NUDGE)]
        assert not usage_limit.is_parked("s1")
        assert any(e["event"] == "session_resumed" for e in events())

    def test_resume_dead_session_archives_orphaned(self, monkeypatch):
        state = self._parked()
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: False)
        assert usage_limit.resume_session(state) is False
        assert not usage_limit.is_parked("s1")
        archived = json.loads(next(usage_limit.DONE_DIR.glob("*.json")).read_text())
        assert archived["status"] == "orphaned"

    def test_resume_failure_increments_attempts(self, monkeypatch):
        state = self._parked()
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        monkeypatch.setattr(usage_limit, "_capture", lambda *a, **k: "no echo here")
        import hermeswire.pane_manager as pm
        monkeypatch.setattr(pm, "send_to_target", lambda *a, **k: None)

        assert usage_limit.resume_session(state) is False
        assert usage_limit.read_park_state("s1")["resume_attempts"] == 1

    def test_resume_gives_up_after_max_attempts(self, monkeypatch):
        state = self._parked(resume_attempts=usage_limit.MAX_RESUME_ATTEMPTS - 1)
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        monkeypatch.setattr(usage_limit, "_capture", lambda *a, **k: "")
        import hermeswire.pane_manager as pm
        monkeypatch.setattr(pm, "send_to_target", lambda *a, **k: None)

        assert usage_limit.resume_session(state) is False
        assert not usage_limit.is_parked("s1")
        archived = json.loads(next(usage_limit.DONE_DIR.glob("*.json")).read_text())
        assert archived["status"] == "resume_failed"

    def test_resume_due_only_past_resume_at(self, monkeypatch):
        self._parked(session="due", resume_at="2026-06-11T02:36:00+00:00")
        self._parked(session="later", resume_at="2026-06-11T09:00:00+00:00")
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        resumed_calls = []

        def fake_resume(state, force=False):
            resumed_calls.append(state["session"])
            usage_limit.archive_state(state, "resumed")
            return True

        monkeypatch.setattr(usage_limit, "resume_session", fake_resume)

        now = datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc)
        assert usage_limit.resume_due(now) == ["due"]
        assert resumed_calls == ["due"]
        assert usage_limit.is_parked("later")

    def test_resume_due_archives_orphans_early(self, monkeypatch):
        self._parked(session="gone", resume_at="2026-06-11T09:00:00+00:00")
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: False)
        now = datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc)
        assert usage_limit.resume_due(now) == []
        assert not usage_limit.is_parked("gone")


# =============================================================================
# Sweep
# =============================================================================


class TestSweep:
    def test_sweep_parks_limited_sessions(self, monkeypatch):
        monkeypatch.setattr(usage_limit, "_list_sessions", lambda: ["work", "idle"])

        def fake_detect(session, pane_index=0):
            return {"session": session, "code": "codex_rate_limited"} if session == "work" else None

        monkeypatch.setattr(usage_limit, "detect_limit", fake_detect)
        parked_calls = []
        monkeypatch.setattr(
            usage_limit, "park",
            lambda session, pane_index=0, source="watchdog", limit=None:
                parked_calls.append(session) or {"session": session},
        )

        result = usage_limit.sweep()
        assert parked_calls == ["work"]
        assert [s["session"] for s in result] == ["work"]

    def test_sweep_disabled_by_config(self, monkeypatch):
        monkeypatch.setattr(usage_limit, "_recovery_config", lambda: (False, set()))

        def boom(*a, **k):
            raise AssertionError("disabled sweep must not list sessions")

        monkeypatch.setattr(usage_limit, "_list_sessions", boom)
        assert usage_limit.sweep() == []

    def test_sweep_skips_excluded_sessions(self, monkeypatch):
        monkeypatch.setattr(
            usage_limit, "_recovery_config", lambda: (True, {"precious"})
        )
        monkeypatch.setattr(usage_limit, "_list_sessions", lambda: ["precious", "other"])
        monkeypatch.setattr(
            usage_limit, "detect_limit",
            lambda session, pane_index=0: {"session": session, "code": "codex_rate_limited"},
        )
        parked_calls = []
        monkeypatch.setattr(
            usage_limit, "park",
            lambda session, pane_index=0, source="watchdog", limit=None:
                parked_calls.append(session) or {"session": session},
        )

        usage_limit.sweep()
        assert parked_calls == ["other"]

    def test_sweep_skips_already_parked(self, monkeypatch):
        usage_limit.write_park_state({"session": "work", "status": "parked"})
        monkeypatch.setattr(usage_limit, "_list_sessions", lambda: ["work"])

        def boom(*a, **k):
            raise AssertionError("should not detect a parked session")

        monkeypatch.setattr(usage_limit, "detect_limit", boom)
        assert usage_limit.sweep() == []


# =============================================================================
# check_and_park (ensure's fast-path probe)
# =============================================================================


class TestCheckAndPark:
    def test_already_parked_short_circuits(self, monkeypatch):
        usage_limit.write_park_state({"session": "s1", "status": "parked"})

        def boom(*a, **k):
            raise AssertionError("must not detect when already parked")

        monkeypatch.setattr(usage_limit, "detect_limit", boom)
        assert usage_limit.check_and_park("s1") is True

    def test_limit_parks(self, monkeypatch):
        monkeypatch.setattr(
            usage_limit, "detect_limit",
            lambda session, pane_index=0: {"session": session, "code": "codex_rate_limited"},
        )
        monkeypatch.setattr(
            usage_limit, "park",
            lambda session, pane_index=0, source="ensure", limit=None:
                usage_limit.write_park_state({"session": session, "status": "parked"}),
        )
        assert usage_limit.check_and_park("s1", source="ensure") is True

    def test_no_limit_is_false(self, monkeypatch):
        monkeypatch.setattr(usage_limit, "detect_limit", lambda *a, **k: None)
        assert usage_limit.check_and_park("s1") is False

    def test_disabled_gates_new_parks(self, monkeypatch):
        monkeypatch.setattr(usage_limit, "_recovery_config", lambda: (False, set()))
        monkeypatch.setattr(
            usage_limit, "detect_limit",
            lambda session, pane_index=0: {"session": session, "code": "codex_rate_limited"},
        )
        assert usage_limit.check_and_park("s1") is False
        assert not usage_limit.is_parked("s1")

    def test_excluded_session_gates_new_parks(self, monkeypatch):
        monkeypatch.setattr(usage_limit, "_recovery_config", lambda: (True, {"s1"}))
        monkeypatch.setattr(
            usage_limit, "detect_limit",
            lambda session, pane_index=0: {"session": session, "code": "codex_rate_limited"},
        )
        assert usage_limit.check_and_park("s1") is False
        assert not usage_limit.is_parked("s1")

    def test_already_parked_wins_over_exclusion(self, monkeypatch):
        # Exclusion gates NEW parks only — an existing park is still honored.
        usage_limit.write_park_state({"session": "s1", "status": "parked"})
        monkeypatch.setattr(usage_limit, "_recovery_config", lambda: (False, {"s1"}))
        assert usage_limit.check_and_park("s1") is True


# =============================================================================
# Config knobs (usage_limit: section in config.yaml)
# =============================================================================


class TestRecoveryConfig:
    def test_defaults_when_section_absent(self):
        from hermeswire.config import _dict_to_config

        cfg = _dict_to_config({})
        assert cfg.usage_limit.enabled is True
        assert cfg.usage_limit.exclude_sessions == []

    def test_section_parsed(self):
        from hermeswire.config import _dict_to_config

        cfg = _dict_to_config({
            "usage_limit": {
                "enabled": False,
                "exclude_sessions": ["jordan", "fragmentz"],
            }
        })
        assert cfg.usage_limit.enabled is False
        assert cfg.usage_limit.exclude_sessions == ["jordan", "fragmentz"]

    def test_malformed_section_falls_back_to_defaults(self):
        from hermeswire.config import _dict_to_config

        cfg = _dict_to_config({"usage_limit": "nonsense"})
        assert cfg.usage_limit.enabled is True
        assert cfg.usage_limit.exclude_sessions == []

        cfg = _dict_to_config({"usage_limit": {"exclude_sessions": "not-a-list"}})
        assert cfg.usage_limit.exclude_sessions == []

    def test_recovery_config_reads_config_object(self, monkeypatch):
        import hermeswire.config as config_mod
        from hermeswire.config import UsageLimitConfig

        class FakeConfig:
            usage_limit = UsageLimitConfig(enabled=False, exclude_sessions=["a", "b"])

        monkeypatch.setattr(config_mod, "get_config", lambda: FakeConfig())
        # The autouse fixture stubs usage_limit._recovery_config — call the
        # original (captured at import, before the fixture patched it).
        assert _ORIG_RECOVERY_CONFIG() == (False, {"a", "b"})


# =============================================================================
# Status / exit-code mappings (ensure + scheduler integration)
# =============================================================================


class TestStatusMappings:
    def test_completion_exit_code(self):
        from hermeswire.completion import status_to_exit_code

        assert status_to_exit_code("usage_limit") == 7
        assert status_to_exit_code("complete") == 0
        assert status_to_exit_code("failed") == 1
        assert status_to_exit_code("incomplete") == 2

    def test_scheduler_status_map(self):
        from hermeswire.scheduler import _EXIT_TO_STATUS, _EXIT_USAGE_LIMIT

        assert _EXIT_USAGE_LIMIT == 7
        assert _EXIT_TO_STATUS[7] == "usage_limit"


# =============================================================================
# Tick
# =============================================================================


class TestTick:
    def test_tick_runs_sweep_then_resume(self, monkeypatch):
        order = []
        monkeypatch.setattr(
            usage_limit, "sweep", lambda: order.append("sweep") or []
        )
        monkeypatch.setattr(
            usage_limit, "resume_due", lambda now=None: order.append("resume") or []
        )
        result = usage_limit.tick()
        assert order == ["sweep", "resume"]
        assert result == {"parked": [], "resumed": [], "waiting": []}
