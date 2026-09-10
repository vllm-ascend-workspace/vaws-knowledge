"""Sync state machine: switch, failure keeps old version, resume, prune, lock."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from distribution.helpers import (
    GIT_SHA,
    GIT_SHA_2,
    GIT_SHA_3,
    DOC_A,
    FakeClient,
    embedding_info,
    make_release_dir,
    metrics_reader_from,
)

from vaws_knowledge.distribution.manifest import version_id_from_sha
from vaws_knowledge.distribution.sync import (
    DistributionState,
    SwitchLock,
    check_and_sync,
    current_shared,
)


def _sync(state_root, release_dir, client, **kwargs):
    return check_and_sync(
        state_root,
        str(release_dir),
        embedding_info=embedding_info(),
        client=client,
        **kwargs,
    )


def test_first_switch_activates_version(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()
    result = _sync(tmp_path / "state", release, client, smoke_query="alpha")
    assert result.status == "switched", result.reason
    version_id = version_id_from_sha(GIT_SHA)
    assert result.root_uri == f"viking://resources/shared/{version_id}"
    assert client.import_kwargs == {"on_conflict": "fail", "vector_mode": "require"}
    assert result.details["import_embedding_calls"] == {}
    assert result.details["smoke_query_hits"] > 0

    current = current_shared(tmp_path / "state")
    assert current is not None
    assert set(current) == {"source_git_sha", "root_uri", "manifest_path"}
    assert current["source_git_sha"] == GIT_SHA
    assert current["root_uri"] == result.root_uri
    assert Path(current["manifest_path"]).is_file()
    payload = json.loads((tmp_path / "state" / "distribution" / "current.json").read_text())
    assert payload["embedding"]["dimension"] == 384


def test_no_change_is_quiet(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()
    state = tmp_path / "state"
    assert _sync(state, release, client).status == "switched"
    again = FakeClient()
    result = _sync(state, release, again)
    assert result.status == "unchanged"
    assert not again.calls  # never touches the instance on no-change


def test_offline_source_keeps_old_version(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()
    state = tmp_path / "state"
    assert _sync(state, release, client).status == "switched"
    result = check_and_sync(
        state, str(tmp_path / "unreachable"), embedding_info=embedding_info(), client=client
    )
    assert result.status == "offline"
    assert "not reachable" in result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_corrupt_pack_keeps_old_version(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "rel1"), client).status == "switched"
    release2 = make_release_dir(tmp_path / "rel2", sha=GIT_SHA_2)
    pack = next(release2.glob("*.ovpack"))
    raw = bytearray(pack.read_bytes())
    raw[-40:] = b"\x00" * 40  # tamper after release.json was written
    pack.write_bytes(raw)
    result = _sync(state, tmp_path / "rel2", client)
    assert result.status == "corrupt", result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA
    assert not any((tmp_path / "state" / "distribution" / "staging").iterdir())


def test_wrong_model_is_incompatible(tmp_path):
    release = make_release_dir(tmp_path / "rel", model="other-model")
    result = _sync(tmp_path / "state", release, FakeClient())
    assert result.status == "incompatible"
    assert "model" in result.reason
    assert current_shared(tmp_path / "state") is None


def test_live_embedding_contract_is_enforced(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    result = check_and_sync(
        tmp_path / "state",
        str(release),
        embedding_info=embedding_info(dimension=768),
        client=FakeClient(),
    )
    assert result.status == "incompatible"
    assert "768" in result.reason


def test_import_failure_keeps_old_version(tmp_path):
    state = tmp_path / "state"
    good = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "rel1"), good).status == "switched"
    failing = FakeClient(fail_import=True)
    result = _sync(state, make_release_dir(tmp_path / "rel2", sha=GIT_SHA_2), failing)
    assert result.status == "error"
    assert "previous version remains active" in result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_no_instance_is_actionable(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    result = check_and_sync(tmp_path / "state", str(release), embedding_info=embedding_info())
    assert result.status == "error"
    assert "no live OpenViking instance" in result.reason
    assert current_shared(tmp_path / "state") is None


def test_interrupted_switch_resumes_without_reimport(tmp_path):
    """Crash after import but before the pointer switch: next run resumes."""
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()

    real_import = client.import_ovpack

    def crash_after_import(*args, **kwargs):
        uri = real_import(*args, **kwargs)
        raise RuntimeError("simulated power loss before pointer switch")

    client.import_ovpack = crash_after_import  # type: ignore[assignment]
    first = _sync(state, release, client)
    assert first.status == "error"
    assert current_shared(state) is None

    client.import_ovpack = real_import  # type: ignore[assignment]
    imports_before = len([c for c in client.calls if c[0] == "import_ovpack"])
    second = _sync(state, release, client)
    assert second.status == "switched", second.reason
    assert second.details.get("import_resumed") is True
    assert len([c for c in client.calls if c[0] == "import_ovpack"]) == imports_before
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_reembedding_during_import_is_rejected(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    counter = {"texts": 0, "generation_rejected": 0}
    client = FakeClient()
    real_import = client.import_ovpack

    def reembedding_import(*args, **kwargs):
        counter["texts"] += 5  # a silent re-embed would show up here
        return real_import(*args, **kwargs)

    client.import_ovpack = reembedding_import  # type: ignore[assignment]
    result = _sync(
        tmp_path / "state", release, client, metrics_reader=metrics_reader_from(counter)
    )
    assert result.status == "error"
    assert "must not recompute" in result.reason
    assert current_shared(tmp_path / "state") is None


def test_query_phase_embedding_calls_are_recorded(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    counter = {"texts": 0, "generation_rejected": 0}
    client = FakeClient()
    real_find = client.find

    def querying_find(*args, **kwargs):
        counter["texts"] += 1  # query vectors are computed locally; that is expected
        return real_find(*args, **kwargs)

    client.find = querying_find  # type: ignore[assignment]
    result = _sync(
        tmp_path / "state",
        release,
        client,
        metrics_reader=metrics_reader_from(counter),
        smoke_query="alpha",
    )
    assert result.status == "switched", result.reason
    assert result.details["import_embedding_calls"].get("texts", 0) == 0
    assert result.details["query_embedding_calls"]["texts"] == 1


def test_prune_keeps_current_and_one_inactive(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    for sha, marker in ((GIT_SHA, "rel1"), (GIT_SHA_2, "rel2"), (GIT_SHA_3, "rel3")):
        release = make_release_dir(tmp_path / marker, sha=sha)
        assert _sync(state, release, client).status == "switched"
    versions = sorted(p.name for p in (state / "distribution" / "versions").iterdir())
    assert versions == sorted([version_id_from_sha(GIT_SHA_2), version_id_from_sha(GIT_SHA_3)])
    removed = [uri for name, uri in client.calls if name == "rm"]
    assert f"viking://resources/shared/{version_id_from_sha(GIT_SHA)}" in removed
    assert current_shared(state)["source_git_sha"] == GIT_SHA_3
    # Old version content is gone from the instance; the new one serves.
    assert f"viking://resources/shared/{version_id_from_sha(GIT_SHA)}" not in client.trees
    assert client.trees[f"viking://resources/shared/{version_id_from_sha(GIT_SHA_3)}"]


def test_private_layers_are_never_touched(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    client.trees["viking://resources/project"] = {"viking://resources/project/p.md": "# P\n\nbody\n"}
    client.trees["viking://resources/candidate"] = {
        "viking://resources/candidate/c.md": "# C\n\nbody\n"
    }
    for sha, marker in ((GIT_SHA, "rel1"), (GIT_SHA_2, "rel2")):
        assert _sync(state, make_release_dir(tmp_path / marker, sha=sha), client).status == "switched"
    assert client.trees["viking://resources/project"]
    assert client.trees["viking://resources/candidate"]
    for name, uri in client.calls:
        if name == "rm":
            assert "/project" not in uri and "/candidate" not in uri


def test_modified_and_deleted_content_follow_the_new_version(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "rel1"), client).status == "switched"
    old_root = current_shared(state)["root_uri"]
    assert DOC_A in client.trees[old_root].values()

    files = {"alpha.md": "# Alpha note\n\nUpdated body.\n"}  # beta.md deleted upstream
    release2 = make_release_dir(tmp_path / "rel2", sha=GIT_SHA_2, files=files)
    assert _sync(state, release2, client).status == "switched"
    new_root = current_shared(state)["root_uri"]
    assert new_root != old_root
    new_docs = client.trees[new_root]
    assert list(new_docs.values()) == ["# Alpha note\n\nUpdated body.\n"]
    # Searches target only the active root; nothing leaks back from the old one.
    hits = client.find("beta", target_uri=new_root)["resources"]
    assert all("beta.md" not in hit["uri"] for hit in hits)


def test_busy_lock_blocks_second_switcher(tmp_path):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")
    dist = DistributionState(state)
    dist.root.mkdir(parents=True, exist_ok=True)
    dist.lock_path.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}))
    result = _sync(state, release, FakeClient())
    assert result.status == "busy"
    assert "in progress" in result.reason
    assert current_shared(state) is None


def test_stale_lock_is_reclaimed(tmp_path):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")
    dist = DistributionState(state)
    dist.root.mkdir(parents=True, exist_ok=True)
    dist.lock_path.write_text(json.dumps({"pid": os.getpid(), "at": time.time() - 3600}))
    result = _sync(state, release, FakeClient(), lock_max_age_s=0.0)
    assert result.status == "switched", result.reason
    assert not dist.lock_path.exists()


def test_concurrent_checks_have_one_switcher(tmp_path):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")

    class SlowClient(FakeClient):
        def import_ovpack(self, *args, **kwargs):
            time.sleep(0.3)  # widen the window so the loser meets the held lock
            return super().import_ovpack(*args, **kwargs)

    results = []

    def run():
        results.append(_sync(state, release, SlowClient()))

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    statuses = [result.status for result in results]
    # Exactly one switcher imports; the other sees the lock or the finished state.
    assert statuses.count("switched") >= 1
    assert all(status in {"switched", "busy", "unchanged"} for status in statuses)
    imports = sum(
        1 for result in results if result.status == "switched" and not result.details.get("import_resumed")
    )
    assert imports == 1
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_switch_lock_reentrant_safety(tmp_path):
    lock = SwitchLock(tmp_path / "sync.lock")
    lock.acquire()
    with pytest.raises(Exception, match="in progress"):
        SwitchLock(tmp_path / "sync.lock").acquire()
    lock.release()
    lock.release()  # double release is harmless
    SwitchLock(tmp_path / "sync.lock").acquire()


def test_corrupt_current_pointer_is_not_fatal(tmp_path):
    state = tmp_path / "state"
    dist = DistributionState(state)
    dist.root.mkdir(parents=True, exist_ok=True)
    dist.current_path.write_text("{corrupt", encoding="utf-8")
    assert current_shared(state) is None
    release = make_release_dir(tmp_path / "rel")
    result = _sync(state, release, FakeClient())
    assert result.status == "switched", result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA
