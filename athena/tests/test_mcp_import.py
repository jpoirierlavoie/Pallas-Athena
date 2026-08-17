"""Les outils de reprise historique du connecteur (lot Q).

Même amorce que test_mcp_tools : on importe handlers/tools SOUS le correctif de
google.cloud.firestore.Client (models/__init__ construit son client à
l'import), puis on remplace les verbes de modèle sur les références liées du
module handlers.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools


# ── get_reference_vocabulary ───────────────────────────────────────────────
# Le déblocage : « Domaine invalide. » ne nomme aucun domaine valide, et aucun
# outil de lecture n'exposait la taxonomie. La classification que le juriste
# demande d'importer ne pouvait qu'être devinée, puis refusée.


@pytest.mark.parametrize(
    "kind",
    ["domaines", "actions", "prescription_types", "forums", "districts", "phases"],
)
def test_chaque_vocabulaire_rend_des_codes_utilisables(kind):
    payload = handlers.get_reference_vocabulary({"kind": kind})
    assert payload["count"] > 0
    assert payload["kind"] == kind
    for item in payload["items"]:
        assert item["code"]
        assert item["label"]
        assert isinstance(item["note"], str)


def test_les_domaines_sont_ceux_que_le_modele_valide():
    """Dérivé de utils.taxonomie, jamais recopié : le module est PUR, donc la
    dérive est structurellement impossible (le précédent _COVERAGE_CODES)."""
    from utils import taxonomie

    codes = [i["code"] for i in
             handlers.get_reference_vocabulary({"kind": "domaines"})["items"]]
    assert codes == [c for c in taxonomie.VALID_DOMAINES if c]


def test_les_actions_se_filtrent_par_domaine_et_en_portent_le_prefixe():
    payload = handlers.get_reference_vocabulary(
        {"kind": "actions", "domaine": "REC"}
    )
    assert payload["count"] > 0
    for item in payload["items"]:
        assert item["code"].startswith("REC-")


def test_sans_filtre_les_actions_sortent_toutes():
    from utils import taxonomie

    payload = handlers.get_reference_vocabulary({"kind": "actions"})
    assert payload["count"] == len(taxonomie.ACTIONS)


def test_un_domaine_inconnu_est_refuse_en_nommant_la_sortie():
    with pytest.raises(tools.ToolArgumentError) as exc:
        handlers.get_reference_vocabulary(
            {"kind": "actions", "domaine": "ZZZ"}
        )
    assert "domaines" in str(exc.value)


def test_le_filtre_domaine_ne_s_applique_qu_aux_actions():
    """Un filtre silencieusement ignoré ferait croire à une liste restreinte
    alors qu'elle est complète."""
    with pytest.raises(tools.ToolArgumentError):
        handlers.get_reference_vocabulary(
            {"kind": "domaines", "domaine": "REC"}
        )


def test_le_vocabulaire_des_phases_porte_les_codes_ET_les_sous_codes():
    items = handlers.get_reference_vocabulary({"kind": "phases"})["items"]
    codes = {i["code"] for i in items}
    assert "CTS" in codes and "CTS-02" in codes
    parent = next(i for i in items if i["code"] == "CTS")
    child = next(i for i in items if i["code"] == "CTS-02")
    assert parent["note"] == "phase"
    assert "CTS" in child["note"]


def test_le_delai_d_une_action_est_annonce_comme_indicatif():
    """La taxonomie SUGGÈRE un délai, elle n'en fixe jamais un. Le « » de
    certaines lignes est voulu (la source n'a pas de période unique) : il ne
    doit pas se lire comme une lacune à combler."""
    description = tools.TOOLS["get_reference_vocabulary"]["description"]
    assert "INDICATIVE" in description
    payload = handlers.get_reference_vocabulary(
        {"kind": "actions", "domaine": "REC"}
    )
    assert any(i["note"] for i in payload["items"])


# ── find_imported ──────────────────────────────────────────────────────────


@pytest.fixture
def legacy(monkeypatch):
    """Remplace models.find_by_legacy_ref — le gestionnaire l'importe
    LOCALEMENT, donc l'attribut est relu à l'appel."""
    import models

    store: dict[str, list[dict]] = {}

    def _find(collection, legacy_ref, limit=5):
        return list(store.get(collection, []))

    monkeypatch.setattr(models, "find_by_legacy_ref", _find)
    return store


def test_find_imported_retrouve_a_travers_les_collections(legacy):
    legacy["dossiers"] = [{"id": "d1", "file_number": "2019-014",
                           "title": "Tremblay c. Lavoie"}]
    legacy["invoices"] = [{"id": "i1", "invoice_number": "2019-F014",
                           "dossier_id": "d1"}]
    payload = handlers.find_imported({"legacy_ref": "L-42"})
    kinds = {m["entity_type"] for m in payload["matches"]}
    assert kinds == {"dossier", "invoice"}
    assert payload["count"] == 2
    assert payload["legacy_ref"] == "L-42"
    facture = next(m for m in payload["matches"] if m["entity_type"] == "invoice")
    assert facture["label"] == "2019-F014"
    assert facture["dossier_id"] == "d1"


def test_find_imported_se_restreint_a_un_type(legacy):
    legacy["dossiers"] = [{"id": "d1", "file_number": "2019-014", "title": "X"}]
    legacy["invoices"] = [{"id": "i1", "invoice_number": "2019-F014"}]
    payload = handlers.find_imported(
        {"legacy_ref": "L-42", "entity_type": "dossier"}
    )
    assert [m["entity_type"] for m in payload["matches"]] == ["dossier"]


def test_find_imported_sans_correspondance_rend_zero(legacy):
    payload = handlers.find_imported({"legacy_ref": "L-inconnue"})
    assert payload["count"] == 0
    assert payload["matches"] == []


def test_find_imported_echoue_ferme(monkeypatch):
    """« Rien n'est revenu, donc je crée » est le geste suivant. Une erreur
    avalée se lirait « absent » et frapperait un doublon que RIEN dans ce
    connecteur ne peut supprimer."""
    import models

    def _boom(collection, legacy_ref, limit=5):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(models, "find_by_legacy_ref", _boom)
    with pytest.raises(RuntimeError):
        handlers.find_imported({"legacy_ref": "L-42"})


def test_find_imported_refuse_une_reference_vide(legacy):
    for blank in ("", "   "):
        with pytest.raises(tools.ToolArgumentError):
            handlers.find_imported({"legacy_ref": blank})


def test_find_imported_nomme_un_contact_par_son_nom_affiche(legacy):
    legacy["parties"] = [{"id": "p1", "type": "organization",
                          "organization_name": "Béton Nord inc."}]
    payload = handlers.find_imported({"legacy_ref": "L-7"})
    assert payload["matches"][0]["label"] == "Béton Nord inc."
    assert payload["matches"][0]["dossier_id"] is None


# ── get_import_audit ───────────────────────────────────────────────────────


