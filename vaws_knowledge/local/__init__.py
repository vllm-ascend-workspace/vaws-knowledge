"""Local OpenViking instance, CPU embedding, and retrieval backends."""

from vaws_knowledge.local.backend import (
    Hit,
    MemoryBackend,
    RetrievalBackend,
    UnavailableBackend,
    backend_for_config,
)
from vaws_knowledge.local.shared import current_shared, shared_search_uri

__all__ = [
    "Hit",
    "MemoryBackend",
    "RetrievalBackend",
    "UnavailableBackend",
    "backend_for_config",
    "current_shared",
    "shared_search_uri",
]
