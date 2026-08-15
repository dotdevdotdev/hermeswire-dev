"""Tests for hermeswire/handoff/instructions.py — the Hermes context chain."""

from pathlib import Path

import pytest

from hermeswire.handoff import instructions


@pytest.fixture
def fake_hermes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.hermes to a tmp dir so tests don't read real user files."""
    fake = tmp_path / ".hermes"
    fake.mkdir()
    monkeypatch.setattr(instructions, "HOME", tmp_path)
    monkeypatch.setattr(instructions, "HERMES_HOME", fake)
    return tmp_path


class TestSoul:
    def test_no_soul_returns_none(self, fake_hermes: Path, tmp_path: Path):
        out = instructions.collect(cwd=tmp_path / "project")
        assert all(i.kind != "soul_md" for i in out)

    def test_soul_present(self, fake_hermes: Path, tmp_path: Path):
        (fake_hermes / ".hermes" / "SOUL.md").write_text("# identity\n")
        project = tmp_path / "project"
        project.mkdir()
        out = instructions.collect(cwd=project)
        soul = next(i for i in out if i.kind == "soul_md")
        assert "# identity" in soul.content


class TestProjectContext:
    def test_hermes_md_picked_up(self, fake_hermes: Path, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".hermes.md").write_text("# project rules\n")
        out = instructions.collect(cwd=project)
        project_instr = [i for i in out if i.kind == "project_hermes_md"]
        assert len(project_instr) == 1
        assert "# project rules" in project_instr[0].content

    def test_hermes_md_walks_up_to_git_root(self, fake_hermes: Path, tmp_path: Path):
        # .hermes.md walks up toward the git root (approximated by walking to HOME).
        project = tmp_path / "project"
        nested = project / "sub"
        nested.mkdir(parents=True)
        (project / ".hermes.md").write_text("# parent\n")
        out = instructions.collect(cwd=nested)
        project_instr = [i for i in out if i.kind == "project_hermes_md"]
        assert len(project_instr) == 1
        assert "# parent" in project_instr[0].content

    def test_first_match_wins_hermes_over_agents(self, fake_hermes: Path, tmp_path: Path):
        # When .hermes.md exists, AGENTS.md is shadowed (first-match-wins).
        project = tmp_path / "project"
        project.mkdir()
        (project / ".hermes.md").write_text("# hermes rules\n")
        (project / "AGENTS.md").write_text("# agents rules\n")
        out = instructions.collect(cwd=project)
        assert any(i.kind == "project_hermes_md" for i in out)
        assert not any(i.kind == "project_context" for i in out)

    def test_agents_md_fallback(self, fake_hermes: Path, tmp_path: Path):
        # No .hermes.md -> AGENTS.md at cwd is the fallback context file.
        project = tmp_path / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("# agents rules\n")
        out = instructions.collect(cwd=project)
        project_instr = [i for i in out if i.kind == "project_context"]
        assert len(project_instr) == 1
        assert "# agents rules" in project_instr[0].content


class TestMemory:
    def test_memory_is_empty_under_hermes(self, fake_hermes: Path, tmp_path: Path):
        # Hermes memory lives in the memory provider, not a per-project MEMORY.md.
        project = tmp_path / "project"
        project.mkdir()
        out = instructions.collect(cwd=project)
        assert all(i.kind != "memory" for i in out)
