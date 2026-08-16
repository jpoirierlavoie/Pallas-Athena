"""Reprise d'une facture historique (models/invoice.create_invoice).

Les quatre arguments par mot-clé de l'import : le numéro d'origine, le total
attendu, l'exigence que TOUTES les sources soient utilisables, et le poste
d'ajustement nommé.

L'invariant central, celui que `test_un_import_ne_touche_jamais_le_compteur_
annuel` épingle : une facture importée ne lit ni n'avance jamais
`counters/invoices-{année}`. Le compteur appartient à la numérotation vivante
de l'application ; un numéro repris appartient à l'ancien système.

Même approche que test_invoice_numbering : on bouche les bibliothèques
absentes d'un interpréteur nu, et on remplace invoice.firestore pour que
@firestore.transactional devienne un décorateur identité — sinon le vrai
décorateur piloterait la fausse Transaction et échouerait.
"""

import importlib
import importlib.util
import os
import sys
import types
from datetime import date, datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UTC = timezone.utc


def _avail(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _stub(name, module):
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg not in sys.modules:
            if _avail(pkg):
                importlib.import_module(pkg)
                continue
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m
            if i > 1:
                setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], m)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


if not _avail("google.cloud.firestore"):
    _fs = types.ModuleType("google.cloud.firestore")
    _fs.Client = mock.MagicMock(name="firestore.Client")
    _fs.Query = type("Query", (), {"ASCENDING": "ASCENDING", "DESCENDING": "DESCENDING"})
    _fs.Transaction = type("Transaction", (), {})
    _fs.transactional = lambda fn: fn
    _stub("google.cloud.firestore", _fs)
if not _avail("google.cloud.firestore_v1.base_query"):
    _bq = types.ModuleType("google.cloud.firestore_v1.base_query")
    _bq.FieldFilter = type(
        "FieldFilter",
        (),
        {"__init__": lambda s, field_path=None, op_string=None, value=None, **k: None},
    )
    _stub("google.cloud.firestore_v1.base_query", _bq)
if not _avail("icalendar"):
    _stub("icalendar", types.ModuleType("icalendar"))
if not _avail("firebase_admin"):
    _fa = types.ModuleType("firebase_admin")
    _fa.__path__ = []
    _stub("firebase_admin", _fa)
    _stub("firebase_admin.auth", types.ModuleType("firebase_admin.auth"))


with mock.patch("google.cloud.firestore.Client"):
    import models.expense as expense_model
    import models.invoice as invoice
    import models.time_entry as time_entry_model


# ── Faux Firestore ─────────────────────────────────────────────────────────
# Étendu par rapport à test_invoice_numbering : create_invoice a besoin de
# limit(), d'une sous-collection (lineitems) et de txn.update().


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Query:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._filters = []
        self._limit = None

    def where(self, filter=None):
        self._filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self, transaction=None):
        rows = list(self._store.get(self._coll, {}).values())
        for fp, _op, val in self._filters:
            rows = [d for d in rows if d.get(fp) == val]
        if self._limit is not None:
            rows = rows[: self._limit]
        return [_Snap(d.get("id"), d) for d in rows]


class _DocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def get(self, transaction=None):
        return _Snap(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = dict(data)

    def update(self, data):
        self._store.setdefault(self._coll, {}).setdefault(self.id, {}).update(data)

    def collection(self, name):
        return _Coll(self._store, f"{self._coll}/{self.id}/{name}")


class _Coll(_Query):
    def document(self, doc_id):
        return _DocRef(self._store, self._coll, doc_id)


class _Txn:
    def __init__(self, store):
        self._store = store

    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, data):
        ref.update(data)


class _DB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _Coll(self._store, name)

    def transaction(self):
        return _Txn(self._store)


class _FS:
    transactional = staticmethod(lambda fn: fn)
    Transaction = object

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


class _FF:
    def __init__(self, field_path=None, op_string=None, value=None, **_k):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


DOSSIER = "d1"
TODAY = date(2026, 6, 15)


