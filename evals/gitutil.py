"""Small git helpers. Every call is argv-only (no shell)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from vikram.logging import get_logger

logger = get_logger(__name__)


class GitError(RuntimeError):
    pass


# Set on the results commit so the post-commit hook ignores it.
SKIP_ENV = "VIKRAM_EVALS_SKIP"


def git(
    repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if check and completed.returncode != 0:
        raise GitError(
            f"git {args[0] if args else ''} failed "
            f"(exit {completed.returncode}): {completed.stderr.strip()[:300]}"
        )
    return completed.stdout


def repo_root(start: Path | None = None) -> Path:
    return Path(git(start or Path.cwd(), "rev-parse", "--show-toplevel").strip())


def rev_parse(repo: Path, ref: str) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or None


def parent_of(repo: Path, sha: str) -> str | None:
    return rev_parse(repo, f"{sha}~1")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def distance(repo: Path, ancestor: str, descendant: str) -> int:
    """Commits reachable from ``descendant`` but not ``ancestor``."""
    return int(git(repo, "rev-list", "--count", f"{ancestor}..{descendant}").strip())


def subject(repo: Path, sha: str) -> str:
    return git(repo, "log", "-1", "--format=%s", sha).strip()


def commit_time(repo: Path, sha: str) -> str:
    return git(repo, "log", "-1", "--format=%cI", sha).strip()


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--no-renames", base, head)
    return [line for line in out.splitlines() if line]


def numstat(repo: Path, base: str, head: str, paths: list[str]) -> tuple[int, int]:
    if not paths:
        return 0, 0
    out = git(repo, "diff", "--numstat", "--no-renames", base, head, "--", *paths)
    added = removed = 0
    for line in out.splitlines():
        a, r, *_ = line.split("\t")
        if a.isdigit():
            added += int(a)
        if r.isdigit():
            removed += int(r)
    return added, removed


def show(repo: Path, ref: str, path: str) -> str | None:
    completed = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def current_branch(repo: Path) -> str | None:
    completed = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or None


def operation_in_progress(repo: Path) -> bool:
    """True during a merge, rebase, cherry-pick, revert or bisect."""
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    markers = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-merge",
        "rebase-apply",
    )
    return any((git_dir / marker).exists() for marker in markers)


def worktree_add(repo: Path, path: Path, sha: str) -> None:
    if path.exists():
        worktree_remove(repo, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", "--force", str(path), sha)


def worktree_remove(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)


def commit_paths(
    repo: Path, paths: list[str], message: str, *, attempts: int = 4
) -> bool:
    """Commit only ``paths``, leaving anything else the user staged alone.

    Retries while another git process holds the index lock.
    """
    for attempt in range(attempts):
        try:
            git(repo, "add", "--", *paths)
            git(
                repo,
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-verify",
                "--quiet",
                "-m",
                message,
                "--only",
                "--",
                *paths,
                env={SKIP_ENV: "1"},
            )
            return True
        except GitError as exc:
            if "index.lock" not in str(exc) or attempt == attempts - 1:
                logger.warning(
                    "eval_history_commit_failed",
                    attempt=attempt + 1,
                )
                return False
            time.sleep(2**attempt)
    return False
