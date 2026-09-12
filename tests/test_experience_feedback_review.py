"""Independent feedback checks: canonical targets, account ownership and retries."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from vaws_knowledge.contribution.github import GitHubError
from vaws_knowledge.feedback import experience_feedback
from vaws_knowledge.server.layers import load_config


REPOSITORY = "vllm-ascend-workspace/vaws-knowledge-corpus"
TARGET = "experience/case-one.md"
ACTOR = {"id": 7, "login": "current-reviewer"}
OTHER = {"id": 8, "login": "another-reviewer"}
EVENT_ID = "0123456789abcdef0123456789abcdef"


def marker(target):
    return f"<!-- vaws-experience-feedback: {target} -->"


def event_body(vote, request_id):
    return f"{vote}\n\n<!-- vaws-experience-vote: {request_id} -->\n"


class FeedbackGitHub:
    def __init__(self):
        self.calls = []
        self.files = {f"corpus/{TARGET}": {"type": "file", "path": f"corpus/{TARGET}", "sha": "a" * 40}}
        self.issues = []
        self.comments = {}
        self.next_comment = 1000
        self.failure = None
        self.duplicate_after_post = False
        self.fail_comment_reads = False

    def issue(self, target=TARGET, *, number=None, pull=False, state="open"):
        number = number or len(self.issues) + 1
        issue = {"number": number, "title": f"Experience feedback: {target}",
                 "body": marker(target), "state": state,
                 "html_url": f"https://github.com/{REPOSITORY}/issues/{number}"}
        if pull:
            issue["pull_request"] = {"url": "https://api.github.com/pulls/999"}
        self.issues.append(issue)
        self.comments.setdefault(number, [])
        return issue

    def comment(self, issue, body, user=ACTOR):
        self.next_comment += 1
        comment = {"id": self.next_comment, "body": body, "user": dict(user),
                   "html_url": "https://untrusted.example/must-not-be-returned"}
        self.comments.setdefault(issue, []).append(comment)
        return comment

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
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "comments":
            if self.fail_comment_reads:
                raise GitHubError(503, path, "comment readback unavailable")
            return self._page(self.comments[int(parts[4])], query)
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
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "comments":
            self._fail("comment_before")
            number = int(parts[4])
            result = self.comment(number, body["body"])
            if self.duplicate_after_post:
                self.duplicate_after_post = False
                self.comment(number, body["body"])
            if self.failure == "comment_after_and_readback":
                self.failure = None
                self.fail_comment_reads = True
                raise GitHubError(503, path, "response lost")
            self._fail("comment_after")
            return dict(result)
        raise AssertionError(f"unexpected POST {path}")

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        raise AssertionError("independent feedback events must never delete past votes")

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
    assert result["feedback_url"] == f"https://github.com/{REPOSITORY}/issues/1#issuecomment-1001"
    assert re.fullmatch(r"\+1\n\n<!-- vaws-experience-vote: [0-9a-f]{32} -->\n", api.comments[1][0]["body"])
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


def test_every_default_call_records_an_independent_positive_or_negative_use(feedback):
    config, api = feedback
    api.issue(state="closed")
    receipts = []
    for vote in ("+1", "+1", "-1", "-1", "+1"):
        result = experience_feedback(config, TARGET, vote, github=api)
        assert result["status"] == "ok"
        receipts.append(result["feedback_url"])
    assert result["counts"] == {"+1": 3, "-1": 2}
    assert len(api.comments[1]) == 5
    assert len(set(receipts)) == 5
    assert len({item["body"] for item in api.comments[1]}) == 5
    assert all(call[0] == "POST" and call[1].endswith("/comments") for call in api.mutations())
    assert len(api.issues) == 1


def test_paginated_issue_and_comment_lookup_ignores_prs_and_finds_the_retry_event(feedback):
    config, api = feedback
    api.issue(pull=True)
    for number in range(2, 101):
        api.issue(target=f"experience/unrelated-{number}.md", number=number)
    issue = api.issue(number=101)
    for number in range(100):
        api.comment(issue["number"], event_body("+1", f"{number:032x}"), OTHER)
    original = api.comment(issue["number"], event_body("-1", EVENT_ID))
    result = experience_feedback(config, TARGET, "-1", github=api, request_id=EVENT_ID)
    assert result["status"] == "ok"
    assert result["issue_url"].endswith("/issues/101")
    assert result["counts"] == {"+1": 100, "-1": 1}
    assert result["feedback_url"].endswith(f"#issuecomment-{original['id']}")
    assert len(api.issues) == 101
    assert api.comments[1] == []
    assert api.mutations() == []


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
    assert api.comments[misleading["number"]] == []


def test_an_ordinary_issue_with_the_same_title_is_not_a_feedback_target(feedback):
    config, api = feedback
    ordinary = api.issue()
    ordinary["body"] = "This issue discusses the experience, but does not declare a feedback target."
    result = experience_feedback(config, TARGET, "+1", github=api)
    assert result["status"] == "ok"
    assert result["issue_url"].endswith("/issues/2")
    assert len(api.issues) == 2
    assert api.comments[ordinary["number"]] == []


def test_percent_in_a_literal_git_path_is_escaped_once_and_not_used_as_traversal(feedback):
    config, api = feedback
    target = "experience/literal%2Fname.md"
    api.files[f"corpus/{target}"] = {"type": "file", "path": f"corpus/{target}"}
    result = experience_feedback(config, target, "+1", github=api)
    assert result["status"] == "ok"
    requested = next(path for method, path, _ in api.calls if "/contents/" in path)
    assert "literal%252Fname.md" in requested
    assert marker(target) in api.issues[0]["body"]


@pytest.mark.parametrize("vote", ["+1", "-1"])
def test_retry_reuses_the_event_and_cannot_change_its_vote(feedback, vote):
    config, api = feedback
    api.issue()
    first = experience_feedback(config, TARGET, vote, github=api, request_id=EVENT_ID)
    repeated = experience_feedback(config, TARGET, vote, github=api, request_id=EVENT_ID)
    assert repeated["status"] == "ok"
    assert repeated["feedback_url"] == first["feedback_url"]
    other_vote = "-1" if vote == "+1" else "+1"
    conflict = experience_feedback(config, TARGET, other_vote, github=api, request_id=EVENT_ID)
    assert conflict["status"] == "conflict"
    assert conflict["retryable"] is False
    assert len(api.comments[1]) == 1
    assert api.comments[1][0]["body"] == event_body(vote, EVENT_ID)


def test_copying_another_authors_event_id_does_not_hijack_a_retry(feedback):
    config, api = feedback
    api.issue()
    copied = api.comment(1, event_body("-1", EVENT_ID), OTHER)
    first = experience_feedback(config, TARGET, "+1", github=api, request_id=EVENT_ID)
    repeated = experience_feedback(config, TARGET, "+1", github=api, request_id=EVENT_ID)
    assert first["status"] == repeated["status"] == "ok"
    assert repeated["counts"] == {"+1": 1, "-1": 1}
    assert first["feedback_url"] == repeated["feedback_url"]
    assert not first["feedback_url"].endswith(f"#issuecomment-{copied['id']}")
    assert len(api.comments[1]) == 2


@pytest.mark.parametrize("vote", ["+1", "-1"])
def test_same_event_concurrent_duplicate_comments_count_only_once(feedback, vote):
    config, api = feedback
    api.issue()
    api.duplicate_after_post = True
    first = experience_feedback(config, TARGET, vote, github=api, request_id=EVENT_ID)
    assert first["status"] == "ok"
    assert len(api.comments[1]) == 2, "GitHub lacks an atomic uniqueness constraint for comments"
    repeated = experience_feedback(config, TARGET, vote, github=api, request_id=EVENT_ID)
    assert repeated["status"] == "ok"
    assert repeated["counts"] == {"+1": int(vote == "+1"), "-1": int(vote == "-1")}
    assert len(api.comments[1]) == 2


def test_ordinary_discussion_and_quoted_markers_do_not_count_as_use_events(feedback):
    config, api = feedback
    api.issue()
    for body in ("+1", "A discussion of a helpful case.", f"Example:\n{event_body('+1', EVENT_ID)}",
                 f"```\n{event_body('-1', EVENT_ID)}```", event_body("+1", "not-an-event-id")):
        api.comment(1, body, OTHER)
    result = experience_feedback(config, TARGET, "-1", github=api)
    assert result["status"] == "ok"
    assert result["counts"] == {"+1": 0, "-1": 1}


@pytest.mark.parametrize("request_id", ["", "a" * 31, "a" * 33, "g" * 32, True, 123,
                                       "private/session/id", "a" * 32 + "\n"])
def test_retry_id_must_be_an_opaque_token_before_any_network(feedback, request_id):
    config, api = feedback
    result = experience_feedback(config, TARGET, "+1", github=api, request_id=request_id)
    assert result["status"] in {"invalid_request_id", "invalid_request"}
    assert result["retryable"] is False
    assert api.calls == []


@pytest.mark.parametrize("vote", ["+1", "-1"])
def test_comment_write_response_loss_is_recovered_by_readback_without_reposting(feedback, vote):
    config, api = feedback
    api.issue()
    api.failure = "comment_after"
    result = experience_feedback(config, TARGET, vote, github=api)
    assert result["status"] == "ok"
    assert len(api.comments[1]) == 1
    assert len(api.mutations()) == 1
    assert result["counts"] == {"+1": int(vote == "+1"), "-1": int(vote == "-1")}


@pytest.mark.parametrize("stage", ["comment_before", "comment_after_and_readback"])
def test_unconfirmed_comment_error_returns_the_same_id_for_recovery(feedback, stage):
    config, api = feedback
    api.issue()
    api.failure = stage
    failed = experience_feedback(config, TARGET, "+1", github=api)
    assert failed["status"] == "error"
    assert failed["retryable"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", failed["request_id"])
    assert len(api.mutations()) == 1, "an unconfirmed write must not be blindly posted again"
    api.fail_comment_reads = False
    recovered = experience_feedback(config, TARGET, "+1", github=api, request_id=failed["request_id"])
    assert recovered["status"] == "ok"
    assert recovered["counts"] == {"+1": 1, "-1": 0}
    assert len(api.comments[1]) == 1


def test_lost_issue_create_response_is_recovered_without_another_issue(feedback):
    config, api = feedback
    api.failure = "create_issue_after"
    failed = experience_feedback(config, TARGET, "+1", github=api)
    assert failed["status"] == "error"
    assert failed["retryable"] is True
    assert len(api.issues) == 1
    recovered = experience_feedback(config, TARGET, "+1", github=api, request_id=failed["request_id"])
    assert recovered["status"] == "ok"
    assert len(api.issues) == 1
    assert recovered["counts"] == {"+1": 1, "-1": 0}
