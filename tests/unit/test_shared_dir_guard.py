"""Shared-dir conflict guard (#618): infra services co-reside, agents conflict."""

import os

from hermeswire.session_cli import _shared_dir_conflicts

REPO = os.path.realpath(os.getcwd())


def _panes(*pairs: tuple[str, str]) -> str:
    return "\n".join(f"{s}\t{p}" for s, p in pairs)


def test_services_excluded():
    out = _panes(
        ("hermeswire-portal", REPO),
        ("hermeswire-tts", REPO),
        ("hermeswire-stt", REPO),
        ("hermeswire-kokoro", REPO),
        ("hermeswire-scheduler", REPO),
        ("hermeswire-notifications", REPO),
    )
    assert _shared_dir_conflicts(out, "hermeswire-dev", REPO) == set()


def test_dev_orchestrator_still_conflicts():
    # The hardcoded `hermeswire` dev session is NOT a service — must still trip.
    out = _panes(("hermeswire", REPO), ("hermeswire-portal", REPO))
    assert _shared_dir_conflicts(out, "hermeswire-dev", REPO) == {"hermeswire"}


def test_self_skipped():
    out = _panes(("hermeswire-dev", REPO))
    assert _shared_dir_conflicts(out, "hermeswire-dev", REPO) == set()


def test_machine_suffixed_service_excluded():
    out = _panes(("hermeswire-portal@box", REPO))
    assert _shared_dir_conflicts(out, "hermeswire-dev", REPO) == set()


def test_non_matching_path_ignored():
    out = _panes(("other-agent", "/some/other/dir"))
    assert _shared_dir_conflicts(out, "hermeswire-dev", REPO) == set()
