"""Pre-deploy configuration checker for a Pallas Athena redeployment.

Written for adopters standing up their OWN instance: it verifies that the
runtime environment and the committed config are internally consistent and no
longer point at the original deployment. Four passes:

  1. Runtime env       — required env vars resolve; fail-open security controls
                         are set (or explicitly acknowledged); SECRET_KEY
                         present locally, or the SIX Secret Manager secrets
                         resolve in production.
  2. Secret Manager    — each secret resolves, is non-empty, carries no stray
                         whitespace, and has the right shape. Never prints a
                         payload — only its shape.
  3. Integrations      — Graph / Bookings / the Outlook mirror, and WHICH
                         field is missing when one is not wired up.
  4. Owner-literal scan — greps the committed config (app.yaml, portail.yaml,
                         dispatch.yaml, config.py, main.py, client/config.py)
                         for values hardcoded to the original owner's
                         deployment that MUST be replaced.

This is a static/offline aid, not a security control — passing it does not
make a deployment secure, it only catches the common "forgot to change X"
mistakes.

THIS FILE IS NOW A THIN CLI. The tables live in
``utils/deployment_inventory``, the rows in ``utils/deployment_report`` and the
checks in ``utils/config_checks`` — all three shared with
« Paramètres → Configuration », so the inventory cannot drift between the
terminal and the screen. That split exists because the knowledge being
reachable only from a terminal WAS the defect: nothing runs this script
automatically, which is how a missing ``cf-origin-secret`` went unreported for
months.

Run from the athena/ directory:

    python -m scripts.check_config          # checks ENV from your shell/.env
    python -m scripts.check_config --prod   # force the production ruleset
    python -m scripts.check_config --json   # structured, for a machine
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_checks import run_all  # noqa: E402
from utils.deployment_report import FAIL, OK, WARN, render_json  # noqa: E402


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        # python-dotenv is optional; skip .env loading when it is not installed.
        pass


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Apply the production ruleset regardless of ENV.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of text.",
    )
    args = parser.parse_args()

    is_prod = args.prod or os.environ.get("ENV") == "production"

    if args.json:
        rpt = run_all(is_prod)
        print(json.dumps(render_json(rpt), indent=2, ensure_ascii=False))
    else:
        print(
            "Pallas Athena config check  "
            f"(mode: {'production' if is_prod else 'development'})"
        )
        rpt = run_all(is_prod, printer=print)

    counts = rpt.counts()
    if not args.json:
        print("\nSummary")
        print("-------")
        print(
            f"  {counts[OK]} ok, {counts[WARN]} warning(s), "
            f"{counts[FAIL]} failure(s)"
        )
    if counts[FAIL]:
        if not args.json:
            print("\nRESULT: FAIL - resolve the failures above before deploying.")
        return 1
    if counts[WARN]:
        if not args.json:
            print(
                "\nRESULT: OK with warnings - review each warning; some are "
                "expected in local dev, and the fork-hygiene warnings are "
                "expected on the ORIGINAL deployment."
            )
        return 0
    if not args.json:
        print("\nRESULT: OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
