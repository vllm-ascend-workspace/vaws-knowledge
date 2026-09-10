"""OpenViking 0.4.19 adapter: native write/find/read/rm on loopback."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from vaws_knowledge.local.backend import Hit, UnavailableBackend
from vaws_knowledge.local.instance import instance_for_config, without_proxies
from vaws_knowledge.local.shared import shared_search_uri
from vaws_knowledge.markdown import LAYERS, URI_ROOT, parse_markdown

LAYER_ROOTS = {layer: f"{URI_ROOT}/{layer}" for layer in LAYERS}


class OpenVikingBackend:
    name = "openviking"

    def __init__(self, config: Any):
        self.config = config
        self.instance = instance_for_config(config)
        self._client: Any = None
        self._reason = ""

    def available(self) -> tuple[bool, str]:
        try:
            self._ensure_client()
        except Exception as exc:  # noqa: BLE001
            self._reason = f"{type(exc).__name__}: {exc}"
            return False, self._reason
        return True, f"openviking {self.instance.describe().get('openviking_url')}"

    def upsert(self, uri: str, content: str, *, layer: str, wait: bool = True) -> None:
        client = self._ensure_client()
        with without_proxies():
            self._ensure_layer(client, layer)
            client.write(
                uri,
                content,
                wait=wait,
                options={"processing_mode": "vectors_only"},
            )
            if wait:
                client.wait_processed(timeout=120)

    def delete(self, uri: str) -> None:
        client = self._ensure_client()
        with without_proxies():
            try:
                client.rm(uri, wait=True, timeout=60)
            except Exception as exc:  # noqa: BLE001
                if "NOT_FOUND" not in str(exc).upper() and "not found" not in str(exc).lower():
                    raise

    def read(self, uri: str) -> str | None:
        client = self._ensure_client()
        with without_proxies():
            try:
                return client.read(uri)
            except Exception as exc:  # noqa: BLE001
                if "NOT_FOUND" in str(exc).upper() or "not found" in str(exc).lower():
                    return None
                raise

    def search(
        self,
        text: str,
        *,
        layers: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        client = self._ensure_client()
        wanted = [name for name in (layers or LAYERS) if name in LAYER_ROOTS]
        fetch = max(int(limit or 8) * 4, 16)
        hits: list[Hit] = []
        state_root = getattr(self.instance, "state_root", None)
        with without_proxies():
            for layer in wanted:
                target = (
                    shared_search_uri(state_root) if layer == "shared" else LAYER_ROOTS[layer]
                )
                payload = client.find(
                    text or "",
                    target_uri=target,
                    limit=fetch,
                    options={
                        "level": 2,
                        "read_content": True,
                        "score_threshold": 0,
                    },
                )
                resources = payload.get("resources") if isinstance(payload, dict) else None
                if not isinstance(resources, list):
                    continue
                for item in resources:
                    if not isinstance(item, dict):
                        continue
                    uri = str(item.get("uri") or "")
                    if not uri:
                        continue
                    content = str(item.get("content") or item.get("text") or "")
                    title, body = parse_markdown(content) if content else ("", "")
                    if not title:
                        title = Path(uri).stem
                    score = float(item.get("score") or item.get("similarity") or 0.0)
                    hits.append(
                        Hit(
                            uri=uri,
                            score=score,
                            title=title,
                            excerpt=" ".join((body or content).split())[:240],
                            layer=layer,
                            content=content,
                        )
                    )
        hits.sort(key=lambda hit: (-hit.score, hit.uri))
        return hits[: max(int(limit or 8), 1)]

    def _ensure_client(self) -> Any:
        if self._client is not None:
            if self.instance.describe().get("live"):
                return self._client
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        try:
            from openviking_sdk import SyncHTTPClient
        except ImportError as exc:
            raise RuntimeError("openviking_sdk is not installed") from exc
        with without_proxies():
            status = self.instance.ensure()
            if not status.get("live"):
                raise RuntimeError("openviking instance is not live")
            url = status["openviking_url"]
            client = SyncHTTPClient(url=url, api_key=self.instance.data_key())
            client.initialize()
        self._client = client
        for layer in LAYERS:
            self._ensure_layer(client, layer)
        return client

    def _ensure_layer(self, client: Any, layer: str) -> None:
        uri = LAYER_ROOTS[layer]
        try:
            client.mkdir(uri)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).upper()
            if "ALREADY" not in text and "EXISTS" not in text:
                raise


def try_openviking(config: Any) -> Any:
    try:
        backend = OpenVikingBackend(config)
        ok, reason = backend.available()
        if ok:
            return backend
        return UnavailableBackend(reason)
    except Exception as exc:  # noqa: BLE001
        return UnavailableBackend(f"openviking unavailable: {type(exc).__name__}: {exc}")
