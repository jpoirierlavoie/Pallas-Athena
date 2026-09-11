"""« Paramètres » — les routes et le CADRE de la page de sécurité.

**Ce que cette suite ne couvre pas, et il faut le dire.** Il n'y a dans ce
dépôt ni `package.json`, ni jest, ni jsdom, ni Playwright : toute la logique
de sécurité est du JavaScript de navigateur que pytest ne peut pas exécuter.
Ce fichier épingle le cadre — routage, CSRF, discipline de nonce, plomberie
de configuration, redirections, contrat du journal — et **pas une ligne** de
la danse de ré-authentification, de l'ordre d'inscription ou des gardes. La
liste M0-M19 de DEPLOYMENT.md est ce qui couvre le reste, à la main.

L'épingle la plus utile ici est `REQUIRE_MFA` : la garde qui interdit de
retirer le dernier facteur est ENTIÈREMENT en aval de cette valeur, donc une
régression de plomberie la désarmerait en silence.
"""

import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.auth_routes as ra
    import routes.settings as rs
    from models import settings as S

from flask import Flask  # noqa: E402

from security import csrf, init_security, limiter  # noqa: E402


def _app(require_mfa=True, ratelimit=False, csrf_on=False):
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["REQUIRE_MFA"] = require_mfa
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["RATELIMIT_ENABLED"] = ratelimit
    # CSRF désactivé par défaut pour que les tests de POST portent sur
    # leur sujet ; UN test l'active pour prouver qu'il est bien appliqué
    # sur ce blueprint (il ne doit JAMAIS être exempté).
    app.config["WTF_CSRF_ENABLED"] = csrf_on
    # `init_security` pour obtenir un VRAI `csp_nonce` et l'en-tête CSP —
    # c'est ce qui rend l'épingle de nonce ci-dessous significative.
    init_security(app)
    app.jinja_env.globals["ms"] = _ms
    # Le gabarit rend `firebase_api_key|tojson` INCONDITIONNELLEMENT ; sans
    # ces globales (fournies en production par le context processor de
    # main.py), `tojson` lève sur `Undefined`. Les autres tests de route y
    # échappent seulement parce que le bloc de base.html est sous un
    # `{% if %}` faux.
    app.jinja_env.globals["firebase_api_key"] = "clef-test"
    app.jinja_env.globals["firebase_project_id"] = "test-project"
    app.jinja_env.globals["firebase_app_id"] = "1:2:web:3"
    app.register_blueprint(rs.settings_bp)
    app.register_blueprint(ra.auth_bp)
    return app


@pytest.fixture()
def web():
    app = _app()
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


@pytest.fixture()
def anon():
    return _app().test_client()


@pytest.fixture(autouse=True)
def _no_firestore(monkeypatch):
    """Le profil vient de la semence : aucun accès Firestore dans ces tests."""
    monkeypatch.setattr(S, "_read_raw", lambda: None, raising=True)


# ── Accès ────────────────────────────────────────────────────────────────

def test_anonymous_is_redirected_with_next(anon):
    r = anon.get("/parametres/")
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]
    assert "next=/parametres/" in r.headers["Location"]


def test_anonymous_cannot_reach_securite(anon):
    assert anon.get("/parametres/securite").status_code == 302


def test_the_profile_page_renders(web):
    r = web.get("/parametres/")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Profil du cabinet" in body
    assert 'name="nom"' in body
    assert 'name="telecopieur"' in body
    assert 'name="gst_number"' in body
    # La limite honnête est ÉCRITE sur la page, pas enterrée dans un commit.
    assert "portail.yaml" in body


def test_the_security_page_renders(web):
    r = web.get("/parametres/securite")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Vérification en deux étapes" in body
    # La marche à suivre est STATIQUE : elle doit se lire même quand le SDK
    # n'a pas démarré.
    assert "REQUIRE_MFA" in body
    assert "Console Firebase" in body


# ── La plomberie dont dépend la garde ────────────────────────────────────

