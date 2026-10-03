"""Pushing the built `data/` directory to the orphan `data` branch.

Everything here runs against local git repositories in tmp directories — no
network, no real remote. The token is a fake; the point of
`test_token_is_absent_from_the_git_remote_config` is that no token ever lands
in `.git/config`, real or fake.
"""

import subprocess
from pathlib import Path

import pytest

from worker.publish import push as push_mod
from worker.publish.push import push_data


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


@pytest.fixture
def tmp_repo(tmp_path: Path, monkeypatch) -> Path:
    """A repo with `main`, a `data/` dir, `origin` pointed at a local bare remote,
    and a fake publish token in the environment."""
    repo = tmp_path / "repo"
    repo.mkdir()
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))

    (repo / "data").mkdir()
    (repo / "data" / "events.json").write_text('{"events": []}\n')

    monkeypatch.setenv("CYBERPULSE_PUBLISH_TOKEN", "github_pat_" + "a" * 22)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return repo


def write_new_event(data_dir: Path) -> None:
    (data_dir / "evt-2026-999999.json").write_text(
        '{"event_id": "evt-2026-999999"}\n'
    )


def git_rev_list_count(repo: Path, ref: str) -> int:
    # `refs/heads/` prefix: the working tree also contains a `data/` directory,
    # and plain `data` would be ambiguous between the two.
    return int(git(repo, "rev-list", "--count", f"refs/heads/{ref}"))


def git_rev_parse(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", rev)


def test_push_is_skipped_when_data_is_unchanged(tmp_repo):
    push_data(tmp_repo / "data")
    r = push_data(tmp_repo / "data")
    assert r.pushed is False and r.reason == "no changes"


def test_push_creates_a_single_commit_orphan_branch(tmp_repo):
    push_data(tmp_repo / "data")
    write_new_event(tmp_repo / "data")
    push_data(tmp_repo / "data")
    assert git_rev_list_count(tmp_repo, "data") == 1      # history stays flat


def test_push_refuses_to_run_without_a_token(tmp_repo, monkeypatch):
    monkeypatch.delenv("CYBERPULSE_PUBLISH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="no publish token"):
        push_data(tmp_repo / "data")


def test_push_never_touches_main(tmp_repo):
    before = git_rev_parse(tmp_repo, "main")
    push_data(tmp_repo / "data")
    assert git_rev_parse(tmp_repo, "main") == before


@pytest.mark.parametrize(
    "branch", ["main", "master", "gh-pages", "data/../main", "data..x", "-data", "datamain"]
)
def test_push_refuses_any_branch_but_data(tmp_repo, branch):
    with pytest.raises(RuntimeError, match="unsafe branch name"):
        push_data(tmp_repo / "data", branch=branch)
    assert git(tmp_repo, "ls-remote", "origin") == ""


def test_push_accepts_a_named_data_branch(tmp_repo):
    assert push_data(tmp_repo / "data", branch="data-preview").pushed is True


def test_token_is_absent_from_the_git_remote_config(tmp_repo):
    push_data(tmp_repo / "data")
    assert "github_pat" not in (tmp_repo / ".git" / "config").read_text()


def test_dry_run_reports_without_pushing(tmp_repo):
    assert push_data(tmp_repo / "data", dry_run=True).pushed is False


def test_push_reports_the_orphan_commit_sha(tmp_repo):
    r = push_data(tmp_repo / "data")
    assert r.pushed is True and r.reason is None
    assert r.commit_sha and len(r.commit_sha) == 40
    assert git_rev_parse(tmp_repo, f"{r.commit_sha}^{{tree}}") == git_rev_parse(
        tmp_repo, "data^{tree}"
    )


def test_push_accepts_a_relative_data_dir(tmp_repo, monkeypatch):
    # settings.data_dir is `Path("data")`; the CLI runs push_data with it.
    monkeypatch.chdir(tmp_repo)
    r = push_data(Path("data"))
    assert r.pushed is True
    assert push_data(Path("data")).reason == "no changes"


def test_a_git_command_that_hangs_is_a_failure_that_does_not_repeat_the_token(
    tmp_repo, monkeypatch
):
    # The original exception repeats the command, which carries the auth header.
    def hang(cmd, **kwargs):
        assert kwargs["timeout"] == push_mod.GIT_TIMEOUT_SECONDS
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(push_mod.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="timed out") as raised:
        push_data(tmp_repo / "data")
    assert raised.value.__suppress_context__ is True
    assert "github_pat" not in str(raised.value)


def test_a_host_knows_whether_it_can_push(tmp_repo, monkeypatch):
    assert push_mod.publish_token_configured() is True
    monkeypatch.delenv("CYBERPULSE_PUBLISH_TOKEN")
    assert push_mod.publish_token_configured() is False
