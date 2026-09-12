"""Every use can add independent feedback; an explicit request ID retries one use."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import re
from threading import Barrier, Lock
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

from vaws_knowledge.contribution.github import GitHubError
from vaws_knowledge.feedback import experience_feedback, experience_path


REPOSITORY = "commons/corpus"
RELATIVE = "experience/eca023e87a91-graph.md"


def vote_body(vote, request_id):
    return f"{vote}\n\n<!-- vaws-experience-vote: {request_id} -->\n"


class FeedbackAPI:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.comments = []
        self.user = {"id": 10, "login": "current-user"}
        self.document = {"path": "corpus/" + RELATIVE, "type": "file"}
        self.next_comment = 1
        self.lose_issue_response = False
        self.fail_comment_before = False
        self.lose_comment_response = False
        self.block_readback_after_loss = False
        self.comments_unavailable = False
        self.post_barrier = None
        self.lock = Lock()

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
        if parsed.path.endswith("/comments"):
            if self.comments_unavailable:
                raise GitHubError(0, path, "readback unavailable")
            items = self.comments
        elif parsed.path.endswith("/issues"):
            items = self.issues
        else:
            raise AssertionError("Unexpected endpoint: " + path)
        start = (int(query.get("page", [1])[0]) - 1) * 100
        with self.lock:
            return deepcopy(items[start:start + 100])

    def post(self, path, body):
        self.calls.append(("POST", path, dict(body)))
        if path.endswith("/issues"):
            result = {"number": len(self.issues) + 1, "html_url": "https://untrusted.invalid/never-return", **body}
            self.issues.append(result)
            if self.lose_issue_response:
                self.lose_issue_response = False
                raise GitHubError(0, path, "response lost")
        elif path.endswith("/comments"):
            if self.fail_comment_before:
                self.fail_comment_before = False
                raise GitHubError(0, path, "not sent")
            if self.post_barrier is not None:
                self.post_barrier.wait(timeout=5)
            with self.lock:
                result = {"id": self.next_comment, "body": body["body"], "user": self.user.copy(),
                          "html_url": "https://untrusted.invalid/comment"}
                self.next_comment += 1
                self.comments.append(result)
            if self.lose_comment_response:
                self.lose_comment_response = False
                self.comments_unavailable = self.block_readback_after_loss
                raise GitHubError(0, path, "response lost")
        else:
            raise AssertionError("Unexpected endpoint: " + path)
        return deepcopy(result)

    def seed_issue(self):
        self.issues.append({"number": 1, "title": "Feedback", "body": f"<!-- vaws-experience-feedback: {RELATIVE} -->"})

    def add_event(self, vote, request_id, user_id=10):
        comment = {"id": self.next_comment, "body": vote_body(vote, request_id), "user": {"id": user_id}}
        self.next_comment += 1
        self.comments.append(comment)
        return comment


@pytest.fixture
def config():
    return SimpleNamespace(publishing={"enabled": True, "fork": "current-user/corpus", "repository": REPOSITORY}, shared_sync={})


@pytest.mark.parametrize("sign", ["+1", "-1"])
def test_same_account_repeated_actual_uses_each_add_a_vote(config, sign):
    github = FeedbackAPI()
    first = experience_feedback(config, RELATIVE, sign, github=github)
    again = experience_feedback(config, RELATIVE, sign, github=github)
    assert first["status"] == again["status"] == "ok"
    assert first["counts"][sign] == 1 and again["counts"][sign] == 2
    assert first["feedback_url"] != again["feedback_url"]
    assert again["feedback_url"] == f"https://github.com/{REPOSITORY}/issues/1#issuecomment-2"
    assert len(github.issues) == 1 and len(github.comments) == 2
    assert "request_id" not in first and "request_id" not in again
    for comment in github.comments:
        assert re.fullmatch(r"[+-]1\n\n<!-- vaws-experience-vote: [0-9a-f]{32} -->\n", comment["body"])
    assert github.comments[0]["body"] != github.comments[1]["body"]
    issue_write = next(body for method, path, body in github.calls if method == "POST" and path.endswith("/issues"))
    assert set(issue_write) == {"title", "body"}
    assert f"https://github.com/{REPOSITORY}/blob/main/corpus/{RELATIVE}" in issue_write["body"]
    assert all(method in {"GET", "POST"} and "reactions" not in path for method, path, _ in github.calls)


def test_positive_and_negative_events_coexist_without_modifying_history(config):
    github = FeedbackAPI()
    experience_feedback(config, RELATIVE, "+1", github=github)
    original = deepcopy(github.comments)
    result = experience_feedback(config, RELATIVE, "-1", github=github)
    assert result["counts"] == {"+1": 1, "-1": 1}
    assert github.comments[:1] == original
    assert [call[0] for call in github.calls if call[0] not in {"GET", "POST"}] == []


def test_same_title_without_exact_marker_is_not_a_feedback_issue(config):
    github = FeedbackAPI()
    github.issues.append({"number": 1, "title": "Experience feedback: " + RELATIVE, "body": "An ordinary discussion."})
    saved = experience_feedback(config, RELATIVE, "+1", github=github)
    assert saved["issue_url"].endswith("/issues/2")
    assert len(github.issues) == 2


def test_replaying_one_request_id_is_idempotent_and_conflicting_vote_is_rejected(config):
    github = FeedbackAPI()
    request_id = uuid.uuid4().hex
    first = experience_feedback(config, RELATIVE, "+1", github=github, request_id=request_id)
    replay = experience_feedback(config, RELATIVE, "+1", github=github, request_id=request_id.upper())
    assert first == replay
    assert len(github.comments) == 1
    conflict = experience_feedback(config, RELATIVE, "-1", github=github, request_id=request_id)
    assert conflict["status"] == "conflict" and not conflict["retryable"]
    assert conflict["request_id"] == request_id
    assert len(github.comments) == 1


def test_different_explicit_ids_are_different_actual_uses(config):
    github = FeedbackAPI()
    for request_id in (uuid.uuid4().hex, uuid.uuid4().hex):
        saved = experience_feedback(config, RELATIVE, "+1", github=github, request_id=request_id)
    assert saved["counts"] == {"+1": 2, "-1": 0}
    assert len(github.comments) == 2


def test_concurrent_distinct_events_from_one_account_both_count(config):
    github = FeedbackAPI()
    github.seed_issue()
    github.post_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(experience_feedback, config, RELATIVE, sign, github=github) for sign in ("+1", "-1")]
        results = [job.result(timeout=10) for job in jobs]
    assert all(result["status"] == "ok" for result in results)
    assert len(github.comments) == 2
    assert {comment["body"].splitlines()[0] for comment in github.comments} == {"+1", "-1"}
    assert {tuple(result["counts"].values()) for result in results}.intersection({(1, 1)})


def test_concurrent_duplicate_request_comments_count_as_one_event(config):
    github = FeedbackAPI()
    github.seed_issue()
    github.post_barrier = Barrier(2)
    request_id = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(experience_feedback, config, RELATIVE, "+1", github=github, request_id=request_id) for _ in range(2)]
        results = [job.result(timeout=10) for job in jobs]
    assert all(result["status"] == "ok" and result["counts"] == {"+1": 1, "-1": 0} for result in results)
    assert len(github.comments) == 2  # GitHub POST is not atomic; counting is deduplicated.


def test_comment_post_failure_before_write_returns_a_retry_id(config):
    github = FeedbackAPI()
    github.fail_comment_before = True
    failed = experience_feedback(config, RELATIVE, "+1", github=github)
    assert failed["status"] == "error" and failed["retryable"]
    assert re.fullmatch(r"[0-9a-f]{32}", failed["request_id"])
    assert github.comments == []
    recovered = experience_feedback(config, RELATIVE, "+1", github=github, request_id=failed["request_id"])
    assert recovered["status"] == "ok" and recovered["counts"] == {"+1": 1, "-1": 0}


def test_lost_comment_post_response_is_recovered_by_readback(config):
    github = FeedbackAPI()
    github.lose_comment_response = True
    recovered = experience_feedback(config, RELATIVE, "+1", github=github)
    assert recovered["status"] == "ok"
    assert recovered["counts"] == {"+1": 1, "-1": 0}
    assert len(github.comments) == 1
    assert len([call for call in github.calls if call[0] == "POST" and call[1].endswith("/comments")]) == 1


def test_lost_post_and_failed_readback_can_retry_the_returned_id(config):
    github = FeedbackAPI()
    github.lose_comment_response = github.block_readback_after_loss = True
    failed = experience_feedback(config, RELATIVE, "-1", github=github)
    assert failed["status"] == "error" and failed["retryable"]
    assert len(github.comments) == 1
    github.comments_unavailable = False
    recovered = experience_feedback(config, RELATIVE, "-1", github=github, request_id=failed["request_id"])
    assert recovered["counts"] == {"+1": 0, "-1": 1}
    assert len(github.comments) == 1


def test_lost_issue_response_retry_finds_the_existing_issue(config):
    github = FeedbackAPI()
    github.lose_issue_response = True
    failed = experience_feedback(config, RELATIVE, "+1", github=github)
    assert failed["status"] == "error" and failed["retryable"]
    recovered = experience_feedback(config, RELATIVE, "+1", github=github, request_id=failed["request_id"])
    assert recovered["status"] == "ok"
    assert len(github.issues) == len(github.comments) == 1


def test_pagination_counts_complete_events_but_ignores_ordinary_comments(config):
    github = FeedbackAPI()
    github.seed_issue()
    for _ in range(105):
        github.add_event("+1", uuid.uuid4().hex)
    duplicate = github.comments[0]["body"]
    for body in ("+1", "-1", "+1 because it helped", duplicate, duplicate + "extra context", vote_body("+2", uuid.uuid4().hex)):
        github.comments.append({"id": github.next_comment, "body": body, "user": github.user.copy()})
        github.next_comment += 1
    saved = experience_feedback(config, RELATIVE, "-1", github=github)
    assert saved["counts"] == {"+1": 105, "-1": 1}
    assert any("/comments?per_page=100&page=2" in call[1] for call in github.calls)


def test_another_account_copying_my_request_id_cannot_claim_my_retry(config):
    github = FeedbackAPI()
    github.seed_issue()
    request_id = uuid.uuid4().hex
    github.add_event("-1", request_id, user_id=20)
    saved = experience_feedback(config, RELATIVE, "+1", github=github, request_id=request_id)
    assert saved["status"] == "ok" and saved["counts"] == {"+1": 1, "-1": 1}
    again = experience_feedback(config, RELATIVE, "+1", github=github, request_id=request_id)
    assert again == saved and len(github.comments) == 2


@pytest.mark.parametrize("request_id", ["", "private-session-id", "/Users/private/host", uuid.uuid4().urn,
                                         str(uuid.uuid4()), "a" * 31, "g" * 32, 123, True])
def test_request_id_is_opaque_hex_and_private_identifiers_are_never_sent(config, request_id):
    github = FeedbackAPI()
    result = experience_feedback(config, RELATIVE, "+1", github=github, request_id=request_id)
    assert result["status"] == "invalid_request_id" and not result["retryable"]
    assert "request_id" not in result
    assert github.calls == []


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
    "viking://resources/shared/bootstrap/experience/exam\nple.md",
])
def test_nonpublic_and_ambiguous_refs_are_rejected_before_remote_access(config, ref):
    github = FeedbackAPI()
    assert experience_feedback(config, ref, "+1", github=github)["status"] == "invalid_ref"
    assert github.calls == []


def test_malformed_transport_payload_is_retryable(config):
    class MalformedAPI(FeedbackAPI):
        def get(self, path):
            raise ValueError("malformed JSON from GitHub")

    result = experience_feedback(config, RELATIVE, "+1", github=MalformedAPI())
    assert result["status"] == "error" and result["retryable"]
    assert "configuration" not in result["reason"]