@pytest.mark.parametrize("valeur,attendu", [(True, "true"), (False, "false")])
def test_require_mfa_round_trips_as_a_json_boolean(valeur, attendu):
    """La garde « on ne retire pas le dernier facteur » est entièrement en
    aval de cette valeur : une régression de plomberie la désarme en
    silence, sans rien casser d'autre."""
    app = _app(require_mfa=valeur)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    body = client.get("/parametres/securite").data.decode("utf-8")
    assert f"var athenaRequireMfa = {attendu};" in body


def test_the_session_uid_is_rendered(web):
    """La garde d'identité : sans cette valeur, la page ne distingue pas
    « pas d'état Firebase » de « une AUTRE identité »."""
    body = web.get("/parametres/securite").data.decode("utf-8")
    assert 'var athenaUid = "u1";' in body


# ── Discipline CSP ───────────────────────────────────────────────────────

def test_every_inline_script_carries_the_csp_nonce(web):
    """`script-src` n'a pas `'unsafe-inline'` : un nonce oublié rend la page
    silencieusement morte."""
    r = web.get("/parametres/securite")
    csp = r.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    body = r.data.decode("utf-8")
    for tag in re.finditer(r"<script([^>]*)>", body):
        attrs = tag.group(1)
        if "src=" in attrs or 'type="application/json"' in attrs:
            continue
        assert f'nonce="{nonce}"' in attrs, f"bloc inline sans nonce : {attrs[:80]}"


def test_the_page_has_no_inline_event_handler(web):
    """Un nonce ne peut pas autoriser un attribut `on*` — la CSP les refuse
    catégoriquement."""
    body = web.get("/parametres/securite").data.decode("utf-8")
    assert not re.search(r"\son[a-z]+=\"", body)


def test_the_security_page_carries_no_htmx_attribute(web):
    """Épingle la décision : `/parametres/` n'est PAS sous un préfixe exempt
    d'App Check comme l'est `/auth/`, et App Check ne s'applique qu'aux
    requêtes portant `HX-Request`. Sans htmx, la page reste hors de ce
    chemin d'application."""
    body = web.get("/parametres/securite").data.decode("utf-8")
    # `base.html` porte `hx-headers` sur <body> pour toute l'application ;
    # ce qui compte est que cette page n'ÉMETTE aucune requête htmx.
    for verbe in ("hx-post", "hx-get", "hx-put", "hx-delete", "hx-patch",
                  "hx-trigger", "hx-target", "hx-include", "hx-swap",
                  "hx-vals", "hx-boost"):
        assert verbe not in body, verbe


def test_one_recaptcha_container_outside_every_x_show(web):
    """Un reCAPTCHA invisible dans un sous-arbre `display:none` ne peut pas
    afficher un défi escaladé, et `verifyPhoneNumber` reste bloqué sans
    erreur ni rejet."""
    body = web.get("/parametres/securite").data.decode("utf-8")
    assert body.count('id="parametres-recaptcha"') == 1
    # Il vit APRÈS la boîte de dépannage, elle-même hors de la racine
    # `x-data` : le conteneur n'est donc dans AUCUN panneau `x-show`.
    # (Un `x-show` subsiste plus loin dans le document : la nav mobile de
    # base.html, qui n'est pas cette page.)
    assert (body.index('id="parametres-recaptcha"')
            > body.index("Si vous n'arrivez plus à vous connecter"))


def test_the_page_declares_no_const_auth(web):
    """Deux `const auth` au niveau supérieur d'un document est une
    SyntaxError qui tue les DEUX blocs."""
    body = web.get("/parametres/securite").data.decode("utf-8")
    # Une DÉCLARATION, pas la mention qu'en fait le commentaire du gabarit.
    assert not re.search(r"const\s+auth\s*=", body)
    assert "var athenaAuth = null;" in body


# ── Les anciennes URL MFA ────────────────────────────────────────────────

@pytest.mark.parametrize("chemin", ["/auth/mfa-setup", "/auth/mfa-manage"])
def test_the_mfa_urls_redirect_to_the_settings_page(web, chemin):
    r = web.get(chemin)
    assert r.status_code == 302          # 302, jamais 301 (mise en cache)
    assert r.headers["Location"].endswith("/parametres/securite")


@pytest.mark.parametrize("chemin", ["/auth/mfa-setup", "/auth/mfa-manage"])
def test_the_mfa_urls_still_require_a_session(anon, chemin):
    r = anon.get(chemin)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


