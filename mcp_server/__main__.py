"""入口：python -m mcp_server [--stdio]

- 缺省：streamable HTTP 常驻，监听 SHEALTH_MCP_HOST:SHEALTH_MCP_PORT
  （默认 127.0.0.1:8180，MCP 端点路径 /mcp）
- --stdio：本地 spawn 场景（客户端拉起子进程、走标准输入输出）
"""
import sys

from mcp_server.server import mcp


def main() -> None:
    transport = "stdio" if "--stdio" in sys.argv[1:] else "streamable-http"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