@pytest.fixture
def store(monkeypatch):
    s = {"invoices": {}, "counters": {}, "timeentries": {}, "expenses": {}}
    db = _DB(s)
    monkeypatch.setattr(invoice, "db", db)
    monkeypatch.setattr(invoice, "firestore", _FS)
    monkeypatch.setattr(invoice, "FieldFilter", _FF)
    # Millésime FIGÉ — jamais un offset dérivé de l'horloge (la leçon du
    # build cassé à 00 h 03 UTC le 2026-08-11). _is_live_sequence_number lit
    # today_mtl, et c'est ce gel qui rend « 2026-F… » refusable de façon
    # déterministe.
    monkeypatch.setattr(invoice, "today_mtl", lambda: TODAY)

    # create_invoice importe get_time_entry / get_expense LOCALEMENT, donc
    # l'attribut est relu à l'appel : il faut les remplacer sur LEUR module,
    # pas sur models.invoice.
    # Rendre une COPIE, comme doc.to_dict() côté Firestore. Rendre le dict
    # vivant ferait qu'une écriture ultérieure dans le magasin modifierait
    # rétroactivement l'instantané de pré-lecture — et le test de course sur
    # l'etag ne pourrait alors jamais être mis en scène.
    monkeypatch.setattr(
        time_entry_model,
        "get_time_entry",
        lambda i: dict(s["timeentries"][i]) if i in s["timeentries"] else None,
    )
    monkeypatch.setattr(
        expense_model,
        "get_expense",
        lambda i: dict(s["expenses"][i]) if i in s["expenses"] else None,
    )
    return s


def _entry(store, eid, *, hours=1.5, rate=30000, dossier_id=DOSSIER, invoiced=False):
    store["timeentries"][eid] = {
        "id": eid,
        "dossier_id": dossier_id,
        "invoiced": invoiced,
        "invoice_id": None,
        "etag": f"etag-{eid}",
        "date": datetime(2019, 11, 4, tzinfo=UTC),
        "description": "Rédaction de la demande",
        "hours": hours,
        "rate": rate,
        "amount": int(round(hours * rate)),
    }
    return eid


def _disb(store, xid, *, amount=5000, taxable=True, dossier_id=DOSSIER, invoiced=False):
    store["expenses"][xid] = {
        "id": xid,
        "dossier_id": dossier_id,
        "invoiced": invoiced,
        "invoice_id": None,
        "etag": f"etag-{xid}",
        "date": datetime(2019, 11, 5, tzinfo=UTC),
        "description": "Timbre judiciaire",
        "amount": amount,
        "taxable": taxable,
    }
    return xid


def _data(**over):
    base = {"dossier_id": DOSSIER, "date": datetime(2019, 11, 8, tzinfo=UTC)}
    base.update(over)
    return base


def _create(store, **kw):
    entries = kw.pop("entries", [])
    disbs = kw.pop("disbs", [])
    data = kw.pop("data", None) or _data()
    return invoice.create_invoice(DOSSIER, entries, disbs, data, **kw)


# ── Le numéro d'origine ────────────────────────────────────────────────────


def test_un_numero_importe_est_conserve_tel_quel(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="2019-F014")
    assert errors == []
    assert doc["invoice_number"] == "2019-F014"


def test_un_import_ne_touche_jamais_le_compteur_annuel(store, monkeypatch):
    """L'épingle de la décision D-2.

    Le générateur est remplacé par une explosion : s'il est seulement
    ATTEINT, le test casse. Et le document compteur reste absent.
    """
    def _boom():
        raise AssertionError("le compteur ne doit jamais être lu sur un import")

    monkeypatch.setattr(invoice, "_generate_invoice_number", _boom)
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="2019-F014")
    assert errors == []
    assert doc["invoice_number"] == "2019-F014"
    assert store["counters"] == {}


def test_sans_numero_explicite_le_compteur_reprend_la_main(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e])
    assert errors == []
    assert doc["invoice_number"] == "2026-F001"
    assert store["counters"]["invoices-2026"]["seq"] == 1


def test_data_invoice_number_ne_peut_pas_forger_un_numero(store):
    """`data` vient de request.form sur le chemin web — il ne doit jamais
    pouvoir porter un numéro."""
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], data=_data(invoice_number="FORGÉ"))
    assert errors == []
    assert doc["invoice_number"] == "2026-F001"


