"""Tests for `hermeswire hooks install` — managed hook files and drift handling.

Issue #10: `hooks install` now deploys hermeswire-owned files into Hermes Agent
targets (``~/.hermes/hooks/`` + a ``hooks:`` block in ``~/.hermes/config.yaml``)
instead of Claude's ``~/.claude/hooks/`` + ``settings.json``. The Claude events
are re-mapped (PermissionRequest/PreToolUse -> pre_tool_call, Notification ->
on_session_end) and every hermeswire-owned file is installed/refreshed whenever
it differs from the packaged source, with doctor/status reporting drift.

Issue #238 (kept): installed copies of idle-handler.sh and queue-processor.sh
silently drifted stale; now all hermeswire-owned files are refreshed.
"""

from pathlib import Path

import pytest
import yaml

from hermeswire.hooks_cli import (
    _install_managed_file,
    _managed_file_state,
    _managed_hook_files,
    install_hooks,
    is_hook_registered,
    register_hook_in_config,
    unregister_hook_from_config,
)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src" / "hook.sh"
    src.parent.mkdir()
    src.write_text("#!/bin/bash\necho current\n")
    return src


@pytest.fixture
def target_dir(tmp_path):
    d = tmp_path / "installed"
    d.mkdir()
    return d


class TestManagedFileState:
    def test_missing(self, source, target_dir):
        assert _managed_file_state(target_dir / "hook.sh", source) == "missing"

    def test_copy_matching_content_ok(self, source, target_dir):
        target = target_dir / "hook.sh"
        target.write_text(source.read_text())
        assert _managed_file_state(target, source) == "ok"

    def test_copy_drifted_content_stale(self, source, target_dir):
        target = target_dir / "hook.sh"
        target.write_text("#!/bin/bash\necho old\n")
        assert _managed_file_state(target, source) == "stale"

    def test_symlink_to_source_ok(self, source, target_dir):
        target = target_dir / "hook.sh"
        target.symlink_to(source)
        assert _managed_file_state(target, source) == "ok"

    def test_symlink_to_wrong_file_stale(self, source, target_dir, tmp_path):
        other = tmp_path / "other.sh"
        other.write_text("nope")
        target = target_dir / "hook.sh"
        target.symlink_to(other)
        assert _managed_file_state(target, source) == "stale"

    def test_dangling_symlink_stale(self, source, target_dir, tmp_path):
        target = target_dir / "hook.sh"
        target.symlink_to(tmp_path / "deleted.sh")
        assert _managed_file_state(target, source) == "stale"


class TestInstallManagedFile:
    def test_installs_symlink_by_default(self, source, target_dir):
        target = target_dir / "hook.sh"
        assert _install_managed_file(source, target) is True
        assert target.is_symlink() and target.resolve() == source.resolve()

    def test_installs_copy_when_requested(self, source, target_dir):
        target = target_dir / "hook.sh"
        assert _install_managed_file(source, target, copy=True) is True
        assert not target.is_symlink()
        assert target.read_text() == source.read_text()
        assert target.stat().st_mode & 0o111  # executable

    def test_symlink_install_never_chmods_the_source(self, source, target_dir):
        """#947: chmod follows symlinks, so a chmod aimed at the installed
        link lands on the SOURCE — which, in a dev checkout, is a tracked
        file. The suite itself was the reproducer: every run flipped
        ``hermeswire/hooks/queue-processor.sh`` to 755 in every dev's tree.
        The symlink path must leave the source's mode alone entirely."""
        import os

        os.chmod(source, 0o644)
        target = target_dir / "hook.sh"
        assert _install_managed_file(source, target) is True
        assert target.is_symlink()
        assert (source.stat().st_mode & 0o777) == 0o644

    def test_current_file_untouched(self, source, target_dir):
        target = target_dir / "hook.sh"
        _install_managed_file(source, target, copy=True)
        assert _install_managed_file(source, target, copy=True) is False

    def test_stale_copy_replaced(self, source, target_dir):
        # The #238 failure mode: a drifted regular file was skipped forever.
        target = target_dir / "hook.sh"
        target.write_text("#!/bin/bash\necho ancient\n")
        assert _install_managed_file(source, target, copy=True) is True
        assert target.read_text() == source.read_text()

    def test_stale_symlink_relinked(self, source, target_dir, tmp_path):
        other = tmp_path / "other.sh"
        other.write_text("nope")
        target = target_dir / "hook.sh"
        target.symlink_to(other)
        assert _install_managed_file(source, target) is True
        assert target.resolve() == source.resolve()

    def test_force_reinstalls_current(self, source, target_dir):
        target = target_dir / "hook.sh"
        _install_managed_file(source, target, copy=True)
        assert _install_managed_file(source, target, force=True) is True
        assert target.is_symlink()  # copy converted to symlink

    def test_creates_target_dir(self, source, tmp_path):
        target = tmp_path / "deep" / "nested" / "hook.sh"
        assert _install_managed_file(source, target) is True
        assert target.exists()


