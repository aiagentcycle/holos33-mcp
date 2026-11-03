"""Bindt alleen op localhost. nginx zet de server publiek."""

from __future__ import annotations

import argparse
import os

import uvicorn

from holos33_mcp.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Holos33 read-only MCP server")
    parser.add_argument("--host", default=os.environ.get("HOLOS33_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOLOS33_MCP_PORT", "8324")))
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("alleen 127.0.0.1 is toegestaan")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info", proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