@pytest.fixture
def audit(monkeypatch):
    world = {
        "dossier": {"id": "d1", "file_number": "2019-014", "title": "T",
                    "status": "actif", "closed_date": None,
                    "client_ids": ["p1"], "hourly_rate": 30000},
        "entries": [], "entries_cursor": None,
        "expenses": [], "expenses_cursor": None,
        "invoices": [], "line_items": {},
    }
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: world["dossier"] if i == "d1" else None)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        lambda fn: world["dossier"]
                        if fn == "2019-014" else None)
    monkeypatch.setattr(handlers.dossier_model, "field_defaults",
                        lambda: {"hourly_rate": 30000})
    monkeypatch.setattr(
        handlers.time_entry_model, "list_time_entries_page",
        lambda **kw: (world["entries"], world["entries_cursor"]))
    monkeypatch.setattr(
        handlers.expense_model, "list_expenses_page",
        lambda **kw: (world["expenses"], world["expenses_cursor"]))
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: list(world["invoices"]))
    monkeypatch.setattr(handlers.invoice_model, "list_line_items",
                        lambda iid: world["line_items"].get(iid, []))
    return world


def test_get_import_audit_exige_exactement_un_selecteur(audit):
    for args in ({}, {"dossier_id": "d1", "file_number": "2019-014"}):
        with pytest.raises(tools.ToolArgumentError):
            handlers.get_import_audit(args)


def test_get_import_audit_par_numero_de_dossier(audit):
    payload = handlers.get_import_audit({"file_number": "2019-014"})
    assert payload["found"] is True
    assert payload["dossier"]["file_number"] == "2019-014"


def test_get_import_audit_dossier_absent(audit):
    payload = handlers.get_import_audit({"dossier_id": "inconnu"})
    assert payload["found"] is False


def test_get_import_audit_compte_ce_que_la_reprise_a_ecrit(audit):
    audit["entries"] = [
        {"id": "e1", "amount": 45000, "invoiced": True, "invoice_id": "i1",
         "created_via": "mcp", "phase": "CTS", "description": "A",
         "date": "2019-11-04"},
        {"id": "e2", "amount": 15000, "invoiced": False,
         "description": "B", "date": "2019-11-05"},
    ]
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    bloc = payload["time"]
    assert bloc["count"] == 2
    assert bloc["invoiced_count"] == 1 and bloc["uninvoiced_count"] == 1
    assert bloc["created_via_mcp_count"] == 1
    assert bloc["unphased_count"] == 1
    assert bloc["amount_cents"] == 60000
    assert bloc["uninvoiced_amount_cents"] == 15000


def test_get_import_audit_supprime_les_controles_de_source_sur_fenetre_tronquee(audit):
    """Le cœur du contrôle : une fenêtre tronquée ferait passer les postes
    d'une facture pour orphelins. On SUPPRIME et on le DIT, plutôt que
    d'accuser une reprise qui va bien."""
    audit["entries_cursor"] = "il-en-reste"
    audit["invoices"] = [{"id": "i1", "invoice_number": "2019-F014",
                          "status": "payée", "subtotal": 45000,
                          "total": 45000}]
    audit["line_items"] = {"i1": [{"id": "li1", "source_id": "e1",
                                   "amount": 45000}]}
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    codes = {f["code"] for f in payload["findings"]}
    assert "IMP-03" not in codes and "IMP-06" not in codes
    assert payload["checks_skipped"] == ["IMP-03", "IMP-06"]
    assert payload["truncated"] is True


def test_get_import_audit_sans_troncature_ne_supprime_rien(audit):
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    assert payload["checks_skipped"] == []
    assert payload["truncated"] is False


def test_get_import_audit_dit_quand_les_postes_sont_illisibles(audit):
    """subtotal_matches_line_items est TRI-ÉTAT : null n'est pas « faux »."""
    audit["invoices"] = [{"id": "i1", "invoice_number": "2019-F014",
                          "status": "payée", "subtotal": 45000,
                          "total": 45000}]
    audit["line_items"] = {"i1": []}
    row = handlers.get_import_audit({"dossier_id": "d1"})["invoices"][0]
    assert row["subtotal_matches_line_items"] is None
    assert row["line_count"] == 0


def test_get_import_audit_signale_une_facture_restee_au_brouillon(audit):
    audit["invoices"] = [{"id": "i1", "invoice_number": "2019-F014",
                          "status": "brouillon", "subtotal": 45000,
                          "total": 45000}]
    audit["line_items"] = {"i1": [{"id": "li1", "source_id": "e1",
                                   "amount": 45000}]}
    audit["entries"] = [{"id": "e1", "amount": 45000, "invoiced": True,
                         "invoice_id": "i1", "description": "A",
                         "date": "2019-11-04"}]
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    assert "IMP-07" in {f["code"] for f in payload["findings"]}


# ── create_partie / update_partie ──────────────────────────────────────────


@pytest.fixture
def contacts(monkeypatch):
    """Capture ce qui est RÉELLEMENT remis au modèle, et les bumps CTag."""
    import models

    world = {
        "created": {}, "updated": {}, "updated_id": None,
        "bumps": [], "existing": None, "legacy": {},
    }

    def _create(data):
        world["created"] = dict(data)
        return {**data, "id": "p-new"}, []

    def _update(pid, data):
        world["updated"] = dict(data)
        world["updated_id"] = pid
        return {**(world["existing"] or {}), **data, "id": pid}, []

    monkeypatch.setattr(handlers.partie_model, "create_partie", _create)
    monkeypatch.setattr(handlers.partie_model, "update_partie", _update)
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: world["existing"])
    monkeypatch.setattr(handlers, "bump_ctag",
                        lambda name: world["bumps"].append(name))
    monkeypatch.setattr(models, "find_by_legacy_ref",
                        lambda c, r, limit=5: list(world["legacy"].get(r, [])))
    return world


_INDIV = {"type": "individual", "last_name": "Tremblay", "first_name": "Jean"}


def test_create_partie_bumpe_exactement_le_carnet_d_adresses(contacts):
    """models/partie ne bumpe JAMAIS — le bump vit dans la route, donc le
    connecteur doit le refaire. Sans lui le contact est en base, visible dans
    l'application, et DavX5 ne le voit jamais."""
    payload = handlers.create_partie(dict(_INDIV))
    assert contacts["bumps"] == ["parties"]
    assert payload["created"] is True
    assert payload["ctag_bumped"] is True and payload["dav_synced"] is True
    assert payload["entity"]["label"] == "Jean Tremblay"


def test_create_partie_en_simulation_n_ecrit_ni_ne_bumpe(contacts):
    payload = handlers.create_partie({**_INDIV, "dry_run": True})
    assert contacts["created"] == {}
    assert contacts["bumps"] == []
    assert payload["ctag_bumped"] is False
    assert any("Simulation" in w for w in payload["warnings"])


def test_un_bump_rate_ne_fait_pas_reessayer(contacts, monkeypatch):
    """Le contact est DÉJÀ écrit. Laisser filer l'exception le rapporterait
    comme un échec, et le modèle réessaierait — un doublon."""
    def _boom(name):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(handlers, "bump_ctag", _boom)
    payload = handlers.create_partie(dict(_INDIV))
    assert payload["created"] is True
    assert payload["ctag_bumped"] is False
    assert any("Ne pas réessayer" in w for w in payload["warnings"])


@pytest.mark.parametrize("dry", [False, True])
def test_une_personne_physique_exige_un_nom_de_famille(contacts, dry):
    """Le refus doit être IDENTIQUE en simulation : un dry_run qui annonce un
    succès que l'appel réel refuse est un mensonge."""
    with pytest.raises(tools.ToolArgumentError, match="last_name"):
        handlers.create_partie({"type": "individual", "first_name": "Jean",
                                "dry_run": dry})