class TestManagedHookFiles:
    """The managed-files table must map to Hermes targets + events (#10)."""

    def test_events_remapped_to_hermes(self):
        files = {name: (dir_, event) for name, dir_, event in _managed_hook_files()}
        perm_dir, perm_ev = files["hermeswire-permission.sh"]
        idle_dir, idle_ev = files["idle-handler.sh"]
        queue_dir, queue_ev = files["queue-processor.sh"]

        assert perm_ev == "pre_tool_call"
        assert idle_ev == "on_session_end"
        assert queue_ev is None

        assert perm_dir.name == "hooks" and perm_dir.parent.name == ".hermes"
        assert idle_dir.name == "hooks" and idle_dir.parent.name == ".hermes"
        assert queue_dir.name == ".hermeswire"


class TestInstallHooks:
    """End-to-end: fake packaged source + fake home, all three files deployed."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        from hermeswire import hooks_cli as main_mod

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        # The hook/skill dir constants are computed at import time from the real
        # home, so re-point them at the fake home too. Config writes resolve via
        # Path.home() at call time, so they follow the Path.home patch above.
        monkeypatch.setattr(main_mod, "HERMES_HOOKS_DIR", home / ".hermes" / "hooks")
        monkeypatch.setattr(main_mod, "HERMES_SKILLS_DIR", home / ".hermes" / "skills")

        # Machine-global installs are refused from a non-canonical package
        # (#936). Pin the running package AS canonical so these measure the
        # install and not the guard, and behave identically in a worktree
        # (package root's .git is a FILE) and in CI's plain clone.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        from hermeswire.safety import provenance as _prov
        monkeypatch.setattr(
            _prov, "canonical_package_dir",
            lambda: Path(__import__("hermeswire").__file__).parent.resolve(),
        )

        hooks_src = tmp_path / "pkg-hooks"
        hooks_src.mkdir()
        for name, _dir, _event in _managed_hook_files():
            (hooks_src / name).write_text(f"#!/bin/bash\n# {name}\n")
        monkeypatch.setattr(main_mod, "get_hooks_source", lambda: hooks_src)

        # Damage-control healing is the safety domain (#11), not hooks install;
        # stub it so this test exercises only the Hermes hook wiring.
        import hermeswire.safety_commands as cs
        monkeypatch.setattr(cs, "heal_damage_control", lambda **kw: {})

        # Scaffold the packaged skills source (wiki + role skills, #23) so
        # install_hooks() -> install_skills() finds a source for each. The
        # role list is derived from the real packaged roles dir.
        skills_src = tmp_path / "pkg-skills"
        skills_src.mkdir()
        (skills_src / "wiki").mkdir()
        (skills_src / "wiki" / "SKILL.md").write_text("# wiki skill\n")
        for role in main_mod._bundled_role_names():
            rs = skills_src / f"hermeswire-{role}"
            rs.mkdir()
            (rs / "SKILL.md").write_text(f"# {role} role skill\n")
        monkeypatch.setattr(main_mod, "get_skills_source", lambda: skills_src)

        return home, hooks_src

    def test_fresh_install_deploys_all_and_registers(self, env):
        home, _src = env
        results = install_hooks()
        assert set(results.values()) == {"installed"}
        assert (home / ".hermes" / "hooks" / "hermeswire-permission.sh").exists()
        assert (home / ".hermes" / "hooks" / "idle-handler.sh").exists()
        assert (home / ".hermeswire" / "queue-processor.sh").exists()
        assert not (home / ".claude").exists()  # no Claude targets are created

        config = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
        events = config["hooks"]
        assert any(h["command"].endswith("hermeswire-permission.sh")
                   for h in events["pre_tool_call"])
        assert any(h["command"].endswith("idle-handler.sh")
                   for h in events["on_session_end"])

    def test_second_run_all_current(self, env):
        install_hooks()
        assert set(install_hooks().values()) == {"current"}

    def test_stale_installed_copy_refreshed(self, env):
        # The exact #238 scenario: an old regular-file copy must be replaced.
        home, _src = env
        install_hooks()
        stale = home / ".hermes" / "hooks" / "idle-handler.sh"
        stale.unlink()
        stale.write_text("#!/bin/bash\n# ancient pre-loop-mode hook\n")
        results = install_hooks()
        assert results["idle-handler.sh"] == "updated"
        assert "ancient" not in stale.resolve().read_text()

    def test_registration_idempotent(self, env):
        home, _src = env
        install_hooks()
        install_hooks()
        config = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
        assert len(config["hooks"]["on_session_end"]) == 1
        assert len(config["hooks"]["pre_tool_call"]) == 1


class TestConfigRegistration:
    """The config.yaml ``hooks:`` block replaces Claude settings.json (#10)."""

    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        from hermeswire import hooks_cli as main_mod

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(main_mod, "HERMES_HOOKS_DIR", home / ".hermes" / "hooks")
        return home

    def test_register_and_query(self):
        assert is_hook_registered("on_session_end", "idle-handler.sh") is False
        assert register_hook_in_config("on_session_end", "idle-handler.sh") is True
        assert is_hook_registered("on_session_end", "idle-handler.sh") is True
        # Different event for the same file is independent.
        assert is_hook_registered("pre_tool_call", "idle-handler.sh") is False

    def test_register_twice_is_noop(self):
        register_hook_in_config("on_session_end", "idle-handler.sh")
        assert register_hook_in_config("on_session_end", "idle-handler.sh") is False

    def test_unregister_removes_only_target_event(self):
        register_hook_in_config("on_session_end", "idle-handler.sh")
        register_hook_in_config("pre_tool_call", "hermeswire-permission.sh")
        assert unregister_hook_from_config("on_session_end", "idle-handler.sh") is True
        assert is_hook_registered("on_session_end", "idle-handler.sh") is False
        assert is_hook_registered("pre_tool_call", "hermeswire-permission.sh") is True

    def test_unregister_missing_returns_false(self):
        assert unregister_hook_from_config("on_session_end", "idle-handler.sh") is False

    def test_register_preserves_unrelated_keys(self, fake_home):
        # The YAML writer must never clobber sibling top-level keys (model:,
        # approvals:, providers:, hooks_auto_accept: ...).
        home = fake_home
        cfg = home / ".hermes" / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("model: gpt-5\napprovals:\n  destructive_slash_confirm: false\n")
        register_hook_in_config("on_session_end", "idle-handler.sh")
        config = yaml.safe_load(cfg.read_text())
        assert config["model"] == "gpt-5"
        assert config["approvals"]["destructive_slash_confirm"] is False
        assert config["hooks"]["on_session_end"][0]["command"].endswith("idle-handler.sh")

    def test_pre_tool_call_entry_has_timeout(self, fake_home):
        home = fake_home
        register_hook_in_config("pre_tool_call", "hermeswire-permission.sh")
        config = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
        entry = config["hooks"]["pre_tool_call"][0]
        assert entry["command"].endswith("hermeswire-permission.sh")
        assert entry["timeout"] == 60

    def test_lifecycle_entry_has_no_matcher(self, fake_home):
        # Hermes ignores matcher on non-tool events; on_session_end must be bare.
        home = fake_home
        register_hook_in_config("on_session_end", "idle-handler.sh")
        config = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
        assert "matcher" not in config["hooks"]["on_session_end"][0]


class TestPackagedHooksPresent:
    """The managed-files table must match what actually ships in the package."""

    def test_all_managed_files_exist_in_source(self):
        from hermeswire.hooks_cli import get_hooks_source
        hooks_source = get_hooks_source()
        for name, _dir, _event in _managed_hook_files():
            assert (hooks_source / name).exists(), f"{name} missing from hermeswire/hooks/"
