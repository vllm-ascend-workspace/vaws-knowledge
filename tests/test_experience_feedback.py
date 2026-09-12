"""GitHub reactions stay separate from corpus edits and local experience text."""

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from vaws_knowledge.contribution.github import GitHubError
from vaws_knowledge.feedback import experience_feedback, experience_path


REPOSITORY = "commons/corpus"
RELATIVE = "experience/eca023e87a91-graph.md"


class FeedbackAPI:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.reactions = []
        self.user = {"id": 10, "login": "current-user"}
        self.document = {"path": "corpus/" + RELATIVE, "type": "file"}
        self.next_reaction = 1
        self.lose_issue_response = False
        self.lose_reaction_response = False
        self.delete_fails = False

    def get(self, path):
        self.calls.append(("GET", path, None))
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path == "/user":
            return self.user.copy()
        if "/contents/" in parsed.path:
            if self.document is None:
                raise GitHubError(404, path)
            return deepcopy(self.document)
        if parsed.path.endswith("/reactions"):
            items = self.reactions
        elif parsed.path.endswith("/issues"):
            items = self.issues
        else:
            raise AssertionError("Unexpected endpoint: " + path)
        start = (int(query.get("page", [1])[0]) - 1) * 100
        return deepcopy(items[start:start + 100])

    def post(self, path, body):
        self.calls.append(("POST", path, dict(body)))
        if path.endswith("/issues"):
            result = {"number": len(self.issues) + 1, "html_url": "https://untrusted.invalid/never-return", **body}
            self.issues.append(result)
            if self.lose_issue_response:
                self.lose_issue_response = False
                raise GitHubError(0, path, "response lost")
        elif path.endswith("/reactions"):
            result = next((r for r in self.reactions if r["content"] == body["content"] and r["user"]["id"] == 10), None)
            if result is None:
                result = {"id": self.next_reaction, "content": body["content"], "user": self.user.copy()}
                self.next_reaction += 1
                self.reactions.append(result)
            if self.lose_reaction_response:
                self.lose_reaction_response = False
                raise GitHubError(0, path, "response lost")
        else:
            raise AssertionError("Unexpected endpoint: " + path)
        return deepcopy(result)

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        if self.delete_fails:
            raise GitHubError(0, path, "network unavailable")
        ident = int(path.rsplit("/", 1)[1])
        self.reactions = [r for r in self.reactions if r["id"] != ident]


@pytest.fixture
def config():
    return SimpleNamespace(publishing={"enabled": True, "fork": "current-user/corpus", "repository": REPOSITORY}, shared_sync={})


def test_feedback_reuses_one_issue_and_one_vote_for_the_github_account(config):
    github = FeedbackAPI()
    first = experience_feedback(config, RELATIVE, "+1", github=github)
    again = experience_feedback(config, RELATIVE, "+1", github=github)
    assert first == again == {"status": "ok", "issue_url": f"https://github.com/{REPOSITORY}/issues/1",
                              "counts": {"+1": 1, "-1": 0}, "vote": "+1"}
    assert len(github.issues) == len(github.reactions) == 1
    writes = [call for call in github.calls if call[0] != "GET"]
    assert [call[1] for call in writes] == [f"/repos/{REPOSITORY}/issues", f"/repos/{REPOSITORY}/issues/1/reactions"]
    assert set(writes[0][2]) == {"title", "body"}
    assert RELATIVE in writes[0][2]["body"]
    assert f"https://github.com/{REPOSITORY}/blob/main/corpus/{RELATIVE}" in writes[0][2]["body"]


def test_same_title_without_exact_marker_is_not_a_feedback_issue(config):
    github = FeedbackAPI()
    github.issues.append({"number": 1, "title": "Experience feedback: " + RELATIVE, "body": "An ordinary discussion."})
    saved = experience_feedback(config, RELATIVE, "+1", github=github)
    assert saved["issue_url"].endswith("/issues/2")
    assert len(github.issues) == 2


def test_switch_adds_new_vote_before_deleting_only_my_old_vote(config):
    github = FeedbackAPI()
    assert experience_feedback(config, RELATIVE, "+1", github=github)["status"] == "ok"
    github.reactions.append({"id": 999, "content": "+1", "user": {"id": 20}})
    github.calls.clear()
    changed = experience_feedback(config, RELATIVE, "-1", github=github)
    assert changed["counts"] == {"+1": 1, "-1": 1}
    writes = [(method, path) for method, path, _ in github.calls if method != "GET"]
    assert [method for method, _ in writes] == ["POST", "DELETE"]
    assert writes[1][1].endswith("/reactions/1")
    assert any(r["id"] == 999 for r in github.reactions)


