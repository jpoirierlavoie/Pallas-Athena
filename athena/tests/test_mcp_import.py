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


def test_les_deux_nouveaux_outils_restent_en_lecture_seule():
    """Ils informent la reprise ; ils n'écrivent rien. Un scope d'écriture
    déclaré ici les retirerait d'un jeton en lecture seule sans raison."""
    for name in ("get_reference_vocabulary", "find_imported",
                 "get_import_audit"):
        assert name not in tools.WRITE_TOOLS
        assert tools.required_scope(name) == "athena:read"
        assert "scope" not in tools.TOOLS[name]
