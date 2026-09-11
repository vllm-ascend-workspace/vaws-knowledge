# MCP stdio transport uses newline-delimited JSON-RPC messages

The Model Context Protocol stdio transport encodes JSON-RPC messages as UTF-8 and delimits them with newlines. Messages must not contain embedded newlines. This is the client-visible stdio contract; it is not a claim about a local hardware or runtime measurement.

## Source

- kind: official_documentation
- title: Transports (stdio)
- provider: Model Context Protocol
- url: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- version: 2025-11-25
- date: 2025-11-25

Official MCP specification page. Sourced documentation, not a local runtime measurement. A client handshake confirms protocol reachability; it does not turn this specification into operational evidence.

Topics: mcp, stdio, json-rpc, newline-delimited

Recorded 2026-09-10 by anonymous from vllm-ascend-workspace/vaws-knowledge.
