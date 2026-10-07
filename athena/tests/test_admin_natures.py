"""The four administration natures outside the firm's results (2026-10-07).

The lawyer's drawings and contributions (``prélèvement``, ``apport``) and the
firm's transfers to or from an account outside the ledger
(``virement_interne_sortant`` / ``_entrant``) move the balance like any entry
but count in no revenue, expense or tax total. These tests pin the MODEL's
rules (every caller goes through it), the pure ``results_statement`` /
``owner_equity`` authorities, the reversal's ``reverses_kind`` stamp and the
integrity checks that watch the natures (``verify_admin_integrity`` checks 4
and 11, ``verify_trust_integrity`` check 10's candidates).

The bench is the shared fake Firestore (``tests/_fake_firestore.py``): the
real client, transactions and ``transactional`` retry loop; assertions read
what is STORED.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from services import comptabilite as svc

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
# A fictitious year, on a frozen clock: no date nor amount here is one of the
# lawyer's (the repository is public).
TODAY = "2031-06-20T16:00:00+00:00"
SORTIES = ("prélèvement", "virement_interne_sortant")
ENTREES = ("apport", "virement_interne_entrant")


def _d(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(TODAY)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "status": "actif",
        "account_type": "carte_crédit", "ledger_balance": 0, "etag": "e1",
    })
    f.seed("dossiers/dos1", {"id": "dos1", "file_number": "2031-001",
                             "title": "Dossier d'essai", "status": "actif"})
    return f


@pytest.fixture
def refusals(monkeypatch) -> list:
    seen: list = []
    real = al.log_admin_ledger_event

    def _spy(event, outcome="success", **fields):
        if event == "admin_transaction_refused":
            seen.append(fields.get("reason"))
        return real(event, outcome, **fields)

    monkeypatch.setattr(al, "log_admin_ledger_event", _spy)
    return seen


def _entry(kind: str, **over) -> dict:
    d = {
        "account_id": "ops1", "kind": kind, "amount": 41357,
        "method": "virement", "counterparty": "Contrepartie d'essai",
        "date": _d(2031, 6, 2), "description": "", "reference": "",
    }
    if kind == "dépense":
        d["category"] = "autre"
    d.update(over)
    return d


def _create(kind: str, **over) -> dict:
    entry, errs = al.create_transaction(_entry(kind, **over))
    assert errs == [], errs
    return entry


def _stored(fake, entry: dict) -> dict:
    return fake.peek(f"admin_transactions/{entry['id']}")


def _ledger(fake, account: str = "ops1") -> int:
    return fake.peek(f"admin_accounts/{account}")["ledger_balance"]


# ══════════════════════════════════════════════════════════════════════
# 1. Vocabulary — one source, every kind in exactly one bucket
# ══════════════════════════════════════════════════════════════════════


def test_every_kind_falls_in_exactly_one_results_bucket():
    buckets = (al.REVENUE_KINDS, al.EXPENSE_KINDS, al.NON_RESULT_KINDS,
               ("paiement_carte",), (al.REVERSAL_KIND,))
    flat = [k for b in buckets for k in b]
    assert sorted(flat) == sorted(al.VALID_KINDS)
    assert len(flat) == len(set(flat))


def test_the_natures_have_labels_directions_and_are_creatable_and_editable():
    assert set(al.KIND_LABELS) == set(al.VALID_KINDS)
    for kind in al.NON_RESULT_KINDS:
        assert kind in al._SIMPLE_KINDS and kind in al._EDITABLE_KINDS
    assert {k: al._KIND_DIRECTION[k] for k in al.NON_RESULT_KINDS} == {
        "prélèvement": "déboursé", "apport": "recette",
        "virement_interne_sortant": "déboursé",
        "virement_interne_entrant": "recette",
    }


def test_kind_moves_keep_the_sign_except_the_historical_switch():
    """Every passage in the table keeps the sign, but one: the dépense <->
    recette_autre switch that predates the natures (pinned elsewhere)."""
    legacy = {("dépense", "recette_autre"), ("recette_autre", "dépense")}
    for old, news in al.KIND_MOVES.items():
        for new in news:
            if (old, new) in legacy:
                continue
            assert al._KIND_DIRECTION[old] == al._KIND_DIRECTION[new], (old, new)
    assert set(al.KIND_MOVES) == set(al._EDITABLE_KINDS)
    assert svc.ADMIN_KIND_MOVES == {k: tuple(v) for k, v in al.KIND_MOVES.items()}
    assert svc.ADMIN_OWNER_KINDS == al.OWNER_KINDS
    assert svc.ADMIN_NON_RESULT_KINDS == al.NON_RESULT_KINDS


def test_the_form_mapping_is_its_own_inverse():
    for sens, code in al.INTERNAL_TRANSFER_KIND_BY_SENS.items():
        assert al.form_kind(code) == (al.INTERNAL_TRANSFER_FORM_KIND, sens)
    assert al.form_kind("dépense") == ("dépense", "")
    assert set(al.INTERNAL_TRANSFER_KIND_BY_SENS.values()) == set(al.INTERNAL_TRANSFER_KINDS)
    assert set(al.FORM_KINDS) - {al.INTERNAL_TRANSFER_FORM_KIND} <= set(al._SIMPLE_KINDS)


# ══════════════════════════════════════════════════════════════════════
# 2. Create — the sign, the canonical split, the refused fields
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind", SORTIES + ENTREES)
def test_each_nature_takes_its_sign_and_its_canonical_split(fake, kind):
    entry = _create(kind)
    stored = _stored(fake, entry)
    sortie = kind in SORTIES
    assert stored["direction"] == ("déboursé" if sortie else "recette")
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        (41357, 0, 0) if sortie else (0, 0, 0))
    assert _ledger(fake) == (-41357 if sortie else 41357)


@pytest.mark.parametrize("kind", SORTIES + ENTREES)
def test_a_contradicting_direction_is_refused(fake, refusals, kind):
    wrong = "recette" if kind in SORTIES else "déboursé"
    entry, errs = al.create_transaction(_entry(kind, direction=wrong))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["sens_incohérent"]]
    assert refusals == ["sens_incohérent"]


@pytest.mark.parametrize("field, value, reason", [
    ("category", "loyer", "catégorie_interdite"),
    ("net_amount", 31013, "ventilation_interdite"),
    ("gst_amount", 1551, "ventilation_interdite"),
    ("qst_amount", 3093, "ventilation_interdite"),
    ("invoice_id", "inv1", "facture_interdite"),
])
@pytest.mark.parametrize("kind", SORTIES + ENTREES)
def test_a_field_the_nature_never_carries_is_refused(fake, refusals, kind, field, value, reason):
    """Régression — sans la règle, la catégorie était effacée sans un mot,
    la ventilation gardée et la facture ignorée."""
    entry, errs = al.create_transaction(_entry(kind, **{field: value}))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES[reason]]
    assert refusals == [reason]
    assert fake.peek_collection("admin_transactions") == {}


@pytest.mark.parametrize("kind", al.OWNER_KINDS)
def test_the_lawyers_own_money_carries_no_dossier(fake, refusals, kind):
    entry, errs = al.create_transaction(_entry(kind, dossier_id="dos1"))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["dossier_interdit"]]


@pytest.mark.parametrize("kind", al.INTERNAL_TRANSFER_KINDS)
def test_an_internal_transfer_may_name_a_dossier(fake, kind):
    entry = _create(kind, dossier_id="dos1")
    stored = _stored(fake, entry)
    assert stored["dossier_id"] == "dos1"
    assert stored["dossier_file_number"] == "2031-001"


@pytest.mark.parametrize("kind", SORTIES + ENTREES)
def test_the_web_forms_neutralized_keys_are_absences(fake, kind):
    """The form always posts every key; a hidden field arrives empty."""
    entry = _create(kind, category="", net_amount=None, gst_amount=None,
                    qst_amount=None, invoice_id="", dossier_id=None,
                    supplier_invoice_ref="")
    assert _stored(fake, entry)["kind"] == kind


# ══════════════════════════════════════════════════════════════════════
# 3. Edit — refusals judged on the fields the edit carries
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind", SORTIES)
def test_a_description_only_edit_of_a_stored_nature_succeeds(fake, kind):
    """Régression (revue du plan) — the stored canonical net (= amount) was
    judged as a split the caller sent: every connector edit was refused."""
    entry = _create(kind)
    updated, errs = al.update_transaction(entry["id"], {"description": "précisé"})
    assert errs == [], errs
    assert updated["description"] == "précisé"


@pytest.mark.parametrize("kind", SORTIES + ENTREES)
def test_an_amount_only_edit_recomputes_the_canonical_split(fake, kind):
    """Régression — the connector cannot send a split for these kinds; the
    model used to demand one (ventilation_requise) or keep the old net."""
    entry = _create(kind)
    updated, errs = al.update_transaction(entry["id"], {"amount": 63271})
    assert errs == [], errs
    stored = _stored(fake, entry)
    sortie = kind in SORTIES
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        (63271, 0, 0) if sortie else (0, 0, 0))
    assert _ledger(fake) == (-63271 if sortie else 63271)


def test_a_split_sent_with_an_edit_of_a_nature_is_refused(fake, refusals):
    entry = _create("prélèvement")
    updated, errs = al.update_transaction(entry["id"], {"net_amount": 31013,
                                                        "gst_amount": 10344})
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["ventilation_interdite"]]
    assert refusals == ["ventilation_interdite"]


def test_a_dépense_becoming_a_drawing_drops_its_category_and_taxes(fake):
    """The spec: moving to a category-less nature clears the category and
    the split; the trail keeps what the entry was."""
    entry = _create("dépense", category="autre", net_amount=35970,
                    gst_amount=1799, qst_amount=3588)
    updated, errs = al.update_transaction(entry["id"], {"kind": "prélèvement"})
    assert errs == [], errs
    stored = _stored(fake, entry)
    assert stored["kind"] == "prélèvement"
    assert stored["direction"] == "déboursé"
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (41357, 0, 0)
    changes = stored["revisions"][-1]["changes"]
    assert changes["kind"] == ["dépense", "prélèvement"]
    assert changes["category"] == ["autre", None]
    assert changes["gst_amount"] == [1799, 0]
    assert _ledger(fake) == -41357


def test_a_drawing_becoming_a_dépense_needs_its_category(fake):
    entry = _create("prélèvement")
    updated, errs = al.update_transaction(entry["id"], {"kind": "dépense"})
    assert errs == [al._ABORT_MESSAGES["catégorie_requise"]]
    updated, errs = al.update_transaction(entry["id"], {"kind": "dépense",
                                                        "category": "loyer"})
    assert errs == [], errs
    assert _stored(fake, entry)["category"] == "loyer"


def test_a_dossier_left_on_an_entry_that_becomes_a_drawing_is_refused(fake, refusals):
    entry = _create("dépense", dossier_id="dos1")
    updated, errs = al.update_transaction(entry["id"], {"kind": "prélèvement"})
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["dossier_interdit"]]
    assert refusals == ["dossier_interdit"]
    # Sent empty (the web form always sends the key), the link goes.
    updated, errs = al.update_transaction(entry["id"], {"kind": "prélèvement",
                                                        "dossier_id": None})
    assert errs == [], errs
    stored = _stored(fake, entry)
    assert stored["dossier_id"] is None
    assert stored["dossier_file_number"] == ""


@pytest.mark.parametrize("old, new", [
    ("dépense", "prélèvement"), ("dépense", "virement_interne_sortant"),
    ("prélèvement", "virement_interne_sortant"), ("prélèvement", "dépense"),
    ("recette_autre", "apport"), ("apport", "virement_interne_entrant"),
    ("virement_interne_entrant", "recette_autre"),
    ("dépense", "recette_autre"), ("recette_autre", "dépense"),
])
def test_allowed_passages(fake, old, new):
    entry = _create(old)
    data = {"kind": new}
    if new == "dépense":
        data["category"] = "loyer"
    updated, errs = al.update_transaction(entry["id"], data)
    assert errs == [], (old, new, errs)
    assert _stored(fake, entry)["direction"] == al._KIND_DIRECTION[new]


@pytest.mark.parametrize("old, new", [
    ("dépense", "apport"), ("dépense", "virement_interne_entrant"),
    ("prélèvement", "apport"), ("prélèvement", "recette_autre"),
    ("apport", "dépense"), ("apport", "prélèvement"),
    ("virement_interne_sortant", "virement_interne_entrant"),
    ("virement_interne_entrant", "virement_interne_sortant"),
    ("recette_autre", "prélèvement"),
])
def test_a_passage_that_would_flip_the_sign_is_refused(fake, refusals, old, new):
    """Régression — any move inside the editable kinds was allowed, so a
    dépense could become an apport: the bank movement's sign flipped and the
    balance moved by twice the amount."""
    entry = _create(old)
    before = _stored(fake, entry)
    data = {"kind": new}
    if new == "dépense":
        data["category"] = "loyer"
    updated, errs = al.update_transaction(entry["id"], data)
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["changement_de_sens"]]
    assert refusals == ["changement_de_sens"]
    assert _stored(fake, entry) == before


def test_a_direction_named_alone_stays_sens_incohérent(fake, refusals):
    entry = _create("prélèvement")
    updated, errs = al.update_transaction(entry["id"], {"direction": "recette"})
    assert errs == [al._ABORT_MESSAGES["sens_incohérent"]]
    assert refusals == ["sens_incohérent"]


# ══════════════════════════════════════════════════════════════════════
# 4. Reversal — the correction carries what it reverses
# ══════════════════════════════════════════════════════════════════════


def test_reversing_a_drawing_copies_no_split_and_stamps_its_kind(fake):
    """Régression — the correction copied the prélèvement's canonical net
    (= amount): the journal printed a negative net for a non-expense."""
    entry = _create("prélèvement")
    reversal, errs = al.reverse_transaction(entry["id"], "saisie en double")
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{reversal['id']}")
    assert stored["kind"] == al.REVERSAL_KIND
    assert stored["reverses_kind"] == "prélèvement"
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (0, 0, 0)
    assert _ledger(fake) == 0


def test_reversing_a_dépense_still_copies_its_category_and_split(fake):
    entry = _create("dépense", category="loyer", net_amount=35970,
                    gst_amount=1799, qst_amount=3588)
    reversal, errs = al.reverse_transaction(entry["id"], "erreur")
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{reversal['id']}")
    assert stored["reverses_kind"] == "dépense"
    assert stored["category"] == "loyer"
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        35970, 1799, 3588)


# ══════════════════════════════════════════════════════════════════════
# 5. The pure authorities — results_statement, owner_equity
# ══════════════════════════════════════════════════════════════════════


def _row(id_, kind, amount, *, direction=None, category=None, net=0, gst=0,
         qst=0, status="compensée", **extra):
    return {"id": id_, "kind": kind, "amount": amount,
            "direction": direction or al._KIND_DIRECTION.get(kind, "recette"),
            "category": category, "net_amount": net, "gst_amount": gst,
            "qst_amount": qst, "status": status, **extra}


def test_results_statement_counts_only_revenue_and_expenses():
    rows = [
        _row("a", "encaissement_facture", 13579),
        _row("b", "recette_autre", 2468),
        _row("c", "dépense", 2299, category="loyer", net=2000, gst=100, qst=199),
        _row("d", "prélèvement", 271828, net=271828),
        _row("e", "apport", 3141),
        _row("f", "virement_interne_sortant", 16180, net=16180),
        _row("g", "virement_interne_entrant", 7071),
        _row("h", "paiement_carte", 42424, direction="déboursé"),
    ]
    statement = al.results_statement(rows)
    assert statement["revenue"] == {"encaissement_facture": 13579, "recette_autre": 2468}
    assert statement["expenses"] == {"loyer": 2299}
    assert statement["expenses_net"] == {"loyer": 2000}
    assert (statement["gst"], statement["qst"]) == (100, 199)
    assert statement["total_revenue"] == 16047
    assert statement["total_expenses"] == 2299


@pytest.mark.parametrize("stamped", [True, False])
@pytest.mark.parametrize("statuses", [("annulée", "annulée"), ("compensée", "en_circulation")])
def test_a_reversal_pair_nets_to_zero(stamped, statuses):
    """Both lifecycles of a contre-passation — reversed while in circulation
    (both annulée) or once cleared (original compensée, reversal in
    circulation) — net out, whether the correction is stamped or older."""
    orig_status, rev_status = statuses
    extra = {"reverses_kind": "dépense"} if stamped else {}
    rows = [
        _row("d1", "dépense", 2299, category="loyer", net=2000, gst=100, qst=199,
             status=orig_status, reversed_by_id="r1"),
        _row("r1", al.REVERSAL_KIND, 2299, direction="recette", category="loyer",
             net=2000, gst=100, qst=199, status=rev_status, reverses_id="d1", **extra),
        _row("e1", "encaissement_facture", 5309, status=orig_status, reversed_by_id="r2"),
        _row("r2", al.REVERSAL_KIND, 5309, direction="déboursé", status=rev_status,
             reverses_id="e1", **({"reverses_kind": "encaissement_facture"} if stamped else {})),
    ]
    statement = al.results_statement(rows)
    assert statement["expenses"] == {"loyer": 0}
    assert statement["expenses_net"] == {"loyer": 0}
    assert (statement["gst"], statement["qst"]) == (0, 0)
    assert statement["revenue"]["encaissement_facture"] == 0


def test_a_reversed_drawing_never_reaches_the_results():
    rows = [
        _row("p1", "prélèvement", 271828, net=271828, reversed_by_id="r1"),
        _row("r1", al.REVERSAL_KIND, 271828, direction="recette",
             reverses_id="p1", reverses_kind="prélèvement"),
    ]
    statement = al.results_statement(rows)
    assert statement["total_revenue"] == 0 and statement["total_expenses"] == 0


def test_an_unresolvable_correction_raises():
    with pytest.raises(al.UnresolvedCorrection):
        al.results_statement([_row("r1", al.REVERSAL_KIND, 101, reverses_id="absent")])


def test_results_statement_over_a_real_journal(fake):
    """The model's own rows, a reversal included, through the authority."""
    _create("dépense", category="loyer", amount=11613, net_amount=10101,
            gst_amount=505, qst_amount=1007)
    _create("recette_autre", amount=2729)
    _create("prélèvement", amount=73891)
    _create("virement_interne_entrant", amount=4363)
    reversed_one = _create("dépense", category="fournitures", amount=3217)
    _, errs = al.reverse_transaction(reversed_one["id"], "doublon")
    assert errs == [], errs
    rows = list(fake.peek_collection("admin_transactions").values())
    statement = al.results_statement(rows)
    assert statement["expenses"] == {"loyer": 11613, "fournitures": 0}
    assert statement["revenue"] == {"encaissement_facture": 0, "recette_autre": 2729}
    assert (statement["gst"], statement["qst"]) == (505, 1007)
    assert _ledger(fake) == sum(
        al.admin_delta(r["direction"], r["amount"]) for r in rows)


