"""Reclassement de phase — la seule écriture qui franchit le mur `invoiced`.

Le mur protège les CHIFFRES de la facture. `phase`/`sous_phase` n'en sont
pas : ils ne figurent sur aucune facture (les postes sont des copies
indépendantes qui n'en portent pas), sur aucun gabarit, dans aucun
sérialiseur DAV — ils ne nourrissent que `budget.aggregate_actuals`, laquelle
compte le travail FACTURÉ. D'où ce lot.

Ce que ces tests épinglent, dans l'ordre d'importance :

1. La FORME de l'écriture. Le modèle fait un ``update()`` partiel de QUATRE
   clés, jamais le ``set()`` du document fusionné qu'exécute
   ``update_time_entry`` : c'est ce qui rend la fonction structurellement
   incapable de déplacer un montant. Une promesse se relit, une forme se
   prouve.
2. Le mur n'a pas bougé : ``update_time_entry`` refuse toujours une entrée
   facturée, ce qui est précisément ce qui rend vrai le
   « invoiced : toujours faux » de SON schéma de sortie.
3. Le saut du non-changement, qui rend une passe de reclassement rejouable.
4. La branche SÈCHE refuse tout ce que l'appel réel refuse — ``run_write``
   court-circuite le dry_run sans jamais appeler le modèle.

Les tests d'intégration font tourner les VRAIS modèles au-dessus d'une fausse
Firestore : monkeypatcher les modèles rendrait vert un gestionnaire branché
sur rien.
"""

import os
import sys
from datetime import datetime, timezone
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
    import mcp.write_support as write_support
    from models import expense as expense_model
    from models import time_entry as time_entry_model

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
SEEDED = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)


# ── Fausse Firestore, avec mouchards sur set() et update() ────────────────


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def get(self):
        return _Snap(self.id, self._store[self._coll].get(self.id))

    def set(self, data):
        # Recorded, never expected: a full-document set() on this path is
        # the failure mode the whole design exists to make impossible.
        self._store["_sets"].append((self._coll, self.id, dict(data)))
        self._store[self._coll][self.id] = dict(data)

    def update(self, data):
        self._store["_updates"].append((self._coll, self.id, dict(data)))
        self._store[self._coll][self.id].update(data)


class _Coll:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll

    def document(self, doc_id):
        return _DocRef(self._store, self._coll, doc_id)


class _DB:
    def __init__(self, store):
        self._store = store
        self.get_all_raises = False

    def collection(self, name):
        self._store.setdefault(name, {})
        return _Coll(self._store, name)

    def get_all(self, refs):
        if self.get_all_raises:
            raise RuntimeError("firestore unavailable")
        return [r.get() for r in refs]


def _time_seed(**over) -> dict:
    doc = {
        "id": "e1", "dossier_id": "d1", "dossier_file_number": "2025-001",
        "dossier_title": "Tremblay c. Lavoie", "date": DT,
        "description": "Rédaction de la défense", "hours": 1.5,
        "rate": 30000, "amount": 45000, "billable": True,
        "invoiced": True, "invoice_id": "i1", "phase": "", "sous_phase": "",
        "legacy_ref": "", "created_at": SEEDED, "updated_at": SEEDED,
        "etag": "etag-0",
    }
    doc.update(over)
    return doc


def _expense_seed(**over) -> dict:
    doc = {
        "id": "x1", "dossier_id": "d1", "dossier_file_number": "2025-001",
        "dossier_title": "Tremblay c. Lavoie", "date": DT,
        "description": "Timbre judiciaire", "category": "timbre_judiciaire",
        "amount": 5000, "taxable": True, "invoiced": True, "invoice_id": "i1",
        "receipt_document_id": None, "phase": "", "sous_phase": "",
        "legacy_ref": "", "created_at": SEEDED, "updated_at": SEEDED,
        "etag": "etag-0",
    }
    doc.update(over)
    return doc


@pytest.fixture
def db(monkeypatch):
    store = {"timeentries": {}, "expenses": {}, "_sets": [], "_updates": []}
    fake = _DB(store)
    monkeypatch.setattr(time_entry_model, "db", fake)
    monkeypatch.setattr(expense_model, "db", fake)
    store["timeentries"]["e1"] = _time_seed()
    store["expenses"]["x1"] = _expense_seed()
    fake.store = store
    return fake


