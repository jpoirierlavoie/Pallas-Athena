"""Les contrôles 5 à 9 de ``scripts/verify_trust_integrity`` (lot 5a).

Le script ne vérifiait que les soldes (contrôles 1 à 4) : une conciliation
complétée qui ne se prouvait plus, une compensation datée dans une période
déjà close, un registre dont les dates reculent, un client à découvert ou
un solde stocké qu'aucune écriture n'appuie passaient tous « ✅ Aucun
écart ». Chaque test ci-dessous échoue sur le script antérieur (il rendait 0
et n'imprimait aucune de ces lignes).

Deux sortes de constats, et le code de sortie dit lesquelles :

* un ÉCART (code 1) — un chiffre ou un invariant sur lequel le registre ne
  tient plus aujourd'hui ;
* une NOTE (code 2 s'il n'y a pas d'écart) — l'historique : une écriture
  que le modèle refuserait aujourd'hui mais qu'il a acceptée à l'époque, ou
  une conciliation complétée sous l'ancien code (avant la refonte « as-of »
  du 2026-07-29). Le registre ne se réécrit pas : une note est une décision
  de l'avocat, jamais une réparation.

Le banc est le faux Firestore partagé (``tests/_fake_firestore.py``) : le
registre est construit par le VRAI modèle (création, compensation,
conciliation, contre-passation, virement), puis — pour reproduire un
historique que le modèle actuel refuse — par l'ancien comportement
(plancher de conciliation neutralisé, comme avant le lot 0b) ou par une
écriture externe. Les horodatages sont fixés explicitement : aucun test ne
dépend de l'horloge murale.
"""

import contextlib
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
    from models import admin_ledger, trust
    from scripts import verify_trust_integrity as vti

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    """A date-only value (midnight UTC)."""
    return datetime(y, m, d, tzinfo=UTC)


