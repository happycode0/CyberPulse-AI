"""Publish the built `data/` directory to the orphan `data` branch.

The `data` branch exists only to feed GitHub Pages: it is force-pushed as a
single parentless commit every time, so history stays flat (one commit, one
tree) and the repository stays far below Pages' 1 GB guidance.

Guarantees:

* **no empty commits** — the local content tree is compared against the remote
  branch tip's tree first; identical content returns `reason="no changes"`;
* **main is never touched** — only `refs/heads/<branch>` is written, and all
  index work happens in a temporary `GIT_INDEX_FILE`;
* **the token never lands in `.git/config`** — credentials travel as an
  `http.extraheader` command-line argument, which is not persisted anywhere,
  and are redacted from error output;
* **`dry_run=True` performs every check and reports what it would do, but
  pushes nothing and moves no refs.**
"""

import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from worker.settings import get_settings

DEFAULT_BRANCH = "data"

# The scheduler pushes while holding the publish lock, so a git command stalled on the network
# would hold back every publish after it. A whole push takes seconds; three minutes is a hang.
GIT_TIMEOUT_SECONDS = 180

# Refuse anything that could smuggle options or pathspecs into git commands.
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")

# Commit identity is set via the environment so the push never depends on
# (or fails because of) git configuration on the host or in the container.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "CyberPulse-AI Publisher",
    "GIT_AUTHOR_EMAIL": "cyberpulse-bot@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "CyberPulse-AI Publisher",
    "GIT_COMMITTER_EMAIL": "cyberpulse-bot@users.noreply.github.com",
}


@dataclass(frozen=True)
class PushResult:
    """Outcome of a :func:`push_data` call.

    `commit_sha` is the pushed commit, or the commit that *would* have been
    pushed when `dry_run=True`; `reason` explains a skipped push.
    """

    pushed: bool
    commit_sha: str | None
    reason: str | None