# Both models, one contract. `code` is a sub-code of a phase neither seed
# carries, so every "applied" assertion is a real change.
_KINDS = {
    "time_entry": {
        "collection": "timeentries", "row_id": "e1", "id_key": "time_entry_id",
        "setter": lambda: time_entry_model.set_time_entry_phase,
        "bulk": lambda: time_entry_model.get_time_entries_bulk,
        "seed": _time_seed,
    },
    "expense": {
        "collection": "expenses", "row_id": "x1", "id_key": "expense_id",
        "setter": lambda: expense_model.set_expense_phase,
        "bulk": lambda: expense_model.get_expenses_bulk,
        "seed": _expense_seed,
    },
}
_BOTH = pytest.mark.parametrize("kind", list(_KINDS), ids=list(_KINDS))


# ══════════════════════════════════════════════════════════════════════
# 1. La forme de l'écriture — le test porteur du lot
# ══════════════════════════════════════════════════════════════════════


@_BOTH
def test_l_ecriture_ne_porte_QUE_quatre_cles(db, kind):
    """La garantie n'est pas une promesse, c'est la forme de l'écriture.

    Un ``update()`` de quatre clés ne peut pas déplacer un montant ; un
    ``set()`` de document fusionné le pourrait à la première régression.
    """
    cfg = _KINDS[kind]
    doc, errors, changed = cfg["setter"]()(cfg["row_id"], "INT", "INT-01")

    assert errors == [] and changed is True
    assert doc["phase"] == "INT" and doc["sous_phase"] == "INT-01"
    assert len(db.store["_updates"]) == 1
    _, doc_id, payload = db.store["_updates"][0]
    assert doc_id == cfg["row_id"]
    assert set(payload) == {"phase", "sous_phase", "updated_at", "etag"}
    # Et surtout : jamais de set() sur ce chemin.
    assert db.store["_sets"] == []


@_BOTH
def test_une_ligne_facturee_se_reclasse_sans_qu_aucun_chiffre_ne_bouge(db, kind):
    cfg = _KINDS[kind]
    before = dict(db.store[cfg["collection"]][cfg["row_id"]])
    assert before["invoiced"] is True  # la situation même que le lot vise

    _, errors, changed = cfg["setter"]()(cfg["row_id"], "", "AUD-02")
    assert errors == [] and changed is True

    after = db.store[cfg["collection"]][cfg["row_id"]]
    assert after["phase"] == "AUD" and after["sous_phase"] == "AUD-02"
    # Tout le reste, octet pour octet — y compris `invoiced` et `invoice_id`,
    # que ce chemin ne consulte jamais.
    moved = {"phase", "sous_phase", "updated_at", "etag"}
    for key, value in before.items():
        if key not in moved:
            assert after[key] == value, key
    assert after["etag"] != before["etag"]


# ══════════════════════════════════════════════════════════════════════
# 2. Le mur n'a pas bougé
# ══════════════════════════════════════════════════════════════════════


def test_update_time_entry_refuse_toujours_une_entree_facturee(db):
    """Son refus est ce qui rend vrai le « invoiced : toujours faux » de son
    propre schéma de sortie. Le reclassement passe à côté du mur, jamais au
    travers."""
    doc, errors = time_entry_model.update_time_entry("e1", {"hours": 2.0})
    assert doc is None
    assert errors == ["Impossible de modifier une entrée déjà facturée."]
    assert db.store["_updates"] == [] and db.store["_sets"] == []


def test_update_expense_refuse_toujours_un_debourse_facture(db):
    doc, errors = expense_model.update_expense("x1", {"amount": 9999})
    assert doc is None
    assert errors == ["Impossible de modifier une dépense déjà facturée."]
    assert db.store["_updates"] == [] and db.store["_sets"] == []


@_BOTH
def test_le_gestionnaire_n_emprunte_jamais_l_ecrivain_large(db, monkeypatch, kind):
    """Si le chemin étroit routait par update_*, il hériterait de son
    ``set()`` — et de tout ce que ce set() peut déplacer."""
    def _boom(*a, **k):
        raise AssertionError("update_* ne doit jamais être appelé ici")

    monkeypatch.setattr(time_entry_model, "update_time_entry", _boom)
    monkeypatch.setattr(expense_model, "update_expense", _boom)
    cfg = _KINDS[kind]
    _, errors, changed = cfg["setter"]()(cfg["row_id"], "PRE", "PRE-02")
    assert errors == [] and changed is True


