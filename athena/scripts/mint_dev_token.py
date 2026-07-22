"""Mint a local-development MCP bearer token (Phase I).

Creates a dev ``oauth_clients`` record plus an access/refresh pair and
prints the raw access token ONCE. Hard-refuses to run in production.

Run from the athena/ directory:

    python -m scripts.mint_dev_token [--hours N] [--write]

``--write`` adds the ``athena:write`` scope so the note-write tools can be
exercised locally; without it the token is read-only, exactly like a
consent flow where the « autoriser l'écriture » box was left unticked.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> None:
    """Load the repo-root .env — config.py resolves required env vars at
    import time, and ``python -m scripts.X`` doesn't go through Flask's
    dotenv loading."""
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        # python-dotenv is optional (dev-only convenience); when it isn't
        # installed, skip .env loading and rely on the ambient environment.
        pass


def main() -> int:
    _load_env()
    if os.environ.get("ENV") == "production":
        print("REFUSED: mint_dev_token must never run with ENV=production.")
        return 1

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=int,
        default=1,
        help="Access-token lifetime in hours (default 1).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Also grant athena:write (the two note-write tools).",
    )
    args = parser.parse_args()

    from datetime import datetime, timedelta, timezone

    from mcp import SCOPE_READ, SCOPE_WRITE
    from mcp import store

    scope = f"{SCOPE_READ} {SCOPE_WRITE}" if args.write else SCOPE_READ

    client = store.create_client(
        "Dev local (mint_dev_token)", ["http://localhost/dev-callback"]
    )
    pair = store.create_token_pair(
        client_id=client["client_id"], scope=scope, resource=None
    )
    if args.hours != 1:
        expire_at = datetime.now(timezone.utc) + timedelta(hours=args.hours)
        store.db.collection(store.TOKENS_COLLECTION).document(
            pair["access_token_hash"]
        ).update({"expire_at": expire_at})

    print(f"client_id:     {client['client_id']}")
    print(f"scope:         {scope}")
    print(f"expires in:    {args.hours} hour(s)")
    print("access token (shown once, never logged):")
    print(pair["access_token"])
    print()
    print("Use it with MCP Inspector or curl:")
    print('  -H "Authorization: Bearer <token>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
