"""vaws-knowledge retrieval service.

Three read layers (`shared`, `project`, `candidate`), one query surface, one
write path (`candidate` only). Markdown is the content authority. OpenViking
indexes it. Nothing in this package imports torch, torch_npu, or talks to
NPU hardware.
"""

from vaws_knowledge import package_version

__all__ = ["package_version"]
