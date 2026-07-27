"""Portal-service configuration — constants (Annexe C) + lazy secrets.

The constants are plain module-level values importable by BOTH services: the
main service's invitation model/emission need PORTAIL_DB, PORTAIL_HOST and
the quotas, and the portal's own factory reads everything. Secrets are
resolved by FUNCTIONS called from the portal app factory only — never at
import time — so the main service can import this module without holding
accessor rights on the portal's secrets (and vice versa: no GRAPH_* value
exists here, spec L1 §8.1).

The _secret helpers mirror config.py's resolution policy (Secret Manager in
production, env vars locally) — duplicated rather than imported because
importing config.py resolves the MAIN service's required secrets at import
time, which the portal's service account must not need.
"""

import os
from functools import lru_cache

# ── Annexe C ─────────────────────────────────────────────────────────────

PORTAIL_HOST = os.environ.get("PORTAIL_HOST", "portail.poirierlavoie.ca")
_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
PORTAIL_BUCKET = os.environ.get(
    "PORTAIL_BUCKET", f"{_PROJECT_ID}-portail-quarantaine"
)
PORTAIL_DB = "portail"          # named Firestore database (NOT the default)
PORTAIL_QUEUE = "portail"       # Cloud Tasks queue name
# Must equal the App Engine application region (verify with
# `gcloud app describe` before creating the queue — spec L1 §8.2).
TASKS_LOCATION = os.environ.get("TASKS_LOCATION", "northamerica-northeast1")
PORTAIL_SESSION_HOURS = 24
PORTAIL_MAX_FILE_MB = 200
PORTAIL_MAX_FILES = 50
PORTAIL_MAX_TOTAL_MB = 1024
INVITATION_DOCUMENTS_JOURS = 14
INVITATION_INTAKE_JOURS = 14    # réservé à la phase L3
CHUNK_MIB = 8                   # multiple of 256 KiB (GCS resumable protocol)

# Inert-handling whitelist (spec §7.5): notably NO svg/html/htm/js, and no
# archives (zip refused in v1 — décision D-4).
PORTAIL_EXTENSIONS = {
    "pdf", "jpg", "jpeg", "png", "heic", "heif", "tif", "tiff",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "eml", "msg",
    "mp3", "m4a", "wav", "mp4", "mov",
}

# ── Lazy secret resolution (mirror of config.py's policy) ────────────────


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


def portail_secret_key() -> str:
    """The portal Flask session key — DISTINCT from the main service's.

    Key separation is the real session boundary: a portal cookie can never
    validate against the main app's signature check, nor the reverse.
    Required — the portal cannot boot without it.
    """
    return _secret("portail-secret-key", "PORTAIL_SECRET_KEY")


def firebase_api_key() -> str:
    """Web API key for the sign-in page (public-by-design, kept out of git)."""
    return _secret("firebase-api-key", "FIREBASE_API_KEY", required=False)
