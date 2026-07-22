"""Tests for the embedded OAuth 2.1 authorization server and bearer auth."""

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask

with mock.patch("google.cloud.firestore.Client"):
    import mcp as mcp_pkg
    import mcp.bearer as bearer
    import mcp.store as store

UTC = timezone.utc
ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
ORIGIN = "https://athena.poirierlavoie.ca"


def _make_app(**config) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(ATHENA_DIR, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["MCP_ENABLED"] = True
    app.config["MCP_CANONICAL_ORIGIN"] = ORIGIN
    app.config["ENV"] = "development"
    app.config["RATELIMIT_ENABLED"] = False
    app.config.update(config)

    from routes.auth_routes import auth_bp
    from security import csrf, limiter

    csrf.init_app(app)
    limiter.init_app(app)
    app.register_blueprint(auth_bp)
    mcp_pkg.register_mcp(app)
    csrf.exempt(mcp_pkg.mcp_bp)
    return app


# ── In-memory fake of the store boundary (protocol tests) ───────────────

class FakeStore:
    """Dict-backed mirror of mcp.store semantics — no Firestore in CI."""

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}

    # clients
    def create_client(self, client_name, redirect_uris):
        client_id = secrets.token_urlsafe(24)
        doc = {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "last_used_at": None,
        }
        self.clients[client_id] = doc
        return doc

    def get_client(self, client_id):
        return self.clients.get(client_id)

    def touch_client(self, client_id):
        if client_id in self.clients:
            self.clients[client_id]["last_used_at"] = datetime.now(UTC)

    # codes
    def create_auth_code(self, client_id, redirect_uri, scope, code_challenge, resource):
        code = secrets.token_urlsafe(32)
        self.codes[store.sha256_hex(code)] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": resource,
            "used": False,
            "family_id": None,
            "expire_at": datetime.now(UTC) + timedelta(seconds=300),
        }
        return code

    def get_auth_code(self, code_hash):
        doc = self.codes.get(code_hash)
        return dict(doc) if doc else None

    def consume_auth_code(self, code_hash, family_id):
        doc = self.codes.get(code_hash)
        if doc is None:
            return None, False
        if doc["used"]:
            return dict(doc), True
        doc["used"] = True
        doc["family_id"] = family_id
        return dict(doc), False

    # tokens
    def create_token_pair(self, client_id, scope, resource, family_id=None):
        family = family_id or uuid.uuid4().hex
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        base = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "family_id": family,
            "revoked": False,
            "rotated_to": None,
            "last_used_at": None,
        }
        self.tokens[store.sha256_hex(access)] = {
            **base,
            "token_type": "access",
            "expire_at": now + timedelta(seconds=3600),
        }
        self.tokens[store.sha256_hex(refresh)] = {
            **base,
            "token_type": "refresh",
            "expire_at": now + timedelta(days=30),
        }
        return {
            "access_token": access,
            "refresh_token": refresh,
            "access_token_hash": store.sha256_hex(access),
            "refresh_token_hash": store.sha256_hex(refresh),
            "family_id": family,
            "scope": scope,
            "expires_in": 3600,
        }

    def get_token(self, token_hash):
        doc = self.tokens.get(token_hash)
        return dict(doc) if doc else None

    def rotate_refresh_token(self, token_hash):
        doc = self.tokens.get(token_hash)
        if doc is None or doc["token_type"] != "refresh":
            return None, "not_found"
        if doc["revoked"]:
            return None, "replayed"
        doc["revoked"] = True
        pair = self.create_token_pair(
            doc["client_id"], doc["scope"], doc["resource"],
            family_id=doc["family_id"],
        )
        doc["rotated_to"] = pair["refresh_token_hash"]
        return pair, ""

    def revoke_token_hash(self, token_hash):
        doc = self.tokens.get(token_hash)
        if doc is None or doc["revoked"]:
            return False
        doc["revoked"] = True
        return True

    def revoke_family(self, family_id):
        count = 0
        for doc in self.tokens.values():
            if doc["family_id"] == family_id and not doc["revoked"]:
                doc["revoked"] = True
                count += 1
        return count

    def stamp_token_last_used(self, token_hash):
        pass


_PATCHED_FUNCS = (
    "create_client",
    "get_client",
    "touch_client",
    "create_auth_code",
    "get_auth_code",
    "consume_auth_code",
    "create_token_pair",
    "get_token",
    "rotate_refresh_token",
    "revoke_token_hash",
    "revoke_family",
    "stamp_token_last_used",
)


