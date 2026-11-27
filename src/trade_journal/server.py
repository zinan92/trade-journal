"""Trade Journal 本地启动入口（默认 http://127.0.0.1:8796）。"""
from __future__ import annotations

import argparse
import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade Journal — 本地 K 线交易复盘工作台")
    sub = parser.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="启动本地服务")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8796)
    args = parser.parse_args()
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8796)
    print(f"Trade Journal 已启动：http://{host}:{port}", flush=True)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
