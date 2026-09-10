"""GitHub CLI transport, reusing the user's existing authentication.

Credentials stay in the environment or the gh credential store. Repository
names and asset paths are data passed as separate arguments, never shell code.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


def repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("expected a GitHub owner/repository name")
    return value


def gh(args: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )
    if check and result.returncode:
        raise RuntimeError((result.stderr or "GitHub operation failed").strip()[:1200])
    return result


def github_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    result = gh(["auth", "token"], check=False, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("GitHub authentication unavailable; sign in with gh auth login")
    return result.stdout.strip()


def api(path: str) -> Any:
    return json.loads(gh(["api", path]).stdout)


def ensure_fork(upstream: str, directory: Path) -> dict[str, str]:
    """Create/reuse the user's fork and one package-owned contribution clone."""
    upstream = repository_name(upstream)
    owner = api("user")["login"]
    fork = repository_name(f"{owner}/{upstream.split('/')[1]}")
    if fork != upstream:
        existing = gh(["api", f"repos/{fork}"], check=False)
        if existing.returncode:
            gh(["repo", "fork", upstream, "--clone=false"])
            metadata = api(f"repos/{fork}")
        else:
            metadata = json.loads(existing.stdout)
        if metadata.get("parent", {}).get("full_name", "").lower() != upstream.lower():
            raise RuntimeError("the matching personal repository is not a fork of the configured corpus")
    metadata = api(f"repos/{upstream}")
    branch = metadata["default_branch"]
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    if not directory.exists():
        gh(["repo", "clone", fork, str(directory)], timeout=180)
    from vaws_knowledge.contribution.gitops import run_git

    origin = run_git(directory, ["remote", "get-url", "origin"]).stdout.strip()
    normalized = origin.removesuffix(".git").replace("git@github.com:", "https://github.com/")
    if normalized.lower() != f"https://github.com/{fork}".lower():
        raise RuntimeError("contribution checkout origin differs from the configured fork")
    remote = run_git(directory, ["remote", "get-url", "upstream"], check=False)
    wanted = f"https://github.com/{upstream}.git"
    if remote.returncode:
        run_git(directory, ["remote", "add", "upstream", wanted])
    elif remote.stdout.strip().removesuffix(".git").replace("git@github.com:", "https://github.com/").lower() != wanted.removesuffix(".git").lower():
        raise RuntimeError("contribution checkout upstream differs from the configured corpus")
    return {"upstream": upstream, "fork": fork, "default_branch": branch, "git_repo": str(directory)}