def _at(m: int, d: int, h: int = 12, y: int = 2026) -> datetime:
    """A true timestamp (UTC)."""
    return datetime(y, m, d, h, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — the script included."""
    mods = [m for n, m in sorted(sys.modules.items())
            if n.startswith("models.") and getattr(m, "db", None) is not None]
    return mods + [vti]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "e0",
    })
    for did, cid, name in (("dos1", "c1", "Jean Tremblay"),
                           ("dos2", "c2", "Marie Roy")):
        f.seed(f"dossiers/{did}", {
            "id": did, "file_number": f"2026-00{did[-1]}", "title": "T c. X",
            "client_ids": [cid], "clients": [{"id": cid, "name": name}],
            "trust_balance": 0, "trust_balance_by_client": {},
            "trust_cleared_by_client": {},
        })
    # The operations account a fee payment's recette lands in (D-4).
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0",
    })
    return f


def _today(monkeypatch, iso: str) -> None:
    """Freeze the ONE Montréal clock read (``utils.deadlines.today_mtl``)."""
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


def _set(fake, path: str, **fields) -> None:
    """Another process rewrites some fields of *path* (history, or a clock
    the test pins instead of reading the wall)."""
    fake.external_write(path, {**fake.peek(path), **fields})


def _tx(tx_id: str) -> str:
    return f"trust_transactions/{tx_id}"


def _entry(**over) -> dict:
    d = {
        "account_id": "acc1", "direction": "recette", "amount": 100000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 1),
        "description": "", "reference": "",
    }
    d.update(over)
    return d


def _create(**over) -> dict:
    entry, errs = trust.create_transaction(_entry(**over))
    assert errs == [], errs
    return entry


def _clear(tx_id: str, day: datetime) -> None:
    _, errs = trust.clear_transaction(tx_id, day)
    assert errs == [], errs


def _run(capsys) -> tuple[int, str]:
    code = vti.main()
    return code, capsys.readouterr().out


@contextlib.contextmanager
def _without_lock_floor(monkeypatch):
    """The trust model before lot 0b: no reconciliation lock floor. What it
    wrote then is what the register holds now."""
    with monkeypatch.context() as m:
        m.setattr(trust, "_read_lock_floor", lambda account_id, txn=None: None)
        yield


def _september(fake, monkeypatch, *, clear_r2_before_close: bool = False):
    """A coherent September register, built by the real model.

    r1: 1 000 $ received 1 Sept., cleared 2 Sept. (before the close);
    r2: 500 $ received 3 Sept., still in transit at the period end;
    a reconciliation to 5 Sept. that balances (statement 1 000 $ + 500 $ in
    transit − 1 500 $ book), completed 6 Sept.
    """
    _today(monkeypatch, "2026-09-20T16:00:00+00:00")
    r1 = _create(amount=100000, date=_d(2026, 9, 1))
    _clear(r1["id"], _d(2026, 9, 2))
    r2 = _create(amount=50000, date=_d(2026, 9, 3))
    statement = 100000
    if clear_r2_before_close:
        _clear(r2["id"], _d(2026, 9, 4))
        statement = 150000
    rec, errs = trust.create_reconciliation("acc1", _d(2026, 9, 5), statement)
    assert errs == [], errs
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == [], errs
    _set(fake, _tx(r1["id"]), created_at=_at(9, 1), updated_at=_at(9, 2))
    _set(fake, _tx(r2["id"]), created_at=_at(9, 3),
         updated_at=_at(9, 4) if clear_r2_before_close else _at(9, 3))
    _set(fake, f"trust_reconciliations/{rec['id']}", completed_date=_at(9, 6))
    return r1, r2, rec


def _july(fake, monkeypatch):
    """The same shape in July — a reconciliation to 15 July — so it can be
    completed BEFORE the as-of rework without dates contradicting stamps."""
    _today(monkeypatch, "2026-07-25T16:00:00+00:00")
    r1 = _create(amount=100000, date=_d(2026, 7, 10))
    _clear(r1["id"], _d(2026, 7, 11))
    r2 = _create(amount=50000, date=_d(2026, 7, 12))
    rec, errs = trust.create_reconciliation("acc1", _d(2026, 7, 15), 100000)
    assert errs == [], errs
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == [], errs
    _set(fake, _tx(r1["id"]), created_at=_at(7, 10), updated_at=_at(7, 11))
    _set(fake, _tx(r2["id"]), created_at=_at(7, 12), updated_at=_at(7, 12))
    _set(fake, f"trust_reconciliations/{rec['id']}", completed_date=_at(7, 16))
    return r1, r2, rec


def _late_clear(fake, monkeypatch, tx_id: str, day: datetime, at: datetime) -> None:
    """A clear dated INSIDE the closed period, made AFTER the close — what
    the pre-lot-0b model allowed."""
    with _without_lock_floor(monkeypatch):
        _clear(tx_id, day)
    _set(fake, _tx(tx_id), updated_at=at)


def _transfer(fake) -> dict:
    """A two-leg transfer dos1/c1 → dos2/c2 by the real model — with ONE
    correction to what it stores.

    ``create_inter_dossier_transfer`` writes the account's NET balance
    (unchanged by the pair) as ``balance_after_account`` on BOTH legs, where
    the running balance after the déboursé leg is ``book − amount``; the
    pair's own reversal (``_stage_pair_reversal``) writes the exact running
    balance. Check 1 — unchanged since Phase K — therefore flags every
    transfer's first leg. That model inconsistency predates this lot and is
    reported, not fixed, here (no behaviour change in this step): the tests
    of checks 5-9 store the exact figure so they speak about their own check.
    """
    leg, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos2", "c2", 20000, "instruction", "virement", ""
    )
    assert errs == [], errs
    stored = fake.peek(_tx(leg["id"]))
    _set(fake, _tx(leg["id"]),
         balance_after_account=stored["balance_after_account"] - leg["amount"])
    return leg


# ══════════════════════════════════════════════════════════════════════
# Le témoin : un registre cohérent passe, code 0
# ══════════════════════════════════════════════════════════════════════


def test_un_registre_coherent_passe_sans_ecart_ni_note(fake, monkeypatch, capsys):
    """Le témoin : création, compensation avant la clôture, conciliation,
    déboursé, paiement d'honoraires adossé à sa recette d'administration,
    virement à deux volets et contre-passation — rien à dire."""
    _september(fake, monkeypatch)
    deb = _create(direction="déboursé", amount=30000, purpose="déboursé_tiers",
                  counterparty="Huissier", date=_d(2026, 9, 10))
    _fee_payment(fake, amount=20000, date=_d(2026, 9, 10))
    _transfer(fake)
    _, errs = trust.reverse_transaction(deb["id"], "chèque perdu")
    assert errs == []
    code, out = _run(capsys)
    assert code == 0, out
    assert "✅ Aucun écart" in out
    assert "Notes à revoir" not in out


def test_une_compensation_datee_dans_la_periode_mais_faite_avant_la_cloture_passe(
    fake, monkeypatch, capsys
):
    """La moitié légitime du contrôle 6 : « Compenser » un dépôt au 4 sept.
    AVANT de concilier au 5 sept. est l'usage ordinaire — jamais signalé."""
    _september(fake, monkeypatch, clear_r2_before_close=True)
    code, out = _run(capsys)
    assert code == 0, out


# ══════════════════════════════════════════════════════════════════════
# 5 + 6 — la conciliation close ne se prouve plus
# ══════════════════════════════════════════════════════════════════════


def test_une_compensation_datee_dans_une_periode_deja_close_est_un_ecart(
    fake, monkeypatch, capsys
):
    """Le défaut que le plancher de compensation ferme : r2, encore en
    circulation à la fin de période, est compensé au 4 sept. APRÈS la
    clôture du 6. Il sort de l'ensemble de résurrection, la conciliation du
    5 ne se re-prouve plus — et le script le disait « ✅ »."""
    _, r2, rec = _september(fake, monkeypatch)
    _late_clear(fake, monkeypatch, r2["id"], _d(2026, 9, 4), _at(9, 10))
    code, out = _run(capsys)
    assert code == 1, out
    assert f"conciliation {rec['id']} (fin de période 2026-09-05" in out
    assert "ne se re-prouve plus à sa fin de période — écart -50000 cents" in out
    assert (f"(écriture {r2['id']}): compensée au 2026-09-04 APRÈS la clôture "
            f"de la période qui couvre cette date") in out


def test_une_compensation_devenue_indatable_par_une_contre_passation_est_une_note(
    fake, monkeypatch, capsys
):
    """Contre-passée après coup, l'écriture a été réécrite : l'instant de sa
    compensation n'est plus que borné (création ≤ … ≤ contre-passation).
    Une borne indécidable est une NOTE à vérifier au relevé — jamais un
    écart deviné."""
    _, r2, _ = _september(fake, monkeypatch)
    _late_clear(fake, monkeypatch, r2["id"], _d(2026, 9, 4), _at(9, 10))
    _, errs = trust.reverse_transaction(r2["id"], "dépôt refusé")
    assert errs == []
    code, out = _run(capsys)
    assert code == 1, out  # the re-proof itself still fails (check 5)
    assert (f"(écriture {r2['id']}): compensée au 2026-09-04, dans la période "
            f"de la conciliation") in out
    assert "à vérifier au relevé" in out
    assert "APRÈS la clôture" not in out


@pytest.mark.parametrize(
    "completed, pre_as_of",
    [
        # 21 h HAE le 29 juillet — AVANT le commit 945572a (22 h 37) : l'ancien
        # code. Le plan dit « avant le 2026-07-29 » ; l'instant du commit est
        # la frontière qui ne classe aucune conciliation de l'ancien code
        # parmi les écarts.
        (datetime(2026, 7, 30, 1, 0, tzinfo=UTC), True),
        # 23 h HAE le 29 juillet — après le commit : le code « as-of ».
        (datetime(2026, 7, 30, 3, 0, tzinfo=UTC), False),
        # Une date de complétion absente ne se place pas après la refonte.
        (None, True),
    ],
)
def test_une_conciliation_de_l_ancien_code_est_une_note_pas_un_ecart(
    fake, monkeypatch, capsys, completed, pre_as_of
):
    _, r2, rec = _july(fake, monkeypatch)
    _set(fake, f"trust_reconciliations/{rec['id']}", completed_date=completed)
    _late_clear(fake, monkeypatch, r2["id"], _d(2026, 7, 13), _at(8, 10))
    code, out = _run(capsys)
    reproof = "ne se re-prouve plus à sa fin de période — écart -50000 cents"
    assert reproof in out
    notes = out.split("Notes à revoir", 1)[1] if "Notes à revoir" in out else ""
    if pre_as_of:
        assert code == 2, out
        assert "✅ Aucun écart" in out
        assert reproof in notes
        assert "avant la conciliation « as-of » du 2026-07-29" in notes
        if completed is not None:
            assert "APRÈS la clôture" in notes
            assert "(ancienne conciliation : à revoir avec l'avocat)" in notes
    else:
        assert code == 1, out
        assert reproof not in notes
        assert "APRÈS la clôture" in out.split("Notes à revoir")[0]


def test_un_contexte_as_of_illisible_est_un_ecart_jamais_un_passage(
    fake, monkeypatch, capsys
):
    _, _, rec = _september(fake, monkeypatch)

    def _boom(*_a, **_kw):
        raise RuntimeError("lecture impossible")

    monkeypatch.setattr(trust, "reconciliation_as_of_context", _boom)
    code, out = _run(capsys)
    assert code == 1, out
    assert f"conciliation {rec['id']}" in out
    assert "contexte as-of illisible (RuntimeError)" in out


# ══════════════════════════════════════════════════════════════════════
# 6 — la date de compensation
# ══════════════════════════════════════════════════════════════════════


def test_une_ecriture_compensee_sans_date_de_compensation_est_un_ecart(
    fake, monkeypatch, capsys
):
    r1, _, _ = _september(fake, monkeypatch)
    _set(fake, _tx(r1["id"]), cleared_date=None)
    code, out = _run(capsys)
    assert code == 1, out
    assert f"(écriture {r1['id']}): compensée sans date de compensation" in out


def test_une_compensation_anterieure_a_l_ecriture_ou_future_est_un_ecart(
    fake, monkeypatch, capsys
):
    r1, _, _ = _september(fake, monkeypatch)
    deb = _create(direction="déboursé", amount=10000, purpose="déboursé_tiers",
                  counterparty="Huissier", date=_d(2026, 9, 12))
    _clear(deb["id"], _d(2026, 9, 14))
    _set(fake, _tx(r1["id"]), cleared_date=_d(2026, 8, 31))   # before 1 Sept.
    _set(fake, _tx(deb["id"]), cleared_date=_d(2026, 9, 25))  # today is 20 Sept.
    code, out = _run(capsys)
    assert code == 1, out
    assert (f"(écriture {r1['id']}): compensée le 2026-08-31, avant sa propre "
            f"date (2026-09-01)") in out
    assert f"(écriture {deb['id']}): date de compensation 2026-09-25 dans le futur" in out


def test_l_ancien_code_de_conciliation_datant_une_ecriture_posterieure_est_une_note(
    fake, monkeypatch, capsys
):
    """Avant la refonte « as-of », cocher une écriture datée APRÈS la fin de
    période lui donnait quand même ``cleared_date = period_end``. Ce n'est
    pas un écart établi : une note."""
    _, _, rec = _july(fake, monkeypatch)
    r3 = _create(amount=30000, date=_d(2026, 7, 20))
    # The old completion (21 July) ticked r3 — en circulation « now » — and
    # stamped it with the period end.
    _set(fake, f"trust_reconciliations/{rec['id']}", completed_date=_at(7, 21))
    _set(fake, _tx(r3["id"]), status="compensée", cleared_date=_d(2026, 7, 15),
         reconciliation_id=rec["id"], created_at=_at(7, 20), updated_at=_at(7, 21))
    account = fake.peek("trust_accounts/acc1")
    _set(fake, "trust_accounts/acc1", bank_balance=account["bank_balance"] + 30000)
    dossier = fake.peek("dossiers/dos1")
    _set(fake, "dossiers/dos1", trust_cleared_by_client={
        "c1": dossier["trust_cleared_by_client"]["c1"] + 30000})
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {r3['id']}): compensée le 2026-07-15, avant sa propre "
            f"date (2026-07-20) — par la conciliation {rec['id']}") in out