# ── Enregistrement du profil ─────────────────────────────────────────────

def test_a_refusal_re_renders_at_200_with_the_submitted_values(web, monkeypatch):
    """htmx n'est pas en cause ici, mais la règle vaut quand même : un
    message destiné à être LU sort en 2xx, et on réaffiche ce qui a été
    soumis — corriger à l'aveugle est pire."""
    r = web.post("/parametres/cabinet", data={
        "nom": "", "organisation": "Cabinet Test",
        "telephone": "", "telecopieur": "", "courriel": "",
        "address_street": "", "address_unit": "", "address_city": "",
        "address_province": "", "address_postal_code": "", "address_country": "",
        "gst_number": "", "qst_number": "",
    })
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Le nom du juriste est requis." in body
    assert "Cabinet Test" in body


def test_a_successful_save_redirects(web, monkeypatch):
    # `routes/settings.py` fait `from models.settings import
    # update_cabinet`, donc le nom est lié DANS le module de route :
    # patcher `S.update_cabinet` ne l'atteindrait pas.
    monkeypatch.setattr(rs, "update_cabinet", lambda data: ({**data}, []))
    r = web.post("/parametres/cabinet", data={"nom": "Me X"})
    assert r.status_code == 302
    assert "ok=1" in r.headers["Location"]


def test_the_form_posts_exactly_the_declared_field_set(web):
    """Un champ glissé dans le gabarit sans entrer dans `_FORM_FIELDS` ne
    serait pas refusé — il serait IGNORÉ, et la main suivante le
    « brancherait » sans qu'aucun test ne tombe."""
    body = web.get("/parametres/").data.decode("utf-8")
    noms = set(re.findall(r'<input[^>]*\sname="([^"]+)"', body))
    noms.discard("csrf_token")
    noms.discard("username")            # leurre d'autocomplétion, non soumis
    assert noms == set(rs._FORM_FIELDS)


# ── Le journal de sécurité ───────────────────────────────────────────────

def test_the_journal_refuses_a_post_without_csrf():
    """Prouve que le blueprint n'a pas été exempté par mégarde."""
    app = _app(csrf_on=True)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    r = client.post("/parametres/securite/journal", data={"event": "password_changed"})
    assert r.status_code == 400


def test_the_journal_requires_a_session(anon):
    r = anon.post("/parametres/securite/journal", data={"event": "password_changed"})
    assert r.status_code == 302


def test_the_journal_accepts_an_allowlisted_event(web):
    r = web.post("/parametres/securite/journal", data={"event": "mfa_enrolled"})
    assert r.status_code == 204


def test_the_journal_refuses_an_unknown_event_and_logs_nothing(web, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="pallas.auth"):
        r = web.post(
            "/parametres/securite/journal",
            data={"event": "arbitraire", "factor_count": "1"},
        )
    assert r.status_code == 400
    assert not [rec for rec in caplog.records if rec.name == "pallas.auth"]


def test_the_journal_clamps_the_factor_count(web, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="pallas.auth"):
        web.post(
            "/parametres/securite/journal",
            data={"event": "mfa_unenrolled", "factor_count": "99"},
        )
    rec = [r for r in caplog.records if r.name == "pallas.auth"][-1]
    assert rec.json_fields["factor_count_client"] == 10


def test_the_journal_ignores_unknown_fields_entirely(web, caplog):
    """Aucun champ libre venu du navigateur n'atteint un journal."""
    import logging
    with caplog.at_level(logging.INFO, logger="pallas.auth"):
        web.post("/parametres/securite/journal", data={
            "event": "password_changed",
            "phone": "+15145550000",
            "note": "texte libre",
        })
    rec = [r for r in caplog.records if r.name == "pallas.auth"][-1]
    assert set(rec.json_fields) == {"event", "outcome"}


# ── La navigation ────────────────────────────────────────────────────────

def test_the_nav_points_at_parametres_on_both_surfaces():
    """La barre latérale ET le menu « Plus » — l'épingle de
    test_comptabilite_parity."""
    import io
    src = io.open(
        os.path.join(_ATHENA, "templates", "base.html"), encoding="utf-8"
    ).read()
    assert src.count('href="/parametres"') == 2
    assert "mfa-manage" not in src
    assert "ms('settings', 20)" in src


