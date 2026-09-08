"""Offline controls for central collection. GitHub is an injected fake."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common, propose_mod  # noqa: E402

import yaml  # noqa: E402
from vaws_knowledge.sync import collect as collect_mod

PARENT_ID = 1196723340
PARENT_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PARENT_TREE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FORK_SHA = "cccccccccccccccccccccccccccccccccccccccc"
FORK_TREE = "dddddddddddddddddddddddddddddddddddddddd"
FORK2_SHA = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
FORK2_TREE = "ffffffffffffffffffffffffffffffffffffffff"
WRONG_SHA = "1212121212121212121212121212121212121212"


def _dump(doc) -> bytes:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).encode("utf-8")


def _blob(data: bytes) -> str:
    return collect_mod.git_blob_sha1(data)


def _tree_entry(path: str, data: bytes, *, mode: str = "100644", type_: str = "blob") -> dict:
    sha = _blob(data)
    return {"path": path, "mode": mode, "type": type_, "sha": sha, "size": len(data)}


class FakeCollectGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.by_id: dict[int, dict] = {}
        self.fork_pages: dict[str, list[list[dict]]] = {}
        self.commits: dict[tuple[str, str], dict] = {}
        self.trees: dict[tuple[str, str], dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.errors: dict[str, collect_mod.GitHubError] = {}

    def writes(self) -> list[tuple[str, str]]:
        return [item for item in self.calls if item[0] != "GET" and item[0] != "PAGINATE"]

    def get(self, path: str) -> object:
        self.calls.append(("GET", path))
        bare = path.split("?")[0]
        if path in self.errors:
            raise self.errors[path]
        if bare in self.errors:
            raise self.errors[bare]
        if bare.startswith("/repositories/"):
            repo_id = int(bare.rsplit("/", 1)[-1])
            if repo_id not in self.by_id:
                raise collect_mod.GitHubError(404, path)
            return dict(self.by_id[repo_id])
        if "/git/blobs/" in bare:
            sha = bare.rsplit("/", 1)[-1]
            if sha not in self.blobs:
                raise collect_mod.GitHubError(404, path)
            data = self.blobs[sha]
            return {
                "sha": sha,
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
                "size": len(data),
            }
        if "/git/trees/" in bare:
            _, rest = bare.split("/repos/", 1)
            name, sha = rest.split("/git/trees/")
            key = (name, sha)
            if key not in self.trees:
                raise collect_mod.GitHubError(404, path)
            return dict(self.trees[key])
        if "/commits/" in bare:
            _, rest = bare.split("/repos/", 1)
            name, ref = rest.split("/commits/")
            key = (name, ref)
            if key not in self.commits:
                raise collect_mod.GitHubError(404, path)
            return dict(self.commits[key])
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path: str, *, max_pages: int) -> tuple[list, bool]:
        self.calls.append(("PAGINATE", path))
        bare = path.split("?")[0]
        pages = self.fork_pages.get(bare, [[]])
        items: list = []
        truncated = False
        for index, page in enumerate(pages):
            if index >= max_pages:
                truncated = True
                break
            items.extend(page)
        if len(pages) > max_pages:
            truncated = True
        return items, truncated


def _repo(repo_id: int, full_name: str, *, fork: bool = False, parent_id: int | None = None, **extra) -> dict:
    body = {
        "id": repo_id,
        "full_name": full_name,
        "fork": fork,
        "private": False,
        "archived": False,
        "disabled": False,
        "default_branch": "main",
        "forks_count": extra.pop("forks_count", 0),
    }
    if fork:
        pid = parent_id if parent_id is not None else PARENT_ID
        body["parent"] = {"id": pid, "full_name": "ignored-name/should-not-prove-identity"}
        body["source"] = {"id": pid, "full_name": "also-ignored/name"}
    body.update(extra)
    return body


def _commit(sha: str, tree: str) -> dict:
    return {"sha": sha, "commit": {"tree": {"sha": tree}}}


class CollectControls(synctest.SyncTestCase):
    def setUp(self):
        super().setUp()
        self.stash = pathlib.Path(tempfile.mkdtemp(prefix="vaws-collect-stash-")).resolve()
        self.addCleanup(shutil.rmtree, self.stash, True)
        self.pr_calls: list = []
        self.api = FakeCollectGitHub()
        renamed = "org-after-transfer/vllm-ascend-workspace"
        self.parent_name = renamed
        self.api.by_id[PARENT_ID] = _repo(PARENT_ID, renamed, forks_count=2)
        self.api.commits[(renamed, "main")] = _commit(PARENT_SHA, PARENT_TREE)
        self.api.trees[(renamed, PARENT_TREE)] = {"tree": [], "truncated": False}

    def _open_pr(self, exports, **kwargs):
        self.pr_calls.append({"exports": list(exports), "kwargs": kwargs})
        self.assertFalse(kwargs.get("allow_duplicate_candidates"))
        return propose_mod.PullRequestResult(status="created", branch="sync/test/abc", url="https://example.invalid/pr/1")

    def _run(self, **kwargs):
        kwargs.setdefault("mode", "preview")
        kwargs.setdefault("parent_id", PARENT_ID)
        kwargs.setdefault("stash", self.stash)
        kwargs.setdefault("repo", synctest.REPO)
        kwargs.setdefault("tools_dir", None)
        kwargs.setdefault("api", self.api)
        kwargs.setdefault("open_pr", self._open_pr)
        kwargs.setdefault("bounds", collect_mod.Bounds())
        return collect_mod.run_collection(**kwargs)

    def _add_fork(self, repo_id: int, full_name: str, sha: str, tree: str, entries: list, *, parent_id: int = PARENT_ID):
        self.api.by_id[repo_id] = _repo(repo_id, full_name, fork=True, parent_id=parent_id)
        self.api.commits[(full_name, "main")] = _commit(sha, tree)
        tree_body = {"tree": list(entries), "truncated": False}
        self.api.trees[(full_name, tree)] = tree_body
        pages = self.api.fork_pages.setdefault(f"/repos/{self.parent_name}/forks", [[]])
        pages[0].append({"id": repo_id, "full_name": full_name, "private": False})

    def _register_blob(self, data: bytes) -> str:
        sha = _blob(data)
        self.api.blobs[sha] = data
        return sha

    def _eligible_doc(self, seed: str) -> bytes:
        entry = self.new_entry(seed)
        doc = {
            "schema_version": 2,
            "kind": "known-failure-signatures",
            "layer": "unverified",
            "updated_at": "2026-09-07",
            "entries": [entry],
        }
        return _dump(doc)

    def test_renamed_parent_is_resolved_by_numeric_id(self):
        data = self._eligible_doc("collect-parent")
        sha = self._register_blob(data)
        self.api.trees[(self.parent_name, PARENT_TREE)] = {
            "tree": [_tree_entry(".agents/knowledge/known-failure-signatures.yaml", data)],
            "truncated": False,
        }
        result = self._run()
        gets = [path for method, path in self.api.calls if method == "GET"]
        self.assertTrue(any(path == f"/repositories/{PARENT_ID}" for path in gets))
        self.assertTrue(any(path.startswith(f"/repos/{self.parent_name}/") for path in gets + [c[1] for c in self.api.calls]))
        self.assertFalse(any("maoxx241/" in path for path in gets))
        self.assertEqual(self.parent_name, result["parent"]["full_name"])
        self.assertEqual(PARENT_ID, result["parent"]["id"])
        self.assertEqual([], self.pr_calls)
        self.assertEqual("preview", result["proposal"]["status"])
        self.assertFalse(result["proposal"]["wrote"])

    def test_wrong_fork_ancestry_is_not_read(self):
        data = self._eligible_doc("wrong-anc")
        self._register_blob(data)
        self._add_fork(
            77,
            "stranger/vllm-ascend-workspace",
            WRONG_SHA,
            "abababababababababababababababababababab",
            [_tree_entry(".agents/knowledge/x.yaml", data)],
            parent_id=999999,
        )
        result = self._run()
        reasons = [item.get("reason") for item in result["coverage"]["rejected"]]
        self.assertIn("wrong_ancestry", reasons)
        blob_gets = [path for method, path in self.api.calls if method == "GET" and "/git/blobs/" in path]
        self.assertEqual([], blob_gets)
        self.assertEqual([], self.pr_calls)

    def test_multi_page_complete_and_limited_discovery(self):
        pages = []
        for page in range(3):
            batch = []
            for offset in range(2):
                repo_id = 2000 + page * 2 + offset
                name = f"fork-{repo_id}/vllm-ascend-workspace"
                batch.append({"id": repo_id, "full_name": name, "private": False})
                self.api.by_id[repo_id] = _repo(repo_id, name, fork=True)
                sha = f"{repo_id:040d}"
                tree = f"{repo_id + 50:040d}"
                self.api.commits[(name, "main")] = _commit(sha, tree)
                self.api.trees[(name, tree)] = {"tree": [], "truncated": False}
            pages.append(batch)
        self.api.fork_pages[f"/repos/{self.parent_name}/forks"] = pages
        complete = self._run(bounds=collect_mod.Bounds(max_pages=5, max_forks=100))
        self.assertEqual(1 + 6, complete["coverage"] and len(complete["coverage"]["discovered"]))
        self.assertFalse(complete.get("truncated_listing"))
        limited = self._run(bounds=collect_mod.Bounds(max_pages=2, max_forks=100), stash=self.stash)
        self.assertTrue(limited.get("truncated_listing"))
        omitted_reasons = [item.get("reason") for item in limited["coverage"]["limit_omitted"]]
        self.assertIn("max_pages", omitted_reasons)

    def test_immutable_commit_and_blob_binding(self):
        data = self._eligible_doc("bind")
        blob_sha = self._register_blob(data)
        self._add_fork(
            10,
            "fork-a/vllm-ascend-workspace",
            FORK_SHA,
            FORK_TREE,
            [{"path": ".agents/knowledge/ok.yaml", "mode": "100644", "type": "blob", "sha": blob_sha, "size": len(data)}],
        )
        result = self._run()
        bindings = result.get("observations") or []
        self.assertTrue(bindings, result)
        self.assertEqual(FORK_SHA, bindings[0]["bindings"][0]["commit_sha"])
        self.assertEqual(blob_sha, bindings[0]["bindings"][0]["blob_sha"])
        frozen_commit_gets = [
            path for method, path in self.api.calls
            if method == "GET" and "/git/trees/" in path
        ]
        self.assertTrue(any(FORK_TREE in path for path in frozen_commit_gets), frozen_commit_gets)

        bad = FakeCollectGitHub()
        bad.by_id = dict(self.api.by_id)
        bad.fork_pages = dict(self.api.fork_pages)
        bad.commits = dict(self.api.commits)
        bad.trees = dict(self.api.trees)
        bad.blobs[blob_sha] = b"not-the-blob"
        mismatch_stash = pathlib.Path(tempfile.mkdtemp(prefix="vaws-collect-mismatch-")).resolve()
        self.addCleanup(shutil.rmtree, mismatch_stash, True)
        mismatch = collect_mod.run_collection(
            mode="preview",
            parent_id=PARENT_ID,
            stash=mismatch_stash,
            repo=synctest.REPO,
            tools_dir=None,
            api=bad,
            open_pr=self._open_pr,
        )
        reasons = [item.get("reason") for item in mismatch["coverage"]["rejected"]]
        self.assertTrue(any(reason == "blob_sha_mismatch" for reason in reasons), mismatch["coverage"]["rejected"])

    def test_executable_symlink_traversal_oversize_rejected_without_fetch(self):
        data = self._eligible_doc("ok")
        blob_sha = self._register_blob(data)
        huge = b"x" * (collect_mod.DEFAULT_MAX_FILE_BYTES + 10)
        huge_sha = "ab" * 20
        self.api.blobs[huge_sha] = huge
        entries = [
            {"path": ".agents/knowledge/exec.yaml", "mode": "100755", "type": "blob", "sha": blob_sha, "size": len(data)},
            {"path": ".agents/knowledge/link.yaml", "mode": "120000", "type": "blob", "sha": blob_sha, "size": 8},
            {"path": ".agents/knowledge/../secret.yaml", "mode": "100644", "type": "blob", "sha": blob_sha, "size": len(data)},
            {"path": ".agents/knowledge/big.yaml", "mode": "100644", "type": "blob", "sha": huge_sha, "size": len(huge)},
            {"path": ".agents/knowledge/ok.yaml", "mode": "100644", "type": "blob", "sha": blob_sha, "size": len(data)},
        ]
        self._add_fork(11, "fork-b/vllm-ascend-workspace", FORK_SHA, FORK_TREE, entries)
        before_blobs = [c for c in self.api.calls if c[0] == "GET" and "/git/blobs/" in c[1]]
        result = self._run()
        reasons = {(item.get("path"), item.get("reason")) for item in result["coverage"]["rejected"]}
        self.assertIn((".agents/knowledge/exec.yaml", "executable"), reasons)
        self.assertIn((".agents/knowledge/link.yaml", "symlink"), reasons)
        self.assertTrue(any(item.get("reason") == "path_traversal" for item in result["coverage"]["rejected"]))
        self.assertIn((".agents/knowledge/big.yaml", "oversize"), reasons)
        blob_paths = [path for method, path in self.api.calls if method == "GET" and "/git/blobs/" in path]
        self.assertTrue(any(blob_sha in path for path in blob_paths))
        self.assertFalse(any(huge_sha in path for path in blob_paths))
        self.assertNotIn(self.stash.resolve(), [pathlib.Path(p).resolve() for p in sys.path if p])

    def test_v1_and_incomplete_are_unsupported_not_exported(self):
        v1 = _dump({"schema_version": 1, "applicable_versions": "CANN 8 and later", "entries": [{"uuid": "x"}]})
        incomplete_entry = self.new_entry("incomplete")
        del incomplete_entry["scope"]["cann"]
        incomplete = _dump(
            {
                "schema_version": 2,
                "kind": "known-failure-signatures",
                "layer": "unverified",
                "updated_at": "2026-09-07",
                "entries": [incomplete_entry],
            }
        )
        v1_sha = self._register_blob(v1)
        inc_sha = self._register_blob(incomplete)
        self._add_fork(
            12,
            "fork-c/vllm-ascend-workspace",
            FORK_SHA,
            FORK_TREE,
            [
                {"path": ".agents/knowledge/v1.yaml", "mode": "100644", "type": "blob", "sha": v1_sha, "size": len(v1)},
                {
                    "path": ".agents/knowledge/incomplete.yaml",
                    "mode": "100644",
                    "type": "blob",
                    "sha": inc_sha,
                    "size": len(incomplete),
                },
            ],
        )
        result = self._run()
        classes = [item.get("classification") or item.get("reason") for item in result["coverage"]["unsupported"]]
        self.assertTrue(any("v1" in str(item).lower() or item == "v1" for item in classes) or any(
            item.get("classification") == "v1" for item in result["coverage"]["unsupported"]
        ), result["coverage"]["unsupported"])
        self.assertTrue(
            any(item.get("classification") == "incomplete" for item in result["coverage"]["unsupported"]),
            result["coverage"]["unsupported"],
        )
        self.assertEqual(0, result["eligible_count"])
        self.assertEqual([], result["export_paths"])
        self.assertEqual([], self.pr_calls)

    def test_valid_v2_candidate_preview_does_not_open_pr(self):
        data = self._eligible_doc("eligible-v2")
        sha = self._register_blob(data)
        self._add_fork(
            13,
            "fork-d/vllm-ascend-workspace",
            FORK_SHA,
            FORK_TREE,
            [{"path": ".agents/knowledge/ok.yaml", "mode": "100644", "type": "blob", "sha": sha, "size": len(data)}],
        )
        result = self._run(mode="preview")
        self.assertGreaterEqual(result["eligible_count"], 1, result)
        self.assertEqual("preview", result["proposal"]["status"])
        self.assertFalse(result["proposal"]["wrote"])
        self.assertEqual([], self.pr_calls)
        self.assertEqual([], self.api.writes())

    def test_schema_or_redaction_failure_makes_zero_pr_calls(self):
        entry = self.new_entry("leak")
        entry["rule"]["resolution"] += " email synthetic-private-person@corp.invalid"
        data = _dump(
            {
                "schema_version": 2,
                "kind": "known-failure-signatures",
                "layer": "unverified",
                "updated_at": "2026-09-07",
                "entries": [entry],
            }
        )
        sha = self._register_blob(data)
        self._add_fork(
            14,
            "fork-e/vllm-ascend-workspace",
            FORK_SHA,
            FORK_TREE,
            [{"path": ".agents/knowledge/leak.yaml", "mode": "100644", "type": "blob", "sha": sha, "size": len(data)}],
        )
        result = self._run(mode="propose")
        self.assertEqual([], self.pr_calls)
        self.assertTrue(
            any(item.get("reason") in {"export_or_gate_failed", "mandatory_gates_failed"} for item in result["coverage"]["rejected"]),
            result["coverage"]["rejected"],
        )
        dumped = json.dumps(collect_mod.public_coverage(result))
        self.assertNotIn("synthetic-private-person@corp.invalid", dumped)

    def test_duplicate_across_forks_is_one_candidate(self):
        data = self._eligible_doc("dup-across")
        sha = self._register_blob(data)
        entry = _tree_entry(".agents/knowledge/ok.yaml", data)
        self._add_fork(15, "fork-f/vllm-ascend-workspace", FORK_SHA, FORK_TREE, [entry])
        self._add_fork(16, "fork-g/vllm-ascend-workspace", FORK2_SHA, FORK2_TREE, [entry])
        result = self._run()
        self.assertEqual(1, result["eligible_count"], result.get("observations"))
        obs = result["observations"][0]
        self.assertEqual(2, len(obs["bindings"]))
        public = collect_mod.public_coverage(result)
        self.assertEqual(1, len(public["observations"]))
        self.assertEqual(2, len(public["observations"][0]["bindings"]))
        self.assertIn(obs["bindings"][0]["blob_sha"], json.dumps(public))
        self.assertIn(obs["bindings"][0]["commit_sha"], json.dumps(public))
        self.assertIn(obs["bindings"][0]["path"], json.dumps(public))

    def test_conflict_and_verified_revision_are_not_written(self):
        a = self.new_entry("conflict-a")
        b = dict(a)
        b["rule"] = dict(a["rule"], resolution=a["rule"]["resolution"] + " different claim")
        b["content_hash"] = _common.content_hash(b)
        data_a = _dump({"schema_version": 2, "kind": "known-failure-signatures", "layer": "unverified", "updated_at": "2026-09-07", "entries": [a]})
        data_b = _dump({"schema_version": 2, "kind": "known-failure-signatures", "layer": "unverified", "updated_at": "2026-09-07", "entries": [b]})
        sha_a = self._register_blob(data_a)
        sha_b = self._register_blob(data_b)
        self._add_fork(17, "fork-h/vllm-ascend-workspace", FORK_SHA, FORK_TREE, [_tree_entry(".agents/knowledge/a.yaml", data_a)])
        self._add_fork(18, "fork-i/vllm-ascend-workspace", FORK2_SHA, FORK2_TREE, [_tree_entry(".agents/knowledge/b.yaml", data_b)])
        result = self._run(mode="propose")
        self.assertTrue(result["conflicts"] or any(item.get("reason") == "conflicting_revision" for item in result["coverage"]["rejected"]))
        self.assertEqual([], self.pr_calls)

        verified = self.entry(synctest.UUID_VERIFIED)
        verified["rule"]["resolution"] += " changed verified claim"
        verified["content_hash"] = _common.content_hash(verified)
        data_v = _dump({"schema_version": 2, "kind": "known-failure-signatures", "layer": "unverified", "updated_at": "2026-09-07", "entries": [verified]})
        sha_v = self._register_blob(data_v)
        api2 = FakeCollectGitHub()
        api2.by_id[PARENT_ID] = self.api.by_id[PARENT_ID]
        api2.commits[(self.parent_name, "main")] = self.api.commits[(self.parent_name, "main")]
        api2.trees[(self.parent_name, PARENT_TREE)] = {"tree": [], "truncated": False}
        api2.blobs[sha_v] = data_v
        api2.by_id[19] = _repo(19, "fork-j/vllm-ascend-workspace", fork=True)
        api2.commits[("fork-j/vllm-ascend-workspace", "main")] = _commit(FORK_SHA, FORK_TREE)
        api2.trees[("fork-j/vllm-ascend-workspace", FORK_TREE)] = {
            "tree": [_tree_entry(".agents/knowledge/v.yaml", data_v)],
            "truncated": False,
        }
        api2.fork_pages[f"/repos/{self.parent_name}/forks"] = [[{"id": 19, "full_name": "fork-j/vllm-ascend-workspace"}]]
        before = synctest.dir_digest(self.corpus_dir)
        result_v = collect_mod.run_collection(
            mode="preview",
            parent_id=PARENT_ID,
            stash=self.stash,
            repo=synctest.REPO,
            tools_dir=None,
            api=api2,
            open_pr=self._open_pr,
        )
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))
        self.assertEqual([], self.pr_calls)

    def test_failed_fetch_preserves_prior_state(self):
        data = self._eligible_doc("fetch-fail")
        sha = _blob(data)
        self._add_fork(
            20,
            "fork-k/vllm-ascend-workspace",
            FORK_SHA,
            FORK_TREE,
            [{"path": ".agents/knowledge/ok.yaml", "mode": "100644", "type": "blob", "sha": sha, "size": len(data)}],
        )
        self.api.errors[f"/repos/fork-k/vllm-ascend-workspace/git/blobs/{sha}"] = collect_mod.GitHubError(500, "blob")
        before = synctest.dir_digest(self.corpus_dir)
        result = self._run(mode="propose")
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))
        self.assertEqual([], self.pr_calls)
        self.assertTrue(any(item.get("reason") == "blob_fetch_failed" for item in result["coverage"]["inaccessible"]))

    def test_invalid_bounds_do_not_unbind(self):
        bounds = collect_mod.Bounds.from_mapping({"max_pages": 0, "max_file_bytes": "-1", "max_forks": "nope"})
        self.assertEqual(collect_mod.DEFAULT_MAX_PAGES, bounds.max_pages)
        self.assertEqual(collect_mod.DEFAULT_MAX_FILE_BYTES, bounds.max_file_bytes)
        self.assertEqual(collect_mod.DEFAULT_MAX_FORKS, bounds.max_forks)

    def test_forbidden_flags_are_not_automatic(self):
        text = (synctest.SYNC_DIR / "collect.py").read_text(encoding="utf-8")
        self.assertIn("FORBIDDEN_FLAGS", text)
        self.assertIn("skip=False", text)
        self.assertNotIn('skip=True', text)

    def test_same_hash_contributor_mismatch_is_conflict(self):
        doc = yaml.safe_load(self._eligible_doc("meta-conflict"))
        other = yaml.safe_load(self._eligible_doc("meta-conflict"))
        other["entries"][0]["provenance"]["contributor"] = "different-review-contributor"
        data_a = _dump(doc)
        data_b = _dump(other)
        self._register_blob(data_a)
        self._register_blob(data_b)
        self._add_fork(810, "fork-alpha/scaffold", FORK_SHA, FORK_TREE, [_tree_entry(".agents/knowledge/ok.yaml", data_a)])
        self._add_fork(811, "fork-beta/scaffold", FORK2_SHA, FORK2_TREE, [_tree_entry(".agents/knowledge/ok.yaml", data_b)])
        result = self._run(mode="propose")
        self.assertTrue(result["conflicts"], result)
        self.assertTrue(any(item.get("reason") == "incompatible_metadata" for item in result["conflicts"]))
        self.assertEqual([], self.pr_calls)
        self.assertEqual(0, result["eligible_count"])

    def test_final_gate_crash_discards_assembled_exports(self):
        data = self._eligible_doc("final-gate")
        self._register_blob(data)
        self._add_fork(
            899,
            "fork-final-gate/scaffold",
            FORK_SHA,
            FORK_TREE,
            [_tree_entry(".agents/knowledge/known-failure-signatures.yaml", data)],
        )
        fault = self.stash / "fault-dependency"
        fault.mkdir()
        (fault / "jsonschema.py").write_text('raise RuntimeError("assembled-document validator crash")\n')

        def runner(cmd, **kw):
            assembled = any("/exports/assembled/" in str(x) for x in cmd)
            validate_script = any(str(x).endswith("/validate.py") for x in cmd)
            validate_module = (
                len(cmd) >= 4 and list(cmd[1:4]) == ["-m", "vaws_knowledge", "validate"]
            )
            if assembled and (validate_script or validate_module):
                env = dict(os.environ)
                env["PYTHONPATH"] = str(fault)
                return subprocess.run(cmd, **kw, env=env, text=True, capture_output=True)
            return synctest._common.default_runner(cmd, **kw)

        handoff = self.stash / "handoff"
        result = self._run(mode="preview", runner=runner, handoff_dir=handoff)
        self.assertEqual([], result["export_paths"])
        self.assertTrue(any(item.get("reason") == "mandatory_gates_failed" for item in result["coverage"]["rejected"]))
        self.assertEqual([], self.pr_calls)
        manifest = json.loads((handoff / "current-success.json").read_text(encoding="utf-8"))
        self.assertEqual([], manifest["files"])
        success_dir = handoff / manifest["directory"]
        self.assertEqual([], list(success_dir.glob("*.yaml")))
        assembled = [p for p in (self.stash / "exports" / "assembled").rglob("*.yaml") if p.is_file()]
        self.assertTrue(assembled, "failed assembled YAML stays isolated, not current success")

    def test_current_success_manifest_ignores_prior_generated_files(self):
        handoff = self.stash / "handoff"
        stale = handoff / "success" / "oldtoken" / "stale.yaml"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale-not-current\n", encoding="utf-8")
        result = self._run(mode="preview", handoff_dir=handoff)
        self.assertEqual(0, result["eligible_count"])
        manifest = json.loads((handoff / "current-success.json").read_text(encoding="utf-8"))
        self.assertEqual([], manifest["files"])
        success_dir = handoff / manifest["directory"]
        self.assertEqual([], list(success_dir.glob("*.yaml")))
        self.assertTrue(stale.is_file())
        self.assertNotEqual(stale.parent.resolve(), success_dir.resolve())


class CollectProposeIdempotency(synctest.SyncTestCase):
    def setUp(self):
        super().setUp()
        self.stash = pathlib.Path(tempfile.mkdtemp(prefix="vaws-collect-stash-")).resolve()
        self.addCleanup(shutil.rmtree, self.stash, True)
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        shutil.copytree(self.corpus_dir, self.repo / "corpus")
        (self.repo / "README").write_text("collect-git\n", encoding="utf-8")
        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)
        env = ["-c", "user.name=collect-test", "-c", "user.email=collect-test@example.invalid"]
        subprocess.run(["git", *env, "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", *env, "-C", str(self.repo), "commit", "--quiet", "-m", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "--quiet", "-u", "origin", "main"], check=True, capture_output=True)
        self.gh_calls = []
        self.existing_prs = []

    def runner(self, cmd, **kwargs):
        if cmd[0] == "gh":
            self.gh_calls.append(cmd)
            if cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.existing_prs), "")
            if cmd[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(cmd, 0, "https://example.invalid/pr/9\n", "")
            raise AssertionError(f"unexpected gh call {cmd}")
        if cmd[0] == "git" and cmd[1] == "commit":
            cmd = ["git", "-c", "user.name=collect-test", "-c", "user.email=collect-test@example.invalid", *cmd[1:]]
        self.assertNotIn("--force", cmd)
        self.assertNotIn("--skip-gates", cmd)
        self.assertNotIn("--drop-undeclared", cmd)
        self.assertNotIn("--allow-duplicate-candidates", cmd)
        return _common.default_runner(cmd, **kwargs)

    def test_repeat_run_finds_existing_proposal(self):
        export = self.make_export([self.new_entry("repeat-collect")])
        first = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="propose",
            runner=self.runner,
            open_pr=lambda *a, **k: propose_mod.open_pull_request(*a, **{**k, "runner": self.runner}),
        )
        self.assertEqual("created", first["status"], first)
        self.existing_prs = [{"url": first["url"]}]
        second = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="propose",
            runner=self.runner,
            open_pr=lambda *a, **k: propose_mod.open_pull_request(*a, **{**k, "runner": self.runner}),
        )
        self.assertEqual("exists", second["status"], second)
        self.assertEqual(1, len([c for c in self.gh_calls if c[1:3] == ["pr", "create"]]))

    def test_preview_from_exports_does_not_call_gh(self):
        export = self.make_export([self.new_entry("preview-export")])
        result = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="preview",
            runner=self.runner,
        )
        self.assertEqual("preview", result["status"])
        self.assertFalse(result["wrote"])
        self.assertEqual([], self.gh_calls)

    def test_verified_revision_opens_nothing(self):
        e = self.entry(synctest.UUID_VERIFIED)
        e["rule"]["resolution"] += " Changed verified claim."
        export = self.make_export([e])
        result = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="propose",
            runner=self.runner,
            open_pr=lambda *a, **k: propose_mod.open_pull_request(*a, **{**k, "runner": self.runner}),
        )
        self.assertIn(result["status"], {"nothing-to-propose", "conflicts"}, result)
        self.assertFalse(result["wrote"])
        self.assertTrue(result.get("conflicts"), result)
        self.assertEqual([], self.gh_calls)
        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", "main:corpus/verified/known-failure-signatures.yaml"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn("Changed verified claim.", show.stdout)

    def test_fresh_main_verified_conflict_uses_proposer_plan(self):
        e = self.new_entry("fresh-main")
        export = self.make_export([e])
        verified = dict(e)
        verified["status"] = "verified"
        verified["confidence"] = "high"
        verified["verification"] = self.entry(synctest.UUID_VERIFIED)["verification"]
        verified["rule"] = dict(e["rule"], resolution=e["rule"]["resolution"] + " already on origin main")
        verified["content_hash"] = _common.content_hash(verified)
        remote_repo = self.tmp / "advance-main"
        subprocess.run(
            ["git", "clone", "--quiet", "--branch", "main", str(self.remote), str(remote_repo)],
            check=True,
            capture_output=True,
        )
        dest = remote_repo / "corpus" / "verified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(dest)
        doc["entries"].append(verified)
        dest.write_text(_common.dump_yaml(doc), encoding="utf-8")
        subprocess.run(["git", "-C", str(remote_repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=collect-test",
                "-c",
                "user.email=collect-test@example.invalid",
                "-C",
                str(remote_repo),
                "commit",
                "--quiet",
                "-m",
                "verified candidate on origin main",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(remote_repo), "push", "--quiet", "origin", "main"], check=True, capture_output=True)
        local_plan = collect_mod.compute_plan(
            [_common.load_export(export)],
            collect_mod.load_corpus(self.repo / "corpus"),
            day="2026-09-10",
        )
        self.assertEqual("new", local_plan.items[0].action)
        result = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="propose",
            runner=self.runner,
            open_pr=lambda *a, **k: propose_mod.open_pull_request(*a, **{**k, "runner": self.runner}),
        )
        actions = [item["action"] for item in (result.get("plan") or {}).get("items", [])]
        self.assertNotEqual(["new"], actions, result)
        self.assertTrue(result.get("conflicts") or "conflict" in actions, result)
        self.assertEqual("fresh-main", result.get("plan_source"), result)
        self.assertFalse(result["wrote"])
        self.assertEqual([], [c for c in self.gh_calls if c[1:3] == ["pr", "create"]])

    def test_fresh_main_noop_uses_proposer_plan(self):
        export = self.make_export([self.entry(synctest.UUID_UNVERIFIED)])
        result = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="propose",
            runner=self.runner,
            open_pr=lambda *a, **k: propose_mod.open_pull_request(*a, **{**k, "runner": self.runner}),
        )
        self.assertEqual("nothing-to-propose", result["status"], result)
        actions = [item["action"] for item in (result.get("plan") or {}).get("items", [])]
        self.assertEqual(["no-op"], actions, result)
        self.assertEqual("fresh-main", result.get("plan_source"), result)
        self.assertFalse(result["wrote"])
        self.assertEqual([], [c for c in self.gh_calls if c[1:3] == ["pr", "create"]])

    def test_central_collection_pr_body_does_not_claim_source_side_scan(self):
        export = self.make_export([self.new_entry("central-body")])
        bodies = []

        def runner(cmd, **kwargs):
            if cmd[:3] == ["gh", "pr", "create"]:
                bodies.append(pathlib.Path(cmd[cmd.index("--body-file") + 1]).read_text(encoding="utf-8"))
            return self.runner(cmd, **kwargs)

        result = collect_mod.propose_exports(
            [export],
            repo=self.repo,
            tools_dir=None,
            mode="propose",
            runner=runner,
        )
        self.assertEqual("created", result["status"], result)
        self.assertTrue(bodies)
        self.assertNotIn("redaction-checked in the fork before", bodies[0])
        self.assertIn("trusted", bodies[0].lower())
        self.assertIn("central", bodies[0].lower())


if __name__ == "__main__":
    unittest.main()