# ══════════════════════════════════════════════════════════════════════
# 7 — l'ordre des dates
# ══════════════════════════════════════════════════════════════════════


def test_une_date_qui_recule_dans_la_sequence_est_un_ecart(fake, monkeypatch, capsys):
    """Le solde reporté et le solde aux livres à une date relisent UN solde
    figé, exact seulement si les dates croissent avec la séquence."""
    _, r2, _ = _september(fake, monkeypatch)
    _set(fake, _tx(r2["id"]), date=_d(2026, 8, 31))
    code, out = _run(capsys)
    assert code == 1, out
    assert (f"seq 2 (écriture {r2['id']}): datée du 2026-08-31, alors qu'une "
            f"écriture antérieure dans la séquence est datée du 2026-09-01") in out


def test_une_ecriture_datee_dans_le_futur_est_un_ecart(fake, monkeypatch, capsys):
    _, r2, _ = _september(fake, monkeypatch)
    _set(fake, _tx(r2["id"]), date=_d(2026, 9, 25))
    code, out = _run(capsys)
    assert code == 1, out
    assert f"(écriture {r2['id']}): datée du 2026-09-25, dans le futur" in out


def test_un_numero_de_sequence_duplique_est_un_ecart(fake, monkeypatch, capsys):
    _, r2, _ = _september(fake, monkeypatch)
    _set(fake, _tx(r2["id"]), sequence=1)
    code, out = _run(capsys)
    assert code == 1, out
    assert "compte acc1: numéro de séquence 1 dupliqué" in out


