"""CLI for hook/skill installation — ``hermeswire hooks ...``.

Installs and heals hermeswire-owned Hermes Agent integration: the permission
hook, idle handler, queue processor, and global skills. These files are
hermeswire-owned (no user edits to preserve), so any drift from the packaged
source is replaced.

Hermes registers hooks in a ``hooks:`` block inside ``~/.hermes/config.yaml``
(the same YAML the Hermes CLI reads at startup) — there is no
``~/.hermes/settings.json``. Each event maps to a list of ``{command, ...}``
entries. See issue #10 for the verified hook contract (events, stdin/stdout
JSON, and the ``~/.hermes/shell-hooks-allowlist.json`` consent gate).
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

HERMES_HOOKS_DIR = Path.home() / ".hermes" / "hooks"
HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"
# Canonical config path for docs/display. Config *writes* resolve through
# _config_path() (Path.home() at call time) so a monkeypatched home in tests
# isolates them — the constants are frozen at import time.
HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"


def get_hooks_source() -> Path:
    """Get the path to the hooks directory in the installed package."""
    # First try: hooks directory inside the hermeswire package
    package_dir = Path(__file__).parent
    hooks_dir = package_dir / "hooks"
    if hooks_dir.exists():
        return hooks_dir

    # Fallback: try importlib.resources (for installed packages)
    try:
        with importlib.resources.files("hermeswire").joinpath("hooks") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find hooks directory in package")


def get_skills_source() -> Path:
    """Get the path to the skills directory in the installed package.

    Mirrors get_hooks_source(): resolves to the bundled package dir so the
    installed symlink is auto-current after `hermeswire rebuild` (which reinstalls
    the wheel), never a transient checkout path.
    """
    package_dir = Path(__file__).parent
    skills_dir = package_dir / "skills"
    if skills_dir.exists():
        return skills_dir

    try:
        with importlib.resources.files("hermeswire").joinpath("skills") as p:
            if p.exists():
                return Path(p)
    except (TypeError, FileNotFoundError):
        pass

    raise FileNotFoundError("Could not find skills directory in package")


def _managed_global_skills() -> list[str]:
    """Hermeswire-owned skills that belong GLOBALLY in ~/.hermes/skills/.

    Two groups:

    - ``wiki`` — the wiki store lives at ~/.hermeswire/wiki/ and is usable from
      any session, so the skill must be globally available (issue #475).
    - ``hermeswire-<role>`` — one per role in ``hermeswire/roles/*.md``. Hermes
      has no ``--append-system-prompt``; role instructions ride ``-s`` skills
      (issue #15), so on a fresh install the ``-s hermeswire-<role>`` flag
      ``build_agent_command`` emits must resolve. The skills are installed
      globally (alongside ``wiki``) so a session on any project finds them —
      the role files are a function of the *session's role*, not its project
      (#23). Third-party skills (cua-driver, shadcn-ui, …) are never touched.

    The role list is derived from the packaged ``roles/`` directory so a new
    role file lands here automatically — no manual registry to forget.
    """
    roles = _bundled_role_names()
    return ["wiki", *(f"hermeswire-{r}" for r in roles)]


def _bundled_role_names() -> list[str]:
    """Stems of ``hermeswire/roles/*.md`` — the source of truth for role skills.

    Resolved through ``importlib.resources`` when installed (the wheel ships
    ``hermeswire/roles/*.md`` via the include glob in pyproject.toml) and a
    plain directory read when running from a checkout, so a fresh install and
    a dev worktree both see the same set. Sorted for stable install order.
    """
    try:
        roles_dir = Path(str(importlib.resources.files("hermeswire").joinpath("roles")))
    except (TypeError, FileNotFoundError):
        return []
    if not roles_dir.is_dir():
        return []
    return sorted(f.stem for f in roles_dir.glob("*.md"))


def _managed_skill_state(target: Path, source: Path) -> str:
    """Drift state of a managed global skill DIRECTORY: missing | stale | ok.

    Skills are directories, so unlike _managed_file_state this never compares
    bytes. A symlink is ok only when it resolves to the packaged source; a real
    directory (the hand-placed pre-#475 state) or a symlink pointing elsewhere is
    stale and must be removed before re-symlinking.
    """
    if target.is_symlink():
        if not target.exists():
            return "stale"  # dangling symlink
        return "ok" if target.resolve() == source.resolve() else "stale"
    if not target.exists():
        return "missing"
    return "stale"  # real dir/file occupying the slot


def _remove_skill_target(target: Path) -> None:
    """Clear whatever occupies a skill slot — symlink, real dir, or stray file."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def install_skills(force: bool = False, copy: bool = False) -> dict[str, str]:
    """Install/refresh hermeswire-owned global skills into ~/.hermes/skills/.

    Each managed skill is a directory installed as a symlink (or copied with
    --copy) pointing at the packaged source, drift-aware: a correct symlink is
    left alone, a real-dir / wrong-symlink target is replaced.

    Returns {name: "installed" | "updated" | "current" | "missing-source"}.
    """
    try:
        skills_source = get_skills_source()
    except FileNotFoundError:
        print("  Warning: skills directory not found, skipping skill installation")
        return {}

    results: dict[str, str] = {}
    for name in _managed_global_skills():
        source = skills_source / name
        if not source.exists():
            print(f"  Warning: skill '{name}' not found in package, skipping")
            results[name] = "missing-source"
            continue

        target = HERMES_SKILLS_DIR / name
        state = _managed_skill_state(target, source)
        if state == "ok" and not force:
            results[name] = "current"
            continue

        existed = target.exists() or target.is_symlink()
        HERMES_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _remove_skill_target(target)
        if copy:
            shutil.copytree(source, target)
        else:
            target.symlink_to(source.resolve(), target_is_directory=True)
        results[name] = "updated" if existed else "installed"

    return results


def skill_drift() -> dict[str, str]:
    """Drift state of hermeswire-owned global skills.

    Returns {name: ok|stale|missing|source-unavailable}. Mirrors
    safety_commands.*_drift() so `hermeswire doctor` can flag a hand-placed or drifted
    skill the same way it flags hook drift.

    `source-unavailable` means the packaged skill can't be resolved in the
    running context — e.g. doctor invoked from a SOURCE checkout, where skills
    only exist inside the built wheel (`hermeswire/skills/`), not on disk. That is
    NOT a drift problem: there is nothing to install from, so doctor skips it
    rather than crying "missing". `missing`/`stale` are reserved for the case
    where the source IS resolvable (installed tool) and the installed copy is
    genuinely absent or wrong.
    """
    try:
        skills_source = get_skills_source()
    except FileNotFoundError:
        return {name: "source-unavailable" for name in _managed_global_skills()}

    drift: dict[str, str] = {}
    for name in _managed_global_skills():
        source = skills_source / name
        target = HERMES_SKILLS_DIR / name
        if not source.exists():
            drift[name] = "source-unavailable"
            continue
        drift[name] = _managed_skill_state(target, source)
    return drift


# ---------------------------------------------------------------------------
# Hermes ``hooks:`` block registration (in ~/.hermes/config.yaml, YAML).
# ---------------------------------------------------------------------------


def _hermes_hook_command(hook_name: str) -> str:
    """The ``command`` string registered for a managed hook (uses ``~`` like the
    old Claude registration, for portability across machines)."""
    return f"~/.hermes/hooks/{hook_name}"


def _config_path() -> Path:
    """Resolve ``~/.hermes/config.yaml`` at call time, not import time.

    The old Claude settings.json writer resolved its path via ``Path.home()``
    inside the function, so tests that monkeypatch ``Path.home`` isolate
    correctly. The module constants (``HERMES_CONFIG`` etc.) are computed at
    import time and would otherwise escape that monkeypatch, so the config
    reader/writer resolves through this helper instead.
    """
    return Path.home() / ".hermes" / "config.yaml"


def _config_entry_for(event: str, hook_name: str) -> dict:
    """Build the config.yaml entry for a managed hook under ``event``.

    Hermes only honors ``matcher``/``timeout`` on the tool gates
    (pre/post_tool_call); lifecycle events (on_session_end) take a bare
    ``{command}`` entry. The permission hook is a general gate that matches
    every tool, so it gets a timeout but no matcher.
    """
    entry: dict = {"command": _hermes_hook_command(hook_name)}
    if event == "pre_tool_call":
        entry["timeout"] = 60
    return entry


def _load_hermes_config() -> dict:
    """Load ~/.hermes/config.yaml as a dict; ``{}`` when missing/unparseable."""
    config_path = _config_path()
    if not config_path.exists():
        return {}
    try:
        data = yaml.safe_load(config_path.read_text())
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_hermes_config(config: dict) -> None:
    """Atomically write config.yaml, preserving unrelated keys and file mode.

    Mirrors the Hermes shell-hooks allowlist writer: write to a temp file in the
    same dir, then os.replace() so a crash never leaves a half-written config.
    """
    config_path = _config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if config_path.exists():
        try:
            mode = config_path.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
    text = yaml.safe_dump(
        config, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(config_path.parent), prefix="config.yaml.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_hook_in_config(event: str, hook_name: str) -> bool:
    """Register a hook under ``event`` in the Hermes ``hooks:`` config block.

    Returns True if config was updated, False if already configured.

    Hermes hook format (config.yaml, not settings.json):

        hooks:
          pre_tool_call:
            - command: "~/.hermes/hooks/hermeswire-permission.sh"
              timeout: 60
          on_session_end:
            - command: "~/.hermes/hooks/idle-handler.sh"
    """
    config = _load_hermes_config()

    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        config["hooks"] = hooks

    entries = hooks.get(event)
    if not isinstance(entries, list):
        entries = []
        hooks[event] = entries

    command = _hermes_hook_command(hook_name)
    if any(isinstance(e, dict) and e.get("command") == command for e in entries):
        return False  # Already registered

    entries.append(_config_entry_for(event, hook_name))
    _save_hermes_config(config)
    return True


# Hermeswire-owned files deployed by `hooks install`. Each entry:
# (filename in hermeswire/hooks/, target directory, Hermes config event or None)
def _managed_hook_files() -> list[tuple[str, Path, str | None]]:
    return [
        ("hermeswire-permission.sh", HERMES_HOOKS_DIR, "pre_tool_call"),
        ("idle-handler.sh", HERMES_HOOKS_DIR, "on_session_end"),
        ("queue-processor.sh", Path.home() / ".hermeswire", None),
    ]


def _managed_file_state(target: Path, source: Path) -> str:
    """Drift state of an hermeswire-managed installed file: missing | stale | ok.

    Symlinks are ok when they resolve to the packaged source; regular files
    are ok when their content matches it byte-for-byte.
    """
    if target.is_symlink():
        if not target.exists():
            return "stale"  # dangling symlink
        return "ok" if target.resolve() == source.resolve() else "stale"
    if not target.exists():
        return "missing"
    try:
        return "ok" if target.read_bytes() == source.read_bytes() else "stale"
    except OSError:
        return "stale"


def _install_managed_file(source: Path, target: Path, force: bool = False, copy: bool = False) -> bool:
    """Install or refresh an hermeswire-owned file (symlink by default).

    These files carry no user edits to preserve — any drift from the packaged
    source is replaced. Returns True if the target was created or updated.
    """
    if not force and _managed_file_state(target, source) == "ok":
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if copy:
        shutil.copy2(source, target)
        # Only the copied file gets a chmod. On the symlink path, chmod
        # FOLLOWS the link — and when the package is a source checkout, the
        # link points at a tracked file, so a chmod aimed at the install
        # lands in the repo and dirties every dev's tree (#947). Execution
        # through a symlink uses the target's mode, and the packaged hook
        # scripts ship 755, so the link needs no chmod of its own.
        target.chmod(0o755)
    else:
        target.symlink_to(source.resolve())
    return True


def install_hooks(
    force: bool = False,
    copy: bool = False,
    allow_foreign: bool = False,
) -> dict[str, str]:
    """Install/refresh all hermeswire-owned hook files + config.yaml registration.

    Returns {filename: "installed" | "updated" | "current" | "missing-source" |
    "refused-foreign"}.

    Refuses outright when run from a checkout that is not the canonically
    installed tool (#936). These targets are machine-global and installed as
    SYMLINKS by default, so `hooks install` from a worktree does not merely copy
    a stale file — it points every session's permission and idle hooks at a task
    branch, which then changes under them on every commit.
    """
    from hermeswire.safety import provenance as prov

    state, canonical, running = prov.install_provenance()
    if state in prov.REFUSING_STATES and not allow_foreign:
        for line in prov.refusal_lines(canonical, running, "hermeswire-owned hooks"):
            print(f"  {line}" if line else "")
        return {name: "refused-foreign" for name, _t, _e in _managed_hook_files()}

    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        print("  Warning: hooks directory not found, skipping hook installation")
        return {}

    results: dict[str, str] = {}
    for hook_name, target_dir, event in _managed_hook_files():
        source = hooks_source / hook_name
        if not source.exists():
            print(f"  Warning: {hook_name} not found in package, skipping")
            results[hook_name] = "missing-source"
            continue

        target = target_dir / hook_name
        existed = target.exists() or target.is_symlink()
        if _install_managed_file(source, target, force=force, copy=copy):
            results[hook_name] = "updated" if existed else "installed"
        else:
            results[hook_name] = "current"

        if event:
            register_hook_in_config(event, hook_name)

    # Heal the full damage-control surface (hook scripts + rules + tooldefs),
    # not just the config matchers. This closes the documented post-rebuild gap:
    # CLAUDE.md tells users to re-run `hooks install` after a rebuild, so it must
    # actually sync the DC files/rules — drift-aware, bringing previously-shipped
    # versions forward and never clobbering a file it does not recognize.
    #
    # NOT quiet, and NOT silently swallowed: this heal can now legitimately
    # decline to act (a newer installed hook, a hand-edited rule), and a decline
    # an operator never sees is the #916/#936 failure mode itself — `hooks
    # install` printing success while the thing it installs stays stale.
    try:
        from hermeswire.safety_commands import heal_damage_control
        dc = heal_damage_control(quiet=True, allow_foreign=allow_foreign)
        held = (
            dc.get("hooks_downgrade_refused", [])
            + dc.get("rules_unknown", [])
            + dc.get("tooldefs_unknown", [])
        )
        if held:
            print(f"  Damage control: {len(held)} file(s) HELD BACK: {', '.join(held)}")
            print("     Details/override: hermeswire safety install --yes [--force]")
        for key, verb in (("rules_updated", "rule"), ("tooldefs_updated", "tooldef")):
            for name in dc.get(key, []):
                print(f"  Damage control: updated {verb} {name}")
        if dc.get("tooldefs_updated") or dc.get("tooldefs_installed"):
            from hermeswire.safety_commands import unattended_grant_notice
            for line in unattended_grant_notice():
                print(line)
    except Exception as e:
        print(f"  Warning: damage-control heal failed: {e}")

    # Global skills (currently just /wiki) are hermeswire-owned too, and rotted
    # silently because nothing ever resynced the hand-placed copies. Heal them
    # on the same install pass — drift-aware, like the hooks above.
    results.update(install_skills(force=force, copy=copy))

    return results


def cmd_hooks_install(args) -> int:
    """Install hermeswire-owned hook files and global skills for HermesWire integration."""
    results = install_hooks(
        force=args.force,
        copy=args.copy,
        allow_foreign=getattr(args, "allow_foreign_source", False),
    )
    if any(state == "refused-foreign" for state in results.values()):
        # A refusal must never be reportable as a successful install (#936).
        return 1
    for hook_name, target_dir, _event in _managed_hook_files():
        state = results.get(hook_name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} {hook_name} -> {target_dir / hook_name}")
        elif state == "current":
            print(f"{hook_name} already current.")

    refreshed_skills = False
    for name in _managed_global_skills():
        state = results.get(name)
        if state in ("installed", "updated"):
            print(f"{state.capitalize()} skill -> /{name} ({HERMES_SKILLS_DIR / name})")
            refreshed_skills = True
    if not refreshed_skills:
        print("Skills already current.")

    return 0


def unregister_hook_from_config(event: str, hook_name: str) -> bool:
    """Remove a hook registered under ``event`` from the Hermes config.yaml.

    Returns True if config was updated, False if not found.
    """
    if not _config_path().exists():
        return False

    config = _load_hermes_config()
    hooks = config.get("hooks")
    if not isinstance(hooks, dict) or event not in hooks:
        return False

    entries = hooks[event]
    if not isinstance(entries, list):
        return False

    command = _hermes_hook_command(hook_name)
    kept = [
        e for e in entries
        if not (isinstance(e, dict) and e.get("command") == command)
    ]
    if len(kept) == len(entries):
        return False  # Hook wasn't registered

    # Clean up empty structures.
    if kept:
        hooks[event] = kept
    else:
        del hooks[event]
    if not hooks:
        del config["hooks"]

    _save_hermes_config(config)
    return True


def is_hook_registered(event: str, hook_name: str) -> bool:
    """Check if a hook is registered under ``event`` in the Hermes config.yaml."""
    if not _config_path().exists():
        return False

    config = _load_hermes_config()
    hooks = config.get("hooks")
    if not isinstance(hooks, dict) or event not in hooks:
        return False

    entries = hooks[event]
    if not isinstance(entries, list):
        return False

    command = _hermes_hook_command(hook_name)
    return any(isinstance(e, dict) and e.get("command") == command for e in entries)


def cmd_hooks_uninstall(args) -> int:
    """Remove all hermeswire-owned hook files and their config.yaml registration."""
    removed_any = False
    for hook_name, target_dir, event in _managed_hook_files():
        target = target_dir / hook_name
        if target.exists() or target.is_symlink():
            target.unlink()
            print(f"Removed: {target}")
            removed_any = True
        if event and unregister_hook_from_config(event, hook_name):
            print(f"Unregistered {hook_name} from Hermes config.yaml")

    if not removed_any:
        print("No hooks installed")

    return 0


def cmd_hooks_status(args) -> int:
    """Check hermeswire-owned hook files and tmux portal sync hooks."""
    print("=== HermesWire Hooks ===")
    try:
        hooks_source = get_hooks_source()
    except FileNotFoundError:
        hooks_source = None

    for hook_name, target_dir, event in _managed_hook_files():
        target = target_dir / hook_name
        print(f"{hook_name}:")

        if not (target.exists() or target.is_symlink()):
            print("  Status: not installed — run 'hermeswire hooks install'")
            continue

        kind = "symlink" if target.is_symlink() else "copy"
        if hooks_source and (hooks_source / hook_name).exists():
            state = _managed_file_state(target, hooks_source / hook_name)
            drift = "" if state == "ok" else " — STALE, run 'hermeswire hooks install'"
        else:
            drift = " — packaged source not found, drift unknown"
        print(f"  Status: installed ({kind}){drift}")
        location = f"{target} -> {target.resolve()}" if target.is_symlink() else str(target)
        print(f"  Location: {location}")

        if event:
            if is_hook_registered(event, hook_name):
                print(f"  Registered: yes ({event} in ~/.hermes/config.yaml)")
            else:
                print("  Registered: NO - run 'hermeswire hooks install' to fix")

    # Tmux portal sync hooks
    print("\n=== Tmux Portal Sync Hooks ===")
    try:
        # Check global hooks first
        global_result = subprocess.run(
            ["tmux", "show-hooks", "-g"],
            capture_output=True,
            text=True,
        )
        global_hooks = global_result.stdout.strip()

        print("Global hooks:")
        has_global_created = "session-created" in global_hooks
        has_global_closed = "session-closed" in global_hooks

        if has_global_created or has_global_closed:
            parts = []
            if has_global_created:
                parts.append("session-created")
            if has_global_closed:
                parts.append("session-closed")
            print(f"  {', '.join(parts)}")
        else:
            print("  none (run 'hermeswire portal restart' to install)")

        # Get list of sessions for per-session hooks
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("\nNo tmux sessions running")
            return 0

        sessions = result.stdout.strip().split("\n") if result.stdout.strip() else []

        if sessions:
            print("\nPer-session hooks:")
            for session in sessions:
                hooks_result = subprocess.run(
                    ["tmux", "show-hooks", "-t", session],
                    capture_output=True,
                    text=True,
                )
                hooks_output = hooks_result.stdout.strip()

                has_session_closed = "session-closed" in hooks_output
                has_kill_pane = "after-kill-pane" in hooks_output

                status_parts = []
                if has_session_closed:
                    status_parts.append("session-closed")
                if has_kill_pane:
                    status_parts.append("after-kill-pane")

                if status_parts:
                    print(f"  {session}: {', '.join(status_parts)}")
                else:
                    print(f"  {session}: none")

    except Exception as e:
        print(f"Error checking tmux hooks: {e}")

    return 0


def register_hooks_parser(subparsers) -> None:
    hooks_parser = subparsers.add_parser(
        "hooks", help="Manage hermeswire hook files (permission, idle handler, queue processor)"
    )
    hooks_subparsers = hooks_parser.add_subparsers(dest="hooks_command")

    # hooks install
    hooks_install = hooks_subparsers.add_parser(
        "install", help="Install/refresh hermeswire hook files and global skills"
    )
    hooks_install.add_argument(
        "--force", "-f", action="store_true", help="Reinstall even when already current"
    )
    hooks_install.add_argument(
        "--copy", action="store_true", help="Copy files instead of symlinking"
    )
    hooks_install.add_argument(
        "--allow-foreign-source", action="store_true",
        help="Let a checkout that is NOT the installed tool write these "
             "machine-global files. Installing from a task branch is what "
             "silently downgraded this machine's security hooks (#936)",
    )
    hooks_install.set_defaults(func=cmd_hooks_install)

    # hooks uninstall
    hooks_uninstall = hooks_subparsers.add_parser(
        "uninstall", help="Remove hermeswire hook files and their registration"
    )
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)

    # hooks status
    hooks_status = hooks_subparsers.add_parser(
        "status", help="Check hook installation status"
    )
    hooks_status.set_defaults(func=cmd_hooks_status)
