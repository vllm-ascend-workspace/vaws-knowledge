"""The MCP surface: framing, capability probing, and degradation.

The behaviour worth pinning down is the last one. The consuming workspace's
standing rule is that an absent fact means "unknown", never "supported", so a
degraded service must still be able to say "I could not look" in a way a
caller can act on. Returning an empty success would break that rule silently.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge import package_version
from vaws_knowledge.server.layers import load_config  # noqa: E402
from vaws_knowledge.server.mcp_server import (  # noqa: E402
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    KnowledgeService,
    handle_message,
    read_message,
    serve,
    write_message,
)

REFERENCE_UUID = "7c2e9a10-4b3d-4f1a-8c6e-2a9b0d4e5f11"

TODAY = dt.date(2026, 9, 7)


def frame(*messages) -> bytes:
    out = io.BytesIO()
    for message in messages:
        write_message(out, message)
    return out.getvalue()


def read_all(raw: bytes) -> list[dict]:
    stream = io.BytesIO(raw)
    messages = []
    while True:
        message = read_message(stream)
        if message is None:
            return messages
        messages.append(message)


def service(**kwargs) -> KnowledgeService:
    return KnowledgeService(config=support.build_config(**kwargs), today=TODAY)


def call(svc, name, arguments=None, request_id=1):
    response = handle_message(
        svc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    return response["result"]


class Framing(unittest.TestCase):
    def test_newline_jsonrpc_round_trip(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        raw = frame(payload)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"Content-Length:", raw)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual([payload], read_all(raw))

    def test_blank_lines_are_skipped(self):
        stream = io.BytesIO(b'\n\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        self.assertEqual(2, read_message(stream)["id"])

    def test_eof_returns_none(self):
        self.assertIsNone(read_message(io.BytesIO(b"")))

    def test_malformed_json_is_a_parse_error_and_the_loop_survives(self):
        stdin = io.BytesIO(b"garbage without json\n" + frame({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
        stdout = io.BytesIO()
        self.assertEqual(0, serve(stdin, stdout, service()))
        responses = read_all(stdout.getvalue())
        self.assertEqual(PARSE_ERROR, responses[0]["error"]["code"])
        self.assertEqual(3, responses[1]["id"])


class Handshake(unittest.TestCase):
    def test_initialize_advertises_the_package_version_and_layers(self):
        result = handle_message(service(), {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
        self.assertEqual(package_version(), result["serverInfo"]["version"])
        info = result["serviceInfo"]
        self.assertEqual(["shared", "project", "candidate"], info["layers_available"])
        self.assertEqual(["candidate"], info["writable_layers"])
        self.assertEqual("unknown", info["degradation_contract"]["absent_fact_means"])
        self.assertEqual(12, len(info["scope_dimensions"]))
        self.assertFalse(info["degraded"])
        self.assertIn("newline-delimited JSON-RPC", info["framing"])
        self.assertEqual(
            ["rule", "measurement", "reference"],
            result["capabilities"]["experimental"]["vaws-knowledge"]["bodies"],
        )

    def test_initialize_reports_absent_layers_with_reasons(self):
        info = handle_message(
            service(shared="missing"), {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )["result"]["serviceInfo"]
        self.assertTrue(info["degraded"])
        self.assertIn("does not exist", info["layers_absent"]["shared"])

    def test_tools_list_exposes_exactly_the_three_tools(self):
        result = handle_message(service(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
        self.assertEqual(
            ["knowledge_query", "knowledge_capture", "knowledge_explain"],
            [tool["name"] for tool in result["tools"]],
        )
        for tool in result["tools"]:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_notifications_get_no_response(self):
        self.assertIsNone(
            handle_message(service(), {"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_unknown_method_is_a_jsonrpc_error_listing_what_is_supported(self):
        response = handle_message(service(), {"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(METHOD_NOT_FOUND, response["error"]["code"])
        self.assertIn("tools/call", response["error"]["data"]["supported"])


class Tools(unittest.TestCase):
    def test_query_returns_structured_content_with_the_envelope(self):
        result = call(
            service(),
            "knowledge_query",
            {"reader_coordinate": support.READER_SOC_A, "text": "hostname resolution"},
        )
        self.assertFalse(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(package_version(), payload["version"])
        self.assertEqual("unknown", payload["absent_fact_semantics"])
        self.assertEqual("vllm-ascend-workspace/vaws-knowledge", payload["source_repo"])
        self.assertIn("source_ref", payload)
        self.assertIn(support.SHARED_SOC_A, [r["uuid"] for r in payload["results"]])
        # The text block must carry the same payload for text-only clients.
        self.assertEqual(payload, json.loads(result["content"][0]["text"]))

    def test_empty_result_set_is_answered_as_unknown(self):
        result = call(
            service(),
            "knowledge_query",
            {"text": "zzz nonexistent symptom zzz", "reader_coordinate": support.READER_SOC_A},
        )
        payload = result["structuredContent"]
        self.assertEqual([], payload["results"])
        self.assertEqual("unknown", payload["answer"])
        self.assertIn("UNKNOWN", payload["answer_detail"])

    def test_explain_expands_one_entry(self):
        payload = call(service(), "knowledge_explain", {"uuid": support.SHARED_SOC_A})[
            "structuredContent"
        ]
        self.assertTrue(payload["found"])
        self.assertEqual(12, len(payload["entry"]["scope"]))
        self.assertTrue(payload["entry"]["verification"]["evidence"])

    def test_explain_of_an_unknown_uuid_is_unknown(self):
        result = call(
            service(), "knowledge_explain", {"uuid": "00000000-0000-4000-8000-000000000000"}
        )
        payload = result["structuredContent"]
        self.assertFalse(result["isError"])
        self.assertFalse(payload["found"])
        self.assertEqual("unknown", payload["answer"])

    def test_explain_without_a_uuid_is_an_argument_error_not_a_crash(self):
        result = call(service(), "knowledge_explain", {})
        self.assertTrue(result["isError"])
        self.assertEqual("invalid_arguments", result["structuredContent"]["error"])

    def test_capture_into_a_non_candidate_layer_is_refused_through_the_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            for layer in ("shared", "project", "verified"):
                with self.subTest(layer=layer):
                    result = call(
                        svc,
                        "knowledge_capture",
                        {"layer": layer, "entry": {"slug": "example-refused"}},
                    )
                    self.assertTrue(result["isError"])
                    payload = result["structuredContent"]
                    self.assertEqual("capture_refused", payload["error"])
                    self.assertEqual(layer, payload["refused_layer"])
            self.assertEqual([], list(pathlib.Path(tmp).iterdir()))

    def test_capture_rejection_reports_every_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            result = call(svc, "knowledge_capture", {"entry": {"slug": "example-incomplete"}})
            self.assertTrue(result["isError"])
            payload = result["structuredContent"]
            self.assertEqual("capture_rejected", payload["error"])
            self.assertTrue(any("scope" in problem for problem in payload["problems"]))

    def test_unknown_tool_is_an_error_result_not_a_dead_server(self):
        result = call(service(), "knowledge_invent")
        self.assertTrue(result["isError"])
        self.assertEqual("unknown_tool", result["structuredContent"]["error"])


class PackagedCorpusQuery(unittest.TestCase):
    def _service(self) -> KnowledgeService:
        config = load_config({}, env={"VAWS_KNOWLEDGE_CANDIDATE_ROOT": ""})
        return KnowledgeService(config=config, today=TODAY)

    def test_unverified_status_reaches_a_packaged_measurement(self):
        payload = call(
            self._service(),
            "knowledge_query",
            {
                "text": "Ascend910B4",
                "statuses": ["unverified"],
                "bodies": ["measurement"],
                "limit": 5,
            },
        )["structuredContent"]
        self.assertTrue(payload["results"], payload)
        self.assertTrue(all(r["body"] == "measurement" for r in payload["results"]))
        self.assertTrue(all(r["status"] == "unverified" for r in payload["results"]))
        self.assertTrue(any("Ascend910B4" in (r.get("summary") or "") for r in payload["results"]))
        self.assertEqual("vllm-ascend-workspace/vaws-knowledge", payload["source_repo"])

    def test_default_statuses_hide_unverified_shared_entries(self):
        payload = call(
            self._service(),
            "knowledge_query",
            {"text": "Ascend910B4", "bodies": ["measurement"], "limit": 5},
        )["structuredContent"]
        self.assertEqual([], payload["results"])
        self.assertNotIn("unverified", payload["request"]["statuses"])


class Degradation(unittest.TestCase):
    def test_no_layers_at_all_still_answers_unknown(self):
        svc = service(shared=False, project=False, candidate=False)
        payload = call(svc, "knowledge_query", {"text": "anything"})["structuredContent"]
        self.assertTrue(payload["degraded"])
        self.assertEqual([], payload["layers_available"])
        self.assertEqual("unknown", payload["answer"])
        self.assertEqual("unknown", payload["degradation_contract"]["absent_fact_means"])

    def test_unreadable_configuration_degrades_instead_of_dying(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vaws-knowledge.json"
            path.write_text("{not json")
            svc = KnowledgeService(config_path=path, env={}, today=TODAY)
            payload = call(svc, "knowledge_query", {"text": "anything"})["structuredContent"]
            self.assertIn("configuration_error", payload)
            self.assertEqual("unknown", payload["answer"])
            self.assertTrue(payload["degraded"])

    def test_a_broken_document_is_reported_rather_than_read_as_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "broken.yaml").write_text("entries: [ unterminated\n")
            svc = service(shared="missing", project=False, candidate=tmp)
            payload = call(
                svc, "knowledge_query", {"text": "anything", "include_unverified": True}
            )["structuredContent"]
            self.assertEqual([], payload["results"])
            self.assertEqual(1, len(payload["load"]["errors"]))
            self.assertEqual("unknown", payload["answer"])

    def test_serve_loop_handles_a_full_session_over_framed_stdio(self):
        stdin = io.BytesIO(
            frame(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_query",
                        "arguments": {"reader_coordinate": support.READER_SOC_A},
                    },
                },
                {"jsonrpc": "2.0", "id": 4, "method": "shutdown"},
            )
        )
        stdout = io.BytesIO()
        self.assertEqual(0, serve(stdin, stdout, service()))
        responses = read_all(stdout.getvalue())
        self.assertEqual([1, 2, 3, 4], [r["id"] for r in responses])
        self.assertEqual(
            4, len(responses[2]["result"]["structuredContent"]["results"])
        )


class StdioSubprocessHandshake(unittest.TestCase):
    """Actual stdio subprocess, not a direct handle_message call."""

    def test_initialize_tools_list_and_query_over_newline_stdio(self):
        import subprocess

        repo = pathlib.Path(__file__).resolve().parent.parent
        env = {**os.environ, "PYTHONPATH": str(repo), "VAWS_KNOWLEDGE_CANDIDATE_ROOT": ""}
        env.pop("VAWS_KNOWLEDGE_CORPUS", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "vaws_knowledge", "server", "--corpus", str(repo / "corpus")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(repo),
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def send(message: dict) -> None:
            proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def recv() -> dict:
            line = proc.stdout.readline()
            self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "eof")
            self.assertFalse(line.startswith(b"Content-Length:"), line[:80])
            return json.loads(line)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            init = recv()
            self.assertEqual(init["id"], 1)
            self.assertEqual(init["result"]["serverInfo"]["name"], "vaws-knowledge")
            self.assertIn("newline-delimited JSON-RPC", init["result"]["serviceInfo"]["framing"])
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = recv()
            names = [tool["name"] for tool in listed["result"]["tools"]]
            self.assertEqual(names, ["knowledge_query", "knowledge_capture", "knowledge_explain"])
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_query",
                        "arguments": {"text": "newline-delimited JSON-RPC", "include_unverified": True},
                    },
                }
            )
            queried = recv()
            payload = queried["result"]["structuredContent"]
            self.assertFalse(queried["result"]["isError"])
            uuids = [row["uuid"] for row in payload["results"]]
            self.assertIn(REFERENCE_UUID, uuids)
            row = next(item for item in payload["results"] if item["uuid"] == REFERENCE_UUID)
            self.assertEqual(row["body"], "reference")
            self.assertEqual(row["evidence_class"], "sourced_reference")
            self.assertFalse(row["applicability"].get("runtime_scoped", True))
            send({"jsonrpc": "2.0", "id": 4, "method": "shutdown"})
            self.assertEqual(recv()["id"], 4)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        leftover = proc.stdout.read()
        self.assertFalse(leftover)
        self.assertNotIn(b"Traceback", proc.stderr.read())


if __name__ == "__main__":
    unittest.main()
