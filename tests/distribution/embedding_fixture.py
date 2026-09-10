"""Test-only loopback embedding endpoint for the live native chain.

Serves the pinned FastEmbed/ONNX model over the OpenAI-compatible
``/v1/embeddings`` shape, rejects generation requests, and exposes cumulative
call counters at ``/health`` so tests can prove the import phase does not
recompute document embeddings. Loads from a pre-existing local model cache
only (``local_files_only``); the cache path arrives via CLI arg, never baked
into committed files.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSION = 384


def serve(*, host: str, port: int, cache_dir: Path, metrics_path: Path | None = None) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("embedding fixture must bind loopback only")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    from fastembed import TextEmbedding

    model = TextEmbedding(
        model_name=MODEL,
        cache_dir=str(cache_dir),
        threads=max(1, min(4, os.cpu_count() or 1)),
        providers=["CPUExecutionProvider"],
        local_files_only=True,
    )
    counts: dict[str, Any] = {
        "requests": 0,
        "texts": 0,
        "characters": 0,
        "compute_s": 0.0,
        "generation_rejected": 0,
        "model": MODEL,
        "dimension": DIMENSION,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            del args

        def _reply(self, payload: Any, status: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self._reply({"ok": True, **counts})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not self.path.rstrip("/").endswith("/embeddings"):
                counts["generation_rejected"] += 1
                self._reply({"error": "generation disabled"}, 501)
                return
            texts = body.get("input")
            if isinstance(texts, str):
                texts = [texts]
            if not isinstance(texts, list) or not texts:
                self._reply({"error": "input is required"}, 400)
                return
            started = time.monotonic()
            vectors = list(model.embed([str(item) for item in texts]))
            elapsed = time.monotonic() - started
            counts["requests"] += 1
            counts["texts"] += len(texts)
            counts["characters"] += sum(len(str(item)) for item in texts)
            counts["compute_s"] += elapsed
            if metrics_path is not None:
                with metrics_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"at": time.time(), "n": len(texts), "compute_s": elapsed}) + "\n")
            self._reply(
                {
                    "object": "list",
                    "model": body.get("model") or MODEL,
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                    "data": [
                        {"object": "embedding", "index": index, "embedding": vector.tolist()}
                        for index, vector in enumerate(vectors)
                    ],
                }
            )

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"ready": True, "pid": os.getpid(), "port": port}), flush=True)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--metrics", default="")
    args = parser.parse_args()
    serve(
        host=args.host,
        port=args.port,
        cache_dir=Path(args.cache_dir),
        metrics_path=Path(args.metrics) if args.metrics else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
