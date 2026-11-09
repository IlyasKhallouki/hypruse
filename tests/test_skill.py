"""`hypruse skill`: the packaged Agent Skill and where it gets linked."""

from pathlib import Path

import pytest

from hypruse import skill

ROOT = Path(__file__).resolve().parent.parent
# a checkout serves skills/hypruse/; a distro or Nix build runs this suite
# against the installed wheel, which carries it as hypruse/skill/
INSTALLED = Path(skill.__file__).resolve().parent / "skill"
SKILL_DIR = INSTALLED if (INSTALLED / "SKILL.md").is_file() else ROOT / "skills" / "hypruse"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_the_checkout_carries_the_skill_and_it_is_well_formed():
    src = skill.packaged_dir()
    assert src == SKILL_DIR
    text = (src / "SKILL.md").read_text()
    assert text.startswith("---\n")
    assert "\nname: hypruse\n" in text
    assert "description:" in text
    # the spec caps a skill body at 500 lines and the directory name must
    # match the name field
    assert len(text.splitlines()) <= 500
    assert skill._ours(src)


def test_install_links_only_agents_present_on_the_machine(home, capsys):
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    assert skill.install([], copy=False) == 0
    canonical = home / ".agents" / "skills" / "hypruse"
    assert (canonical / "SKILL.md").is_file()
    assert not canonical.is_symlink()  # a real copy: uvx environments are ephemeral
    for agent_dir in (home / ".claude" / "skills", home / ".codex" / "skills"):
        link = agent_dir / "hypruse"
        assert link.is_symlink() and link.resolve() == canonical.resolve()
    # nothing conjured for agents that are not installed
    assert not (home / ".cursor").exists()
    assert not (home / ".pi").exists()
    out = capsys.readouterr().out
    assert "claude-code:" in out and "codex:" in out and "cursor" not in out


def test_naming_an_agent_creates_its_directory(home):
    assert skill.install(["pi"], copy=False) == 0
    link = home / ".pi" / "agent" / "skills" / "hypruse"
    assert link.is_symlink()


def test_unknown_agent_is_a_usage_error(home, capsys):
    assert skill.install(["emacs"], copy=False) == 2
    assert "unknown agent emacs" in capsys.readouterr().err


def test_copy_mode_copies(home):
    (home / ".claude").mkdir()
    assert skill.install([], copy=True) == 0
    target = home / ".claude" / "skills" / "hypruse"
    assert target.is_dir() and not target.is_symlink()
    assert (target / "SKILL.md").is_file()


def test_reinstall_is_idempotent(home, capsys):
    (home / ".claude").mkdir()
    skill.install([], copy=False)
    capsys.readouterr()
    assert skill.install([], copy=False) == 0
    assert "already linked" in capsys.readouterr().out
    link = home / ".claude" / "skills" / "hypruse"
    assert link.is_symlink() and (link / "SKILL.md").is_file()


def test_someone_elses_skill_is_never_replaced(home, capsys):
    other = home / ".agents" / "skills" / "hypruse"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: hypruse-fork\n---\n")
    assert skill.install([], copy=False) == 1
    assert "not hypruse's skill" in capsys.readouterr().err
    assert (other / "SKILL.md").read_text().startswith("---\nname: hypruse-fork")

    # same at an agent's directory: linked elsewhere, that one left alone
    (other / "SKILL.md").write_text("---\nname: hypruse\n---\n")  # now ours, so install runs
    foreign = home / ".claude" / "skills" / "hypruse"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: something-else\n---\n")
    assert skill.install([], copy=False) == 0
    assert "left alone" in capsys.readouterr().out
    assert not foreign.is_symlink()


def test_hermes_profiles_each_get_a_link(home):
    (home / ".hermes" / "profiles" / "work").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "play").mkdir(parents=True)
    assert skill.install([], copy=False) == 0
    for profile in ("work", "play"):
        assert (home / ".hermes" / "profiles" / profile / "skills" / "hypruse").is_symlink()
    assert (home / ".hermes" / "skills" / "hypruse").is_symlink()


def test_uninstall_removes_ours_and_only_ours(home, capsys):
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    skill.install([], copy=False)
    foreign = home / ".codex" / "skills" / "hypruse"
    foreign.unlink()
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("---\nname: mine\n---\n")
    capsys.readouterr()
    assert skill.uninstall() == 0
    assert not (home / ".claude" / "skills" / "hypruse").exists()
    assert not (home / ".agents" / "skills" / "hypruse").exists()
    assert (foreign / "SKILL.md").is_file()
    assert skill.uninstall() == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_main_path_prints_the_packaged_directory(capsys):
    assert skill.main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(SKILL_DIR)


def test_a_build_without_the_skill_says_so(monkeypatch, capsys, home):
    monkeypatch.setattr(skill, "packaged_dir", lambda: None)
    assert skill.main(["path"]) == 1
    assert skill.install([], copy=False) == 1
    assert "no skill directory" in capsys.readouterr().err