# ══════════════════════════════════════════════════════════════════════
# 8 — les règles du lot 0b sur l'historique : des NOTES
# ══════════════════════════════════════════════════════════════════════


def _fee_payment(fake, *, recette: bool = True, **over) -> dict:
    """A fee payment the way the web writes it since D-4: the trust entry,
    then its administration recette linked by ``trust_transaction_id``
    (check 10). ``recette=False`` is the fee payment whose recette never
    came (the fail-open after-commit write)."""
    fake.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2026-F001", "dossier_id": "dos1",
        "status": "envoyée", "total": 50000, "amount_due": 50000,
        "amount_paid": 0, "retainer_applied": 0,
    })
    fee = _create(**{
        "direction": "déboursé", "amount": 30000, "purpose": "virement_honoraires",
        "counterparty": "Me Avocat", "invoice_id": "inv1", "date": _d(2026, 9, 10),
        **over,
    })
    if recette:
        _admin_recette(fee)
    return fee


def _admin_recette(fee: dict, **over) -> dict:
    """The operations-account recette mirroring *fee*, by the real admin
    model (the link travels as the keyword, as routes/trust sends it)."""
    data = {
        "account_id": "ops1",
        "kind": "encaissement_facture" if fee.get("invoice_id") else "recette_autre",
        "amount": fee["amount"], "method": "virement", "counterparty": "Fidéicommis",
        "date": fee["date"], "description": "Paiement d'honoraires du fidéicommis",
        "invoice_id": fee.get("invoice_id"),
    }
    data.update(over)
    recette, errs = admin_ledger.create_transaction(
        data, trust_transaction_id=fee["id"])
    assert errs == [], errs
    return recette


def test_un_paiement_d_honoraires_retire_par_traite_est_une_note_art_58(
    fake, monkeypatch, capsys
):
    _september(fake, monkeypatch)
    fee = _fee_payment(fake)
    _set(fake, _tx(fee["id"]), method="traite")
    code, out = _run(capsys)
    assert code == 2, out
    assert "✅ Aucun écart" in out
    assert (f"(écriture {fee['id']}): paiement d'honoraires retiré par « Traite » "
            f"— l'art. 58") in out


def test_un_paiement_d_honoraires_sur_une_facture_a_provision_est_une_note(
    fake, monkeypatch, capsys
):
    _september(fake, monkeypatch)
    fee = _fee_payment(fake)
    _set(fake, "invoices/inv1", retainer_applied=10000)
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {fee['id']}): paiement d'honoraires tiré sur la facture "
            f"2026-F001, qui impute déjà une provision de 10000 cents") in out


def test_une_facture_liee_illisible_est_un_ecart(fake, monkeypatch, capsys):
    _september(fake, monkeypatch)
    fee = _fee_payment(fake)
    server = fake._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kw):
        if any(d.endswith("/invoices/inv1") for d in request["documents"]):
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    code, out = _run(capsys)
    assert code == 1, out
    assert f"(écriture {fee['id']}): facture liée inv1 illisible" in out


def test_un_retrait_en_especes_est_une_note_art_57(fake, monkeypatch, capsys):
    _september(fake, monkeypatch)
    cash = _create(direction="déboursé", amount=20000, purpose="déboursé_tiers",
                   counterparty="Huissier", date=_d(2026, 9, 10))
    _set(fake, _tx(cash["id"]), method="comptant")
    code, out = _run(capsys)
    assert code == 2, out
    assert f"(écriture {cash['id']}): retrait en espèces — interdit par l'art. 57" in out


