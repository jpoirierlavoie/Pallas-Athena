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
    from models import trust
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
    déboursé, virement à deux volets et contre-passation — rien à dire."""
    _september(fake, monkeypatch)
    deb = _create(direction="déboursé", amount=30000, purpose="déboursé_tiers",
                  counterparty="Huissier", date=_d(2026, 9, 10))
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


def _fee_payment(fake, **over) -> dict:
    fake.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2026-F001", "dossier_id": "dos1",
        "status": "envoyée", "total": 50000, "amount_due": 50000,
        "amount_paid": 0, "retainer_applied": 0,
    })
    return _create(**{
        "direction": "déboursé", "amount": 30000, "purpose": "virement_honoraires",
        "counterparty": "Me Avocat", "invoice_id": "inv1", "date": _d(2026, 9, 10),
        **over,
    })


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


def test_une_ecriture_annulee_n_est_pas_un_retrait(fake, monkeypatch, capsys):
    """Un chèque annulé n'a jamais quitté le compte : les règles de retrait
    ne s'y appliquent pas — ni à sa correction, qui copie son mode."""
    _september(fake, monkeypatch)
    fee = _fee_payment(fake)
    _, errs = trust.reverse_transaction(fee["id"], "chèque perdu")
    assert errs == []
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
