"""``scripts/rectify_registers`` — a repair that writes around the models.

The trust register is append-only and a reconciled administration entry is
locked, so nothing in the application corrects a payee typed wrong or a
date the Firestore console left with a time of day. The script does it for
a closed list of non-money fields, from a plan the lawyer approved, with
the house rule's stamps and a ``revisions`` trail. These tests run it on the
shared fake Firestore against registers the REAL models built, and pin what
it refuses: another day, a money field, a payee outside the firm profile, a
plan the store no longer matches — and that one refusal writes nothing.
"""

import json
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
    from scripts import rectify_registers as rr
    from scripts import verify_trust_integrity as vti

from tests._accounting_history import FEE_PAYEE, legacy_fee_entry  # noqa: E402
from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _at(m: int, d: int, h: int = 12) -> datetime:
    return datetime(2026, m, d, h, tzinfo=UTC)


@pytest.fixture
def fake(monkeypatch):
    mods = [m for n, m in sorted(sys.modules.items())
            if n.startswith("models.") and getattr(m, "db", None) is not None]
    f = install(monkeypatch, *mods, rr, vti)
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0, "etag": "e0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "client_ids": ["c1"], "clients": [{"id": "c1", "name": "Jean Tremblay"}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    })
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0",
    })
    return f


def _today(monkeypatch, iso: str) -> None:
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


def _set(fake, path: str, **fields) -> None:
    fake.external_write(path, {**fake.peek(path), **fields})


def _deposit(**over) -> dict:
    data = {
        "account_id": "acc1", "direction": "recette", "amount": 100000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Jean Tremblay",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 1),
        "description": "", "reference": "",
    }
    data.update(over)
    entry, errs = trust.create_transaction(data)
    assert errs == [], errs
    return entry


def _closed_september(fake, monkeypatch) -> dict:
    """A deposit cleared on 2 Sept., inside a reconciliation to 5 Sept.
    completed on 6 Sept. — the shape check 6 judges by ``updated_at``."""
    _today(monkeypatch, "2026-09-20T16:00:00+00:00")
    entry = _deposit()
    _, errs = trust.clear_transaction(entry["id"], _d(2026, 9, 2))
    assert errs == [], errs
    rec, errs = trust.create_reconciliation("acc1", _d(2026, 9, 5), 100000)
    assert errs == [], errs
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == [], errs
    _set(fake, f"trust_transactions/{entry['id']}", created_at=_at(9, 1), updated_at=_at(9, 2))
    _set(fake, f"trust_reconciliations/{rec['id']}", completed_date=_at(9, 6))
    return fake.peek(f"trust_transactions/{entry['id']}")


def _plan(tmp_path, *changes: dict, motif: str = "Approuvé par l'avocat.") -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"motif": motif, "changements": list(changes)},
                               default=str), encoding="utf-8")
    return str(path)


def _change(register: str, entry_id: str, field: str, before, after, **extra) -> dict:
    if isinstance(before, datetime):
        before = before.isoformat()
    return {"registre": register, "ecriture": entry_id, "champ": field,
            "avant": before, "apres": after, **extra}


def _run(capsys, *argv) -> tuple[int, str]:
    code = rr.main(list(argv))
    return code, capsys.readouterr().out


def _fee_payment_doc(fake, counterparty: str) -> str:
    """A fee payment as the July transcription left it: the client named as
    the payee. Seeded as stored history — the model refuses that payee."""
    fake.seed("trust_transactions/fee1", {
        "id": "fee1", "account_id": "acc1", "sequence": 7, "direction": "déboursé",
        "amount": 50000, "purpose": trust.FEE_PAYMENT_PURPOSE, "method": "virement",
        "counterparty": counterparty, "invoice_id": None,
        "invoice_external_ref": "256401-01", "date": _d(2026, 6, 1),
        "status": "compensée", "cleared_date": _d(2026, 6, 1),
        "created_at": _at(7, 17), "updated_at": _at(7, 17), "etag": "f0",
    })
    return "fee1"


# ══════════════════════════════════════════════════════════════════════
# À blanc par défaut, et la trace quand il écrit
# ══════════════════════════════════════════════════════════════════════


