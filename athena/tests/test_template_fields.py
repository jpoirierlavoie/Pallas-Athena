"""Tests for utils/template_fields.py — catalog, aliases, resolution."""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.template_fields import (
    CATALOG,
    FLAT_ALIASES,
    MANUAL_FIELDS,
    Classification,
    classify_placeholders,
    fallback_value,
    french_long_date,
    is_block_name,
    resolve_values,
    salutations_default,
)

TODAY = date(2026, 4, 25)

FIRM = {
    "nom": "Me Jason Poirier Lavoie",
    "adresse_civique": "450 rue Sainte-Catherine Ouest, bureau 300",
    "ville": "Montréal",
    "province": "Québec",
    "code_postal": "H3B 1A1",
    "telephone": "+1 (514) 555-0000",
    "courriel": "jason@poirierlavoie.ca",
}


def _dossier(**overrides) -> dict:
    base = {
        "id": "d1",
        "title": "Tremblay c. Lavoie",
        "file_number": "2026-042",
        "court_file_number": "500-05-123456-241",
        "tribunal": "Cour supérieure",
        "competence": "Chambre civile",
        "district_judiciaire": "Montréal",
        "palais_de_justice": "Montréal",
        "role": "demandeur",
        "clients": [{"id": "p1", "name": "Jean Tremblay"}],
        "opposing_parties": [{"id": "p2", "name": "Marc Lavoie"}],
    }
    base.update(overrides)
    return base


def _individu(**overrides) -> dict:
    base = {
        "id": "p1",
        "type": "individual",
        "contact_role": "client",
        "prefix": "",
        "first_name": "Jean",
        "last_name": "Tremblay",
        "gender": "",
        "organization": "",
        "organization_name": "",
        "email": "jean@example.com",
        "email_work": "jean@travail.com",
        "phone_home": "",
        "phone_cell": "+15145551234",
        "phone_work": "",
        "address_street": "12 rue Principale",
        "address_unit": "",
        "address_city": "Montréal",
        "address_province": "Québec",
        "address_postal_code": "H2X 1Y6",
        "address_country": "Canada",
        "work_address_street": "",
        "work_address_unit": "",
        "work_address_city": "",
        "work_address_province": "",
        "work_address_postal_code": "",
        "work_address_country": "",
        "bar_number": "",
    }
    base.update(overrides)
    return base


def _avocat(**overrides) -> dict:
    base = _individu(
        id="p3",
        contact_role="avocat_adverse",
        prefix="Me",
        first_name="Claire",
        last_name="Dubois",
        email="claire@perso.com",
        email_work="cdubois@cabinet.ca",
        phone_work="+15145550001",
        organization="Dubois Avocats inc.",
        work_address_street="1000 boul. René-Lévesque",
        work_address_unit="bureau 2200",
        work_address_city="Montréal",
        work_address_province="Québec",
        work_address_postal_code="H3B 4W5",
        work_address_country="Canada",
        bar_number="123456",
    )
    base.update(overrides)
    return base


def _resolve(names, **kwargs):
    defaults = dict(dossier=None, client=None, adverse=None,
                    destinataire=None, firm=FIRM, today=TODAY)
    defaults.update(kwargs)
    return resolve_values(names, **defaults)


# ── Alias table (§6.6 — every row) ──────────────────────────────────────

_ALIAS_EXPECTATIONS = {
    "district": "Montréal",
    "numero_dossier": "500-05-123456-241",
    "tribunal": "Cour supérieure",
    "chambre": "Chambre civile",
    "référence_interne": "2026-042",
    "intitulé_dossier": "Tremblay c. Lavoie",
    "rôle": "demanderesse",
    "demandeur": "Jean Tremblay",
    "défendeur": "Marc Lavoie",
    "adresse_demandeur": "12 rue Principale, Montréal (Québec) H2X 1Y6",
    "adresse_défendeur": "1000 boul. René-Lévesque, bureau 2200, Montréal (Québec) H3B 4W5",
    "ville_procédure": "Montréal",
    "ville_lettre": "Montréal",
    "date_procédure": "25 avril 2026",
    "date_lettre": "25 avril 2026",
    "civilité_récipient": "Maître",
    "civilité": "Maître",
    "prénom_récipient": "Claire",
    "nom_récipient": "Dubois",
    "cabinet_récipient": "Dubois Avocats inc.",
    "adresse_civique_récipient": "1000 boul. René-Lévesque, bureau 2200",
    "ville_récipient": "Montréal",
    "province_récipient": "Québec",
    "code_postal_récipient": "H3B 4W5",
    "pays_récipient": "Canada",
}


def test_every_alias_row_maps_to_a_catalog_field():
    for flat, canonical in FLAT_ALIASES.items():
        assert canonical in CATALOG, f"{flat} -> {canonical} missing from CATALOG"


