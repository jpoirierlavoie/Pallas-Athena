"""L'adresse d'une personne morale — ce que la FICHE et le FORMULAIRE montrent.

Le formulaire de contact masque le bloc « Coordonnées personnelles » pour une
entreprise : l'adresse d'une personne morale saisie côté juriste ne peut vivre
que dans ``work_address_*``. La fiche affichait donc en permanence une carte
personnelle vide, et rien n'indiquait au juriste où saisir cette adresse.

Rendu RÉEL du gabarit de fiche (la leçon du 2026-08-13 : épingler ce que le
navigateur reçoit, pas la source) ; le formulaire, lui, bascule ses intitulés
côté client via Alpine — un rendu serveur ne peut qu'attester la présence des
deux libellés, ce que fait l'épingle de source correspondante.
"""

import os
import sys

import pytest
from flask import Flask
from markupsafe import Markup

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")


@pytest.fixture()
def env():
    """Un Jinja qui rend les gabarits de contact hors de toute route : url_for
    est bouchonné (les endpoints vivent dans des blueprints qui importeraient
    Firestore), les filtres et globaux maison sont ceux de main.py."""
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.globals["url_for"] = lambda *a, **k: "#"
    app.jinja_env.globals["csp_nonce"] = ""
    app.jinja_env.filters["phone"] = lambda v: v or ""
    app.jinja_env.filters["to_mtl"] = lambda v: v
    return app


def _partie(**over) -> dict:
    base = {
        "id": "p1", "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay", "prefix": "",
        "organization_name": "", "trade_name": "", "organization": "",
        "email": "", "email_work": "", "phone_cell": "", "phone_home": "",
        "phone_work": "", "fax": "", "job_title": "", "job_role": "",
        "address_street": "", "address_unit": "", "address_city": "",
        "address_province": "", "address_postal_code": "", "address_country": "",
        "work_address_street": "", "work_address_unit": "",
        "work_address_city": "", "work_address_province": "",
        "work_address_postal_code": "", "work_address_country": "",
        "notes": "", "mandataires": [], "kyc_document_ids": [],
        "identity_verified": "non_vérifié", "conflict_check": "non_vérifié",
        "bar_number": "", "company_neq": "", "language": "", "gender": "",
        "pronouns": "", "governing_law": "", "birth_date": None,
        "created_at": None, "updated_at": None,
    }
    base.update(over)
    return base


_MORALE = dict(
    type="organization",
    organization_name="Constructions Beaubien inc.",
    first_name="", last_name="",
    work_address_street="500 rue Beaubien Est",
    work_address_unit="bureau 300",
    work_address_city="Montréal",
    work_address_province="Québec",
    work_address_postal_code="H2S 1S5",
    work_address_country="Canada",
)


def _fiche(env, partie: dict) -> str:
    with env.test_request_context("/parties/p1"):
        from flask import render_template

        return render_template(
            "parties/detail.html", partie=partie, dossiers=[], mandataires=[],
            mandataire_kind_labels={}, role_labels={}, type_labels={},
        )


def test_la_fiche_d_une_entreprise_montre_son_adresse_et_pas_de_carte_vide(env):
    html = _fiche(env, _partie(**_MORALE))
    assert "500 rue Beaubien Est" in html
    assert "Montréal (Québec) H2S 1S5" in html
    # La carte personnelle disparaît — plus de « Aucune coordonnée
    # personnelle » permanent sur chaque entreprise.
    assert "Aucune coordonnée personnelle" not in html
    assert "Coordonnées personnelles" not in html
    assert "Coordonnées de l'entreprise" in html


def test_la_fiche_d_un_individu_garde_ses_deux_cartes(env):
    """Non-régression : rien ne change pour une personne physique."""
    html = _fiche(env, _partie(
        address_street="12 rue Principale", address_city="Montréal",
        address_province="Québec", address_postal_code="H2X 1Y6",
        address_country="Canada",
    ))
    assert "Coordonnées personnelles" in html
    assert "Coordonnées professionnelles" in html
    assert "Coordonnées de l'entreprise" not in html
    assert "12 rue Principale" in html