# ══════════════════════════════════════════════════════════════════════
# 3. Le saut du non-changement
# ══════════════════════════════════════════════════════════════════════


@_BOTH
def test_le_meme_code_deux_fois_n_ecrit_rien_la_seconde(db, kind):
    """Ce qui rend une passe de reclassement rejouable : pas de churn
    d'``updated_at``, donc pas de fausse piste pour un lecteur qui filtre
    sur `updated_since`."""
    cfg = _KINDS[kind]
    cfg["setter"]()(cfg["row_id"], "CTS", "CTS-02")
    stamped = dict(db.store[cfg["collection"]][cfg["row_id"]])
    db.store["_updates"].clear()

    doc, errors, changed = cfg["setter"]()(cfg["row_id"], "CTS", "CTS-02")
    assert errors == [] and changed is False
    assert db.store["_updates"] == []
    after = db.store[cfg["collection"]][cfg["row_id"]]
    assert after["updated_at"] == stamped["updated_at"]
    assert after["etag"] == stamped["etag"]
    assert doc["sous_phase"] == "CTS-02"


@_BOTH
def test_une_phase_seule_impute_au_00(db, kind):
    cfg = _KINDS[kind]
    doc, errors, _ = cfg["setter"]()(cfg["row_id"], "MEE", "")
    assert errors == []
    assert (doc["phase"], doc["sous_phase"]) == ("MEE", "MEE-00")


@_BOTH
def test_un_sous_code_seul_deduit_sa_phase(db, kind):
    cfg = _KINDS[kind]
    doc, errors, _ = cfg["setter"]()(cfg["row_id"], "", "EXP-03")
    assert errors == []
    assert (doc["phase"], doc["sous_phase"]) == ("EXP", "EXP-03")


# ══════════════════════════════════════════════════════════════════════
# 4. Refus (modèle)
# ══════════════════════════════════════════════════════════════════════


@_BOTH
@pytest.mark.parametrize(
    "phase,sous,fragment",
    [
        ("INS", "CTS-02", "n'appartient pas"),
        ("", "XXX-99", "Sous-phase invalide"),
        ("ZZZ", "", "Phase invalide"),
        ("", "", "phase du litige est requise"),
    ],
    ids=["couple-contradictoire", "sous-code-inconnu", "phase-inconnue",
         "aucun-code"],
)
def test_un_couple_invalide_est_refuse_sans_rien_ecrire(
    db, kind, phase, sous, fragment
):
    cfg = _KINDS[kind]
    doc, errors, changed = cfg["setter"]()(cfg["row_id"], phase, sous)
    assert doc is None and changed is False
    assert any(fragment in e for e in errors), errors
    assert db.store["_updates"] == [] and db.store["_sets"] == []


@_BOTH
def test_un_id_inconnu_est_refuse(db, kind):
    cfg = _KINDS[kind]
    doc, errors, changed = cfg["setter"]()("absent", "PRE", "PRE-01")
    assert doc is None and changed is False and errors
    assert db.store["_updates"] == []


# ══════════════════════════════════════════════════════════════════════
# 5. Les lecteurs en lot échouent FERMÉ
# ══════════════════════════════════════════════════════════════════════


@_BOTH
def test_le_lecteur_en_lot_propage_une_panne(db, kind):
    """Dégradé à ``{}`` — la posture de get_parties_bulk — il fabriquerait
    « introuvable » pour CHAQUE ligne du lot, et l'appelant prendrait ce
    rapport pour argent comptant."""
    cfg = _KINDS[kind]
    db.get_all_raises = True
    with pytest.raises(Exception):
        cfg["bulk"]()([cfg["row_id"]])


@_BOTH
def test_le_lecteur_en_lot_rend_ce_qui_existe(db, kind):
    cfg = _KINDS[kind]
    got = cfg["bulk"]()([cfg["row_id"], "absent", cfg["row_id"]])
    assert set(got) == {cfg["row_id"]}
    assert cfg["bulk"]()([]) == {}


