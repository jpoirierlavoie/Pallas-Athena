"""The deployment configuration checks — run them from a CLI or from a route.

Moved out of ``scripts/check_config.py`` so the knowledge is reachable from
more than a terminal. That coupling was the whole defect: CLAUDE.md records
that ``scripts/check_config.py`` is the only thing which reports a missing
``cf-origin-secret``, *and nothing runs it* — so the entire edge-defence layer
was off for months with nothing signalling it.

The split is three-way and deliberate:

* ``utils/deployment_inventory`` — the tables. PURE, stdlib only.
* ``utils/deployment_report`` — the rows. PURE, stdlib only.
* **this module** — the checks. Reads the environment, the committed files and
  Secret Manager, so it is emphatically NOT pure; it is the only one of the
  three a pure test cannot import for free.

``scripts/check_config.py`` is now a thin CLI over :func:`run_all`, and
« Paramètres → Configuration » renders the same rows. One inventory, one set of
predicates, two surfaces — which is the point.

NEVER RENDER OR LOG A PAYLOAD. Every secret row carries a length, a version
and a verdict; the value itself does not leave this module. The rule comes
from the original checker and is repeated here because this module is what a
web page consumes.
"""

import os
import re
from typing import Optional

from utils.deployment_inventory import (
    FAIL_OPEN_ENV,
    OWNER_FINGERPRINT_RE,
    OWNER_LITERALS,
    REQUIRED_ENV,
    SCAN_FILES,
    SECRET_BACKED_ENV,
    SECRETS,
    stray_whitespace,
)
from utils.deployment_report import FAIL, OK, WARN, Report

_ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ATHENA_DIR)

# Section titles are STABLE IDENTIFIERS, and they are the CLI's headings too.
# They stay English because every message under them is English and this
# module is developer-facing; the settings page keys its own French headings
# off these constants rather than having the check layer dictate its wording.
SECTION_ENV = "1. Runtime environment and fail-open controls"
SECTION_SECRETS = "2. Secret Manager"
SECTION_INTEGRATIONS = "3. Integrations"
SECTION_FORK = "4. Fork hygiene (owner-specific values)"


def is_production() -> bool:
    return os.environ.get("ENV") == "production"


# ── 1. Runtime environment ───────────────────────────────────────────────


def check_runtime_env(rpt: Report, is_prod: bool) -> None:
    rpt.section(SECTION_ENV)

    for var in REQUIRED_ENV:
        if os.environ.get(var, ""):
            rpt.emit(OK, f"{var} is set", var=var)
        else:
            rpt.emit(
                FAIL,
                f"{var} is REQUIRED and unset (config.py raises at import "
                "without it)",
                var=var,
            )

    # SECRET_KEY: env var locally, Secret Manager in prod.
    if is_prod:
        check_prod_secrets(rpt)
    else:
        if os.environ.get("SECRET_KEY"):
            rpt.emit(OK, "SECRET_KEY is set (local dev)", var="SECRET_KEY")
        else:
            rpt.emit(
                FAIL, "SECRET_KEY is unset — the app will not boot", var="SECRET_KEY"
            )

    for var, consequence in FAIL_OPEN_ENV.items():
        if os.environ.get(var):
            rpt.emit(OK, f"{var} is set", var=var)
        elif is_prod and var in SECRET_BACKED_ENV:
            # Reporting these "unset in production" from os.environ is a FALSE
            # NEGATIVE by construction: config._secret() never consults the
            # env var when ENV=production. The secret pass is the authority.
            rpt.emit(
                OK,
                f"{var} comes from Secret Manager in production — see the "
                "Secret Manager section",
                var=var,
            )
        elif is_prod:
            rpt.emit(
                WARN, f"{var} unset in production — {consequence}", var=var,
                fail_open=True,
            )
        else:
            rpt.emit(OK, f"{var} unset (fine for local dev)", var=var)

    if os.environ.get("REQUIRE_MFA", "true").lower() != "true":
        rpt.emit(
            WARN,
            "REQUIRE_MFA is not 'true' — Phone MFA is not enforced",
            var="REQUIRE_MFA",
        )
    else:
        rpt.emit(OK, "REQUIRE_MFA=true", var="REQUIRE_MFA")

    if os.environ.get("MCP_ENABLED", "true").lower() == "true":
        if not os.environ.get("MCP_CANONICAL_ORIGIN", "") and is_prod:
            rpt.emit(
                WARN,
                "MCP_ENABLED=true but MCP_CANONICAL_ORIGIN is unset (defaults "
                "to the owner's domain in config.py)",
                var="MCP_CANONICAL_ORIGIN",
            )


