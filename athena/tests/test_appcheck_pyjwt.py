"""The App Check verifier, run for real against the pinned pyjwt.

pyjwt is a transitive dependency (firebase-admin → app_check) that the
requirements.in CVE-hygiene block pins explicitly since 2026-09-30. A version
bump changes behaviour these tests observe WITHOUT any repo code change — the
"dependency bumps are a silent trigger" rule of CLAUDE.md — so they drive
`firebase_admin.app_check`'s own `_AppCheckService` with a locally generated
RSA key and a JWKS served through pyjwt's REAL fetch path (only the opener is
replaced), rather than mocking the library they exist to pin.

What is pinned:
- a valid RS256 token verifies;
- a forged token naming an UNKNOWN kid no longer costs one JWKS fetch per
  request (CVE-2026-101917 — pyjwt 2.14.0 added the refetch cooldown);
- an HS256 token signed with the public key is refused (the algorithm-
  confusion class of the 2.13.0 advisories);
- and the two services' failure log lines never carry the token's kid, which
  pyjwt formats into its exception message.
"""

import io
import json
import logging
import os
import pathlib
import sys
import time
import types

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

import jwt  # noqa: E402
import jwt.jwks_client  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from firebase_admin import app_check  # noqa: E402
from flask import Flask  # noqa: E402

PROJECT = "test-project"
ISSUER = "https://firebaseappcheck.googleapis.com/123456"
KID = "cle-1"
FORGED_KID = "kid-choisi-par-l-attaquant"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_server(rsa_key, monkeypatch):
    """pyjwt's real `fetch_data`, with only the HTTP opener replaced.

    Returns the list of fetched URLs so a test can count the round trips a
    request cost.
    """
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    body = json.dumps({"keys": [public_jwk]}).encode("utf-8")
    fetched: list[str] = []

    def _serve(req, *args, **kwargs):
        fetched.append(req.full_url)
        return io.BytesIO(body)

    class _Opener:
        def open(self, req, *args, **kwargs):
            return _serve(req)

    # Both transports pyjwt has used: 2.14+ builds an opener (to refuse
    # redirects — CVE-2026-102267), 2.13 called urlopen. Patching both keeps
    # a future bump from reaching the network, and lets an OLD pin fail on
    # the cooldown assertions alone.
    request_module = jwt.jwks_client.urllib.request
    monkeypatch.setattr(request_module, "build_opener", lambda *handlers: _Opener())
    monkeypatch.setattr(request_module, "urlopen", _serve)
    return fetched


@pytest.fixture
def service(jwks_server):
    # A fresh service per test: the JWKS cache and the cooldown clock live on
    # its PyJWKClient.
    return app_check._AppCheckService(types.SimpleNamespace(project_id=PROJECT))


def _claims() -> dict:
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": ["projects/123456", f"projects/{PROJECT}"],
        "sub": "1:123456:web:abcdef",
        "iat": now,
        "exp": now + 300,
    }


def _rs256(rsa_key, kid: str = KID) -> str:
    return jwt.encode(
        _claims(), rsa_key, algorithm="RS256", headers={"kid": kid, "typ": "JWT"}
    )


def test_the_pin_carries_the_refetch_cooldown():
    major, minor = (int(x) for x in jwt.__version__.split(".")[:2])
    assert (major, minor) >= (2, 14), (
        "pyjwt < 2.14 re-fetches the JWKS for every unknown kid "
        "(CVE-2026-101917) — see requirements.in"
    )


def test_a_valid_rs256_token_verifies(service, rsa_key):
    claims = service.verify_token(_rs256(rsa_key))
    assert claims["app_id"] == "1:123456:web:abcdef"


def test_unknown_kids_no_longer_cost_one_fetch_each(service, rsa_key, jwks_server):
    # The first verification fills the cache (one fetch).
    service.verify_token(_rs256(rsa_key))
    assert len(jwks_server) == 1

    # Twenty forged tokens, twenty distinct unknown kids — under 2.13.0 this
    # was twenty more outbound HTTPS fetches. Each is still refused.
    for i in range(20):
        with pytest.raises(Exception):
            service.verify_token(_rs256(rsa_key, kid=f"{FORGED_KID}-{i}"))
    assert len(jwks_server) <= 2, (
        f"{len(jwks_server)} JWKS fetches for 20 forged kids — the cooldown "
        "is gone"
    )

    # And the real key keeps verifying from the cache, with no fetch.
    before = len(jwks_server)
    service.verify_token(_rs256(rsa_key))
    assert len(jwks_server) == before


