"""Helpers shared by the tests/test_server_*.py suite.

Kept next to the fixtures it points at. The tests import it by adding this
directory to sys.path, so the suite runs from any working directory and does
not depend on the host's environment variables: every config here is built
with an explicit empty ``env``.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any, Mapping

FIXTURES = pathlib.Path(__file__).resolve().parent
REPO = FIXTURES.parent.parent.parent

from vaws_knowledge.server.layers import ServiceConfig, load_config

#: The reader whose build the shared SoC-A entries were established on.
READER_SOC_A: dict[str, str] = {
    "soc": "ExampleSoC-A",
    "cann": "0.0.EXAMPLE",
    "driver": "0.0.example",
    "torch": "2.5.1",
    "torch_npu": "2.5.1.example",
    "vllm": "0.9.2",
    "vllm_ascend": "0.0.0+example",
    "model": "example-model",
    "topology": "tp8",
    "execution_mode": "aclgraph",
}

SHARED_SOC_A = "aa11bb22-1111-4aaa-8bbb-cc33dd44ee55"
SHARED_SOC_B = "bb22cc33-2222-4bbb-9ccc-dd44ee55ff66"
SHARED_STALE = "cc33dd44-3333-4ccc-accc-ee55ff667788"
SHARED_RESOLVED = "dd44ee55-4444-4ddd-bddd-ff6677889900"
SHARED_DEPRECATED = "ee55ff66-5555-4eee-8eee-001122334455"
SHARED_SUPERSEDED = "ff6677aa-6666-4fff-9fff-112233445566"
PROJECT_ONLY = "1122aabb-7777-4aaa-a111-223344556677"
CANDIDATE_ONLY = "2233bbcc-8888-4bbb-b222-334455667788"


def _layer_spec(value: Any, fixture_dir: str) -> dict[str, Any]:
    if value is True:
        return {"roots": [fixture_dir]}
    if value is False:
        return {"enabled": False}
    if value == "missing":
        return {"roots": ["no-such-directory"]}
    return {"roots": [str(value)]}


def build_config(
    *,
    shared: Any = True,
    project: Any = True,
    candidate: Any = True,
    identity: Mapping[str, str] | None = None,
    policy: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ServiceConfig:
    """Build a ServiceConfig over the fixture layers.

    Each layer argument accepts ``True`` (use the fixture directory),
    ``False`` (disabled), ``"missing"`` (configured at a path that does not
    exist) or an explicit path.
    """

    mapping: dict[str, Any] = {
        "layers": {
            "shared": _layer_spec(shared, "shared"),
            "project": _layer_spec(project, "project"),
            "candidate": _layer_spec(candidate, "candidate"),
        }
    }
    if identity:
        mapping["identity"] = dict(identity)
    if policy:
        mapping["policy"] = dict(policy)
    return load_config(mapping, env=dict(env or {}), base_dir=FIXTURES)


def uuids(payload_or_results: Any) -> list[str]:
    """Extract result uuids from a QueryResponse dict or a result list."""

    results = (
        payload_or_results["results"]
        if isinstance(payload_or_results, dict)
        else payload_or_results
    )
    return [item["uuid"] for item in results]


def by_uuid(payload: Mapping[str, Any], uuid: str) -> dict[str, Any]:
    for item in payload["results"]:
        if item["uuid"] == uuid:
            return item
    raise AssertionError(f"{uuid} not in results: {uuids(payload)}")
