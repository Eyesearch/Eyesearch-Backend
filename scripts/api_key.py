"""Issue, list and revoke API keys.

    python scripts/api_key.py create "partner name" [--per-minute 60] [--daily 10000]
    python scripts/api_key.py list
    python scripts/api_key.py revoke <key_id>

The key is printed once, at creation. Only its hash is stored, so it cannot be
printed again — a lost key is replaced, not recovered.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import build_client  # noqa: E402
from app.engine.config import get_settings  # noqa: E402
from app.security import create_key, list_keys, revoke_key  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Eyesearch API keys")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("create", help="issue a key")
    make.add_argument("name")
    make.add_argument("--per-minute", type=int, default=60)
    make.add_argument("--daily", type=int, default=10000)

    sub.add_parser("list", help="list keys")

    kill = sub.add_parser("revoke", help="revoke a key by id")
    kill.add_argument("key_id")

    args = parser.parse_args()
    client = build_client(get_settings())
    try:
        if args.command == "create":
            key, record = await create_key(
                client,
                args.name,
                rate_limit_per_minute=args.per_minute,
                daily_quota=args.daily,
            )
            print(f"key_id : {record.key_id}")
            print(f"name   : {record.name}")
            print(f"limits : {record.rate_limit_per_minute}/min, {record.daily_quota}/day")
            print(f"\nkey (shown once): {key}\n")
            return 0

        if args.command == "list":
            for record in await list_keys(client):
                state = "revoked" if record.revoked else "active"
                print(
                    f"{record.key_id}  {record.prefix}…  {record.name:24}  {state:8}  "
                    f"{record.calls_total:8} calls  ({record.calls_today} today)"
                )
            return 0

        if await revoke_key(client, args.key_id):
            print(f"revoked {args.key_id}")
            return 0
        print(f"no active key with id {args.key_id}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
