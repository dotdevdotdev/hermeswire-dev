"""Tests for `hermeswire init` default vs --assisted dispatch (issue #493)."""

from argparse import Namespace
from unittest.mock import patch

from hermeswire.system_cli import cmd_init


def _run(assisted: bool) -> dict:
    """Invoke cmd_init with the version/pip preflight stubbed out, capturing
    the skip_session value passed to run_onboarding."""
    captured = {}

    def fake_onboarding(skip_session: bool = True, force: bool = False) -> int:
        captured["skip_session"] = skip_session
        captured["force"] = force
        return 0

    with patch("hermeswire.system_cli.check_python_version", return_value=True), \
         patch("hermeswire.system_cli.check_pip_environment", return_value=True), \
         patch("hermeswire.onboarding.run_onboarding", side_effect=fake_onboarding):
        rc = cmd_init(Namespace(assisted=assisted, force=False))

    captured["rc"] = rc
    return captured


def test_init_default_skips_agent_session():
    """Default `hermeswire init` ends on the portal-URL next steps."""
    result = _run(assisted=False)
    assert result["skip_session"] is True
    assert result["rc"] == 0


def test_init_assisted_spawns_agent_session():
    """`hermeswire init --assisted` opts back into the Claude setup session."""
    result = _run(assisted=True)
    assert result["skip_session"] is False
    assert result["rc"] == 0