@pytest.fixture()
def fake(monkeypatch):
    bearer.reset_brake_state()
    fake_store = FakeStore()
    for name in _PATCHED_FUNCS:
        monkeypatch.setattr(store, name, getattr(fake_store, name))
    yield fake_store
    bearer.reset_brake_state()


@pytest.fixture()
def app(fake):
    return _make_app()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "test-user"
        sess["expires_at"] = datetime.now(UTC) + timedelta(hours=1)


def _register_client(fake) -> dict:
    return fake.create_client("Claude", [CLAUDE_CALLBACK])


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize_params(client_doc, challenge, **overrides) -> dict:
    params = {
        "response_type": "code",
        "client_id": client_doc["client_id"],
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "athena:read",
        "state": "xyz123",
    }
    params.update(overrides)
    return params


def _consent_allow(client, fake, client_doc, challenge) -> str:
    """Run the consent flow (GET → POST allow) and return the auth code."""
    _login(client)
    page = client.get("/oauth/authorize", query_string=_authorize_params(client_doc, challenge))
    assert page.status_code == 200
    token_match = re.search(
        rb'name="csrf_token" value="([^"]+)"', page.data
    )
    assert token_match, "consent page must embed a CSRF token"
    form = _authorize_params(client_doc, challenge)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "allow"
    resp = client.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK)
    code_match = re.search(r"[?&]code=([^&]+)", location)
    assert code_match and "state=xyz123" in location
    return code_match.group(1)


def _consent_form(client, client_doc, challenge, **extra):
    """Return a ready-to-POST consent form (CSRF harvested from the page)."""
    _login(client)
    page = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(client_doc, challenge, **extra),
    )
    assert page.status_code == 200
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token_match
    form = _authorize_params(client_doc, challenge, **extra)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "allow"
    return form, page


def _code_from(resp):
    assert resp.status_code == 302
    match = re.search(r"[?&]code=([^&]+)", resp.headers["Location"])
    assert match
    return match.group(1)


def _exchange(client, client_doc, code, verifier, **overrides):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": client_doc["client_id"],
        "code_verifier": verifier,
    }
    form.update(overrides)
    return client.post("/oauth/token", data=form)


# ── Metadata documents ──────────────────────────────────────────────────

def test_authorization_server_metadata(client):
    doc = client.get("/.well-known/oauth-authorization-server").get_json()
    assert doc["issuer"] == ORIGIN
    assert doc["authorization_endpoint"] == f"{ORIGIN}/oauth/authorize"
    assert doc["token_endpoint"] == f"{ORIGIN}/oauth/token"
    assert doc["registration_endpoint"] == f"{ORIGIN}/oauth/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["scopes_supported"] == ["athena:read", "athena:write"]


def test_protected_resource_metadata_both_paths(client):
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        doc = client.get(path).get_json()
        assert doc["resource"] == f"{ORIGIN}/mcp"
        assert doc["authorization_servers"] == [ORIGIN]
        assert doc["bearer_methods_supported"] == ["header"]


# ── Dynamic Client Registration ─────────────────────────────────────────

def test_register_happy_path(client, fake):
    resp = client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK]},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["client_id"] in fake.clients
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [CLAUDE_CALLBACK]


def test_register_rejects_non_allowlisted_redirect(client):
    resp = client.post(
        "/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_redirect_uri"


def test_register_localhost_only_outside_production(fake):
    dev = _make_app(ENV="development").test_client()
    assert (
        dev.post(
            "/oauth/register", json={"redirect_uris": ["http://localhost:6274/cb"]}
        ).status_code
        == 201
    )
    prod = _make_app(ENV="production").test_client()
    assert (
        prod.post(
            "/oauth/register", json={"redirect_uris": ["http://localhost:6274/cb"]}
        ).status_code
        == 400
    )


def test_register_sanitizes_client_name(client, fake):
    resp = client.post(
        "/oauth/register",
        json={
            "client_name": "<script>alert(1)</script>Claude",
            "redirect_uris": [CLAUDE_CALLBACK],
        },
    )
    assert resp.get_json()["client_name"] == "alert(1)Claude"


# ── Authorize (consent) ─────────────────────────────────────────────────

def test_authorize_unauthenticated_redirects_to_login_with_full_query(client, fake):
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    # The OAuth query string must survive the login round-trip.
    assert "code_challenge" in resp.headers["Location"]


def test_authorize_unknown_client_renders_error_page(client, fake):
    _login(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        query_string={
            "response_type": "code",
            "client_id": "nope",
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert "Client OAuth inconnu".encode() in resp.data


def test_authorize_mismatched_redirect_renders_error_page(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(
            client_doc, challenge, redirect_uri="https://evil.example/cb"
        ),
    )
    assert resp.status_code == 400
    assert "redirection".encode() in resp.data.lower()


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        # athena:write is a SUPPORTED scope now, so it is no longer invalid to
        # request — it is simply never granted without the consent checkbox
        # (see the grant-rule tests below). A wholly unknown scope still is.
        ({"scope": "athena:admin"}, "invalid_scope"),
        ({"resource": "https://evil.example/mcp"}, "invalid_target"),
    ],
)
def test_authorize_post_validation_errors_redirect_with_state(
    client, fake, override, expected_error
):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(client_doc, challenge, **override),
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK)
    assert f"error={expected_error}" in location
    assert "state=xyz123" in location


