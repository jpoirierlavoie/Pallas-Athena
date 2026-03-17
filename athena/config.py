"""Environment-based configuration for Pallas Athena."""

import os


class Config:
    """Base configuration loaded from environment variables."""

    # Flask
    SECRET_KEY: str = os.environ["SECRET_KEY"]
    ENV: str = os.environ.get("ENV", "development")

    # Firebase / GCP
    FIREBASE_PROJECT_ID: str = os.environ["FIREBASE_PROJECT_ID"]
    FIREBASE_API_KEY: str = os.environ.get("FIREBASE_API_KEY", "")
    FIREBASE_APP_ID: str = os.environ.get("FIREBASE_APP_ID", "")
    FIREBASE_STORAGE_BUCKET: str = os.environ["FIREBASE_STORAGE_BUCKET"]

    # Single authorized user
    AUTHORIZED_USER_EMAIL: str = os.environ["AUTHORIZED_USER_EMAIL"]

    # Session
    SESSION_LIFETIME_HOURS: int = int(os.environ.get("SESSION_LIFETIME_HOURS", "12"))

    # DAV Basic Auth (separate from Firebase Auth)
    DAV_USERNAME: str = os.environ.get("AUTHORIZED_USER_EMAIL", "")
    DAV_PASSWORD_HASH: str = os.environ.get("DAV_PASSWORD_HASH", "")

    # App Check (reCAPTCHA Enterprise)
    APPCHECK_ENABLED: bool = os.environ.get("APPCHECK_ENABLED", "false").lower() == "true"
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