def test_le_remboursement_en_especes_de_l_art_72_n_est_pas_signale(
    fake, monkeypatch, capsys
):
    """L'exception de l'art. 72, telle que le modèle l'inscrit : rembourser
    en espèces une somme de 7 500 $ ou plus reçue en espèces."""
    _september(fake, monkeypatch)
    receipt = _create(amount=800000, method="comptant", date=_d(2026, 9, 10))
    _clear(receipt["id"], _d(2026, 9, 11))
    refund = _create(direction="déboursé", amount=300000, purpose="remise_client",
                     method="comptant", counterparty="Client",
                     cash_receipt_id=receipt["id"], date=_d(2026, 9, 12))
    code, out = _run(capsys)
    assert code == 0, out
    assert refund["id"] not in out


def test_une_ecriture_inscrite_apres_la_cloture_mais_datee_dans_la_periode_est_une_note(
    fake, monkeypatch, capsys
):
    """Le plancher de conciliation sur la création (D14). Une recette en
    circulation datée dans la période close ne dérange pas la re-preuve
    (elle compte « en transit ») : seule cette note la montre."""
    _september(fake, monkeypatch)
    with _without_lock_floor(monkeypatch):
        late = _create(amount=10000, date=_d(2026, 9, 4))
    _set(fake, _tx(late["id"]), created_at=_at(9, 10), updated_at=_at(9, 10))
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {late['id']}): inscrite le 2026-09-10 08:00 mais datée "
            f"du 2026-09-04, dans la période déjà close de la conciliation") in out


def test_une_correction_contre_passee_a_son_tour_est_une_note(fake, monkeypatch, capsys):
    _september(fake, monkeypatch)
    deb = _create(direction="déboursé", amount=10000, purpose="déboursé_tiers",
                  counterparty="Huissier", date=_d(2026, 9, 10))
    rev, errs = trust.reverse_transaction(deb["id"], "erreur")
    assert errs == []
    _set(fake, _tx(rev["id"]), reversed_by_id="tx-historique")
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {rev['id']}): correction contre-passée à son tour "
            f"(par tx-historique)") in out


def test_un_seul_volet_d_un_virement_contre_passe_est_une_note(fake, monkeypatch, capsys):
    _september(fake, monkeypatch)
    leg = _transfer(fake)
    _set(fake, _tx(leg["id"]), reversed_by_id="tx-historique")
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {leg['id']}): un seul volet du virement inter-dossiers a "
            f"été contre-passé (autre volet {leg['related_transaction_id']})") in out


def test_un_virement_inter_dossiers_a_un_seul_volet_est_une_note(
    fake, monkeypatch, capsys
):
    """Le formulaire de création offrait l'objet « virement inter-dossiers » :
    une écriture sans volet contrepartie lié a déplacé le solde d'un seul
    dossier. Mesuré avant que le lot 5 réserve l'objet au virement à deux
    volets ; le virement à deux volets du modèle, lui, n'est pas signalé."""
    _september(fake, monkeypatch)
    single = _create(direction="déboursé", amount=10000,
                     purpose="virement_inter_dossiers", counterparty="Marie Roy",
                     date=_d(2026, 9, 10))
    pair_leg = _transfer(fake)
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {single['id']}): virement inter-dossiers à un seul volet "
            f"(Déboursé de 10000 cents)") in out
    assert f"(écriture {pair_leg['id']}): virement inter-dossiers" not in out


def test_une_ecriture_annulee_n_est_pas_un_retrait(fake, monkeypatch, capsys):
    """Un chèque annulé n'a jamais quitté le compte : les règles de retrait
    ne s'y appliquent pas — ni à sa correction, qui copie son mode.

    Réécrit à la revue du lot 5a (contrôle 10) : la contre-passation du
    paiement d'honoraires entraîne, comme la route (``routes/trust.
    _contrepasser_recette_administration``), celle de sa recette
    d'administration — sans elle, l'argent revenu au fidéicommis resterait
    compté au compte d'opérations, ce que le contrôle 10 signale."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake)
    _, errs = trust.reverse_transaction(fee["id"], "chèque perdu")
    assert errs == []
    recette = next(
        r for r in fake.peek_collection("admin_transactions").values()
        if r.get("trust_transaction_id") == fee["id"])
    _, errs = admin_ledger.reverse_transaction(
        recette["id"], "Contre-passation du virement au fidéicommis",
        allow_linked=True)
    assert errs == [], errs
    assert fake.peek(_tx(fee["id"]))["status"] == "annulée"
    _set(fake, _tx(fee["id"]), method="traite")
    code, out = _run(capsys)
    assert code == 0, out


# ══════════════════════════════════════════════════════════════════════
# 9 — les soldes par client, côté dossier
# ══════════════════════════════════════════════════════════════════════


def test_un_solde_stocke_sans_aucune_ecriture_est_un_ecart(fake, monkeypatch, capsys):
    """Les contrôles 3 et 4 partent des écritures : un solde stocké pour un
    client qui n'en a aucune leur est invisible."""
    _september(fake, monkeypatch)
    _set(fake, "dossiers/dos2", trust_balance=5000,
         trust_balance_by_client={"c2": 5000}, trust_cleared_by_client={"c2": 5000})
    code, out = _run(capsys)
    assert code == 1, out
    assert ("dossier dos2/c2: solde stocké (livres 5000 cents, compensé 5000 "
            "cents) sans aucune écriture au registre") in out
    assert "dossier dos2: trust_balance stocké 5000 sans aucune écriture au registre" in out


