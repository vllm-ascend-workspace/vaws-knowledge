"""Trusted advisory review wiring: association, fetch, gates, no approval."""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from typing import Any, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bot.advisory_review import (  # noqa: E402
    ADVISORY_MARKER,
    load_advisory_artifact,
    publish_advisory_comment,
    render_advisory_markdown,
    run_trusted_advisory,
)
from bot.publish_comment import GitHubError, Refuse  # noqa: E402
from bot.report import MARKER as REVIEW_MARKER  # noqa: E402
from tests.test_bot_triage_grok import FAKE_ENV, FakeTransport, _load_response  # noqa: E402
from tests.test_publish_comment import (  # noqa: E402
    OWNER_REPO,
    RUN_ID,
    SOURCE_SHA,
    NEW_SHA,
    FakeGitHub,
    bot_comment,
    human_comment,
    make_event,
    make_pull,
)

SYNC_DIR = REPO / "sync"
if str(SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(SYNC_DIR))
import collect as collect_mod  # noqa: E402
import yaml  # noqa: E402

EXAMPLE = REPO / "examples" / "valid-entry.yaml"


class AdvisoryGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.trees: dict[tuple[str, str], dict] = {}
        self.blobs: dict[str, bytes] = {}

    def get(self, path: str) -> Any:
        bare = path.split("?")[0]
        if "/git/trees/" in bare or "/git/blobs/" in bare:
            self.calls.append(("GET", path, None))
        if "/git/trees/" in bare:
            _, rest = bare.split("/repos/", 1)
            name, sha = rest.split("/git/trees/")
            key = (name, sha)
            if key not in self.trees:
                raise GitHubError(404, path, "no tree")
            return dict(self.trees[key])
        if "/git/blobs/" in bare:
            sha = bare.rsplit("/", 1)[-1]
            if sha not in self.blobs:
                raise GitHubError(404, path, "no blob")
            data = self.blobs[sha]
            import base64

            return {
                "sha": sha,
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
                "size": len(data),
            }
        return super().get(path)


def _example_bytes() -> bytes:
    return EXAMPLE.read_bytes()


