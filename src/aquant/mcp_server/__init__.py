"""aquant.mcp_server:MCP server(薄桥模式),通过 stdio 暴露本地 FastAPI 能力。

模块结构:
- server:MCPServer 实例与 3 个 tool(health / validate / backtest),纯 HTTP 转发;
- 本包不 import aquant.runner / aquant.agents 等重依赖,保持「薄桥」定位。
"""

from aquant.mcp_server.server import DEFAULT_API_BASE, mcp, main

__all__ = ["DEFAULT_API_BASE", "mcp", "main"]