def test_owner_equity_nets_contributions_drawings_and_their_corrections():
    rows = [
        _row("a1", "apport", 12121),
        _row("a2", "apport", 3434),
        _row("p1", "prélèvement", 141421, net=141421),
        _row("p2", "prélèvement", 17320, net=17320),
        _row("r1", al.REVERSAL_KIND, 17320, direction="recette",
             reverses_id="p2", reverses_kind="prélèvement"),
        _row("v1", "virement_interne_entrant", 22360),
        _row("v2", "virement_interne_sortant", 22222, net=22222),
        _row("x1", "recette_autre", 919),
        _row("r2", al.REVERSAL_KIND, 919, direction="déboursé", reverses_id="x1"),
    ]
    assert al.owner_equity(rows) == {
        "contributions": 15555, "drawings": 141421, "net": -125866,
    }


# ══════════════════════════════════════════════════════════════════════
# 6. Integrity — checks 4 and 11, and trust check 10's candidates
# ══════════════════════════════════════════════════════════════════════


def _run_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_admin_integrity as vai

    install(monkeypatch, vai, fake=fake)
    code = vai.main()
    return code, capsys.readouterr().out


def test_a_register_using_the_natures_is_clean(fake, monkeypatch, capsys):
    _create("prélèvement")
    _create("apport", amount=2953)
    _create("virement_interne_sortant", dossier_id="dos1", amount=5743)
    drawing = _create("prélèvement", amount=1913)
    _, errs = al.reverse_transaction(drawing["id"], "erreur de saisie")
    assert errs == [], errs
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 0, out
    assert "Aucun écart" in out


