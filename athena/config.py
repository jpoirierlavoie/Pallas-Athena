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
    """Resolve a sensitive value: Secret Manager in prod, env var locally."""
    if _is_production():
        return _from_secret_manager(secret_id)
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

    # Multi-Factor Authentication
    REQUIRE_MFA: bool = os.environ.get("REQUIRE_MFA", "true").lower() == "true"

    # Rate limiting
    RATE_LIMIT_LOGIN: str = os.environ.get("RATE_LIMIT_LOGIN", "5 per minute")

    # Request size limits
    MAX_CONTENT_LENGTH: int = 25 * 1024 * 1024  # 25 MB (document uploads)

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
