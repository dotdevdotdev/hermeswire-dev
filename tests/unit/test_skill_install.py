"""Tests for hermeswire-owned GLOBAL skill install/drift (issue #475 + #23).

Global skills were hand-placed at wiki-setup and never resynced, so a stale or
missing copy rotted invisibly (#475). Issue #23 extended the managed set from
just ``/wiki`` to also include one ``hermeswire-<role>`` skill per role in
``hermeswire/roles/*.md`` — ``build_agent_command`` emits ``-s hermeswire-<role>``
and the skills must resolve on a fresh install. These tests cover the drift-aware
symlink install + doctor-facing drift report. Everything runs against
monkeypatched temp dirs — the real ~/.hermes/skills/ is never touched.
"""

import pathlib
import shutil

import pytest

import hermeswire.hooks_cli as m
from hermeswire.doctor_cli import _render_skill_section
from hermeswire.hooks_cli import (
    _bundled_role_names,
    _managed_global_skills,
    _managed_skill_state,
    install_hooks,
    install_skills,
    skill_drift,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake packaged-source skills dir and a fake ~/.hermes/skills target dir.

    Scaffolds a source dir for every managed skill (wiki + one per bundled
    role), so install_skills() finds a source for each. The role list is read
    from the real packaged ``hermeswire/roles/*.md`` via ``_bundled_role_names``
    — a new role file lands here automatically, mirroring the production path.
    """
    source = tmp_path / "pkg" / "skills"
    (source / "wiki").mkdir(parents=True)
    (source / "wiki" / "SKILL.md").write_text("# wiki skill\n")
    for role in _bundled_role_names():
        skill_dir = source / f"hermeswire-{role}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {role} role skill\n")

    target_root = tmp_path / "hermes" / "skills"

    monkeypatch.setattr(m, "HERMES_SKILLS_DIR", target_root)
    monkeypatch.setattr(m, "get_skills_source", lambda: source)
    return source, target_root


def test_managed_global_skills_is_wiki_plus_roles():
    """wiki + one hermeswire-<role> per bundled role, in stable (sorted) order."""
    expected = ["wiki", *(f"hermeswire-{r}" for r in _bundled_role_names())]
    assert _managed_global_skills() == expected


def test_managed_global_skills_includes_core_roles():
    """The roles hermeswire actually injects must be present (#23)."""
    skills = set(_managed_global_skills())
    for role in ("worker", "worker-worktree", "orchestrator", "reviewer",
                 "reviewer-worktree", "soul", "voice"):
        assert f"hermeswire-{role}" in skills


def test_install_symlinks_fresh(env):
    source, target_root = env
    results = install_skills()
    assert results["wiki"] == "installed"
    for role in _bundled_role_names():
        name = f"hermeswire-{role}"
        assert results[name] == "installed", name
        target = target_root / name
        assert target.is_symlink(), name
        assert target.resolve() == (source / name).resolve()


def test_install_replaces_real_dir_copy(env):
    """The pre-#475 state: ~/.hermes/skills/wiki is a REAL directory."""
    source, target_root = env
    target = target_root / "wiki"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# stale hand-placed copy\n")

    assert _managed_skill_state(target, source / "wiki") == "stale"

    results = install_skills()
    assert results["wiki"] == "updated"
    assert target.is_symlink()
    assert target.resolve() == (source / "wiki").resolve()


def test_install_heals_wrong_symlink(env, tmp_path):
    source, target_root = env
    bogus = tmp_path / "somewhere-else"
    bogus.mkdir()
    target_root.mkdir(parents=True)
    target = target_root / "wiki"
    target.symlink_to(bogus, target_is_directory=True)

    assert _managed_skill_state(target, source / "wiki") == "stale"

    results = install_skills()
    assert results["wiki"] == "updated"
    assert target.resolve() == (source / "wiki").resolve()


def test_install_is_idempotent(env):
    first = install_skills()
    second = install_skills()
    for name in _managed_global_skills():
        assert first[name] == "installed", name
        assert second[name] == "current", name


def test_install_copy_mode(env):
    source, target_root = env
    results = install_skills(copy=True)
    assert results["wiki"] == "installed"

    target = target_root / "wiki"
    assert not target.is_symlink()
    assert target.is_dir()
    assert (target / "SKILL.md").read_text() == "# wiki skill\n"


def test_install_missing_source(env):
    source, _ = env
    shutil.rmtree(source / "wiki")
    results = install_skills()
    assert results["wiki"] == "missing-source"
    # Role skills are still installed from their intact sources.
    for role in _bundled_role_names():
        assert results[f"hermeswire-{role}"] == "installed"


def test_skill_drift_ok_stale_missing(env):
    source, target_root = env

    # missing — every managed skill reports missing
    drift = skill_drift()
    for name in _managed_global_skills():
        assert drift[name] == "missing", name

    # ok after install
    install_skills()
    assert skill_drift() == {name: "ok" for name in _managed_global_skills()}

    # stale when one is replaced with a real dir
    target = target_root / "wiki"
    target.unlink()
    target.mkdir()
    drift = skill_drift()
    assert drift["wiki"] == "stale"
    for role in _bundled_role_names():
        assert drift[f"hermeswire-{role}"] == "ok"


def test_skill_drift_source_unavailable_when_no_source(env, monkeypatch):
    """A source checkout (skill only in the built wheel) → source-unavailable,
    NOT missing — so doctor doesn't false-positive."""
    def boom():
        raise FileNotFoundError("no skills dir")

    monkeypatch.setattr(m, "get_skills_source", boom)
    assert skill_drift() == {
        name: "source-unavailable" for name in _managed_global_skills()
    }


def test_install_force_resymlinks_when_ok(env):
    """state 'ok' + force → still re-creates the symlink (updated, not current)."""
    source, target_root = env
    install_skills()
    assert _managed_skill_state(target_root / "wiki", source / "wiki") == "ok"

    results = install_skills(force=True)
    assert results["wiki"] == "updated"
    assert (target_root / "wiki").resolve() == (source / "wiki").resolve()


# --- doctor wiring (issue #475 fix: source-unavailable must not be an issue) ---

def test_doctor_section_flags_missing_and_increments(env):
    """Source resolvable + target absent → doctor reports an issue."""
    # Every managed skill is missing → one issue per skill.
    assert _render_skill_section() == len(_managed_global_skills())


def test_doctor_section_flags_stale_and_increments(env):
    """Source resolvable + real-dir target → stale → doctor reports an issue."""
    _, target_root = env
    target = target_root / "wiki"
    target.mkdir(parents=True)
    # wiki is stale, the rest are missing.
    assert _render_skill_section() == len(_managed_global_skills())


def test_doctor_section_clean_when_installed(env):
    install_skills()
    assert _render_skill_section() == 0


def test_doctor_section_no_issue_when_source_unavailable(env, monkeypatch):
    """Running from a checkout (no packaged source) must NOT bump issues_found."""
    def boom():
        raise FileNotFoundError("no skills dir")

    monkeypatch.setattr(m, "get_skills_source", boom)
    assert _render_skill_section() == 0


# --- install_hooks() actually drives install_skills() ---

def test_install_hooks_installs_skills(env, tmp_path, monkeypatch):
    """install_hooks() must heal global skills on the same pass."""
    # Stub out the hook half so we exercise only the skill wiring, and keep
    # damage-control healing from touching the real machine.
    monkeypatch.setattr(m, "get_hooks_source", lambda: tmp_path / "no-hooks")
    import hermeswire.safety_commands as cs
    monkeypatch.setattr(cs, "heal_damage_control", lambda **kw: {})
    # install_hooks now refuses outright from a non-canonical package (#936).
    # Pin the running package AS canonical so this measures the skill wiring
    # and not the guard — and so it behaves identically in a worktree (package
    # root's .git is a FILE) and in CI's plain clone.
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    from hermeswire.safety import provenance as _prov
    monkeypatch.setattr(
        _prov, "canonical_package_dir",
        lambda: pathlib.Path(__import__("hermeswire").__file__).parent.resolve(),
    )
    _, target_root = env
    target = target_root / "wiki"
    assert not target.exists()

    results = install_hooks()
    assert results.get("wiki") == "installed"
    assert target.is_symlink()
    # Role skills install on the same pass (#23).
    for role in _bundled_role_names():
        name = f"hermeswire-{role}"
        assert results.get(name) == "installed", name
        assert (target_root / name).is_symlink(), name
