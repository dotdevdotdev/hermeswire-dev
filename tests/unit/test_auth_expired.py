"""Hermes provider auth failure is detected, named, escalated once, gated (#13).

The original (#906) keyed on a Claude transcript's ``error: authentication_failed``
field. Hermes has no transcript and no ``/login`` — auth is provider-based
(``hermes auth``, ``~/.hermes/auth.json``), and failure surfaces as a structured
``AuthError(provider=…, code=…, relogin_required=…)`` on a ``hermes -z``/``-q``
stderr (exit 1) or via ``hermes auth status <provider>``.

The fixtures here are built from the Hermes *field shape*, not a hand-written
rendered phrase, for the same reason the original fixtures were verbatim: a
detector written against a prettier invented string would pass and still miss
the real thing. The traps this file is built to fall into on purpose:

* ``AUTH_ERROR_STDERR`` is the shape of a hard failure — ``relogin_required``
  and a keyed-on ``code``. A detector matching a fixed rendered phrase would
  silently stop working on a rewording.
* ``TRANSIENT_STDERR`` is ``codex_rate_limited`` — structurally identical,
  reaches the predicate, and only the code check stops it from gating the
  whole machine on a transient blip.
* ``HARD_AUTH_CODES`` vs ``TRANSIENT_CODES``: the whole argument for keying on
  the structured code, not the text.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentwire import auth_expired

# The shape of a hard Hermes auth failure on `hermes -z` stderr (exit 1).
AUTH_ERROR_STDERR = (
    "hermes -z: agent failed: AuthError(message='nous: subscription expired', "
    "provider='nous', code='subscription_expired', relogin_required=True)"
)

# A transient rate-limit — structurally identical, must NOT gate.
TRANSIENT_STDERR = (
    "hermes -z: agent failed: AuthError(message='rate limited', "
    "provider='nous', code='codex_rate_limited', relogin_required=False)"
)

# A plain upstream overload — not auth at all.
OVERLOADED_STDERR = "hermes -z: agent failed: provider returned 500 (overloaded)"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate CONFIG_DIR into tmp_path (the auth-expired state lives there)."""
    monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path / "agentwire")
    return tmp_path


@pytest.fixture
def ok_email():
    ok = SimpleNamespace(success=True, error=None)
    with patch("agentwire.channels.email.send_email", return_value=ok):
        yield


# --------------------------------------------------------------------------
# Classification — the hard/transient split
# --------------------------------------------------------------------------


class TestAuthErrorClassification:
    def test_every_hard_code_gates(self):
        for code in auth_expired.HARD_AUTH_CODES:
            assert auth_expired.auth_error_is_hard({"code": code}) is True

    def test_relogin_required_is_hard_even_with_unknown_code(self):
        assert auth_expired.auth_error_is_hard(
            {"code": "something_new", "relogin_required": True}) is True

    def test_transient_codes_do_not_gate(self):
        for code in auth_expired.TRANSIENT_CODES:
            assert auth_expired.auth_error_is_hard({"code": code}) is False

    def test_rate_limit_does_not_gate(self):
        assert auth_expired.auth_error_is_hard({"code": "codex_rate_limited"}) is False

    def test_plain_overload_does_not_gate(self):
        assert auth_expired.auth_error_is_hard({"code": "overloaded"}) is False

    def test_missing_code_and_flag_is_not_hard(self):
        assert auth_expired.auth_error_is_hard({}) is False
        assert auth_expired.auth_error_is_hard({"message": "nope"}) is False

    def test_none_is_not_hard(self):
        assert auth_expired.auth_error_is_hard(None) is False

    def test_object_with_attributes_is_accepted(self):
        err = SimpleNamespace(code="no_usable_credits", relogin_required=False)
        assert auth_expired.auth_error_is_hard(err) is True

    def test_string_relogin_required_is_coerced_by_parser(self):
        parsed = auth_expired.parse_auth_error(AUTH_ERROR_STDERR)
        assert parsed is not None
        assert parsed["relogin_required"] is True


# --------------------------------------------------------------------------
# Detection — parsing the stderr surface
# --------------------------------------------------------------------------


