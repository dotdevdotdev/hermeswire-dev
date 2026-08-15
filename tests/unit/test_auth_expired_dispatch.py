"""The Hermes auth-failure detector fires through the REAL dispatch paths (#13).

`tests/unit/test_auth_expired.py` proves the detector recognises the Hermes
``AuthError`` shape. That is necessary and not sufficient: the original
incident's cost came from `wait_for_completion_signal` polling forever and the
scheduler dispatching into the outage again, so what has to be proven here is
that those two functions — the actual ones, not stand-ins — change behaviour.

Every test below drives the shipped code path:

* ``wait_for_completion_signal`` with a hard ``AuthError`` on the failed run's
  stderr (and with an exited headless ``hermes -z`` process), asserting it
  RETURNS ``auth_expired`` instead of looping until the session dies.
* ``_dispatch_ensure_task`` with an outage recorded, asserting `ensure` is
  never invoked and ``last_run`` is not consumed.
* ``_session_has_agent`` distinguishing a Hermes agent-python from a
  daemon-python, so a live session reads alive and a daemon doesn't.
"""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_auth_expired import AUTH_ERROR_STDERR

from agentwire import auth_expired, completion
from agentwire.completion import CompletionTimeout, status_to_exit_code, wait_for_completion_signal
from agentwire.ensure_cli import ENSURE_EXIT_AUTH_EXPIRED
from agentwire.scheduler.models import _EXIT_TO_STATUS, SchedulerTask, TaskState


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    monkeypatch.setattr("agentwire.completion.TASKS_DIR", tmp_path / "tasks")
    return tmp_path


def _parked_off(monkeypatch):
    monkeypatch.setattr("agentwire.usage_limit.check_and_park", lambda *a, **k: False)


def _agent_alive(monkeypatch):
    monkeypatch.setattr("agentwire.completion._session_has_agent", lambda s: True)


