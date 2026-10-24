"""Headless-dispatch routing for ``exit_on_complete`` tasks.

``_is_headless_dispatch`` chooses between headless ``hermes -z`` (process exit =
completion, no lingering session) and the interactive REPL. It must read the
CAPTURED unattended marker (``core._UNATTENDED_ENV``), not ``os.environ``:
``_capture_unattended_env`` pops the marker out of the process env at import
(#674), so an ``os.environ`` read would silently route every unattended dispatch
to the interactive path — which never tears the session down.
"""

from types import SimpleNamespace

from hermeswire.ensure_cli import _is_headless_dispatch


def _task(exit_on_complete=True):
    return SimpleNamespace(exit_on_complete=exit_on_complete)


class TestIsHeadlessDispatch:
    def test_unattended_non_persistent_is_headless(self, monkeypatch):
        monkeypatch.setattr("hermeswire.ensure_cli.is_unattended", lambda: True)
        assert _is_headless_dispatch(_task(True)) is True

    def test_persistent_is_never_headless(self, monkeypatch):
        monkeypatch.setattr("hermeswire.ensure_cli.is_unattended", lambda: True)
        assert _is_headless_dispatch(_task(False)) is False

    def test_not_unattended_is_never_headless(self, monkeypatch):
        monkeypatch.setattr("hermeswire.ensure_cli.is_unattended", lambda: False)
        assert _is_headless_dispatch(_task(True)) is False


class TestUnattendedMarkerReadFromCapturedEnv:
    def test_is_unattended_reads_captured_marker_not_os_environ(self, monkeypatch):
        import hermeswire.core as core

        monkeypatch.delenv("HERMESWIRE_UNATTENDED", raising=False)
        monkeypatch.setattr(core, "_UNATTENDED_ENV", {"HERMESWIRE_UNATTENDED": "1"})
        assert core.is_unattended() is True

    def test_headless_dispatch_uses_captured_marker(self, monkeypatch):
        """The regression: the marker is out of os.environ (captured at import)
        but the dispatch must still route headless."""
        import hermeswire.core as core

        monkeypatch.delenv("HERMESWIRE_UNATTENDED", raising=False)
        monkeypatch.setattr(core, "_UNATTENDED_ENV", {"HERMESWIRE_UNATTENDED": "1"})
        assert _is_headless_dispatch(_task(True)) is True