def push_data(
    data_dir: Path, *, branch: str = "data", dry_run: bool = False
) -> PushResult:
    """Force-push `data_dir`'s contents to `branch` as a single orphan commit.

    Returns `PushResult(pushed=False, reason="no changes")` when the remote
    branch already holds exactly this content. Raises `RuntimeError` when there
    is no publish token, no data, or a git command fails.
    """
    token = _publish_token()
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise RuntimeError(f"unsafe branch name: {branch!r}")

    # Absolute: git resolves GIT_WORK_TREE relative to a different cwd than
    # `-C`, so a relative path (e.g. the default settings.data_dir) would
    # silently build the wrong tree.
    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        raise RuntimeError(f"no data directory to publish: {data_dir}")
    files = [p for p in data_dir.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError(f"no data files to publish in {data_dir}")

    repo = _repo_root(data_dir)
    tree = _content_tree(data_dir)
    url = _remote_url(repo)
    auth = _auth_header(token)
    secret_values = (token, base64.b64encode(token.encode()).decode())

    tip = _remote_tip(repo, url, branch, auth, secret_values)
    if tip is not None:
        remote_tree = _remote_tree(repo, url, branch, auth, secret_values)
        if remote_tree == tree:
            return PushResult(pushed=False, commit_sha=None, reason="no changes")

    message = (
        f"data snapshot: {len(files)} files, "
        f"{datetime.now(UTC).isoformat(timespec='seconds')}"
    )
    commit = _git(
        repo,
        "commit-tree",
        tree,
        "-m",
        message,
        extra_env=_COMMIT_ENV,
        secrets=secret_values,
    )

    if dry_run:
        return PushResult(pushed=False, commit_sha=commit, reason="dry run")

    _git(
        repo,
        *auth,
        "push",
        "--force",
        url,
        f"{commit}:refs/heads/{branch}",
        secrets=secret_values,
    )
    # Keep the local clone's `data` ref in sync with what was pushed. This is
    # the only ref the push writes besides the remote's; `main` is untouched.
    _git(repo, "update-ref", f"refs/heads/{branch}", commit, secrets=secret_values)
    return PushResult(pushed=True, commit_sha=commit, reason=None)


def _publish_token() -> str:
    raw = os.environ.get("CYBERPULSE_PUBLISH_TOKEN", "").strip()
    if not raw:
        secret = get_settings().github_token
        raw = secret.get_secret_value().strip() if secret else ""
    if not raw:
        raise RuntimeError(
            "no publish token: set CYBERPULSE_PUBLISH_TOKEN (or github_token)"
        )
    return raw


def publish_token_configured() -> bool:
    """Whether this host can push at all. A host without a token builds `data/` and stops."""
    try:
        _publish_token()
    except RuntimeError:
        return False
    return True


def _auth_header(token: str) -> list[str]:
    """Credentials as a transient `http.extraheader` argument.

    Passed with `-c` on each command, it is never written to `.git/config`
    (unlike embedding the token in the remote URL). GitHub accepts the basic
    scheme with any PAT as the password.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]


def _repo_root(data_dir: Path) -> Path:
    out = _git(data_dir, "rev-parse", "--show-toplevel", secrets=())
    if not out:
        raise RuntimeError(f"{data_dir} is not inside a git repository")
    return Path(out)


def _remote_url(repo: Path) -> str:
    try:
        url = _git(repo, "remote", "get-url", "origin", secrets=())
    except RuntimeError:
        url = ""
    if not url:
        url = f"https://github.com/{get_settings().github_repository}.git"
    return url


def _content_tree(data_dir: Path) -> str:
    """Write the tree for `data_dir`'s contents (rooted at the data files).

    Runs `git add` with a temporary index and the work tree pointed at
    `data_dir`, so the repository's real index and `main` are never touched
    and `.gitignore` (which ignores `/data/` on main) does not apply.
    """
    with tempfile.TemporaryDirectory(prefix="cyberpulse-push-") as tmp:
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_WORK_TREE": str(data_dir),
        }
        _git(data_dir, "add", "-f", "-A", extra_env=env, secrets=())
        return _git(data_dir, "write-tree", extra_env=env, secrets=())


def _remote_tip(
    repo: Path, url: str, branch: str, auth: list[str], secrets: tuple[str, ...]
) -> str | None:
    out = _git(repo, *auth, "ls-remote", url, f"refs/heads/{branch}", secrets=secrets)
    if not out:
        return None
    return out.split()[0]


def _remote_tree(
    repo: Path, url: str, branch: str, auth: list[str], secrets: tuple[str, ...]
) -> str:
    """Fetch the remote tip (read-only) and return its root tree sha."""
    _git(
        repo,
        *auth,
        "fetch",
        "--force",
        url,
        f"refs/heads/{branch}",
        secrets=secrets,
    )
    return _git(repo, "rev-parse", "FETCH_HEAD^{tree}", secrets=secrets)


def _git(
    repo: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> str:
    """Run `git -C repo args...`, returning stripped stdout.

    Failures raise `RuntimeError`; `secrets` are redacted from everything the
    error message repeats (command echo included), so a token can never leak
    through a traceback or a stray `GIT_TRACE`.

    Git never prompts (there is no one to answer), and a command still running
    after `GIT_TIMEOUT_SECONDS` is killed and raised as a failure.
    """
    cmd = ["git", "-C", str(repo), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}
    echo = _redact(" ".join(args), secrets)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # `from None`: the original exception repeats the command, auth header and all.
        raise RuntimeError(f"git {echo} timed out after {GIT_TIMEOUT_SECONDS}s") from None
    if proc.returncode != 0:
        detail = _redact(proc.stderr.strip() or proc.stdout.strip(), secrets)
        raise RuntimeError(f"git {echo} failed: {detail}")
    return proc.stdout.strip()


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m worker.publish.push",
        description="Force-push the built data directory to the data branch.",
    )
    parser.add_argument("data_dir", nargs="?", default=None)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do every check and report, but push nothing",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    data_dir = Path(args.data_dir) if args.data_dir else settings.data_dir
    try:
        result = push_data(data_dir, branch=args.branch, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 1
    if result.reason == "no changes":
        print("data unchanged; nothing pushed")
    elif result.pushed:
        print(f"pushed {result.commit_sha} to {args.branch}")
    else:
        print(f"dry run: would push {result.commit_sha} to {args.branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
