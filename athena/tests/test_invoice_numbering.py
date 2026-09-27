"""Year-sequential invoice numbering (models/invoice.py).

An invoice number is « YYYY-FNNN » again — the MONTRÉAL calendar year, the
« F » marker, and a 3-digit zero-padded year-wide sequence (user decision
2026-08-12, reverting the per-file « {file_number}-NN » scheme of
2026-07-17 after four weeks; the six invoices that scheme minted keep
their numbers for ever — an accounting artifact sent to a client is never
renumbered).

Rewritten on 2026-09-26 (lot 0b). The number used to be allocated by a
standalone ``_generate_invoice_number()`` that committed the counter in a
transaction of its OWN, before the invoice's: every abort of the invoice
transaction — a source edited in between, a concurrent invoicing — burned a
number and left a hole in the fee journal. The allocation now lives INSIDE
``create_invoice``'s transaction, and there is no standalone allocator left
to test. So every pin below goes through ``create_invoice``, on the shared
fake Firestore (``tests/_fake_firestore.py`` — the client, its transactions
and the ``transactional`` retry loop are the real ones), and asserts on what
is STORED.
"""

import os
import sys
import uuid
from datetime import date, datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import expense as expense_model
    from models import invoice
    from models import time_entry as time_entry_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DOSSIER = "d1"
WHEN = datetime(2026, 6, 15, tzinfo=UTC)
COUNTER = "counters/invoices-2026"


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, invoice, time_entry_model, expense_model)
    # Millésime FIGÉ (jamais un offset dérivé de l'horloge — la leçon du
    # test de retard du 2026-08-11) : le numéro lit today_mtl, pas
    # datetime.now, et ce gel est ce que pinne test_millesime_de_montreal.
    monkeypatch.setattr(invoice, "today_mtl", lambda: date(2026, 6, 15))
    return f


def _entry(fake, eid: str, *, etag: str = "e0") -> str:
    fake.seed(f"timeentries/{eid}", {
        "id": eid, "dossier_id": DOSSIER, "date": WHEN,
        "description": "Rédaction", "hours": 1.0, "rate": 30000,
        "amount": 30000, "billable": True, "invoiced": False,
        "invoice_id": None, "etag": etag,
    })
    return eid


def _create(fake, eid: str = ""):
    eid = eid or _entry(fake, f"te-{uuid.uuid4().hex[:8]}")
    return invoice.create_invoice(
        DOSSIER, [eid], [], {"dossier_id": DOSSIER, "date": WHEN},
    )


def _number(fake) -> str:
    doc, errors = _create(fake)
    assert errors == [], errors
    return doc["invoice_number"]


# ── La séquence annuelle ──────────────────────────────────────────────────


def test_sequence_annuelle_monotone(fake):
    assert [_number(fake) for _ in range(3)] == [
        "2026-F001", "2026-F002", "2026-F003"]
    assert fake.peek(COUNTER)["seq"] == 3


def test_compteur_existant_continue_sans_reamorcage(fake):
    # Le portrait de production du retour (2026-08-12) : compteur à 30 —
    # le prochain numéro est F031, jamais une réutilisation (un numéro
    # alloué puis supprimé reste un trou pour toujours).
    fake.seed(COUNTER, {"seq": 30})
    assert _number(fake) == "2026-F031"


def test_amorcage_ignore_les_numeros_par_dossier(fake):
    # Premier usage d'une année SANS compteur : l'amorçage balaie les
    # factures existantes — les « YYYY-FNNN » comptent, les numéros par
    # dossier de la parenthèse 2026-07-17→2026-08-12 sont invisibles au
    # préfixe « 2026-F » et ne peuvent ni collisionner ni décaler la suite.
    fake.seed_collection("invoices", {
        "i1": {"id": "i1", "invoice_number": "2026-F007"},
        "i2": {"id": "i2", "invoice_number": "2026-F012"},
        "i3": {"id": "i3", "invoice_number": "2026-001-05"},
        "i4": {"id": "i4", "invoice_number": "2026-028-02"},
    })
    assert _number(fake) == "2026-F013"


def test_amorcage_sans_facture_annuelle_demarre_a_f001(fake):
    fake.seed("invoices/i1", {"id": "i1", "invoice_number": "2026-001-05"})
    assert _number(fake) == "2026-F001"


def test_millesime_de_montreal(fake, monkeypatch):
    # Le préfixe suit le jour civil de MONTRÉAL (today_mtl — l'unique
    # horloge maison), plus l'année UTC : une facture du 31 décembre au
    # soir porte le millésime en cours. Pinné en pointant today_mtl sur
    # une autre année que celle de l'horloge murale.
    monkeypatch.setattr(invoice, "today_mtl", lambda: date(2030, 12, 31))
    assert _number(fake) == "2030-F001"
    assert fake.peek("counters/invoices-2030")["seq"] == 1


def test_debordement_a_quatre_chiffres_apres_f999(fake):
    fake.seed(COUNTER, {"seq": 999})
    assert _number(fake) == "2026-F1000"