# ── « Paramètres → Configuration » — la page de diagnostic ──────────────
# Elle rend le MÊME rapport que `python -m scripts.check_config`, par les mêmes
# tables et les mêmes prédicats. Le script existait déjà et savait déjà
# repérer un `cf-origin-secret` absent ; rien ne l'exécutait, ce qui est
# exactement comment le contrôle d'origine est resté éteint des mois.

from utils.deployment_report import FAIL, OK, WARN, Report  # noqa: E402
from utils import config_checks as _cc  # noqa: E402


def _rapport(op_level=OK, fork_rows=0, detail=None):
    """Un rapport fabriqué : sections opérationnelles + hygiène de fork."""
    rpt = Report()
    rpt.section(_cc.SECTION_ENV)
    rpt.emit(op_level, "une ligne opérationnelle", **(detail or {}))
    rpt.section(_cc.SECTION_FORK)
    for i in range(fork_rows):
        rpt.emit(WARN, f"athena/app.yaml: still contains the owner's thing {i}")
    return rpt


def test_anonymous_cannot_reach_configuration(anon):
    r = anon.get("/parametres/configuration")
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


def test_the_configuration_page_renders(web):
    r = web.get("/parametres/configuration")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Amorçage et contrôles fail-open" in body
    assert "Hygiène de fork" in body


def test_the_configuration_page_is_get_only():
    """Lecture seule pour toujours : un chemin d'écriture ici contournerait
    les gardes des modules. Épinglé sur l'inventaire des méthodes, pas sur
    l'absence d'un formulaire."""
    app = _app()
    regles = [r for r in app.url_map.iter_rules()
              if r.rule == "/parametres/configuration"]
    assert len(regles) == 1
    assert regles[0].methods <= {"GET", "HEAD", "OPTIONS"}


def test_the_configuration_page_carries_no_htmx_and_no_script():
    """Épinglé sur la SOURCE du gabarit, jamais sur le corps rendu : `base.html`
    porte `hx-headers` sur son `<body>` et ses scripts de pied, donc un
    balayage du rendu mesurerait la base et casserait le jour où la nav gagne
    une pastille htmx.

    Pourquoi la propriété compte : `/parametres` n'est PAS sous un préfixe
    exempt d'App Check (contrairement à `/auth/`), et App Check ne s'applique
    qu'aux requêtes portant `HX-Request` — une page de diagnostic qui
    dépendrait d'un échange htmx pourrait donc se vider en 401 précisément
    sur le déploiement neuf où l'attestation n'est pas encore câblée. Et un
    fragment d'erreur en 4xx ne paraîtrait jamais (htmx n'échange que les
    2xx)."""
    brut = io.open(
        os.path.join(_ATHENA, "templates", "settings", "configuration.html"),
        encoding="utf-8",
    ).read()
    # Les commentaires Jinja PARLENT de la règle (« aucun attribut hx-* ») ;
    # les garder ferait matcher la prose au lieu du code — la faute que trois
    # tests de ce lot ont déjà commise. On les retire, sans regex : une
    # alternance non gourmande sur un corps quelconque est exactement la forme
    # qui a coûté 43 s de ReDoS deux fois à ce dépôt.
    source = "".join(
        bloc.split("#}", 1)[-1] if i else bloc
        for i, bloc in enumerate(brut.split("{#"))
    )
    assert "hx-" not in source
    assert "<script" not in source


def test_the_badge_ignores_fork_hygiene(web, monkeypatch):
    """L'épingle qui compte. Sur le déploiement d'ORIGINE la section hygiène
    porte ~25 avertissements parfaitement normaux ; les compter dans
    l'en-tête apprendrait à ignorer la page, et c'est justement l'attention
    qu'un secret manquant réclame."""
    monkeypatch.setattr(
        rs.config_checks, "run_all",
        lambda *a, **k: _rapport(op_level=OK, fork_rows=25),
    )
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "Configuration opérationnelle saine" in body
    # Mais la section est bien là, avec son propre compte.
    assert "Hygiène de fork" in body
    assert "25 valeurs du propriétaire" in body