def test_a_blanc_rien_n_est_ecrit(fake, monkeypatch, capsys, tmp_path):
    entry = _closed_september(fake, monkeypatch)
    before = fake.peek(f"trust_transactions/{entry['id']}")
    plan = _plan(tmp_path, _change("fideicommis", entry["id"], "counterparty",
                                   "Jean Tremblay", "M. Jean Tremblay"))
    code, out = _run(capsys, plan)
    assert code == 0, out
    assert "« Jean Tremblay » → « M. Jean Tremblay »" in out
    assert "À blanc : rien n'a été écrit" in out
    assert fake.peek(f"trust_transactions/{entry['id']}") == before


def test_une_rectification_garde_sa_trace_et_ne_trompe_pas_le_controle_6(
    fake, monkeypatch, capsys, tmp_path
):
    """Les tampons de la maison (updated_at, etag, updated_via) et une entrée
    « revisions » qui garde l'updated_at remplacé — sans quoi le contrôle 6
    lirait la compensation du 2 sept. comme faite APRÈS la clôture du 6."""
    entry = _closed_september(fake, monkeypatch)
    plan = _plan(tmp_path, _change("fideicommis", entry["id"], "counterparty",
                                   "Jean Tremblay", "M. Jean Tremblay",
                                   motif="Nom du payeur complété."))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 0, out
    stored = fake.peek(f"trust_transactions/{entry['id']}")
    assert stored["counterparty"] == "M. Jean Tremblay"
    assert stored["updated_via"] == "script"
    assert stored["etag"] != entry["etag"]
    assert stored["updated_at"] > _at(9, 2)
    (trail,) = stored["revisions"]
    assert trail["via"] == "script"
    assert trail["motif"] == "Nom du payeur complété."
    assert trail["changes"] == {"counterparty": ["Jean Tremblay", "M. Jean Tremblay"]}
    assert trail["updated_at_before"] == _at(9, 2)
    # Nothing else moved: amount, status, sequence, balances.
    for key in ("amount", "status", "sequence", "balance_after_account",
                "balance_after_client", "cleared_date", "date"):
        assert stored[key] == entry[key], key

    assert vti.main() == 0, capsys.readouterr().out


def test_une_heure_est_ramenee_a_minuit_du_meme_jour(fake, monkeypatch, capsys, tmp_path):
    entry = _closed_september(fake, monkeypatch)
    path = f"trust_transactions/{entry['id']}"
    _set(fake, path, date=datetime(2026, 9, 1, 23, tzinfo=UTC))
    assert vti.main() == 2  # check 11's note
    capsys.readouterr()
    plan = _plan(tmp_path, _change("fideicommis", entry["id"], "date",
                                   datetime(2026, 9, 1, 23, tzinfo=UTC), "2026-09-01"))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 0, out
    assert fake.peek(path)["date"] == _d(2026, 9, 1)
    assert vti.main() == 0, capsys.readouterr().out


def test_le_beneficiaire_d_un_paiement_d_honoraires_suit_le_profil_du_cabinet(
    fake, capsys, tmp_path
):
    """D23 (art. 58) lie aussi une réparation : un paiement d'honoraires a
    pour bénéficiaire l'avocat ou son cabinet tels que les nomme le profil,
    et le nom s'inscrit comme le profil l'écrit."""
    fee = _fee_payment_doc(fake, "Mme Cliente")
    refused = _plan(tmp_path, _change("fideicommis", fee, "counterparty",
                                      "Mme Cliente", "Me Quelqu'un d'autre"))
    code, out = _run(capsys, refused, "--appliquer")
    assert code == 1, out
    assert "D23, art. 58" in out
    assert fake.peek(f"trust_transactions/{fee}")["counterparty"] == "Mme Cliente"

    accepted = _plan(tmp_path, _change("fideicommis", fee, "counterparty",
                                       "Mme Cliente", FEE_PAYEE.upper()))
    code, out = _run(capsys, accepted, "--appliquer")
    assert code == 0, out
    assert fake.peek(f"trust_transactions/{fee}")["counterparty"] == FEE_PAYEE


def test_la_reference_de_facture_d_un_paiement_d_honoraires(fake, capsys, tmp_path):
    fee = _fee_payment_doc(fake, FEE_PAYEE)
    plan = _plan(tmp_path, _change("fideicommis", fee, "invoice_external_ref",
                                   "256401-01", "256401-04"))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 0, out
    assert fake.peek(f"trust_transactions/{fee}")["invoice_external_ref"] == "256401-04"


