"""Environment-based configuration for Pallas Athena.

In production (ENV=production on App Engine), sensitive values are pulled from
Google Cloud Secret Manager. Locally, they are read from environment variables
(typically supplied by a gitignored .env file loaded by Flask).
"""

import os
from functools import lru_cache


def _is_production() -> bool:
    return os.environ.get("ENV") == "production"


@lru_cache(maxsize=None)
def _from_secret_manager(secret_id: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    project = os.environ["FIREBASE_PROJECT_ID"]
    name = f"projects/{project}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def _secret(secret_id: str, env_var: str, required: bool = True) -> str:
    """Resolve a sensitive value: Secret Manager in prod, env var locally.

    Optional secrets (``required=False``) resolve to ``""`` when absent in
    either source instead of failing application startup.
    """
    if _is_production():
        try:
            return _from_secret_manager(secret_id)
        except Exception:
            if required:
                raise
            return ""
    value = os.environ.get(env_var, "")
    if required and not value:
        raise RuntimeError(
            f"Missing required env var {env_var}. "
            f"Set it in your local .env file or shell environment."
        )
    return value


class Config:
    """Base configuration loaded from environment variables and Secret Manager."""

    # Flask
    SECRET_KEY: str = _secret("flask-secret-key", "SECRET_KEY")
    ENV: str = os.environ.get("ENV", "development")

    # Firebase / GCP — non-secret identifiers
    FIREBASE_PROJECT_ID: str = os.environ["FIREBASE_PROJECT_ID"]
    FIREBASE_APP_ID: str = os.environ.get("FIREBASE_APP_ID", "")
    FIREBASE_STORAGE_BUCKET: str = os.environ["FIREBASE_STORAGE_BUCKET"]
    # Public-by-design (rendered to the browser) but kept out of git.
    FIREBASE_API_KEY: str = _secret("firebase-api-key", "FIREBASE_API_KEY", required=False)

    # Single authorized user
    AUTHORIZED_USER_EMAIL: str = os.environ["AUTHORIZED_USER_EMAIL"]

    # Session
    SESSION_LIFETIME_HOURS: int = int(os.environ.get("SESSION_LIFETIME_HOURS", "12"))

    # DAV Basic Auth (separate from Firebase Auth)
    DAV_USERNAME: str = os.environ.get("AUTHORIZED_USER_EMAIL", "")
    DAV_PASSWORD_HASH: str = _secret("dav-password-hash", "DAV_PASSWORD_HASH", required=False)

    # App Check (reCAPTCHA Enterprise)
    RECAPTCHA_ENTERPRISE_SITE_KEY: str = os.environ.get("RECAPTCHA_ENTERPRISE_SITE_KEY", "")
    APPCHECK_DEBUG_TOKEN: str = os.environ.get("APPCHECK_DEBUG_TOKEN", "")  # local dev only

    # Cloudflare origin secret (optional): when set, security.py requires the
    # X-Origin-Auth header (injected by a Cloudflare Transform Rule) on every
    # request, defeating direct-to-App-Engine access with a spoofed Host.
    CF_ORIGIN_SECRET: str = _secret("cf-origin-secret", "CF_ORIGIN_SECRET", required=False)

    # Multi-Factor Authentication
    REQUIRE_MFA: bool = os.environ.get("REQUIRE_MFA", "true").lower() == "true"

    # Rate limiting
    RATE_LIMIT_LOGIN: str = os.environ.get("RATE_LIMIT_LOGIN", "5 per minute")

    # MCP connector (Phase I) — kill switch + canonical origin.
    # MCP_CANONICAL_ORIGIN is the OAuth issuer and the base of the RFC 8707
    # resource identifier; it must never be derived from request.host
    # (Host-header trust). Override locally for MCP Inspector testing.
    MCP_ENABLED: bool = os.environ.get("MCP_ENABLED", "true").lower() == "true"
    # Second, narrower kill switch: turns the two note-write tools off
    # (they vanish from tools/list and are refused at tools/call) without
    # taking the read-only connector down with them.
    MCP_WRITE_ENABLED: bool = (
        os.environ.get("MCP_WRITE_ENABLED", "true").lower() == "true"
    )
    MCP_CANONICAL_ORIGIN: str = os.environ.get(
        "MCP_CANONICAL_ORIGIN", "https://athena.poirierlavoie.ca"
    ).rstrip("/")

    # Request size limits
    MAX_CONTENT_LENGTH: int = 25 * 1024 * 1024  # 25 MB (document uploads)

    # Microsoft Graph — outbound email (portail client L1; reused by the
    # future phase J notification pipeline). All optional: unset means the
    # outbound-email feature is disabled and callers degrade with a French
    # message (never a crash). MAIN SERVICE ONLY — none of these values may
    # appear in the portal service's environment (spec L1 §8.1).
    GRAPH_TENANT_ID: str = os.environ.get("GRAPH_TENANT_ID", "")
    GRAPH_CLIENT_ID: str = os.environ.get("GRAPH_CLIENT_ID", "")
    GRAPH_SENDER_UPN: str = os.environ.get("GRAPH_SENDER_UPN", "")
    # Nom d'affichage de l'expéditeur (le « name » du from Graph). Sans lui,
    # les clients voient le nom d'annuaire de la boîte dont reception@ est
    # l'alias — « Jason Poirier Lavoie » — plutôt que la réception du cabinet.
    # Vide = comportement historique (aucun name dans la charge sendMail).
    # NB : Exchange Online peut réécrire le nom d'affichage d'un expéditeur de
    # l'organisation vers sa valeur d'annuaire ; si ce réglage ne tient pas à
    # l'essai réel, la solution durable est de convertir reception@ en boîte
    # partagée portant son propre nom (opération Exchange, pas code).
    GRAPH_SENDER_NAME: str = os.environ.get("GRAPH_SENDER_NAME", "")
    GRAPH_CLIENT_SECRET: str = _secret(
        "graph-client-secret", "GRAPH_CLIENT_SECRET", required=False
    )

    @classmethod
    def graph_configured(cls) -> bool:
        """True when every Graph credential needed to send email is present."""
        return bool(
            cls.GRAPH_TENANT_ID
            and cls.GRAPH_CLIENT_ID
            and cls.GRAPH_CLIENT_SECRET
            and cls.GRAPH_SENDER_UPN
        )

    # Bookings sync (phase L2). Imports « Bookings with me » reservations as
    # hearings gated behind confirmation="à_confirmer". Reuses the L1 Graph
    # app registration (adds the Calendars.ReadWrite application permission —
    # a manual Entra step). MAIN SERVICE ONLY.
    BOOKINGS_SYNC_ACTIVE: bool = (
        os.environ.get("BOOKINGS_SYNC_ACTIVE", "true").lower() == "true"
    )
    # Subject-keyword predicate: a Bookings meeting type is detected when the
    # subject ENDS with « {séparateur} {keyword} » (case- and accent-folded;
    # comma-separated env). « Bookings with me » names the event
    # « {Customer} - {Service} », so the service name is a SUFFIX — and the
    # anchor is what keeps « Réunion d'équipe » out (see graph_calendrier
    # .mot_cle_correspondant: the organizer is the juriste for a self-created
    # event too, so the keyword is the ONLY discriminant).
    BOOKINGS_SUBJECT_KEYWORDS: tuple[str, ...] = tuple(
        k.strip()
        for k in os.environ.get(
            "BOOKINGS_SUBJECT_KEYWORDS", "Consultation"
        ).split(",")
        if k.strip()
    )
    # Keyword → hearing_type. Keys are accent/case-folded (the lookup folds the
    # detected keyword the same way). Both values belong to the EXISTING
    # extrajudiciaire vocabulary of models/hearing.py, so forum_of keeps
    # deriving correctly and the Calendar already has a label and a colour for
    # them. A keyword with no entry falls back to BOOKINGS_TYPE_DEFAUT and is
    # LOGGED — adding a service in app.yaml without mapping it here must not
    # be silent.
    BOOKINGS_TYPE_PAR_MOT_CLE: dict[str, str] = {
        "consultation": "consultation",
        "reunion": "rencontre",
    }
    BOOKINGS_TYPE_DEFAUT: str = "consultation"
    BOOKINGS_SYNC_LOOKAHEAD_DAYS: int = int(
        os.environ.get("BOOKINGS_SYNC_LOOKAHEAD_DAYS", "90")
    )
    BOOKINGS_SYNC_LOOKBACK_DAYS: int = int(
        os.environ.get("BOOKINGS_SYNC_LOOKBACK_DAYS", "1")
    )
    # The mailbox whose calendar is queried (the juriste's Bookings mailbox).
    # Empty → the sync short-circuits (nothing to query).
    BOOKINGS_JURISTE_UPN: str = os.environ.get("BOOKINGS_JURISTE_UPN", "")
    # §4.4 predicate tuning: log the raw JSON of the first detected + first
    # undetected event at DEBUG (domains only — never full addresses).
    BOOKINGS_DEBUG_PAYLOAD: bool = (
        os.environ.get("BOOKINGS_DEBUG_PAYLOAD", "false").lower() == "true"
    )

    @classmethod
    def bookings_configured(cls) -> bool:
        """True when Bookings sync can run (Graph creds + a mailbox to poll)."""
        return bool(cls.graph_configured() and cls.BOOKINGS_JURISTE_UPN)

    # Intake trigger (phase L3). False in L2: the confirmation screen shows the
    # « envoyer le formulaire d'ouverture » checkbox DISABLED with a tooltip,
    # and the confirm route never emits an intake invitation.
    FEATURE_INTAKE: bool = (
        os.environ.get("FEATURE_INTAKE", "false").lower() == "true"
    )

    # Firm info (displayed on invoices)
    FIRM_NAME: str = os.environ.get("FIRM_NAME", "")
    FIRM_STREET: str = os.environ.get("FIRM_STREET", "")
    FIRM_UNIT: str = os.environ.get("FIRM_UNIT", "")
    FIRM_CITY: str = os.environ.get("FIRM_CITY", "")
    FIRM_PROVINCE: str = os.environ.get("FIRM_PROVINCE", "QC")
    FIRM_POSTAL_CODE: str = os.environ.get("FIRM_POSTAL_CODE", "")
    FIRM_PHONE: str = os.environ.get("FIRM_PHONE", "")
    FIRM_EMAIL: str = os.environ.get("FIRM_EMAIL", "")
    GST_NUMBER: str = os.environ.get("GST_NUMBER", "")
    QST_NUMBER: str = os.environ.get("QST_NUMBER", "")