def test_un_client_a_decouvert_est_un_ecart(fake, monkeypatch, capsys):
    """Un chemin réel du modèle : la recette compensée est contre-passée
    APRÈS que son argent a été déboursé (le chèque du client est revenu
    sans provision). La contre-passation échappe au contrôle de découvert —
    à dessein —, et le client doit maintenant 1 000 $ au compte."""
    _september(fake, monkeypatch)
    rec2 = _create(amount=100000, dossier_id="dos2", client_id="c2",
                   date=_d(2026, 9, 10))
    _clear(rec2["id"], _d(2026, 9, 11))
    _create(direction="déboursé", amount=100000, dossier_id="dos2", client_id="c2",
            purpose="remise_client", counterparty="Marie Roy", date=_d(2026, 9, 12))
    _, errs = trust.reverse_transaction(rec2["id"], "chèque sans provision")
    assert errs == []
    code, out = _run(capsys)
    assert code == 1, out
    assert ("dossier dos2/c2: découvert en fidéicommis — solde aux livres "
            "-100000 cents, fonds compensés -100000 cents") in out
    # The stored maps agree with the register: this is not check 3 speaking.
    assert "trust_cleared_by_client stocké" not in out


def test_les_codes_de_sortie_sont_epingles(fake, monkeypatch, capsys):
    """0 propre · 1 écart(s) · 2 notes seulement — une note n'est jamais
    confondue avec un registre propre."""
    _september(fake, monkeypatch)
    assert _run(capsys)[0] == 0
    cash = _create(direction="déboursé", amount=20000, purpose="déboursé_tiers",
                   counterparty="Huissier", date=_d(2026, 9, 10))
    _set(fake, _tx(cash["id"]), method="comptant")
    assert _run(capsys)[0] == 2
    _set(fake, "trust_accounts/acc1", book_balance=1)
    assert _run(capsys)[0] == 1


# ══════════════════════════════════════════════════════════════════════
# 10 — le lien D-4 : chaque paiement d'honoraires et sa recette
# d'administration (revue du lot 5a)
# ══════════════════════════════════════════════════════════════════════


def _recettes_of(fake, fee_id: str) -> list[dict]:
    return [r for r in fake.peek_collection("admin_transactions").values()
            if r.get("trust_transaction_id") == fee_id]


def test_un_paiement_d_honoraires_sans_recette_apres_la_regle_d4_est_un_ecart(
    fake, monkeypatch, capsys
):
    """La recette d'administration s'inscrit APRÈS le commit du fidéicommis
    et échoue ouvert (un bandeau) : l'argent a quitté le fidéicommis, le
    compte d'opérations ne l'a jamais vu. Le script le disait « ✅ »."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake, recette=False)
    _set(fake, _tx(fee["id"]), created_at=_at(9, 10))
    code, out = _run(capsys)
    assert code == 1, out
    assert (f"(écriture {fee['id']}): paiement d'honoraires de 30000 cents sorti "
            f"du fidéicommis sans aucune recette d'administration qui l'adosse "
            f"(D-4)") in out.split("Notes à revoir")[0]


def test_un_paiement_d_honoraires_sans_recette_avant_la_regle_d4_est_une_note(
    fake, monkeypatch, capsys
):
    """Avant le 2026-08-17 11 h 23 (commit e719588), le compte
    d'administration était facultatif : l'absence est de l'historique."""
    _today(monkeypatch, "2026-08-17T16:00:00+00:00")
    dep = _create(amount=100000, date=_d(2026, 8, 10))
    _clear(dep["id"], _d(2026, 8, 11))
    fee = _fee_payment(fake, recette=False, date=_d(2026, 8, 17))
    _set(fake, _tx(dep["id"]), created_at=_at(8, 10), updated_at=_at(8, 11))
    before_d4 = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)  # 11 h HAE
    _set(fake, _tx(fee["id"]), created_at=before_d4, updated_at=before_d4)
    code, out = _run(capsys)
    assert code == 2, out
    notes = out.split("Notes à revoir", 1)[1]
    assert f"(écriture {fee['id']}): paiement d'honoraires de 30000 cents" in notes
    assert "avant la règle D-4 du 2026-08-17" in notes


def test_une_recette_inscrite_a_la_main_apres_le_bandeau_est_une_note(
    fake, monkeypatch, capsys
):
    """Quand la recette automatique échoue, la fiche du fidéicommis dit
    « inscrivez-la manuellement au registre d'administration » — et une
    recette inscrite à la main ne peut JAMAIS porter le lien (il ne voyage
    que par un argument nommé qu'aucun formulaire n'atteint). Suivre la
    consigne de l'application ne doit pas produire un écart : une recette
    non liée du même montant — un encaissement de la même facture — fait du
    lien manquant une NOTE à confirmer. Un montant différent n'en est pas
    une."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake, recette=False)
    _set(fake, _tx(fee["id"]), created_at=_at(9, 10))
    # Réécrit au lot 5a (étape 2) : un encaissement porte désormais son
    # paiement sur la facture DANS sa propre transaction, si bien que le
    # second encaissement ci-dessous voit le premier et se heurte au solde
    # vivant d'une facture de 500 $ (299,99 + 300 > 500). Le test ne porte
    # pas sur ce plafond : la facture reçoit la place des deux.
    _set(fake, "invoices/inv1", total=100000, amount_due=100000)
    recette, errs = admin_ledger.create_transaction({
        "account_id": "ops1", "kind": "encaissement_facture", "amount": 29999,
        "method": "virement", "counterparty": "Fidéicommis",
        "date": _d(2026, 9, 11), "invoice_id": "inv1",
    })
    assert errs == []
    assert _run(capsys)[0] == 1          # 299,99 $ is not the 300 $ that left
    manual, errs = admin_ledger.create_transaction({
        "account_id": "ops1", "kind": "encaissement_facture", "amount": 30000,
        "method": "virement", "counterparty": "Fidéicommis",
        "date": _d(2026, 9, 11), "invoice_id": "inv1",
    })
    assert errs == []
    code, out = _run(capsys)
    assert code == 2, out
    notes = out.split("Notes à revoir", 1)[1]
    assert (f"(écriture {fee['id']}): paiement d'honoraires de 30000 cents sans "
            f"recette d'administration LIÉE ; une recette non liée du même "
            f"montant existe ({manual['id']})") in notes
    assert recette["id"] not in out


def test_une_recette_datee_avant_son_paiement_d_honoraires_est_une_note_d16(
    fake, monkeypatch, capsys
):
    """D16 sur l'historique : l'argent arrive au compte d'opérations le jour
    où il quitte le fidéicommis, ou après — jamais avant."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake, recette=False)
    early = _admin_recette(fee, date=_d(2026, 9, 9))
    code, out = _run(capsys)
    assert code == 2, out
    assert (f"(écriture {fee['id']}): recette d'administration {early['id']} "
            f"datée du 2026-09-09, AVANT le paiement d'honoraires qu'elle porte "
            f"(2026-09-10)") in out.split("Notes à revoir", 1)[1]


