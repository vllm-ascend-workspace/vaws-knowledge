"""stdio JSON-RPC MCP server exposing the knowledge service.

Three tools:

    knowledge_query     search the mounted layers, with a reader coordinate
    knowledge_capture   write one entry to the candidate layer (only)
    knowledge_explain   expand one entry by uuid into its full record

Framing is newline-delimited JSON-RPC on stdio, matching the MCP stdio
transport. Messages MUST NOT contain embedded newlines. The official MCP SDK
is not a dependency: this package must run on a laptop with nothing installed
but PyYAML. If the SDK is importable we say so in ``initialize`` (so a caller
can tell what it is talking to) but we still do our own framing. Nothing but
JSON-RPC messages is written to stdout.

Degradation contract
--------------------
The consuming workspace has a standing rule: **an absent fact means
"unknown", never "supported"**. That has to survive partial availability, so:

* every response carries `version` (the installed package version),
  `layers_available`, `layers_absent` (with reasons) and `degraded`;
* every response carries `absent_fact_semantics: "unknown"`;
* an empty result set is returned as an explicit "unknown" reading, not as an
  empty success;
* a layer that fails to load is reported in `load.errors` rather than being
  silently skipped, so a caller can tell "nothing matched" from "we could not
  look".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from vaws_knowledge import package_version

from . import layers as layers_mod
from .capture import CaptureRefused, CaptureRejected, capture
from .layers import (
    ENV_CORPUS,
    LAYERS,
    ConfigError,
    ServiceConfig,
    load_config,
    shared_source,
)
from .query import READER_DIMENSIONS, SCOPE_DIMENSIONS, explain, query

SERVER_NAME = "vaws-knowledge"
MCP_PROTOCOL_VERSION = "2025-11-25"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

DEGRADATION_CONTRACT = {
    "absent_fact_means": "unknown",
    "detail": (
        "A missing entry is never evidence that something works. When a layer is "
        "absent or fails to load, responses say so; treat any gap as unexamined."
    ),
}


class FramingError(Exception):
    """A message could not be read off the wire."""


# --------------------------------------------------------------------------
# framing — newline-delimited JSON-RPC (MCP stdio)
# --------------------------------------------------------------------------


def write_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in body:
        raise FramingError("MCP stdio message must not contain an embedded newline")
    stream.write(body + b"\n")
    stream.flush()


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one newline-delimited JSON-RPC message. Returns None at EOF."""

    while True:
        line = stream.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FramingError(f"invalid JSON line: {exc}") from exc
        if not isinstance(payload, dict):
            raise FramingError("JSON-RPC message must be an object")
        return payload


# --------------------------------------------------------------------------
# tool schemas
# --------------------------------------------------------------------------

