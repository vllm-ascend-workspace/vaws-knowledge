"""Native summaries preserve history without becoming current knowledge."""

from vaws_knowledge.server.layers import load_config
from vaws_knowledge.server.query import explain
from vaws_knowledge.summary_hook import capture_summary


def test_summary_is_an_experience_even_when_the_caller_selects_knowledge(tmp_path):
    config = load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "layers": {"shared": False, "project": False,
                   "candidate": {"root": str(tmp_path / "knowledge")}},
        "experience": {"layers": {"candidate": {"root": str(tmp_path / "experiences")}}},
        "publishing": {"enabled": False},
    }, env={})
    text = "An old graph workaround fixed this run; its cause was not confirmed."
    payload = {"hook_event_name": "Stop", "last_assistant_message": text}
    first = capture_summary(payload, config=config, client="codex")
    second = capture_summary(payload, config=config, client="codex")
    assert first["kind"] == "experience"
    assert first["ref"] == second["ref"]
    assert not list((tmp_path / "knowledge").glob("*.md"))
    assert len(list((tmp_path / "experiences").glob("*.md"))) == 1
    assert not explain(config, first["ref"])["found"]
    assert explain(config.for_kind("experience"), first["ref"])["content"] == text