@pytest.mark.parametrize("dry", [False, True])
def test_une_personne_morale_exige_un_nom_legal(contacts, dry):
    with pytest.raises(tools.ToolArgumentError, match="organization_name"):
        handlers.create_partie({"type": "organization", "dry_run": dry})


def test_les_deux_familles_de_champs_ne_se_melangent_pas(contacts):
    with pytest.raises(tools.ToolArgumentError, match="ne se mélangent pas"):
        handlers.create_partie({"type": "organization",
                                "organization_name": "Béton Nord inc.",
                                "last_name": "Tremblay"})


# ── Le bloc d'adresse ──────────────────────────────────────────────────────


_FULL_ADDRESS = {
    "address_street": "150 rue King", "address_unit": "",
    "address_city": "Toronto", "address_province": "Ontario",
    "address_postal_code": "M5H 1J9", "address_country": "Canada",
}


def test_une_adresse_complete_passe_telle_quelle(contacts):
    handlers.create_partie({**_INDIV, **_FULL_ADDRESS})
    for key, value in _FULL_ADDRESS.items():
        assert contacts["created"][key] == value


@pytest.mark.parametrize("missing", ["address_city", "address_province",
                                     "address_street", "address_country"])
def test_un_bloc_d_adresse_partiel_est_refuse(contacts, missing):
    """apply_address_defaults écrit Montréal / Québec / Canada DANS le
    dictionnaire de l'appelant dès qu'une rue est présente : un contact
    torontois sans ville serait silencieusement déménagé — sur une facture
    que le client recevra."""
    partial = {k: v for k, v in _FULL_ADDRESS.items() if k != missing}
    with pytest.raises(tools.ToolArgumentError, match="BLOC"):
        handlers.create_partie({**_INDIV, **partial})
    assert contacts["created"] == {}


def test_un_bloc_partiel_est_refuse_aussi_en_simulation(contacts):
    partial = {k: v for k, v in _FULL_ADDRESS.items() if k != "address_city"}
    with pytest.raises(tools.ToolArgumentError, match="BLOC"):
        handlers.create_partie({**_INDIV, **partial, "dry_run": True})


def test_unit_et_code_postal_peuvent_rester_vides(contacts):
    """Une adresse sans numéro d'unité est banale ; l'exiger refuserait des
    contacts parfaitement valides."""
    handlers.create_partie({
        **_INDIV, "address_street": "1 rue X", "address_unit": "",
        "address_city": "Laval", "address_province": "Québec",
        "address_postal_code": "", "address_country": "Canada",
    })
    assert contacts["created"]["address_city"] == "Laval"


def test_aucune_adresse_du_tout_reste_permis(contacts):
    handlers.create_partie(dict(_INDIV))
    assert not any(k.startswith("address_") for k in contacts["created"])


# ── Ce qui n'est PAS adressable ────────────────────────────────────────────


@pytest.mark.parametrize("forbidden", [
    "id", "etag", "created_at", "updated_at", "vcard_uid",
    "identity_verified", "identity_verified_date", "conflict_check",
    "conflict_check_notes", "kyc_document_ids", "mandataires", "birth_date",
])
def test_les_champs_interdits_ne_sont_pas_adressables(forbidden):
    """Le schéma est la garde : une machine n'atteste pas qu'une identité a
    été vérifiée, et un mandataires vide effacerait la liste."""
    for name in ("create_partie", "update_partie"):
        props = tools.TOOLS[name]["input_schema"]["properties"]
        assert forbidden not in props, (name, forbidden)


def test_le_type_ne_se_change_pas_en_correction():
    assert "type" not in tools.TOOLS["update_partie"]["input_schema"]["properties"]
    assert "type" in tools.TOOLS["create_partie"]["input_schema"]["properties"]


def test_les_vocabulaires_non_valides_par_le_modele_sont_bornes_au_schema():
    """VALID_PREFIXES / LANGUAGES / GENDERS / PRONOUNS sont déclarés dans le
    modèle et JAMAIS vérifiés par son _validate : le formulaire web les
    contraint par un <select>, le modèle non. Sur le chemin du connecteur,
    l'enum du schéma est donc l'UNIQUE garde."""
    from models import partie as partie_model

    props = tools.TOOLS["create_partie"]["input_schema"]["properties"]
    for field, vocab in (
        ("prefix", partie_model.VALID_PREFIXES),
        ("language", partie_model.VALID_LANGUAGES),
        ("gender", partie_model.VALID_GENDERS),
        ("pronouns", partie_model.VALID_PRONOUNS),
    ):
        # Le modèle admet « » (absence) ; l'enum du connecteur ne l'expose
        # pas — on omet le paramètre pour ne rien dire.
        assert set(props[field]["enum"]) == {v for v in vocab if v}


# ── update_partie ──────────────────────────────────────────────────────────


def test_update_partie_refuse_un_id_inconnu_meme_en_simulation(contacts):
    contacts["existing"] = None
    for dry in (False, True):
        with pytest.raises(tools.ToolArgumentError, match="introuvable"):
            handlers.update_partie({"partie_id": "absent", "notes": "x",
                                    "dry_run": dry})


def test_update_partie_ne_remet_que_ce_qui_change(contacts):
    """PATCH strict : le modèle écrit le document ENTIER, donc une clé
    présente et vide EFFACE. Un champ omis ne doit jamais atteindre le
    modèle."""
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "Tremblay", "email": "a@b.ca",
                            "notes": "ancienne note"}
    handlers.update_partie({"partie_id": "p1", "notes": "nouvelle note"})
    assert set(contacts["updated"]) == {"notes"}
    assert contacts["updated_id"] == "p1"


def test_update_partie_ne_transmet_jamais_un_id(contacts):
    """Le modèle fusionne sans re-fixer l'id : un id fourni corromprait le
    CHAMP id sans changer le chemin du document, ce qui casse en silence la
    pagination par curseur et les scans de mandataires."""
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "Tremblay"}
    handlers.update_partie({"partie_id": "p1", "notes": "x"})
    assert "id" not in contacts["updated"]


def test_update_partie_sans_aucun_champ_est_refuse(contacts):
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "Tremblay"}
    with pytest.raises(tools.ToolArgumentError, match="Aucun champ"):
        handlers.update_partie({"partie_id": "p1"})


def test_update_partie_bumpe_le_carnet(contacts):
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "Tremblay"}
    payload = handlers.update_partie({"partie_id": "p1", "notes": "x"})
    assert contacts["bumps"] == ["parties"]
    assert payload["updated"] is True


def test_update_partie_remonte_le_refus_du_modele_tel_quel(contacts, monkeypatch):
    """_validate re-valide le document FUSIONNÉ : un contact héritant d'un
    téléphone illisible refuse toute modification, en nommant un champ que
    l'appelant n'a pas touché. Le message doit remonter mot pour mot pour que
    l'opérateur répare la racine."""
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "Tremblay"}
    monkeypatch.setattr(
        handlers.partie_model, "update_partie",
        lambda pid, data: (None, ["Cellulaire : Numéro de téléphone invalide."]))
    with pytest.raises(tools.ToolArgumentError, match="Cellulaire"):
        handlers.update_partie({"partie_id": "p1", "notes": "x"})


