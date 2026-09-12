"""A historical case must never occupy a current-knowledge result slot."""

from vaws_knowledge.local.backend import backend_for_config
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.server.query import explain, query


def test_two_stores_share_engine_but_not_top_k_or_explain(tmp_path):
    config = load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "layers": {"shared": {"enabled": False}, "project": {"enabled": False},
                   "candidate": {"roots": [str(tmp_path / "knowledge")]}}
    }, env={})
    experience = config.for_kind("experience")
    # Create the experience backend first: views must still reuse one engine.
    old = capture(title="Graph case", content="graph replay padding old implementation", config=experience)
    new = capture(title="Graph case", content="graph current conclusion", config=config)
    assert backend_for_config(config) is backend_for_config(experience)
    for i in range(25):
        capture(title=f"Historical case {i}", content="graph replay padding old implementation", config=experience)
    found = query(config, text="graph replay padding", limit=1)
    assert [hit["ref"] for hit in found.results] == [new["ref"]]
    assert all(hit["kind"] == "knowledge" for hit in found.results)
    historical = query(experience, text="graph", limit=50)
    assert len(historical.results) == 26
    assert historical.to_dict()["kind"] == "experience"
    assert any("Historical commands" in note for note in historical.notes)
    assert not explain(config, old["ref"])["found"]
    assert not explain(experience, new["ref"])["found"]
    assert old["path"] != new["path"]


def test_active_pack_query_and_explain_cannot_cross_kind(tmp_path, monkeypatch):
    config = load_config({"backend": "memory", "state_root": str(tmp_path),
        "layers": {"shared": {"roots": [str(tmp_path / "retired-source")]}}}, env={})
    root = "viking://resources/shared/v0123456789ab"
    files = [{"path": "knowledge/current.md"}, {"path": "experience/history.md"}]
    active = {"root_uri": root, "source_git_sha": "a" * 40}
    monkeypatch.setattr("vaws_knowledge.local.shared.current_shared", lambda state: active)
    monkeypatch.setattr("vaws_knowledge.server.query.current_shared", lambda state: active)
    backend = backend_for_config(config)
    for entry in files:
        backend.upsert(root + "/" + entry["path"], "# Case\n\nsharedcanary", layer="shared")
    # Even an unrelated stale record in this version cannot consume a result
    # slot: the backend must search only the selected kind directory.
    backend.upsert(root + "/untyped.md", "# Case\n\nsharedcanary", layer="shared")
    for kind, filename in (("knowledge", "current.md"), ("experience", "history.md")):
        selected = config.for_kind(kind)
        expected = f"{root}/{kind}/{filename}"
        found = query(selected, text="sharedcanary", layers=["shared"], limit=1)
        assert [hit["ref"] for hit in found.results] == [expected]
        assert explain(selected, expected)["found"]
        other = files[1 if kind == "knowledge" else 0]["path"]
        assert not explain(selected, root + "/" + other)["found"]
        assert not explain(selected, expected.replace("v0123456789ab", "vffffffffffff"))["found"]
        assert not explain(selected, root + "/untyped.md")["found"]
