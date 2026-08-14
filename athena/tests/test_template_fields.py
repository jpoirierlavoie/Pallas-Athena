"""Tests for utils/template_fields.py — catalog, aliases, resolution."""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.template_fields import (
    CATALOG,
    FLAT_ALIASES,
    MANUAL_FIELDS,
    classify_placeholders,
    fallback_value,
    french_long_date,
    is_uppercase_name,
    resolve_values,
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


def test_format_honoraires_parts_splits_label_and_rate():
    from utils.template_fields import format_honoraires, format_honoraires_parts

    nbsp = " "
    d = _dossier(fee_type="hourly", hourly_rate=25000)
    assert format_honoraires_parts(d) == ("Horaire", f"250,00{nbsp}$/h")
    # Rate-less type → empty rate part (the card shows the label alone).
    assert format_honoraires_parts(_dossier(fee_type="pro_bono")) == ("Pro bono", "")
    assert format_honoraires_parts(_dossier(fee_type="")) is None
    # The joined gabarit form is exactly label — rate (or the bare label).
    assert format_honoraires(d) == f"Horaire — 250,00{nbsp}$/h"


def test_sommaire_resolves_namespaced_and_flat():
    d = _dossier(sommaire="Réclamation pour vices cachés.")
    r = _resolve(["dossier.sommaire", "sommaire"], dossier=d)
    assert r["dossier.sommaire"] == "Réclamation pour vices cachés."
    assert r["sommaire"] == "Réclamation pour vices cachés."
    # Absent/empty on legacy dossiers → unresolved (empty popup input).
    assert "sommaire" not in _resolve(["sommaire"], dossier=_dossier())


def test_positions_unresolved_for_role_autre():
    resolved = _resolve(
        ["demandeur", "défendeur"],
        dossier=_dossier(role="autre"),
        client=_individu(),
        adverse=_avocat(),
    )
    assert "demandeur" not in resolved
    assert "défendeur" not in resolved


# ── Names: bare by default, "_avec_civilite" twin keeps the honorific ────

def test_positions_bare_by_default_and_avec_civilite_twin():
    # The snapshot name carries the honorific (display_name prepends prefix);
    # the position fields render bare, the twin keeps it.
    d = _dossier(
        role="demandeur",
        clients=[{"id": "p1", "name": "M. Jean Tremblay"}],
        opposing_parties=[{"id": "p2", "name": "Me Claire Dubois"}],
    )
    r = _resolve(
        ["dossier.demandeur", "dossier.demandeur_avec_civilite",
         "dossier.defendeur", "dossier.defendeur_avec_civilite"],
        dossier=d,
    )
    assert r["dossier.demandeur"] == "Jean Tremblay"
    assert r["dossier.demandeur_avec_civilite"] == "M. Jean Tremblay"
    assert r["dossier.defendeur"] == "Claire Dubois"
    assert r["dossier.defendeur_avec_civilite"] == "Me Claire Dubois"


def test_nom_complet_bare_by_default_and_avec_civilite_twin():
    r = _resolve(
        ["destinataire.nom_complet", "destinataire.nom_complet_avec_civilite"],
        destinataire=_avocat(),  # prefix "Me"
    )
    assert r["destinataire.nom_complet"] == "Claire Dubois"
    assert r["destinataire.nom_complet_avec_civilite"] == "Me Claire Dubois"


def test_avec_civilite_accented_and_flat_spellings_resolve():
    d = _dossier(role="demandeur", clients=[{"id": "1", "name": "M. Jean Tremblay"}])
    r = _resolve(
        ["demandeur_avec_civilité", "demandeur_avec_civilite",
         "destinataire.nom_complet_avec_civilité"],
        dossier=d, destinataire=_avocat(),
    )
    assert r["demandeur_avec_civilité"] == "M. Jean Tremblay"
    assert r["demandeur_avec_civilite"] == "M. Jean Tremblay"
    assert r["destinataire.nom_complet_avec_civilité"] == "Me Claire Dubois"


def test_side_names_strip_per_name_and_leave_org_untouched():
    d = _dossier(
        role="demandeur",
        clients=[{"id": "1", "name": "Mme Marie Roy"},
                 {"id": "2", "name": "9123-4567 Québec inc."}],
        opposing_parties=[{"id": "3", "name": "Marc Lavoie"}],
    )
    assert _resolve(["demandeur"], dossier=d)["demandeur"] == (
        "Marie Roy, 9123-4567 Québec inc."
    )
    assert _resolve(["dossier.demandeur_avec_civilite"], dossier=d)[
        "dossier.demandeur_avec_civilite"
    ] == "Mme Marie Roy, 9123-4567 Québec inc."


# ── Civilité / organisation (§6.3) ──────────────────────────────────────

def test_civilite_is_passthrough_not_resolved():
    # Civilité must be placed and filled by the user (letters yes, court
    # procedures no) — it is never auto-resolved, whatever the spelling.
    avocat = _avocat()
    resolved = _resolve(
        ["civilité", "civilité_récipient", "destinataire.civilite", "CIVILITÉ"],
        destinataire=avocat,
    )
    assert resolved == {}


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


def test_client_prefers_personal_address_when_it_exists():
    """La préférence par rôle décide du bloc ESSAYÉ EN PREMIER — un client
    garde « personnelle d'abord » même lorsqu'une adresse de travail existe.
    (Renommé le 2026-08-14 : l'assertion est inchangée, mais l'invariant
    n'est plus « jamais le travail » — c'est « le personnel s'il existe ».)"""
    p = _individu(work_address_street="99 rue Bureau", work_address_city="Laval")
    resolved = _resolve(["client.ville"], client=p)
    assert resolved["client.ville"] == "Montréal"


# ── Repli entre les deux blocs (2026-08-14) ─────────────────────────────
#
# Le formulaire de contact masque le bloc PERSONNEL pour une personne morale :
# l'adresse d'une entreprise saisie côté juriste ne peut vivre que dans
# work_address_*, alors que le portail écrit celle d'une entreprise dans
# address_*. Sans repli, une note d'honoraires pour une personne morale
# imprimait « [CHAMP MANQUANT : destinataire.adresse_complete] ».


def _personne_morale(**overrides) -> dict:
    """Une entreprise telle que le FORMULAIRE JURISTE la produit : adresse et
    courriel dans le bloc professionnel, bloc personnel vide (masqué)."""
    base = _individu(
        id="p9",
        type="organization",
        organization_name="Constructions Beaubien inc.",
        first_name="", last_name="", prefix="",
        email="", email_work="info@beaubien.ca",
        address_street="", address_city="", address_province="",
        address_postal_code="", address_country="",
        work_address_street="500 rue Beaubien Est",
        work_address_unit="bureau 300",
        work_address_city="Montréal",
        work_address_province="Québec",
        work_address_postal_code="H2S 1S5",
        work_address_country="Canada",
    )
    base.update(overrides)
    return base


def test_organization_address_falls_back_to_the_work_block():
    """LE bogue signalé : le destinataire d'une note d'honoraires."""
    resolved = _resolve(
        ["destinataire.adresse_complete", "destinataire.adresse_civique",
         "destinataire.ville", "destinataire.code_postal",
         "destinataire.courriel", "destinataire.nom_complet"],
        destinataire=_personne_morale(),
    )
    assert resolved["destinataire.adresse_complete"] == (
        "500 rue Beaubien Est, bureau 300, Montréal (Québec) H2S 1S5"
    )
    assert resolved["destinataire.adresse_civique"] == "500 rue Beaubien Est, bureau 300"
    assert resolved["destinataire.ville"] == "Montréal"
    assert resolved["destinataire.code_postal"] == "H2S 1S5"
    # Le courriel suit le bloc retenu — corrigé par effet de bord.
    assert resolved["destinataire.courriel"] == "info@beaubien.ca"
    assert resolved["destinataire.nom_complet"] == "Constructions Beaubien inc."


def test_organization_from_the_portal_keeps_the_personal_block():
    """Le portail écrit l'adresse d'une entreprise dans address_* : le repli
    joue dans les DEUX sens, jamais une règle fondée sur le type."""
    org = _personne_morale(
        address_street="12 rue Principale", address_city="Montréal",
        address_province="Québec", address_postal_code="H2X 1Y6",
        address_country="Canada", email="info@portail.ca",
        work_address_street="", work_address_unit="", work_address_city="",
        work_address_province="", work_address_postal_code="",
    )
    resolved = _resolve(
        ["destinataire.adresse_complete", "destinataire.courriel"],
        destinataire=org,
    )
    assert resolved["destinataire.adresse_complete"].startswith("12 rue Principale")
    assert resolved["destinataire.courriel"] == "info@portail.ca"


def test_individual_client_with_only_a_work_address_falls_back_too():
    """Le cas symétrique, réel lui aussi : un client qui n'a donné que son
    adresse de bureau."""
    p = _individu(
        address_street="", address_city="", address_postal_code="",
        work_address_street="99 rue Bureau", work_address_city="Laval",
        work_address_province="Québec", work_address_postal_code="H7N 1A1",
    )
    resolved = _resolve(["client.adresse_complete", "client.ville"], client=p)
    assert resolved["client.adresse_complete"].startswith("99 rue Bureau")
    assert resolved["client.ville"] == "Laval"


def test_a_city_only_block_still_feeds_the_component_fields():
    """Aucun bloc n'a de rue : le second critère (la ville) évite de tout
    perdre — adresse_complete reste absente (elle exige une rue)."""
    p = _individu(
        address_street="", address_city="", address_postal_code="",
        work_address_street="", work_address_city="Laval",
    )
    resolved = _resolve(["client.ville", "client.adresse_complete"], client=p)
    assert resolved["client.ville"] == "Laval"
    assert "client.adresse_complete" not in resolved


def test_no_address_at_all_resolves_nothing():
    p = _individu(
        address_street="", address_city="", address_province="",
        address_postal_code="", address_country="",
    )
    resolved = _resolve(
        ["client.adresse_complete", "client.ville", "client.adresse_civique"],
        client=p,
    )
    assert resolved == {}


def test_courriel_falls_back_when_the_selected_block_has_none():
    """Revue 2026-08-14 : `is_work` ne signifie plus « ce rôle préfère le
    professionnel » mais « voici le bloc qui portait une adresse ». Sans
    repli propre au courriel, un client n'ayant enregistré que son adresse
    de bureau perdait son courriel personnel — le défaut même que le repli
    d'adresse venait de supprimer, réintroduit sur la ligne d'à côté."""
    # Client dont la SEULE adresse est celle du bureau, sans courriel pro.
    p = _individu(
        address_street="", address_city="", address_postal_code="",
        email="jean@example.com", email_work="",
        work_address_street="99 rue Bureau", work_address_city="Laval",
    )
    resolved = _resolve(["client.courriel", "client.ville"], client=p)
    assert resolved["client.ville"] == "Laval"          # l'adresse a bien replié
    assert resolved["client.courriel"] == "jean@example.com"   # …sans perdre le courriel

    # Rôle « travail d'abord » SANS aucune adresse : le courriel personnel
    # reste servi (le cas terminal renvoyait is_work=True).
    avocat = _avocat(
        work_address_street="", work_address_city="", work_address_unit="",
        work_address_postal_code="", address_street="", address_city="",
        address_postal_code="", email_work="",
    )
    resolved = _resolve(["destinataire.courriel"], destinataire=avocat)
    assert resolved["destinataire.courriel"] == "claire@perso.com"


def test_a_list_valued_address_component_never_raises():
    """Un client CardDAV non-DavX5 stocke une composante ADR en LISTE quand
    une virgule n'est pas échappée (documenté : mcp.handlers._addr_str). Un
    .strip() nu levait AttributeError — et la faisait remonter jusqu'au
    rendu de l'accusé du portail, dont le marqueur au-plus-une-fois est
    déjà posé à ce moment-là."""
    p = _individu(address_street=["450 rue Sainte-Catherine", "Ouest"])
    resolved = _resolve(
        ["client.adresse_civique", "client.adresse_complete"], client=p,
    )
    assert resolved["client.adresse_civique"] == "450 rue Sainte-Catherine, Ouest"
    assert "450 rue Sainte-Catherine, Ouest" in resolved["client.adresse_complete"]
    assert selected_address_of(p)["street"] == "450 rue Sainte-Catherine, Ouest"


def selected_address_of(partie):
    from utils.template_fields import selected_address

    return selected_address(partie)


def test_selected_email_is_the_public_twin():
    from utils.template_fields import selected_email

    assert selected_email(_personne_morale()) == "info@beaubien.ca"
    assert selected_email(_individu()) == "jean@example.com"
    assert selected_email(_individu(email="", email_work="")) == ""


def test_selected_address_is_the_public_authority():
    """L'accusé du portail et le connecteur MCP lisent CE choix — le module
    reste pur (importable sans Firestore)."""
    from utils.template_fields import selected_address

    assert selected_address(_personne_morale())["city"] == "Montréal"
    assert selected_address(_personne_morale())["street"] == "500 rue Beaubien Est"
    assert selected_address(_avocat())["street"] == "1000 boul. René-Lévesque"
    assert selected_address(_individu())["street"] == "12 rue Principale"


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
        "TRIBUNAL",                # ALL-CAPS alias → still auto (case-insensitive)
        "destinataire.nom",        # canonical → destinataire slot
        "client.nom_complet",      # canonical → client slot
        "date_lettre",             # alias → no slot
        "objet_lettre",            # known manual
        "FAITS",                   # former block → passthrough
        "civilité",                # civilité → passthrough (no longer auto)
        "champ_mystère",           # unknown → passthrough
    ]
    c = classify_placeholders(names)
    assert c.auto["tribunal"] == "dossier.tribunal"
    assert c.auto["TRIBUNAL"] == "dossier.tribunal"
    assert c.auto["destinataire.nom"] == "destinataire.nom"
    assert c.auto["client.nom_complet"] == "client.nom_complet"
    assert c.auto["date_lettre"] == "date.aujourdhui"
    assert c.manual == ["objet_lettre"]
    assert c.passthrough == ["FAITS", "civilité", "champ_mystère"]
    assert c.slots_required == {"dossier", "client", "destinataire"}