# ══════════════════════════════════════════════════════════════════════
# Ce qu'il refuse — et un refus n'écrit rien
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("field, before, after, said", [
    ("date", "2026-09-01T00:00:00+00:00", "2026-08-31", "seule l'heure se rectifie"),
    ("amount", 100000, 1, "ne se rectifie pas ici"),
    ("status", "compensée", "annulée", "ne se rectifie pas ici"),
    ("counterparty", "Jean Tremblay", "<b>X</b>", "serait altéré"),
    ("counterparty", "Quelqu'un d'autre", "M. Jean Tremblay", "a changé depuis"),
])
def test_un_changement_refuse_refuse_tout_le_plan(
    fake, monkeypatch, capsys, tmp_path, field, before, after, said
):
    entry = _closed_september(fake, monkeypatch)
    snapshot = fake.peek(f"trust_transactions/{entry['id']}")
    # A valid change comes first: the refusal after it must take it down too.
    if field == "counterparty":
        valid = _change("fideicommis", entry["id"], "date", entry["date"], "2026-09-01")
    else:
        valid = _change("fideicommis", entry["id"], "counterparty",
                        "Jean Tremblay", "M. Jean Tremblay")
    plan = _plan(tmp_path, valid, _change("fideicommis", entry["id"], field, before, after))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 1, out
    assert "Plan refusé — rien n'a été écrit" in out
    assert said in out
    assert fake.peek(f"trust_transactions/{entry['id']}") == snapshot


def test_une_creation_ne_peut_pas_suivre_la_derniere_modification(
    fake, capsys, tmp_path
):
    """``created_at`` se rétablit (la console l'avait réécrit), jamais au-delà
    de la dernière modification de l'écriture."""
    row, errs = admin_ledger.create_transaction({
        "account_id": "ops1", "kind": "dépense", "category": "loyer", "amount": 1000,
        "method": "virement", "counterparty": "Fournisseur", "date": _d(2026, 9, 1),
        "description": "", "reference": "", "supplier_invoice_ref": "",
    })
    assert errs == [], errs
    path = f"admin_transactions/{row['id']}"
    stored = fake.peek(path)
    true_created = stored["created_at"]
    _set(fake, path, created_at=_d(2026, 8, 30))
    late = _plan(tmp_path, _change("administration", row["id"], "created_at",
                                   _d(2026, 8, 30), "2099-01-01T00:00:00+00:00"))
    code, out = _run(capsys, late, "--appliquer")
    assert code == 1, out
    assert "ne peut pas suivre la dernière modification" in out

    back = _plan(tmp_path, _change("administration", row["id"], "created_at",
                                   _d(2026, 8, 30), true_created.isoformat(),
                                   motif="Création réelle, lue par restauration à un instant."))
    code, out = _run(capsys, back, "--appliquer")
    assert code == 0, out
    after = fake.peek(path)
    assert after["created_at"] == true_created
    assert after["revisions"][-1]["changes"]["created_at"][0] == _d(2026, 8, 30)


def test_un_plan_applique_se_relance_sans_rien_ecrire(fake, monkeypatch, capsys, tmp_path):
    entry = _closed_september(fake, monkeypatch)
    plan = _plan(tmp_path, _change("fideicommis", entry["id"], "counterparty",
                                   "Jean Tremblay", "M. Jean Tremblay"))
    assert _run(capsys, plan, "--appliquer")[0] == 0
    applied = fake.peek(f"trust_transactions/{entry['id']}")
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 0, out
    assert "déjà fait" in out
    assert "Rien à écrire" in out
    assert fake.peek(f"trust_transactions/{entry['id']}") == applied


