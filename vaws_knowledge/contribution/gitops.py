"""Local git writes for a knowledge fork clone.

Network push is the caller's job (or a later transport). This module only
touches a local repository the caller owns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vaws_knowledge.contribution.documents import require_git_sha
from vaws_knowledge.contribution.errors import TransportError


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git failed").strip()
        raise TransportError(f"git {' '.join(args)} failed: {detail}")
    return proc


def current_sha(repo: Path, ref: str = "HEAD") -> str:
    proc = run_git(repo, ["rev-parse", ref])
    return require_git_sha(proc.stdout.strip())


def commit_public_file(
    repo: Path,
    *,
    branch: str,
    relpath: str,
    content: str,
    message: str,
    start_ref: str = "HEAD",
) -> str:
    """Create or reuse ``branch`` with ``relpath`` set to ``content``. Idempotent."""

    posix = relpath.replace("\\", "/").lstrip("/")
    if not posix or ".." in posix.split("/"):
        raise TransportError("invalid contribution path")
    exists = run_git(repo, ["rev-parse", "--verify", branch], check=False)
    if exists.returncode == 0:
        run_git(repo, ["checkout", branch])
    else:
        run_git(repo, ["checkout", "-B", branch, start_ref])
    dest = Path(repo) / posix
    dest.parent.mkdir(parents=True, exist_ok=True)
    encoded = content if content.endswith("\n") else content + "\n"
    if dest.is_file() and dest.read_text(encoding="utf-8") == encoded:
        status = run_git(repo, ["status", "--porcelain", "--", posix])
        if not (status.stdout or "").strip():
            return current_sha(repo)
    dest.write_text(encoded, encoding="utf-8", newline="\n")
    run_git(repo, ["add", "--", posix])
    staged = run_git(repo, ["diff", "--cached", "--name-only"])
    if not (staged.stdout or "").strip():
        return current_sha(repo)
    run_git(repo, ["commit", "-m", message])
    return current_sha(repo)


def commit_files(
    repo: Path,
    *,
    branch: str,
    files: dict[str, str],
    message: str,
    start_ref: str = "HEAD",
) -> str:
    """Commit several paths on ``branch``. Idempotent when the tree is unchanged."""

    exists = run_git(repo, ["rev-parse", "--verify", branch], check=False)
    if exists.returncode == 0:
        run_git(repo, ["checkout", branch])
    else:
        run_git(repo, ["checkout", "-B", branch, start_ref])
    changed = False
    for relpath, content in files.items():
        posix = relpath.replace("\\", "/").lstrip("/")
        if not posix or ".." in posix.split("/"):
            raise TransportError("invalid contribution path")
        dest = Path(repo) / posix
        dest.parent.mkdir(parents=True, exist_ok=True)
        encoded = content if content.endswith("\n") else content + "\n"
        if dest.is_file() and dest.read_text(encoding="utf-8") == encoded:
            continue
        dest.write_text(encoded, encoding="utf-8", newline="\n")
        run_git(repo, ["add", "--", posix])
        changed = True
    if not changed:
        staged = run_git(repo, ["diff", "--cached", "--name-only"])
        if not (staged.stdout or "").strip():
            return current_sha(repo)
    run_git(repo, ["commit", "-m", message])
    return current_sha(repo)