def test_cabinet_and_date_require_no_slot():
    c = classify_placeholders(["cabinet.nom", "date.aujourdhui", "pièces_jointes"])
    assert c.slots_required == set()


def test_is_uppercase_name():
    assert is_uppercase_name("FAITS")
    assert is_uppercase_name("CONTENU_LETTRE")
    assert is_uppercase_name("LISTE_PIÈCES")
    assert is_uppercase_name("TRIBUNAL")
    assert not is_uppercase_name("tribunal")
    assert not is_uppercase_name("Tribunal")
    assert not is_uppercase_name("dossier.titre")
    assert not is_uppercase_name("123")  # no letters


# ── Case-insensitive matching + capitalized output ──────────────────────

def test_uppercase_placeholder_resolves_and_uppercases_value():
    resolved = _resolve(["TRIBUNAL", "DISTRICT"], dossier=_dossier())
    assert resolved["TRIBUNAL"] == "COUR SUPÉRIEURE"
    assert resolved["DISTRICT"] == "MONTRÉAL"


def test_mixed_and_lower_case_keep_source_casing():
    resolved = _resolve(["Tribunal", "tribunal"], dossier=_dossier())
    assert resolved["Tribunal"] == "Cour supérieure"
    assert resolved["tribunal"] == "Cour supérieure"


