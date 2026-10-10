"""Recover XHS note links from exact author IDs in a local source dataset."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import Settings
from app.crawlers.xiaohongshu_url_resolver import NOTE_ID, recover_author
from app.services.engagement_service import EngagementService


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


async def run(args: argparse.Namespace) -> None:
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("输入必须是 JSON 数组")
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        parsed = urlsplit(url)
        if parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}:
            continue
        note_id = parsed.path.rsplit("/", 1)[-1]
        account = str(row.get("author_id") or "").strip()
        if NOTE_ID.fullmatch(note_id) and account and "xsec_token=" not in parsed.query:
            grouped[account].add(note_id)

    status_path = args.output.with_name(args.output.stem + "-status.json")
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    done: dict[str, dict[str, str]] = status.get("done", {})
    failures: dict[str, str] = status.get("failures", {})
    settings = Settings(
        XIAOHONGSHU_SESSION_MODE="stored",
        BROWSER_FALLBACK_ENABLED=False,
        RELIABILITY_MODE="economy",
    )
    service = EngagementService.from_settings(settings)
    processed = 0
    try:
        for account, targets in grouped.items():
            if account in done:
                continue
            if args.max_authors and processed >= args.max_authors:
                break
            try:
                done[account] = await asyncio.wait_for(
                    recover_author(service.crawler, account, targets, args.max_pages),
                    timeout=args.timeout,
                )
                failures.pop(account, None)
            except Exception as exc:
                failures[account] = f"{type(exc).__name__}: {str(exc)[:160]}"
            processed += 1
            recovered = {note_id: url for item in done.values() for note_id, url in item.items()}
            output = [
                {**row, "url": recovered.get(urlsplit(str(row.get("url") or "")).path.rsplit("/", 1)[-1], row.get("url"))}
                if isinstance(row, dict) else row
                for row in rows
            ]
            save_json(args.output, output)
            save_json(status_path, {"done": done, "failures": failures})
            print(
                f"authors={len(done)}/{len(grouped)} recovered_notes={len(recovered)} failures={len(failures)}",
                flush=True,
            )
            if args.delay and (not args.max_authors or processed < args.max_authors):
                await asyncio.sleep(args.delay)
    finally:
        await service.crawler.aclose()
        if service.proxy_provider is not None:
            await service.proxy_provider.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="根据小红书号补全本地笔记链接")
    parser.add_argument("--input", type=Path, default=Path(".local/top20_platform_data.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/top20_platform_data_xhs_enriched.json"))
    parser.add_argument("--max-authors", type=int, default=0, help="本次最多处理多少个作者；0 为全部")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--delay", type=float, default=4)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
