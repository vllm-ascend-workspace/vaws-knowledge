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
    if not config.publishing.get("enabled"):
        return {"status": "disabled"}
    event = payload.get("hook_event_name")
    if event == "afterAgentResponse" and client == "cursor":
        text = payload.get("text")
    elif event == "Stop" and client in {"codex", "claude"}:
        text = payload.get("last_assistant_message")
    else:
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
    for key in ("session_id", "turn_id", "conversation_id", "generation_id"):
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
    except Exception as exc:
        print(f"Knowledge summary capture deferred: {type(exc).__name__}", file=sys.stderr)
    print("{}")  # observe-only, never continue or block the client
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
