# -*- coding: utf-8 -*-
"""Le double rendu d'un reCAPTCHA INVISIBLE — les DEUX pages qui en portent un.

Un vérificateur invisible ne nettoie pas son conteneur. Vérifié dans les
octets du SDK vendorisé (`static/vendor/firebase-auth-compat-10.12.2.js`),
trois gardes `isInvisible||` qui se lisent ensemble :

    clear(){…this.isInvisible||this.container.childNodes.forEach(…)}
    validateStartingState(){…isInvisible||!this.container.hasChildNodes…}
    makeRenderPromise: …isInvisible||(t=document.createElement("div"),…)

Donc `clear()` ne retire AUCUN nœud, l'assertion « conteneur déjà sale » est
SAUTÉE, et le rendu se fait SUR le conteneur plutôt que dans un enfant
jetable. Un second `grecaptcha.render()` sur le même élément lève une `Error`
NUE — sans `.code` — donc invisible à `messageErreur`, qui retombe sur un
message générique, ET invisible au journal serveur, qui ne transporte que le
code. La seule réparation est de rendre le nœud VIERGE en le REMPLAÇANT.

**Pourquoi un fichier pour deux pages.** Elles ont été écrites à onze mois
d'intervalle et portent la même construction. Les épingler séparément
accepterait qu'une main future répare l'une et laisse l'autre — ce qui est
exactement l'état trouvé le 2026-09-14 : la page de sécurité échouait en
production (« Impossible d'envoyer le code. Réessayez. », et le journal
Identity Platform sans AUCUN `StartMfaEnrollment` : la requête n'a jamais
quitté le navigateur) pendant que « Renvoyer le code » de la page de
CONNEXION portait le même défaut, latent et plus grave.

⚠ **Ce que ce fichier NE prouve PAS.** Il n'exécute pas le JavaScript — ni
`package.json`, ni jest, ni jsdom, ni Playwright dans ce dépôt. Il épingle
que le remplacement est ÉMIS et qu'il PRÉCÈDE la construction. Que Firebase
accepte le résultat se vérifie à la main : M0 de DEPLOYMENT.md §6.5.2.
"""

import io
import os
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

from flask import Flask  # noqa: E402

from security import init_security  # noqa: E402


# (libellé, url, gabarit, en-tête de la fonction qui reconstruit, id du conteneur)
PAGES = [
    (
        "securite",
        "/parametres/securite",
        "templates/settings/securite.html",
        "reinitRecaptcha()",
        "parametres-recaptcha",
    ),
    (
        "connexion",
        "/auth/login",
        "templates/auth/login.html",
        "initRecaptcha()",
        "mfa-recaptcha-container",
    ),
]

_IDS = [p[0] for p in PAGES]


def _app():
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["REQUIRE_MFA"] = True
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["RATELIMIT_ENABLED"] = False
    app.config["WTF_CSRF_ENABLED"] = False
    # La page de connexion rend `config.RECAPTCHA_ENTERPRISE_SITE_KEY|tojson`
    # INCONDITIONNELLEMENT : sans la clé, `tojson` lève sur `Undefined`.
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = ""
    init_security(app)
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["firebase_api_key"] = "clef-test"
    app.jinja_env.globals["firebase_project_id"] = "test-project"
    app.jinja_env.globals["firebase_app_id"] = "1:2:web:3"
    app.register_blueprint(rs.settings_bp)
    app.register_blueprint(ra.auth_bp)
    return app


def _page(url: str) -> str:
    """La page RENDUE. `/auth/login` se demande SANS session — une session
    valide y est renvoyée au tableau de bord (`routes/auth_routes.py`)."""
    client = _app().test_client()
    if url.startswith("/parametres/"):
        with client.session_transaction() as s:
            s["user_id"] = "u1"
            s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    r = client.get(url)
    assert r.status_code == 200, (url, r.status_code)
    return r.get_data(as_text=True)