class TestParseAuthError:
    def test_parses_the_real_hard_shape(self):
        parsed = auth_expired.parse_auth_error(AUTH_ERROR_STDERR)
        assert parsed is not None
        assert parsed["provider"] == "nous"
        assert parsed["code"] == "subscription_expired"
        assert parsed["relogin_required"] is True

    def test_parses_without_relogin_required(self):
        text = ("hermes -z: agent failed: AuthError(provider='openrouter', "
                "code='no_usable_credits')")
        parsed = auth_expired.parse_auth_error(text)
        assert parsed is not None
        assert parsed["provider"] == "openrouter"
        assert parsed["code"] == "no_usable_credits"
        assert "relogin_required" not in parsed

    def test_parses_double_quoted_and_bare_values(self):
        parsed = auth_expired.parse_auth_error(
            'AuthError(provider="nous", code="subscription_required")')
        assert parsed is not None
        assert parsed["provider"] == "nous"
        assert parsed["code"] == "subscription_required"

    def test_transient_code_is_parsed_but_not_hard(self):
        parsed = auth_expired.parse_auth_error(TRANSIENT_STDERR)
        assert parsed is not None
        assert parsed["code"] == "codex_rate_limited"
        assert auth_expired.auth_error_is_hard(parsed) is False

    def test_non_auth_error_is_none(self):
        assert auth_expired.parse_auth_error(OVERLOADED_STDERR) is None

    def test_empty_and_none_are_none(self):
        assert auth_expired.parse_auth_error("") is None
        assert auth_expired.parse_auth_error(None) is None

    def test_unclosed_auth_error_is_none(self):
        assert auth_expired.parse_auth_error("AuthError(provider='nous'") is None


# --------------------------------------------------------------------------
# Detection — the pre-flight surface
# --------------------------------------------------------------------------


class TestProbeProviderAuth:
    def _probe(self, stdout, returncode=0):
        with patch("agentwire.auth_expired.subprocess.run",
                   return_value=SimpleNamespace(stdout=stdout, stderr="",
                                                returncode=returncode)) as run:
            result = auth_expired.probe_provider_auth("nous")
        return result

    def test_hard_failure_reports(self):
        result = self._probe(
            '{"logged_in": false, "error": {"provider": "nous", '
            '"code": "subscription_expired"}}')
        assert result == {"provider": "nous", "code": "subscription_expired",
                          "relogin_required": False}

    def test_logged_in_is_healthy(self):
        assert self._probe('{"logged_in": true}') is None

    def test_string_error_is_parsed(self):
        result = self._probe(
            '{"logged_in": false, "error": "AuthError(provider=\'nous\', '
            'code=\'no_usable_credits\')"}')
        assert result == {"provider": "nous", "code": "no_usable_credits",
                          "relogin_required": False}

    def test_transient_error_is_not_an_outage(self):
        assert self._probe(
            '{"logged_in": false, "error": {"provider": "nous", '
            '"code": "codex_rate_limited"}}') is None

    def test_unparseable_output_is_none(self):
        assert self._probe("not json") is None

    def test_subprocess_failure_is_none(self):
        with patch("agentwire.auth_expired.subprocess.run",
                   side_effect=OSError("no hermes")):
            assert auth_expired.probe_provider_auth("nous") is None

    def test_no_provider_is_none(self):
        assert auth_expired.probe_provider_auth("") is None


# --------------------------------------------------------------------------
# Detection — detect() orchestration
# --------------------------------------------------------------------------


class TestDetect:
    def test_stderr_surface_wins(self):
        detail = auth_expired.detect("s", stderr=AUTH_ERROR_STDERR)
        assert detail is not None
        assert detail["source"] == "stderr"
        assert detail["provider"] == "nous"
        assert detail["code"] == "subscription_expired"

    def test_transient_stderr_falls_through_to_preflight(self):
        # Transient stderr + hard pre-flight → pre-flight decides.
        with patch("agentwire.auth_expired.probe_provider_auth",
                   return_value={"provider": "nous", "code": "account_missing"}):
            detail = auth_expired.detect("s", stderr=TRANSIENT_STDERR, provider="nous")
        assert detail is not None
        assert detail["source"] == "preflight"

    def test_preflight_only(self):
        with patch("agentwire.auth_expired.probe_provider_auth",
                   return_value={"provider": "nous", "code": "account_missing"}):
            detail = auth_expired.detect("s", provider="nous")
        assert detail == {"session": "s", "source": "preflight",
                          "provider": "nous", "code": "account_missing"}

    def test_nothing_is_none(self):
        assert auth_expired.detect("s") is None

    def test_no_provider_skips_subprocess(self):
        # The polling path passes no provider: no `hermes auth status` call.
        with patch("agentwire.auth_expired.probe_provider_auth") as probe:
            auth_expired.detect("s", stderr=None, provider=None)
        probe.assert_not_called()