def test_un_numero_deja_utilise_est_refuse_et_rien_n_est_ecrit(store):
    store["invoices"]["autre"] = {"id": "autre", "invoice_number": "2019-F014"}
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="2019-F014")
    assert doc is None
    assert "existe déjà" in errors[0]
    # Ni facture, ni source basculée.
    assert set(store["invoices"]) == {"autre"}
    assert store["timeentries"][e]["invoiced"] is False


def test_un_numero_du_millesime_courant_est_refuse(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="2026-F031")
    assert doc is None
    assert "millésime en cours" in errors[0]
    assert store["invoices"] == {}


def test_un_numero_d_une_annee_passee_est_accepte(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="2019-F007")
    assert errors == []
    assert doc["invoice_number"] == "2019-F007"


def test_un_numero_d_une_annee_future_est_accepte(store):
    # Son compteur n'existe pas encore : son premier usage s'amorcera sur
    # _scan_max_invoice_seq, qui verra ce numéro et continuera au-dessus.
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="2027-F003")
    assert errors == []
    assert doc["invoice_number"] == "2027-F003"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "A" * 33, "2019/F<014>", "2019\nF014", "FAC#014", "2019_F014"],
)
def test_un_numero_vide_trop_long_ou_a_caracteres_interdits_est_refuse(store, bad):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number=bad)
    assert doc is None
    assert errors
    assert store["invoices"] == {}


def test_les_blancs_de_bordure_sont_normalises_pas_refuses(store):
    """.strip() est une normalisation, pas une troncature : un numéro copié
    d'un PDF traîne souvent une espace ou un saut de ligne."""
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="  2019-F014\n")
    assert errors == []
    assert doc["invoice_number"] == "2019-F014"


def test_un_numero_trop_long_est_refuse_jamais_tronque(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number="B" * 40)
    assert doc is None
    assert "jamais tronqué" in errors[0]


def test_un_numero_importe_n_est_pas_un_entier(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], invoice_number=1042)
    assert doc is None
    assert "chaîne" in errors[0]


# ── require_all_sources ────────────────────────────────────────────────────


def test_une_source_manquante_facturee_ou_etrangere_est_refusee_avec_son_motif(store):
    ok = _entry(store, "e1")
    facturee = _entry(store, "e2", invoiced=True)
    etrangere = _entry(store, "e3", dossier_id="autre-dossier")
    doc, errors = _create(
        store,
        entries=[ok, facturee, etrangere, "e-inexistante"],
        invoice_number="2019-F014",
        require_all_sources=True,
    )
    assert doc is None
    message = errors[0]
    assert "e2" in message and "déjà facturée" in message
    assert "e3" in message and "autre dossier" in message
    assert "e-inexistante" in message and "introuvable" in message
    assert store["invoices"] == {}
    assert store["timeentries"][ok]["invoiced"] is False


def test_sans_le_drapeau_une_source_inutilisable_reste_escamotee(store):
    """Le comportement historique du chemin web, inchangé."""
    ok = _entry(store, "e1")
    _entry(store, "e2", invoiced=True)
    doc, errors = _create(store, entries=[ok, "e2"])
    assert errors == []
    assert doc["subtotal_fees"] == 45000  # une seule ligne retenue


def test_un_id_en_double_dans_une_liste_est_refuse(store):
    e = _entry(store, "e1")
    doc, errors = _create(
        store,
        entries=[e, e],
        invoice_number="2019-F014",
        require_all_sources=True,
    )
    assert doc is None
    assert "double" in errors[0]
    assert "e1" in errors[0]


# ── expected_total ─────────────────────────────────────────────────────────


def test_le_total_attendu_qui_correspond_laisse_passer(store):
    e = _entry(store, "e1")
    # 1,5 h × 300,00 $ = 450,00 $ ; TPS 22,50 $ ; TVQ 44,89 $ (arrondi
    # ROUND_HALF_UP sur 44,8875 $) ; total 517,39 $. Arithmétique épinglée
    # en dur — c'est le chiffre que le juriste comparera au papier.
    doc, errors = _create(
        store, entries=[e], invoice_number="2019-F014", expected_total=51739
    )
    assert errors == []
    assert doc["total"] == 51739
    assert doc["gst_amount"] == 2250
    assert doc["qst_amount"] == 4489


