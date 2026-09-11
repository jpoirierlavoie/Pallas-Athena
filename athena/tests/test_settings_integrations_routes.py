"""« Paramètres → Intégrations » — la page, et les BALAYAGES DÉRIVÉS.

Les balayages sont la moitié qui compte. La couture de ce lot repose sur des
paramètres nommés à défaut `None` : un appelant qui en oublie un ne casse
rien, il retombe en SILENCE sur la valeur du déploiement, et la page affiche
alors une valeur que le code n'emploie pas. Un test dérivé — jamais une liste
écrite à la main, la leçon `_PHASED_TOOLS` — transforme cette discipline en
porte de déploiement.
"""

import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.auth_routes as ra
    import routes.settings as rs
    from models import integrations as I

from flask import Flask  # noqa: E402

from security import init_security  # noqa: E402
from utils import integrations_defaults as D  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]


def _app(csrf_on=False):
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ROOT, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["REQUIRE_MFA"] = True
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["RATELIMIT_ENABLED"] = False
    app.config["WTF_CSRF_ENABLED"] = csrf_on
    init_security(app)
    app.jinja_env.globals["ms"] = _ms
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
def _store(monkeypatch):
    """Un magasin en mémoire — jamais un MagicMock (il accepterait ce que
    Firestore refuse)."""
    st: dict = {"state": "absent", "doc": None}

    def _read_state():
        if st["state"] == "unreadable":
            return None, "unreadable"
        if st["doc"] is None:
            return None, "absent"
        return dict(st["doc"]), "ok"

    def _set(payload):
        st["doc"] = dict(payload)

    monkeypatch.setattr(I, "_read_state", _read_state, raising=True)
    monkeypatch.setattr(
        I, "db",
        type("_DB", (), {"collection": lambda self, n: type("_C", (), {
            "document": lambda self2, k: type("_D", (), {
                "set": lambda self3, payload: _set(payload)
            })()
        })()})(),
        raising=True,
    )
    return st


def _valid_form(**over):
    base = {
        "bookings_subject_keywords": "Consultation, Rencontre",
        "bookings_type_par_mot_cle": "consultation = consultation",
        "bookings_type_defaut": "consultation",
        "bookings_sync_lookahead_days": "90",
        "bookings_sync_lookback_days": "1",
        "miroir_outlook_lookahead_days": "365",
        "miroir_outlook_lookback_days": "30",
        "graph_sender_name": "Poirier Lavoie, avocat",
    }
    base.update(over)
    return base


# ── La page ───────────────────────────────────────────────────────────────

def test_anonymous_cannot_reach_it(anon):
    r = anon.get("/parametres/integrations")
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


def test_the_page_renders(web):
    r = web.get("/parametres/integrations")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Détection des rendez-vous Bookings" in body
    assert "Réglages fixés au déploiement" in body
    assert "Coupe-circuits" in body


def test_the_four_tabs_are_on_every_settings_page(web):
    for chemin in ("/parametres/", "/parametres/securite",
                   "/parametres/integrations", "/parametres/configuration"):
        body = web.get(chemin).data.decode("utf-8")
        for onglet in ("Profil du cabinet", "Sécurité", "Intégrations",
                       "Configuration"):
            assert onglet in body, f"{chemin} manque l'onglet {onglet}"


def test_the_page_carries_no_htmx_and_no_script():
    """Épinglé sur la SOURCE, jamais sur le rendu : `base.html` porte
    `hx-headers` et ses scripts de pied. `/parametres` n'est sous aucun
    préfixe exempté d'App Check, et un fragment d'erreur en 4xx ne paraîtrait
    jamais (htmx n'échange que les 2xx)."""
    brut = io.open(
        _ROOT / "templates" / "settings" / "integrations.html", encoding="utf-8"
    ).read()
    source = "".join(
        bloc.split("#}", 1)[-1] if i else bloc
        for i, bloc in enumerate(brut.split("{#"))
    )
    assert "hx-" not in source
    assert "<script" not in source


# ── Le magasin illisible ──────────────────────────────────────────────────

def test_an_unreadable_store_says_so_and_disables_save(web, _store):
    _store["state"] = "unreadable"
    body = web.get("/parametres/integrations").data.decode("utf-8")
    assert "magasin de réglages est illisible" in body
    assert "disabled" in body


def test_the_POST_refuses_server_side_while_unreadable(web, _store):
    """Un bouton désactivé n'est PAS une garde — les outils de développement
    le réactivent. Le refus vit au serveur."""
    _store["state"] = "unreadable"
    r = web.post("/parametres/integrations/enregistrer", data=_valid_form())
    assert r.status_code == 200
    assert "illisible" in r.data.decode("utf-8")
    assert _store["doc"] is None, "une écriture a eu lieu malgré le refus"


# ── Refus et succès ───────────────────────────────────────────────────────

def test_a_refusal_re_renders_at_200_with_the_submitted_values(web):
    r = web.post(
        "/parametres/integrations/enregistrer",
        data=_valid_form(bookings_subject_keywords=""),
    )
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "mot-clé" in body
    # La valeur SOUMISE est réaffichée (ici le nom d'expéditeur), sinon la
    # correction se ferait à l'aveugle.
    assert "Poirier Lavoie, avocat" in body


