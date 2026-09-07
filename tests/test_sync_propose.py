"""sync/propose.py: per-entry, idempotent, unverified-only, never deleting.

The git/gh path is exercised against a local bare repository with `gh`
stubbed, so the whole flow runs offline. What that leaves unproven is only
`gh` itself accepting the exact arguments; see the PR description.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common, plan_mod, propose_mod  # noqa: E402


def uuids_in(path: pathlib.Path) -> list[str]:
    return [e["uuid"] for e in _common.load_yaml(path)["entries"]]


class ProposeMechanics(synctest.SyncTestCase):
    def test_unchanged_reexport_produces_no_proposal(self):
        export = self.make_export([self.entry(synctest.UUID_UNVERIFIED), self.entry(synctest.UUID_VERIFIED)])
        before = synctest.dir_digest(self.corpus_dir)
        plan, proposal, written = self.propose_apply([export])
        self.assertEqual({"no-op": 2}, {k: v for k, v in plan.counts().items() if v})
        self.assertTrue(proposal.is_empty)
        self.assertEqual([], written)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))

    def test_changed_entry_produces_exactly_one_revision_of_the_same_uuid(self):
        before = self.entry(synctest.UUID_UNVERIFIED)
        e = self.entry(synctest.UUID_UNVERIFIED)
        e["rule"]["resolution"] += " Also pin the capture list in the launch profile."
        plan, proposal, written = self.propose_apply([self.make_export([e])], day="2026-09-10")
        self.assertEqual(1, plan.counts()["revision"])
        self.assertEqual([synctest.UUID_UNVERIFIED], proposal.applied)
        self.assertEqual(1, len(written))
        self.assertEqual("corpus/unverified/known-failure-signatures.yaml", proposal.display_path(written[0]))
        entries = uuids_in(written[0])
        self.assertEqual(1, entries.count(synctest.UUID_UNVERIFIED), "one identity, one entry")
        self.assertEqual(sorted(entries), entries, "documents are kept sorted by uuid")
        after = self.entry(synctest.UUID_UNVERIFIED)
        self.assertEqual(_common.content_hash(e), after["content_hash"])
        self.assertEqual("2026-09-10", after["lifecycle"]["updated_at"])
        self.assertEqual(before["verification"]["last_verified_at"], after["verification"]["last_verified_at"])
        self.assertEqual(before["lifecycle"]["first_seen"], after["lifecycle"]["first_seen"])
        # and re-proposing the same export is now a no-op
        plan2, proposal2, _ = self.propose_apply([self.make_export([e])])
        self.assertEqual(1, plan2.counts()["no-op"])
        self.assertTrue(proposal2.is_empty)

    def test_two_forks_proposing_the_same_uuid_yield_one_entry(self):
        shared = self.new_entry("shared")
        fork_a = self.make_export([shared], origin="fork-a/vllm-ascend-workspace", name="a.yaml")
        variant = dict(shared)
        variant["rule"] = dict(shared["rule"], avoidance="Fork B adds an avoidance note.")
        fork_b = self.make_export([variant], origin="fork-b/vllm-ascend-workspace", name="b.yaml")

        # Both plan against the same base; both see `new`.
        base = self.corpus()
        plan_a = plan_mod.compute_plan([_common.load_export(fork_a)], base, day="2026-09-10")
        plan_b = plan_mod.compute_plan([_common.load_export(fork_b)], base, day="2026-09-10")
        self.assertEqual("new", plan_a.items[0].action)
        self.assertEqual("new", plan_b.items[0].action)

        # A lands first. B is re-planned against the merged base — which is
        # what open_pull_request does by planning in a fresh worktree — and
        # degrades to a revision instead of a second entry.
        propose_mod.apply_proposal(propose_mod.build_proposal(plan_a, base), base)
        merged = self.corpus()
        plan_b2 = plan_mod.compute_plan([_common.load_export(fork_b)], merged, day="2026-09-10")
        self.assertEqual("revision", plan_b2.items[0].action)
        propose_mod.apply_proposal(propose_mod.build_proposal(plan_b2, merged), merged)

        path = self.corpus_dir / "unverified" / "known-failure-signatures.yaml"
        self.assertEqual(1, uuids_in(path).count(shared["uuid"]))
        final = self.entry(shared["uuid"])
        self.assertEqual("Fork B adds an avoidance note.", final["rule"]["avoidance"])
        self.assertEqual("fork-b/vllm-ascend-workspace", final["provenance"]["origin_repo"])

    def test_stale_plan_cannot_insert_a_second_copy(self):
        # Even if B's stale `new` plan were applied without re-planning, the
        # write primitive upserts by uuid, so the identity still lands once.
        shared = self.new_entry("shared2")
        base = self.corpus()
        export = self.make_export([shared])
        stale_plan = plan_mod.compute_plan([_common.load_export(export)], base)
        propose_mod.apply_proposal(propose_mod.build_proposal(stale_plan, base), base)
        propose_mod.apply_proposal(propose_mod.build_proposal(stale_plan, base), base)
        path = self.corpus_dir / "unverified" / "known-failure-signatures.yaml"
        self.assertEqual(1, uuids_in(path).count(shared["uuid"]))

    def test_near_duplicate_is_reported_and_not_written(self):
        dup = self.entry(synctest.UUID_VERIFIED)
        dup["uuid"] = synctest.fresh_uuid("dup")
        before = synctest.dir_digest(self.corpus_dir)
        plan, proposal, written = self.propose_apply([self.make_export([dup])])
        self.assertEqual(1, plan.counts()["duplicate-candidate"])
        self.assertTrue(proposal.is_empty)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))
        body = propose_mod.render_pr_body(proposal)
        self.assertIn(dup["uuid"], body)
        self.assertIn(synctest.UUID_VERIFIED, body, "both sides are reported")
        self.assertIn("needs a human", body)

    def test_duplicate_candidates_land_only_when_explicitly_allowed(self):
        dup = self.entry(synctest.UUID_VERIFIED)
        dup["uuid"] = synctest.fresh_uuid("dup-allowed")
        plan, proposal, written = self.propose_apply([self.make_export([dup])], allow_duplicate_candidates=True)
        self.assertEqual([dup["uuid"]], proposal.applied)
        self.assertIn(dup["uuid"], uuids_in(written[0]))
        self.assertIn(synctest.UUID_VERIFIED, self.corpus().index, "the original is untouched")

    def test_new_entries_never_touch_verified(self):
        verified_before = (self.corpus_dir / "verified" / "known-failure-signatures.yaml").read_bytes()
        e = self.new_entry("iota")
        e["status"], e["confidence"] = "verified", "high"
        plan, proposal, written = self.propose_apply([self.make_export([e])])
        self.assertEqual(1, len(written))
        for path in written:
            self.assertIn("unverified", path.parts)
            self.assertNotEqual("verified", path.parent.name)
        self.assertEqual(verified_before, (self.corpus_dir / "verified" / "known-failure-signatures.yaml").read_bytes())
        self.assertEqual("unverified", self.entry(e["uuid"])["status"])

    def test_revision_of_a_verified_entry_is_never_written(self):
        e = self.entry(synctest.UUID_VERIFIED)
        e["rule"]["resolution"] += " Changed claim."
        before = synctest.dir_digest(self.corpus_dir)
        plan, proposal, written = self.propose_apply([self.make_export([e])])
        self.assertEqual(1, plan.counts()["conflict"])
        self.assertTrue(proposal.is_empty)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))

    def test_hand_edited_plan_targeting_verified_is_refused(self):
        e = self.new_entry("kappa")
        corpus = self.corpus()
        plan = plan_mod.compute_plan([_common.load_export(self.make_export([e]))], corpus)
        plan.items[0].target_path = "corpus/verified/known-failure-signatures.yaml"
        with self.assertRaises(_common.SyncError):
            propose_mod.build_proposal(plan, corpus)

    def test_new_kind_creates_an_unverified_document(self):
        e = self.new_entry("lambda")
        plan, proposal, written = self.propose_apply([self.make_export([e], kind="version-compatibility")])
        self.assertEqual([self.corpus_dir / "unverified" / "version-compatibility.yaml"], written)
        doc = _common.load_yaml(written[0])
        self.assertEqual({"schema_version": 2, "kind": "version-compatibility", "layer": "unverified"},
                         {k: doc[k] for k in ("schema_version", "kind", "layer")})

    def test_proposal_id_and_branch_are_deterministic(self):
        e = self.new_entry("mu")
        corpus = self.corpus()
        export = self.make_export([e])
        p1 = propose_mod.build_proposal(plan_mod.compute_plan([_common.load_export(export)], corpus), corpus)
        p2 = propose_mod.build_proposal(plan_mod.compute_plan([_common.load_export(export)], corpus), corpus)
        self.assertEqual(p1.branch_name(), p2.branch_name())
        self.assertTrue(p1.branch_name().startswith("sync/fork-a-vllm-ascend-workspace/"))


class ProposeCli(synctest.SyncTestCase):
    def _run(self, *extra, tools=synctest.TOOLS_PASS, export=synctest.EXPORT_FIXTURE):
        cmd = [sys.executable, str(synctest.SYNC_DIR / "propose.py"), "--export", str(export),
               "--corpus", str(self.corpus_dir), "--tools-dir", str(tools), "--today", "2026-09-09", *extra]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_preview_writes_nothing(self):
        before = synctest.dir_digest(self.corpus_dir)
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("would write 1 file(s)", proc.stdout)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))

    def test_apply_then_apply_again_is_idempotent(self):
        proc = self._run("--apply")
        self.assertEqual(0, proc.returncode, proc.stderr)
        after_first = synctest.dir_digest(self.corpus_dir)
        self.assertIn(synctest.UUID_EXPORT_NEW, uuids_in(self.corpus_dir / "unverified" / "known-failure-signatures.yaml"))
        proc = self._run("--apply")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("nothing to propose", proc.stdout)
        self.assertEqual(after_first, synctest.dir_digest(self.corpus_dir))

    def test_missing_gates_fail_closed_before_writing(self):
        before = synctest.dir_digest(self.corpus_dir)
        proc = self._run("--apply", tools=synctest.TOOLS_MISSING)
        self.assertEqual(_common.EXIT_GATE, proc.returncode)
        self.assertIn("gate unavailable", proc.stdout + proc.stderr)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))

    def test_failing_redaction_gate_blocks_the_proposal(self):
        e = self.new_entry("nu", avoidance="synthetic text with the marker QUARANTINE-ME inside")
        export = self.make_export([e])
        before = synctest.dir_digest(self.corpus_dir)
        proc = self._run("--apply", tools=synctest.TOOLS_MARKER, export=export)
        self.assertEqual(_common.EXIT_GATE, proc.returncode)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))

    def test_strict_refuses_to_write_when_a_human_is_needed(self):
        dup = self.entry(synctest.UUID_VERIFIED)
        dup["uuid"] = synctest.fresh_uuid("strict")
        export = self.make_export([dup, self.new_entry("xi")])
        before = synctest.dir_digest(self.corpus_dir)
        proc = self._run("--apply", "--strict", export=export)
        self.assertEqual(_common.EXIT_ERROR, proc.returncode)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))


@unittest.skipUnless(shutil.which("git"), "git is required for the PR flow test")
class OpenPullRequestOffline(synctest.SyncTestCase):
    """Real git against a local bare remote; `gh` replaced by a recording stub."""

    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(self.remote)], check=True)
        self.repo = self.tmp / "clone"
        subprocess.run(["git", "clone", "--quiet", str(self.remote), str(self.repo)], check=True,
                       capture_output=True)
        env = ["-c", "user.name=sync-test", "-c", "user.email=sync-test@example.invalid"]
        shutil.copytree(synctest.CORPUS_BASE, self.repo / "corpus")
        shutil.copytree(synctest.TOOLS_PASS, self.repo / "tools")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--quiet", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", *env, "-C", str(self.repo), "commit", "--quiet", "-m", "base corpus"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "--quiet", "-u", "origin", "main"], check=True,
                       capture_output=True)
        self.gh_calls = []
        self.existing_prs = []

    def runner(self, cmd, **kwargs):
        if cmd[0] == "gh":
            self.gh_calls.append(cmd)
            if cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.existing_prs), "")
            if cmd[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(cmd, 0, "https://example.invalid/pr/1\n", "")
            raise AssertionError(f"unexpected gh call {cmd}")
        if cmd[0] == "git" and cmd[1] == "commit":
            cmd = ["git", "-c", "user.name=sync-test", "-c", "user.email=sync-test@example.invalid", *cmd[1:]]
        self.assertNotIn("--force", cmd)
        self.assertNotIn("-f", cmd)
        self.assertFalse(any(a.startswith("--force") for a in cmd), cmd)
        return _common.default_runner(cmd, **kwargs)

    def _open(self, export):
        return propose_mod.open_pull_request(
            [export], repo=self.repo, tools_dir=None, remote="origin", base="main", day="2026-09-10",
            allow_duplicate_candidates=False, runner=self.runner, log=lambda *_: None,
        )

    def test_creates_branch_commit_and_pr_without_force(self):
        e = self.new_entry("pr-one")
        tmp_root = pathlib.Path(synctest.tempfile.gettempdir()).resolve()
        worktrees_before = set(tmp_root.glob("vaws-sync-*"))
        result = self._open(self.make_export([e]))
        self.assertEqual("created", result.status, result.detail)
        self.assertEqual("https://example.invalid/pr/1", result.url)
        create = [c for c in self.gh_calls if c[1:3] == ["pr", "create"]]
        self.assertEqual(1, len(create))
        self.assertIn("--base", create[0])
        self.assertEqual("main", create[0][create[0].index("--base") + 1])
        self.assertEqual(result.branch, create[0][create[0].index("--head") + 1])
        # the branch exists on the remote and carries exactly the new entry
        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{result.branch}:corpus/unverified/known-failure-signatures.yaml"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn(e["uuid"], show.stdout)
        verified = subprocess.run(
            ["git", "-C", str(self.remote), "diff", "--stat", "main", result.branch, "--", "corpus/verified"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual("", verified.stdout.strip(), "nothing under corpus/verified/ changed")
        # the caller's working tree and main are untouched
        self.assertEqual("main", subprocess.run(["git", "-C", str(self.repo), "branch", "--show-current"],
                                                capture_output=True, text=True).stdout.strip())
        self.assertEqual(worktrees_before, set(tmp_root.glob("vaws-sync-*")), "worktree cleaned up")

    def test_reopening_the_same_proposal_reports_the_existing_pr(self):
        e = self.new_entry("pr-two")
        export = self.make_export([e])
        first = self._open(export)
        self.assertEqual("created", first.status, first.detail)
        self.existing_prs = [{"url": first.url}]
        second = self._open(export)
        self.assertEqual("exists", second.status)
        self.assertEqual(first.url, second.url)
        self.assertEqual(1, len([c for c in self.gh_calls if c[1:3] == ["pr", "create"]]))

    def test_noop_export_opens_nothing(self):
        result = self._open(self.make_export([self.entry(synctest.UUID_UNVERIFIED)]))
        self.assertEqual("nothing-to-propose", result.status)
        self.assertEqual([], self.gh_calls)
        branches = subprocess.run(["git", "-C", str(self.remote), "branch", "--list", "sync/*"],
                                  capture_output=True, text=True).stdout
        self.assertEqual("", branches.strip())

    def test_second_fork_after_merge_becomes_a_revision_not_a_second_entry(self):
        shared = self.new_entry("pr-shared")
        first = self._open(self.make_export([shared], origin="fork-a/x", name="a.yaml"))
        self.assertEqual("created", first.status, first.detail)
        # simulate the maintainer merging A's PR
        subprocess.run(["git", "-C", str(self.repo), "pull", "--quiet", "--ff-only", "origin", "main"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(self.repo),
                        "merge", "--quiet", "--no-edit", f"origin/{first.branch}"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "--quiet", "origin", "main"], check=True,
                       capture_output=True)
        variant = dict(shared, rule=dict(shared["rule"], avoidance="Fork B note."))
        second = self._open(self.make_export([variant], origin="fork-b/x", name="b.yaml"))
        self.assertEqual("created", second.status, second.detail)
        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{second.branch}:corpus/unverified/known-failure-signatures.yaml"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(1, show.count(shared["uuid"]))
        self.assertIn("Fork B note.", show)


if __name__ == "__main__":
    unittest.main()