def test_uppercase_namespaced_field_uppercased():
    resolved = _resolve(["DOSSIER.TRIBUNAL"], dossier=_dossier())
    assert resolved["DOSSIER.TRIBUNAL"] == "COUR SUPÉRIEURE"


# ── Client role fields (§6.1) ───────────────────────────────────────────

def test_role_fields_raw_label_and_feminin():
    resolved = _resolve(
        ["dossier.role", "dossier.role_label", "dossier.role_feminin", "rôle"],
        dossier=_dossier(role="demandeur"),
    )
    assert resolved["dossier.role"] == "demandeur"
    assert resolved["dossier.role_label"] == "Demandeur"
    assert resolved["dossier.role_feminin"] == "demanderesse"
    assert resolved["rôle"] == "demanderesse"


def test_role_label_uppercased_when_placeholder_caps():
    resolved = _resolve(["DOSSIER.ROLE_LABEL"], dossier=_dossier(role="défendeur"))
    assert resolved["DOSSIER.ROLE_LABEL"] == "DÉFENDEUR"


def test_civilite_and_salutations_classified_passthrough():
    c = classify_placeholders(
        ["civilité", "civilité_récipient", "destinataire.civilite", "salutations"]
    )
    assert c.auto == {}
    assert c.manual == []
    assert set(c.passthrough) == {
        "civilité", "civilité_récipient", "destinataire.civilite", "salutations"
    }