class TestSessionHasAgentHermes:
    """A Hermes agent is a python process; the liveness check must not confuse
    it with a daemon-python (or a bare shell)."""

    def test_bare_shell_is_not_agent(self):
        assert completion._command_is_agent("zsh") is False
        assert completion._command_is_agent("bash") is False

    def test_hermes_binary_is_agent(self):
        assert completion._command_is_agent("hermes") is True

    def test_python_requires_hermes_cmdline(self, monkeypatch):
        monkeypatch.setattr("agentwire.completion._pid_is_hermes_agent", lambda pid: True)
        assert completion._command_is_agent("python3.13", "4242") is True
        monkeypatch.setattr("agentwire.completion._pid_is_hermes_agent", lambda pid: False)
        assert completion._command_is_agent("python3.13", "4242") is False

    def test_pid_inspection_separates_agent_from_daemon(self):
        agent = SimpleNamespace(returncode=0,
                                stdout="/Users/dotdev/.local/share/uv/tools/hermes-agent/bin/python hermes chat --cli\n",
                                stderr="")
        daemon = SimpleNamespace(returncode=0, stdout="agentwire scheduler\n", stderr="")
        with patch("agentwire.completion.subprocess.run", return_value=agent):
            assert completion._pid_is_hermes_agent("1") is True
        with patch("agentwire.completion.subprocess.run", return_value=daemon):
            assert completion._pid_is_hermes_agent("1") is False

    def test_session_has_agent_resolves_a_python_pane(self, monkeypatch):
        panes = SimpleNamespace(returncode=0, stdout="python3.13\t4242\n", stderr="")
        monkeypatch.setattr("agentwire.completion.subprocess.run", lambda *a, **k: panes)
        monkeypatch.setattr("agentwire.completion._pid_is_hermes_agent", lambda pid: True)
        assert completion._session_has_agent("s") is True

    def test_missing_session_is_dead(self, monkeypatch):
        monkeypatch.setattr(
            "agentwire.completion.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
        assert completion._session_has_agent("s") is False


class TestCompletionWaitReturnsInsteadOfHanging:
    def test_wait_reports_auth_failure_from_stderr(self, env, monkeypatch):
        """The shipped wait returns a named cause in one tick, not a hang.

        Drives the real `wait_for_completion_signal` against the shape of the
        run that hung: session alive, no usage-limit dialog, no summary file —
        but the failed turn's stderr carries the hard AuthError.
        """
        _agent_alive(monkeypatch)
        _parked_off(monkeypatch)

        summary = env / "task-summary-s-x-2026-08-04T08-00-00.md"
        started = time.time()
        result = wait_for_completion_signal(
            "s", poll_interval=0.01, summary_path=summary,
            stderr=AUTH_ERROR_STDERR, max_duration=2,
        )
        assert time.time() - started < 5, "must not poll on a turn that was refused"
        assert result["status"] == "auth_expired"
        assert "nous" in result["summary"]
        assert "subscription_expired" in result["summary"]

    def test_the_outage_is_recorded_so_the_fleet_can_be_gated(self, env, monkeypatch):
        _agent_alive(monkeypatch)
        _parked_off(monkeypatch)

        summary = env / "task-summary-s-x-2026-08-04T08-00-00.md"
        assert auth_expired.outage_active() is None
        wait_for_completion_signal(
            "s", poll_interval=0.01, summary_path=summary,
            stderr=AUTH_ERROR_STDERR, max_duration=2)
        outage = auth_expired.outage_active()
        assert outage is not None
        assert "s" in outage["sessions"]
        assert outage["provider"] == "nous"

    def test_a_transient_rate_limit_does_not_gate(self, env, monkeypatch):
        """A `codex_rate_limited` failure must NOT record an outage."""
        _agent_alive(monkeypatch)
        _parked_off(monkeypatch)
        from test_auth_expired import TRANSIENT_STDERR

        summary = env / "task-summary-s-x-2026-08-04T08-00-00.md"
        # The wait keeps polling (session alive, no summary, no hard failure),
        # so bound it with max_duration; the assert is that no outage appears.
        with pytest.raises(CompletionTimeout):
            wait_for_completion_signal(
                "s", poll_interval=0.01, summary_path=summary,
                stderr=TRANSIENT_STDERR, max_duration=1)
        assert auth_expired.outage_active() is None

    def test_a_successful_turn_reopens_the_gate(self, env, monkeypatch):
        auth_expired.record_outage({"session": "s", "provider": "nous",
                                    "code": "subscription_expired"})
        assert auth_expired.outage_active() is not None

        _agent_alive(monkeypatch)
        _parked_off(monkeypatch)
        summary = env / "task-summary-s-x-2026-08-05T04-00-00.md"
        summary.write_text("---\nstatus: complete\nsummary: did the thing\n---\n")

        result = wait_for_completion_signal("s", poll_interval=0.01, summary_path=summary)
        assert result["status"] == "complete"
        assert auth_expired.outage_active() is None, "the promise must be true"
        assert auth_expired.read_state() is None, "the record itself is gone"

    def test_clearing_a_gate_that_was_never_set_is_harmless(self, env, monkeypatch):
        _agent_alive(monkeypatch)
        _parked_off(monkeypatch)
        summary = env / "task-summary-s-x-2026-08-05T04-00-00.md"
        summary.write_text("---\nstatus: complete\nsummary: fine\n---\n")

        assert auth_expired.read_state() is None
        assert wait_for_completion_signal(
            "s", poll_interval=0.01, summary_path=summary)["status"] == "complete"

    def test_a_healthy_session_is_untouched(self, env, monkeypatch):
        _agent_alive(monkeypatch)
        _parked_off(monkeypatch)
        summary = env / "task-summary-s-x-2026-08-04T08-00-00.md"
        summary.write_text("## Status: complete\n\nDid the thing.\n")

        result = wait_for_completion_signal("s", poll_interval=0.01, summary_path=summary)
        assert result["status"] != "auth_expired"
        assert auth_expired.outage_active() is None


class TestHeadlessCompletion:
    """Headless `hermes -z`/`-q`: completion is the process exit, not a pane."""

    def test_process_exit_with_auth_failure_reports_the_cause(self, env, monkeypatch):
        _parked_off(monkeypatch)
        proc = SimpleNamespace(poll=lambda: 1, returncode=1, stderr=AUTH_ERROR_STDERR)
        result = wait_for_completion_signal(
            "s", poll_interval=0.01,
            summary_path=env / "task-summary-s-x-2026-08-04T08-00-00.md",
            process=proc, max_duration=2)
        assert result["status"] == "auth_expired"
        assert auth_expired.outage_active() is not None

    def test_process_exit_zero_with_summary_returns_complete(self, env, monkeypatch):
        _parked_off(monkeypatch)
        summary = env / "task-summary-s-x-2026-08-05T04-00-00.md"
        summary.write_text("---\nstatus: complete\nsummary: done\n---\n")
        proc = SimpleNamespace(poll=lambda: 0, returncode=0, stderr="")
        result = wait_for_completion_signal(
            "s", poll_interval=0.01, summary_path=summary, process=proc)
        assert result["status"] == "complete"

    def test_process_exit_one_without_summary_is_a_timeout(self, env, monkeypatch):
        _parked_off(monkeypatch)
        proc = SimpleNamespace(poll=lambda: 0, returncode=1, stderr="some non-auth error")
        with pytest.raises(CompletionTimeout):
            wait_for_completion_signal(
                "s", poll_interval=0.01,
                summary_path=env / "task-summary-s-x-2026-08-04T08-00-00.md",
                process=proc, max_duration=2)


class TestEnsureWiresTheAnchorAndStopsRetrying:
    """Drives the real `_run_ensure_task` — a wire that isn't connected is
    invisible to a test that only exercises `wait_for_completion_signal`."""

    def _run(self, tmp_path, signal, task_overrides=None):
        from agentwire import ensure_cli
        from agentwire.tasks import parse_task_config
        from agentwire.templating import TemplateContext

        task = parse_task_config("t", {"prompt": "do the thing",
                                       **(task_overrides or {})})
        ctx = TemplateContext(session="s", task="t", project_root=str(tmp_path))
        args = SimpleNamespace(session="s", task="t")
        (tmp_path / ".agentwire").mkdir(exist_ok=True)

        with patch.object(ensure_cli, "send_task_prompt", return_value=True) as send, \
             patch("agentwire.ensure_cli.tmux_session_exists", return_value=True), \
             patch("agentwire.session_ready.wait_for_session_ready", return_value=True), \
             patch("agentwire.completion.wait_for_completion_signal") as wait, \
             patch("agentwire.completion.write_task_context"), \
             patch("agentwire.completion.clear_task_context"), \
             patch("agentwire.ensure_cli.subprocess.run"), \
             patch("agentwire.ensure_cli.time.sleep"):
            wait.return_value = signal
            rc = ensure_cli._run_ensure_task(
                args, "s", task, ctx, "/bin/sh", tmp_path, json_mode=False)
        return rc, send, wait

    def test_the_attempt_anchor_is_passed_down(self, env, tmp_path):
        before = time.time()
        _, _, wait = self._run(tmp_path, {"status": "complete", "summary": "ok"})
        since = wait.call_args.kwargs["transcript_since"]
        assert since is not None, "ensure must anchor the window at the attempt"
        assert before <= since <= time.time()

    def test_auth_expired_is_not_retried(self, env, tmp_path):
        """Every retry refuses identically — spending them is pure waste."""
        rc, send, _ = self._run(
            tmp_path,
            {"status": "auth_expired", "summary": "Hermes provider auth expired — …"},
            task_overrides={"retries": 3},
        )
        assert send.call_count == 1, "no re-launch, no re-prompt into a dead login"
        assert rc == ENSURE_EXIT_AUTH_EXPIRED

    def test_an_ordinary_failure_still_retries(self, env, tmp_path):
        rc, send, _ = self._run(
            tmp_path, {"status": "failed", "summary": "nope"},
            task_overrides={"retries": 1})
        assert send.call_count == 2


class TestSchedulerGatesTheRestOfTheFleet:
    def _task(self):
        return SchedulerTask(name="ai-morning-briefing", project="/tmp/p",
                             session="ai-briefing", task="briefing")

    def test_dispatch_is_skipped_while_the_outage_is_fresh(self, env):
        auth_expired.record_outage({"session": "memory-manager", "provider": "nous",
                                    "code": "subscription_expired"})
        prior = TaskState(last_run=datetime(2026, 8, 4, tzinfo=timezone.utc), run_count=7)

        from agentwire.scheduler.dispatch import _dispatch_ensure_task

        with patch("agentwire.scheduler.dispatch._dispatch_worktree_task") as wt, \
             patch("agentwire.scheduler.dispatch._dispatch_inplace_task") as ip:
            state = _dispatch_ensure_task(None, self._task(), prior)

        wt.assert_not_called()
        ip.assert_not_called(), "no session launch, no prompt, no ceiling to burn"
        assert state.last_status == "auth_expired"
        assert state.last_run == prior.last_run, "stays eligible the moment re-auth runs"
        assert state.run_count == prior.run_count

    def test_dispatch_resumes_once_the_outage_goes_stale(self, env):
        auth_expired.record_outage({"session": "memory-manager"})
        state = auth_expired.read_state()
        state["last_seen"] = (
            auth_expired._now() - auth_expired.OUTAGE_TTL - auth_expired.OUTAGE_TTL
        ).isoformat()
        auth_expired.write_state(state)

        from agentwire.scheduler.dispatch import _dispatch_ensure_task

        with patch("agentwire.scheduler.dispatch._dispatch_inplace_task",
                   return_value=TaskState(last_status="complete")) as ip:
            result = _dispatch_ensure_task(None, self._task(), TaskState())
        ip.assert_called_once(), "one probe is allowed through"
        assert result.last_status == "complete"

    def test_no_outage_means_no_change_in_behaviour(self, env):
        from agentwire.scheduler.dispatch import _dispatch_ensure_task

        with patch("agentwire.scheduler.dispatch._dispatch_inplace_task",
                   return_value=TaskState(last_status="complete")) as ip:
            _dispatch_ensure_task(None, self._task(), TaskState())
        ip.assert_called_once()


class TestExitCodeIsDistinct:
    def test_auth_expired_is_not_a_timeout(self, env):
        assert status_to_exit_code("auth_expired") == ENSURE_EXIT_AUTH_EXPIRED == 8
        assert status_to_exit_code("incomplete") == 2
        assert status_to_exit_code("usage_limit") == 7

    def test_the_scheduler_maps_the_code_back(self, env):
        assert _EXIT_TO_STATUS[ENSURE_EXIT_AUTH_EXPIRED] == "auth_expired"