@pytest.mark.parametrize("patch, expected", [
    ({"category": "autre"}, "Prélèvement de l'avocat portant une catégorie (autre)"),
    ({"net_amount": 31357, "gst_amount": 10000},
     "Prélèvement de l'avocat ventilé 31357+10000+0"),
    ({"invoice_id": "inv1"}, "Prélèvement de l'avocat liée à une facture"),
    ({"trust_transaction_id": "t1"}, "Prélèvement de l'avocat liée au fidéicommis"),
    ({"dossier_id": "dos1"}, "Prélèvement de l'avocat rattaché à un dossier"),
    ({"kind": "retrait_perso"}, "type « retrait_perso » inconnu du registre"),
])
def test_check_11_names_a_broken_nature(fake, monkeypatch, capsys, patch, expected):
    """Régression — a nature carrying what its model refuses (written outside
    it), or a kind the register does not know, passed every check."""
    entry = _create("prélèvement")
    path = f"admin_transactions/{entry['id']}"
    fake.external_write(path, {**fake.peek(path), **patch})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert expected in out


def test_check_11_names_a_reversal_of_a_nature_carrying_a_category(
    fake, monkeypatch, capsys
):
    entry = _create("apport")
    reversal, errs = al.reverse_transaction(entry["id"], "erreur")
    assert errs == [], errs
    path = f"admin_transactions/{reversal['id']}"
    fake.external_write(path, {**fake.peek(path), "category": "autre"})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert "contre-passation d'un « Apport de l'avocat » portant une catégorie" in out