def test_an_operational_warning_does_reach_the_badge(web, monkeypatch):
    monkeypatch.setattr(
        rs.config_checks, "run_all",
        lambda *a, **k: _rapport(op_level=WARN, fork_rows=25),
    )
    body = web.get("/parametres/configuration").data.decode("utf-8")
    # La ligne fabriquée doit paraître : sans elle, l'assertion suivante
    # pourrait passer sur le VRAI rapport (en développement il porte des
    # avertissements attendus), et ne prouverait alors rien.
    assert "une ligne opérationnelle" in body
    assert "demandent votre lecture" in body
    assert "Configuration opérationnelle saine" not in body


def test_an_operational_failure_reaches_the_badge(web, monkeypatch):
    monkeypatch.setattr(
        rs.config_checks, "run_all",
        lambda *a, **k: _rapport(op_level=FAIL, fork_rows=0),
    )
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "une ligne opérationnelle" in body
    assert "Au moins un point bloquant" in body


def test_the_fork_section_is_collapsed(web):
    """25 lignes ambre dépliées en permanence, c'est une page qu'on cesse de
    lire."""
    body = web.get("/parametres/configuration").data.decode("utf-8")
    i = body.index("Hygiène de fork")
    assert "<details>" in body[i:]


def test_no_row_detail_is_dumped_into_the_page(web, monkeypatch):
    """Le contrat « shape only » vaut jusqu'au gabarit : il ne rend que
    `detail_fr` et `version`, jamais le dictionnaire entier. Sinon une clé
    ajoutée un jour au rapport paraîtrait à l'écran sans que personne l'ait
    décidé."""
    monkeypatch.setattr(
        rs.config_checks, "run_all",
        lambda *a, **k: _rapport(detail={"secret_value": "SENTINELLE-XYZ",
                                         "version": "7"}),
    )
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "SENTINELLE-XYZ" not in body
    assert "version 7" in body          # la forme, oui


def test_the_page_states_what_it_cannot_see(web):
    """La limite honnête est ÉCRITE sur la page — notamment qu'aucune lecture
    Google Cloud ne peut vérifier la règle Cloudflare, et qu'une page ne se
    consulte que lorsqu'on l'ouvre."""
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "Ce que cette page ne peut pas voir" in body
    assert "Cloudflare" in body
    assert "origin_secret_disabled" in body


# ── La recette sur la page ───────────────────────────────────────────────

def _rapport_secret(secret_id="cf-origin-secret"):
    rpt = Report()
    rpt.section(_cc.SECTION_SECRETS)
    rpt.emit(OK, "une ligne de secret", secret_id=secret_id, version="12")
    return rpt


def test_a_secret_row_carries_its_recipe(web, monkeypatch):
    monkeypatch.setattr(rs.config_checks, "run_all",
                        lambda *a, **k: _rapport_secret())
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "une ligne de secret" in body, "la ligne fabriquée doit paraître"
    assert "Écrire une nouvelle version de ce secret" in body
    assert "gcloud secrets versions add cf-origin-secret" in body
    assert "bash seulement" in body
    assert "Cloud Shell" in body


def test_a_row_whose_secret_id_is_UNKNOWN_renders_no_recipe(web, monkeypatch):
    """La garde qui compte. `detail["secret_id"]` vient d'un rapport ; la
    recherche passe par une table FERMÉE dans la ROUTE, donc une chaîne
    arbitraire ne peut pas devenir une ligne de shell sur une page dont le
    métier entier est « copiez ceci et exécutez-le »."""
    monkeypatch.setattr(
        rs.config_checks, "run_all",
        lambda *a, **k: _rapport_secret("cf-origin-secret; rm -rf /"),
    )
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "une ligne de secret" in body
    assert "Écrire une nouvelle version" not in body
    assert "rm -rf" not in body


def test_a_row_with_no_secret_id_renders_no_recipe(web, monkeypatch):
    """Les lignes d'environnement de la même section n'en portent pas."""
    monkeypatch.setattr(rs.config_checks, "run_all",
                        lambda *a, **k: _rapport(op_level=OK))
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "Écrire une nouvelle version" not in body