# ── 2. Secret Manager ────────────────────────────────────────────────────


def check_prod_secrets(rpt: Report) -> None:
    rpt.section(SECTION_SECRETS)
    project = os.environ.get("FIREBASE_PROJECT_ID", "")
    if not project:
        rpt.emit(FAIL, "Cannot verify Secret Manager: FIREBASE_PROJECT_ID unset")
        return
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
    except Exception as exc:  # noqa: BLE001 — best-effort, creds may be absent
        rpt.emit(
            WARN,
            f"Could not init Secret Manager client to verify secrets "
            f"({type(exc).__name__}); skipping",
        )
        return

    for secret in SECRETS:
        name = f"projects/{project}/secrets/{secret.secret_id}/versions/latest"
        try:
            response = client.access_secret_version(request={"name": name})
        except Exception as exc:  # noqa: BLE001
            kind = type(exc).__name__
            # A DENIAL is not an ABSENCE. `portail-secret-key` belongs to the
            # portail service and its own service account; the main service
            # legitimately holds no accessor on it, so reporting that as a
            # missing secret would be a false alarm on a CORRECT deployment.
            if kind in ("PermissionDenied", "Forbidden"):
                rpt.emit(
                    WARN,
                    f"secret '{secret.secret_id}' is not readable from this "
                    f"service ({kind}) — expected when it belongs to another "
                    "service account; verify it from there",
                    secret_id=secret.secret_id,
                    outcome="unreadable",
                )
                continue
            rpt.emit(
                FAIL if secret.required else WARN,
                f"secret '{secret.secret_id}' did not resolve ({kind}) — "
                f"{secret.consequence}",
                secret_id=secret.secret_id,
                outcome="absent",
            )
            continue

        # The payload matters, not just its existence. Nothing in config.py
        # strips it, so surrounding whitespace becomes part of the value: a
        # trailing newline on cf-origin-secret makes hmac.compare_digest fail
        # against a header no Transform Rule can reproduce, and EVERY request
        # answers 403. Never print the payload — only its shape.
        payload = response.payload.data.decode("utf-8", "replace")
        version = str(getattr(response, "name", "")).rsplit("/", 1)[-1]

        if not payload:
            rpt.emit(
                WARN,
                f"secret '{secret.secret_id}' resolves but is EMPTY — "
                f"{secret.consequence}",
                secret_id=secret.secret_id, outcome="empty", version=version,
            )
            continue

        stray = stray_whitespace(payload)
        if stray:
            rpt.emit(
                FAIL,
                f"secret '{secret.secret_id}' has surrounding whitespace "
                f"({len(payload) - len(payload.strip())} stray char(s)) — it "
                "is compared byte for byte; recreate it without them",
                secret_id=secret.secret_id, outcome="stray_whitespace",
                version=version, detail_fr=stray,
            )
            continue

        shape_error = secret.shape(payload) if secret.shape else None
        if shape_error:
            rpt.emit(
                FAIL,
                f"secret '{secret.secret_id}' has the wrong shape "
                f"({len(payload)} chars) — {secret.consequence}",
                secret_id=secret.secret_id, outcome="bad_shape",
                version=version, detail_fr=shape_error,
            )
            continue

        rpt.emit(
            OK,
            f"secret '{secret.secret_id}' resolves ({len(payload)} chars, no "
            "stray whitespace)",
            secret_id=secret.secret_id, outcome="ok",
            version=version, char_length=len(payload),
        )


# ── 3. Integrations ──────────────────────────────────────────────────────