# ── Missing-value strings (§6.7 — exact) ────────────────────────────────

def test_fallback_value_exact_strings():
    assert fallback_value("numero_dossier", is_auto=True) == (
        "[CHAMP MANQUANT : numero_dossier]"
    )
    assert fallback_value("FAITS", is_auto=False) == "[À COMPLÉTER : FAITS]"
    assert fallback_value("champ_mystère", is_auto=False) == (
        "[À COMPLÉTER : champ_mystère]"
    )


def test_manual_fields_defaults():
    assert MANUAL_FIELDS["pièces_jointes"]["default"] == "Aucune"
    assert "SOUS TOUTES RÉSERVES" in MANUAL_FIELDS["privilège"]["options"]
    assert "courriel" in MANUAL_FIELDS["transmission_lettre"]["options"]


# ── « Mandat » card fields (mandate/classification/fees/lifecycle) ───────

def test_mandat_card_placeholders_resolve():
    from datetime import datetime, timezone

    d = _dossier(
        mandate_type="judiciaire",
        domaine="RCV",
        fee_type="mixed",
        hourly_rate=25000,
        flat_fee=500000,
        opened_date=datetime(2026, 1, 5, tzinfo=timezone.utc),
        closed_date=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    r = _resolve(
        ["dossier.type_mandat", "dossier.type_dossier", "dossier.type_honoraires",
         "dossier.honoraires", "dossier.taux_horaire", "dossier.forfait",
         "dossier.ouverture", "dossier.fermeture", "dossier.retention",
         "type_mandat", "type_dossier", "date_fermeture", "retention"],
        dossier=d,
    )
    from utils import taxonomie
    assert r["dossier.type_mandat"] == "Judiciaire (ad litem)"
    # « Type de dossier » became « Domaine »; the legacy placeholder now
    # resolves to the domaine label so existing gabarits keep filling.
    # Asserted against the live label so an editorial rename doesn't break it.
    assert r["dossier.type_dossier"] == taxonomie.DOMAINE_LABELS["RCV"]
    assert r["dossier.type_honoraires"] == "Mixte"
    assert r["dossier.honoraires"] == "Mixte — 250,00 $/h + 5 000,00 $"
    assert r["dossier.taux_horaire"] == "250,00 $"
    assert r["dossier.forfait"] == "5 000,00 $"
    assert r["dossier.ouverture"] == "5 janvier 2026"
    assert r["dossier.fermeture"] == "14 juillet 2026"
    # Rétention = fermeture + 7 ans (derived).
    assert r["dossier.retention"] == "14 juillet 2033"
    # Flat aliases resolve to the same values.
    assert r["type_mandat"] == "Judiciaire (ad litem)"
    assert r["type_dossier"] == taxonomie.DOMAINE_LABELS["RCV"]
    assert r["date_fermeture"] == "14 juillet 2026"
    assert r["retention"] == "14 juillet 2033"


def test_contingency_percent_renders_in_honoraires():
    nbsp = " "
    d = _dossier(fee_type="contingency", contingency_percent=2500)
    r = _resolve(["dossier.honoraires", "dossier.pourcentage"], dossier=d)
    assert r["dossier.honoraires"] == f"Contingence — 25{nbsp}%"
    assert r["dossier.pourcentage"] == f"25{nbsp}%"
    # Mixte carries all three components.
    d = _dossier(fee_type="mixed", hourly_rate=25000, flat_fee=500000,
                 contingency_percent=3333)
    r = _resolve(["dossier.honoraires"], dossier=d)
    assert r["dossier.honoraires"] == (
        f"Mixte — 250,00{nbsp}$/h + 5{nbsp}000,00{nbsp}$ + 33,33{nbsp}%"
    )
    # Contingency with no stored rate still renders the label alone.
    r = _resolve(["dossier.honoraires"], dossier=_dossier(fee_type="contingency"))
    assert r["dossier.honoraires"] == "Contingence"


def test_rate_less_fee_types_render_label_alone():
    for fee_type, label in [("pro_bono", "Pro bono"),
                            ("aide_juridique", "Aide juridique")]:
        r = _resolve(["dossier.honoraires", "dossier.type_honoraires"],
                     dossier=_dossier(fee_type=fee_type))
        assert r["dossier.honoraires"] == label
        assert r["dossier.type_honoraires"] == label
    # A stale rate left on the doc from a previous fee type must NOT leak in.
    d = _dossier(fee_type="pro_bono", hourly_rate=25000, flat_fee=500000,
                 contingency_percent=2500)
    r = _resolve(["dossier.honoraires"], dossier=d)
    assert r["dossier.honoraires"] == "Pro bono"


def test_mandat_fields_unresolved_when_data_absent():
    # An open dossier (no closure) leaves fermeture/rétention unresolved; an
    # hourly dossier leaves forfait unresolved.
    d = _dossier(fee_type="hourly", hourly_rate=25000)
    r = _resolve(
        ["dossier.fermeture", "dossier.retention", "dossier.forfait",
         "dossier.honoraires"],
        dossier=d,
    )
    assert "dossier.fermeture" not in r
    assert "dossier.retention" not in r
    assert "dossier.forfait" not in r
    assert r["dossier.honoraires"] == "Horaire — 250,00 $/h"