# ── legacy_ref ─────────────────────────────────────────────────────────────


def test_une_reference_d_origine_deja_prise_est_refusee(contacts):
    contacts["legacy"]["L-42"] = [{"id": "p-existant"}]
    with pytest.raises(tools.ToolArgumentError, match="p-existant"):
        handlers.create_partie({**_INDIV, "legacy_ref": "L-42"})
    assert contacts["created"] == {}


def test_une_reference_d_origine_libre_passe(contacts):
    handlers.create_partie({**_INDIV, "legacy_ref": "L-43"})
    assert contacts["created"]["legacy_ref"] == "L-43"


def test_update_ne_revérifie_pas_une_reference_inchangee(contacts):
    """Sinon un contact ne pourrait plus jamais être corrigé : sa propre
    référence est déjà prise… par lui-même."""
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "T", "legacy_ref": "L-42"}
    contacts["legacy"]["L-42"] = [{"id": "p1"}]
    handlers.update_partie({"partie_id": "p1", "legacy_ref": "L-42",
                            "notes": "x"})
    assert contacts["updated"]["notes"] == "x"


# ── create_dossier / update_dossier ────────────────────────────────────────


@pytest.fixture
def dossiers(monkeypatch):
    import models

    world = {
        "created": {}, "updated": {}, "existing": None,
        "by_number": {}, "parties": {}, "legacy": {},
    }

    def _create(data):
        world["created"] = dict(data)
        doc = {**data, "id": "d-new"}
        # Le VRAI dérivateur : create_dossier l'applique, et le taire ici
        # ferait passer pour muet un avertissement qui parle en production.
        handlers.dossier_model._apply_prescription_deadline(doc)
        return doc, []

    def _update(did, data):
        world["updated"] = dict(data)
        doc = {**(world["existing"] or {}), **data, "id": did}
        handlers.dossier_model._apply_prescription_deadline(doc)
        return doc, []

    monkeypatch.setattr(handlers.dossier_model, "create_dossier", _create)
    monkeypatch.setattr(handlers.dossier_model, "update_dossier", _update)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: world["existing"])
    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        lambda fn: world["by_number"].get(fn))
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: world["parties"].get(i))
    monkeypatch.setattr(models, "find_by_legacy_ref",
                        lambda c, r, limit=5: list(world["legacy"].get(r, [])))
    world["parties"]["p1"] = {"id": "p1", "type": "individual",
                              "last_name": "Tremblay", "first_name": "Jean"}
    world["parties"]["p2"] = {"id": "p2", "type": "individual",
                              "last_name": "Lavoie"}
    world["parties"]["av1"] = {"id": "av1", "type": "individual",
                               "prefix": "Me", "last_name": "Roy"}
    return world


def _mk(**over):
    base = {"file_number": "2019-014", "title": "Tremblay c. Lavoie",
            "clients": [{"partie_id": "p1", "roles": ["demandeur"]}]}
    base.update(over)
    return base


def test_create_dossier_resout_et_instantane_les_parties(dossiers):
    """Les noms sont des INSTANTANÉS que l'appelant ne doit pas pouvoir
    falsifier : c'est ce qu'une procédure générée cite."""
    handlers.create_dossier(_mk(
        opposing_parties=[{"partie_id": "p2", "roles": ["défendeur"],
                           "avocat_partie_id": "av1"}]))
    created = dossiers["created"]
    assert created["clients"][0] == {
        "id": "p1", "name": "Jean Tremblay", "roles": ["demandeur"],
        "avocat_id": "", "avocat_name": "",
    }
    adverse = created["opposing_parties"][0]
    assert adverse["name"] == "Lavoie"
    assert adverse["avocat_id"] == "av1" and adverse["avocat_name"] == "Me Roy"


def test_une_partie_introuvable_est_un_refus_francais_pas_un_KeyError(dossiers):
    """_rebuild_party_mirrors indice en BRUT c["id"] : sans cette résolution
    préalable, une entrée non résolue lèverait un KeyError NON RATTRAPÉ dans
    le modèle — un 500, pas une erreur de validation."""
    with pytest.raises(tools.ToolArgumentError, match="Contact introuvable"):
        handlers.create_dossier(_mk(clients=[{"partie_id": "fantome"}]))
    assert dossiers["created"] == {}


def test_une_entree_de_partie_sans_id_est_refusee(dossiers):
    with pytest.raises(tools.ToolArgumentError, match="partie_id"):
        handlers.create_dossier(_mk(clients=[{"roles": ["demandeur"]}]))


def test_un_role_inconnu_est_refuse_pas_ecarte_en_silence(dossiers):
    """Le formulaire web les écarte silencieusement ; un connecteur qui
    jetterait la moitié des rôles rapporterait un succès qui n'en est pas un."""
    with pytest.raises(tools.ToolArgumentError, match="Rôle de partie inconnu"):
        handlers.create_dossier(_mk(
            clients=[{"partie_id": "p1", "roles": ["capitaine"]}]))


def test_une_partie_des_deux_cotes_est_refusee(dossiers):
    with pytest.raises(tools.ToolArgumentError, match="client et comme partie"):
        handlers.create_dossier(_mk(
            opposing_parties=[{"partie_id": "p1"}]))


def test_un_numero_de_dossier_deja_pris_est_refuse(dossiers):
    dossiers["by_number"]["2019-014"] = {"id": "d-existant"}
    with pytest.raises(tools.ToolArgumentError, match="existe déjà"):
        handlers.create_dossier(_mk())
    assert dossiers["created"] == {}


def test_la_verification_d_unicite_echoue_fermee(dossiers, monkeypatch):
    def _raises(fn):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        _raises)
    with pytest.raises(RuntimeError):
        handlers.create_dossier(_mk())
    assert dossiers["created"] == {}


def test_un_dossier_peut_naitre_ferme_et_le_dit(dossiers):
    payload = handlers.create_dossier(_mk(status="fermé",
                                          closed_date="2019-12-01"))
    assert dossiers["created"]["status"] == "fermé"
    assert any("DavX5" in w for w in payload["warnings"])


def test_un_taux_horaire_nul_est_accepte(dossiers):
    """Pro bono / aide juridique. Le refuser bloquerait la reprise ET
    empoisonnerait chaque entrée de temps : create_time_entry prend le taux du
    dossier par défaut, donc le dossier facturerait à 300 $/h."""
    handlers.create_dossier(_mk(hourly_rate=0, fee_type="pro_bono"))
    assert dossiers["created"]["hourly_rate"] == 0


def test_le_numero_de_cour_derive_les_metadonnees_judiciaires(dossiers):
    handlers.create_dossier(_mk(court_file_number="500-05-123456-241"))
    created = dossiers["created"]
    assert created["greffe_number"] == "500"
    assert created["district_judiciaire"] == "Montréal"


def test_normalize_forum_est_appele_et_ce_qu_il_ecarte_est_dit(dossiers):
    """normalize_forum vit dans la ROUTE, jamais dans le modèle. Sans lui, un
    dossier préjudiciaire n'aurait pas son numéro « Préjudiciaire » et
    {{dossier.numero_cour}} se remplirait vide."""
    payload = handlers.create_dossier(_mk(
        forum_type="prejudiciaire", court_file_number="500-05-123456-241",
        district_judiciaire="Montréal"))
    assert dossiers["created"]["court_file_number"] == "Préjudiciaire"
    assert any("Préjudiciaire" in w for w in payload["warnings"])


