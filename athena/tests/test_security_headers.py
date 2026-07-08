"""Unit tests for security.py response headers — CSP and Early Hints."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from security import CSP, init_security


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

def test_csp_allows_rocket_loader_origin():
    # Cloudflare Rocket Loader injects its loader from ajax.cloudflare.com;
    # without this origin the (future, enforcing) CSP would break it and the
    # report-only policy floods /csp-report on every page view.
    assert "https://ajax.cloudflare.com" in CSP
    assert "script-src 'self' https://ajax.cloudflare.com" in CSP


def test_csp_header_present_and_report_only():
    app = _make_app()
    resp = app.test_client().get("/")
    assert resp.headers["Content-Security-Policy-Report-Only"] == CSP
    assert "Content-Security-Policy" not in (
        set(resp.headers.keys()) - {"Content-Security-Policy-Report-Only"}
    )


# ── Early Hints (Link headers) ─────────────────────────────────────────────

def test_link_header_on_full_page_html():
    app = _make_app()
    resp = app.test_client().get("/")
    link = resp.headers.get("Link", "")
    assert "</static/vendor/app.5f4afed2.css>; rel=preload; as=style" in link
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