def _card_payment(fake) -> tuple[str, str]:
    leg, errs = al.create_card_payment("ops1", "card1", 47221, _d(2031, 5, 12),
                                       "virement")
    assert errs == [], errs
    return leg["id"], leg["related_transaction_id"]


def test_check_4_names_card_legs_on_different_days(fake, monkeypatch, capsys):
    """Régression — check 4 compared links, amounts and directions, never
    dates: moving one leg of a pair passed unseen."""
    bank, card = _card_payment(fake)
    path = f"admin_transactions/{card}"
    fake.external_write(path, {**fake.peek(path), "date": _d(2031, 5, 13)})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert f"écriture {bank}: jambes de carte à des jours différents (2031-05-12 et 2031-05-13)" in out


def test_check_4_reads_the_utc_day_not_the_instant(fake, monkeypatch, capsys):
    """A time of day on one leg is check 10's NOTE, never a second date."""
    bank, card = _card_payment(fake)
    path = f"admin_transactions/{card}"
    fake.external_write(path, {**fake.peek(path), "date": _d(2031, 5, 12, 4)})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 2, out
    assert "jours différents" not in out


def test_check_4_names_a_partner_leg_that_is_not_a_card_payment(fake, monkeypatch, capsys):
    bank, card = _card_payment(fake)
    path = f"admin_transactions/{card}"
    fake.external_write(path, {**fake.peek(path), "kind": "recette_autre"})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert f"écriture {bank}: l'autre jambe ({card}) n'est pas un paiement de carte" in out


