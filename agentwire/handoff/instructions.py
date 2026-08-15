"""
Enumerate the Hermes context chain for inclusion in a handoff bundle.

The bundle must be portable across machines and project folders, so the
receiving agent doesn't need to recreate the sender's environment to act on
the handoff. We inline Hermes's context sources:

- ~/.hermes/SOUL.md (identity — always injected, slot #1)
- .hermes.md / HERMES.md (project context, git-root walk, first-match-wins)
- AGENTS.md (cwd) then CLAUDE.md (cwd) as fallbacks when no .hermes.md exists
- memory (the Hermes memory provider — no per-project MEMORY.md exists)
"""

from __future__ import annotations

from pathlib import Path

from .schema import Instruction

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _soul() -> Instruction | None:
    p = HERMES_HOME / "SOUL.md"
    content = _read(p)
    if content is None:
        return None
    return Instruction(path=str(p), content=content, kind="soul_md")


def _project_context(cwd: Path) -> Instruction | None:
    """Find the ONE project context file Hermes loads.

    Hermes loads a single project context file, first-match-wins:
    .hermes.md/HERMES.md (walk up toward the git root) → AGENTS.md (cwd) →
    CLAUDE.md (cwd). There is no nested per-directory chain.
    """
    cursor = cwd.resolve()
    while True:
        for name in (".hermes.md", "HERMES.md"):
            candidate = cursor / name
            if candidate.exists():
                content = _read(candidate)
                if content is not None:
                    return Instruction(
                        path=str(candidate), content=content,
                        kind="project_hermes_md",
                    )
        if cursor == cursor.parent or cursor == HOME:
            break
        cursor = cursor.parent

    for name in ("AGENTS.md", "CLAUDE.md"):
        candidate = cwd / name
        if candidate.exists():
            content = _read(candidate)
            if content is not None:
                return Instruction(
                    path=str(candidate), content=content, kind="project_context",
                )
    return None


def _memory(cwd: Path) -> list[Instruction]:
    """Hermes memory lives in the memory provider, not a per-project MEMORY.md.

    There is no ~/.hermes/.../memory/MEMORY.md equivalent; return nothing rather
    than invent a path.
    """
    return []


def collect(cwd: str | Path | None = None) -> list[Instruction]:
    """Collect the Hermes context chain for the given project directory.

    Args:
        cwd: Project directory. Defaults to current working directory.

    Returns:
        List of Instruction objects in Hermes's load order:
        SOUL.md (identity) → project context (.hermes.md / AGENTS.md) → memory.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    out: list[Instruction] = []

    if soul := _soul():
        out.append(soul)
    if project := _project_context(cwd_path):
        out.append(project)
    out.extend(_memory(cwd_path))

    return out