def _code_de_la_fonction(html: str, entete: str) -> str:
    """Le CODE du corps de `entete`, commentaires de ligne RETIRÉS.

    Les commentaires partent d'abord, pour deux raisons distinctes. Les
    nôtres CITENT les octets du SDK, accolades comprises, donc le comptage
    d'accolades ne doit pas dépendre de leur équilibre par chance. Et
    surtout : une épingle qui se satisferait d'une PHRASE parlant du
    remplacement au lieu de l'appel serait une décoration — c'est le piège
    du `tr -d`, rencontré quatre fois dans ce dépôt, où une épingle mesurait
    la prose expliquant le défaut plutôt que la chose épinglée.

    Le corps se délimite par COMPTAGE d'accolades et non par une borne
    d'indentation : le `if (…) { … }` qui précède la construction fermerait
    AVANT elle, si bien qu'une borne naïve mesurerait un fragment ne
    contenant ni le remplacement ni la construction — et passerait sur la
    mutation même qu'elle vise.
    """
    reste = html[html.index(entete):]
    reste = chr(10).join(
        ligne.split("//")[0] for ligne in reste.split(chr(10))
    )
    debut = reste.index("{")
    profondeur = 0
    for k in range(debut, len(reste)):
        if reste[k] == "{":
            profondeur += 1
        elif reste[k] == "}":
            profondeur -= 1
            if profondeur == 0:
                return reste[debut:k + 1]
    raise AssertionError("accolade jamais refermee apres " + entete)


@pytest.mark.parametrize("page,url,gabarit,entete,conteneur", PAGES, ids=_IDS)
def test_the_rebuild_REPLACES_the_container_BEFORE_rendering(
    page, url, gabarit, entete, conteneur
):
    """`clear()` ne suffit pas, et l'ordre est la moitié de la réparation :
    remplacer APRÈS avoir construit laisserait le rendu sur le nœud sale."""
    corps = _code_de_la_fonction(_page(url), entete)

    assert "replaceChild" in corps, (
        page + " : le conteneur est REUTILISE. Un reCAPTCHA invisible ne "
        "nettoie pas le sien, donc le second rendu leve une Error sans code."
    )
    assert "createElement('div')" in corps, page

    i_remplace = corps.index("replaceChild")
    i_rendu = corps.index("RecaptchaVerifier")
    assert i_remplace < i_rendu, (
        page + " : le remplacement SUIT la construction — donc le rendu se "
        "fait encore sur le noeud sale, et le noeud neuf est deja obsolete."
    )


@pytest.mark.parametrize("page,url,gabarit,entete,conteneur", PAGES, ids=_IDS)
def test_the_fresh_container_keeps_the_SAME_id(
    page, url, gabarit, entete, conteneur
):
    """Un nœud neuf sans son identifiant est un nœud que plus rien ne
    retrouve : le rendu suivant chercherait un conteneur absent, et le SDK
    lèverait `auth/argument-error` à chaque envoi."""
    html = _page(url)
    corps = _code_de_la_fonction(html, entete)

    assert (".id = '" + conteneur + "'") in corps, (
        page + " : le conteneur neuf ne reprend pas l'identifiant "
        + conteneur
    )
    assert html.count('id="' + conteneur + '"') == 1, (
        page + " : le gabarit ne porte pas EXACTEMENT un conteneur "
        + conteneur + " — la resolution se fait par identifiant, donc un "
        "second serait silencieusement ignore."
    )


def test_EVERY_template_constructing_a_verifier_is_pinned_HERE():
    """L'épingle qui ferme la CLASSE plutôt que ses deux instances.

    Les deux pages actuelles ont été écrites à onze mois d'intervalle et
    portaient le même défaut. Une troisième copierait la construction sans
    le remplacement, et rien ne le dirait — la page marcherait au premier
    envoi, puis échouerait sur le second sans code d'erreur exploitable.
    """
    portent = set()
    for dossier, _, fichiers in os.walk(os.path.join(_ATHENA, "templates")):
        for fichier in fichiers:
            if not fichier.endswith(".html"):
                continue
            chemin = os.path.join(dossier, fichier)
            if "RecaptchaVerifier" in io.open(chemin, encoding="utf-8").read():
                portent.add(
                    os.path.relpath(chemin, _ATHENA).replace(os.sep, "/")
                )

    attendus = {p[2] for p in PAGES}
    assert portent == attendus, (
        "un gabarit construit un RecaptchaVerifier sans etre epingle ici : "
        + repr(sorted(portent - attendus))
        + " ; ou un gabarit epingle n'en construit plus : "
        + repr(sorted(attendus - portent))
    )
