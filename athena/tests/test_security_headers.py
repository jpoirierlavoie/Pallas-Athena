"""Unit tests for security.py response headers — CSP and Early Hints."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template_string

from security import build_csp, init_security


def _make_app(**config) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config.update(config)
    init_security(app)

    @app.route("/")
    def index():
        return "<html><body>ok</body></html>"

    @app.route("/api")
    def api():
        return {"ok": True}

    @app.route("/auth/login")
    def login():
        return "<html><body>login</body></html>"

    @app.route("/offline")
    def offline():
        return "<html><body>hors ligne</body></html>"

    return app


# ── CSP ────────────────────────────────────────────────────────────────────

def test_build_csp_shape():
    # Nonce-based enforcing CSP (Rocket Loader disabled). script-src carries a
    # nonce + 'unsafe-eval' (Alpine) but NO 'unsafe-inline' and NO
    # ajax.cloudflare.com; style-src keeps 'unsafe-inline' (reCAPTCHA).
    csp = build_csp("TESTNONCE")
    script_src = csp.split("style-src", 1)[0]
    assert "script-src 'self' 'nonce-TESTNONCE' 'unsafe-eval'" in script_src
    assert "'unsafe-inline'" not in script_src          # gone from script-src
    assert "ajax.cloudflare.com" not in csp             # Rocket Loader disabled
    assert "cloudflareinsights" not in csp              # Web Analytics beacon not allowlisted
    assert "style-src 'self' 'unsafe-inline'" in csp    # reCAPTCHA dynamic styles
    assert "object-src 'none'" in csp                   # hardening
    assert "firebaseio.com" not in csp                  # vestigial RTDB origin dropped
    assert csp.rstrip().endswith("report-uri /csp-report")
    # Default stays locked down: only the consent screen may post off-origin.
    assert "form-action 'self';" in csp
    assert "claude.ai" not in csp


def _form_action(csp: str) -> list[str]:
    """The form-action directive's source list, exactly as sent.

    Parsed rather than substring-matched: a substring check on the whole
    header cannot tell `https://claude.ai` from `https://claude.ai.evil`,
    nor which directive the origin belongs to.
    """
    for directive in csp.split(";"):
        parts = directive.split()
        if parts and parts[0] == "form-action":
            return parts[1:]
    return []


# ── form-action / the OAuth consent redirect ───────────────────────────────

def test_consent_page_allows_the_oauth_callback_redirect():
    """form-action covers the WHOLE redirect chain of a submission. The
    consent POST answers 302 to Claude's callback, so 'self' alone blocks
    the authorization code from ever reaching the client and the connector
    can never be added."""
    from security import _FORM_ACTION_OAUTH, build_csp

    csp = build_csp("N", _FORM_ACTION_OAUTH)
    assert "form-action 'self' https://claude.ai https://claude.com;" in csp
    # Everything else in the policy is untouched by the widening.
    assert "object-src 'none'" in csp
    assert csp.replace(_FORM_ACTION_OAUTH, "'self'") == build_csp("N")


def test_form_action_is_widened_only_on_the_consent_path():
    app = _make_app(ENV="production")

    @app.route("/oauth/authorize")
    def consent():
        return "consent"

    client = app.test_client()
    consent_csp = client.get("/oauth/authorize").headers[
        "Content-Security-Policy"
    ]
    other_csp = client.get("/").headers["Content-Security-Policy"]
    # Compare the directive's SOURCE LIST, not a substring of the whole
    # header: `"https://claude.ai" in csp` would also pass on
    # `https://claude.ai.evil.example`, and matches any other directive that
    # happens to name the origin.
    assert _form_action(consent_csp) == [
        "'self'", "https://claude.ai", "https://claude.com",
    ]
    assert _form_action(other_csp) == ["'self'"]


def test_form_action_allows_loopback_outside_production():
    app = _make_app(ENV="development")

    @app.route("/oauth/authorize")
    def consent():
        return "consent"

    csp = app.test_client().get("/oauth/authorize").headers[
        "Content-Security-Policy"
    ]
    sources = _form_action(csp)
    assert "http://localhost:*" in sources
    assert "http://127.0.0.1:*" in sources


def test_form_action_covers_every_allowed_oauth_redirect_uri():
    """The CSP source list is hand-maintained; pin it against the actual
    redirect-URI allowlist so the two cannot drift."""
    import os
    from unittest import mock
    from urllib.parse import urlparse

    os.environ.setdefault("SECRET_KEY", "test-secret")
    os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
    os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
    os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")
    with mock.patch("google.cloud.firestore.Client"):
        from mcp import ALLOWED_REDIRECT_URIS

    from security import _FORM_ACTION_OAUTH

    sources = set(_FORM_ACTION_OAUTH.split())
    for uri in ALLOWED_REDIRECT_URIS:
        parsed = urlparse(uri)
        assert f"{parsed.scheme}://{parsed.netloc}" in sources, uri


def test_csp_header_enforced_with_matching_nonce():
    app = _make_app()

    @app.route("/nonce-probe")
    def _probe():
        return render_template_string('<script nonce="{{ csp_nonce }}">1;</script>')

    resp = app.test_client().get("/nonce-probe")
    header = resp.headers["Content-Security-Policy"]
    # Enforced header only — no stale report-only duplicate.
    assert "Content-Security-Policy-Report-Only" not in resp.headers
    # The nonce in the header must equal the nonce rendered into the inline
    # <script> — otherwise every inline script is blocked in production.
    m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", header)
    assert m, header
    nonce = m.group(1)
    assert f'nonce="{nonce}"'.encode() in resp.data
    assert header == build_csp(nonce)


def test_csp_nonce_is_per_request():
    app = _make_app()

    def _nonce():
        h = app.test_client().get("/").headers["Content-Security-Policy"]
        return re.search(r"'nonce-([A-Za-z0-9_-]+)'", h).group(1)

    assert _nonce() != _nonce()  # a fresh nonce every request


# ── Early Hints (Link headers) ─────────────────────────────────────────────

def test_link_header_on_full_page_html():
    app = _make_app()
    resp = app.test_client().get("/")
    link = resp.headers.get("Link", "")
    assert "</static/vendor/app.0346c0c3.css>; rel=preload; as=style" in link
    # The font preload MUST keep `crossorigin` + its type — losing either
    # makes the browser fetch the woff2 twice (fonts are CORS-mode fetches).
    assert (
        'noto-sans-v42-latin-wght.woff2>; rel=preload; as=font;'
        ' type="font/woff2"; crossorigin' in link
    )
    assert (
        'material-symbols-outlined-v364-15c3a869.woff2>; rel=preload; as=font;'
        ' type="font/woff2"; crossorigin' in link
    )
    assert "htmx-2.0.4.min.js>; rel=preload; as=script" in link
    assert "alpinejs-3.15.12.min.js>; rel=preload; as=script" in link
    # No App Check configured — its assets must not be hinted.
    assert "appcheck-boot" not in link
    assert "preconnect" not in link


def test_link_header_includes_appcheck_assets_when_configured():
    app = _make_app(RECAPTCHA_ENTERPRISE_SITE_KEY="test-site-key")
    resp = app.test_client().get("/")
    link = resp.headers.get("Link", "")
    assert "firebase-app-compat-10.12.2.js>; rel=preload; as=script" in link
    assert "firebase-app-check-compat-10.12.2.js>; rel=preload; as=script" in link
    assert "appcheck-boot.fee929af.js>; rel=preload; as=script" in link
    assert "<https://www.gstatic.com>; rel=preconnect" in link
    assert "<https://www.google.com>; rel=preconnect" in link


def test_login_page_gets_its_own_hint_set():
    # login.html is standalone: no htmx, no appcheck-boot, but it loads the
    # auth SDK — the heaviest asset on the cold-start path — and uses
    # reCAPTCHA (phone MFA) even without an App Check site key.
    app = _make_app()
    resp = app.test_client().get("/auth/login")
    link = resp.headers.get("Link", "")
    assert "firebase-auth-compat-10.12.2.js>; rel=preload; as=script" in link
    assert (
        'noto-sans-v42-latin-wght.woff2>; rel=preload; as=font;'
        ' type="font/woff2"; crossorigin' in link
    )
    assert (
        'material-symbols-outlined-v364-15c3a869.woff2>; rel=preload; as=font;'
        ' type="font/woff2"; crossorigin' in link
    )
    assert "<https://www.gstatic.com>; rel=preconnect" in link
    assert "htmx" not in link
    assert "appcheck-boot" not in link


def test_no_link_header_for_offline_page():
    app = _make_app()
    resp = app.test_client().get("/offline")
    assert "Link" not in resp.headers


def test_no_link_header_for_htmx_partials():
    app = _make_app()
    resp = app.test_client().get("/", headers={"HX-Request": "true"})
    assert "Link" not in resp.headers


def test_no_link_header_for_non_html():
    app = _make_app()
    resp = app.test_client().get("/api")
    assert "Link" not in resp.headers


def test_no_link_header_for_404():
    app = _make_app()
    resp = app.test_client().get("/does-not-exist")
    assert resp.status_code == 404
    assert "Link" not in resp.headers


def test_baseline_security_headers_unchanged():
    app = _make_app()
    resp = app.test_client().get("/")
    assert resp.headers["Cache-Control"] == (
        "no-store, no-cache, must-revalidate, private"
    )
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


# ── Request size caps (Phase H: template upload exemption) ─────────────────
#
# The size guard is an app-level before_request keyed on the path, so it
# fires even for unrouted paths — a 404 (not 413) response proves the
# request passed the size check.

_2MB = b"x" * (2 * 1024 * 1024)
_11MB = b"x" * (11 * 1024 * 1024)


def _post_size(path: str, body: bytes) -> int:
    app = _make_app()
    return app.test_client().post(path, data=body).status_code


def test_template_create_allows_up_to_10mb():
    assert _post_size("/gabarits/", _2MB) != 413


def test_template_create_rejects_over_10mb():
    assert _post_size("/gabarits/", _11MB) == 413


def test_template_update_allows_up_to_10mb():
    assert _post_size("/gabarits/abc-123", _2MB) != 413
    assert _post_size("/gabarits/abc-123", _11MB) == 413


def test_generation_post_stays_at_1mb():
    assert _post_size("/gabarits/generer", _2MB) == 413


def test_template_sub_routes_stay_at_1mb():
    # /gabarits/<id>/delete and friends are not upload paths.
    assert _post_size("/gabarits/abc-123/delete", _2MB) == 413


def test_other_routes_still_capped_at_1mb():
    assert _post_size("/api", _2MB) == 413


# ── Origin secret vs App Engine-internal traffic (portail L1 §8.3) ─────────

def test_origin_secret_blocks_without_header():
    app = _make_app(CF_ORIGIN_SECRET="s3cret")
    assert app.test_client().get("/").status_code == 403


def test_origin_secret_passes_with_header():
    app = _make_app(CF_ORIGIN_SECRET="s3cret")
    reponse = app.test_client().get("/", headers={"X-Origin-Auth": "s3cret"})
    assert reponse.status_code == 200


def test_origin_secret_bypassed_only_for_internal_dispatch_headers():
    # Cloud Tasks / cron traffic (source 0.1.0.2) carries X-AppEngine-* headers
    # that App Engine STRIPS from all external traffic — presence proves the
    # dispatch is internal, so the origin secret must not block it. The header
    # VALUE is not trusted here; each machine handler re-checks its own.
    app = _make_app(CF_ORIGIN_SECRET="s3cret")
    web = app.test_client()
    assert web.get(
        "/", headers={"X-AppEngine-QueueName": "portail"}
    ).status_code == 200
    assert web.get(
        "/", headers={"X-Appengine-Cron": "true"}
    ).status_code == 200
    # A plain external request (no internal header) is still refused.
    assert web.get("/").status_code == 403


def test_origin_secret_bypassed_for_appengine_warmup():
    """`/_ah/*` never transits Cloudflare, so it carries no injected header.

    Untested until August 2026, and it is the exemption that decides whether a
    cold instance can warm up once the secret is armed: App Engine sends
    `/_ah/warmup` before routing live traffic, and a 403 there would leave the
    Firestore channel unprimed on every cold start.
    """
    app = _make_app(CF_ORIGIN_SECRET="s3cret")

    @app.route("/_ah/warmup")
    def warmup():
        return "", 200

    assert app.test_client().get("/_ah/warmup").status_code == 200


# ── HSTS: one value, repeated in ten places, previously pinned by nothing ──
#
# `Strict-Transport-Security` is emitted by security.py, by
# client/security.py, and by the static handlers of BOTH yaml files (static
# handlers are served by the App Engine frontend and never reach Flask's
# after_request hook). Until August 2026 no test asserted it anywhere, so the
# ten copies could drift silently — and two handlers had already fallen out.
#
# These tests DERIVE the inventory from the yaml files instead of holding a
# list. A hand-kept inventory decays: that is the same lesson the MCP suite
# learned when `list_invoices` slipped past a hand-written tuple.

_ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(_ATHENA_DIR, *parts), encoding="utf-8") as fh:
        return fh.read()


def _yaml_handlers(text: str) -> list[dict]:
    """Parse the `handlers:` blocks of an App Engine yaml.

    Deliberately small rather than pulling in PyYAML, which is not a
    dependency of this project (runtime or dev). It reads only the shape these
    two files actually use: `  - url: X` followed by indented `key: value`
    lines, with an `http_headers:` sub-block. `_assert_parse_is_sane` below is
    what stops a silently-empty parse from passing every other assertion.
    """
    handlers: list[dict] = []
    current: dict | None = None
    in_headers = False
    for raw in text.splitlines():
        if raw.startswith("  - url:"):
            current = {"url": raw.split(":", 1)[1].strip(), "headers": {}}
            handlers.append(current)
            in_headers = False
            continue
        if current is None or not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith("    "):        # dedented out of the block
            current = None
            continue
        stripped = raw.strip()
        if stripped == "http_headers:":
            in_headers = True
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip().strip('"')
        if in_headers and raw.startswith("      "):
            current["headers"][key.strip()] = value
        else:
            in_headers = False
            current[key.strip()] = value
    return handlers


def _assert_parse_is_sane(handlers: list[dict], name: str) -> None:
    assert len(handlers) >= 3, f"{name}: parser found {len(handlers)} handlers"
    assert handlers[-1]["url"] == "/.*", f"{name}: last handler is not the catch-all"
    assert handlers[-1].get("script") == "auto", f"{name}: catch-all is not script:auto"


def _static_handlers(handlers: list[dict]) -> list[dict]:
    """Handlers served by the App Engine frontend, which bypass Flask."""
    return [h for h in handlers if "static_files" in h or "static_dir" in h]


def _hsts_of(app_response_headers) -> str:
    return app_response_headers["Strict-Transport-Security"]


def test_hsts_is_the_same_string_in_all_ten_places():
    """Flask is the reference; every other copy must equal it byte for byte."""
    canonical = _hsts_of(_make_app().test_client().get("/").headers)

    copies: list[tuple[str, str]] = [("security.py (Flask)", canonical)]

    # The portail service's own after_request, read as source: importing
    # client.security would drag in the portal's config resolution.
    portail_src = _read("client", "security.py")
    found = re.findall(r'"(max-age=[^"]+)"', portail_src)
    assert found, "client/security.py: no HSTS literal found"
    copies += [("client/security.py", v) for v in found]

    for yaml_name in ("app.yaml", "portail.yaml"):
        handlers = _yaml_handlers(_read(yaml_name))
        _assert_parse_is_sane(handlers, yaml_name)
        for h in _static_handlers(handlers):
            value = h["headers"].get("Strict-Transport-Security")
            assert value, f"{yaml_name} {h['url']}: no Strict-Transport-Security"
            copies.append((f"{yaml_name} {h['url']}", value))

    assert len(copies) >= 10, f"expected at least 10 copies, found {len(copies)}"
    divergent = [(where, v) for where, v in copies if v != canonical]
    assert not divergent, f"HSTS diverges from {canonical!r}: {divergent}"


def test_hsts_value_still_satisfies_the_preload_requirements():
    """Consistency is not enough — the value itself must stay strong.

    The preload list demands max-age >= 31536000 (one year) plus
    includeSubDomains. The edge served 15552000 (180 days) for months while
    the code said 63072000, so this asserts the property, not just that the
    ten copies agree on whatever they happen to say.
    """
    value = _hsts_of(_make_app().test_client().get("/").headers)
    max_age = int(re.search(r"max-age=(\d+)", value).group(1))
    assert max_age >= 31536000, f"max-age={max_age} is below the one-year floor"
    assert "includeSubDomains" in value
    assert "preload" in value


def test_every_static_handler_carries_the_full_baseline():
    """The rule is TOTAL — no 'HTML and scripts only' carve-out.

    /sw.js and /manifest.json sat without HSTS or `secure: always` precisely
    because they fell outside an exception list. Handlers with `script: auto`
    are excluded on a principled basis, not an arbitrary one: those responses
    go through Flask, where _add_security_headers sets the same headers.
    """
    for yaml_name in ("app.yaml", "portail.yaml"):
        handlers = _yaml_handlers(_read(yaml_name))
        _assert_parse_is_sane(handlers, yaml_name)
        for h in _static_handlers(handlers):
            where = f"{yaml_name} {h['url']}"
            assert h.get("secure") == "always", f"{where}: missing `secure: always`"
            assert h["headers"].get("X-Content-Type-Options") == "nosniff", (
                f"{where}: missing nosniff"
            )
            assert h["headers"].get("Strict-Transport-Security"), (
                f"{where}: missing HSTS"
            )
        assert handlers[-1].get("secure") == "always", (
            f"{yaml_name}: the catch-all must still force HTTPS"
        )
