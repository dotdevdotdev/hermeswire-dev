"""Tests for session-context headroom + auto-management (issue #8 Hermes rewrite).

The Claude Code footer-bar scraper (``parse_context_bar`` / ``parse_model``) is
gone — Hermes has no ``[███░░] NN%`` footer. Headroom is now computed from the
Hermes session store (``sessions`` table token columns + the model's context
window), read via ``session_context``. These tests cover the store-driven
headroom math, the no-store-row fail-safe, the two-tick low-headroom
persistence, and the policy/``/clear``-vs-``/compress`` routing.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermeswire import session_context as sc
from hermeswire.config import CustomServiceConfig


@pytest.fixture(autouse=True)
def isolated_low_markers(tmp_path, monkeypatch):
    """Point low-headroom markers + events into tmp; never write into $HOME."""
    monkeypatch.setattr(sc, "_LOW_MARKER_DIR", tmp_path / "session-context-low")
    monkeypatch.setattr(sc, "EVENTS_FILE", tmp_path / "session-context-events.jsonl")


def _row(model: str | None = "gpt-5", input_tokens=0, output_tokens=0):
    return {
        "id": "abc123",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


# ── Headroom math (store → %) ────────────────────────────────────────────────


def test_headroom_pct_basic():
    with patch.object(sc, "_context_length", return_value=100_000):
        assert sc._headroom_pct(_row(input_tokens=20_000)) == 80
        assert sc._headroom_pct(_row(input_tokens=80_000, output_tokens=20_000)) == 0
        assert sc._headroom_pct(_row(input_tokens=0, output_tokens=0)) == 100


def test_headroom_pct_clamped_and_unknown():
    with patch.object(sc, "_context_length", return_value=100_000):
        # Over the window clamps to 0%, never negative.
        assert sc._headroom_pct(_row(input_tokens=150_000)) == 0
        # No model → unknown, never "0%".
        assert sc._headroom_pct(_row(model=None)) is None
    with patch.object(sc, "_context_length", return_value=None):
        assert sc._headroom_pct(_row()) is None


def test_agent_command_regex():
    # Hermes REPL panes: hermes / uv / python3*
    assert sc._is_agent_command("hermes")
    assert sc._is_agent_command("uv")
    assert sc._is_agent_command("python3")
    assert sc._is_agent_command("python3.13")
    # Legacy Claude + shells are NOT agents anymore (#7).
    assert not sc._is_agent_command("node")
    assert not sc._is_agent_command("claude")
    assert not sc._is_agent_command("2.1.185")
    assert not sc._is_agent_command("zsh")
    assert not sc._is_agent_command("")


# ── session_context (store-driven) ───────────────────────────────────────────


def _sc(command="hermes", row=None, ctx_len=100_000, threshold=20):
    with patch.object(sc, "_pane_command", return_value=command), patch.object(
        sc, "_session_row", return_value=row
    ), patch.object(sc, "_context_length", return_value=ctx_len):
        return sc.session_context("s", warn_threshold=threshold)


def test_daemon_skipped_gracefully():
    c = _sc(command="zsh")
    assert c.is_agent is False
    assert c.remaining_pct is None
    assert c.model is None
    assert c.flagged is False
    assert "daemon" in c.note


def test_agent_no_store_row_is_unknown_not_zero():
    # A launched-but-never-prompted pane has no store row → unknown/skip, never 0%.
    c = _sc(command="hermes", row=None)
    assert c.is_agent is True
    assert c.remaining_pct is None
    assert c.flagged is False
    assert "no store row" in c.note


def test_agent_healthy_from_store():
    c = _sc(command="uv", row=_row(input_tokens=20_000))
    assert c.is_agent is True
    assert c.remaining_pct == 80
    assert c.model == "gpt-5"
    assert c.flagged is False


def test_agent_flagged_when_low():
    c = _sc(command="python3.13", row=_row(input_tokens=80_000))
    assert c.is_agent is True
    assert c.remaining_pct == 20
    assert c.flagged is True  # 20 <= 20 warn threshold
    assert "LOW" in c.note


def test_agent_no_model_is_unknown_headroom():
    c = _sc(command="hermes", row=_row(model=None))
    assert c.remaining_pct is None
    assert c.flagged is False
    assert "unknown headroom" in c.note


def test_threshold_boundary_inclusive():
    at = _sc(row=_row(input_tokens=80_000))  # 20% remaining
    assert at.flagged is True
    above = _sc(row=_row(input_tokens=79_000))  # 21% remaining
    assert above.flagged is False


# ── Policy resolution ─────────────────────────────────────────────────────────


def _cfg(policies=None):
    """Minimal fake Config for resolve_policy (only the fields it reads)."""
    return SimpleNamespace(
        session_context=SimpleNamespace(
            warn_remaining_pct=20, auto_enabled=True, policies=policies or {},
        ),
        services=SimpleNamespace(custom=[]),
    )


def _svc(name, policy="none"):
    return CustomServiceConfig(name=name, context_policy=policy)


def test_resolve_policy_service_default_on():
    cfg = _cfg()
    with patch.object(sc, "_warn_threshold", return_value=20), patch(
        "hermeswire.services.registry",
        return_value=[_svc("hermeswire-notifications", "clear")],
    ):
        assert sc.resolve_policy("hermeswire-notifications", cfg) == "clear"


def test_resolve_policy_config_override_wins():
    cfg = _cfg(policies={"hermeswire-notifications": "compact"})
    with patch(
        "hermeswire.services.registry",
        return_value=[_svc("hermeswire-notifications", "clear")],
    ):
        # Explicit per-session override beats the service-registry default.
        assert sc.resolve_policy("hermeswire-notifications", cfg) == "compact"


def test_resolve_policy_unknown_session_is_none():
    cfg = _cfg()
    with patch("hermeswire.services.registry", return_value=[]):
        assert sc.resolve_policy("some-random-session", cfg) == "none"


def test_resolve_policy_invalid_service_value_is_none():
    cfg = _cfg()
    with patch(
        "hermeswire.services.registry",
        return_value=[_svc("svc", "nonsense")],
    ):
        assert sc.resolve_policy("svc", cfg) == "none"


# ── act_on_session ────────────────────────────────────────────────────────────


def _ctx_obj(remaining, is_agent=True, flagged=None):
    if flagged is None:
        flagged = remaining is not None and remaining <= 20
    return sc.SessionContext(
        session="s", pane=0, is_agent=is_agent, remaining_pct=remaining,
        model="gpt-5", flagged=flagged, note="",
    )


def test_act_skips_when_no_policy():
    r = sc.act_on_session("s", "none", threshold=20)
    assert r["acted"] is False
    assert r["skipped"] == "no_policy"


def test_act_skips_when_above_threshold():
    with patch.object(sc, "session_context", return_value=_ctx_obj(80)):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["skipped"] == "above_threshold"


def test_act_skips_when_unknown():
    # Agent but no store row → unknown, never auto-/clear.
    with patch.object(sc, "session_context", return_value=_ctx_obj(None, is_agent=True)):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["skipped"] == "unknown"


def test_act_first_low_sighting_defers_and_marks():
    # Low headroom on a single read only records — no action until it persists.
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["deferred"] == "first_low_sighting"
    assert sc._low_seen("s") is True


def test_act_acts_after_two_low_ticks():
    # First tick marks; second tick (marker present) actually /clears.
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)):
        assert sc.act_on_session("s", "clear", threshold=20)["deferred"] == "first_low_sighting"
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)), patch(
        "hermeswire.prompt_router.safe_deliver", return_value=(True, "delivered")
    ) as deliver:
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is True
    assert r["command"] == "/clear"
    deliver.assert_called_once_with("s", 0, "/clear")
    assert sc._low_seen("s") is False  # marker cleared after a successful clear


def test_act_compact_routes_compress():
    sc._mark_low("s")
    with patch.object(sc, "session_context", return_value=_ctx_obj(5)), patch(
        "hermeswire.prompt_router.safe_deliver", return_value=(True, "delivered")
    ) as deliver:
        r = sc.act_on_session("s", "compact", threshold=20)
    assert r["acted"] is True
    assert r["command"] == "/compress"
    deliver.assert_called_once_with("s", 0, "/compress")


def test_act_defers_when_safe_deliver_refuses():
    sc._mark_low("s")
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)), patch(
        "hermeswire.prompt_router.safe_deliver",
        return_value=(False, "delivery_unverified"),
    ):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["deferred"] == "delivery_unverified"


def test_act_defers_when_send_raises():
    sc._mark_low("s")
    with patch.object(sc, "session_context", return_value=_ctx_obj(10)), patch(
        "hermeswire.prompt_router.safe_deliver", side_effect=RuntimeError("boom")
    ):
        r = sc.act_on_session("s", "clear", threshold=20)
    assert r["acted"] is False
    assert r["deferred"] == "send_failed"


# ── tick ──────────────────────────────────────────────────────────────────────


def test_tick_skips_when_auto_disabled():
    cfg = _cfg()
    cfg.session_context.auto_enabled = False
    with patch("hermeswire.config.get_config", return_value=cfg):
        assert sc.tick() == {"skipped": "disabled"}


def test_tick_acts_only_on_opted_in_sessions():
    cfg = _cfg()
    calls = []

    def fake_act(session, policy, threshold):
        calls.append((session, policy))
        return {"session": session, "acted": True, "command": "/clear",
                "remaining_pct": 10, "policy": policy}

    def fake_resolve(session, c):
        return "clear" if session == "hermeswire-notifications" else "none"

    with patch("hermeswire.config.get_config", return_value=cfg), patch.object(
        sc, "_list_local_sessions",
        return_value=["hermeswire-notifications", "fragmentz", "hermeswire-scheduler"],
    ), patch.object(sc, "resolve_policy", side_effect=fake_resolve), patch.object(
        sc, "act_on_session", side_effect=fake_act
    ):
        result = sc.tick()

    # Only the opted-in session was evaluated/acted on — never fragmentz.
    assert calls == [("hermeswire-notifications", "clear")]
    assert [e["session"] for e in result["acted"]] == ["hermeswire-notifications"]