def test_alias_resolution_full_table():
    avocat = _avocat()
    resolved = _resolve(
        list(_ALIAS_EXPECTATIONS),
        dossier=_dossier(),
        client=_individu(),
        adverse=avocat,
        destinataire=avocat,
    )
    for flat, expected in _ALIAS_EXPECTATIONS.items():
        assert resolved.get(flat) == expected, flat


# ── Role derivation (§6.2) ──────────────────────────────────────────────

def test_position_swap_when_role_is_defendeur():
    resolved = _resolve(
        ["demandeur", "défendeur", "adresse_demandeur"],
        dossier=_dossier(role="défendeur"),
        client=_individu(),
        adverse=_avocat(),
    )
    # Our client is now the défendeur; the opposing side is the demandeur.
    assert resolved["défendeur"] == "Jean Tremblay"
    assert resolved["demandeur"] == "Marc Lavoie"
    # Demandeur-side address comes from the adverse slot partie.
    assert resolved["adresse_demandeur"].startswith("1000 boul. René-Lévesque")


def test_role_feminin_map_and_autre_unresolved():
    for role, expected in [
        ("demandeur", "demanderesse"),
        ("défendeur", "défenderesse"),
        ("intervenant", "intervenante"),
        ("mis en cause", "mise en cause"),
    ]:
        resolved = _resolve(["rôle"], dossier=_dossier(role=role))
        assert resolved["rôle"] == expected
    assert "rôle" not in _resolve(["rôle"], dossier=_dossier(role="autre"))


def test_positions_unresolved_for_role_autre():
    resolved = _resolve(
        ["demandeur", "défendeur"],
        dossier=_dossier(role="autre"),
        client=_individu(),
        adverse=_avocat(),
    )
    assert "demandeur" not in resolved
    assert "défendeur" not in resolved


# ── Civilité / organisation (§6.3) ──────────────────────────────────────

def test_civilite_from_prefix_and_gender():
    assert _resolve(["destinataire.civilite"],
                    destinataire=_individu(prefix="Mme"))["destinataire.civilite"] == "Madame"
    assert _resolve(["destinataire.civilite"],
                    destinataire=_individu(prefix="M."))["destinataire.civilite"] == "Monsieur"
    assert _resolve(["destinataire.civilite"],
                    destinataire=_individu(prefix="", gender="F"))["destinataire.civilite"] == "Madame"
    assert _resolve(["destinataire.civilite"],
                    destinataire=_individu(prefix="", gender="M"))["destinataire.civilite"] == "Monsieur"
    # Neither prefix nor recognized gender → unresolved.
    assert "destinataire.civilite" not in _resolve(
        ["destinataire.civilite"], destinataire=_individu(prefix="", gender="")
    )


def test_organization_partie_prenom_unresolved():
    org = _individu(
        type="organization",
        organization_name="9123-4567 Québec inc.",
        first_name="", last_name="",
    )
    resolved = _resolve(
        ["destinataire.prenom", "destinataire.nom",
         "destinataire.nom_complet", "destinataire.organisation"],
        destinataire=org,
    )
    assert "destinataire.prenom" not in resolved
    assert "destinataire.nom" not in resolved
    assert resolved["destinataire.nom_complet"] == "9123-4567 Québec inc."
    assert resolved["destinataire.organisation"] == "9123-4567 Québec inc."


def test_organisation_prefers_employment_over_legal_name():
    p = _individu(organization="Cabinet Untel", organization_name="Untel inc.")
    assert _resolve(["destinataire.organisation"], destinataire=p)[
        "destinataire.organisation"
    ] == "Cabinet Untel"


# ── Address rules (§6.4) ────────────────────────────────────────────────

def test_one_line_address_full_names_and_unit():
    resolved = _resolve(["client.adresse_complete"],
                        client=_individu(address_unit="app. 4"))
    assert resolved["client.adresse_complete"] == (
        "12 rue Principale, app. 4, Montréal (Québec) H2X 1Y6"
    )


def test_foreign_country_appended():
    p = _individu(address_country="France", address_province="",
                  address_postal_code="75001")
    resolved = _resolve(["client.adresse_complete"], client=p)
    assert resolved["client.adresse_complete"].endswith(", France")


def test_work_address_preferred_for_avocat_adverse_and_courriel_follows():
    avocat = _avocat()
    resolved = _resolve(
        ["destinataire.adresse_complete", "destinataire.courriel",
         "destinataire.ville"],
        destinataire=avocat,
    )
    assert resolved["destinataire.adresse_complete"].startswith(
        "1000 boul. René-Lévesque"
    )
    assert resolved["destinataire.courriel"] == "cdubois@cabinet.ca"

    # Same lawyer without a work address → personal address + email.
    sans_bureau = _avocat(work_address_street="")
    resolved = _resolve(
        ["destinataire.adresse_complete", "destinataire.courriel"],
        destinataire=sans_bureau,
    )
    assert resolved["destinataire.adresse_complete"].startswith("12 rue Principale")
    assert resolved["destinataire.courriel"] == "claire@perso.com"


