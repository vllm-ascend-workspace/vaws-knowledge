"""Independent feedback checks: canonical targets, account ownership and retries."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from vaws_knowledge.contribution.github import GitHubError
from vaws_knowledge.feedback import experience_feedback
from vaws_knowledge.server.layers import load_config


REPOSITORY = "vllm-ascend-workspace/vaws-knowledge-corpus"
TARGET = "experience/case-one.md"
ACTOR = {"id": 7, "login": "current-reviewer"}
OTHER = {"id": 8, "login": "another-reviewer"}


def marker(target):
    return f"<!-- vaws-experience-feedback: {target} -->"


class FeedbackGitHub:
    def __init__(self):
        self.calls = []
        self.files = {f"corpus/{TARGET}": {"type": "file", "path": f"corpus/{TARGET}", "sha": "a" * 40}}
        self.issues = []
        self.reactions = {}
        self.next_reaction = 1000
        self.failure = None

    def issue(self, target=TARGET, *, number=None, pull=False, state="open"):
        number = number or len(self.issues) + 1
        issue = {"number": number, "title": f"Experience feedback: {target}",
                 "body": marker(target), "state": state,
                 "html_url": f"https://github.com/{REPOSITORY}/issues/{number}"}
        if pull:
            issue["pull_request"] = {"url": "https://api.github.com/pulls/999"}
        self.issues.append(issue)
        self.reactions.setdefault(number, [])
        return issue

    def reaction(self, issue, content, user=ACTOR):
        self.next_reaction += 1
        reaction = {"id": self.next_reaction, "content": content, "user": dict(user)}
        self.reactions.setdefault(issue, []).append(reaction)
        return reaction

    def _parts(self, path):
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if parts[0] != "user":
            assert parts[:3] == ["repos", *REPOSITORY.split("/")], path
        return parts, parse_qs(parsed.query)

    @staticmethod
    def _page(items, query):
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("per_page", ["30"])[0])
        return [dict(item) for item in items[(page - 1) * size:page * size]]

    def _fail(self, stage):
        if self.failure == stage:
            self.failure = None
            raise GitHubError(503, "feedback-test", "temporary transport failure")

    def get(self, path):
        self.calls.append(("GET", path, None))
        self._fail("get")
        parts, query = self._parts(path)
        if parts == ["user"]:
            return dict(ACTOR)
        if parts[3] == "contents":
            requested = unquote("/".join(parts[4:]))
            assert query.get("ref", ["main"])[0] == "main"
            if requested not in self.files:
                raise GitHubError(404, path, "not found")
            return dict(self.files[requested])
        if parts[3:] == ["issues"]:
            assert query.get("state") == ["all"], "closed feedback issues remain reusable"
            return self._page(self.issues, query)
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "reactions":
            return self._page(self.reactions[int(parts[4])], query)
        if len(parts) == 5 and parts[3] == "issues":
            issue = next(item for item in self.issues if item["number"] == int(parts[4]))
            reactions = self.reactions[issue["number"]]
            return {**issue, "reactions": {vote: sum(r["content"] == vote for r in reactions) for vote in ("+1", "-1")}}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body):
        self.calls.append(("POST", path, dict(body)))
        parts, _ = self._parts(path)
        if parts[3:] == ["issues"]:
            self._fail("create_issue_before")
            issue = self.issue(number=max([i["number"] for i in self.issues] or [0]) + 1)
            issue.update(body)
            self._fail("create_issue_after")
            return dict(issue)
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "reactions":
            self._fail("reaction_before")
            number = int(parts[4])
            existing = next((r for r in self.reactions[number]
                             if r["user"]["id"] == ACTOR["id"] and r["content"] == body["content"]), None)
            result = existing or self.reaction(number, body["content"])
            self._fail("reaction_after")
            return dict(result)
        raise AssertionError(f"unexpected POST {path}")

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        self._fail("delete")
        parts, _ = self._parts(path)
        assert len(parts) == 7 and parts[3] == "issues" and parts[5] == "reactions"
        number, reaction_id = int(parts[4]), int(parts[6])
        reaction = next(r for r in self.reactions[number] if r["id"] == reaction_id)
        assert reaction["user"]["id"] == ACTOR["id"], "must never remove another account's feedback"
        self.reactions[number].remove(reaction)

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]


@pytest.fixture
def feedback(tmp_path):
    config = load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "publishing": {"enabled": True, "repository": REPOSITORY, "fork": "author/vaws-knowledge-corpus",
                       "default_branch": "main"},
        "shared_sync": {"enabled": False},
    }, env={}).for_kind("experience")
    return config, FeedbackGitHub()


@pytest.mark.parametrize("ref", [
    TARGET,
    f"viking://resources/shared/{TARGET}",
    f"viking://resources/shared/bootstrap/{TARGET}",
    f"viking://resources/shared/v0123456789ab/{TARGET}",
    f"viking://resources/shared/repairs/0123456789abcdef/v0123456789ab/{TARGET}",
])
def test_public_experience_ref_creates_only_one_canonical_feedback_issue(feedback, tmp_path, ref):
    config, api = feedback
    result = experience_feedback(config, ref, "+1", github=api)
    assert result["status"] == "ok"
    assert result["issue_url"] == f"https://github.com/{REPOSITORY}/issues/1"
    assert result["counts"] == {"+1": 1, "-1": 0}
    assert len(api.issues) == 1
    assert marker(TARGET) in api.issues[0]["body"]
    assert not list(tmp_path.rglob("*.md")), "feedback must not create searchable Markdown notes"


@pytest.mark.parametrize("ref", [
    "knowledge/case-one.md", "../experience/case-one.md", "experience/../knowledge/note.md",
    "viking://resources/shared/v0123456789ab/experience/%2e%2e/knowledge/note.md", "experience/case-one.md?query=private",
    "experience/case-one.md#private", "experience\\case-one.md", "C:/experience/case-one.md",
    "viking://resources/candidate/experience/case-one.md", "viking://resources/project/experience/case-one.md",
    "viking://resources/shared/vINVALID/experience/case-one.md",
    "viking://foreign/shared/v0123456789ab/experience/case-one.md",
    "https://github.com/foreign/repository/blob/main/corpus/experience/case-one.md",
])
def test_local_foreign_or_unsafe_refs_cannot_trigger_public_writes(feedback, ref):
    config, api = feedback
    result = experience_feedback(config, ref, "+1", github=api)
    assert result["status"] == "invalid_ref"
    assert api.mutations() == []


@pytest.mark.parametrize("vote", [True, False, 1, -1, 1.0, None, "1", "helpful", "0"])
def test_only_explicit_plus_or_minus_string_votes_are_accepted(feedback, vote):
    config, api = feedback
    assert experience_feedback(config, TARGET, vote, github=api)["status"] == "invalid_vote"
    assert api.calls == []


@pytest.mark.parametrize("settings", [{"enabled": False, "fork": "author/corpus"}, {"enabled": True}, {}])
def test_feedback_respects_existing_public_contribution_authorization(feedback, settings):
    config, api = feedback
    config.publishing = {"repository": REPOSITORY, **settings}
    assert experience_feedback(config, TARGET, "+1", github=api)["status"] == "disabled"
    assert api.calls == []


@pytest.mark.parametrize("entry", [None, {"type": "dir"}, {"type": "symlink"}, {"type": "submodule"},
                                    {"type": "file", "path": "corpus/knowledge/not-an-experience.md"}])
def test_feedback_requires_the_exact_existing_canonical_experience_file(feedback, entry):
    config, api = feedback
    if entry is None:
        api.files.clear()
    else:
        api.files[f"corpus/{TARGET}"] = {"path": f"corpus/{TARGET}", **entry}
    result = experience_feedback(config, TARGET, "+1", github=api)
    assert result["status"] in {"not_found", "invalid_ref"}
    assert api.mutations() == []


def test_repeated_vote_is_idempotent_and_switch_only_removes_the_current_accounts_old_vote(feedback):
    config, api = feedback
    issue = api.issue(state="closed")
    mine = api.reaction(issue["number"], "+1")
    theirs = api.reaction(issue["number"], "+1", OTHER)
    unrelated = api.reaction(issue["number"], "heart")
    same = experience_feedback(config, TARGET, "+1", github=api)
    assert same["status"] == "ok"
    assert api.mutations() == []
    changed = experience_feedback(config, TARGET, "-1", github=api)
    assert changed["status"] == "ok"
    assert changed["counts"] == {"+1": 1, "-1": 1}
    assert mine not in api.reactions[1]
    assert theirs in api.reactions[1] and unrelated in api.reactions[1]
    writes = api.mutations()
    assert [call[0] for call in writes] == ["POST", "DELETE"]
    assert len(api.issues) == 1


def test_paginated_issue_and_reaction_lookup_ignores_prs_and_finds_the_current_account(feedback):
    config, api = feedback
    api.issue(pull=True)
    for number in range(2, 101):
        api.issue(target=f"experience/unrelated-{number}.md", number=number)
    issue = api.issue(number=101)
    for number in range(100):
        api.reaction(issue["number"], "+1", {"id": number + 100, "login": f"reviewer-{number}"})
    api.reaction(issue["number"], "-1")
    result = experience_feedback(config, TARGET, "+1", github=api)
    assert result["status"] == "ok"
    assert result["issue_url"].endswith("/issues/101")
    assert result["counts"] == {"+1": 101, "-1": 0}
    assert len(api.issues) == 101
    assert api.reactions[1] == []


def test_same_basename_in_different_experience_directories_has_separate_feedback(feedback):
    config, api = feedback
    for target in ("experience/graph/case.md", "experience/eager/case.md"):
        api.files[f"corpus/{target}"] = {"type": "file", "path": f"corpus/{target}"}
        result = experience_feedback(config, target, "+1", github=api)
        assert result["status"] == "ok"
    assert len(api.issues) == 2
    assert marker("experience/graph/case.md") in api.issues[0]["body"]
    assert marker("experience/eager/case.md") in api.issues[1]["body"]


def test_an_issue_with_a_conflicting_marker_is_not_reused_even_if_its_title_matches(feedback):
    config, api = feedback
    misleading = api.issue(target="experience/another-case.md")
    misleading["title"] = f"Experience feedback: {TARGET}"
    correct = api.issue()
    result = experience_feedback(config, TARGET, "+1", github=api)
    assert result["status"] == "ok"
    assert result["issue_url"].endswith(f"/issues/{correct['number']}")
    assert api.reactions[misleading["number"]] == []


def test_an_ordinary_issue_with_the_same_title_is_not_a_feedback_target(feedback):
    config, api = feedback
    ordinary = api.issue()
    ordinary["body"] = "This issue discusses the experience, but does not declare a feedback target."
    result = experience_feedback(config, TARGET, "+1", github=api)
    assert result["status"] == "ok"
    assert result["issue_url"].endswith("/issues/2")
    assert len(api.issues) == 2
    assert api.reactions[ordinary["number"]] == []


def test_percent_in_a_literal_git_path_is_escaped_once_and_not_used_as_traversal(feedback):
    config, api = feedback
    target = "experience/literal%2Fname.md"
    api.files[f"corpus/{target}"] = {"type": "file", "path": f"corpus/{target}"}
    result = experience_feedback(config, target, "+1", github=api)
    assert result["status"] == "ok"
    requested = next(path for method, path, _ in api.calls if "/contents/" in path)
    assert "literal%252Fname.md" in requested
    assert marker(target) in api.issues[0]["body"]


@pytest.mark.parametrize("stage", ["reaction_before", "reaction_after", "delete"])
def test_failed_vote_switch_is_retryable_and_does_not_lose_the_previous_vote(feedback, stage):
    config, api = feedback
    api.issue()
    previous = api.reaction(1, "+1")
    api.failure = stage
    failed = experience_feedback(config, TARGET, "-1", github=api)
    assert failed["status"] == "error"
    assert failed["retryable"] is True
    assert previous in api.reactions[1]
    recovered = experience_feedback(config, TARGET, "-1", github=api)
    assert recovered["status"] == "ok"
    assert recovered["counts"] == {"+1": 0, "-1": 1}
    assert len(api.reactions[1]) == 1
    assert len(api.issues) == 1


def test_lost_issue_create_response_is_recovered_without_another_issue(feedback):
    config, api = feedback
    api.failure = "create_issue_after"
    failed = experience_feedback(config, TARGET, "+1", github=api)
    assert failed["status"] == "error"
    assert failed["retryable"] is True
    assert len(api.issues) == 1
    recovered = experience_feedback(config, TARGET, "+1", github=api)
    assert recovered["status"] == "ok"
    assert len(api.issues) == 1
    assert recovered["counts"] == {"+1": 1, "-1": 0}
