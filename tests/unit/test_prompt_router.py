"""Tests for prompt_router — Hermes hook/callback routing, markers, delivery.

Hermes has no screen-scrapable dialog: prompts arrive via the ``pre_tool_call``
hook / terminal-tool approval callback, and the marker written by
``route_prompt`` is the only record of a blocked pane. These tests exercise that
marker-based model plus the delivery/agent-detection primitives that remain.
"""

import json
from types import SimpleNamespace

import pytest

from hermeswire import prompt_router
from hermeswire.prompt_router import PromptInfo, parse_ask_options

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def router_home(tmp_path, monkeypatch):
    """Isolate marker state + events under a temp dir."""
    monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
    monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "events.jsonl")
    return tmp_path


def _events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestParseAskOptions:
    def test_labels_and_descriptions(self):
        block = "❯ 1. Teal\n     matches brand\n  2. Orange\n     high contrast\n"
        assert parse_ask_options(block) == [
            {"number": 1, "label": "Teal", "description": "matches brand"},
            {"number": 2, "label": "Orange", "description": "high contrast"},
        ]

    def test_strips_ansi(self):
        block = "\x1b[1m❯ 1. Yes\x1b[0m\n  2. No\n"
        assert [o["label"] for o in parse_ask_options(block)] == ["Yes", "No"]