def test_trust_check_10_candidates_are_the_models_revenue_kinds(fake, monkeypatch):
    """An apport or a transfer from trust never stands for a fee payment's
    manual recette; the candidate kinds are the model's REVENUE_KINDS."""
    from scripts import verify_trust_integrity as vti

    install(monkeypatch, vti, fake=fake)
    other = _create("recette_autre", amount=2953)
    _create("apport", amount=2953)
    _create("virement_interne_entrant", amount=2953)
    problems: list = []
    linked, unlinked = vti._admin_rows_by_trust_link(problems)
    assert problems == []
    assert [r["id"] for r in unlinked] == [other["id"]]
    assert al.REVENUE_KINDS == ("encaissement_facture", "recette_autre")


# ══════════════════════════════════════════════════════════════════════
# 7. Review fixes — derived texts, unstamped corrections (a rollback)
# ══════════════════════════════════════════════════════════════════════


def test_the_refusal_texts_name_every_kind_of_their_source():
    """Régression (revue) — the texts restated the kinds in prose: a kind
    joining a source would have been missing from every refusal."""
    messages = al._ABORT_MESSAGES
    for kind, direction in al._KIND_DIRECTION.items():
        label = f"« {al.KIND_LABELS[kind]} »"
        assert label in messages["sens_incohérent"], kind
        if kind in al._EDITABLE_KINDS:
            assert label in messages["changement_de_sens"], kind
    for reason in ("catégorie_interdite", "ventilation_interdite", "facture_interdite"):
        for kind in al.NON_RESULT_KINDS:
            assert f"« {al.KIND_LABELS[kind]} »" in messages[reason], (reason, kind)
    for kind in al.OWNER_KINDS:
        assert f"« {al.KIND_LABELS[kind]} »" in messages["dossier_interdit"], kind