def test_une_entreprise_du_portail_garde_sa_carte_personnelle(env):
    """Le portail écrit l'adresse d'une entreprise dans address_* : la carte
    reparaît dès qu'une donnée y vit — ne jamais rendre une donnée
    inatteignable en la masquant."""
    html = _fiche(env, _partie(
        **{**_MORALE,
           "work_address_street": "", "work_address_unit": "",
           "work_address_city": "", "work_address_postal_code": "",
           "address_street": "12 rue Principale", "address_city": "Montréal",
           "address_province": "Québec", "address_postal_code": "H2X 1Y6",
           "address_country": "Canada"},
    ))
    assert "Coordonnées personnelles" in html
    assert "12 rue Principale" in html


# ── Le formulaire : les intitulés basculent côté client (Alpine) ─────────


def _spans_du_formulaire() -> list[tuple[str, str, bool]]:
    """(condition x-show, texte, x-cloak présent) de chaque <span> d'intitulé
    du formulaire — la paire condition↔libellé, pas seulement sa présence."""
    import re

    src = open(
        os.path.join(_ATHENA, "templates", "parties", "form.html"),
        encoding="utf-8",
    ).read()
    out = []
    for balise, texte in re.findall(r"<span (x-show=[^>]*)>([^<]+)</span>", src):
        condition = balise.split('x-show="', 1)[1].split('"', 1)[0]
        out.append((condition, texte.strip(), "x-cloak" in balise))
    return out


def test_le_formulaire_nomme_le_bloc_selon_le_type():
    """Épingle la PAIRE condition↔libellé : une inversion des deux conditions
    laisserait passer un simple « le texte existe quelque part » (revue
    2026-08-14). Le texte français vit dans le CONTENU de l'élément, jamais
    dans une expression JS — « l'entreprise » porte une apostrophe qui
    terminerait la chaîne dans l'attribut (leçon jsattr du 2026-08-13)."""
    spans = _spans_du_formulaire()
    par_texte = {texte: (condition, cloak) for condition, texte, cloak in spans}

    for texte in ("Coordonnées de l'entreprise", "Adresse"):
        condition, cloak = par_texte[texte]
        assert condition == "partieType === 'organization'", texte
        # x-cloak : sans lui, les DEUX libellés s'affichent au premier rendu,
        # avant qu'Alpine ne démarre.
        assert cloak, f"x-cloak manquant sur « {texte} »"

    for texte in ("Coordonnées professionnelles", "Adresse professionnelle"):
        condition, _cloak = par_texte[texte]
        assert condition == "partieType !== 'organization'", texte

    # Aucune apostrophe française à l'intérieur d'une expression Alpine.
    for condition, _texte, _cloak in spans:
        assert "'" not in condition.replace("'organization'", "").replace(
            "'individual'", ""
        )


# ── L'export des contacts (revue 2026-08-14) ────────────────────────────


def test_l_export_des_contacts_lit_l_adresse_retenue(monkeypatch):
    """Les colonnes lisaient email/address_city bruts : une personne morale
    s'exportait sans ville NI courriel. Elles lisent désormais l'autorité
    partagée (_ville / _courriel dérivés, motif de _display_name)."""
    from unittest import mock

    with mock.patch("google.cloud.firestore.Client"):
        import routes.parties as rp

    monkeypatch.setattr(rp, "list_parties", lambda **kw: [_partie(**_MORALE)])
    app = Flask(__name__)
    with app.test_request_context("/parties/export/csv"):
        rows = rp._get_export_parties()

    assert rows[0]["_ville"] == "Montréal"
    assert rows[0]["_courriel"] == ""          # cette fixture n'a pas de courriel
    assert rows[0]["_display_name"] == "Constructions Beaubien inc."
    # Les colonnes pointent bien sur les clés dérivées.
    cles_csv = [k for k, _label in rp._EXPORT_COLUMNS_CSV]
    assert "_ville" in cles_csv and "_courriel" in cles_csv
    assert "address_city" not in cles_csv and "email" not in cles_csv