def test_une_recette_qui_ne_couvre_pas_le_paiement_est_un_ecart(
    fake, monkeypatch, capsys
):
    _september(fake, monkeypatch)
    fee = _fee_payment(fake, recette=False)
    _admin_recette(fee, amount=20000)
    code, out = _run(capsys)
    assert code == 1, out
    assert (f"(écriture {fee['id']}): paiement d'honoraires de 30000 cents, mais "
            f"les recettes d'administration encore debout qui le portent "
            f"totalisent 20000 cents") in out


def test_un_virement_partage_entre_deux_recettes_passe_mais_une_seule_contre_passee_non(
    fake, monkeypatch, capsys
):
    """La forme de la reprise : un virement qui acquitte deux factures porte
    deux recettes dont la somme égale le virement au cent près — propre.
    L'ancienne cascade de contre-passation de la route ne contre-passait que
    la PREMIÈRE (``find_by_trust_transaction``, supprimé au lot 5a — la
    cascade lit désormais ``list_by_trust_transaction`` et les contre-passe
    toutes) : une seconde restée debout — la forme que ce test construit à
    la main — laisse le compte d'opérations compter l'argent revenu au
    fidéicommis, et le contrôle doit le dire."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake, recette=False)
    first = _admin_recette(fee, amount=10000)
    _admin_recette(fee, amount=20000)
    assert _run(capsys)[0] == 0
    _, errs = trust.reverse_transaction(fee["id"], "chèque perdu")
    assert errs == []
    _, errs = admin_ledger.reverse_transaction(
        first["id"], "Contre-passation du virement au fidéicommis", allow_linked=True)
    assert errs == []
    code, out = _run(capsys)
    assert code == 1, out
    assert (f"(écriture {fee['id']}): paiement d'honoraires annulé, mais 20000 "
            f"cents de recette d'administration liée restent debout") in out


def test_un_paiement_contre_passe_dont_la_recette_reste_debout_est_un_ecart(
    fake, monkeypatch, capsys
):
    """La cascade de la route échoue ouverte : la contre-passation du
    fidéicommis tient, la recette d'administration aussi."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake)
    _clear(fee["id"], _d(2026, 9, 11))
    _, errs = trust.reverse_transaction(fee["id"], "honoraires remboursés")
    assert errs == []
    assert fake.peek(_tx(fee["id"]))["status"] == "compensée"
    code, out = _run(capsys)
    assert code == 1, out
    [recette] = _recettes_of(fake, fee["id"])
    assert (f"(écriture {fee['id']}): paiement d'honoraires contre-passé, mais "
            f"30000 cents de recette d'administration liée restent debout "
            f"({recette['id']})") in out


def test_une_recette_liee_a_une_ecriture_qui_n_est_pas_un_paiement_est_un_ecart(
    fake, monkeypatch, capsys
):
    r1, _, _ = _september(fake, monkeypatch)
    recette = _admin_recette({"id": r1["id"], "amount": 5000, "date": _d(2026, 9, 10)})
    code, out = _run(capsys)
    assert code == 1, out
    assert (f"recette(s) d'administration {recette['id']} (5000 cents) liée(s) à "
            f"l'écriture du fidéicommis {r1['id']}, qui n'est pas un paiement "
            f"d'honoraires du registre") in out


def test_un_registre_d_administration_illisible_est_un_ecart(fake, monkeypatch, capsys):
    """Un contrôle qui ne peut pas lire doit le dire — jamais passer."""
    _september(fake, monkeypatch)
    _fee_payment(fake)
    server = fake._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kw):
        if request["parent"].endswith("/documents") and any(
            s.collection_id == "admin_transactions"
            for s in request["structured_query"].from_
        ):
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(server, "run_query", failing)
    code, out = _run(capsys)
    assert code == 1, out
    assert "registre d'administration illisible (RuntimeError)" in out


