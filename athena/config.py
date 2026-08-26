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
        # Le service Bookings « Rencontre » (renommé de « Réunion » le
        # 2026-07-30, bascule nette — aucun rendez-vous ne portait l'ancien
        # nom). Le mot-clé plié coïncide avec le type d'audience : le libellé
        # Bookings et le type Athéna disent le même mot.
        "rencontre": "rencontre",
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

    # Miroir Outlook (2026-07-29). Pushes confirmed Athéna hearings into the
    # juriste's DEFAULT Outlook calendar every 10 min (they then count toward
    # Exchange free/busy, so « Bookings with me » stops offering slots over a
    # court date). One-way, Firestore-read-only; loop-proof against the
    # Bookings sync via the extended-property marker (utils/graph_calendrier).
    # Same mailbox and credentials as the Bookings sync (bookings_configured).
    MIROIR_OUTLOOK_ACTIF: bool = (
        os.environ.get("MIROIR_OUTLOOK_ACTIF", "true").lower() == "true"
    )
    # Lookahead 365 (court dates are set well beyond the Bookings sync's 90
    # days; calendarView caps at 1825 days — 4× margin). Lookback 30: cleans
    # up mirrors of hearings deleted/postponed up to a month after the fact;
    # beyond that a past mirror stays in Outlook as history (documented).
    MIROIR_OUTLOOK_LOOKAHEAD_DAYS: int = int(
        os.environ.get("MIROIR_OUTLOOK_LOOKAHEAD_DAYS", "365")
    )
    MIROIR_OUTLOOK_LOOKBACK_DAYS: int = int(
        os.environ.get("MIROIR_OUTLOOK_LOOKBACK_DAYS", "30")
    )

    # Intake trigger (phase L3). False in L2: the confirmation screen shows the
    # « envoyer le formulaire d'ouverture » checkbox DISABLED with a tooltip,
    # and the confirm route never emits an intake invitation.
    FEATURE_INTAKE: bool = (
        os.environ.get("FEATURE_INTAKE", "false").lower() == "true"
    )

    # ── Chat IA (Phase N) — Claude on Vertex AI ─────────────────────────────
    # The turn worker runs on the dedicated « chat » App Engine service
    # (chat.yaml, default service account — roles/aiplatform.user is its ONE
    # extra grant); the UI lives on default. Auth is ADC — no API key exists
    # anywhere. Location is GLOBAL — verified live 2026-08-26: the spec's
    # multi-region « us » answers 501 UNIMPLEMENTED for these models and
    # us-east5 404s them; global is the ONLY serving location, and it bills
    # at the BASE rate (accepted residency trade-off, user decision — global
    # routes worldwide). Host/location stay env-overridable so a regional
    # endpoint, if these models ever gain one, is a config edit.
    CHAT_VERTEX_HOST: str = os.environ.get(
        "CHAT_VERTEX_HOST", "aiplatform.googleapis.com"
    )
    CHAT_VERTEX_LOCATION: str = os.environ.get("CHAT_VERTEX_LOCATION", "global")

    # CLOSED model allowlist (SPEC_PHASE_N_CHAT.md §9). Covered Models
    # (Fable-class, Mythos-class) are EXCLUDED: mandatory 30-day
    # prompt/response retention under the Advanced AI Safety Addendum —
    # disqualifying for privileged material. Adding ANY future model requires
    # verifying its retention class FIRST. claude-opus-5: VERIFIED 2026-08-26
    # (Jason, Model Garden) — NOT a Covered Model, zero retention, it stays
    # (user decision D6); the documented fallback is claude-opus-4-8 — swap the
    # entry, never widen the list. vertex_model_id values are overridable by
    # env because Model Garden may require the @-versioned id, recorded at
    # the same ops step.
    #
    # effort / max_tokens sizing (2026-08-26 — the ADAPTIVE-thinking repair).
    # These models take `thinking: {"type": "adaptive"}` and are steered by
    # `output_config.effort`; the fixed-budget form is REMOVED and returns a
    # 400 (see chat/vertex.py's request-surface note). Consequence for
    # sizing: the old « budget < max_tokens » arithmetic no longer bounds
    # anything — an adaptive thinker spends against max_tokens, and EFFORT is
    # the knob. What still holds: one task = one NON-STREAMED Vertex call
    # bounded by CHAT_VERTEX_READ_TIMEOUT_S (540 s), and at a conservative
    # ~40 tok/s max_tokens is the worst-case duration (20 480 ≈ 512 s).
    # Raising max_tokens OR effort is how a turn starts dying on the
    # platform's 10-minute task deadline — recompute before touching either.
    VERTEX_EFFORTS: frozenset = frozenset(
        {"low", "medium", "high", "xhigh", "max"}
    )
    CHAT_MODELS: dict[str, dict] = {
        "claude-sonnet-5": {
            "vertex_model_id": os.environ.get(
                "CHAT_SONNET_VERTEX_ID", "claude-sonnet-5"
            ),
            "label_fr": "Sonnet 5 — quotidien / administration",
            "effort": "high",
            "max_tokens": 16384,
        },
        "claude-opus-5": {
            "vertex_model_id": os.environ.get(
                "CHAT_OPUS_VERTEX_ID", "claude-opus-5"
            ),
            "label_fr": "Opus 5 — recherche / rédaction",
            "effort": "high",
            "max_tokens": 20480,
        },
    }
    CHAT_DEFAULT_MODEL: str = "claude-sonnet-5"

    # Chat-side write kill switch — DISTINCT from MCP_WRITE_ENABLED, which is
    # connector-scoped (its consent-screen semantics do not transfer). False →
    # the write tools leave the chat's tool array; reads unaffected.
    CHAT_WRITE_ENABLED: bool = (
        os.environ.get("CHAT_WRITE_ENABLED", "true").lower() == "true"
    )

    # Turn-chain sizing. CHAT_CHAIN_MAX_CALLS is the hard ceiling of model
    # calls per turn (hitting it finalizes the turn `failed`, loudly).
    # CHAT_TASK_RETRY_TERMINAL is the X-AppEngine-TaskRetryCount at which the
    # worker finalizes `failed` and consumes the task — it must stay BELOW the
    # queue's max-attempts (8) so the terminalization write itself has retries.
    CHAT_CHAIN_MAX_CALLS: int = int(os.environ.get("CHAT_CHAIN_MAX_CALLS", "12"))
    CHAT_TASK_RETRY_TERMINAL: int = int(
        os.environ.get("CHAT_TASK_RETRY_TERMINAL", "5")
    )
    CHAT_VERTEX_CONNECT_TIMEOUT_S: int = 10
    # 540 < gunicorn --timeout 570 (chat.yaml) < the 600 s App Engine deadline
    # for auto-scaled task requests: each guard fires before the next.
    CHAT_VERTEX_READ_TIMEOUT_S: int = int(
        os.environ.get("CHAT_VERTEX_READ_TIMEOUT_S", "540")
    )

    # Firestore 1 MiB/doc guard: a content block whose serialized JSON exceeds
    # the offload threshold is stored VERBATIM in Storage
    # (users/{uid}/chat/{conversation}/{turn}/{uuid}.json) and replaced by a
    # storage_ref pointer; the budget is the belt — at commit, the largest
    # remaining inline blocks offload until the projected doc size fits.
    # Assembly ALWAYS rehydrates from Storage (sha256-verified): thinking
    # signatures and web_search encrypted_content must be replayed byte-exact
    # or Vertex refuses the continuation with a 400.
    CHAT_BLOCK_OFFLOAD_BYTES: int = 100_000
    CHAT_TURN_DOC_BUDGET_BYTES: int = 900_000
    CHAT_MESSAGE_MAX_CHARS: int = 50_000
    CHAT_WEB_SEARCH_MAX_USES: int = int(
        os.environ.get("CHAT_WEB_SEARCH_MAX_USES", "5")
    )

    # Pricing snapshot (SPEC §7) — CONFIG, never code. USD per million tokens
    # at Vertex list prices. multiregion_multiplier is 1.0: the GLOBAL
    # endpoint bills at the base rate — the +10 % premium applies only to
    # regional/multi-region endpoints, which do not serve these models
    # (verified 2026-08-26); restore 1.10 if a regional fallback is ever
    # configured. Sonnet 5 introductory pricing (2 $/10 $) ended 2026-08-31 —
    # this snapshot starts at the standard rate. Opus 5 rates: to confirm at
    # the Model Garden step (seeded at the Opus-4.8 tier).
    # web_search: 10 $ per 1 000 searches, billed via server_tool_use counts.
    # `version` is stamped on every recorded segment so a later rate change
    # never silently re-prices history.
    CHAT_PRICING: dict = {
        "version": "2026-08-26",
        "multiregion_multiplier": 1.0,
        "web_search_usd_per_1000": 10.0,
        "models": {
            "claude-sonnet-5": {
                "input_usd_per_mtok": 3.00,
                "output_usd_per_mtok": 15.00,
                "cache_write_usd_per_mtok": 3.75,
                "cache_read_usd_per_mtok": 0.30,
            },
            "claude-opus-5": {
                "input_usd_per_mtok": 5.00,
                "output_usd_per_mtok": 25.00,
                "cache_write_usd_per_mtok": 6.25,
                "cache_read_usd_per_mtok": 0.50,
            },
        },
    }

    # Legal-research Cloudflare Workers (chat tools legislation_* /
    # jurisprudence_*). Plain server-to-server REST with a bearer token per
    # Worker (user decision D10: two DISTINCT secrets — independent
    # revocation). All optional: an unconfigured Worker's tools are simply
    # absent from the chat's tool array (French degrade via the charter's
    # citation rule — never a crash). These tools NEVER appear on the external
    # MCP connector: claude.ai reaches the Workers directly already.
    LEGISLATION_WORKER_URL: str = os.environ.get("LEGISLATION_WORKER_URL", "")
    JURISPRUDENCE_WORKER_URL: str = os.environ.get("JURISPRUDENCE_WORKER_URL", "")
    LEGISLATION_WORKER_TOKEN: str = _secret(
        "legislation-worker-token", "LEGISLATION_WORKER_TOKEN", required=False
    )
    JURISPRUDENCE_WORKER_TOKEN: str = _secret(
        "jurisprudence-worker-token", "JURISPRUDENCE_WORKER_TOKEN", required=False
    )

    @classmethod
    def worker_configured(cls, worker: str) -> bool:
        """True when the named Worker ("legislation"/"jurisprudence") is callable."""
        if worker == "legislation":
            return bool(cls.LEGISLATION_WORKER_URL and cls.LEGISLATION_WORKER_TOKEN)
        if worker == "jurisprudence":
            return bool(cls.JURISPRUDENCE_WORKER_URL and cls.JURISPRUDENCE_WORKER_TOKEN)
        return False

    # Firm info (displayed on invoices)
    FIRM_NAME: str = os.environ.get("FIRM_NAME", "")
    FIRM_STREET: str = os.environ.get("FIRM_STREET", "")
    FIRM_UNIT: str = os.environ.get("FIRM_UNIT", "")
    FIRM_CITY: str = os.environ.get("FIRM_CITY", "")
    FIRM_PROVINCE: str = os.environ.get("FIRM_PROVINCE", "QC")
    FIRM_POSTAL_CODE: str = os.environ.get("FIRM_POSTAL_CODE", "")
    FIRM_PHONE: str = os.environ.get("FIRM_PHONE", "")
    FIRM_FAX: str = os.environ.get("FIRM_FAX", "")
    FIRM_EMAIL: str = os.environ.get("FIRM_EMAIL", "")
    GST_NUMBER: str = os.environ.get("GST_NUMBER", "")
    QST_NUMBER: str = os.environ.get("QST_NUMBER", "")