_COORDINATE_SCHEMA = {
    "type": "object",
    "description": (
        "The reader's own build. Every dimension you omit comes back as "
        "'unchecked' on each result -- omission narrows nothing."
    ),
    "properties": {dim: {"type": "string"} for dim in SCOPE_DIMENSIONS},
    "additionalProperties": False,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "knowledge_query",
        "description": (
            "Search the mounted knowledge layers. Default result set: shared + "
            "project at status verified, plus stale (labelled with a warning) and "
            "resolved (with its fix reference). Sourced references in those layers "
            "are included even when unverified, labelled by source and trust, not "
            "as local observations. Operational unverified rules and measurements "
            "remain opt-in. Runtime entries whose scope does not cover the supplied "
            "reader coordinate are dropped, not silently returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Free-text symptom or question."},
                "fingerprint": {
                    "type": "string",
                    "description": "An observed signature, matched against rule.fingerprints.",
                },
                "reader_coordinate": _COORDINATE_SCHEMA,
                "layers": {
                    "type": "array",
                    "items": {"enum": list(LAYERS)},
                    "description": "Override which layers are consulted.",
                },
                "statuses": {
                    "type": "array",
                    "items": {
                        "enum": ["verified", "unverified", "stale", "deprecated", "resolved"]
                    },
                    "description": "Explicit status filter; replaces the default set.",
                },
                "include_unverified": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Opt in to operational unverified rules and measurements, "
                        "and to the candidate layer that holds them. Sourced "
                        "references in shared/project are already in the default "
                        "result set."
                    ),
                },
                "include_non_matching": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Also return entries whose scope excludes the reader, "
                        "labelled applies=false with the mismatching dimensions."
                    ),
                },
                "kind": {"type": "string"},
                "bodies": {
                    "type": "array",
                    "items": {"enum": ["rule", "measurement", "reference"]},
                    "description": "Payload variants to return. Default: all supported bodies.",
                },
                "limit": {"type": "integer", "default": 20, "minimum": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "knowledge_capture",
        "description": (
            "Write one entry into the candidate layer. Refuses any other layer: "
            "shared and project are review-gated. Stamps provenance and computes "
            "content_hash per docs/federation.md."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["entry"],
            "properties": {
                "entry": {
                    "type": "object",
                    "description": (
                        "A schema v2 entry. Runtime bodies (rule/measurement) still "
                        "require all twelve scope dimensions. A sourced reference "
                        "body must not invent those coordinates. uuid, content_hash, "
                        "provenance and lifecycle dates are stamped when absent."
                    ),
                },
                "kind": {"type": "string", "default": "known-failure-signatures"},
                "layer": {
                    "enum": list(LAYERS),
                    "default": "candidate",
                    "description": "Only 'candidate' is accepted; anything else is refused.",
                },
                "dry_run": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "knowledge_explain",
        "description": (
            "Expand one entry by uuid into its full record: complete scope "
            "coordinate, verification evidence, concrete verified_against "
            "environment, lifecycle and conflicts."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["uuid"],
            "properties": {
                "uuid": {"type": "string"},
                "reader_coordinate": _COORDINATE_SCHEMA,
                "layers": {"type": "array", "items": {"enum": list(LAYERS)}},
            },
            "additionalProperties": False,
        },
    },
]


def _sdk_available() -> bool:
    try:  # pragma: no cover - depends on the host environment
        import mcp  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


class KnowledgeService:
    """Tool implementations plus the degradation envelope."""

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        config_path: str | Path | None = None,
        config_mapping: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        today: _dt.date | None = None,
    ):
        self.today = today
        self.config_error: str | None = None
        if config is not None:
            self.config = config
        else:
            try:
                self.config = load_config(config_mapping, path=config_path, env=env)
            except ConfigError as exc:
                # Unreadable config is not a reason to die: come up with no
                # layers and say plainly that everything is unknown.
                self.config_error = str(exc)
                self.config = ServiceConfig(mounts={}, warnings=[f"configuration error: {exc}"])

    # -- envelope ---------------------------------------------------------

    def envelope(self) -> dict[str, Any]:
        consulted = self.config.consulted()
        env: dict[str, Any] = {
            "version": package_version(),
            "layers_available": consulted["layers_available"],
            "layers_absent": consulted["layers_absent"],
            "degraded": consulted["degraded"],
            "absent_fact_semantics": "unknown",
            "degradation_contract": DEGRADATION_CONTRACT,
        }
        env.update(shared_source())
        if self.config_error:
            env["configuration_error"] = self.config_error
        return env

    def server_info(self) -> dict[str, Any]:
        info = self.config.describe()
        info.update(
            {
                "server": {"name": SERVER_NAME, "version": package_version()},
                "tools": [tool["name"] for tool in TOOLS],
                "degradation_contract": DEGRADATION_CONTRACT,
                "official_mcp_sdk_importable": _sdk_available(),
                "framing": "newline-delimited JSON-RPC (MCP stdio; SDK not required)",
                "reader_coordinate_dimensions": list(READER_DIMENSIONS),
                "scope_dimensions": list(SCOPE_DIMENSIONS),
                "writable_layers": ["candidate"],
            }
        )
        return info

    # -- tools ------------------------------------------------------------

    def knowledge_query(self, args: Mapping[str, Any]) -> dict[str, Any]:
        response = query(
            self.config,
            text=args.get("text"),
            fingerprint=args.get("fingerprint"),
            reader_coordinate=args.get("reader_coordinate"),
            layers=args.get("layers"),
            statuses=args.get("statuses"),
            include_unverified=bool(args.get("include_unverified", False)),
            include_non_matching=bool(args.get("include_non_matching", False)),
            kind=args.get("kind"),
            bodies=args.get("bodies"),
            limit=int(args.get("limit", 20) or 20),
            today=self.today,
        )
        payload = response.to_dict()
        payload.update(self.envelope())
        if not payload["results"]:
            payload["answer"] = "unknown"
            payload["answer_detail"] = payload["no_result_meaning"]
        return payload

    def knowledge_explain(self, args: Mapping[str, Any]) -> dict[str, Any]:
        uuid = str(args.get("uuid") or "").strip()
        if not uuid:
            raise ValueError("uuid is required")
        payload = explain(
            self.config,
            uuid,
            reader_coordinate=args.get("reader_coordinate"),
            layers=args.get("layers"),
            today=self.today,
        )
        payload.update(self.envelope())
        if not payload.get("found"):
            payload["answer"] = "unknown"
        return payload

    def knowledge_capture(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry = args.get("entry")
        if not isinstance(entry, Mapping):
            raise ValueError("entry must be an object")
        payload = capture(
            entry,
            kind=str(args.get("kind") or "known-failure-signatures"),
            layer=str(args.get("layer") or "candidate"),
            config=self.config,
            today=self.today,
            dry_run=bool(args.get("dry_run", False)),
        )
        payload.update(self.envelope())
        return payload

    def call_tool(self, name: str, args: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Returns ``(payload, is_error)``. Refusals are results, not crashes."""

        handlers = {
            "knowledge_query": self.knowledge_query,
            "knowledge_capture": self.knowledge_capture,
            "knowledge_explain": self.knowledge_explain,
        }
        handler = handlers.get(name)
        if handler is None:
            return (
                {
                    "ok": False,
                    "error": "unknown_tool",
                    "detail": f"no such tool: {name}",
                    "available_tools": list(handlers),
                    **self.envelope(),
                },
                True,
            )
        try:
            return handler(args), False
        except CaptureRefused as exc:
            return (
                {
                    "ok": False,
                    "error": "capture_refused",
                    "refused_layer": exc.layer,
                    "detail": str(exc),
                    **self.envelope(),
                },
                True,
            )
        except CaptureRejected as exc:
            return (
                {
                    "ok": False,
                    "error": "capture_rejected",
                    "problems": exc.problems,
                    "detail": str(exc),
                    **self.envelope(),
                },
                True,
            )
        except ValueError as exc:
            return (
                {"ok": False, "error": "invalid_arguments", "detail": str(exc), **self.envelope()},
                True,
            )
        except Exception as exc:  # noqa: BLE001 - a tool fault is not a dead server
            return (
                {
                    "ok": False,
                    "error": "internal_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "answer": "unknown",
                    **self.envelope(),
                },
                True,
            )


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def handle_message(service: KnowledgeService, message: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, Mapping) or "method" not in message:
        return _error(message.get("id") if isinstance(message, Mapping) else None,
                      INVALID_REQUEST, "not a JSON-RPC request")

    method = str(message.get("method"))
    request_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params") or {}
    if not isinstance(params, Mapping):
        return None if is_notification else _error(request_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return None if is_notification else _result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "experimental": {
                        "vaws-knowledge": {
                            "version": package_version(),
                            "bodies": ["rule", "measurement", "reference"],
                        },
                    },
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": package_version(),
                },
                "serviceInfo": service.server_info(),
            },
        )

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return None if is_notification else _result(request_id, {"version": package_version()})

    if method == "shutdown":
        return None if is_notification else _result(request_id, {})

    if method == "tools/list":
        return None if is_notification else _result(
            request_id, {"tools": TOOLS, "version": package_version()}
        )

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, Mapping):
            return None if is_notification else _error(
                request_id, INVALID_PARAMS, "arguments must be an object"
            )
        payload, is_error = service.call_tool(name, args)
        if is_notification:
            return None
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "structuredContent": payload,
                "isError": is_error,
            },
        )

    if is_notification:
        return None
    return _error(
        request_id,
        METHOD_NOT_FOUND,
        f"unsupported method: {method}",
        {"supported": ["initialize", "tools/list", "tools/call", "ping", "shutdown"]},
    )


def serve(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    service: KnowledgeService | None = None,
) -> int:
    reader = stdin or sys.stdin.buffer
    writer = stdout or sys.stdout.buffer
    service = service or KnowledgeService()

    while True:
        try:
            message = read_message(reader)
        except FramingError as exc:
            write_message(writer, _error(None, PARSE_ERROR, f"framing error: {exc}"))
            continue
        if message is None:
            return 0
        try:
            response = handle_message(service, message)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            response = _error(
                message.get("id") if isinstance(message, Mapping) else None,
                INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
            )
        if response is not None:
            write_message(writer, response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vaws-knowledge server",
        description="stdio MCP server over a local vaws-knowledge corpus checkout",
    )
    parser.add_argument(
        "--corpus",
        help="corpus root (directory containing verified/) or a checkout that holds corpus/verified/",
    )
    parser.add_argument("--config", help="path to a JSON/YAML service config")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print resolved mounts as JSON and exit (no server loop)",
    )
    args = parser.parse_args(argv)

    if layers_mod.yaml is None:
        print(layers_mod.YAML_MISSING_HINT, file=sys.stderr)

    env = dict(os.environ)
    if args.corpus:
        env[ENV_CORPUS] = args.corpus

    try:
        service = KnowledgeService(config_path=args.config, env=env)
    except ConfigError as exc:  # pragma: no cover - KnowledgeService absorbs these
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.describe:
        print(json.dumps(service.server_info(), indent=2, ensure_ascii=False))
        return 0

    return serve(service=service)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