def test_owner_equity_resolves_an_unstamped_correction_through_its_original():
    """Régression (revue) — a reversal made by an older release (a rollback)
    carries no reverses_kind: it was read as « nothing » and the reversed
    drawing kept counting. Resolved through its original when present; left
    out, never guessed, when the original lies outside the rows."""
    drawing = _row("p1", "prélèvement", 17320, net=17320, reversed_by_id="r1")
    correction = _row("r1", al.REVERSAL_KIND, 17320, direction="recette",
                      net=17320, reverses_id="p1")
    assert al.owner_equity([drawing, correction]) == {
        "contributions": 0, "drawings": 0, "net": 0}
    assert al.owner_equity([correction]) == {
        "contributions": 0, "drawings": 0, "net": 0}


def test_check_11_resolves_an_unstamped_correction_of_a_nature(fake, monkeypatch, capsys):
    """Régression (revue) — an older release's reversal copies the drawing's
    canonical net and stamps nothing: check 11 skipped it while the journal
    printed a negative net."""
    entry = _create("prélèvement")
    reversal, errs = al.reverse_transaction(entry["id"], "erreur")
    assert errs == [], errs
    path = f"admin_transactions/{reversal['id']}"
    stored = {**fake.peek(path), "net_amount": 41357}
    stored.pop("reverses_kind")
    fake.external_write(path, stored)
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert ("contre-passation d'un « Prélèvement de l'avocat » portant une "
            "catégorie ou une ventilation") in out


def test_check_11_names_a_stamp_that_contradicts_the_original(fake, monkeypatch, capsys):
    entry = _create("apport")
    reversal, errs = al.reverse_transaction(entry["id"], "erreur")
    assert errs == [], errs
    path = f"admin_transactions/{reversal['id']}"
    fake.external_write(path, {**fake.peek(path), "reverses_kind": "recette_autre"})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert ("contre-passation notée « recette_autre », l'écriture qu'elle "
            "contre-passe est « apport »") in out