def test_client_role_never_prefers_work_address():
    p = _individu(work_address_street="99 rue Bureau", work_address_city="Laval")
    resolved = _resolve(["client.ville"], client=p)
    assert resolved["client.ville"] == "Montréal"


def test_telephone_preference_work_then_cell_then_home():
    p = _individu(phone_work="+15145550001", phone_cell="+15145550002")
    assert "555-0001" in _resolve(["client.telephone"], client=p)["client.telephone"]
    p = _individu(phone_work="", phone_cell="+15145550002")
    assert "555-0002" in _resolve(["client.telephone"], client=p)["client.telephone"]


# ── resolve_values omission + firm/date ─────────────────────────────────

def test_resolve_omits_empty_source_fields():
    resolved = _resolve(
        ["numero_dossier", "tribunal", "client.numero_barreau"],
        dossier=_dossier(court_file_number="", tribunal="Cour supérieure"),
        client=_individu(bar_number=""),
    )
    assert "numero_dossier" not in resolved
    assert "client.numero_barreau" not in resolved
    assert resolved["tribunal"] == "Cour supérieure"


def test_no_dossier_leaves_dossier_fields_unresolved():
    resolved = _resolve(["tribunal", "ville_lettre", "date_lettre"])
    assert "tribunal" not in resolved
    assert resolved["ville_lettre"] == "Montréal"
    assert resolved["date_lettre"] == "25 avril 2026"


def test_french_long_date_first_of_month():
    assert french_long_date(date(2026, 5, 1)) == "1er mai 2026"
    assert french_long_date(date(2026, 12, 25)) == "25 décembre 2026"


def test_date_iso():
    assert _resolve(["date.aujourdhui_iso"])["date.aujourdhui_iso"] == "2026-04-25"


# ── Classification (§6.8) ───────────────────────────────────────────────

def test_classification_buckets_and_slots():
    names = [
        "tribunal",                # alias → dossier slot
        "civilité_récipient",      # alias → destinataire slot
        "client.nom_complet",      # canonical → client slot
        "date_lettre",             # alias → no slot
        "objet_lettre",            # known manual
        "FAITS",                   # block
        "LISTE_PIÈCES",            # block with accented uppercase
        "champ_mystère",           # unknown
    ]
    c = classify_placeholders(names)
    assert c.auto["tribunal"] == "dossier.tribunal"
    assert c.auto["civilité_récipient"] == "destinataire.civilite"
    assert c.auto["client.nom_complet"] == "client.nom_complet"
    assert c.auto["date_lettre"] == "date.aujourdhui"
    assert c.manual_scalar == ["objet_lettre"]
    assert c.blocks == ["FAITS", "LISTE_PIÈCES"]
    assert c.unknown == ["champ_mystère"]
    assert c.slots_required == {"dossier", "client", "destinataire"}


def test_cabinet_and_date_require_no_slot():
    c = classify_placeholders(["cabinet.nom", "date.aujourdhui", "pièces_jointes"])
    assert c.slots_required == set()


def test_is_block_name_convention():
    assert is_block_name("FAITS")
    assert is_block_name("CONTENU_LETTRE")
    assert is_block_name("LISTE_PIÈCES")
    assert not is_block_name("tribunal")
    assert not is_block_name("dossier.titre")
    assert not is_block_name("123")  # no letters


# ── Missing-value strings (§6.7 — exact) ────────────────────────────────

def test_fallback_value_exact_strings():
    assert fallback_value("numero_dossier", is_auto=True) == (
        "[CHAMP MANQUANT : numero_dossier]"
    )
    assert fallback_value("FAITS", is_auto=False) == "[À COMPLÉTER : FAITS]"
    assert fallback_value("champ_mystère", is_auto=False) == (
        "[À COMPLÉTER : champ_mystère]"
    )


def test_salutations_default():
    assert salutations_default("Maître") == (
        "Veuillez agréer, Maître, l'expression de mes salutations distinguées"
    )
    assert "Madame, Monsieur" in salutations_default(None)


def test_manual_fields_defaults():
    assert MANUAL_FIELDS["pièces_jointes"]["default"] == "Aucune"
    assert "SOUS TOUTES RÉSERVES" in MANUAL_FIELDS["privilège"]["options"]
    assert "courriel" in MANUAL_FIELDS["transmission_lettre"]["options"]