def test_authorize_decision_requires_csrf(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    form = _authorize_params(client_doc, challenge)
    form["decision"] = "allow"
    resp = client.post("/oauth/authorize", data=form)  # no csrf_token
    assert resp.status_code == 400
    assert not fake.codes  # no code was issued


def test_authorize_deny_redirects_access_denied(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    form = _authorize_params(client_doc, challenge)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "deny"
    resp = client.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    assert "error=access_denied" in resp.headers["Location"]
    assert "state=xyz123" in resp.headers["Location"]


def test_consent_page_shows_client_name(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    assert "Claude".encode() in page.data
    assert "Autoriser".encode() in page.data
    assert "Refuser".encode() in page.data


# ── Write grant: the consent checkbox is the ONLY path ──────────────────

def test_consent_page_discloses_write_and_no_longer_claims_read_only(client, fake):
    """The screen is the only human-readable description of what the token
    can do; nothing else couples it to the tool registry."""
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    body = page.data.decode("utf-8")
    # The old copy was a flat misrepresentation once write tools exist.
    assert "Aucune modification de vos données n'est possible" not in body
    assert "demande un accès <strong>en lecture seule</strong>" not in body
    # The grant control and what it grants must both be named.
    assert 'name="grant_write"' in body
    assert "Autoriser l'écriture des notes" in body
    assert "créer une nouvelle note dans un dossier" in body
    assert "ajouter du texte à la fin d'une note existante" in body
    # Default state is unchecked — least privilege.
    checkbox = re.search(r'<input type="checkbox" name="grant_write"[^>]*>', body)
    assert checkbox and "checked" not in checkbox.group(0)


def test_unticked_checkbox_grants_read_only(client, fake):
    """Even when the CLIENT requests write. The hidden scope field is
    attacker-modifiable and must never be able to escalate on its own."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    form, _ = _consent_form(
        client, client_doc, challenge, scope="athena:read athena:write"
    )
    assert "grant_write" not in form
    code = _code_from(client.post("/oauth/authorize", data=form))
    stored = fake.codes[store.sha256_hex(code)]
    assert stored["scope"] == "athena:read"


def test_ticked_checkbox_grants_read_and_write(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge)
    form["grant_write"] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    assert fake.codes[store.sha256_hex(code)]["scope"] == "athena:read athena:write"
    # And the granted scope must reach the token response (RFC 6749 §3.3:
    # the AS may grant a scope other than requested, but MUST echo it).
    body = _exchange(client, client_doc, code, verifier).get_json()
    assert body["scope"] == "athena:read athena:write"


def test_write_only_request_still_yields_a_usable_read_scope(client, fake):
    """bearer.py demands athena:read on EVERY /mcp call — a write-only grant
    would be a permanently dead connector that 403s even on initialize."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge, scope="athena:write")
    form["grant_write"] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    assert fake.codes[store.sha256_hex(code)]["scope"].split() == [
        "athena:read", "athena:write"
    ]


def test_write_kill_switch_removes_the_checkbox_and_refuses_the_grant(fake):
    """MCP_WRITE_ENABLED=false must not merely hide the control."""
    app = _make_app(MCP_WRITE_ENABLED=False)
    client = app.test_client()
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    assert 'name="grant_write"' not in page.data.decode("utf-8")
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    form = _authorize_params(client_doc, challenge)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "allow"
    form["grant_write"] = "on"  # forged past the missing control
    code = _code_from(client.post("/oauth/authorize", data=form))
    assert fake.codes[store.sha256_hex(code)]["scope"] == "athena:read"


def test_refresh_rotation_never_widens_the_scope(client, fake):
    """A read-only family must never rotate itself into write."""
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge)
    code = _code_from(client.post("/oauth/authorize", data=form))
    first = _exchange(client, client_doc, code, verifier).get_json()
    assert first["scope"] == "athena:read"
    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_doc["client_id"],
        },
    ).get_json()
    assert rotated["scope"] == "athena:read"
    # Assert the STORED doc too: bearer.py reads that, not the echo.
    assert fake.tokens[store.sha256_hex(rotated["access_token"])]["scope"] == (
        "athena:read"
    )


