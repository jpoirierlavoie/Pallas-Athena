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
    assert "</static/vendor/app.af95b30d.css>; rel=preload; as=style" in link
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