def test_the_recipe_is_inert_text(web, monkeypatch):
    """Aucun `|safe` dans le gabarit, et le shell vit dans un `<textarea>`
    en lecture seule : Jinja échappe, le navigateur redécode au parsage, donc
    ce qu'on copie est ce qui a été écrit — sans qu'aucun caractère de la
    commande puisse fermer une balise."""
    brut = io.open(
        os.path.join(_ATHENA, "templates", "settings", "configuration.html"),
        encoding="utf-8",
    ).read()
    # Les commentaires Jinja PARLENT de la règle (« aucun `|safe` ») : les
    # garder ferait matcher la prose au lieu du code — la faute que ce lot a
    # déjà commise quatre fois. Retirés sans regex, comme le test voisin.
    source = "".join(
        bloc.split("#}", 1)[-1] if i else bloc
        for i, bloc in enumerate(brut.split("{#"))
    )
    assert "|safe" not in source and "| safe" not in source

    monkeypatch.setattr(rs.config_checks, "run_all",
                        lambda *a, **k: _rapport_secret())
    body = web.get("/parametres/configuration").data.decode("utf-8")
    debut = body.index("gcloud secrets versions add")
    fragment = body[debut - 400:debut + 200]
    assert "<textarea readonly" in fragment


def test_the_page_says_a_new_version_needs_a_redeploy(web, monkeypatch):
    """La moitié qu'on oublie : `config.py` lit ses secrets dans le corps de
    classe, une fois par processus. Écrire une version ne l'applique PAS —
    mais cette page relit `versions/latest` à chaque affichage, donc elle
    valide la nouvelle version AVANT le redéploiement qui l'active. C'est
    tout ce qui manquait à la boucle."""
    monkeypatch.setattr(rs.config_checks, "run_all",
                        lambda *a, **k: _rapport_secret())
    body = web.get("/parametres/configuration").data.decode("utf-8")
    assert "prochain" in body and "déploiement" in body


def _onglets_declares() -> tuple[list, list]:
    """Les endpoints et les libellés lus DANS le gabarit d'onglets.

    Motifs délibérément linéaires (classes tempérées, aucun quantificateur
    imbriqué) : ce dépôt a payé 43 s de ReDoS deux fois, et un test n'est pas
    dispensé de la règle.
    """
    brut = io.open(
        os.path.join(_ATHENA, "templates", "settings", "_onglets.html"),
        encoding="utf-8",
    ).read()
    endpoints = re.findall(r"url_for\('settings\.([a-z_]+)'\)", brut)
    libelles = [t.strip() for t in re.findall(r">\s*([^<>]+?)\s*</a>", brut)]
    return endpoints, libelles


def test_every_settings_page_carries_every_tab():
    """DÉRIVÉ des deux côtés, et c'est tout l'objet du remplacement.

    L'ancien test s'appelait `..._three_settings_pages_carry_the_three_tabs`
    et énumérait trois chemins et trois libellés À LA MAIN : il était périmé
    depuis l'arrivée du quatrième onglet, et n'avait rien signalé. Passer 3 à
    5 aurait perpétué le défaut au lieu d'en fermer la classe — d'où une
    dérivation : les pages viennent de `url_map`, les libellés du gabarit.
    Un cinquième onglet est alors couvert le jour où il est écrit.
    """
    app = _app()
    endpoints, libelles = _onglets_declares()
    assert endpoints and libelles and len(endpoints) == len(libelles)

    # Les pages : toute règle GET du blueprint « settings ».
    pages = {
        r.endpoint.split(".", 1)[1]: r.rule
        for r in app.url_map.iter_rules()
        if r.endpoint.startswith("settings.")
        and r.methods <= {"GET", "HEAD", "OPTIONS"}
    }
    assert pages, "aucune page de Paramètres ?"

    # Aucun onglet ne pointe une page qui n'existe pas...
    assert set(endpoints) <= set(pages), set(endpoints) - set(pages)
    # ...et aucune page n'est ORPHELINE de la barre d'onglets : c'est le sens
    # que le test périmé avait perdu.
    assert set(pages) == set(endpoints), set(pages) - set(endpoints)

    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    for endpoint, rule in sorted(pages.items()):
        body = client.get(rule).data.decode("utf-8")
        for libelle in libelles:
            assert libelle in body, f"{rule} manque l'onglet « {libelle} »"