def test_un_forum_administratif_ecarte_le_district_en_le_disant(dossiers):
    payload = handlers.create_dossier(_mk(
        forum_type="administratif", forum="taq", district_judiciaire="Montréal"))
    assert dossiers["created"]["district_judiciaire"] == ""
    assert any("district" in w.lower() for w in payload["warnings"])


def test_une_date_pour_agir_calculee_est_annoncee(dossiers):
    """_apply_prescription_deadline ÉCRASE prescription_date dès que le droit
    d'action et un délai périodique coexistent — en silence, et le connecteur
    ne peut pas forcer une valeur historique par-dessus."""
    payload = handlers.create_dossier(_mk(
        droit_action_date="2019-01-15", prescription_type="3_ans"))
    assert any("CALCULÉE" in w for w in payload["warnings"])


def test_create_dossier_en_simulation_n_ecrit_rien(dossiers):
    payload = handlers.create_dossier(_mk(dry_run=True))
    assert dossiers["created"] == {}
    assert any("Simulation" in w for w in payload["warnings"])


# ── update_dossier ─────────────────────────────────────────────────────────


def test_update_dossier_refuse_un_changement_de_statut_en_donnant_la_raison(dossiers):
    """Fermer un dossier exige la purge DavX5 côté route. Un dossier fermé
    ici laisserait ses tâches, ses notes et ses audiences sur le téléphone
    pour toujours."""
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif"}
    with pytest.raises(tools.ToolArgumentError, match="DavX5"):
        handlers.update_dossier({"dossier_id": "d1", "status": "fermé"})


@pytest.mark.parametrize("forbidden", ["status", "file_number", "closed_date",
                                       "clients", "opposing_parties"])
def test_les_champs_non_corrigibles_ne_sont_pas_adressables(forbidden):
    props = tools.TOOLS["update_dossier"]["input_schema"]["properties"]
    assert forbidden not in props


