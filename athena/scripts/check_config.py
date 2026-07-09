"""Pre-deploy configuration checker for a Pallas Athena redeployment.

Written for adopters standing up their OWN instance: it verifies that the
runtime environment and the committed config are internally consistent and no
longer point at the original deployment. It performs three passes:

  1. Runtime env       — required env vars resolve; fail-open security controls
                         are set (or explicitly acknowledged); SECRET_KEY present
                         locally, or the four Secret Manager secrets resolve in
                         production.
  2. Owner-literal scan — greps the committed config (app.yaml, config.py,
                         main.py) for values hardcoded to the original owner's
                         deployment that MUST be replaced.
  3. Summary            — OK / WARN / FAIL counts; exit code 1 if any FAIL.

This is a static/offline aid, not a security control — passing it does not make
a deployment secure, it only catches the common "forgot to change X" mistakes.

Run from the athena/ directory:

    python -m scripts.check_config          # checks ENV from your shell/.env
    python -m scripts.check_config --prod   # force the production ruleset
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ATHENA_DIR)

# ── Values hardcoded to the original deployment. An adopter must replace every
# one of these before going live. Keep this list in sync with DEPLOYMENT.md
# ("Owner-specific values to replace").
OWNER_LITERALS: dict[str, str] = {
    "GCP project id": "athena-pallas",
    "Storage bucket": "athena-pallas.firebasestorage.app",
    "Firebase app id": "1:1073430388324:web:6d60d0d67d74ce1b730971",
    "Authorized user email": "jason@poirierlavoie.ca",
    "Firm name": "Me Jason Poirier Lavoie",
    "reCAPTCHA site key": "6LeR9Y0sAAAAAJhkKWa_wYdbproZUQFjEO7FkfEb",
    "MCP canonical origin": "https://athena.poirierlavoie.ca",
    "Domain": "athena.poirierlavoie.ca",
    "TWA package": "ca.poirierlavoie.athena",
}

# Files that legitimately carry deployment config (scanned in pass 2). Docs and
# tests are excluded on purpose — they reference the owner's ids as examples.
_SCAN_FILES = [
    os.path.join(_ATHENA_DIR, "app.yaml"),
    os.path.join(_ATHENA_DIR, "config.py"),
    os.path.join(_ATHENA_DIR, "main.py"),
]

_REQUIRED_ENV = ["FIREBASE_PROJECT_ID", "FIREBASE_STORAGE_BUCKET", "AUTHORIZED_USER_EMAIL"]

# Fail-open controls: unset in production means the protection silently disables.
_FAIL_OPEN_ENV = {
    "RECAPTCHA_ENTERPRISE_SITE_KEY": "Firebase App Check is disabled (HTMX requests unverified).",
    "CF_ORIGIN_SECRET": "The Cloudflare origin-secret check is disabled (direct-to-App-Engine access not blocked).",
    "DAV_PASSWORD_HASH": "DAV Basic Auth cannot succeed — DavX5 sync is unavailable.",
}

_OK, _WARN, _FAIL = "OK", "WARN", "FAIL"
_SYMBOL = {_OK: "  ok ", "WARN": "warn ", "FAIL": "FAIL "}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def add(self, level: str, message: str) -> None:
        self.rows.append((level, message))

    def section(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * len(title))

    def emit(self, level: str, message: str) -> None:
        self.add(level, message)
        print(f"  [{_SYMBOL[level]}] {message}")

    def counts(self) -> dict[str, int]:
        c = {_OK: 0, _WARN: 0, _FAIL: 0}
        for level, _ in self.rows:
            c[level] += 1
        return c


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        # python-dotenv is optional; skip .env loading when it is not installed.
        pass


def _check_runtime_env(rpt: Report, is_prod: bool) -> None:
    rpt.section("1. Runtime environment")

    for var in _REQUIRED_ENV:
        val = os.environ.get(var, "")
        if val:
            rpt.emit(_OK, f"{var} is set")
        else:
            rpt.emit(_FAIL, f"{var} is REQUIRED and unset (config.py raises at import without it)")

    # SECRET_KEY: env var locally, Secret Manager in prod.
    if is_prod:
        _check_prod_secrets(rpt)
    else:
        if os.environ.get("SECRET_KEY"):
            rpt.emit(_OK, "SECRET_KEY is set (local dev)")
        else:
            rpt.emit(_FAIL, "SECRET_KEY is unset — the app will not boot")

    for var, consequence in _FAIL_OPEN_ENV.items():
        if os.environ.get(var):
            rpt.emit(_OK, f"{var} is set")
        elif is_prod:
            rpt.emit(_WARN, f"{var} unset in production — {consequence}")
        else:
            rpt.emit(_OK, f"{var} unset (fine for local dev)")

    if os.environ.get("REQUIRE_MFA", "true").lower() != "true":
        rpt.emit(_WARN, "REQUIRE_MFA is not 'true' — Phone MFA is not enforced")
    else:
        rpt.emit(_OK, "REQUIRE_MFA=true")

    mcp_origin = os.environ.get("MCP_CANONICAL_ORIGIN", "")
    if os.environ.get("MCP_ENABLED", "true").lower() == "true":
        if not mcp_origin and is_prod:
            rpt.emit(_WARN, "MCP_ENABLED=true but MCP_CANONICAL_ORIGIN is unset (defaults to the owner's domain in config.py)")


def _check_prod_secrets(rpt: Report) -> None:
    project = os.environ.get("FIREBASE_PROJECT_ID", "")
    if not project:
        rpt.emit(_FAIL, "Cannot verify Secret Manager: FIREBASE_PROJECT_ID unset")
        return
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
    except Exception as exc:  # noqa: BLE001 — best-effort, creds may be absent
        rpt.emit(_WARN, f"Could not init Secret Manager client to verify secrets ({type(exc).__name__}); skipping")
        return

    for secret_id, required in (
        ("flask-secret-key", True),
        ("firebase-api-key", False),
        ("dav-password-hash", False),
        ("cf-origin-secret", False),
    ):
        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        try:
            client.access_secret_version(request={"name": name})
            rpt.emit(_OK, f"secret '{secret_id}' resolves")
        except Exception as exc:  # noqa: BLE001
            level = _FAIL if required else _WARN
            rpt.emit(level, f"secret '{secret_id}' did not resolve ({type(exc).__name__})")


def _check_owner_literals(rpt: Report) -> None:
    rpt.section("2. Owner-specific values still in the committed config")
    found_any = False
    for path in _SCAN_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, _REPO_ROOT)
        for label, literal in OWNER_LITERALS.items():
            if literal in text:
                found_any = True
                rpt.emit(_WARN, f"{rel}: still contains the owner's {label} ('{literal}')")
    # main.py fingerprint (only matters for the Android TWA)
    main_py = os.path.join(_ATHENA_DIR, "main.py")
    if os.path.exists(main_py):
        with open(main_py, encoding="utf-8") as fh:
            text = fh.read()
        if re.search(r"47:3B:05:FB", text):
            found_any = True
            rpt.emit(_WARN, "main.py: assetlinks.json still has the owner's TWA signing fingerprint (only relevant if you ship the Android TWA)")
    if not found_any:
        rpt.emit(_OK, "no owner-specific literals detected in app.yaml / config.py / main.py")


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prod", action="store_true", help="Apply the production ruleset regardless of ENV.")
    args = parser.parse_args()

    is_prod = args.prod or os.environ.get("ENV") == "production"
    print(f"Pallas Athena config check  (mode: {'production' if is_prod else 'development'})")

    rpt = Report()
    _check_runtime_env(rpt, is_prod)
    _check_owner_literals(rpt)

    counts = rpt.counts()
    rpt.section("Summary")
    print(f"  {counts[_OK]} ok, {counts[_WARN]} warning(s), {counts[_FAIL]} failure(s)")
    if counts[_FAIL]:
        print("\nRESULT: FAIL - resolve the failures above before deploying.")
        return 1
    if counts[_WARN]:
        print("\nRESULT: OK with warnings - review each warning; some are expected in local dev.")
        return 0
    print("\nRESULT: OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