# ══════════════════════════════════════════════════════════════════════
# Une lecture STABLE : une écriture validée pendant la vérification
# (revue du lot 5a, étape 1 — concurrence)
#
# Le script lit le registre en plusieurs passes non transactionnelles :
# les comptes d'abord (leurs soldes dénormalisés), puis les écritures de
# chaque compte, puis les dossiers, puis le registre d'administration. Une
# écriture validée ENTRE deux de ces lectures — l'avocat dans un autre
# onglet, ou Claude par le connecteur — fabriquait un « écart » qu'aucune
# donnée ne porte : le solde stocké lu AVANT l'écriture, les écritures
# lues APRÈS. Code 1, « à corriger ou à expliquer avant d'aller plus
# loin ». Chaque écriture du registre réécrit son compte dans la même
# transaction ; deux relevés égaux de leurs update_time encadrant une
# passe prouvent donc qu'aucune n'a été validée pendant.
# ══════════════════════════════════════════════════════════════════════


def test_une_ecriture_validee_pendant_la_lecture_ne_fabrique_pas_d_ecart(
    fake, monkeypatch, capsys
):
    """Un dépôt est validé après la lecture des comptes et avant celle des
    écritures : l'ancien script comparait le book_balance d'AVANT au registre
    d'APRÈS et rendait « ❌ book_balance stocké 150000 ≠ recalculé 160000 ».
    Le script relit : la seconde passe est stable et propre."""
    _september(fake, monkeypatch)
    real = vti._account_transactions
    raced = []

    def racing(account_id):
        if not raced:
            raced.append(_create(amount=10000, date=_d(2026, 9, 15)))
        return real(account_id)

    monkeypatch.setattr(vti, "_account_transactions", racing)
    code, out = _run(capsys)
    assert raced, "the concurrent write must have happened"
    assert code == 0, out
    assert "book_balance stocké" not in out
    assert "nouvelle passe" in out


def test_un_paiement_d_honoraires_valide_pendant_la_lecture_ne_fabrique_pas_d_ecart(
    fake, monkeypatch, capsys
):
    """Le contrôle 10 lit le registre d'administration EN DERNIER : un
    paiement d'honoraires et sa recette validés entre la lecture du
    fidéicommis et celle de l'administration laissaient une recette « liée à
    une écriture qui n'est pas un paiement d'honoraires du registre »."""
    _september(fake, monkeypatch)
    real = vti._check_client_balances
    raced = []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not raced:
            raced.append(_fee_payment(fake, date=_d(2026, 9, 15)))
        return result

    monkeypatch.setattr(vti, "_check_client_balances", racing)
    code, out = _run(capsys)
    assert raced
    assert code == 0, out
    assert "qui n'est pas un paiement d'honoraires du registre" not in out


def test_un_registre_qui_change_a_chaque_passe_est_un_ecart_nomme(
    fake, monkeypatch, capsys
):
    """Une écriture à CHAQUE passe : le script ne conclut jamais sur une
    lecture instable. Il le dit en tête des écarts, code 1 — jamais un
    « ✅ » ni un écart présenté comme établi."""
    _september(fake, monkeypatch)
    real = vti._account_transactions
    writes = []

    def racing(account_id):
        writes.append(_create(amount=100 + len(writes), date=_d(2026, 9, 15)))
        return real(account_id)

    monkeypatch.setattr(vti, "_account_transactions", racing)
    code, out = _run(capsys)
    assert code == 1, out
    assert len(writes) == vti.STABLE_READ_ATTEMPTS
    assert (f"le registre a changé pendant chacune des "
            f"{vti.STABLE_READ_ATTEMPTS} passes") in out


def test_une_lecture_stable_ne_fait_qu_une_passe(fake, monkeypatch, capsys):
    _september(fake, monkeypatch)
    real = vti.collect
    passes = []

    def counting():
        passes.append(1)
        return real()

    monkeypatch.setattr(vti, "collect", counting)
    code, out = _run(capsys)
    assert code == 0, out
    assert passes == [1]
    assert "nouvelle passe" not in out


def test_chaque_ecriture_des_deux_registres_deplace_le_releve(fake, monkeypatch):
    """Le relevé repose sur une propriété des modèles : chaque écriture du
    fidéicommis ou de l'administration réécrit son COMPTE dans la même
    transaction. Épinglée ici pour chaque verbe — un verbe qui cesserait de
    le faire rendrait la garde aveugle à ses écritures."""
    _today(monkeypatch, "2026-09-20T16:00:00+00:00")
    marks = vti._write_marks()

    def moved() -> bool:
        nonlocal marks
        now = vti._write_marks()
        changed = now != marks
        marks = now
        return changed

    r = _create(amount=100000, date=_d(2026, 9, 1))
    assert moved(), "trust create"
    _clear(r["id"], _d(2026, 9, 2))
    assert moved(), "trust clear"
    d = _create(direction="déboursé", amount=1000, purpose="déboursé_tiers",
                counterparty="Huissier", date=_d(2026, 9, 3))
    assert moved()
    fee = _fee_payment(fake, date=_d(2026, 9, 10))
    assert moved(), "fee payment + admin recette"
    admin_row = _recettes_of(fake, fee["id"])[0]
    _, errs = admin_ledger.clear_transaction(admin_row["id"], _d(2026, 9, 11))
    assert errs == [] and moved(), "admin clear"
    _, errs = trust.reverse_transaction(d["id"], "chèque perdu")
    assert errs == [] and moved(), "trust reverse"
    _, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos2", "c2", 20000, "instruction", "virement", "")
    assert errs == [] and moved(), "trust transfer"
    rec, errs = trust.create_reconciliation("acc1", _d(2026, 9, 5), 100000)
    assert errs == []
    moved()
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == [] and moved(), "trust reconciliation completed"