def test_update_dossier_ajoute_une_partie_sans_effacer_les_autres(dossiers):
    """_rebuild_party_mirrors recalcule client_ids sans diff ni
    avertissement : passer [A] à un dossier qui porte [A, B] SUPPRIMERAIT B
    en silence en rapportant un succès."""
    dossiers["existing"] = {
        "id": "d1", "file_number": "2019-014", "status": "actif",
        "clients": [{"id": "p1", "name": "Jean Tremblay", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
    }
    handlers.update_dossier({"dossier_id": "d1",
                             "add_clients": [{"partie_id": "p2"}]})
    written = dossiers["updated"]["clients"]
    assert [c["id"] for c in written] == ["p1", "p2"]


def test_ajouter_une_partie_deja_presente_est_un_refus_atomique(dossiers):
    dossiers["existing"] = {
        "id": "d1", "file_number": "2019-014", "status": "actif",
        "clients": [{"id": "p1", "name": "Jean Tremblay", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
    }
    with pytest.raises(tools.ToolArgumentError, match="figurent déjà"):
        handlers.update_dossier({"dossier_id": "d1",
                                 "add_clients": [{"partie_id": "p1"}]})
    assert dossiers["updated"] == {}


def test_update_dossier_ne_remet_que_ce_qui_change(dossiers):
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif", "title": "ancien"}
    handlers.update_dossier({"dossier_id": "d1", "sommaire": "résumé"})
    assert set(dossiers["updated"]) == {"sommaire"}


def test_update_dossier_refuse_un_id_inconnu_meme_en_simulation(dossiers):
    dossiers["existing"] = None
    for dry in (False, True):
        with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
            handlers.update_dossier({"dossier_id": "absent", "sommaire": "x",
                                     "dry_run": dry})


def test_update_dossier_sans_champ_est_refuse(dossiers):
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif"}
    with pytest.raises(tools.ToolArgumentError, match="Aucun champ"):
        handlers.update_dossier({"dossier_id": "d1"})


def test_les_notes_de_prescription_sont_enfin_atteignables(dossiers):
    """Un vrai champ du modèle, écrit par le formulaire web et absent de
    _COMPLETABLE_FIELDS : le connecteur ne pouvait pas l'atteindre."""
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif"}
    handlers.update_dossier({"dossier_id": "d1",
                             "prescription_notes": "Suspension convenue."})
    assert dossiers["updated"]["prescription_notes"] == "Suspension convenue."


# ── update_time_entry / update_expense ─────────────────────────────────────


@pytest.fixture
def billing(monkeypatch):
    world = {"entry": None, "expense": None, "written": {}}

    def _upd_entry(eid, data):
        world["written"] = dict(data)
        return {**(world["entry"] or {}), **data, "id": eid}, []

    def _upd_expense(xid, data):
        world["written"] = dict(data)
        return {**(world["expense"] or {}), **data, "id": xid}, []

    monkeypatch.setattr(handlers.time_entry_model, "get_time_entry",
                        lambda i: world["entry"])
    monkeypatch.setattr(handlers.time_entry_model, "update_time_entry", _upd_entry)
    monkeypatch.setattr(handlers.expense_model, "get_expense",
                        lambda i: world["expense"])
    monkeypatch.setattr(handlers.expense_model, "update_expense", _upd_expense)
    world["entry"] = {"id": "e1", "dossier_id": "d1", "description": "Rédaction",
                      "hours": 1.5, "rate": 30000, "amount": 45000,
                      "billable": True, "invoiced": False,
                      "phase": "CTS", "sous_phase": "CTS-02"}
    world["expense"] = {"id": "x1", "dossier_id": "d1", "description": "Timbre",
                        "amount": 5000, "taxable": False, "invoiced": False,
                        "category": "timbre_judiciaire",
                        "phase": "PRE", "sous_phase": "PRE-00"}
    return world


@pytest.mark.parametrize("dry", [False, True])
def test_une_entree_deja_facturee_est_refusee_y_compris_en_simulation(billing, dry):
    """LE contrôle de la famille. Le modèle refuse aussi, mais run_write
    court-circuite sur dry_run sans jamais l'appeler : sans cette pré-lecture,
    une simulation annoncerait un succès que l'appel réel refuse."""
    billing["entry"]["invoiced"] = True
    billing["entry"]["invoice_id"] = "i1"
    with pytest.raises(tools.ToolArgumentError, match="déjà porté"):
        handlers.update_time_entry({"time_entry_id": "e1", "hours": 2.0,
                                    "dry_run": dry})
    assert billing["written"] == {}


def test_le_refus_nomme_la_vraie_voie_de_retour(billing):
    """void_invoice, dans l'application, libère chaque source. Dire que rien
    n'est possible serait faux."""
    billing["entry"]["invoiced"] = True
    with pytest.raises(tools.ToolArgumentError, match="annulez la facture"):
        handlers.update_time_entry({"time_entry_id": "e1", "hours": 2.0})


def test_omettre_billable_ne_refacture_pas(billing):
    """La ligne la plus dangereuse de la famille serait args.get("billable",
    True) : elle refacturerait en silence une entrée délibérément non
    facturable ET rematérialiserait son montant, le modèle recalculant à
    chaque sauvegarde."""
    billing["entry"]["billable"] = False
    handlers.update_time_entry({"time_entry_id": "e1",
                                "description": "Corrigée"})
    assert set(billing["written"]) == {"description"}
    assert "billable" not in billing["written"]


def test_omettre_taxable_n_ajoute_pas_de_tvq(billing):
    handlers.update_expense({"expense_id": "x1", "description": "Corrigé"})
    assert "taxable" not in billing["written"]


def test_un_booleen_faux_atteint_bien_le_modele(billing):
    """Un `or True` quelque part mangerait le False — le piège inverse."""
    handlers.update_time_entry({"time_entry_id": "e1", "billable": False})
    assert billing["written"]["billable"] is False


def test_omettre_les_deux_cles_de_phase_n_efface_pas_la_classification(billing):
    """_resolve_phase_pair({}) rend ("", ""), et les modèles écrivent le
    document ENTIER : écrire ce couple effacerait la classification."""
    handlers.update_time_entry({"time_entry_id": "e1", "hours": 2.0})
    assert "phase" not in billing["written"]
    assert "sous_phase" not in billing["written"]


def test_nommer_une_seule_cle_de_phase_ecrit_les_deux(billing):
    """apply_sous_phase_default impute mais ne RÉPARE pas : une re-phase
    seule contre un sous-code étranger stocké serait rejetée par _validate."""
    handlers.update_time_entry({"time_entry_id": "e1", "phase": "INS"})
    assert billing["written"]["phase"] == "INS"
    assert billing["written"]["sous_phase"] == "INS-00"


def test_un_couple_de_phase_contradictoire_est_refuse(billing):
    with pytest.raises(tools.ToolArgumentError, match="n'appartient pas"):
        handlers.update_time_entry({"time_entry_id": "e1", "phase": "INS",
                                    "sous_phase": "CTS-02"})


@pytest.mark.parametrize("forbidden", ["dossier_id", "invoiced", "invoice_id",
                                       "amount", "id"])
def test_les_champs_derives_ou_d_imputation_ne_sont_pas_adressables(forbidden):
    for name in ("update_time_entry", "update_expense"):
        props = tools.TOOLS[name]["input_schema"]["properties"]
        assert forbidden not in props, (name, forbidden)


def test_le_montant_d_un_debourse_reste_adressable(billing):
    """Asymétrie voulue : le modèle ne recalcule JAMAIS un déboursé, donc un
    montant historique s'importe et se corrige exactement."""
    assert "amount_cents" in tools.TOOLS["update_expense"]["input_schema"]["properties"]
    handlers.update_expense({"expense_id": "x1", "amount_cents": 5250})
    assert billing["written"]["amount"] == 5250


def test_un_id_inconnu_est_refuse(billing):
    billing["entry"] = None
    with pytest.raises(tools.ToolArgumentError, match="introuvable"):
        handlers.update_time_entry({"time_entry_id": "absent", "hours": 1.0})


def test_sans_aucun_champ_c_est_un_refus(billing):
    with pytest.raises(tools.ToolArgumentError, match="Aucun champ"):
        handlers.update_time_entry({"time_entry_id": "e1"})


# ── Les heures au quart d'heure (D-11) ─────────────────────────────────────


def test_un_quart_d_heure_s_importe_exactement(billing, monkeypatch):
    """round(0.25, 1) == 0.2 : à 300 $/h, 60,00 $ là où la facture papier
    imprime 75,00 $ — en silence, et l'écart faisait ensuite échouer la
    réconciliation de la facture par une différence que l'appelant ne pouvait
    pas combler."""
    created = {}
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": "d1", "file_number": "2019-014",
                                   "title": "T", "status": "actif",
                                   "hourly_rate": 30000})
    monkeypatch.setattr(
        handlers.time_entry_model, "create_time_entry",
        lambda data: (created.update(data) or ({**data, "id": "e",
                                                "amount": 7500}, [])))
    handlers.create_time_entry({"dossier_id": "d1", "date": "2019-11-04",
                                "description": "Appel", "hours": 0.25})
    assert created["hours"] == 0.25

    handlers.update_time_entry({"time_entry_id": "e1", "hours": 0.75})
    assert billing["written"]["hours"] == 0.75


@pytest.mark.parametrize("bad", [0.125, 0.333, 1.0001])
def test_plus_de_deux_decimales_est_refuse_pas_arrondi(billing, bad):
    with pytest.raises(tools.ToolArgumentError, match="deux décimales"):
        handlers.update_time_entry({"time_entry_id": "e1", "hours": bad})
    assert billing["written"] == {}


def test_des_heures_nulles_ou_negatives_restent_refusees(billing):
    for bad in (0, -1.0):
        with pytest.raises(tools.ToolArgumentError, match="positif"):
            handlers.update_time_entry({"time_entry_id": "e1", "hours": bad})


# ── import_invoice ─────────────────────────────────────────────────────────


@pytest.fixture
def facture(monkeypatch):
    import models

    world = {
        "entries": {"e1": {"id": "e1", "dossier_id": "d1", "amount": 45000,
                           "invoiced": False, "description": "Rédaction",
                           "taxable": True}},
        "expenses": {"x1": {"id": "x1", "dossier_id": "d1", "amount": 5000,
                            "invoiced": False, "description": "Timbre",
                            "taxable": True}},
        "call": {}, "parties": {"p1": {"id": "p1", "type": "individual",
                                       "last_name": "Tremblay"}},
        "legacy": {}, "numeros_pris": set(),
    }

    def _create(dossier_id, eids, xids, data, **kw):
        world["call"] = {"dossier_id": dossier_id, "entry_ids": list(eids),
                         "expense_ids": list(xids), "data": dict(data), **kw}
        return {**data, "id": "i-new", "invoice_number": kw.get("invoice_number"),
                "status": "brouillon", "subtotal_fees": 45000,
                "subtotal_expenses": 5000, "subtotal": 50000,
                "gst_amount": 2500, "qst_amount": 4988, "total": 57488}, []

    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": "d1", "file_number": "2019-014",
                                   "title": "T", "status": "fermé",
                                   "clients": [{"id": "p1", "name": "Jean"}]}
                        if i == "d1" else None)
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: world["parties"].get(i))
    monkeypatch.setattr(handlers.time_entry_model, "get_time_entry",
                        lambda i: world["entries"].get(i))
    monkeypatch.setattr(handlers.expense_model, "get_expense",
                        lambda i: world["expenses"].get(i))
    monkeypatch.setattr(handlers.invoice_model, "create_invoice", _create)
    # Explicite : sans cela le vrai lecteur interroge le MagicMock du client
    # Firestore, dont l'itération par défaut est vide — les tests passeraient
    # par accident plutôt que par contrat.
    monkeypatch.setattr(handlers.invoice_model, "invoice_number_exists",
                        lambda n: n in world["numeros_pris"])
    monkeypatch.setattr(models, "find_by_legacy_ref",
                        lambda c, r, limit=5: list(world["legacy"].get(r, [])))
    return world