class TestResolveParent:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hermeswire.core.CONFIG_DIR", tmp_path)
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: True)
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: None)
        self.tmp_path = tmp_path

    def _write_creator(self, session, creator):
        d = self.tmp_path / "sessions" / session
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            '{"created_by": "%s", "created_via": "new"}' % creator
        )

    def test_worker_pane_routes_to_pane_zero(self):
        assert prompt_router.resolve_parent("orch", 3) == ("orch", 0)

    def test_creator_metadata_wins(self, monkeypatch):
        self._write_creator("child", "orch")
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "yml-parent")
        assert prompt_router.resolve_parent("child", 0) == ("orch", 0)

    def test_yml_parent_fallback(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_parent_from_config", lambda p: "yml-parent")
        assert prompt_router.resolve_parent("child", 0) == ("yml-parent", 0)

    def test_no_parent(self):
        assert prompt_router.resolve_parent("standalone", 0) is None

    def test_self_creator_skipped(self):
        self._write_creator("child", "child")
        assert prompt_router.resolve_parent("child", 0) is None


class TestIsAgentPane:
    def test_unambiguous_agent_commands(self, monkeypatch):
        for cmd in ("node", "claude", "hermes", "2.1.170"):
            monkeypatch.setattr(prompt_router, "pane_command", lambda s, p, c=cmd: c)
            assert prompt_router.is_agent_pane("s", 0) is True, cmd

    def test_non_agent_commands(self, monkeypatch):
        for cmd in ("zsh", "bash", "vim", "less", ""):
            monkeypatch.setattr(prompt_router, "pane_command", lambda s, p, c=cmd: c)
            assert prompt_router.is_agent_pane("s", 0) is False, cmd

    def test_python_disambiguated_by_cmdline(self, monkeypatch):
        # python3* is ALSO what daemons report; the process cmdline decides.
        monkeypatch.setattr(prompt_router, "pane_command", lambda s, p: "python3.13")
        monkeypatch.setattr(prompt_router, "_pane_runs_hermes", lambda s, p: True)
        assert prompt_router.is_agent_pane("s", 0) is True
        monkeypatch.setattr(prompt_router, "_pane_runs_hermes", lambda s, p: False)
        assert prompt_router.is_agent_pane("s", 0) is False


class TestPaneRunsHermes:
    def _cmdlines(self, by_pid):
        return lambda pid: by_pid.get(pid, "")

    def test_agent_admitted_despite_skill_flags(self, monkeypatch):
        # The agent's own -s hermeswire-<role> flags contain "hermeswire"; a
        # substring discriminator would wrongly reject a live agent. The
        # word-boundary match on the hermes BINARY admits it.
        monkeypatch.setattr(prompt_router, "_pane_pid", lambda s, p: "100")
        monkeypatch.setattr(prompt_router, "_pane_child_pids", lambda pid: ["200"])
        monkeypatch.setattr(
            prompt_router,
            "_cmdline_of",
            self._cmdlines(
                {
                    "100": "-zsh",
                    "200": "python /Users/dotdev/.local/bin/hermes chat --cli "
                    "--source tool --accept-hooks --yolo "
                    "-s hermeswire-worker-worktree,hermeswire-contributor",
                }
            ),
        )
        assert prompt_router._pane_runs_hermes("s", 0) is True

    def test_daemon_rejected(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_pane_pid", lambda s, p: "100")
        monkeypatch.setattr(prompt_router, "_pane_child_pids", lambda pid: [])
        monkeypatch.setattr(
            prompt_router,
            "_cmdline_of",
            self._cmdlines(
                {"100": "python /Users/dotdev/.local/bin/hermeswire portal start"}
            ),
        )
        assert prompt_router._pane_runs_hermes("s", 0) is False

    def test_shell_rejected(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_pane_pid", lambda s, p: "100")
        monkeypatch.setattr(prompt_router, "_pane_child_pids", lambda pid: [])
        monkeypatch.setattr(prompt_router, "_cmdline_of", self._cmdlines({"100": "-zsh"}))
        assert prompt_router._pane_runs_hermes("s", 0) is False


class TestInputBoxContent:
    def test_draft_after_glyph(self):
        assert prompt_router.input_box_content("❯ build the app") == "build the app"

    def test_empty_prompt(self):
        assert prompt_router.input_box_content("❯") == ""

    def test_agent_running_state(self):
        # ⚕ before the glyph means "agent running" but no draft.
        assert prompt_router.input_box_content("⚕ ❯") == ""

    def test_no_prompt_renders_none(self):
        assert prompt_router.input_box_content("no prompt here") is None

    def test_last_prompt_line_wins(self):
        cap = "❯ old stuff\nsome output\n❯ current draft"
        assert prompt_router.input_box_content(cap) == "current draft"


class TestScreenShowsLiveMenu:
    def test_always_false(self):
        # Hermes has no screen-scrapable menu; the gate is inert.
        assert prompt_router.screen_shows_live_menu("anything") is False
        assert prompt_router.screen_shows_live_menu("") is False


class TestSafeDeliver:
    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: True)
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: True)
        self.sent = []
        import hermeswire.session_ready as session_ready

        monkeypatch.setattr(
            session_ready, "send_verified",
            lambda session, message, **kw: self.sent.append((session, message)) or True,
        )

    def test_delivers_to_safe_target(self):
        ok, reason = prompt_router.safe_deliver("orch", 0, "hi")
        assert ok and reason == "delivered"
        assert self.sent == [("orch", "hi")]

    def test_refuses_dead_session(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_session_exists", lambda s: False)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "target_gone")

    def test_refuses_shell_pane(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "is_agent_pane", lambda s, p: False)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "target_not_agent")

    def test_unverified_send_reported(self, monkeypatch):
        import hermeswire.session_ready as session_ready

        monkeypatch.setattr(session_ready, "send_verified", lambda *a, **kw: False)
        assert prompt_router.safe_deliver("orch", 0, "x") == (False, "delivery_unverified")


class TestMarkers:
    def test_roundtrip(self, router_home):
        prompt_router.write_marker("sess", 1, kind="question", hash="abc")
        marker = prompt_router.read_marker("sess", 1)
        assert marker["kind"] == "question" and marker["hash"] == "abc"

    def test_clear(self, router_home):
        prompt_router.write_marker("sess", 1, kind="question", hash="abc")
        prompt_router.clear_marker("sess", 1)
        assert prompt_router.read_marker("sess", 1) is None

    def test_worktree_session_names_nest(self, router_home):
        prompt_router.write_marker("proj/branch", 0, kind="question", hash="x")
        assert prompt_router.read_marker("proj/branch", 0)["hash"] == "x"


class TestBuildMessage:
    def test_message_paraphrases_never_looks_live(self):
        info = PromptInfo(
            kind="permission", question="Hermes wants to run: git push",
            options=[{"number": 1, "label": "once"}], summary="run: git push",
        )
        message = prompt_router.build_message("worker", 0, info)
        assert "❯" not in message
        assert "Esc to cancel" not in message
        assert message.startswith("[PROMPT from worker pane 0] kind=permission")

    def test_message_carries_answer_contract(self):
        info = PromptInfo(kind="question", question="Ship it?",
                          options=[{"number": 1, "label": "Yes"}])
        message = prompt_router.build_message("child", 0, info)
        assert f"--expect {info.content_hash()}" in message
        assert "hermeswire prompts answer -s 'child' --pane 0" in message
        assert "1=Yes" in message


class TestRoutePrompt:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.delivered = []
        monkeypatch.setattr(
            prompt_router, "resolve_parent", lambda s, p, pp=None: ("orch", 0)
        )
        monkeypatch.setattr(
            prompt_router, "safe_deliver",
            lambda ts, tp, text: self.delivered.append((ts, text)) or (True, "delivered"),
        )

    def _info(self):
        return PromptInfo(kind="question", question="Ship it?",
                          options=[{"number": 1, "label": "Yes"}])

    def test_routes_and_marks(self):
        parent = prompt_router.route_prompt("child", 0, self._info())
        assert parent == "orch"
        assert self.delivered[0][0] == "orch"
        marker = prompt_router.read_marker("child", 0)
        assert marker["status"] == "delivered" and marker["notified_at"]

    def test_no_parent_marks_without_delivery(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        assert prompt_router.route_prompt("solo", 0, self._info()) is None
        assert self.delivered == []
        assert prompt_router.read_marker("solo", 0)["status"] == "no_parent"

    def test_deferred_delivery_marks_unnotified(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "safe_deliver", lambda *a: (False, "target_not_agent"))
        assert prompt_router.route_prompt("child", 0, self._info()) is None
        assert prompt_router.read_marker("child", 0)["notified_at"] is None

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            prompt_router, "resolve_parent",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert prompt_router.route_prompt("child", 0, self._info()) is None


class TestNotifyPermissionRequest:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.routed = []
        monkeypatch.setattr(
            prompt_router, "route_prompt",
            lambda s, p, info, source="sweep", project_path=None: self.routed.append(
                (info, source)
            ) or "orch",
        )

    def test_clarify_maps_to_question(self):
        prompt_router.notify_permission_request("child", 0, {
            "tool_name": "clarify",
            "tool_input": {"question": "Ship it?", "choices": ["Yes", "No"]},
        })
        info, source = self.routed[0]
        assert info.kind == "question" and source == "hook"
        assert info.question == "Ship it?"
        assert [o["label"] for o in info.options] == ["Yes", "No"]

    def test_command_maps_to_permission(self):
        prompt_router.notify_permission_request("child", 0, {
            "tool_name": "Bash", "tool_input": {"command": "git push"},
        })
        info, source = self.routed[0]
        assert info.kind == "permission" and source == "hook"
        assert "git push" in info.question
        assert [o["label"] for o in info.options] == ["once", "session", "always", "deny"]


class TestAnswer:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.sent_keys = []
        monkeypatch.setattr(
            prompt_router, "_tmux",
            lambda args, timeout=5: self.sent_keys.append(args)
            or SimpleNamespace(returncode=0, stdout=""),
        )

    def test_answers_matching_marker(self):
        prompt_router.write_marker("child", 0, kind="question", hash="h1")
        ok, msg = prompt_router.answer("child", 0, "h1", ["2"])
        assert ok
        assert self.sent_keys == [["send-keys", "-t", "child.0", "2"]]
        assert prompt_router.read_marker("child", 0) is None

    def test_refuses_when_no_marker(self):
        ok, msg = prompt_router.answer("child", 0, "whatever", ["2"])
        assert not ok and "no live prompt" in msg
        assert self.sent_keys == []

    def test_refuses_when_hash_mismatch(self):
        prompt_router.write_marker("child", 0, kind="question", hash="h1")
        ok, msg = prompt_router.answer("child", 0, "different", ["1"])
        assert not ok and "DIFFERENT prompt" in msg
        assert self.sent_keys == []


class TestSweep:
    @pytest.fixture(autouse=True)
    def _wire(self, router_home, monkeypatch):
        self.routed = []
        monkeypatch.setattr(prompt_router, "_router_config", lambda: (True, set()))
        monkeypatch.setattr(
            prompt_router, "list_panes",
            lambda: [SimpleNamespace(session="child", pane=0, command="hermes", path="/x")],
        )
        monkeypatch.setattr(
            prompt_router, "route_prompt",
            lambda s, p, info, **k: self.routed.append((s, p, info)) or "orch",
        )

    def _marker_fields(self, **over):
        base = dict(kind="question", question="Q?", options=[], summary="")
        base.update(over)
        return base

    def test_freshly_notified_marker_is_active(self):
        now = prompt_router._now().isoformat()
        prompt_router.write_marker(
            "child", 0, status="delivered", notified_at=now, detected_at=now,
            **self._marker_fields(),
        )
        result = prompt_router.sweep()
        assert [e["session"] for e in result["active"]] == ["child"]
        assert self.routed == []

    def test_renotifies_after_ttl(self):
        from datetime import timedelta as td

        old = (prompt_router._now() - prompt_router.RENOTIFY_TTL - td(minutes=1)).isoformat()
        now = prompt_router._now().isoformat()
        prompt_router.write_marker(
            "child", 0, status="delivered", notified_at=old, detected_at=now,
            **self._marker_fields(),
        )
        result = prompt_router.sweep()
        assert [e["session"] for e in result["routed"]] == ["child"]
        assert len(self.routed) == 1

    def test_never_notified_marker_is_retried(self):
        now = prompt_router._now().isoformat()
        prompt_router.write_marker(
            "child", 0, status="deferred", notified_at=None, detected_at=now,
            **self._marker_fields(),
        )
        result = prompt_router.sweep()
        assert [e["session"] for e in result["routed"]] == ["child"]

    def test_gone_pane_gcs_after_ttl(self):
        from datetime import timedelta as td

        old = (prompt_router._now() - prompt_router.MARKER_GC_TTL - td(minutes=1)).isoformat()
        prompt_router.write_marker(
            "dead", 0, status="delivered", notified_at=None, detected_at=old,
            **self._marker_fields(),
        )
        prompt_router.sweep()  # "dead" not in list_panes -> GC
        assert prompt_router.read_marker("dead", 0) is None

    def test_disabled_config(self, monkeypatch):
        monkeypatch.setattr(prompt_router, "_router_config", lambda: (False, set()))
        assert all(v == [] for v in prompt_router.sweep().values())


class TestBlockedPanes:
    def test_reads_markers(self, router_home, monkeypatch):
        monkeypatch.setattr(prompt_router, "_router_config", lambda: (True, set()))
        monkeypatch.setattr(
            prompt_router, "list_panes",
            lambda: [SimpleNamespace(session="child", pane=0, command="hermes", path="/x")],
        )
        prompt_router.write_marker(
            "child", 0, kind="question", question="Q?", options=[], summary="",
            status="delivered", notified_at=prompt_router._now().isoformat(),
            detected_at=prompt_router._now().isoformat(), parent="orch",
        )
        blocked = prompt_router.blocked_panes()
        assert len(blocked) == 1
        assert blocked[0]["session"] == "child"
        assert blocked[0]["status"] == "waiting"
        assert blocked[0]["question"] == "Q?"


class TestRecordSessionCreator:
    def _agent(self):
        from hermeswire.core import AgentCommand
        return AgentCommand(command="hermes chat --cli", posture="bypass")

    def test_records_and_merges(self, tmp_path, monkeypatch):
        import hermeswire.__main__ as cli

        monkeypatch.setattr("hermeswire.core.CONFIG_DIR", tmp_path)
        cli.store_session_metadata("child", {"existing": "kept"})
        cli.record_session_launch("child", self._agent(), tmp_path,
                                  created_by="orch", created_via="new")
        meta = cli.load_session_metadata("child")
        assert meta["created_by"] == "orch"
        assert meta["existing"] == "kept"
