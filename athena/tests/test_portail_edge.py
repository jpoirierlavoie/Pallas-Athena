"""The portal's edge layers 2 and 3 — origin secret and appspot block.

Until 2026-09-30 the portal shared the app's firewall (Cloudflare ranges only)
and nothing else of the main service's edge defence: anyone fronting its
appspot host with THEIR OWN Cloudflare zone reached it, and
``CF-Connecting-IP`` — the key of every portal rate limit — was theirs to
choose. `client/security.enforce_edge` copies the main service's two checks
(the portal never imports security.py); these tests pin both, the fail-open
arming, the one structured warning, and the import isolation that forces the
copy.
"""

import ast
import logging
import os
import pathlib
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")
os.environ.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")

from flask import Flask  # noqa: E402

import client.app as client_app  # noqa: E402
from client import config as pconfig  # noqa: E402
from client import security as psec  # noqa: E402

ATHENA = pathlib.Path(__file__).resolve().parent.parent
SECRET = "s3cret-token-from-the-edge"


@pytest.fixture(autouse=True)
def _fresh_warn_once():
    psec._WARNED_ONCE.clear()
    yield
    psec._WARNED_ONCE.clear()


def _edge_app(secret: str = "", *, env: str = "production",
              off=("cf_origin_secret_unset", "")) -> Flask:
    app = Flask(__name__)
    app.config.update(ENV=env, CF_ORIGIN_SECRET=secret, CF_ORIGIN_SECRET_OFF=off)
    app.before_request(psec.enforce_edge)

    @app.route("/sante")
    def sante():
        return "ok"

    @app.route("/_ah/warmup")
    def warmup():
        return "warm"

    return app


def _get(app, path="/sante", host="portail.example.ca", **headers):
    return app.test_client().get(path, headers={"Host": host, **headers})


# ── Layer 2: the origin secret ───────────────────────────────────────────


def test_an_armed_portal_refuses_a_request_without_the_header():
    assert _get(_edge_app(SECRET)).status_code == 403


def test_an_armed_portal_serves_the_header_the_edge_injects():
    resp = _get(_edge_app(SECRET), **{"X-Origin-Auth": SECRET})
    assert resp.status_code == 200


def test_a_wrong_or_non_ascii_header_is_a_403_never_a_500():
    app = _edge_app(SECRET)
    assert _get(app, **{"X-Origin-Auth": SECRET + "x"}).status_code == 403
    # compare_digest raises on a non-ASCII str: compared as bytes, a forged
    # header carrying one is refused like any other.
    assert _get(app, **{"X-Origin-Auth": "sécret"}).status_code == 403


def test_app_engine_paths_and_internal_dispatches_never_transit_cloudflare():
    app = _edge_app(SECRET)
    assert _get(app, "/_ah/warmup").status_code == 200
    assert _get(app, **{"X-AppEngine-QueueName": "portail"}).status_code == 200
    assert _get(app, **{"X-Appengine-Cron": "true"}).status_code == 200


def test_an_unarmed_portal_serves_and_says_so_once(caplog):
    app = _edge_app("")
    with caplog.at_level(logging.WARNING, logger="pallas.security"):
        assert _get(app).status_code == 200
        assert _get(app).status_code == 200
    events = [r.json_fields for r in caplog.records
              if getattr(r, "json_fields", {}).get("event") == "origin_secret_disabled"]
    assert len(events) == 1
    assert events[0]["reason"] == "cf_origin_secret_unset"


def test_an_unreadable_secret_names_why_the_check_is_off(caplog):
    app = _edge_app("", off=("cf_origin_secret_unreadable", "PermissionDenied"))
    with caplog.at_level(logging.WARNING, logger="pallas.security"):
        assert _get(app).status_code == 200
    (event,) = [r.json_fields for r in caplog.records
                if getattr(r, "json_fields", {}).get("event") == "origin_secret_disabled"]
    assert event["reason"] == "cf_origin_secret_unreadable"
    assert event["error_type"] == "PermissionDenied"


def test_outside_production_an_unarmed_portal_is_quiet(caplog):
    with caplog.at_level(logging.WARNING, logger="pallas.security"):
        assert _get(_edge_app("", env="development")).status_code == 200
    assert not [r for r in caplog.records if r.name == "pallas.security"]