def _imp(**over):
    base = {"dossier_id": "d1", "invoice_number": "2019-F014",
            "date": "2019-11-08", "expected_total_cents": 57488,
            "time_entry_ids": ["e1"], "expense_ids": ["x1"]}
    base.update(over)
    return base


def test_le_numero_passe_par_le_mot_cle_jamais_par_data(facture):
    """`data` est ce que request.form remplit sur le chemin web : un numéro
    qui y transiterait serait forgeable depuis le formulaire."""
    handlers.import_invoice(_imp())
    call = facture["call"]
    assert call["invoice_number"] == "2019-F014"
    assert "invoice_number" not in call["data"]


def test_les_trois_gardes_du_modele_sont_armees(facture):
    handlers.import_invoice(_imp())
    call = facture["call"]
    assert call["expected_total"] == 57488
    assert call["require_all_sources"] is True


def test_le_statut_et_le_paiement_ne_sont_jamais_transmis(facture):
    """Décision D-4 : le connecteur n'écrit ni statut ni paiement."""
    handlers.import_invoice(_imp())
    for forbidden in ("status", "amount_paid", "paid_date"):
        assert forbidden not in facture["call"]["data"]
    props = tools.TOOLS["import_invoice"]["input_schema"]["properties"]
    for forbidden in ("status", "amount_paid", "paid_date", "subtotal",
                      "total", "gst_amount", "qst_amount"):
        assert forbidden not in props


def test_sans_aucune_source_c_est_un_refus(facture):
    with pytest.raises(tools.ToolArgumentError, match="sources réelles"):
        handlers.import_invoice(_imp(time_entry_ids=[], expense_ids=[]))


def test_le_prevol_nomme_chaque_source_fautive(facture):
    facture["entries"]["e2"] = {"id": "e2", "dossier_id": "d1",
                                "amount": 1000, "invoiced": True,
                                "description": "X"}
    facture["entries"]["e3"] = {"id": "e3", "dossier_id": "autre",
                                "amount": 1000, "invoiced": False,
                                "description": "Y"}
    with pytest.raises(tools.ToolArgumentError) as exc:
        handlers.import_invoice(_imp(
            time_entry_ids=["e1", "e2", "e3", "e-absente"], dry_run=True))
    msg = str(exc.value)
    assert "e2" in msg and "déjà facturée" in msg
    assert "e3" in msg and "autre dossier" in msg
    assert "e-absente" in msg and "introuvable" in msg


def test_la_simulation_calcule_vraiment_les_totaux(facture):
    """Pas une estimation : la vraie compute_totals sur les vraies sources,
    pour que le juriste réconcilie contre le PDF avant d'écrire."""
    payload = handlers.import_invoice(_imp(dry_run=True))
    assert facture["call"] == {}                     # rien n'a été écrit
    entity = payload["entity"]
    assert entity["subtotal_cents"] == 50000
    assert entity["gst_amount_cents"] == 2500
    assert entity["total_cents"] == 57488
    assert entity["status"] == "brouillon"
    assert {l["source_id"] for l in payload["line_preview"]} == {"e1", "x1"}


def test_la_simulation_annonce_un_ecart_de_total(facture):
    payload = handlers.import_invoice(_imp(expected_total_cents=57000,
                                           dry_run=True))
    assert any("REFUSERA" in w for w in payload["warnings"])


def test_un_total_attendu_manquant_est_refuse(facture):
    args = _imp()
    del args["expected_total_cents"]
    with pytest.raises(tools.ToolArgumentError, match="expected_total_cents"):
        handlers.import_invoice(args)


def test_l_ajustement_entre_dans_l_apercu_sans_source(facture):
    payload = handlers.import_invoice(_imp(
        adjustment={"amount_cents": -5000, "description": "Remise"},
        expected_total_cents=1, dry_run=True))
    lignes = payload["line_preview"]
    ajust = [l for l in lignes if not l["source_id"]]
    assert len(ajust) == 1
    assert ajust[0]["amount_cents"] == -5000
    assert payload["line_count"] == 3


def test_un_ajustement_mal_forme_est_refuse_des_la_simulation(facture):
    with pytest.raises(tools.ToolArgumentError, match="description"):
        handlers.import_invoice(_imp(
            adjustment={"amount_cents": -5000}, dry_run=True))


def test_un_client_disparu_est_un_refus_pas_une_adresse_vide(facture):
    """L'adresse est GELÉE sur la facture : une adresse vide sur un document
    que le client détient déjà est irréparable après coup."""
    facture["parties"].clear()
    with pytest.raises(tools.ToolArgumentError, match="introuvable"):
        handlers.import_invoice(_imp())


def test_l_adresse_de_facturation_vient_du_modele_partage(facture):
    handlers.import_invoice(_imp())
    billing = facture["call"]["data"]["billing_address"]
    assert billing["name"] == "Tremblay"
    assert set(billing) == {"name", "street", "unit", "city", "province",
                            "postal_code"}


def test_la_provision_est_transmise(facture):
    """L'exclure rendait inimportable toute facture ayant appliqué une
    provision : le solde resterait gonflé et la facture ne pourrait jamais
    se solder."""
    handlers.import_invoice(_imp(retainer_applied_cents=20000))
    assert facture["call"]["data"]["retainer_applied"] == 20000


def test_le_gel_des_sources_et_le_brouillon_sont_annonces(facture):
    payload = handlers.import_invoice(_imp())
    joined = " ".join(payload["warnings"])
    assert "annulez la facture dans l'application" in joined
    assert "BROUILLON" in joined
    assert "Journal des honoraires" in joined


def test_import_invoice_ne_pretend_aucune_synchronisation(facture):
    """Les factures ne sont pas exposées en DAV : fabriquer ces clés
    annoncerait une synchronisation qui n'existe pas."""
    payload = handlers.import_invoice(_imp())
    assert "ctag_bumped" not in payload
    assert "dav_synced" not in payload


def test_un_refus_du_modele_remonte_tel_quel(facture, monkeypatch):
    monkeypatch.setattr(
        handlers.invoice_model, "create_invoice",
        lambda *a, **kw: (None, ["Le total reconstitué (57488 ¢) ne "
                                 "correspond pas au total attendu (57000 ¢)"]))
    with pytest.raises(tools.ToolArgumentError, match="ne correspond pas"):
        handlers.import_invoice(_imp(expected_total_cents=57000))


# ══════════════════════════════════════════════════════════════════════════
# Revue de code du lot — les six constats, chacun épinglé
# ══════════════════════════════════════════════════════════════════════════


def test_un_bloc_d_adresse_incomplet_n_efface_pas_l_appartement(contacts):
    """CONSTAT 1. Le bloc était émis en entier avec args.get(k, "") : omettre
    « unit » et « postal_code » les écrivait vides, donc les EFFAÇAIT à la
    correction — sur l'adresse à laquelle un client est facturé. Les six clés
    doivent être PRÉSENTES, pas seulement non vides."""
    contacts["existing"] = {"id": "p1", "type": "individual",
                            "last_name": "T", "address_unit": "300",
                            "address_postal_code": "H3B 1A7"}
    quatre = {"address_street": "1 rue X", "address_city": "Laval",
              "address_province": "Québec", "address_country": "Canada"}
    with pytest.raises(tools.ToolArgumentError, match="absente"):
        handlers.update_partie({"partie_id": "p1", **quatre})
    assert contacts["updated"] == {}

    # Envoyées vides EXPLICITEMENT, elles s'effacent — c'est une intention.
    handlers.update_partie({"partie_id": "p1", **quatre,
                            "address_unit": "", "address_postal_code": ""})
    assert contacts["updated"]["address_unit"] == ""


