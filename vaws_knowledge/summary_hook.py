"""Save the final text provided by a native hook. Never read full transcripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.layers import ServiceConfig, load_config


def capture_summary(payload: dict[str, Any], *, config: ServiceConfig, client: str) -> dict[str, Any]:
    # Use each client's documented response event. Grok can import other
    # clients' hooks, so its native marker must not be handled a second time.
    if client == "grok":
        if payload.get("hookEventName") != "stop":
            return {"status": "no_summary"}
        text = payload.get("lastAssistantMessage")
    elif "hookEventName" in payload:
        return {"status": "no_summary"}
    elif client == "cursor" and payload.get("hook_event_name") == "afterAgentResponse":
        text = payload.get("text")
    elif client in {"codex", "claude", "kimi"} and payload.get("hook_event_name") == "Stop":
        text = payload.get("last_assistant_message")
    else:
        # Client adapters may supply their native final response. Storage stays
        # here; locating a particular client's response stays with its adapter.
        return {"status": "no_summary"}
    if not isinstance(text, str) or not text.strip():
        return {"status": "no_summary"}
    text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.S).strip()
    if len(text) < 24:
        return {"status": "no_summary"}
    # The digest makes repeated hook delivery idempotent without inventing a
    # task identity. Client-provided source fields stay in the private sidecar.
    heading = next((line.strip().lstrip("# ") for line in text.splitlines() if line.strip()), "Session observation")
    title = f"{heading[:100]} [{hashlib.sha256(text.encode()).hexdigest()[:8]}]"
    source = {"client": client}
    for key in ("session_id", "turn_id", "conversation_id", "generation_id", "sessionId", "promptId"):
        if isinstance(payload.get(key), str):
            source[key] = payload[key]
    saved = capture(title=title, content=text, source=source, config=config, index=False)
    return {"status": "saved", "ref": saved["ref"], "contribution": saved["contribution"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(1_048_577)
        if len(raw) <= 1_048_576:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                capture_summary(payload, config=load_config(path=args.config), client=args.client)
    except Exception:
        pass  # optional capture must never interrupt the client
    print("{}")  # observe-only, never continue or block the client
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
