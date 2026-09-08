"""vaws-knowledge retrieval service.

Three read layers (`shared`, `project`, `candidate`), one query surface, one
write path (`candidate` only). Nothing in this package imports torch,
torch_npu, or talks to NPU hardware: it only reads and writes YAML/JSON
knowledge documents.
"""

from vaws_knowledge import package_version

__all__ = ["package_version"]