# ══════════════════════════════════════════════════════════════════════
# 6. Le connecteur — outils simples
# ══════════════════════════════════════════════════════════════════════


def test_l_outil_simple_reclasse_une_entree_facturee(db):
    payload = handlers.set_time_entry_phase(
        {"time_entry_id": "e1", "sous_phase": "INT-02"}
    )
    assert payload["outcome"] == "applied"
    assert payload["entity"]["invoiced"] is True   # tout l'objet du lot
    assert payload["entity"]["sous_phase"] == "INT-02"
    assert payload["entity"]["phase_label"] == "Introduction de l'instance"
    # L'écho porte les chiffres, inchangés : c'est la preuve lisible côté
    # appelant que rien d'autre n'a bougé.
    assert payload["entity"]["amount_cents"] == 45000
    assert payload["entity"]["hours"] == 1.5


def test_l_outil_simple_annonce_le_non_changement(db):
    handlers.set_expense_phase({"expense_id": "x1", "phase": "PRE"})
    db.store["_updates"].clear()
    payload = handlers.set_expense_phase({"expense_id": "x1", "phase": "PRE"})
    assert payload["outcome"] == "unchanged"
    assert db.store["_updates"] == []


def test_l_outil_simple_leve_sur_un_refus(db):
    """Un appelant qui a nommé UNE ligne veut une erreur, pas un rapport
    d'une ligne."""
    with pytest.raises(tools.ToolArgumentError, match="introuvable"):
        handlers.set_time_entry_phase(
            {"time_entry_id": "absent", "phase": "PRE"}
        )
    with pytest.raises(tools.ToolArgumentError, match="n'appartient pas"):
        handlers.set_time_entry_phase(
            {"time_entry_id": "e1", "phase": "INS", "sous_phase": "CTS-02"}
        )
    with pytest.raises(tools.ToolArgumentError, match="Aucun code de phase"):
        handlers.set_time_entry_phase({"time_entry_id": "e1"})
    assert db.store["_updates"] == []


# ══════════════════════════════════════════════════════════════════════
# 7. Le connecteur — lots
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def many(db):
    """Cinq entrées facturées, dont une déjà classée."""
    for i in range(2, 6):
        db.store["timeentries"][f"e{i}"] = _time_seed(
            id=f"e{i}", phase="CTS" if i == 5 else "",
            sous_phase="CTS-02" if i == 5 else "",
        )
    return db


def test_le_lot_rend_une_ligne_par_demande_DANS_L_ORDRE(many):
    demandes = [
        {"time_entry_id": "e3", "sous_phase": "AUD-01"},
        {"time_entry_id": "absent", "sous_phase": "PRE-01"},
        {"time_entry_id": "e5", "sous_phase": "CTS-02"},
        {"time_entry_id": "e2", "phase": "INS"},
    ]
    payload = handlers.set_time_entry_phase_bulk({"entries": demandes})

    assert [r["id"] for r in payload["results"]] == [
        "e3", "absent", "e5", "e2"
    ]
    assert [r["outcome"] for r in payload["results"]] == [
        "applied", "refused", "unchanged", "applied"
    ]
    assert payload["requested"] == 4
    assert (payload["applied"], payload["unchanged"], payload["refused"]) == (
        2, 1, 1
    )
    assert (
        payload["applied"] + payload["unchanged"] + payload["refused"]
        == payload["requested"]
    )


def test_une_ligne_refusee_n_arrete_pas_ses_voisines(many):
    payload = handlers.set_time_entry_phase_bulk({"entries": [
        {"time_entry_id": "e2", "sous_phase": "PRE-01"},
        {"time_entry_id": "e3", "phase": "INS", "sous_phase": "CTS-02"},
        {"time_entry_id": "e4", "sous_phase": "PRE-01"},
    ]})
    assert [r["outcome"] for r in payload["results"]] == [
        "applied", "refused", "applied"
    ]
    assert many.store["timeentries"]["e2"]["sous_phase"] == "PRE-01"
    assert many.store["timeentries"]["e4"]["sous_phase"] == "PRE-01"
    assert many.store["timeentries"]["e3"]["sous_phase"] == ""


