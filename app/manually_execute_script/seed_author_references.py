"""Verify numeric/public author IDs using public work references, without new API parameters."""

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import Settings
from app.manually_execute_script.seed_douyin_authors import seed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--platform", choices=("douyin", "kuaishou"), required=True)
    parser.add_argument("--author-id", action="append", default=[])
    args = parser.parse_args()
    result = asyncio.run(seed(args.input, set(args.author_id), Settings(), platform=args.platform))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
