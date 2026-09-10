from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from contribution.support import FakeContributionGitHub, init_git_repo
from distribution.helpers import make_corpus, make_manifest, make_pack

from vaws_knowledge.contribution.pending import iter_pending
from vaws_knowledge.contribution.submit import SubmitConfig, submit_pending
from vaws_knowledge.distribution.release import GitHubReleaseSource
from vaws_knowledge.distribution.errors import SourceUnavailable
from vaws_knowledge.distribution.sync import SwitchLock
from vaws_knowledge.publishing import queue_capture, run_once
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.summary_hook import capture_summary
from vaws_knowledge.corpus_check import validate_corpus


def configured(tmp_path):
    return load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "layers": {"candidate": {"root": str(tmp_path / "candidate")}},
        "publishing": {"enabled": True, "repository": "example/corpus", "fork": "author/corpus",
                       "git_repo": str(tmp_path / "fork")},
    }, env={})


def test_capture_queues_without_network_or_index(tmp_path):
    config = configured(tmp_path)
    with patch("vaws_knowledge.github_transport.gh", side_effect=AssertionError("network in capture")), \
         patch("vaws_knowledge.server.capture.backend_for_config", side_effect=AssertionError("index in hook")):
        saved = capture(title="A local result", content="A candidate can be saved before its public PR exists.",
                        config=config, index=False)
    assert saved["ok"] and saved["contribution"]["status"] == "pending"
    assert len(iter_pending(config.state_root)) == 1


def test_summary_event_has_no_transcript_dependency_and_is_idempotent(tmp_path):
    config = configured(tmp_path)
    payload = {"hook_event_name": "Stop", "session_id": "native-id",
               "transcript_path": str(tmp_path / "missing-transcript"),
               "last_assistant_message": "The launch environment now preserves the prepared CANN paths."}
    first = capture_summary(payload, config=config, client="codex")
    second = capture_summary(payload, config=config, client="codex")
    assert first["ref"] == second["ref"]
    assert len(iter_pending(config.state_root)) == 1
    payload["hook_event_name"] = "PreToolUse"
    assert capture_summary(payload, config=config, client="codex")["status"] == "no_summary"


def test_private_candidate_stays_unchanged_and_public_copy_is_redacted(tmp_path):
    config = configured(tmp_path)
    candidate = tmp_path / "private.md"
    raw = "# Runtime observation\n\nThe source was /Users/example/code/private-file.py. The local repair succeeded.\n"
    candidate.write_text(raw)
    queue_capture(config, candidate)
    public = next((config.state_root / "contribution/public").glob("*.md")).read_text()
    assert "/Users/example" not in public
    assert candidate.read_text() == raw


def test_disabled_capture_does_not_queue_historical_content(tmp_path):
    config = configured(tmp_path)
    config.publishing = {}
    saved = capture(title="Local only", content="This observation remains private until publishing is configured.",
                    config=config, index=False)
    assert saved["contribution"]["status"] == "local_only"
    assert iter_pending(config.state_root) == []


def test_live_worker_lock_skips_concurrent_submission(tmp_path):
    config = configured(tmp_path)
    with SwitchLock(config.state_root / "publishing.lock"):
        assert run_once(config, force=True)["status"] == "busy"


def test_fork_branch_is_pushed_before_pr_creation_and_retry_reuses_it(tmp_path):
    config = configured(tmp_path)
    fork = tmp_path / "fork"
    init_git_repo(fork)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    for name in ("origin", "upstream"):
        subprocess.run(["git", "-C", str(fork), "remote", "add", name, str(remote)], check=True)
    subprocess.run(["git", "-C", str(fork), "push", "origin", "main"], check=True, capture_output=True)
    capture(title="Fork transfer", content="The public branch must exist remotely before opening a pull request.",
            config=config, index=False)
    record = iter_pending(config.state_root)[0]

    class Github(FakeContributionGitHub):
        def post(self, path, body):
            if path.endswith("/pulls"):
                pushed = subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", record.branch], text=True).strip()
                assert len(pushed) == 40
            return super().post(path, body)

    github = Github()
    kwargs = dict(state_root=config.state_root, public_root=config.state_root / "contribution/public",
                  git_repo=fork, github=github, config=SubmitConfig("example/corpus", "example/corpus", push_remote="origin"))
    result = submit_pending(record, **kwargs)
    assert result.status == "pr_open"
    assert submit_pending(result, **kwargs).pr_number == result.pr_number


def release_source(tmp_path):
    pack = tmp_path / "source.ovpack"
    entries = make_corpus()
    manifest = make_manifest(pack, entries, make_pack(pack, entries))
    source = GitHubReleaseSource("example/corpus", cache_dir=tmp_path / "cache")
    tag = "knowledge-" + manifest["version_id"]
    metadata = {"id": 1, "tag_name": tag, "assets": [
        {"name": name, "state": "uploaded", "browser_download_url": f"https://github.com/example/corpus/releases/download/{tag}/{name}"}
        for name in ("release.json", pack.name)
    ]}
    source._json = lambda suffix: {"object": {"sha": manifest["source"]["git_sha"]}} if suffix.startswith("git/") else metadata
    downloads = []

    def download(asset, destination, **_kwargs):
        downloads.append(asset["name"])
        destination.write_bytes(json.dumps(manifest).encode() if asset["name"] == "release.json" else pack.read_bytes())

    source._download = download
    return source, manifest, downloads


def test_release_fetch_caches_verified_pack_and_checks_git_identity(tmp_path):
    source, manifest, downloads = release_source(tmp_path)
    source.fetch()
    source.fetch()
    assert downloads.count("source.ovpack") == 1
    old = source._json
    source._json = lambda suffix: {"object": {"sha": "f" * 40}} if suffix.startswith("git/") else old(suffix)
    with pytest.raises(SourceUnavailable, match="Git identity"):
        source.fetch()


def test_release_download_corruption_is_refused(tmp_path):
    source, manifest, _downloads = release_source(tmp_path)
    manifest["pack"]["sha256"] = "0" * 64
    with pytest.raises(SourceUnavailable, match="integrity"):
        source.fetch()


def test_corpus_check_blocks_bad_format_and_private_paths_without_echoing_them(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "bad.md").write_text("# Missing body\n")
    (root / "private.md").write_text("# Private path\n\nLocated at /Users/example/private/source.py.\n")
    result = validate_corpus(tmp_path)
    assert not result["ok"]
    assert len(result["problems"]) == 2
    assert "/Users/example" not in json.dumps(result)
