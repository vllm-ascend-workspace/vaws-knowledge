"""Local git writes for a knowledge fork clone.

Network push is the caller's job (or a later transport). This module only
touches a local repository the caller owns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vaws_knowledge.contribution.documents import require_git_sha, require_relative_path, safe_file_path
from vaws_knowledge.contribution.errors import TransportError


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransportError(f"git {args[0]} unavailable: {type(exc).__name__}") from exc
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
    require_existing: bool = False,
) -> str:
    """Create or reuse ``branch`` with ``relpath`` set to ``content``. Idempotent."""

    posix = require_relative_path(relpath)
    run_git(repo, ["check-ref-format", "--branch", branch])
    if require_existing:
        entry = run_git(repo, ["ls-tree", start_ref, "--", posix]).stdout.strip()
        if not entry or entry.split()[0] not in {"100644", "100755"}:
            raise TransportError("explicit public revision target does not exist as a regular file in the base")
    exists = run_git(repo, ["rev-parse", "--verify", branch], check=False)
    if exists.returncode == 0:
        run_git(repo, ["checkout", branch])
    else:
        run_git(repo, ["checkout", "-B", branch, start_ref])
    dest = safe_file_path(repo, posix)
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
