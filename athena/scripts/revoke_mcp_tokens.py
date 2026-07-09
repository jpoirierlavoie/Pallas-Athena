"""Break-glass: revoke every MCP token (Phase I).

Marks every document in ``oauth_tokens`` revoked; Claude's next call gets a
401 until the user re-authorizes the connector. Optionally purges OAuth
clients as well (``--purge-clients`` removes stale never-used registrations;
``--purge-all-clients`` removes every client, forcing a fresh DCR).

Run from the athena/ directory:

    python -m scripts.revoke_mcp_tokens [--purge-clients | --purge-all-clients]
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
        # python-dotenv is optional; skip .env loading when it isn't installed
        # (env vars must then already be present in the environment).
        pass


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purge-clients",
        action="store_true",
        help="Also delete never-used client registrations older than 30 days.",
    )
    parser.add_argument(
        "--purge-all-clients",
        action="store_true",
        help="Also delete EVERY registered OAuth client.",
    )
    args = parser.parse_args()

    from mcp import store

    revoked = store.revoke_all_tokens()
    print(f"Revoked tokens: {revoked}")

    if args.purge_all_clients:
        deleted = 0
        for snap in store.db.collection(store.CLIENTS_COLLECTION).stream():
            snap.reference.delete()
            deleted += 1
        print(f"Deleted clients: {deleted}")
    elif args.purge_clients:
        deleted = store.purge_stale_clients(max_age_days=30)
        print(f"Deleted stale never-used clients: {deleted}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
