"""Tests for hermeswire/hooks/idle-handler.sh — the Hermes ``on_session_end`` observer.

Issue #12: Hermes has no idle/Notification event, so the Claude idle-handler's
two-pass summary + ``/exit`` + loop-iteration dance is gone. The file now fires
on Hermes's real ``on_session_end`` lifecycle transition and does exactly three
things: read the summary the agent already wrote (instructed via the launch
prompt), queue it to the parent through the queue-processor protocol, and
remove the task-context file — the completion signal ``ensure``'s wait blocks
on. The usage-limit / prompt-routing / cohort guards that the old idle hook
carried now live in the scheduler (``usage_limit.is_parked`` /
``cohort.blocking``), so they are no longer in this file.

These tests extract the *actual* jq filters / blocks from the hook source and
run them through real jq/bash, so a regression back to the idle model (or the
jq ``//`` boolean coercion from issue #234) fails the suite.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "hermeswire" / "hooks" / "idle-handler.sh"
)


def _source() -> str:
    return HOOK_PATH.read_text()


def _extract_jq_filter(var_name: str) -> str:
    """Pull the jq filter the hook uses to read ``var_name`` from the context file."""
    match = re.search(rf"^\s*{var_name}=\$\(jq -r '([^']+)'", _source(), re.MULTILINE)
    assert match, f"could not find jq read for {var_name} in idle-handler.sh"
    return match.group(1)


pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed"
)


def _run_jq(jq_filter: str, payload: dict) -> str:
    result = subprocess.run(
        ["jq", "-r", jq_filter],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestBooleanContextReads:
    """Stored booleans must round-trip; only null/absent falls back to the default.

    The #234 regression: jq's ``//`` coerced a stored false to true. The hook's
    ``exit_on_complete`` read still carries the explicit null-check idiom.
    """

    @pytest.mark.parametrize("payload,expected", [
        ({"exit_on_complete": False}, "false"),
        ({"exit_on_complete": True}, "true"),
        ({}, "true"),
        ({"exit_on_complete": None}, "true"),
    ])
    def test_exit_on_complete_round_trip(self, payload, expected):
        jq_filter = _extract_jq_filter("exit_on_complete")
        assert _run_jq(jq_filter, payload) == expected


class TestSessionEndTrigger:
    """The observer fires on Hermes ``on_session_end``, not Claude idle."""

    def test_reads_session_end_fields_not_notification(self):
        source = _source()
        # on_session_end payload keys, not the old Notification idle_prompt.
        assert "session_id" in source
        assert "completed" in source
        # No idle-timer trigger remains.
        assert "idle_prompt" not in source
        assert "notification_type" not in source

    def test_no_idle_artifacts_remain(self):
        source = _source()
        # No /exit, no tmux kill-session, no two-pass sleep-and-reprompt loop.
        assert '"/exit"' not in source
        assert "kill-session" not in source
        assert "kill --pane" not in source
        assert "loop_review" not in source


class TestContextCleanupOnSessionEnd:
    """The context file is removed on session end — the completion signal
    ``wait_for_completion_signal`` (REPL path) blocks on."""

    def test_context_is_removed(self):
        source = _source()
        assert 'rm -f "$task_context_file"' in source

    def test_cleanup_precedes_queueing(self):
        source = _source()
        cleanup = source.find('rm -f "$task_context_file"')
        queue = source.find("queue_file=")
        assert cleanup != -1 and queue != -1
        # Cleanup must happen before the summary is queued, so a stale context
        # never outlives the run that produced it.
        assert cleanup < queue


class TestQueueProtocolPreserved:
    """The observer queues the summary through the same mkdir-lock + jsonl
    protocol the queue-processor.sh drains — an append racing the processor's
    head-trim must not lose a message."""

    def test_mkdir_lock_and_jsonl_append(self):
        source = _source()
        assert 'lock_dir="${queue_dir}/${tmux_session}.lock"' in source
        assert "mkdir \"$lock_dir\"" in source
        assert "jq -Rs ." in source
        assert '>> "$queue_file"' in source

    def test_starts_queue_processor(self):
        source = _source()
        assert 'queue-processor.sh' in source


class TestUsageLimitAndCohortGuardsRemoved:
    """The park / cohort guards now live in the scheduler (Python), not here."""

    def test_no_usage_limit_guard(self):
        assert "usage-limit/${tmux_session}.json" not in _source()

    def test_no_cohort_guard(self):
        assert "cohort_file=" not in _source()
