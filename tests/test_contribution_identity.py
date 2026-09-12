"""Contribution revisions preserve document identity through actual Git diffs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from contribution.support import FakeContributionGitHub, init_git_repo, ordinary_md

from vaws_knowledge.contribution.documents import MarkdownDocument
from vaws_knowledge.contribution.errors import IdentityError
from vaws_knowledge.contribution.gitops import run_git
from vaws_knowledge.contribution.github import GitHubError
from vaws_knowledge.contribution.pending import iter_pending
from vaws_knowledge.contribution.submit import SubmitConfig, prepare_candidate, submit_pending


class ReviewGitHub(FakeContributionGitHub):
    """A human can close or merge a PR; a create callback simulates new capture."""

    def __init__(self):
        super().__init__()
        self.after_create = None

    def post(self, path, body):
        result = super().post(path, body)
        callback, self.after_create = self.after_create, None
        if callback is not None:
            callback()
        return result

    def patch(self, path, body):
        self.calls.append(("PATCH", path, dict(body)))
        self._fail(path)
        number = int(path.rsplit("/", 1)[-1])
        self.pulls[number].update(body)
        return dict(self.pulls[number])

    def finish(self, number, *, merged):
        self.pulls[number].update(state="closed", merged=merged)


class Contribution:
    def __init__(self, root):
        self.root = root
        self.state = root / "state"
        self.public = root / "public"
        self.candidate = root / "工作 notes" / "note.md"
        self.candidate.parent.mkdir(parents=True)
        self.repo = root / "fork"
        self.base = init_git_repo(self.repo)
        self.github = ReviewGitHub()
        self.config = SubmitConfig("owner/corpus", "author/corpus")

    def git(self, *args):
        return run_git(self.repo, list(args)).stdout.strip()

    def prepare(self, title="Graph observation", body="The tested graph replay completed.", **kwargs):
        self.candidate.write_text(ordinary_md(title, body), encoding="utf-8")
        return prepare_candidate(self.candidate, state_root=self.state, public_root=self.public, **kwargs)

    def submit(self, record):
        return submit_pending(record, state_root=self.state, public_root=self.public,
                              git_repo=self.repo, github=self.github, config=self.config)

    def record(self):
        records = iter_pending(self.state)
        assert len(records) == 1
        return records[0]

    def creates(self):
        return [call for call in self.github.calls if call[0] == "POST" and call[1].endswith("/pulls")]

    def commit_main(self, files):
        self.git("checkout", "main")
        for relpath, content in files.items():
            path = self.repo / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "-m", "Corpus maintainer update")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def contribution(tmp_path):
    return Contribution(tmp_path)


def test_title_and_body_revisions_modify_one_file_and_reuse_open_pr(contribution):
    h = contribution
    first = h.submit(h.prepare())
    initial_path, initial_branch, initial_pr = first.public_relpath, first.branch, first.pr_number
    initial_digest, initial_head = first.content_digest, first.head_sha
    second = h.prepare("A revised heading", "The corrected observation retains its original file.")
    assert second.public_relpath == initial_path
    assert second.content_digest != initial_digest
    assert h.record().content_digest == second.content_digest
    assert h.record().to_dict()["schema"] == "vaws-knowledge-contribution-pending/v2"
    submitted = h.submit(second)
    assert (submitted.public_relpath, submitted.branch, submitted.pr_number) == (
        initial_path, initial_branch, initial_pr)
    assert submitted.head_sha != initial_head
    assert h.git("diff", "--name-status", initial_head, submitted.head_sha) == f"M\tcorpus/{initial_path}"
    assert h.git("show", f"{submitted.head_sha}:corpus/{initial_path}").startswith("# A revised heading")
    repeated = h.submit(h.prepare("A revised heading", "The corrected observation retains its original file."))
    assert repeated.head_sha == submitted.head_sha
    assert len(h.creates()) == 1
    assert len(list(h.public.rglob("*.md"))) == 1


def test_candidate_path_alias_reuses_identity_but_same_basename_does_not(contribution):
    h = contribution
    first = h.prepare()
    h.candidate.write_text(ordinary_md("Changed", "A revision reached through the same filesystem path."))
    alias = h.candidate.parent / ".." / h.candidate.parent.name / h.candidate.name
    revised = prepare_candidate(alias, state_root=h.state, public_root=h.public)
    assert revised.public_relpath == first.public_relpath
    assert len(iter_pending(h.state)) == 1
    other = h.root / "another folder" / h.candidate.name
    other.parent.mkdir()
    other.write_text(ordinary_md("Distinct observation", "Another source with the same basename."))
    independent = prepare_candidate(other, state_root=h.state, public_root=h.public)
    assert independent.public_relpath != first.public_relpath
    assert len(iter_pending(h.state)) == 2


def test_new_experience_path_is_allocated_independently_of_its_content(contribution):
    h = contribution
    first = h.prepare(kind="experience")
    other = prepare_candidate(h.candidate, state_root=h.root / "fresh state", public_root=h.root / "fresh public",
                              kind="experience")
    assert first.content_digest == other.content_digest
    assert first.public_relpath != other.public_relpath
    assert first.content_digest.removeprefix("sha256:")[:12] not in first.public_relpath


def test_identical_experience_from_another_candidate_is_an_independent_case(contribution):
    h = contribution
    first = h.prepare(kind="experience")
    other_candidate = h.root / "another-case.md"
    other_candidate.write_text(h.candidate.read_text(encoding="utf-8"), encoding="utf-8")
    second = prepare_candidate(other_candidate, state_root=h.state, public_root=h.public, kind="experience")
    assert first.content_digest == second.content_digest
    assert first.public_relpath != second.public_relpath
    assert len(iter_pending(h.state)) == 2


def test_default_knowledge_path_uses_the_semantic_unicode_title(contribution):
    h = contribution
    first = h.prepare("图模式回放约束", "The guide states the constraints of graph replay.")
    assert first.public_relpath == "knowledge/图模式回放约束.md"
    revised = h.prepare("修订后的标题", "The same guide now explains an additional constraint.")
    assert revised.public_relpath == first.public_relpath


@pytest.mark.parametrize("kind", ["knowledge", "experience"])
@pytest.mark.parametrize("legacy_path", ["9c7f245689ab-contribution.md", "guides/retained-name.md"])
def test_fresh_agent_updates_the_exact_existing_git_path(contribution, kind, legacy_path):
    h = contribution
    target = f"{kind}/{legacy_path}"
    old = ordinary_md("Earlier heading", "This published statement needs a correction.")
    base = h.commit_main({f"corpus/{target}": old})
    assert iter_pending(h.state) == []
    prepared = h.prepare("Corrected heading", "Another agent revised the existing published document.",
                         kind=kind, public_relpath=target)
    submitted = h.submit(prepared)
    assert submitted.status == "pr_open"
    assert submitted.public_relpath == target
    assert h.git("diff", "--name-status", base, submitted.head_sha) == f"M\tcorpus/{target}"
    assert h.git("ls-tree", "-r", "--name-only", submitted.head_sha, "corpus") == f"corpus/{target}"


def test_fresh_agent_identical_public_content_does_not_open_an_empty_pr(contribution):
    h = contribution
    target = "knowledge/9c7f245689ab-contribution.md"
    text = ordinary_md("Graph observation", "The tested graph replay completed.")
    base = h.commit_main({f"corpus/{target}": text})
    submitted = h.submit(h.prepare(public_relpath=target))
    assert submitted.public_relpath == target
    assert submitted.status != "pr_open"
    assert h.creates() == []
    assert h.git("rev-parse", "HEAD") == base


def test_explicit_missing_experience_target_does_not_become_a_new_document(contribution):
    h = contribution
    before = h.git("rev-parse", "HEAD")
    prepared = h.prepare(kind="experience", public_relpath="experience/misspelled-target.md")
    result = h.submit(prepared)
    assert result.status != "pr_open"
    assert result.last_error
    assert h.creates() == []
    assert h.git("rev-parse", "HEAD") == before
    assert not (h.repo / "corpus/experience/misspelled-target.md").exists()


def test_knowledge_category_path_is_created_then_revised_in_place(contribution):
    h = contribution
    target = "knowledge/runtime/graph-replay.md"
    first = h.submit(h.prepare(public_relpath=target))
    assert first.status == "pr_open"
    assert h.git("diff", "--name-status", h.base, first.head_sha) == f"A\tcorpus/{target}"
    revised = h.submit(h.prepare("Revised guide", "The classified entry now includes a correction.",
                                public_relpath=target))
    assert revised.status == "pr_open"
    assert revised.pr_number == first.pr_number
    assert revised.branch == first.branch
    assert h.git("diff", "--name-status", first.head_sha, revised.head_sha) == f"M\tcorpus/{target}"


def test_identical_knowledge_content_at_different_paths_remains_independent(contribution):
    h = contribution
    first = h.submit(h.prepare(public_relpath="knowledge/runtime/overview.md"))
    other_candidate = h.root / "another-source.md"
    other_candidate.write_text(h.candidate.read_text(encoding="utf-8"), encoding="utf-8")
    second = prepare_candidate(other_candidate, state_root=h.state, public_root=h.public,
                               public_relpath="knowledge/operations/overview.md")
    submitted = h.submit(second)
    assert first.content_digest == submitted.content_digest
    assert first.public_relpath != submitted.public_relpath
    assert first.pr_number != submitted.pr_number
    assert len(iter_pending(h.state)) == 2
    assert len(h.creates()) == 2


def test_explicitly_associated_candidate_keeps_the_target_on_later_revisions(contribution):
    h = contribution
    first = h.prepare(public_relpath="knowledge/runtime/guide.md")
    other_candidate = h.root / "another-source.md"
    other_candidate.write_text(ordinary_md("Another title", "A second source explicitly selects this entry."))
    associated = prepare_candidate(other_candidate, state_root=h.state, public_root=h.public,
                                   public_relpath=first.public_relpath)
    other_candidate.write_text(ordinary_md("Later title", "A later revision retains the explicit association."))
    revised = prepare_candidate(other_candidate, state_root=h.state, public_root=h.public)
    assert revised.public_relpath == associated.public_relpath == first.public_relpath
    assert len(iter_pending(h.state)) == 1


def test_same_default_knowledge_path_from_another_candidate_requires_explicit_target(contribution):
    h = contribution
    first = h.prepare()
    other_candidate = h.root / "another-source.md"
    other_candidate.write_text(h.candidate.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises((ValueError, IdentityError)):
        prepare_candidate(other_candidate, state_root=h.state, public_root=h.public)
    assert len(iter_pending(h.state)) == 1
    assert (h.public / first.public_relpath).read_text(encoding="utf-8") == h.candidate.read_text(encoding="utf-8")


@pytest.mark.parametrize("merged", [False, True])
def test_finished_pr_revision_starts_a_new_branch_from_latest_main(contribution, merged):
    h = contribution
    first = h.submit(h.prepare())
    previous_path, previous_branch, previous_pr = first.public_relpath, first.branch, first.pr_number
    h.github.finish(previous_pr, merged=merged)
    main_files = {"corpus/knowledge/unrelated.md": ordinary_md("Other note", "A concurrent maintainer change.")}
    if merged:
        main_files[f"corpus/{previous_path}"] = ordinary_md("Reviewed heading", "The reviewer clarified the original observation.")
    latest_main = h.commit_main(main_files)
    revised = h.prepare("Later revision", "A new observation after the earlier review completed.")
    submitted = h.submit(revised)
    assert submitted.status == "pr_open"
    assert submitted.public_relpath == previous_path
    assert submitted.branch != previous_branch
    assert submitted.pr_number != previous_pr
    assert h.git("merge-base", latest_main, submitted.head_sha) == latest_main
    assert h.git("show", f"{submitted.head_sha}:corpus/knowledge/unrelated.md") == main_files["corpus/knowledge/unrelated.md"].strip()
    operation = "M" if merged else "A"
    assert h.git("diff", "--name-status", latest_main, submitted.head_sha) == f"{operation}\tcorpus/{previous_path}"
    assert len(h.creates()) == 2


@pytest.mark.parametrize("merged", [False, True])
def test_repeated_original_capture_does_not_reopen_a_finished_pr(contribution, merged):
    h = contribution
    first = h.submit(h.prepare())
    h.github.finish(first.pr_number, merged=merged)
    if merged:
        h.commit_main({f"corpus/{first.public_relpath}": ordinary_md("Reviewed heading", "Human edits remain authoritative.")})
    repeated = h.submit(h.prepare())
    assert repeated.public_relpath == first.public_relpath
    assert repeated.pr_number == first.pr_number
    assert len(h.creates()) == 1


def test_changes_only_to_redacted_content_do_not_create_a_revision(contribution):
    h = contribution
    first = h.submit(h.prepare(body="The observation was recorded at 10.20.30.40 and reproduced once."))
    second = h.prepare(body="The observation was recorded at 10.20.30.41 and reproduced once.")
    assert second.public_relpath == first.public_relpath
    assert second.content_digest == first.content_digest
    repeated = h.submit(second)
    assert repeated.head_sha == first.head_sha
    assert len(h.creates()) == 1
    assert "10.20.30.41" in h.candidate.read_text()
    assert "10.20.30.41" not in (h.public / repeated.public_relpath).read_text()


def test_failed_redaction_cannot_submit_the_previous_public_copy(contribution):
    h = contribution
    old = h.prepare()
    h.candidate.write_text("# Title without body\n", encoding="utf-8")
    blocked = prepare_candidate(h.candidate, state_root=h.state, public_root=h.public)
    assert blocked.status == "blocked_redaction"
    assert blocked.public_relpath == old.public_relpath
    assert h.submit(blocked).status == "blocked_redaction"
    assert h.submit(old).status == "blocked_redaction"
    assert h.creates() == []
    assert h.git("rev-parse", "HEAD") == h.base
    recovered = h.submit(h.prepare("Recovered", "The public document has useful content again."))
    assert recovered.public_relpath == old.public_relpath
    assert recovered.status == "pr_open"
    assert len(iter_pending(h.state)) == 1


def test_prepare_during_pr_creation_keeps_new_revision_pending(contribution):
    h = contribution
    first = h.prepare()
    next_body = "A correction arrived while the previous contribution was in flight."
    captured = []
    h.github.after_create = lambda: captured.append(h.prepare("Latest heading", next_body))
    h.submit(first)
    assert len(captured) == 1
    queued = h.record()
    assert queued.public_relpath == first.public_relpath
    assert queued.content_digest == MarkdownDocument.from_text(ordinary_md("Latest heading", next_body)).digest
    assert queued.status in {"pending", "awaiting_transport"}
    assert next_body in (h.public / queued.public_relpath).read_text()
    finished = h.submit(queued)
    assert finished.status == "pr_open"
    assert next_body in h.git("show", f"{finished.head_sha}:corpus/{finished.public_relpath}")
    assert len(h.creates()) == 1
    assert h.record().content_digest == queued.content_digest


def test_lost_create_response_and_new_capture_retry_the_same_pr(contribution):
    h = contribution
    first = h.prepare()
    latest_body = "A correction was captured before the lost create response was recovered."

    def capture_then_lose_response():
        h.prepare("Latest correction", latest_body)
        raise GitHubError(0, "/repos/owner/corpus/pulls", "response lost")

    h.github.after_create = capture_then_lose_response
    h.submit(first)
    queued = h.record()
    assert queued.content_digest == MarkdownDocument.from_text(ordinary_md("Latest correction", latest_body)).digest
    assert queued.status in {"pending", "awaiting_transport"}
    recovered = h.submit(queued)
    assert recovered.status == "pr_open"
    assert latest_body in h.git("show", f"{recovered.head_sha}:corpus/{recovered.public_relpath}")
    assert len(h.creates()) == 1


def test_public_copy_symlink_cannot_supply_content_for_submission(contribution):
    h = contribution
    record = h.prepare()
    outside = h.root / "private-outside.md"
    outside.write_text(h.candidate.read_text(encoding="utf-8"), encoding="utf-8")
    public_copy = h.public / record.public_relpath
    public_copy.unlink()
    try:
        public_copy.symlink_to(outside)
    except OSError:
        pytest.skip("this platform does not allow unprivileged symlinks")
    submitted = h.submit(record)
    assert submitted.status != "pr_open"
    assert h.creates() == []
    assert h.git("rev-parse", "HEAD") == h.base
    assert outside.read_text(encoding="utf-8") == h.candidate.read_text(encoding="utf-8")


@pytest.mark.parametrize("public_relpath", [
    "../escaped.md", "/knowledge/absolute.md", "knowledge/../escaped.md",
    "knowledge/./note.md", "knowledge//note.md", "experience/wrong-kind.md",
    "knowledge/note.txt", "C:/knowledge/drive.md", "C:\\knowledge\\drive.md",
    "\\\\host\\share\\note.md", "knowledge\\..\\escaped.md",
])
def test_unsafe_or_wrong_kind_public_paths_are_rejected_before_writes(contribution, public_relpath):
    h = contribution
    with pytest.raises((ValueError, IdentityError)):
        h.prepare(public_relpath=public_relpath)
    assert iter_pending(h.state) == []
    assert not list(h.public.rglob("*.md"))
    assert h.creates() == []