def test_un_total_attendu_different_ne_brule_aucun_numero(store):
    e = _entry(store, "e1")
    doc, errors = _create(
        store, entries=[e], invoice_number="2019-F014", expected_total=51700
    )
    assert doc is None
    assert "ne correspond pas" in errors[0]
    assert "51739" in errors[0] and "51700" in errors[0]
    assert store["invoices"] == {}
    assert store["counters"] == {}
    assert store["timeentries"][e]["invoiced"] is False


def test_le_refus_de_total_nomme_les_sources_escamotees(store):
    """La moitié actionnable du message : « 1 ligne retenue pour 2 fournies »
    désigne l'escamotage silencieux comme cause probable de l'écart."""
    ok = _entry(store, "e1")
    _entry(store, "e2", invoiced=True)
    doc, errors = _create(store, entries=[ok, "e2"], expected_total=99999)
    assert doc is None
    assert "1 ligne(s) retenue(s) pour 2 source(s) fournie(s)" in errors[0]


def test_un_total_attendu_non_entier_est_refuse(store):
    e = _entry(store, "e1")
    for bad in ("51739", 517.39, True):
        doc, errors = _create(store, entries=[e], expected_total=bad)
        assert doc is None
        assert "entier de cents" in errors[0]


def test_sans_total_attendu_le_comportement_est_inchange(store):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e])
    assert errors == []
    assert doc["total"] == 51739


# ── Le poste d'ajustement ──────────────────────────────────────────────────


def test_un_ajustement_nomme_entre_comme_poste_d_honoraires(store):
    e = _entry(store, "e1")
    doc, errors = _create(
        store,
        entries=[e],
        invoice_number="2019-F014",
        adjustment={
            "amount_cents": -5000,
            "description": "Remise de courtoisie",
        },
    )
    assert errors == []
    # Une remise réduit des HONORAIRES : elle tombe dans subtotal_fees, donc
    # dans la colonne « Honoraires » du journal du Barreau, et laisse
    # expense_split intact.
    assert doc["subtotal_fees"] == 40000
    assert doc["subtotal_expenses"] == 0
    items = list(store["invoices/" + doc["id"] + "/lineitems"].values())
    ajust = [i for i in items if i["description"] == "Remise de courtoisie"]
    assert len(ajust) == 1
    assert ajust[0]["type"] == "fee"
    assert ajust[0]["amount"] == -5000
    assert not ajust[0]["source_id"]      # le SEUL poste sans source
    assert ajust[0]["hours"] is None and ajust[0]["rate"] is None


def test_un_ajustement_taxable_bouge_la_base_de_taxe(store):
    e = _entry(store, "e1")
    doc, _ = _create(
        store,
        entries=[e],
        adjustment={"amount_cents": -5000, "description": "Remise"},
    )
    # Base taxable 400,00 $ → TPS 20,00 $.
    assert doc["gst_amount"] == 2000


def test_un_ajustement_non_taxable_ne_bouge_pas_les_taxes(store):
    e = _entry(store, "e1")
    doc, _ = _create(
        store,
        entries=[e],
        adjustment={
            "amount_cents": -5000,
            "description": "Remise après taxes",
            "taxable": False,
        },
    )
    # Base taxable inchangée à 450,00 $ : la remise a été appliquée APRÈS
    # les taxes sur la facture d'origine.
    assert doc["gst_amount"] == 2250
    assert doc["subtotal_fees"] == 40000


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"amount_cents": 0, "description": "Rien"}, "n'explique rien"),
        ({"amount_cents": -500}, "description` est requise"),
        ({"amount_cents": -500, "description": "   "}, "description` est requise"),
        ({"amount_cents": "-500", "description": "X"}, "entier de cents"),
        ({"amount_cents": True, "description": "X"}, "entier de cents"),
        ({"amount_cents": -500, "description": "X" * 501}, "dépasse"),
        ({"amount_cents": -500, "description": "X", "taxable": "oui"}, "booléen"),
        ("pas un objet", "objet"),
    ],
)
def test_un_ajustement_mal_forme_est_refuse(store, bad, fragment):
    e = _entry(store, "e1")
    doc, errors = _create(store, entries=[e], adjustment=bad)
    assert doc is None
    assert fragment in errors[0]
    assert store["invoices"] == {}


# ── Invariants que la reprise rend enfin testables ─────────────────────────