def check_integrations(rpt: Report) -> None:
    """Graph / Bookings / the Outlook mirror — configured, and if not, WHICH.

    Reuses the existing predicates rather than re-deriving "is this wired
    up?", but they are four-way ``and``s that answer only true/false, so the
    missing field is named here — that is the difference between a page that
    says « non configuré » and one that says what to do next.
    """
    rpt.section(SECTION_INTEGRATIONS)
    try:
        from config import Config
    except Exception as exc:  # noqa: BLE001
        rpt.emit(WARN, f"Config unavailable ({type(exc).__name__}); skipping")
        return

    graph_fields = {
        "GRAPH_TENANT_ID": Config.GRAPH_TENANT_ID,
        "GRAPH_CLIENT_ID": Config.GRAPH_CLIENT_ID,
        "GRAPH_SENDER_UPN": Config.GRAPH_SENDER_UPN,
        "GRAPH_CLIENT_SECRET": Config.GRAPH_CLIENT_SECRET,
    }
    missing = sorted(k for k, v in graph_fields.items() if not v)
    if Config.graph_configured():
        rpt.emit(
            OK,
            "Microsoft Graph is configured (outbound email available)",
            integration="graph",
        )
    else:
        rpt.emit(
            WARN,
            "Microsoft Graph is NOT configured — outbound email is disabled "
            f"(missing: {', '.join(missing)})",
            integration="graph", missing=missing,
        )

    if Config.bookings_configured():
        rpt.emit(
            OK,
            "Bookings sync + Outlook mirror can run (Graph + a mailbox)",
            integration="bookings",
        )
    else:
        why = "BOOKINGS_JURISTE_UPN" if Config.graph_configured() else "Graph"
        rpt.emit(
            WARN,
            f"Bookings sync and the Outlook mirror cannot run — {why} is "
            "absent (both cron jobs no-op and say so every 10 minutes)",
            integration="bookings",
        )

    # An EMPTY keyword tuple is a total, silent outage: the predicate never
    # matches, nothing is imported, and the absence loop then flags every
    # already-imported reservation as cancelled by the client.
    if Config.BOOKINGS_SYNC_ACTIVE and not Config.BOOKINGS_SUBJECT_KEYWORDS:
        rpt.emit(
            FAIL,
            "BOOKINGS_SYNC_ACTIVE is true but BOOKINGS_SUBJECT_KEYWORDS is "
            "EMPTY — the sync would import nothing and flag existing "
            "reservations as cancelled",
            integration="bookings",
        )


# ── 4. Fork hygiene ──────────────────────────────────────────────────────


def check_owner_literals(rpt: Report) -> None:
    rpt.section(SECTION_FORK)
    found_any = False
    for rel in SCAN_FILES:
        path = os.path.join(_REPO_ROOT, rel)
        if not os.path.exists(path):
            # A typo in SCAN_FILES would disarm a check silently, which is why
            # tests/test_deployment_inventory.py pins that every entry exists.
            rpt.emit(WARN, f"{rel}: listed for scanning but not found", file=rel)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for label, literal in OWNER_LITERALS.items():
            if literal in text:
                found_any = True
                rpt.emit(
                    WARN,
                    f"{rel}: still contains the owner's {label} ('{literal}')",
                    file=rel, literal_label=label,
                )

    main_py = os.path.join(_ATHENA_DIR, "main.py")
    if os.path.exists(main_py):
        with open(main_py, encoding="utf-8") as fh:
            if re.search(OWNER_FINGERPRINT_RE, fh.read()):
                found_any = True
                rpt.emit(
                    WARN,
                    "main.py: assetlinks.json still has the owner's TWA "
                    "signing fingerprint (only relevant if you ship the "
                    "Android TWA)",
                    file="athena/main.py", literal_label="TWA fingerprint",
                )

    if not found_any:
        rpt.emit(
            OK,
            f"no owner-specific literals detected in the {len(SCAN_FILES)} "
            "scanned config files",
        )


# ── The whole thing ──────────────────────────────────────────────────────


def run_all(is_prod: Optional[bool] = None, printer=None) -> Report:
    """Every pass, into one report.

    ``printer`` is how the CLI gets its output; a route passes none and reads
    ``report.rows``.
    """
    if is_prod is None:
        is_prod = is_production()
    rpt = Report(printer=printer)
    check_runtime_env(rpt, is_prod)
    check_integrations(rpt)
    check_owner_literals(rpt)
    return rpt


__all__ = [
    "SECTION_ENV",
    "SECTION_FORK",
    "SECTION_INTEGRATIONS",
    "SECTION_SECRETS",
    "check_integrations",
    "check_owner_literals",
    "check_prod_secrets",
    "check_runtime_env",
    "is_production",
    "run_all",
]