def test_l_ancre_d_import_est_atteignable_sur_le_temps_et_le_debourse():
    """CONSTAT 2. find_imported annonce chercher dans timeentries et
    expenses ; sans legacy_ref sur ces quatre outils, une reprise reprise
    après 24 h n'y trouvait RIEN et recréait chaque ligne."""
    for name in ("create_time_entry", "create_expense",
                 "update_time_entry", "update_expense"):
        assert "legacy_ref" in tools.TOOLS[name]["input_schema"]["properties"]


def test_les_collections_cherchees_sont_celles_qu_on_peut_ancrer():
    """La cohérence elle-même, dérivée : toute collection que find_imported
    interroge doit avoir au moins un outil capable d'y poser l'ancre."""
    ancrables = {
        coll
        for name, spec in tools.TOOLS.items()
        if "legacy_ref" in spec["input_schema"]["properties"]
        for coll in _collections_written_by(name)
    }
    cherchees = {coll for _t, coll in handlers._LEGACY_COLLECTIONS}
    assert cherchees <= ancrables, cherchees - ancrables


def _collections_written_by(tool: str) -> set:
    return {
        "create_partie": {"parties"}, "update_partie": {"parties"},
        "create_dossier": {"dossiers"}, "update_dossier": {"dossiers"},
        "create_time_entry": {"timeentries"},
        "update_time_entry": {"timeentries"},
        "create_expense": {"expenses"}, "update_expense": {"expenses"},
        "import_invoice": {"invoices"},
    }.get(tool, set())


def test_l_audit_se_tait_quand_les_sources_sont_illisibles(audit):
    """CONSTAT 3. list_*_page échoue OUVERT à ([], None) : une panne
    Firestore était indiscernable de « ce dossier n'a aucun travail », et
    tous les postes de toutes les factures passaient alors pour orphelins —
    des manquements fabriqués contre une reprise parfaitement saine."""
    audit["entries"] = []          # la « panne »
    audit["expenses"] = []
    audit["invoices"] = [{"id": "i1", "invoice_number": "2019-F014",
                          "status": "payée", "subtotal": 45000,
                          "total": 45000}]
    audit["line_items"] = {"i1": [{"id": "li1", "source_id": "e1",
                                   "amount": 45000}]}
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    codes = {f["code"] for f in payload["findings"]}
    assert "IMP-03" not in codes and "IMP-06" not in codes
    assert payload["checks_skipped"] == ["IMP-03", "IMP-06"]


def test_l_audit_ne_se_tait_pas_sur_une_facture_sans_source_citee(audit):
    """Le garde ne doit pas devenir un silence général : une facture dont
    AUCUN poste ne cite de source (rien qu'un ajustement) n'est pas une
    lecture ratée."""
    audit["invoices"] = [{"id": "i1", "invoice_number": "F1",
                          "status": "payée", "subtotal": -5000,
                          "total": -5000}]
    audit["line_items"] = {"i1": [{"id": "li1", "source_id": "",
                                   "amount": -5000}]}
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    assert payload["checks_skipped"] == []


def test_la_simulation_refuse_un_numero_deja_pris(facture):
    """CONSTAT 4. La branche sèche ne rejouait ni la validation du numéro ni
    le contrôle d'unicité : elle annonçait « created: true » pour un numéro
    que l'appel réel refuse — le refus le plus probable d'une reprise
    relancée."""
    facture["numeros_pris"].add("2019-F014")
    with pytest.raises(tools.ToolArgumentError, match="existe déjà"):
        handlers.import_invoice(_imp(dry_run=True))
    # …et l'appel réel le refuse identiquement.
    with pytest.raises(tools.ToolArgumentError, match="existe déjà"):
        handlers.import_invoice(_imp())
    assert facture["call"] == {}


def test_la_simulation_refuse_le_millesime_courant(facture):
    from utils.deadlines import today_mtl

    annee = today_mtl().strftime("%Y")
    with pytest.raises(tools.ToolArgumentError, match="millésime en cours"):
        handlers.import_invoice(_imp(invoice_number=f"{annee}-F031",
                                     dry_run=True))


def test_la_simulation_refuse_une_source_en_double(facture):
    with pytest.raises(tools.ToolArgumentError, match="double"):
        handlers.import_invoice(_imp(time_entry_ids=["e1", "e1"],
                                     dry_run=True))


def test_update_dossier_vide_un_champ_texte_au_lieu_de_l_ignorer(dossiers):
    """CONSTAT 5. « » était SAUTÉ, donc update_dossier rendait
    « updated: true » sans avoir rien fait du champ — alors qu'il sait vider
    title et sommaire. Un no-op silencieux sur un outil de remplacement."""
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif", "fee_notes": "ancienne note"}
    handlers.update_dossier({"dossier_id": "d1", "fee_notes": ""})
    assert dossiers["updated"]["fee_notes"] == ""


def test_update_dossier_refuse_de_vider_ce_qu_il_ne_sait_pas_vider(dossiers):
    """Une date ou un montant dont la forme vide est None ou une valeur
    dérivée : refuser est honnête, ignorer ne l'était pas."""
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif"}
    for champ in ("droit_action_date", "valeur", "hourly_rate"):
        with pytest.raises(tools.ToolArgumentError, match="ne peut pas être vidé"):
            handlers.update_dossier({"dossier_id": "d1", champ: ""})


def test_complete_dossier_continue_d_ignorer_un_vide(dossiers):
    """L'autre moitié du contrat : complete_dossier REMPLIT ce qui est vide,
    donc une valeur vide n'y est rien à faire, pas une instruction."""
    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif"}
    with pytest.raises(tools.ToolArgumentError, match="Aucun champ"):
        handlers.complete_dossier({"dossier_id": "d1", "fee_notes": ""})


def test_le_sommaire_voyage_a_son_vrai_plafond(dossiers):
    """CONSTAT 6. Le schéma annonçait 5000 (ce que le modèle stocke) et le
    gestionnaire refusait au-delà de 2000, avec un message citant une limite
    qui ne s'applique pas à ce champ."""
    for name in ("create_dossier", "update_dossier", "complete_dossier"):
        props = tools.TOOLS[name]["input_schema"]["properties"]
        assert props["sommaire"]["maxLength"] == 5000, name

    dossiers["existing"] = {"id": "d1", "file_number": "2019-014",
                            "status": "actif"}
    handlers.update_dossier({"dossier_id": "d1", "sommaire": "x" * 4000})
    assert len(dossiers["updated"]["sommaire"]) == 4000


def test_les_deux_nouveaux_outils_restent_en_lecture_seule():
    """Ils informent la reprise ; ils n'écrivent rien. Un scope d'écriture
    déclaré ici les retirerait d'un jeton en lecture seule sans raison."""
    for name in ("get_reference_vocabulary", "find_imported",
                 "get_import_audit"):
        assert name not in tools.WRITE_TOOLS
        assert tools.required_scope(name) == "athena:read"
        assert "scope" not in tools.TOOLS[name]