# --------------------------------------------------------------------------
# Active provider resolution
# --------------------------------------------------------------------------


class TestActiveProvider:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_PROVIDER", "openrouter")
        assert auth_expired._active_provider() == "openrouter"

    def test_reads_auth_json(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".hermes").mkdir(parents=True)
        (home / ".hermes" / "auth.json").write_text(
            '{"version": 1, "active_provider": "nous"}')
        monkeypatch.setenv("HOME", str(home))
        assert auth_expired._active_provider() == "nous"

    def test_no_auth_json_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert auth_expired._active_provider() is None


# --------------------------------------------------------------------------
# Outage state: escalate once, and never wedge the board
# --------------------------------------------------------------------------


class TestOutageState:
    def test_first_detection_escalates_and_repeats_do_not(self, env, ok_email):
        auth_expired.record_outage({"session": "a", "provider": "nous",
                                    "code": "subscription_expired"})
        auth_expired.record_outage({"session": "b", "provider": "nous",
                                    "code": "subscription_expired"})
        state = auth_expired.read_state()
        assert state["sessions"] == ["a", "b"]
        assert state["provider"] == "nous"
        assert state["code"] == "subscription_expired"

    def test_escalation_resumes_after_the_ttl(self, env, ok_email):
        with patch("agentwire.channels.email.send_email",
                   return_value=SimpleNamespace(success=True, error=None)) as mail:
            auth_expired.record_outage({"session": "a"})
            state = auth_expired.read_state()
            stale = auth_expired._now() - auth_expired.ESCALATE_TTL - timedelta(minutes=1)
            state["escalated_at"] = stale.isoformat()
            auth_expired.write_state(state)
            auth_expired.record_outage({"session": "a"})
        assert mail.call_count == 2

    def test_detected_at_is_carried_forward_not_refreshed(self, env, ok_email):
        first = auth_expired.record_outage({"session": "a"})
        auth_expired.record_outage({"session": "a"})
        assert auth_expired.read_state()["detected_at"] == first["detected_at"]

    def test_a_failing_escalation_still_records_the_outage(self, env):
        with patch("agentwire.channels.email.send_email",
                   side_effect=RuntimeError("no key")):
            auth_expired.record_outage({"session": "a"})
        assert auth_expired.read_state() is not None
        assert auth_expired.outage_active() is not None

    def test_a_stale_outage_stops_gating(self, env, ok_email):
        auth_expired.record_outage({"session": "a"})
        assert auth_expired.outage_active() is not None
        state = auth_expired.read_state()
        state["last_seen"] = (
            auth_expired._now() - auth_expired.OUTAGE_TTL - timedelta(minutes=1)
        ).isoformat()
        auth_expired.write_state(state)
        assert auth_expired.outage_active() is None, "must reopen for a probe"

    def test_a_corrupt_state_file_does_not_gate(self, env):
        auth_expired.state_path().parent.mkdir(parents=True, exist_ok=True)
        auth_expired.state_path().write_text("{ not json")
        assert auth_expired.read_state() is None
        assert auth_expired.outage_active() is None

    def test_no_state_is_no_outage(self, env):
        assert auth_expired.outage_active() is None

    def test_clear_state_removes_the_record(self, env, ok_email):
        auth_expired.record_outage({"session": "a"})
        assert auth_expired.clear_state() is True
        assert auth_expired.read_state() is None
        assert auth_expired.clear_state() is False


# --------------------------------------------------------------------------
# Copy: names the provider and the hermes commands, never /login
# --------------------------------------------------------------------------


class TestCopy:
    def test_summary_line_names_provider_and_code(self):
        detail = {"provider": "nous", "code": "subscription_expired",
                  "source": "stderr"}
        line = auth_expired.summary_line(detail)
        assert "nous" in line
        assert "subscription_expired" in line
        assert "stderr" in line

    def test_summary_line_without_detail_is_safe(self):
        line = auth_expired.summary_line(None)
        assert "login expired" in line

    def test_no_claude_login_strings_remain(self):
        import agentwire.auth_expired as mod

        source = Path(mod.__file__).read_text()
        for banned in ("authentication_failed", "/login", "Login expired",
                       "PROJECTS_DIR", "encode_project_path"):
            assert banned not in source, f"{banned!r} must not appear in auth_expired.py"