def test_les_sources_retenues_passent_a_facturee(store):
    e = _entry(store, "e1")
    x = _disb(store, "x1")
    doc, errors = _create(store, entries=[e], disbs=[x], invoice_number="2019-F014")
    assert errors == []
    assert store["timeentries"][e]["invoiced"] is True
    assert store["timeentries"][e]["invoice_id"] == doc["id"]
    assert store["expenses"][x]["invoiced"] is True


def test_un_etag_source_modifie_avorte_toute_la_creation(store, monkeypatch):
    e = _entry(store, "e1")
    original = time_entry_model.get_time_entry

    def _shifting(i):
        row = original(i)
        # La pré-lecture voit un etag, la relecture transactionnelle en voit
        # un autre : exactement la course que _SourceConflictError existe
        # pour attraper.
        store["timeentries"][i]["etag"] = "etag-changé"
        return row

    monkeypatch.setattr(time_entry_model, "get_time_entry", _shifting)
    doc, errors = _create(store, entries=[e], invoice_number="2019-F014")
    assert doc is None
    assert "modifiées ou facturées" in errors[0]
    assert store["invoices"] == {}


def test_la_provision_hors_bornes_reste_refusee(store):
    e = _entry(store, "e1")
    for bad in (-1, 51740):
        doc, errors = _create(store, entries=[e], data=_data(retainer_applied=bad))
        assert doc is None
        assert "provision" in errors[0]


def test_une_facture_importee_nait_brouillon(store):
    e = _entry(store, "e1")
    doc, _ = _create(store, entries=[e], invoice_number="2019-F014")
    assert doc["status"] == "brouillon"
    assert doc["amount_paid"] == 0
    assert doc["paid_date"] is None


def test_la_date_fournie_est_conservee_et_la_due_date_en_derive(store):
    e = _entry(store, "e1")
    doc, _ = _create(store, entries=[e], invoice_number="2019-F014")
    assert doc["date"] == datetime(2019, 11, 8, tzinfo=UTC)
    assert doc["due_date"] == datetime(2019, 12, 8, tzinfo=UTC)


# ── billing_address_from ───────────────────────────────────────────────────
# Remontée verbatim de routes/invoices._build_billing_address pour que le
# formulaire web et le connecteur ne puissent pas facturer le même client à
# deux adresses différentes selon la surface qui émet.


def test_l_adresse_de_facturation_prefere_le_bloc_professionnel():
    partie = {
        "type": "individual", "first_name": "Jean", "last_name": "Tremblay",
        "address_street": "1 rue Personnelle", "address_city": "Laval",
        "work_address_street": "450 rue Sainte-Catherine Ouest",
        "work_address_unit": "300", "work_address_city": "Montréal",
        "work_address_province": "Québec", "work_address_postal_code": "H3B 1A7",
    }
    assert invoice.billing_address_from(partie) == {
        "name": "Jean Tremblay",
        "street": "450 rue Sainte-Catherine Ouest",
        "unit": "300",
        "city": "Montréal",
        "province": "Québec",
        "postal_code": "H3B 1A7",
    }


def test_l_adresse_de_facturation_retombe_sur_le_bloc_personnel():
    partie = {
        "type": "individual", "first_name": "Jean", "last_name": "Tremblay",
        "address_street": "1 rue Personnelle", "address_city": "Laval",
        "address_province": "Québec", "address_postal_code": "H7A 1A1",
    }
    got = invoice.billing_address_from(partie)
    assert got["street"] == "1 rue Personnelle" and got["city"] == "Laval"


def test_l_adresse_de_facturation_nomme_une_personne_morale_par_son_nom_legal():
    partie = {"type": "organization", "organization_name": "Béton Nord inc.",
              "trade_name": "Béton Nord"}
    assert invoice.billing_address_from(partie)["name"] == "Béton Nord inc."


def test_l_adresse_de_facturation_porte_toujours_la_cle_name():
    """utils/invoice_docx._partie_from_billing_address mappe billing["name"]
    vers le destinataire du Word. selected_address, l'autorité par RÔLE, ne
    rend AUCUNE clé « name » — d'où le refus documenté d'y basculer ici : le
    document sortirait sans destinataire."""
    got = invoice.billing_address_from({"type": "individual", "last_name": "X"})
    assert set(got) == {"name", "street", "unit", "city", "province", "postal_code"}
    assert got["province"] == "QC"      # le défaut historique, inchangé