def test_un_paiement_a_un_tiers_se_reclasse_en_debourse_a_un_tiers(
    fake, monkeypatch, capsys, tmp_path
):
    """Décision de l'avocat (2026-10-06, séquences 28 et 42) — les honoraires
    d'une avocate-conseil, facturés au client comme débours et payés
    directement du fidéicommis, avaient été inscrits « Paiement
    d'honoraires » : deux notes pour toujours (bénéficiaire hors du profil —
    D23 —, aucune recette au compte d'opérations — D-4). Reclassée,
    l'écriture dit vrai et les deux notes tombent ; la référence de la
    facture qu'elle a réglée reste sur la fiche."""
    _today(monkeypatch, "2026-09-20T16:00:00+00:00")
    deposit = _deposit(amount=100000, date=_d(2026, 9, 1))
    _, errs = trust.clear_transaction(deposit["id"], _d(2026, 9, 2))
    assert errs == [], errs
    fee = legacy_fee_entry({
        "account_id": "acc1", "amount": 75000, "method": "virement",
        "counterparty": "Me Ines Benadda", "dossier_id": "dos1", "client_id": "c1",
        "date": _d(2026, 9, 3), "invoice_external_ref": "257701-01",
        "description": "", "reference": "",
    })
    path = f"trust_transactions/{fee['id']}"
    # Written before the D-4 rule, as the two production entries were.
    _set(fake, f"trust_transactions/{deposit['id']}", created_at=_at(7, 1), updated_at=_at(7, 2))
    _set(fake, path, created_at=_at(7, 18), updated_at=_at(7, 18))
    code = vti.main()
    out = capsys.readouterr().out
    assert code == 2, out
    assert "bénéficiaire n'est ni l'avocat ni son cabinet" in out
    assert "sans aucune recette d'administration" in out

    plan = _plan(tmp_path, _change("fideicommis", fee["id"], "purpose",
                                   trust.FEE_PAYMENT_PURPOSE, "déboursé_tiers",
                                   motif="Honoraires de l'avocate-conseil payés directement."))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 0, out
    stored = fake.peek(path)
    assert stored["purpose"] == "déboursé_tiers"
    assert stored["invoice_external_ref"] == "257701-01"
    assert stored["counterparty"] == "Me Ines Benadda"
    assert stored["revisions"][-1]["changes"] == {
        "purpose": [trust.FEE_PAYMENT_PURPOSE, "déboursé_tiers"]}
    code = vti.main()
    assert code == 0, capsys.readouterr().out


@pytest.mark.parametrize("target, linked, said", [
    ("déboursé_tiers", True, "une recette du registre d'administration y est liée (n° 9)"),
    ("remise_client", False, "seulement en « Déboursé à un tiers »"),
])
def test_la_reclassification_est_refusee_hors_de_son_cas(
    fake, capsys, tmp_path, target, linked, said
):
    """Une recette liée au registre d'administration dit que l'argent est
    allé au cabinet : c'était bien un paiement d'honoraires, et le
    reclasser rendrait cette recette orpheline. Et aucun autre objet."""
    fee = _fee_payment_doc(fake, "Me Ines Benadda")
    if linked:
        fake.seed("admin_transactions/rec1", {
            "id": "rec1", "account_id": "ops1", "sequence": 9,
            "kind": "encaissement_facture", "direction": "recette", "amount": 50000,
            "trust_transaction_id": fee, "status": "compensée", "date": _d(2026, 6, 1),
        })
    plan = _plan(tmp_path, _change("fideicommis", fee, "purpose",
                                   trust.FEE_PAYMENT_PURPOSE, target))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 1, out
    assert said in out
    assert fake.peek(f"trust_transactions/{fee}")["purpose"] == trust.FEE_PAYMENT_PURPOSE


def test_seul_un_paiement_d_honoraires_se_reclasse(fake, monkeypatch, capsys, tmp_path):
    entry = _closed_september(fake, monkeypatch)
    plan = _plan(tmp_path, _change("fideicommis", entry["id"], "purpose",
                                   "dépôt_client", "déboursé_tiers"))
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 1, out
    assert "seul un « Paiement d'honoraires » se reclasse" in out


def test_une_ecriture_modifiee_avant_le_lot_fait_tout_echouer(
    fake, monkeypatch, capsys, tmp_path
):
    """Le lot est gardé par l'instant de lecture de chaque document : une
    écriture validée entre la lecture et le lot fait échouer TOUT le lot —
    les deux documents restent tels quels."""
    entry = _closed_september(fake, monkeypatch)
    other = _deposit(amount=5000, date=_d(2026, 9, 10))
    plan = _plan(
        tmp_path,
        _change("fideicommis", entry["id"], "counterparty", "Jean Tremblay", "M. Jean Tremblay"),
        _change("fideicommis", other["id"], "counterparty", "Jean Tremblay", "M. Jean Tremblay"),
    )
    real_batch = rr.db.batch

    def racing_batch():
        batch = real_batch()
        real_commit = batch.commit

        def commit(*args, **kwargs):
            _set(fake, f"trust_transactions/{other['id']}", description="entre-temps")
            return real_commit(*args, **kwargs)

        batch.commit = commit
        return batch

    monkeypatch.setattr(rr.db, "batch", racing_batch)
    code, out = _run(capsys, plan, "--appliquer")
    assert code == 1, out
    assert "Rien n'a été écrit" in out
    assert fake.peek(f"trust_transactions/{entry['id']}")["counterparty"] == "Jean Tremblay"
    assert fake.peek(f"trust_transactions/{other['id']}")["counterparty"] == "Jean Tremblay"