def test_a_successful_save_redirects_with_ok(web, _store):
    r = web.post("/parametres/integrations/enregistrer", data=_valid_form())
    assert r.status_code == 302
    assert "ok=1" in r.headers["Location"]
    assert _store["doc"] is not None


def test_an_unchecked_box_actually_CLEARS_the_flag(web, _store):
    """Le piège du contrat de présence : une case non cochée n'est pas
    soumise du tout. Si la route lisait `f.get(...)` par présence, il
    deviendrait impossible de DÉCOCHER."""
    web.post("/parametres/integrations/enregistrer",
             data=_valid_form(bookings_debug_payload="on"))
    assert _store["doc"]["bookings_debug_payload"] is True
    web.post("/parametres/integrations/enregistrer", data=_valid_form())
    assert _store["doc"]["bookings_debug_payload"] is False


def test_csrf_is_enforced_on_the_post():
    app = _app(csrf_on=True)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    r = client.post("/parametres/integrations/enregistrer", data=_valid_form())
    assert r.status_code == 400


# ── LES BALAYAGES DÉRIVÉS ─────────────────────────────────────────────────

def test_the_form_posts_exactly_the_editable_fields(web):
    """Un champ de formulaire sans consommateur est une page qui ment ; un
    champ modifiable sans entrée au formulaire est une valeur inatteignable.
    Dérivé de `EDITABLE_FIELDS`, jamais d'une liste recopiée.

    Mesuré sur le HTML RENDU, pas sur la source : les quatre compteurs de
    jours sont émis par une boucle Jinja (`name="{{ key }}"`), donc un
    balayage de la source ne les verrait pas — et conclurait à tort qu'ils
    manquent. Le rendu dit ce que le navigateur postera réellement.
    """
    page = web.get("/parametres/integrations").data.decode("utf-8")
    # Borné au <form> : `base.html` porte un <meta name="viewport"> qui n'a
    # rien à voir avec ce que le navigateur postera.
    # Ancré sur l'ACTION : `base.html` ouvre un formulaire de déconnexion
    # avant le bloc de contenu, donc le premier `<form` de la page n'est pas
    # celui-ci.
    debut = page.index("integrations/enregistrer")
    src = page[debut:page.index("</form>", debut)]
    names = set(re.findall(r'name="([a-z_]+)"', src))
    names.discard("csrf_token")
    assert names == set(D.EDITABLE_FIELDS), (
        f"manquants: {set(D.EDITABLE_FIELDS) - names} ; "
        f"en trop: {names - set(D.EDITABLE_FIELDS)}"
    )


def test_no_editable_value_is_read_through_app_config_or_jinja():
    """`app.config.from_object(Config)` publie ces noms comme un INSTANTANÉ
    d'import. Zéro lecteur aujourd'hui — et cet instantané devient
    activement trompeur maintenant que la valeur d'à côté s'édite. Le
    balayage interdit la voie qui contournerait la couture."""
    suspects = []
    for chemin in list((_ROOT).rglob("*.py")) + list((_ROOT).rglob("*.html")):
        if ".venv" in str(chemin) or "tests" in chemin.parts:
            continue
        txt = io.open(chemin, encoding="utf-8", errors="ignore").read()
        for name in D.EDITABLE_FIELDS:
            up = name.upper()
            for motif in (f'config["{up}"]', f"config['{up}']",
                          f'config.get("{up}"', f"config.get('{up}'",
                          f"config.{up}"):
                if motif in txt:
                    suspects.append(f"{chemin.name}: {motif}")
    assert not suspects, suspects


def test_every_graph_calendrier_call_site_passes_the_keywords():
    """Un site oublié retombe en SILENCE sur la valeur du déploiement."""
    src = io.open(_ROOT / "routes" / "taches_bookings.py", encoding="utf-8").read()
    for fn in ("est_reservation", "extraire", "mot_cle_correspondant"):
        for m in re.finditer(rf"graph_calendrier\.{fn}\(([^)]*)\)", src):
            assert "mots_cles=" in m.group(1), f"{fn}: {m.group(0)}"


def test_every_courriel_caller_passes_the_display_name():
    for rel in ("routes/taches_portail.py", "services/portail_emission.py"):
        src = io.open(_ROOT / rel, encoding="utf-8").read()
        for m in re.finditer(r"courriel\.envoyer\(([^;]*?)\)\n", src, re.S):
            assert "expediteur_nom=" in m.group(1), f"{rel}: {m.group(0)[:120]}"


def test_the_pure_modules_still_import_no_models():
    """La propriété qui porte toute la couture : les quatre modules Graph
    doivent rester importables sans client Firestore. Épinglé sur l'AST."""
    import ast
    for rel in ("utils/graph.py", "utils/graph_calendrier.py",
                "utils/graph_miroir.py", "utils/courriel.py",
                "utils/integrations_defaults.py"):
        tree = ast.parse(io.open(_ROOT / rel, encoding="utf-8").read())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                mod = (node.module or "").split(".")[0]
            elif isinstance(node, ast.Import):
                mod = node.names[0].name.split(".")[0]
            assert mod != "models", f"{rel} importe models (ligne {node.lineno})"