class AdvisoryWiring(unittest.TestCase):
    def setUp(self):
        self.stash = pathlib.Path(tempfile.mkdtemp(prefix="vaws-advisory-stash-")).resolve()
        self.addCleanup(shutil.rmtree, self.stash, True)
        self.api = AdvisoryGitHub()
        data = _example_bytes()
        sha = collect_mod.git_blob_sha1(data)
        self.api.blobs[sha] = data
        self.api.trees[(OWNER_REPO, SOURCE_SHA)] = {
            "tree": [
                {
                    "path": "examples/valid-entry.yaml",
                    "mode": "100644",
                    "type": "blob",
                    "sha": sha,
                    "size": len(data),
                }
            ],
            "truncated": False,
        }

    def _run(self, event=None, transport=None, environ=None, **kwargs):
        return run_trusted_advisory(
            event or make_event(),
            OWNER_REPO,
            self.api,
            self.stash,
            root=REPO,
            environ=environ if environ is not None else FAKE_ENV,
            transport=transport or FakeTransport(response=_load_response("empty-candidates.json")),
            python=sys.executable,
            **kwargs,
        )

    def test_missing_grok_config_is_unavailable_with_zero_provider_calls(self):
        transport = FakeTransport(response=_load_response("valid-candidate.json"))
        artifact = self._run(environ={}, transport=transport)
        self.assertEqual("unavailable", artifact["status"], artifact)
        self.assertEqual([], transport.calls)
        self.assertFalse(artifact["provider"]["called"])
        self.assertIn("XAI_API_KEY", artifact.get("reason", "") + json.dumps(artifact))
        self.assertNotEqual("success", artifact["status"])
        self.assertIn("not a successful semantic review", artifact.get("permits", ""))

    def test_model_advisory_cannot_approve(self):
        transport = FakeTransport(response=_load_response("approval-claim.json"))
        artifact = self._run(transport=transport)
        if transport.calls:
            self.assertNotEqual("success", artifact["status"])
            self.assertNotIn("candidates", artifact)
        self.assertNotIn("verified", artifact.get("permits", ""))
        md = render_advisory_markdown(
            {
                "advisory": True,
                "status": artifact["status"],
                "reason": artifact.get("reason"),
                "notes": artifact.get("notes"),
                "provider": artifact.get("provider"),
                "binding": {"run": RUN_ID, "head": SOURCE_SHA, "repo": OWNER_REPO, "pr": 5},
            }
        )
        self.assertTrue(md.startswith(ADVISORY_MARKER))
        self.assertNotEqual(md.splitlines()[0], REVIEW_MARKER)
        self.assertIn("cannot approve", md.lower() + json.dumps(artifact.get("notes") or []).lower())

    def test_spoofed_and_stale_association_do_not_fetch_or_comment(self):
        transport = FakeTransport(response=_load_response("empty-candidates.json"))
        stale = make_event()
        self.api.pulls[5] = make_pull(sha=NEW_SHA)
        with self.assertRaises(Refuse):
            self._run(event=stale, transport=transport)
        self.assertEqual([], transport.calls)
        blob_gets = [c for c in self.api.calls if c[0] == "GET" and "/git/blobs/" in c[1]]
        self.assertEqual([], blob_gets)

        spoofed = make_event(head_sha=SOURCE_SHA)
        spoofed["workflow_run"]["name"] = "not-the-gate-workflow"
        with self.assertRaises(Refuse):
            run_trusted_advisory(
                spoofed,
                OWNER_REPO,
                self.api,
                self.stash,
                root=REPO,
                environ=FAKE_ENV,
                transport=transport,
            )
        self.assertEqual([], transport.calls)

    def test_comment_does_not_overwrite_human_or_verdict(self):
        self.api.comments = [
            human_comment(1, ADVISORY_MARKER + "\nplease keep"),
            bot_comment(2, REVIEW_MARKER + "\nverdict"),
        ]
        artifact = {
            "advisory": True,
            "status": "unavailable",
            "reason": "XAI_API_KEY is not configured",
            "notes": ["advisory only"],
            "provider": {"called": False},
            "binding": {"run": RUN_ID, "head": SOURCE_SHA, "repo": OWNER_REPO, "pr": 5},
            "permits": "nothing; advisory unavailable is not a successful semantic review",
        }
        outcome = publish_advisory_comment(make_event(), artifact, OWNER_REPO, self.api)
        writes = self.api.writes()
        self.assertEqual(1, len(writes))
        self.assertEqual("POST", writes[0][0])
        body = writes[0][2]["body"]
        self.assertTrue(body.startswith(ADVISORY_MARKER))
        self.assertNotIn(REVIEW_MARKER, body.splitlines()[0])
        patched = [item for item in writes if item[0] == "PATCH"]
        self.assertEqual([], patched)

    def test_schema_failure_at_head_makes_zero_model_calls(self):
        bad = (REPO / "tests" / "fixtures" / "tools" / "invalid" / "hash-mismatch.yaml").read_bytes()
        sha = collect_mod.git_blob_sha1(bad)
        self.api.blobs[sha] = bad
        self.api.trees[(OWNER_REPO, SOURCE_SHA)] = {
            "tree": [
                {
                    "path": "corpus/unverified/bad.yaml",
                    "mode": "100644",
                    "type": "blob",
                    "sha": sha,
                    "size": len(bad),
                }
            ],
            "truncated": False,
        }
        transport = FakeTransport(response=_load_response("approval-claim.json"))
        artifact = self._run(transport=transport, environ=FAKE_ENV)
        self.assertEqual([], transport.calls)
        self.assertFalse(artifact["provider"]["called"])
        self.assertNotEqual("success", artifact["status"])

    def test_current_head_cannot_enlarge_immutable_set(self):
        data = _example_bytes()
        sha = collect_mod.git_blob_sha1(data)
        self.api.trees[(OWNER_REPO, NEW_SHA)] = {
            "tree": [
                {
                    "path": "examples/valid-entry.yaml",
                    "mode": "100644",
                    "type": "blob",
                    "sha": sha,
                    "size": len(data),
                }
            ],
            "truncated": False,
        }
        self.api.pulls[5] = make_pull(sha=SOURCE_SHA)
        transport = FakeTransport(response=_load_response("empty-candidates.json"))
        artifact = self._run(transport=transport)
        tree_gets = [c[1] for c in self.api.calls if c[0] == "GET" and "/git/trees/" in c[1]]
        self.assertTrue(any(SOURCE_SHA in path for path in tree_gets), tree_gets)
        self.assertFalse(any(NEW_SHA in path for path in tree_gets))

    def test_artifact_loader_rejects_permit_smuggling(self):
        work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        payload = {
            "advisory": True,
            "status": "success",
            "permits": "corpus/verified/",
            "provider": {"called": False},
        }
        (work / "advisory.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(Refuse):
            load_advisory_artifact(work)

    def test_binding_mismatch_writes_nothing(self):
        artifact = {
            "advisory": True,
            "status": "unavailable",
            "reason": "fixture unavailable",
            "provider": {"called": False},
            "binding": {"run": RUN_ID + 1, "head": "f" * 40, "repo": "wrong/other", "pr": 999},
            "permits": "nothing",
        }
        with self.assertRaises(Refuse):
            publish_advisory_comment(make_event(), artifact, OWNER_REPO, self.api)
        self.assertEqual([], self.api.writes())

    def test_missing_binding_writes_nothing(self):
        artifact = {
            "advisory": True,
            "status": "unavailable",
            "provider": {"called": False},
            "permits": "nothing",
        }
        with self.assertRaises(Refuse):
            publish_advisory_comment(make_event(), artifact, OWNER_REPO, self.api)
        self.assertEqual([], self.api.writes())

    def test_matching_binding_updates_owned_advisory_comment(self):
        self.api.comments = [
            bot_comment(7, ADVISORY_MARKER + "\nold advisory\n"),
        ]
        artifact = {
            "advisory": True,
            "status": "unavailable",
            "reason": "XAI_API_KEY is not configured",
            "provider": {"called": False},
            "binding": {"run": RUN_ID, "head": SOURCE_SHA, "repo": OWNER_REPO, "pr": 5},
            "permits": "nothing; advisory unavailable is not a successful semantic review",
        }
        outcome = publish_advisory_comment(make_event(), artifact, OWNER_REPO, self.api)
        self.assertEqual("update", outcome["action"])
        writes = self.api.writes()
        self.assertEqual(1, len(writes))
        self.assertEqual("PATCH", writes[0][0])

    def test_failed_selected_blob_is_unavailable_with_zero_provider_calls(self):
        tree = self.api.trees[(OWNER_REPO, SOURCE_SHA)]
        tree["tree"].append(
            {
                "path": "corpus/unverified/unfetched.yaml",
                "mode": "100644",
                "type": "blob",
                "sha": "a1" * 20,
                "size": 1,
            }
        )
        transport = FakeTransport(response=_load_response("empty-candidates.json"))
        artifact = self._run(transport=transport, environ=FAKE_ENV)
        self.assertEqual([], transport.calls)
        self.assertFalse(artifact["provider"]["called"])
        self.assertNotEqual("success", artifact["status"])
        dumped = json.dumps(artifact)
        self.assertIn("unfetched", dumped)
        self.assertGreater(artifact["coverage"]["omitted_count"], 0)


if __name__ == "__main__":
    unittest.main()