# ── Layer 3: the appspot host ────────────────────────────────────────────


@pytest.mark.parametrize("host", [
    "portail-dot-athena-pallas.nn.r.appspot.com",
    "PORTAIL-DOT-ATHENA-PALLAS.NN.R.APPSPOT.COM.",
    "appspot.com",
])
def test_the_appspot_host_is_refused_armed_or_not(host):
    assert _get(_edge_app(""), host=host).status_code == 403
    assert _get(_edge_app(SECRET), host=host,
                **{"X-Origin-Auth": SECRET}).status_code == 403


def test_app_engine_s_own_paths_pass_on_the_appspot_host():
    app = _edge_app(SECRET)
    host = "portail-dot-athena-pallas.nn.r.appspot.com"
    assert _get(app, "/_ah/warmup", host=host).status_code == 200
    assert _get(app, host=host, **{"X-Appengine-Cron": "true"}).status_code == 200


# ── The resolver and the factory ─────────────────────────────────────────


def test_the_resolver_reads_the_environment_outside_production(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("CF_ORIGIN_SECRET", SECRET)
    assert pconfig.cf_origin_secret() == pconfig.OriginSecret(SECRET, "")
    monkeypatch.setenv("CF_ORIGIN_SECRET", "")
    assert pconfig.cf_origin_secret() == pconfig.OriginSecret(
        "", "cf_origin_secret_unset")


def test_the_resolver_fails_open_in_production_and_says_why(monkeypatch):
    monkeypatch.setenv("ENV", "production")

    class PermissionDenied(Exception):
        pass

    def _denied(_secret_id):
        raise PermissionDenied("no accessor")

    monkeypatch.setattr(pconfig, "_from_secret_manager", _denied)
    assert pconfig.cf_origin_secret() == pconfig.OriginSecret(
        "", "cf_origin_secret_unreadable", "PermissionDenied")
    monkeypatch.setattr(pconfig, "_from_secret_manager", lambda _s: SECRET)
    assert pconfig.cf_origin_secret() == pconfig.OriginSecret(SECRET, "")


def test_the_factory_arms_the_edge_and_runs_it_first(monkeypatch):
    monkeypatch.setattr(client_app, "cf_origin_secret",
                        lambda: pconfig.OriginSecret(SECRET, ""))
    with mock.patch("utils.tracing_setup.init_app"):
        with mock.patch.object(client_app, "_init_firebase"):
            app = client_app.create_portail_app()
    assert app.config["CF_ORIGIN_SECRET"] == SECRET
    chain = app.before_request_funcs[None]
    assert chain.index(psec.enforce_edge) < chain.index(psec.verify_app_check)
    client = app.test_client()
    assert client.get("/sante").status_code == 403
    assert client.get("/sante", headers={"X-Origin-Auth": SECRET}).status_code == 200


# ── Why the checks are COPIED: the portal imports none of the main service ─

_FORBIDDEN_TOP_LEVEL = {"security", "config", "models", "main", "routes", "dav", "mcp"}


def test_no_portal_module_imports_the_main_service():
    """models/__init__ builds the default database's Firestore client at
    import and config.py resolves the MAIN service's required secrets — a
    portal that imported either would need permissions its least-privilege
    account deliberately lacks. Pinned here, where the copied edge checks
    would be the first temptation to import security.py."""
    offenders, scanned = [], 0
    for path in sorted((ATHENA / "client").rglob("*.py")):
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in _FORBIDDEN_TOP_LEVEL:
                    offenders.append(f"{path.relative_to(ATHENA)}:{node.lineno} {name}")
    assert scanned > 5
    assert offenders == []


# ── The main service's twin: a forged non-ASCII header is a 403 too ──────


def test_the_main_service_refuses_a_non_ascii_header_with_a_403():
    import security

    app = Flask(__name__)
    app.config.update(ENV="production", CF_ORIGIN_SECRET=SECRET)
    app.before_request(security._enforce_origin_secret)

    @app.route("/x")
    def x():
        return "ok"

    client = app.test_client()
    assert client.get("/x", headers={"X-Origin-Auth": "sécret"}).status_code == 403
    assert client.get("/x", headers={"X-Origin-Auth": SECRET}).status_code == 200
