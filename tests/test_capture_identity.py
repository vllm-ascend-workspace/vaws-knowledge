"""Agent corrections retain candidate and public identities through MCP and CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from vaws_knowledge.contribution.pending import iter_pending, save_pending
from vaws_knowledge.publishing import run_once
from vaws_knowledge.server.capture import CaptureRejected, capture
from vaws_knowledge.server.capture_cli import main as capture_main
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.server.mcp_server import KnowledgeService


def configuration(tmp_path):
    return load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "layers": {"candidate": {"root": str(tmp_path / "candidate")}, "shared": {"roots": []}},
        "publishing": {"enabled": True, "repository": "example/corpus", "fork": "author/corpus",
                       "git_repo": str(tmp_path / "fork")},
    }, env={})


@pytest.mark.parametrize("kind", ["knowledge", "experience"])
def test_mcp_ref_renames_in_place_and_returns_stable_public_target(tmp_path, kind):
    config = configuration(tmp_path)
    service = KnowledgeService(config=config)
    first, error = service.call_tool(f"{kind}_capture", {"title": "Graph buffer", "content": "Initial observation."})
    assert not error
    second, error = service.call_tool(f"{kind}_capture", {
        "title": "Graph buffer lifetime", "content": "Corrected scope and evidence.", "ref": first["ref"],
    })
    assert not error
    assert first["ref"] == second["ref"]
    assert first["path"] == second["path"]
    assert first["contribution"]["public_relpath"] == second["contribution"]["public_relpath"]
    assert len(iter_pending(config.state_root)) == 1
    assert "Corrected scope" in Path(second["path"]).read_text()


def test_explicit_category_paths_keep_equal_knowledge_entries_separate(tmp_path):
    config = configuration(tmp_path)
    first = capture(title="Buffer lifetime", content="Buffers live through replay.", config=config, index=False,
                    public_relpath="knowledge/graph/buffers.md")
    second = capture(title="Buffer lifetime", content="Buffers live through replay.", config=config, index=False,
                     public_relpath="knowledge/operators/buffers.md")
    assert first["path"] != second["path"]
    assert len(iter_pending(config.state_root)) == 2
    with pytest.raises(CaptureRejected, match="multiple candidates"):
        capture(title="Buffer lifetime", content="Ambiguous edit.", config=config, index=False)
    revised = capture(title="Graph allocation", content="Rechecked current allocation.", ref=first["ref"],
                      config=config, index=False)
    assert revised["contribution"]["public_relpath"] == "knowledge/graph/buffers.md"
    assert "Buffers live" in Path(second["path"]).read_text()


def test_explicit_experience_correction_uses_local_candidate_and_keeps_filename(tmp_path):
    config = configuration(tmp_path).for_kind("experience")
    target = "experience/old-hash-case.md"
    first = capture(title="Corrected explanation", content="The earlier cause was only a hypothesis.",
                    public_relpath=target, config=config, index=False)
    second = capture(title="Additional evidence", content="This evidence narrows the historical conclusion.",
                     public_relpath=target, config=config, index=False)
    assert first["path"] == second["path"]
    assert second["contribution"]["public_relpath"] == target
    assert iter_pending(config.state_root)[0].requires_existing


@pytest.mark.parametrize("target", ["experience/wrong-kind.md", "knowledge/../escape.md", "knowledge/C:/x.md"])
def test_invalid_public_target_fails_before_candidate_write(tmp_path, target):
    config = configuration(tmp_path)
    with pytest.raises(CaptureRejected):
        capture(title="Reject", content="Must not write this.", public_relpath=target, config=config, index=False)
    assert not list(tmp_path.rglob("*.md"))


def test_unknown_ref_and_conflicting_selectors_do_not_write(tmp_path):
    config = configuration(tmp_path)
    with pytest.raises(CaptureRejected):
        capture(title="Reject", content="No implicit creation.", ref="missing.md", config=config, index=False)
    with pytest.raises(CaptureRejected):
        capture(title="Reject", content="No ambiguous binding.", ref="missing.md",
                public_relpath="knowledge/entry.md", config=config, index=False)
    assert not list(tmp_path.rglob("*.md"))


def test_cli_accepts_fixed_entry_and_then_ref_for_title_correction(tmp_path, capsys):
    config = configuration(tmp_path)
    with patch("vaws_knowledge.server.capture_cli.load_config", return_value=config):
        assert capture_main(["--title", "Buffers", "--content", "Initial conclusion.",
                             "--public-relpath", "knowledge/graph/buffers.md"]) == 0
        first = json.loads(capsys.readouterr().out)
        assert capture_main(["--title", "Graph buffers", "--content", "Updated conclusion.",
                             "--ref", first["ref"]]) == 0
        second = json.loads(capsys.readouterr().out)
    assert second["contribution"]["public_relpath"] == "knowledge/graph/buffers.md"
    assert second["ref"] == first["ref"]


def test_worker_closed_poll_cannot_overwrite_newer_capture(tmp_path):
    config = configuration(tmp_path)
    initial = capture(title="A result", content="The original result.", config=config, index=False)
    pending = iter_pending(config.state_root)[0]
    pending.status = "pr_open"
    pending.pr_number = 1
    pending.pr_url = "https://github.com/example/corpus/pull/1"
    save_pending(config.state_root, pending)

    class Github:
        def get(self, path):
            capture(title="A corrected result", content="A newer result arrived while polling.",
                    ref=initial["ref"], config=config, index=False)
            return {"state": "closed", "merged": True}

    with patch("vaws_knowledge.github_transport.github_token", return_value="test"), \
         patch("vaws_knowledge.contribution.github.UrllibContributionGitHub", return_value=Github()):
        result = run_once(config, force=True)
    latest = iter_pending(config.state_root)[0]
    assert latest.status == "pending"
    assert latest.title == "A corrected result"
    assert result["contributions"][0]["status"] == "pending"


def test_feedback_tool_requires_no_reason_and_preserves_compact_result(tmp_path):
    config = configuration(tmp_path)
    service = KnowledgeService(config=config)
    result = {"status": "ok", "vote": "+1", "counts": {"+1": 1, "-1": 0},
              "issue_url": "https://github.com/example/corpus/issues/3"}
    with patch("vaws_knowledge.feedback.experience_feedback", return_value=result) as feedback:
        actual, error = service.call_tool("experience_feedback", {"ref": "experience/case.md", "vote": "+1"})
    assert not error and actual == result
    feedback.assert_called_once_with(config, "experience/case.md", "+1")


def test_feedback_cli_negative_vote_and_compact_json(tmp_path, capsys):
    from vaws_knowledge.cli import main

    config = configuration(tmp_path)
    with patch("vaws_knowledge.feedback_cli.load_config", return_value=config), \
         patch("vaws_knowledge.feedback_cli.experience_feedback", return_value={"status": "ok", "vote": "-1"}):
        assert main(["experience-feedback", "--ref", "experience/case.md", "--vote=-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "vote": "-1"}