def test_refresh_rotation_preserves_the_write_grant(client, fake):
    """The direction that silently breaks the feature: if rotation narrowed
    the scope, note writing would work for 60 minutes and then the tools
    would vanish from tools/list mid-conversation, with no error."""
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge)
    form["grant_write"] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    first = _exchange(client, client_doc, code, verifier).get_json()
    assert first["scope"] == "athena:read athena:write"
    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_doc["client_id"],
        },
    ).get_json()
    assert rotated["scope"] == "athena:read athena:write"
    assert fake.tokens[store.sha256_hex(rotated["access_token"])]["scope"] == (
        "athena:read athena:write"
    )


# ── Token endpoint ──────────────────────────────────────────────────────

def test_full_pkce_round_trip(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)

    resp = _exchange(client, client_doc, code, verifier)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "athena:read"
    assert store.sha256_hex(body["access_token"]) in fake.tokens
    assert store.sha256_hex(body["refresh_token"]) in fake.tokens
    # The code is burned.
    code_doc = fake.codes[store.sha256_hex(code)]
    assert code_doc["used"] is True


def test_wrong_verifier_rejected(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    other_verifier, _ = _pkce_pair()
    resp = _exchange(client, client_doc, code, other_verifier)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_grant"


def test_expired_code_rejected(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    fake.codes[store.sha256_hex(code)]["expire_at"] = datetime.now(UTC) - timedelta(
        seconds=1
    )
    resp = _exchange(client, client_doc, code, verifier)
    assert resp.get_json()["error"] == "invalid_grant"


def test_redirect_uri_mismatch_rejected(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    resp = _exchange(
        client, client_doc, code, verifier,
        redirect_uri="https://claude.com/api/mcp/auth_callback",
    )
    assert resp.get_json()["error"] == "invalid_grant"


def test_reused_code_revokes_family(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)

    first = _exchange(client, client_doc, code, verifier)
    assert first.status_code == 200
    tokens = first.get_json()

    replay = _exchange(client, client_doc, code, verifier)
    assert replay.get_json()["error"] == "invalid_grant"
    # Every token minted from the replayed code is dead.
    assert fake.tokens[store.sha256_hex(tokens["access_token"])]["revoked"]
    assert fake.tokens[store.sha256_hex(tokens["refresh_token"])]["revoked"]


def test_refresh_rotation_issues_new_pair_and_revokes_old(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    tokens = _exchange(client, client_doc, code, verifier).get_json()

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_doc["client_id"],
        },
    )
    assert resp.status_code == 200
    new_tokens = resp.get_json()
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["scope"] == "athena:read"

    old_hash = store.sha256_hex(tokens["refresh_token"])
    assert fake.tokens[old_hash]["revoked"] is True
    assert fake.tokens[old_hash]["rotated_to"] == store.sha256_hex(
        new_tokens["refresh_token"]
    )
    # Same family across the rotation.
    assert (
        fake.tokens[store.sha256_hex(new_tokens["refresh_token"])]["family_id"]
        == fake.tokens[old_hash]["family_id"]
    )


def test_revoked_refresh_replay_kills_family(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    tokens = _exchange(client, client_doc, code, verifier).get_json()

    def refresh(token):
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": client_doc["client_id"],
            },
        )

    rotated = refresh(tokens["refresh_token"]).get_json()
    replay = refresh(tokens["refresh_token"])  # old token again
    assert replay.get_json()["error"] == "invalid_grant"
    # The rotated successor pair is dead too.
    assert fake.tokens[store.sha256_hex(rotated["access_token"])]["revoked"]
    assert fake.tokens[store.sha256_hex(rotated["refresh_token"])]["revoked"]


def test_unsupported_grant_type(client):
    resp = client.post("/oauth/token", data={"grant_type": "password"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "unsupported_grant_type"


def test_token_missing_fields_invalid_request(client):
    resp = client.post("/oauth/token", data={"grant_type": "authorization_code"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"


# ── Revocation ──────────────────────────────────────────────────────────

def test_revoke_store_failure_returns_503_not_200(client, fake, monkeypatch):
    def boom(token_hash):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(store, "get_token", boom)
    resp = client.post("/oauth/revoke", data={"token": "whatever"})
    assert resp.status_code == 503


def test_revoke_refresh_revokes_family_and_unknown_token_is_200(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    tokens = _exchange(client, client_doc, code, verifier).get_json()

    resp = client.post("/oauth/revoke", data={"token": tokens["refresh_token"]})
    assert resp.status_code == 200
    assert fake.tokens[store.sha256_hex(tokens["access_token"])]["revoked"]

    assert client.post("/oauth/revoke", data={"token": "unknown"}).status_code == 200


# ── Bearer validation on /mcp ───────────────────────────────────────────

def _mcp_ping(client, token, headers=None):
    return client.post(
        "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
    )


def _issue_tokens(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    return _exchange(client, client_doc, code, verifier).get_json()


def test_bearer_valid_token_passes(client, fake):
    tokens = _issue_tokens(client, fake)
    assert _mcp_ping(client, tokens["access_token"]).status_code == 200


def test_bearer_missing_token_401_with_resource_metadata(client, fake):
    resp = client.post(
        "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        content_type="application/json",
    )
    assert resp.status_code == 401
    challenge = resp.headers["WWW-Authenticate"]
    assert "Bearer" in challenge
    assert f'{ORIGIN}/.well-known/oauth-protected-resource/mcp' in challenge


def test_bearer_invalid_expired_revoked_and_refresh_tokens_rejected(client, fake):
    tokens = _issue_tokens(client, fake)
    access_hash = store.sha256_hex(tokens["access_token"])

    bearer.reset_brake_state()
    assert _mcp_ping(client, "no-such-token").status_code == 401

    fake.tokens[access_hash]["expire_at"] = datetime.now(UTC) - timedelta(seconds=1)
    bearer.reset_brake_state()
    assert _mcp_ping(client, tokens["access_token"]).status_code == 401

    fake.tokens[access_hash]["expire_at"] = datetime.now(UTC) + timedelta(hours=1)
    fake.tokens[access_hash]["revoked"] = True
    bearer.reset_brake_state()
    assert _mcp_ping(client, tokens["access_token"]).status_code == 401

    # A refresh token is never accepted as a bearer credential.
    bearer.reset_brake_state()
    resp = _mcp_ping(client, tokens["refresh_token"])
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]


def test_bearer_insufficient_scope_403(client, fake):
    tokens = _issue_tokens(client, fake)
    fake.tokens[store.sha256_hex(tokens["access_token"])]["scope"] = "other:scope"
    bearer.reset_brake_state()
    resp = _mcp_ping(client, tokens["access_token"])
    assert resp.status_code == 403
    assert 'error="insufficient_scope"' in resp.headers["WWW-Authenticate"]


def test_bearer_brake_trips_after_threshold(client, fake):
    bearer.reset_brake_state()
    for _ in range(20):
        assert _mcp_ping(client, "bad-token").status_code == 401
    resp = _mcp_ping(client, "bad-token")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_bearer_origin_allowlist(client, fake):
    tokens = _issue_tokens(client, fake)
    assert (
        _mcp_ping(
            client, tokens["access_token"], headers={"Origin": "https://claude.ai"}
        ).status_code
        == 200
    )
    resp = _mcp_ping(
        client, tokens["access_token"], headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403


# ── Store invariants ────────────────────────────────────────────────────

def test_sha256_hex_is_the_document_key():
    assert store.sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()


def test_expiry_enforced_in_code_despite_ttl_lag():
    # A doc that still exists in Firestore (TTL deletion lags) but whose
    # expire_at is past must be treated as dead.
    stale = {"expire_at": datetime.now(UTC) - timedelta(days=2)}
    assert store.is_expired(stale) is True
    live = {"expire_at": datetime.now(UTC) + timedelta(minutes=5)}
    assert store.is_expired(live) is False
    assert store.is_expired({}) is True  # missing expire_at fails closed