@pytest.mark.parametrize("lost", ["lose_issue_response", "lose_reaction_response"])
def test_lost_response_retry_does_not_duplicate_issue_or_vote(config, lost):
    github = FeedbackAPI()
    setattr(github, lost, True)
    uncertain = experience_feedback(config, RELATIVE, "+1", github=github)
    assert uncertain["status"] == "error" and uncertain["retryable"]
    recovered = experience_feedback(config, RELATIVE, "+1", github=github)
    assert recovered["status"] == "ok"
    assert len(github.issues) == len(github.reactions) == 1


def test_failed_switch_delete_recovers_without_duplicate_votes(config):
    github = FeedbackAPI()
    experience_feedback(config, RELATIVE, "+1", github=github)
    github.delete_fails = True
    failed = experience_feedback(config, RELATIVE, "-1", github=github)
    assert failed["status"] == "error"
    assert {r["content"] for r in github.reactions} == {"+1", "-1"}
    github.delete_fails = False
    assert experience_feedback(config, RELATIVE, "-1", github=github)["counts"] == {"+1": 0, "-1": 1}


@pytest.mark.parametrize("publishing", [{}, {"enabled": True}, {"enabled": False, "fork": "user/corpus"}])
def test_disabled_never_reads_auth_or_sends_a_remote_request(config, publishing, monkeypatch):
    config.publishing = publishing
    github = FeedbackAPI()
    monkeypatch.setattr("vaws_knowledge.feedback.github_token", lambda: pytest.fail("auth must not be read"))
    assert experience_feedback(config, RELATIVE, "+1", github=github)["status"] == "disabled"
    assert github.calls == []


@pytest.mark.parametrize("vote", [1, -1, True, False, None, "👍", "positive", "0"])
def test_vote_is_exactly_plus_or_minus_one_string(config, vote):
    github = FeedbackAPI()
    assert experience_feedback(config, RELATIVE, vote, github=github)["status"] == "invalid_vote"
    assert github.calls == []


@pytest.mark.parametrize("document", [None, {"type": "dir", "path": "corpus/" + RELATIVE},
                                       {"type": "file", "path": "corpus/experience/wrong.md"}])
def test_document_must_exist_in_the_canonical_experience_path(config, document):
    github = FeedbackAPI()
    github.document = document
    assert experience_feedback(config, RELATIVE, "+1", github=github)["status"] == "not_found"
    assert not [call for call in github.calls if call[0] != "GET"]


def test_configured_canonical_repository_and_branch_are_used(config):
    config.publishing.pop("repository")
    config.shared_sync = {"repository": "fallback/corpus", "base_ref": "release/main"}
    github = FeedbackAPI()
    result = experience_feedback(config, RELATIVE, "+1", github=github)
    assert result["issue_url"] == "https://github.com/fallback/corpus/issues/1"
    contents = next(call[1] for call in github.calls if "/contents/" in call[1])
    assert parse_qs(urlsplit(contents).query)["ref"] == ["release/main"]
    assert "/blob/release%2Fmain/" in github.issues[0]["body"]


@pytest.mark.parametrize("ref", [
    "viking://resources/shared/" + RELATIVE,
    "viking://resources/shared/bootstrap/" + RELATIVE,
    "viking://resources/shared/v0123456789ab/" + RELATIVE,
    "viking://resources/shared/repairs/0123456789abcdef/v0123456789ab/" + RELATIVE,
])
def test_shared_reference_is_resolved_to_a_stable_experience_path(ref):
    assert experience_path(ref) == RELATIVE


@pytest.mark.parametrize("ref", [
    "knowledge/example.md", "candidate/experience/example.md", "project/experience/example.md",
    "viking://resources/candidate/experience/example.md", "viking://resources/project/experience/example.md",
    "viking://resources/shared/knowledge/example.md", "https://github.com/other/repo/blob/main/experience/example.md",
    "viking://resources/shared/bootstrap/experience/%2E%2E/knowledge/example.md",
    "viking://resources/shared/bootstrap/experience/example.md?candidate=private",
    "viking://resources/shared/bootstrap/experience/example.md#private",
])
def test_nonpublic_and_ambiguous_refs_are_rejected_before_remote_access(config, ref):
    github = FeedbackAPI()
    assert experience_feedback(config, ref, "+1", github=github)["status"] == "invalid_ref"
    assert github.calls == []
