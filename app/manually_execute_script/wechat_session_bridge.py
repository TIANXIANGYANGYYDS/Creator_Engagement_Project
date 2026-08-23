from __future__ import annotations

import argparse
import os

import uvicorn

from app.crawlers.wechat_session_bridge import (
    WeChatSessionBridge,
    WeChatSessionStore,
    create_wechat_bridge_app,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行仅保留内存凭据的微信公众号本地会话桥",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8210)
    parser.add_argument("--ttl-seconds", type=int, default=25 * 60)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("会话桥只允许绑定回环地址；远程访问请使用 SSH 隧道")
    token = os.environ.get("WECHAT_SESSION_BRIDGE_TOKEN", "").strip()
    if len(token) < 24:
        raise SystemExit(
            "请先设置至少 24 字符的 WECHAT_SESSION_BRIDGE_TOKEN；不要把 token 发到聊天或提交到 Git"
        )
    bridge = WeChatSessionBridge(
        store=WeChatSessionStore(ttl_seconds=args.ttl_seconds),
    )
    app = create_wechat_bridge_app(token, bridge=bridge)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