def test_scan_max_invoice_seq_tolere_les_suffixes_non_numeriques(fake):
    fake.seed_collection("invoices", {
        "i1": {"id": "i1", "invoice_number": "2026-F009"},
        "i2": {"id": "i2", "invoice_number": "2026-Fxx"},   # jamais émis par nous
        "i3": {"id": "i3", "invoice_number": ""},
    })
    assert invoice._scan_max_invoice_seq("2026-F") == 9


# ── L'allocation vit DANS la transaction de la facture (lot 0b) ───────────


def test_un_conflit_de_source_ne_brule_aucun_numero(fake, monkeypatch):
    """LA régression. Une source modifiée entre la pré-lecture et la relecture
    transactionnelle fait avorter la création — c'était vrai avant. Mais le
    numéro, lui, avait déjà été alloué et COMMIS par une transaction à part :
    un trou dans la séquence, qu'aucun avocat ne pouvait expliquer au Barreau.
    Le compteur doit rester où il était, et la facture suivante prendre le
    numéro que celle-ci n'a pas eu. (Vérifié en rétablissant l'ancien
    allocateur : le compteur passait à 8 et le test tombait.)"""
    fake.seed(COUNTER, {"seq": 7})
    eid = _entry(fake, "te1")
    real_get = time_entry_model.get_time_entry
    rivaled = []

    def _pre_read_then_rival(entry_id):
        doc = real_get(entry_id)
        if not rivaled:
            # Another tab edits the entry between the pre-read and the
            # transaction: a new etag, as every model write mints.
            stored = fake.peek(f"timeentries/{entry_id}")
            stored.update(hours=2.0, amount=60000, etag="rival")
            fake.external_write(f"timeentries/{entry_id}", stored)
            rivaled.append(entry_id)
        return doc

    monkeypatch.setattr(time_entry_model, "get_time_entry", _pre_read_then_rival)
    doc, errors = _create(fake, eid)
    assert doc is None
    assert "modifiées ou facturées entre-temps" in errors[0]
    assert fake.peek(COUNTER)["seq"] == 7, "un avortement a brûlé un numéro"
    assert fake.peek_collection("invoices") == {}
    assert fake.peek(f"timeentries/{eid}")["invoiced"] is False

    # La facture suivante reprend exactement là : aucun trou.
    monkeypatch.setattr(time_entry_model, "get_time_entry", real_get)
    assert _number(fake) == "2026-F008"


def test_le_compteur_est_lu_dans_la_transaction_et_commis_avec_la_facture(fake):
    """La forme qui rend le trou impossible : l'écriture du compteur voyage
    dans LE MÊME commit que la facture, ses lignes et la bascule de ses
    sources — jamais dans un commit à elle."""
    fake.seed(COUNTER, {"seq": 4})
    fake.reset_logs()
    doc, errors = _create(fake)
    assert errors == [], errors
    counter_commits = [c for c in fake.commits
                       if any(path == COUNTER for _kind, path in c.ops)]
    assert len(counter_commits) == 1
    paths = {path for _kind, path in counter_commits[0].ops}
    assert f"invoices/{doc['id']}" in paths
    assert counter_commits[0].transaction is not None
    assert any(r.transactional and COUNTER in r.paths for r in fake.reads), (
        "le compteur doit être relu DANS la transaction de la facture"
    )


def test_deux_premieres_factures_de_l_annee_concurrentes_ne_partagent_pas_un_numero(
    fake,
):
    """Premier usage de l'année : les deux créations calculent le même
    amorçage hors transaction, puis lisent un compteur ABSENT dans la leur.
    Le premier commit gagne ; le second avorte (il a lu un document qui a
    changé) et la boucle de reprise du vrai `transactional` relit le compteur
    écrit par le gagnant."""
    eid = _entry(fake, "te-late")
    fired = []

    def _winner_commits_first(info):
        if not fired and any(p == COUNTER for _k, p in info.ops):
            fired.append(info.index)
            fake.external_write(COUNTER, {"seq": 1})

    remove = fake.add_commit_hook(_winner_commits_first)
    try:
        doc, errors = _create(fake, eid)
    finally:
        remove()
    assert errors == [], errors
    assert fired, "the rival commit never ran — the test proves nothing"
    assert doc["invoice_number"] == "2026-F002"
    assert fake.peek(COUNTER)["seq"] == 2


def test_un_echec_d_allocation_fait_avorter_la_creation(fake, monkeypatch):
    """« Allocation failure aborts invoice creation » : jamais un numéro
    deviné, jamais une facture sans numéro, jamais une source basculée."""
    def _boom(_prefix):
        raise RuntimeError("scan indisponible")

    monkeypatch.setattr(invoice, "_scan_max_invoice_seq", _boom)
    eid = _entry(fake, "te1")
    doc, errors = _create(fake, eid)
    assert doc is None
    assert errors == [
        "Impossible de générer le numéro de facture. Veuillez réessayer."]
    assert fake.peek_collection("invoices") == {}
    assert fake.peek(COUNTER) is None
    assert fake.peek(f"timeentries/{eid}")["invoiced"] is False


def test_l_allocateur_autonome_n_existe_plus():
    """Un allocateur qui commet de son côté EST le défaut : s'il revenait,
    un appelant pourrait de nouveau brûler un numéro hors de la transaction
    de la facture."""
    assert not hasattr(invoice, "_generate_invoice_number")