def test_une_ligne_refusee_n_invente_aucun_fait_sur_elle(many):
    payload = handlers.set_time_entry_phase_bulk({"entries": [
        {"time_entry_id": "absent", "sous_phase": "PRE-01"},
    ]})
    row = payload["results"][0]
    assert row["outcome"] == "refused" and row["reason"]
    # null, jamais un défaut : affirmer « non facturée » d'une ligne qu'on
    # n'a pas lue serait inventer un fait sur elle.
    assert row["dossier_id"] is None and row["invoiced"] is None


def test_un_id_en_double_refuse_TOUT_le_lot(many):
    with pytest.raises(tools.ToolArgumentError, match="figure deux fois"):
        handlers.set_time_entry_phase_bulk({"entries": [
            {"time_entry_id": "e2", "sous_phase": "PRE-01"},
            {"time_entry_id": "e3", "sous_phase": "AUD-01"},
            {"time_entry_id": "e2", "sous_phase": "INS-01"},
        ]})
    assert many.store["_updates"] == []


def test_le_plafond_et_le_lot_vide_sont_refuses(many):
    trop = [{"time_entry_id": f"e{i}", "phase": "PRE"}
            for i in range(tools.PHASE_BULK_MAX + 1)]
    with pytest.raises(tools.ToolArgumentError, match="plafonné"):
        handlers.set_time_entry_phase_bulk({"entries": trop})
    with pytest.raises(tools.ToolArgumentError, match="au moins une ligne"):
        handlers.set_time_entry_phase_bulk({"entries": []})
    assert many.store["_updates"] == []


def test_le_lot_n_altere_aucun_chiffre(many):
    avant = {i: dict(d) for i, d in many.store["timeentries"].items()}
    handlers.set_time_entry_phase_bulk({"entries": [
        {"time_entry_id": i, "sous_phase": "JUG-01"} for i in avant
    ]})
    for i, before in avant.items():
        after = many.store["timeentries"][i]
        for key in ("hours", "rate", "amount", "description", "billable",
                    "invoiced", "invoice_id", "date", "dossier_id"):
            assert after[key] == before[key], (i, key)


# ══════════════════════════════════════════════════════════════════════
# 8. Le gestionnaire valide LUI-MÊME, sans se reposer sur le schéma
# ══════════════════════════════════════════════════════════════════════


def test_les_quatre_familles_de_refus_coexistent_dans_un_lot(many):
    """L'enum de sous-codes du schéma refuserait « XXX-99 », mais il
    refuserait le LOT ENTIER : un rapport ligne par ligne n'existe que si
    le gestionnaire valide chaque couple lui-même. C'est aussi la seule
    garde qui survivrait à une dérive entre l'enum et `utils/phases` —
    sans elle, un code inconnu partirait au modèle et le lot annoncerait
    « applied » là où rien n'a été écrit."""
    payload = handlers.set_time_entry_phase_bulk({"entries": [
        {"time_entry_id": "absent", "sous_phase": "PRE-01"},
        {"time_entry_id": "e2", "phase": "INS", "sous_phase": "CTS-02"},
        {"time_entry_id": "e3"},
        # Un code que l'enum du schéma ne laisse pas passer AUJOURD'HUI.
        {"time_entry_id": "e4", "sous_phase": "XXX-99"},
    ]})
    assert payload["refused"] == 4
    assert [r["outcome"] for r in payload["results"]] == ["refused"] * 4
    assert "Sous-phase invalide" in payload["results"][3]["reason"]
    assert many.store["timeentries"]["e4"]["sous_phase"] == ""
    assert many.store["_updates"] == []


# ══════════════════════════════════════════════════════════════════════
# 9. Idempotence
# ══════════════════════════════════════════════════════════════════════


def test_la_meme_cle_rejoue_le_rapport_sans_reecrire(many, monkeypatch):
    monkeypatch.setattr(write_support, "db", many)
    args = {"entries": [{"time_entry_id": "e2", "sous_phase": "AUD-01"}],
            "idempotency_key": "reclassement-2025-001-01"}

    first = handlers.set_time_entry_phase_bulk(dict(args))
    assert first["applied"] == 1 and first["idempotent_replay"] is False
    many.store["_updates"].clear()

    again = handlers.set_time_entry_phase_bulk(dict(args))
    assert again["idempotent_replay"] is True
    assert again["results"] == first["results"]
    assert many.store["_updates"] == []
