"""`_guarded_launch_command` (#739, #743) — the pane's cd+agent-launch line
must guard a missing worktree dir instead of crashing the agent into a bare
shell nobody reaps, and (#743) route the alert to a real parent — not just
the owner's email — when one is recorded in the launch env."""

from hermeswire.core import _guarded_launch_command


class TestGuardedLaunchCommand:
    def test_cd_success_runs_agent(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        assert cmd.startswith("cd /tmp/wt || {")
        # Braced: the agent command is several statements since #901, and an
        # unbraced `;` would let `claude` run after a failed cd.
        assert cmd.endswith("&& { claude --flag; }")

    def test_cd_failure_exits_without_running_agent(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        # The guard clause exits the shell on cd failure — the agent segment
        # is only reachable via the `&&` after a successful cd.
        assert "exit 1" in cmd
        assert 'HERMESWIRE_UNATTENDED' in cmd

    def test_bare_posture_has_no_agent_segment(self):
        cmd = _guarded_launch_command("/tmp/wt", None)
        assert cmd == (
            'cd /tmp/wt || { echo "hermeswire: worktree missing at launch, '
            'aborting: /tmp/wt" >&2; [ -n "$HERMESWIRE_CREATED_BY" ] && hermeswire '
            'msg send --to "$HERMESWIRE_CREATED_BY" --kind escalation --subject '
            '"hermeswire: worktree missing at launch — '
            '${HERMESWIRE_SESSION_NAME:-unknown session}" --body "cd failed at '
            'launch: /tmp/wt" >/dev/null 2>&1; [ -z "$HERMESWIRE_CREATED_BY" ] && '
            '[ "$HERMESWIRE_UNATTENDED" = "1" ] && hermeswire email --subject '
            '"hermeswire: worktree missing — '
            '${HERMESWIRE_SESSION_NAME:-unknown session}" --body "cd failed at '
            'launch: /tmp/wt" >/dev/null 2>&1; exit 1; }'
        )

    def test_path_with_spaces_is_quoted(self):
        cmd = _guarded_launch_command("/tmp/my wt", "claude")
        assert "cd '/tmp/my wt'" in cmd

    def test_alert_guarded_on_unattended_env_var(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert '[ "$HERMESWIRE_UNATTENDED" = "1" ] && hermeswire email' in cmd


class TestParentEscalation:
    """#743: a real parent (`$HERMESWIRE_CREATED_BY`, stamped by
    `_set_session_name_env` only for a non-root session) gets the crash
    routed to its msg inbox; the owner-email fallback still fires only when
    there's no parent."""

    def test_parent_notify_clause_present(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert (
            '[ -n "$HERMESWIRE_CREATED_BY" ] && hermeswire msg send '
            '--to "$HERMESWIRE_CREATED_BY" --kind escalation'
        ) in cmd

    def test_email_fallback_branch_still_present(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude")
        assert (
            '[ -z "$HERMESWIRE_CREATED_BY" ] && [ "$HERMESWIRE_UNATTENDED" = "1" ] '
            '&& hermeswire email'
        ) in cmd

    def test_path_still_quoted_with_parent_escalation_present(self):
        cmd = _guarded_launch_command("/tmp/my wt", "claude")
        assert "cd '/tmp/my wt'" in cmd
        assert '$HERMESWIRE_CREATED_BY' in cmd

    def test_agent_still_gated_behind_successful_cd(self):
        cmd = _guarded_launch_command("/tmp/wt", "claude --flag")
        assert cmd.endswith("; exit 1; } && { claude --flag; }")

    def test_a_multi_statement_agent_command_stays_inside_the_guard(self):
        """#901's prelude made the agent command multi-statement. Every one of
        its statements has to sit behind the `&&`, or a failed cd runs the
        agent from the wrong directory — the zombie this guard exists for.
        Verified against a real shell in test_launch_line_reentry.py."""
        cmd = _guarded_launch_command("/tmp/wt", "setup=1; claude --flag")
        assert cmd.endswith("&& { setup=1; claude --flag; }")
