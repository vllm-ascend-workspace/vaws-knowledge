"""Collision-resistant Markdown identity without user-supplied ids."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vaws_knowledge.markdown import (
    document_slug,
    load_document,
    save_document,
    slugify,
)


class DocumentIdentity(unittest.TestCase):
    def test_distinct_chinese_titles_do_not_share_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_document(root, layer="candidate", title="图模式失败", content="graph compile")
            second = save_document(root, layer="candidate", title="通信超时", content="rpc timeout")
            self.assertNotEqual(first.path, second.path)
            self.assertNotEqual(first.uri, second.uri)
            self.assertNotEqual(slugify("图模式失败"), "图模式失败")
            self.assertEqual("captured-entry", slugify("图模式失败"))
            self.assertTrue(first.path.is_file())
            self.assertTrue(second.path.is_file())
            self.assertEqual("图模式失败", load_document(first.path, layer="candidate", root=root).title)
            self.assertEqual("通信超时", load_document(second.path, layer="candidate", root=root).title)

    def test_same_title_updates_the_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_document(root, layer="candidate", title="图模式失败", content="first body")
            again = save_document(
                root,
                layer="candidate",
                title="图模式失败",
                content="second body",
                path=first.path,
            )
            self.assertEqual(first.path, again.path)
            self.assertEqual("second body", load_document(first.path, layer="candidate", root=root).content)
            self.assertEqual(1, len(list(root.glob("*.md"))))

    def test_ascii_normalization_collision_still_splits(self) -> None:
        self.assertEqual(slugify("Hello World!!!"), slugify("Hello World"))
        self.assertNotEqual(document_slug("Hello World!!!"), document_slug("Hello World"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_document(root, layer="candidate", title="Hello World!!!", content="one")
            second = save_document(root, layer="candidate", title="Hello World", content="two")
            self.assertNotEqual(first.path, second.path)
            self.assertEqual("one", load_document(first.path, layer="candidate", root=root).content)

    def test_nested_same_basename_gets_distinct_uris(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "a" / "note.md"
            right = root / "b" / "note.md"
            left.parent.mkdir()
            right.parent.mkdir()
            left.write_text("# Left note\n\nfrom a\n", encoding="utf-8")
            right.write_text("# Right note\n\nfrom b\n", encoding="utf-8")
            loaded_left = load_document(left, layer="project", root=root)
            loaded_right = load_document(right, layer="project", root=root)
            self.assertNotEqual(loaded_left.uri, loaded_right.uri)
            self.assertEqual("viking://resources/project/knowledge/a/note.md", loaded_left.uri)
            self.assertEqual("viking://resources/project/knowledge/b/note.md", loaded_right.uri)

    def test_existing_stem_file_stays_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "captured-entry.md"
            path.write_text("# 图模式失败\n\nold stored body\n", encoding="utf-8")
            sidecar = path.with_suffix(".meta.json")
            sidecar.write_text(
                json.dumps(
                    {
                        "slug": "captured-entry",
                        "title": "图模式失败",
                        "layer": "candidate",
                        "uri": "viking://resources/candidate/captured-entry.md",
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_document(path, layer="candidate", root=root)
            self.assertEqual("图模式失败", loaded.title)
            self.assertEqual("viking://resources/candidate/knowledge/captured-entry.md", loaded.uri)
            self.assertEqual("viking://resources/candidate/captured-entry.md", json.loads(sidecar.read_text())["uri"])
            self.assertEqual("old stored body", loaded.content)


def test_updating_prose_keeps_recorded_context_without_requiring_metadata(tmp_path):
    from vaws_knowledge.markdown import save_document, meta_path
    import json

    original = save_document(tmp_path, layer="candidate", title="Reference", content="Observed once.",
                             source={"run": "old evidence"}, conditions={"soc": "A3"},
                             evidence="Existing trace excerpt")
    updated = save_document(tmp_path, layer="candidate", title="Reference", content="Still uncertain.")
    assert updated.path == original.path
    assert updated.source == original.source
    assert updated.conditions == original.conditions
    assert updated.evidence == original.evidence
    assert "status" not in json.loads(meta_path(updated.path).read_text(encoding="utf-8"))


def test_explicit_kind_namespace_is_required_for_uri_classification():
    from vaws_knowledge.markdown import kind_from_uri, uri_for

    assert kind_from_uri(uri_for("candidate", "note", kind="experience")) == "experience"
    assert kind_from_uri(uri_for("shared", "note")) == "knowledge"
    assert kind_from_uri("viking://resources/shared/v123/experience/note.md") == "experience"
    assert kind_from_uri("viking://resources/shared/repairs/0123456789abcdef/v0123456789ab/knowledge/note.md") == "knowledge"
    for uri in (
        "viking://resources/candidate/old.md",
        "viking://resources/shared/bootstrap/old.md",
        "viking://resources/shared/repairs/invalid/v0123456789ab/knowledge/note.md",
        "viking://resources/candidate/knowledge/../experience/note.md",
        "viking://resources/candidate/experience",
    ):
        assert kind_from_uri(uri) == ""


def test_sidecar_identity_cannot_redirect_into_other_store(tmp_path):
    from vaws_knowledge.markdown import meta_path

    note = tmp_path / "note.md"
    note.write_text("# Observed failure\n\nA historical observation.\n", encoding="utf-8")
    meta_path(note).write_text(json.dumps({
        "uri": "viking://resources/shared/current/knowledge/claim.md",
        "kind": "knowledge",
    }), encoding="utf-8")
    loaded = load_document(note, layer="candidate", root=tmp_path, kind="experience")
    assert loaded.kind == "experience"
    assert loaded.uri == "viking://resources/candidate/experience/note.md"


def test_aliases_cannot_expose_opposite_store_or_external_file(tmp_path):
    import pytest
    from vaws_knowledge.markdown import iter_markdown_files

    root = tmp_path / "corpus"
    opposite = root / "experience"
    opposite.mkdir(parents=True)
    observed = opposite / "case.md"
    observed.write_text("# Case\n\nOriginal history.\n")
    alias = root / "alias.md"
    alias.symlink_to(observed)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nAnother store.\n")
    (root / "external.md").symlink_to(outside)
    assert iter_markdown_files(root, kind="knowledge") == []
    with pytest.raises(ValueError, match="outside the selected"):
        save_document(root, path=alias, layer="candidate", title="Case", content="Overwrite attempt.")
    assert observed.read_text() == "# Case\n\nOriginal history.\n"