def test_an_hs256_token_signed_with_the_public_key_is_refused(service, rsa_key):
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    # Signed by hand: pyjwt refuses to USE a PEM as an HMAC secret, which is
    # half of the guarantee; the verifier must refuse the result too.
    import base64
    import hashlib
    import hmac

    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _b64(json.dumps({"alg": "HS256", "kid": KID, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    forged = f"{header}.{payload}.{signature}"

    with pytest.raises(ValueError):
        service.verify_token(forged)


def test_a_wrong_audience_is_a_value_error_raised_from_the_pyjwt_class(
        service, rsa_key):
    """What the failure log's `cause_type` relies on: firebase-admin wraps
    the rejection in a bare ValueError raised FROM the pyjwt error, whose
    CLASS says which check failed (here, a misconfigured project)."""
    claims = {**_claims(), "aud": ["projects/autre-projet"]}
    token = jwt.encode(claims, rsa_key, algorithm="RS256",
                       headers={"kid": KID, "typ": "JWT"})
    with pytest.raises(ValueError) as excinfo:
        service.verify_token(token)
    assert isinstance(excinfo.value.__cause__, jwt.InvalidAudienceError)


# ── The failure log lines ────────────────────────────────────────────────


def _raise_with_kid(token):
    raise jwt.PyJWKClientError(
        f'Unable to find a signing key that matches: "{FORGED_KID}"'
    )


def _raise_wrapped_audience(token):
    try:
        raise jwt.InvalidAudienceError(f"Audience doesn't match {FORGED_KID}")
    except jwt.InvalidAudienceError as inner:
        raise ValueError(f"The provided App Check token has incorrect "
                         f"\"aud\" (audience) claim: {FORGED_KID}") from inner


def _logged_text(caplog) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(getattr(record, "json_fields", "")))
    return "\n".join(parts)


def test_the_main_service_never_logs_the_forged_kid(monkeypatch, caplog):
    import security

    monkeypatch.setattr(app_check, "verify_token", _raise_with_kid)
    app = Flask(__name__)
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = "site-key"
    app.before_request(security._verify_app_check)

    @app.route("/partiel")
    def partiel():
        return "ok"

    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get(
            "/partiel",
            headers={"HX-Request": "true", "X-Firebase-AppCheck": "jeton"},
        )
    assert resp.status_code == 401
    text = _logged_text(caplog)
    assert "appcheck_failure" in text
    assert "PyJWKClientError" in text
    assert FORGED_KID not in text


@pytest.mark.parametrize("which", ["main", "portal"])
def test_the_failure_line_names_the_wrapped_class_never_its_text(
        monkeypatch, caplog, which):
    """firebase-admin raises a bare ValueError for nearly every rejection:
    without the wrapped pyjwt CLASS, an expired token, a wrong audience (a
    misconfigured project) and a bad signature would all read « ValueError »."""
    monkeypatch.setattr(app_check, "verify_token", _raise_wrapped_audience)
    app = Flask(__name__)
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = "site-key"
    if which == "main":
        import security

        app.before_request(security._verify_app_check)
        path, method, headers = "/partiel", "get", {"HX-Request": "true"}
    else:
        from client import security as portail_security

        app.before_request(portail_security.verify_app_check)
        path, method, headers = "/api/renvoi", "post", {}

    @app.route(path, methods=["GET", "POST"])
    def target():
        return "ok"

    with caplog.at_level(logging.WARNING):
        resp = getattr(app.test_client(), method)(
            path, headers={**headers, "X-Firebase-AppCheck": "jeton"})
    assert resp.status_code == 401
    (fields,) = [r.json_fields for r in caplog.records
                 if getattr(r, "json_fields", {}).get("event") == "appcheck_failure"]
    assert fields["error_type"] == "ValueError"
    assert fields["cause_type"] == "InvalidAudienceError"
    assert FORGED_KID not in _logged_text(caplog)


def test_the_portal_never_logs_the_forged_kid(monkeypatch, caplog):
    from client import security as portail_security

    monkeypatch.setattr(app_check, "verify_token", _raise_with_kid)
    app = Flask(__name__)
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = "site-key"
    app.before_request(portail_security.verify_app_check)

    @app.route("/api/renvoi", methods=["POST"])
    def renvoi():
        return "ok"

    with caplog.at_level(logging.WARNING):
        resp = app.test_client().post(
            "/api/renvoi", headers={"X-Firebase-AppCheck": "jeton"}
        )
    assert resp.status_code == 401
    text = _logged_text(caplog)
    assert "appcheck_failure" in text
    assert "PyJWKClientError" in text
    assert FORGED_KID not in text
