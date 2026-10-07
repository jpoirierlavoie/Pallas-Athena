"""``scripts/reclassify_admin_ledger`` — the one-shot reclassification of the
administration journal, written OUTSIDE the models.

The bench is built by the REAL models on the shared fake Firestore: an
operations account whose July is reconciled (so its entries are locked for
the model), a credit card paid from it, a dossier, a trust register. A CSV
in the lawyer's format (synthetic amounts and names only — the repository is
public) names the changes. These tests pin what the script writes, what it
refuses and why, that a refusal or a dry run writes nothing, that a second
pass finds everything done, the trust cross-checks, the §6 controls, the
fingerprint, the backup and the round trip of the restoration.
"""

import csv
import hashlib
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import uuid
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
    from models import trust
    from scripts import reclassify_admin_ledger as rla
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

from google.api_core import exceptions as gexc  # noqa: E402

from tests._accounting_history import legacy_fee_entry, legacy_trust_entry  # noqa: E402
from tests._fake_firestore import FakeFirestore, install  # noqa: E402

UTC = timezone.utc
TODAY = "2027-09-25T16:00:00+00:00"
DOSSIER_ID = "0b5e1c2a-1111-4111-8111-000000000001"
OTHER_DOSSIER_ID = "0b5e1c2a-1111-4111-8111-000000000002"
CLIENT_ID = "0b5e1c2a-1111-4111-8111-0000000000c1"
TRUST_ID = "0b5e1c2a-1111-4111-8111-0000000000f1"
TRUST_ID_2 = "0b5e1c2a-1111-4111-8111-0000000000f2"
FILE_NUMBER = "500-22-000001-269"
OWNER = "Me Exemple (compte personnel)"
TRUST = rla.TRUST_COUNTERPARTY
MOTIF_1 = "Reclassement : vérification du journal du 2026-10-07, ligne 1"


def _d(m: int, d: int) -> datetime:
    return datetime(2027, m, d, tzinfo=UTC)


def _freeze_today(set_attr, iso: str) -> None:
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    set_attr(dl, "datetime", _Clock)


def _ok(result):
    value, errors = result
    assert errors == [], errors
    return value


def _set(fake, path: str, **fields) -> None:
    fake.external_write(path, {**fake.peek(path), **fields})


# ── the bench ─────────────────────────────────────────────────────────────


def _entry(account: dict, kind: str, amount: int, day: datetime, *, counterparty: str,
           category=None, dossier_id=None, **split) -> dict:
    data = {"account_id": account["id"], "kind": kind, "amount": amount,
            "method": "virement", "counterparty": counterparty, "date": day,
            "description": "", "reference": "", "supplier_invoice_ref": ""}
    if category:
        data["category"] = category
    if dossier_id:
        data["dossier_id"] = dossier_id
    data.update(split)
    return _ok(al.create_transaction(data))


def _trust(amount: int, direction: str, purpose: str, day: datetime,
           account: str = TRUST_ID) -> dict:
    return _ok(trust.create_transaction({
        "account_id": account, "direction": direction, "amount": amount,
        "purpose": purpose, "method": "virement", "counterparty": "Client Exemple",
        "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID, "date": day,
        "description": "", "reference": "REF-EX",
    }))


def _line(ligne: int, groupe: str, entry: dict, *, statut: str = "appliquer", **columns) -> dict:
    """One CSV row in the lawyer's format, its « actuel » columns read off
    the stored entry; *columns* sets the targets (and anything else)."""
    row = {
        "ligne": str(ligne), "groupe": groupe, "statut": statut,
        "sequence": str(entry["sequence"]), "entry_id": entry["id"],
        "account_id": entry["account_id"],
        "date_actuelle": entry["date"].date().isoformat(),
        "montant_cents": str(entry["amount"]), "sens": entry["direction"],
        "kind_actuel": entry["kind"], "kind_cible": "",
        "category_actuelle": entry.get("category") or rla.NONE_MARK, "category_cible": "",
        "dossier_id_actuel": entry.get("dossier_id") or rla.NONE_MARK,
        "dossier_cible": "", "dossier_id_cible": "",
        "counterparty_actuel": entry.get("counterparty") or rla.NONE_MARK,
        "counterparty_cible": "", "date_cible": "",
        "supplier_invoice_ref_cible": "", "description_cible": "",
        "verification_prealable": "", "note": "",
    }
    unknown = set(columns) - set(row)
    assert not unknown, unknown
    row.update(columns)
    return row


def build_bench(fake, set_attr) -> dict:
    _freeze_today(set_attr, TODAY)
    fake.seed(f"trust_accounts/{TRUST_ID}", {
        "id": TRUST_ID, "name": "Général", "status": "actif", "account_type": "général",
        "book_balance": 0, "bank_balance": 0, "etag": "t0",
    })
    fake.seed(f"dossiers/{DOSSIER_ID}", {
        "id": DOSSIER_ID, "file_number": FILE_NUMBER, "title": "Exemple c. Démonstration",
        "client_ids": [CLIENT_ID], "clients": [{"id": CLIENT_ID, "name": "Client Exemple"}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    })
    ops = _ok(al.create_account({"name": "Opérations", "account_type": "opérations"}))
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    e: dict = {}
    # July — reconciled just below, so every one is LOCKED for the model.
    e["b1"] = _entry(ops, "dépense", 25013, _d(7, 3), category="autre", counterparty=OWNER)
    e["b2"] = _entry(ops, "recette_autre", 5021, _d(7, 10), counterparty=OWNER)
    e["a"] = _entry(ops, "dépense", 11505, _d(7, 15), category="honoraires_professionnels",
                    counterparty="Huissiers Exemple inc.",
                    net_amount=10007, gst_amount=500, qst_amount=998)
    rec = _ok(al.create_reconciliation(ops["id"], _d(7, 31), -25013 + 5021 - 11505))
    _ok(al.complete_reconciliation(rec["id"], [e["b1"]["id"], e["b2"]["id"], e["a"]["id"]]))
    # August.
    e["b1d"] = _entry(ops, "dépense", 12004, _d(8, 4), category="autre", counterparty=OWNER)
    e["charge"] = _entry(card, "dépense", 30000, _d(8, 2), category="fournitures",
                         counterparty="Papeterie Exemple")
    e["b3"] = _entry(ops, "dépense", 40027, _d(8, 5), category="autre", counterparty=TRUST)
    e["b3j"] = _entry(ops, "dépense", 1234, _d(8, 10), category="frais_bancaires",
                      counterparty="Banque Exemple")
    e["b4"] = _entry(ops, "recette_autre", 1234, _d(8, 12), counterparty="Banque Exemple")
    leg = _ok(al.create_card_payment(ops["id"], card["id"], 30000, _d(8, 18), "virement"))
    e["carte"] = fake.peek(f"admin_transactions/{leg['id']}")
    e["volet"] = fake.peek(f"admin_transactions/{leg['related_transaction_id']}")
    e["c"] = _entry(ops, "dépense", 650, _d(8, 20), category="intérêts",
                    counterparty="Banqe Exemple")
    e["e"] = _entry(ops, "recette_autre", 2000, _d(8, 22), counterparty="Client Exemple",
                    dossier_id=DOSSIER_ID)
    # The trust register.
    t: dict = {}
    t["t0"] = _trust(100003, "recette", "dépôt_client", _d(7, 1))
    _ok(trust.clear_transaction(t["t0"]["id"], _d(7, 2)))
    t["t1a"] = _trust(25009, "recette", "dépôt_client", _d(8, 5))
    t["t1b"] = _trust(15018, "recette", "dépôt_client", _d(8, 5))
    t["t2"] = _trust(1234, "recette", "dépôt_client", _d(8, 10))
    # Same day, same amount, the OTHER sens: never the counterpart of line 5.
    t["t3"] = _trust(1234, "déboursé", "remise_client", _d(8, 10))
    t["t4"] = _trust(1234, "déboursé", "remise_client", _d(8, 12))
    lines = [
        _line(1, "B1 prélèvement", e["b1"], kind_cible="prélèvement",
              category_cible=rla.NONE_MARK),
        _line(2, "B1 prélèvement + D date", e["b1d"], kind_cible="prélèvement",
              category_cible=rla.NONE_MARK, date_cible="2027-08-05"),
        _line(3, "B2 apport", e["b2"], kind_cible="apport"),
        _line(4, "B3 virement interne sortant", e["b3"], kind_cible="virement_interne_sortant",
              category_cible=rla.NONE_MARK,
              verification_prealable=("Dépôts correspondants au registre du fidéicommis : "
                                      f"{t['t1a']['id']} + {t['t1b']['id']}")),
        _line(5, "B3 virement interne sortant", e["b3j"], kind_cible="virement_interne_sortant",
              category_cible=rla.NONE_MARK, counterparty_cible=TRUST,
              verification_prealable="Relevé du fidéicommis : dépôt du même jour"),
        _line(6, "B4 virement interne entrant", e["b4"], kind_cible="virement_interne_entrant",
              counterparty_cible=TRUST,
              verification_prealable="Relevé du fidéicommis : retrait du même jour"),
        _line(7, "A catégorie et dossier", e["a"], category_cible="huissier",
              dossier_cible=FILE_NUMBER, dossier_id_cible=DOSSIER_ID,
              supplier_invoice_ref_cible="F-0001",
              description_cible="Signification de la demande"),
        _line(8, "C payeur ou bénéficiaire", e["c"], counterparty_cible="Banque Exemple"),
        _line(9, "D date", e["carte"], date_cible="2027-08-19",
              verification_prealable=("Paiement de carte : modifier aussi le volet lié de la "
                                      f"carte ({e['volet']['id']})")),
        _line(10, "E exclu", e["e"], statut="exclu", note="Revue à part"),
    ]
    return {"ops": ops, "card": card, "e": e, "t": t, "lines": lines}


def _write_csv(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rla.CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _lines(bench: dict, **changes) -> list[dict]:
    """A copy of the bench's rows, ``{line number: {column: value}}`` applied."""
    rows = [dict(row) for row in bench["lines"]]
    for row in rows:
        row.update(changes.get(f"l{row['ligne']}", {}))
    return rows


class Cli:
    def __init__(self, tmp_path: pathlib.Path, capsys):
        self.tmp, self.capsys, self.n = tmp_path, capsys, 0
        self.backups = tmp_path / "sauvegardes"
        self.backups.mkdir()

    def report(self) -> pathlib.Path:
        self.n += 1
        return self.tmp / f"rapport-{self.n}.md"

    def csv(self, rows: list[dict]) -> pathlib.Path:
        self.n += 1
        return _write_csv(self.tmp / f"reclassement-{self.n}.csv", rows)

    def run(self, *argv) -> tuple[int, str]:
        code = rla.main([str(a) for a in argv])
        return code, self.capsys.readouterr().out

    @staticmethod
    def _read(path: pathlib.Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def dry(self, csv_path) -> tuple[int, str, str, str]:
        report = self.report()
        code, out = self.run(csv_path, "--rapport", report)
        found = re.search(r"Empreinte : ([0-9a-f]{16})", out)
        return code, out, self._read(report), found.group(1) if found else ""

    def apply(self, csv_path, fingerprint: str) -> tuple[int, str, str]:
        report = self.report()
        code, out = self.run(csv_path, "--rapport", report, "--sauvegarde", self.backups,
                             "--appliquer", fingerprint)
        return code, out, self._read(report)

    def migrate(self, rows: list[dict]) -> pathlib.Path:
        """Dry run, then apply with its fingerprint; returns the CSV."""
        csv_path = self.csv(rows)
        code, out, _report, fingerprint = self.dry(csv_path)
        assert code == 0, out
        code, out, report = self.apply(csv_path, fingerprint)
        assert code == 0, out + report
        return csv_path

    def restore(self, backup: pathlib.Path, fingerprint: str = "") -> tuple[int, str, str, str]:
        report = self.report()
        argv = ["--restaurer", backup, "--rapport", report]
        if fingerprint:
            argv += ["--appliquer", fingerprint]
        code, out = self.run(*argv)
        found = re.search(r"Empreinte : ([0-9a-f]{16})", out)
        return code, out, self._read(report), found.group(1) if found else ""


@pytest.fixture
def fake(monkeypatch):
    mods = [m for n, m in sorted(sys.modules.items())
            if n.startswith("models.") and getattr(m, "db", None) is not None]
    return install(monkeypatch, *mods, rla, rr, vai, vti)


@pytest.fixture
def bench(fake, monkeypatch):
    return build_bench(fake, monkeypatch.setattr)


@pytest.fixture
def cli(tmp_path, capsys):
    return Cli(tmp_path, capsys)


def _tx(entry_or_id) -> str:
    entry_id = entry_or_id if isinstance(entry_or_id, str) else entry_or_id["id"]
    return f"admin_transactions/{entry_id}"


def _verdicts(plan) -> dict:
    return {v.line.ligne: v for v in plan.verdicts}


def _codes(verdict) -> list[str]:
    return [code for code, _message in verdict.reasons]


def _store_snapshot(fake) -> dict:
    return {name: fake.peek_collection(name)
            for name in ("admin_transactions", "admin_accounts", "admin_reconciliations",
                         "trust_transactions", "counters")}


# ══════════════════════════════════════════════════════════════════════
# 1. Dry run, then the write — what is written, and nothing else
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_the_report_says_everything(fake, bench, cli):
    csv_path = cli.csv(bench["lines"])
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(csv_path)
    assert code == 0, out
    assert len(fake.commits) == commits
    assert _store_snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert "Mode : à blanc" in report
    assert f"Projet : `{fake.project}`" in report
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert f"SHA-256 du CSV : `{digest}`" in report
    assert f"Empreinte : **{fingerprint}**" in report
    assert "À blanc : rien n'a été écrit" in out

    plan = rla.prepare(csv_path)
    assert {n: v.state for n, v in _verdicts(plan).items()} == dict.fromkeys(range(1, 10),
                                                                              rla.PENDING)
    roles = [w.role for w in plan.writes]
    # Nine entries, the card leg, and the two accounts whose days move.
    assert (roles.count("écriture"), roles.count("volet_carte"), roles.count("compte")) == (9, 1, 2)
    assert plan.blocking == [] and plan.gaps == [] and plan.impossible == []


def test_the_migration_writes_what_the_csv_says_and_nothing_else(fake, bench, cli):
    e = bench["e"]
    stored = {k: fake.peek(_tx(v)) for k, v in e.items()}
    accounts = {k: fake.peek(f"admin_accounts/{bench[k]['id']}") for k in ("ops", "card")}
    csv_path = cli.migrate(bench["lines"])
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()

    b1 = fake.peek(_tx(e["b1"]))
    assert (b1["kind"], b1["category"]) == ("prélèvement", None)
    for key in ("amount", "direction", "status", "cleared_date", "sequence", "account_id",
                "net_amount", "gst_amount", "qst_amount", "reconciliation_id", "date",
                "created_at", "created_via"):
        assert b1[key] == stored["b1"][key], key
    assert b1["updated_via"] == "script"
    assert b1["etag"] != stored["b1"]["etag"]
    (trail,) = b1["revisions"]
    assert trail["motif"] == MOTIF_1
    assert trail["via"] == "script"
    assert trail["source_sha256"] == digest
    assert trail["changes"] == {"kind": ["dépense", "prélèvement"], "category": ["autre", None]}
    assert trail["updated_at_before"] == stored["b1"]["updated_at"]

    assert fake.peek(_tx(e["b2"]))["kind"] == "apport"
    assert fake.peek(_tx(e["b3"]))["kind"] == "virement_interne_sortant"
    b3j = fake.peek(_tx(e["b3j"]))
    assert (b3j["kind"], b3j["category"], b3j["counterparty"]) == (
        "virement_interne_sortant", None, TRUST)
    assert fake.peek(_tx(e["b4"]))["counterparty"] == TRUST
    assert fake.peek(_tx(e["c"]))["counterparty"] == "Banque Exemple"
    b1d = fake.peek(_tx(e["b1d"]))
    assert (b1d["kind"], b1d["date"]) == ("prélèvement", _d(8, 5))

    # Group A: the category, the dossier and its snapshots — the TAXED split
    # stays as it was.
    a = fake.peek(_tx(e["a"]))
    assert (a["category"], a["dossier_id"], a["dossier_file_number"], a["dossier_title"]) == (
        "huissier", DOSSIER_ID, FILE_NUMBER, "Exemple c. Démonstration")
    assert (a["supplier_invoice_ref"], a["description"]) == ("F-0001", "Signification de la demande")
    assert (a["net_amount"], a["gst_amount"], a["qst_amount"]) == (10007, 500, 998)

    # The card payment: BOTH legs on the 19th, each with its own trail.
    for key in ("carte", "volet"):
        leg = fake.peek(_tx(e[key]))
        assert leg["date"] == _d(8, 19), key
        assert leg["revisions"][-1]["motif"].endswith("ligne 9"), key
        assert leg["revisions"][-1]["changes"] == {"date": [_d(8, 18), _d(8, 19)]}, key
        assert leg["status"] == stored[key]["status"], key
    # Their accounts: stamped (the reconciliation sentinel), never rebalanced.
    for key in ("ops", "card"):
        after = fake.peek(f"admin_accounts/{bench[key]['id']}")
        assert after["ledger_balance"] == accounts[key]["ledger_balance"], key
        assert after["etag"] != accounts[key]["etag"], key
        assert after["updated_via"] == "script", key
    # Untouched: the excluded line, and the card charge nobody named.
    assert fake.peek(_tx(e["e"])) == stored["e"]
    assert fake.peek(_tx(e["charge"])) == stored["charge"]

    assert vai.main() == 0


def test_a_second_pass_finds_every_line_applied_and_writes_nothing(fake, bench, cli):
    cli.migrate(bench["lines"])
    applied, commits = _store_snapshot(fake), len(fake.commits)
    csv_path = cli.csv(bench["lines"])
    code, out, report, fingerprint = cli.dry(csv_path)
    assert code == 0, out
    plan = rla.prepare(csv_path)
    assert {n: v.state for n, v in _verdicts(plan).items()} == dict.fromkeys(range(1, 10),
                                                                              rla.DONE)
    assert plan.writes == []
    code, out, report = cli.apply(csv_path, fingerprint)
    assert code == 0, out
    assert "Rien à écrire" in out
    assert len(fake.commits) == commits
    assert _store_snapshot(fake) == applied


@pytest.mark.parametrize("ligne, column, value, code", [
    (1, "sequence", "999", "séquence_différente"),
    (1, "account_id", OTHER_DOSSIER_ID, "compte_différent"),
    (1, "montant_cents", "24999", "montant_différent"),
    (1, "sens", "recette", "sens_différent"),
    (1, "date_actuelle", "2027-07-04", "actuel_différent:date"),
    (1, "dossier_id_actuel", DOSSIER_ID, "actuel_différent:dossier_id"),
    (1, "counterparty_actuel", "Quelqu'un d'autre", "actuel_différent:counterparty"),
    (1, "kind_actuel", "recette_autre", "valeur_inattendue:kind"),
    (1, "category_actuelle", "loyer", "valeur_inattendue:category"),
    (8, "kind_actuel", "recette_autre", "actuel_différent:kind"),
    (8, "category_actuelle", "loyer", "actuel_différent:category"),
    (2, "date_actuelle", "2027-08-03", "valeur_inattendue:date"),
    (8, "counterparty_actuel", "Autre Banque", "valeur_inattendue:counterparty"),
    (7, "dossier_id_actuel", OTHER_DOSSIER_ID, "valeur_inattendue:dossier_id"),
])
def test_a_stored_value_the_csv_does_not_describe_refuses_its_line(
    fake, bench, cli, ligne, column, value, code
):
    """Rule 4: the CSV describes the store; at the slightest difference the
    line is not applied — and only that line."""
    csv_path = cli.csv(_lines(bench, **{f"l{ligne}": {column: value}}))
    verdicts = _verdicts(rla.prepare(csv_path))
    assert verdicts[ligne].state == rla.REFUSED
    assert code in _codes(verdicts[ligne])
    assert all(v.state == rla.PENDING for n, v in verdicts.items() if n != ligne)


def test_a_refused_line_stays_untouched_while_the_others_are_written(fake, bench, cli):
    stored = fake.peek(_tx(bench["e"]["b2"]))
    cli.migrate(_lines(bench, l3={"sequence": "999"}))
    assert fake.peek(_tx(bench["e"]["b2"])) == stored
    assert fake.peek(_tx(bench["e"]["b1"]))["kind"] == "prélèvement"


def test_a_line_half_done_is_refused(fake, bench, cli):
    """Part done, part not: someone touched the entry — it is neither redone
    nor completed."""
    _set(fake, _tx(bench["e"]["b1"]), kind="prélèvement")   # its category is still « autre »
    verdicts = _verdicts(rla.prepare(cli.csv(bench["lines"])))
    assert _codes(verdicts[1]) == ["état_mixte"]


def test_a_card_leg_left_behind_is_a_mixed_state(fake, bench, cli):
    cli.migrate(bench["lines"])
    _set(fake, _tx(bench["e"]["volet"]), date=_d(8, 18))
    verdicts = _verdicts(rla.prepare(cli.csv(bench["lines"])))
    assert _codes(verdicts[9]) == ["état_mixte"]
    assert verdicts[1].state == rla.DONE


# ══════════════════════════════════════════════════════════════════════
# 2. The guards
# ══════════════════════════════════════════════════════════════════════


def _trail(n: int) -> list[dict]:
    return [{"at": _d(9, 1), "via": "web", "changes": {"description": ["a", "b"]}}] * n


@pytest.mark.parametrize("key, ligne, fields, code", [
    ("b1", 1, {"invoice_id": "facture-exemple"}, "liée_facture"),
    ("b2", 3, {"trust_transaction_id": "fideicommis-exemple"}, "liée_fideicommis"),
    ("b1", 1, {"status": "annulée"}, "annulée"),
    ("b1", 1, {"reversed_by_id": "correction-exemple"}, "lien_contre_passation"),
    ("b1", 1, {"net_amount": 24000, "gst_amount": 435, "qst_amount": 565},
     "ventilation_non_canonique"),
    ("b2", 3, {"net_amount": 5021}, "ventilation_non_canonique"),
    ("b1", 1, {"revisions": _trail(24)}, "fil_plein"),
    ("volet", 9, {"revisions": _trail(24)}, "volet_carte:fil_plein"),
    ("volet", 9, {"status": "annulée"}, "volet_carte:annulée"),
])
def test_a_guard_refuses_the_line(fake, bench, cli, key, ligne, fields, code):
    _set(fake, _tx(bench["e"][key]), **fields)
    verdicts = _verdicts(rla.prepare(cli.csv(bench["lines"])))
    assert code in _codes(verdicts[ligne])
    assert sum(v.state == rla.REFUSED for v in verdicts.values()) == 1


@pytest.mark.parametrize("key", ["b1", "volet"])
def test_twenty_three_trail_entries_leave_room_and_nothing_is_trimmed(fake, bench, cli, key):
    _set(fake, _tx(bench["e"][key]), revisions=_trail(23))
    cli.migrate(bench["lines"])
    revisions = fake.peek(_tx(bench["e"][key]))["revisions"]
    assert len(revisions) == 24
    assert revisions[:23] == _trail(23)


def test_a_reversed_entry_is_refused_group_A_and_card_payment_included(
    fake, bench, cli
):
    e = bench["e"]
    for key in ("b1", "a", "carte"):
        _ok(al.reverse_transaction(e[key]["id"], "Erreur de saisie"))
    verdicts = _verdicts(rla.prepare(cli.csv(bench["lines"])))
    for ligne in (1, 7, 9):
        assert "lien_contre_passation" in _codes(verdicts[ligne]), ligne
    # Both legs of the reversed card payment are refused, not one.
    assert "volet_carte:lien_contre_passation" in _codes(verdicts[9])
    assert {n for n, v in verdicts.items() if v.state == rla.REFUSED} == {1, 7, 9}


@pytest.mark.parametrize("ligne, columns, code", [
    (1, {"kind_cible": "apport", "category_cible": rla.NONE_MARK}, "sens_incohérent"),
    (1, {"kind_cible": "virement_interne_entrant", "category_cible": rla.NONE_MARK},
     "passage_non_permis"),
    (1, {"category_cible": ""}, "catégorie_restante"),
    (8, {"category_cible": rla.NONE_MARK}, "catégorie_requise"),
    (3, {"category_cible": "loyer"}, "catégorie_restante"),
    (10, {"statut": "appliquer", "category_cible": "loyer"}, "catégorie_hors_dépense"),
    (9, {"kind_cible": "virement_interne_sortant"}, "nature_structurelle"),
    (9, {"counterparty_cible": "Autre carte"}, "paiement_carte_autre_champ"),
    (9, {"verification_prealable": "volet non nommé"}, "volet_carte_non_désigné"),
    (9, {"verification_prealable": f"volet {OTHER_DOSSIER_ID}"}, "volet_carte_différent"),
])
def test_a_change_the_nature_refuses(fake, bench, cli, ligne, columns, code):
    verdicts = _verdicts(rla.prepare(cli.csv(_lines(bench, **{f"l{ligne}": columns}))))
    assert code in _codes(verdicts[ligne])


def test_an_apport_keeps_no_dossier(fake, bench, cli):
    """The excluded entry carries a dossier: making it an apport without
    clearing the dossier is refused; with « (aucune) », it is cleared."""
    e = bench["e"]
    rows = _lines(bench, l10={"statut": "appliquer", "groupe": "B2 apport",
                              "kind_cible": "apport"})
    assert "dossier_interdit" in _codes(_verdicts(rla.prepare(cli.csv(rows)))[10])
    rows = _lines(bench, l10={"statut": "appliquer", "groupe": "B2 apport",
                              "kind_cible": "apport", "dossier_id_cible": rla.NONE_MARK})
    cli.migrate(rows)
    stored = fake.peek(_tx(e["e"]))
    assert (stored["kind"], stored["dossier_id"], stored["dossier_file_number"],
            stored["dossier_title"]) == ("apport", None, "", "")


@pytest.mark.parametrize("columns, code", [
    ({"dossier_cible": "500-22-999999-269"}, "dossier_numéro_différent"),
    ({"dossier_id_cible": OTHER_DOSSIER_ID}, "dossier_introuvable"),
])
def test_a_group_A_dossier_is_read_and_its_number_checked(fake, bench, cli, columns, code):
    verdicts = _verdicts(rla.prepare(cli.csv(_lines(bench, l7=columns))))
    assert _codes(verdicts[7]) == [code]


@pytest.mark.parametrize("damage, code, said", [
    ("lien", "volet_carte_incohérent", "son lien ne revient pas à l'écriture"),
    ("absent", "volet_carte_introuvable", "introuvable"),
])
def test_a_card_leg_that_does_not_answer_refuses_its_payment(
    fake, bench, cli, damage, code, said
):
    """The leg the card payment names must exist and name it back: a leg
    linked elsewhere, or gone, refuses line 9 — neither leg is written."""
    volet = _tx(bench["e"]["volet"])
    if damage == "lien":
        _set(fake, volet, related_transaction_id=OTHER_DOSSIER_ID)
    else:
        fake.external_delete(volet)
    plan = rla.prepare(cli.csv(bench["lines"]))
    verdicts = _verdicts(plan)
    assert _codes(verdicts[9]) == [code]
    assert said in verdicts[9].reasons[0][1]
    assert {n for n, v in verdicts.items() if v.state == rla.REFUSED} == {9}
    assert not {_tx(bench["e"]["carte"]), volet} & {w.path for w in plan.writes}


_PAIR_VERIFIED = "lien réciproque, même montant, même jour, sens opposé : vérifiés"


def _section(report: str, title: str) -> str:
    return report.split(f"\n## {title}\n")[1].split("\n## ")[0]


@pytest.mark.parametrize("damage, code", [
    ("montant", "volet_carte_incohérent"),
    ("laissé", "état_mixte"),
])
def test_a_card_pair_that_failed_its_checks_is_never_reported_verified(
    fake, bench, cli, damage, code
):
    """The report says « vérifiés » only once every pair check passed: a leg
    whose amount changed, or a leg left behind, reads ÉCHEC and its reason."""
    assert _PAIR_VERIFIED in _section(cli.dry(cli.csv(bench["lines"]))[2], "Paiement de carte")
    volet = _tx(bench["e"]["volet"])
    if damage == "montant":
        _set(fake, volet, amount=29999)
    else:
        cli.migrate(bench["lines"])
        _set(fake, volet, date=_d(8, 18))
    csv_path = cli.csv(bench["lines"])
    verdict = _verdicts(rla.prepare(csv_path))[9]
    assert _codes(verdict) == [code]
    section = _section(cli.dry(csv_path)[2], "Paiement de carte")
    assert "vérifiés" not in section
    assert f"contrôles du volet : ÉCHEC — {verdict.reasons[0][1]}" in section


# ══════════════════════════════════════════════════════════════════════
# 3. The days
# ══════════════════════════════════════════════════════════════════════


def test_a_card_draft_ending_on_the_payment_day_refuses_the_move(fake, bench, cli):
    """The card leg is judged on ITS account: the card's pending
    reconciliation saw the payment on the 18th — on the 19th it would
    leave it."""
    _ok(al.create_reconciliation(bench["card"]["id"], _d(8, 18), 0))
    verdict = _verdicts(rla.prepare(cli.csv(bench["lines"])))[9]
    assert _codes(verdict) == ["volet_carte:date_conciliation"]


def test_a_completed_reconciliation_ending_between_the_two_days_refuses(fake, bench, cli):
    # As of Aug 4: July's −31 497, the Aug 4 dépense (12 004) left outstanding.
    rec = _ok(al.create_reconciliation(bench["ops"]["id"], _d(8, 4), -31497))
    _ok(al.complete_reconciliation(rec["id"], []))
    verdict = _verdicts(rla.prepare(cli.csv(bench["lines"])))[2]
    assert _codes(verdict) == ["date_conciliation"]


def test_a_day_moves_only_within_its_month(fake, bench, cli):
    verdict = _verdicts(rla.prepare(cli.csv(_lines(bench, l2={"date_cible": "2027-09-02"}))))[2]
    assert _codes(verdict) == ["date_hors_mois"]


def test_a_future_day_is_refused(fake, bench, cli, monkeypatch):
    _freeze_today(monkeypatch.setattr, "2027-08-04T16:00:00+00:00")
    verdicts = _verdicts(rla.prepare(cli.csv(bench["lines"])))
    assert _codes(verdicts[2]) == ["date_future"]
    assert "volet_carte:date_future" in _codes(verdicts[9])


def test_a_clearing_before_the_new_day_is_refused(fake, bench, cli):
    _ok(al.clear_transaction(bench["e"]["b1d"]["id"], _d(8, 4)))
    verdict = _verdicts(rla.prepare(cli.csv(bench["lines"])))[2]
    assert _codes(verdict) == ["date_compensation"]


def test_a_reconciliation_prepassed_before_the_batch_fails_as_modified(
    fake, bench, cli, monkeypatch
):
    """The reconciliation's sentinel compares the ACCOUNT's etag: the batch
    that moves a day stamps it, so a reconciliation computed before the
    batch no longer completes over entries that moved."""
    card = bench["card"]
    # As of Aug 31 the card shows the charge outstanding and the payment in
    # transit: book 0, statement 0. Nothing is ticked, so only the ACCOUNT's
    # etag can see the batch.
    rec = _ok(al.create_reconciliation(card["id"], _d(8, 31), 0))
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]
    real_context = al.reconciliation_as_of_context
    applied: dict = {}

    def pre_pass_then_batch(account_id, as_of):
        context = real_context(account_id, as_of)
        applied["code"], applied["out"], _report = cli.apply(csv_path, fingerprint)
        return context

    monkeypatch.setattr(al, "reconciliation_as_of_context", pre_pass_then_batch)
    done, errors = al.complete_reconciliation(rec["id"], [])
    assert applied["code"] == 0, applied["out"]
    assert done is None
    assert errors == [al._ABORT_MESSAGES["conciliation_modifiée"]]
    assert fake.peek(f"admin_reconciliations/{rec['id']}")["status"] == "brouillon"


# ══════════════════════════════════════════════════════════════════════
# 4. The trust account — reported, never written
# ══════════════════════════════════════════════════════════════════════


def _checks(plan) -> dict:
    return {c.ligne: c for c in plan.trust_checks}


def test_trust_movements_are_found_by_id_and_by_day(fake, bench, cli):
    t = bench["t"]
    csv_path = cli.csv(bench["lines"])
    plan = rla.prepare(csv_path)
    checks = _checks(plan)
    assert set(checks) == {4, 5, 6}
    # Line 4: the two deposits it names, whose sum is its amount.
    assert checks[4].outcome == rla.FOUND and checks[4].method == "identifiant"
    assert {m["id"] for m in checks[4].matches} == {t["t1a"]["id"], t["t1b"]["id"]}
    # Line 5: the same day holds a deposit AND a withdrawal of the same
    # amount — only the opposite sens is the counterpart.
    assert checks[5].outcome == rla.FOUND and checks[5].method == "jour"
    assert [m["id"] for m in checks[5].matches] == [t["t2"]["id"]]
    assert [m["id"] for m in checks[6].matches] == [t["t4"]["id"]]
    # Never a link written.
    code, out, _report = cli.apply(csv_path, plan.fingerprint)
    assert code == 0, out
    for key in ("b3", "b3j", "b4"):
        assert fake.peek(_tx(bench["e"][key])).get("trust_transaction_id") is None
    report = cli.dry(csv_path)[2]
    assert "Ligne 5 — TROUVÉ" in report
    assert f"dossier {FILE_NUMBER}" in report and "client Client Exemple" in report
    assert "réf. REF-EX" in report


def test_a_day_the_verification_names_otherwise_is_observed(fake, bench, cli):
    rows = _lines(bench, l5={"verification_prealable": "Relevé du fidéicommis : +12,34 $ le 2027-08-11"})
    report = cli.dry(cli.csv(rows))[2]
    assert ("Ligne 5 : la vérification préalable nomme le 2027-08-11 ; la recherche au "
            "fidéicommis porte sur le jour de l'écriture, le 2027-08-10.") in report
    assert "Ligne 6 : la vérification" not in report


def test_named_entries_not_summing_to_the_amount_refuse_the_line(fake, bench, cli):
    rows = _lines(bench, l4={"verification_prealable": f"Dépôt : {bench['t']['t1a']['id']}"})
    plan = rla.prepare(cli.csv(rows))
    assert _checks(plan)[4].outcome == rla.INCOHERENT
    assert _codes(_verdicts(plan)[4]) == ["fideicommis_incohérent"]


def test_a_correction_and_an_inter_dossier_leg_are_set_aside(
    fake, bench, cli, monkeypatch
):
    """On Aug 10 a second trust account holds a correction and an
    inter-dossier transfer leg of the same amount, in the right sens: set
    aside and listed apart — without that, line 5 would be AMBIGU."""
    fake.seed(f"trust_accounts/{TRUST_ID_2}", {
        "id": TRUST_ID_2, "name": "Général bis", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0, "etag": "t2",
    })
    _freeze_today(monkeypatch.setattr, "2027-08-10T16:00:00+00:00")
    deposit = _trust(10003, "recette", "dépôt_client", _d(8, 10), account=TRUST_ID_2)
    _ok(trust.clear_transaction(deposit["id"], _d(8, 10)))
    withdrawal = _trust(1234, "déboursé", "remise_client", _d(8, 10), account=TRUST_ID_2)
    correction = _ok(trust.reverse_transaction(withdrawal["id"], "Erreur de saisie"))
    transfer = legacy_trust_entry({
        "account_id": TRUST_ID_2, "direction": "recette", "amount": 1234,
        "purpose": trust.TRANSFER_PURPOSE, "method": "virement", "counterparty": "Dossier voisin",
        "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID, "date": _d(8, 10),
        "description": "", "reference": "",
    })
    _freeze_today(monkeypatch.setattr, TODAY)
    check = _checks(rla.prepare(cli.csv(bench["lines"])))[5]
    assert check.outcome == rla.FOUND
    assert [m["id"] for m in check.matches] == [bench["t"]["t2"]["id"]]
    assert {(entry["id"], why) for entry, why in check.aside} == {
        (correction["id"], "écriture de correction"),
        (transfer["id"], "virement inter-dossiers"),
    }


def test_a_fee_payment_behind_an_incoming_transfer_refuses_the_line(fake, bench, cli):
    """Fees taken from trust are revenue: booking them as an internal
    transfer would take them out of the results. The lawyer decides."""
    fake.seed(f"trust_accounts/{TRUST_ID_2}", {
        "id": TRUST_ID_2, "name": "Général bis", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0, "etag": "t2",
    })
    deposit = _trust(10003, "recette", "dépôt_client", _d(8, 14), account=TRUST_ID_2)
    _ok(trust.clear_transaction(deposit["id"], _d(8, 14)))
    legacy_fee_entry({
        "account_id": TRUST_ID_2, "amount": 1234, "method": "virement",
        "counterparty": "Cabinet Exemple", "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID,
        "date": _d(8, 14), "invoice_external_ref": "F-EX-01", "description": "", "reference": "",
    })
    incoming = _entry(bench["ops"], "recette_autre", 1234, _d(8, 14), counterparty="Banque Exemple")
    rows = bench["lines"] + [_line(11, "B4 virement interne entrant", incoming,
                                   kind_cible="virement_interne_entrant",
                                   counterparty_cible=TRUST)]
    plan = rla.prepare(cli.csv(rows))
    assert _checks(plan)[11].outcome == rla.INCOHERENT
    assert _codes(_verdicts(plan)[11]) == ["fideicommis_incohérent"]
    assert _verdicts(plan)[6].state == rla.PENDING


def test_a_fee_payment_among_several_candidates_still_refuses_the_line(fake, bench, cli):
    """Line 6 (B4) has its same-day withdrawal, a remise au client; a fee
    payment of the same amount on the same day makes two candidates. The fee
    payment is judged BEFORE ambiguity: INCOHÉRENT, every candidate listed,
    nothing claimed, the line refused — never an AMBIGU that lets the
    transfer through."""
    t4 = bench["t"]["t4"]
    fee = legacy_fee_entry({
        "account_id": TRUST_ID, "amount": 1234, "method": "virement",
        "counterparty": "Cabinet Exemple", "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID,
        "date": _d(8, 12), "invoice_external_ref": "F-EX-03", "description": "", "reference": "",
    })
    csv_path = cli.csv(bench["lines"])
    plan = rla.prepare(csv_path)
    check = _checks(plan)[6]
    assert check.outcome == rla.INCOHERENT
    assert {m["id"] for m in check.matches} == {t4["id"], fee["id"]}
    assert f"{fee['id']} : paiement d'honoraires, parmi 2 candidats le 2027-08-12" in check.detail
    assert _codes(_verdicts(plan)[6]) == ["fideicommis_incohérent"]
    assert _tx(bench["e"]["b4"]) not in {w.path for w in plan.writes}
    code, out, _report = cli.apply(csv_path, plan.fingerprint)
    assert code == 0, out
    stored = fake.peek(_tx(bench["e"]["b4"]))
    assert (stored["kind"], stored["counterparty"]) == ("recette_autre", "Banque Exemple")


def test_absent_and_ambiguous_do_not_block_and_nothing_is_claimed_twice(
    fake, bench, cli
):
    e, t = bench["e"], bench["t"]
    # A second deposit of 12,34 $ on Aug 10 (another trust account) makes
    # line 5 ambiguous; line 8, pointed at the trust account, finds nothing
    # on Aug 20; line 11 names a deposit line 4 already claimed.
    fake.seed(f"trust_accounts/{TRUST_ID_2}", {
        "id": TRUST_ID_2, "name": "Général bis", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0, "etag": "t2",
    })
    _trust(1234, "recette", "dépôt_client", _d(8, 10), account=TRUST_ID_2)
    late = _entry(bench["ops"], "dépense", 25009, _d(8, 25), category="autre", counterparty=TRUST)
    rows = _lines(bench, l8={"counterparty_cible": TRUST}) + [
        _line(11, "B3 virement interne sortant", late, kind_cible="virement_interne_sortant",
              category_cible=rla.NONE_MARK,
              verification_prealable=f"Dépôt : {t['t1a']['id']}")]
    csv_path = cli.csv(rows)
    plan = rla.prepare(csv_path)
    checks = _checks(plan)
    assert checks[5].outcome == rla.AMBIGUOUS
    assert checks[8].outcome == rla.ABSENT
    assert checks[11].outcome == rla.INCOHERENT
    assert "déjà attribuée à la ligne 4" in checks[11].detail
    verdicts = _verdicts(plan)
    assert verdicts[5].state == verdicts[8].state == rla.PENDING
    assert plan.blocking == []
    code, out, _report = cli.apply(csv_path, plan.fingerprint)
    assert code == 0, out
    assert fake.peek(_tx(e["c"]))["counterparty"] == TRUST


@pytest.mark.parametrize("failure", ["registre", "tronqué", "identifiant", "comptes"])
def test_a_failed_trust_read_is_unreadable_never_absent(
    fake, bench, cli, monkeypatch, failure
):
    def boom(*_args, **_kwargs):
        raise gexc.ServiceUnavailable("indisponible")

    if failure == "registre":
        monkeypatch.setattr(trust, "list_register", boom)
    elif failure == "tronqué":
        real = trust.list_register
        monkeypatch.setattr(trust, "list_register",
                            lambda *a, **k: (real(*a, **k)[0], True))
    elif failure == "identifiant":
        monkeypatch.setattr(trust, "get_transaction_strict", boom)
    else:
        monkeypatch.setattr(trust, "list_accounts", boom)
    csv_path = cli.csv(bench["lines"])
    plan = rla.prepare(csv_path)
    unreadable = {c.ligne for c in plan.trust_checks if c.outcome == rla.UNREADABLE}
    assert unreadable == ({4} if failure == "identifiant" else {5, 6})
    assert all(c.outcome != rla.ABSENT for c in plan.trust_checks)
    assert any("ILLISIBLE" in b for b in plan.blocking)
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(csv_path, plan.fingerprint)
    assert code == 1, out
    assert "Refusé — rien n'a été écrit" in out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before


# ══════════════════════════════════════════════════════════════════════
# 5. The controls of §6
# ══════════════════════════════════════════════════════════════════════


def test_the_controls_are_exact_before_simulated_and_real(fake, bench, cli):
    plan = rla.prepare(cli.csv(bench["lines"]))
    assert plan.gaps == [] and plan.impossible == []
    # What the CSV implies — gross by kind and category, nets from the moved
    # rows, no TPS/TVQ: the dépenses moved out, the receipts moved out, the
    # bailiff's invoice moved between two categories.
    assert plan.expected == {
        "expenses:autre": -(25013 + 12004 + 40027),
        "expenses:frais_bancaires": -1234,
        "expenses:honoraires_professionnels": -11505,
        "expenses:huissier": 11505,
        "expenses_net:autre": -(25013 + 12004 + 40027),
        "expenses_net:frais_bancaires": -1234,
        "expenses_net:honoraires_professionnels": -10007,
        "expenses_net:huissier": 10007,
        "revenue:recette_autre": -(5021 + 1234),
        "total_expenses": -(25013 + 12004 + 40027 + 1234),
        "total_revenue": -(5021 + 1234),
    }
    for key in ("6.1", "6.2", "6.3", "6.4"):
        assert plan.before[key] == plan.simulated[key], key
    months = plan.before["6.2"][bench["ops"]["id"]]
    assert list(months)[0] == "2027-07-31" and list(months)[-1] == "2027-09-30"
    assert set(plan.month_accounts) == {bench["ops"]["id"], bench["card"]["id"]}
    proof = plan.before["6.3"][bench["ops"]["id"]]
    assert proof["plancher"] == "2027-07-31"
    assert [p["ecart"] for p in proof["preuves"].values()] == [0]

    code, out, report = cli.apply(plan.csv_path, plan.fingerprint)
    assert code == 0, out
    assert "Contrôles après réel : conformes (avant = après)" in report
    assert "Écart à la passe approuvée : aucun (après réel = après simulé)" in report
    assert vai.main() == 0


def _table(section: str) -> dict:
    """``{first cell: [cells]}`` of the Markdown table rows in *section*."""
    rows = {}
    for text in section.splitlines():
        if text.startswith("| "):
            cells = [cell.strip() for cell in text.strip().strip("|").split(" | ")]
            rows[cells[0]] = cells
    return rows


def test_the_final_report_judges_the_results_statement_on_the_real_column_too(
    fake, bench, cli, monkeypatch
):
    """§6.5 in the final report: an expense recorded between the commit and
    the re-read moves the REAL results, not the simulated ones — the row
    reads ÉCART, where a status computed on the simulation alone read
    « conforme »."""
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]

    def then_an_expense(commit, *a, **k):
        result = commit(*a, **k)
        _entry(bench["ops"], "dépense", 777, _d(9, 1), category="loyer",
               counterparty="Immeuble Exemple")
        return result

    _wrap_commit(monkeypatch, then_an_expense)
    code, out, report = cli.apply(csv_path, fingerprint)
    assert code == 0, out
    rows = _table(report.split("### 6.5")[1].split("\n### ")[0])
    assert rows["Poste"][-5:] == ["après (réel)", "Écart (simulé)", "Écart (réel)",
                                  "Écart attendu (CSV)", "Statut"]
    for label in ("Total des dépenses", "Dépenses (montant) — loyer",
                  "Dépenses (net) — loyer"):
        assert rows[label][-1] == "ÉCART", rows[label]
    # The simulated delta is still the CSV's on those rows; elsewhere both are.
    assert rows["Total des dépenses"][-4] == rows["Total des dépenses"][-2]
    assert rows["Revenus — recette_autre"][-1] == "conforme"
    assert rows["TPS"][-1] == "conforme"


def test_a_control_that_moves_blocks_the_application(fake, bench, cli, monkeypatch):
    """The control proves it catches something: a batch that changed an
    amount would move the simulated balances — and nothing is written."""
    real = rla.build_payloads

    def tampered(writes, now, source_sha, motif):
        payloads = real(writes, now, source_sha, motif)
        payloads[_tx(bench["e"]["a"])]["amount"] = 1
        return payloads

    monkeypatch.setattr(rla, "build_payloads", tampered)
    csv_path = cli.csv(bench["lines"])
    plan = rla.prepare(csv_path)
    assert any(g.startswith("§6.1") for g in plan.gaps)
    assert any(g.startswith("§6.5") for g in plan.gaps)
    commits = len(fake.commits)
    code, out, _report = cli.apply(csv_path, plan.fingerprint)
    assert code == 1, out
    assert len(fake.commits) == commits


@pytest.mark.parametrize("seed, said", [
    ("invoices/fac-ex", "§6.4 impossible"),
    ("admin_transactions/corr-ex", "§6.5 impossible"),
])
def test_a_control_that_cannot_be_computed_blocks_the_application(fake, bench, cli, seed, said):
    if seed.startswith("invoices"):
        fake.seed(seed, {"id": "fac-ex", "amount_due": None, "amount_paid": 0})
    else:
        # A correction whose original is neither stamped nor found.
        fake.seed(seed, {"id": "corr-ex", "account_id": bench["ops"]["id"], "sequence": 999,
                         "kind": "correction", "direction": "recette", "amount": 100,
                         "reverses_id": "introuvable", "status": "en_circulation",
                         "date": _d(9, 1)})
    plan = rla.prepare(cli.csv(bench["lines"]))
    assert any(said in b for b in plan.blocking), plan.blocking
    commits = len(fake.commits)
    assert cli.apply(plan.csv_path, plan.fingerprint)[0] == 1
    assert len(fake.commits) == commits


def test_simulated_check_10_lists_a_fee_payment_losing_its_candidate(
    fake, bench, cli
):
    """A receipt of the same amount after an unlinked fee payment is its
    candidate manual recette (check 10); reclassified as an apport, it no
    longer is — the report says so."""
    fake.seed(f"trust_accounts/{TRUST_ID_2}", {
        "id": TRUST_ID_2, "name": "Général bis", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0, "etag": "t2",
    })
    deposit = _trust(10003, "recette", "dépôt_client", _d(7, 5), account=TRUST_ID_2)
    _ok(trust.clear_transaction(deposit["id"], _d(7, 5)))
    fee = legacy_fee_entry({
        "account_id": TRUST_ID_2, "amount": 5021, "method": "virement",
        "counterparty": "Cabinet Exemple", "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID,
        "date": _d(7, 9), "invoice_external_ref": "F-EX-02", "description": "", "reference": "",
    })
    plan = rla.prepare(cli.csv(bench["lines"]))
    assert plan.fee_changes == [(fee["id"], [bench["e"]["b2"]["id"]], [])]
    report = cli.dry(plan.csv_path)[2]
    assert f"paiement d'honoraires {fee['id']}" in report


# ══════════════════════════════════════════════════════════════════════
# 6. The fingerprint
# ══════════════════════════════════════════════════════════════════════


def deterministic_fingerprint(workdir: str) -> str:
    """The dry run's fingerprint over the bench, every id and clock fixed —
    what the PYTHONHASHSEED test runs in fresh processes. Never under pytest:
    it rebinds ``uuid.uuid4`` and every module's ``db`` for good."""
    counter = itertools.count(1)
    uuid.uuid4 = lambda: uuid.UUID(int=next(counter))
    fake = FakeFirestore()
    for name, module in list(sys.modules.items()):
        if (name == "models" or name.startswith(("models.", "scripts."))) \
                and getattr(module, "db", None) is not None:
            module.db = fake
    bench = build_bench(fake, setattr)
    # Refused, excluded and written lines alike: the fingerprint covers all.
    rows = _lines(bench, l3={"sequence": "999"}, l8={"counterparty_actuel": "Autre"})
    return rla.prepare(_write_csv(pathlib.Path(workdir) / "r.csv", rows)).fingerprint


_HASHSEED_PROBE = textwrap.dedent("""
    import sys
    sys.path.insert(0, ".")
    from tests import test_reclassify_admin_ledger as t
    print("@@EMPREINTE@@" + t.deterministic_fingerprint(sys.argv[1]))
""")


def test_the_fingerprint_is_stable_across_processes(tmp_path):
    """Two processes, two hash seeds: the same fingerprint — no set or dict
    iteration order enters it."""
    found = []
    for seed in ("0", "4242"):
        work = tmp_path / seed
        work.mkdir()
        proc = subprocess.run(
            [sys.executable, "-c", _HASHSEED_PROBE, str(work)], cwd=_ATHENA,
            env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True,
            encoding="utf-8", timeout=240, stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        line = next(l for l in proc.stdout.splitlines() if l.startswith("@@EMPREINTE@@"))
        found.append(line[len("@@EMPREINTE@@"):])
    assert re.fullmatch(r"[0-9a-f]{16}", found[0])
    assert found[0] == found[1]


def test_a_different_fingerprint_writes_nothing(fake, bench, cli):
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(csv_path, "0" * 16)
    assert code == 1, out
    assert "l'empreinte a changé" in out
    assert "Empreinte approuvée : `0000000000000000` — DIFFÈRE" in report
    # Something moved since the approved dry run: a new approval is needed.
    _set(fake, _tx(bench["e"]["b2"]), description="entre-temps")
    before = _store_snapshot(fake)
    code, out, _report = cli.apply(csv_path, fingerprint)
    assert code == 1, out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before
    assert not list(cli.backups.iterdir())


def test_an_entry_recorded_since_the_dry_run_calls_for_a_new_approval(fake, bench, cli):
    """The print covers EVERY planned write, the accounts' stamp-only updates
    included (plan §5): an entry recorded on the operations account after
    the approved dry run moves the account's update time — the approval no
    longer holds and nothing is written. A new dry run, approved, applies."""
    csv_path = cli.csv(bench["lines"])
    code, out, _report, fingerprint = cli.dry(csv_path)
    assert code == 0, out
    _entry(bench["ops"], "dépense", 777, _d(9, 1), category="loyer",
           counterparty="Immeuble Exemple")
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(csv_path, fingerprint)
    assert code == 1, out + report
    assert "l'empreinte a changé" in out
    assert f"Empreinte approuvée : `{fingerprint}` — DIFFÈRE" in report
    assert len(fake.commits) == commits and _store_snapshot(fake) == before
    assert not list(cli.backups.iterdir())
    code, out, _report, renewed = cli.dry(csv_path)
    assert code == 0 and renewed != fingerprint, out
    code, out, report = cli.apply(csv_path, renewed)
    assert code == 0, out + report


# ══════════════════════════════════════════════════════════════════════
# 7. The write: the race, the lost answer, the backup
# ══════════════════════════════════════════════════════════════════════


def _wrap_commit(monkeypatch, around):
    real_batch = rla.db.batch

    def batch():
        b = real_batch()
        real_commit = b.commit
        b.commit = lambda *a, **k: around(real_commit, *a, **k)
        return b

    monkeypatch.setattr(rla.db, "batch", batch)


def test_an_entry_changed_before_the_batch_fails_it_whole(fake, bench, cli, monkeypatch):
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]

    def racing(commit, *a, **k):
        _set(fake, _tx(bench["e"]["b2"]), description="entre-temps")
        return commit(*a, **k)

    _wrap_commit(monkeypatch, racing)
    code, out, _report = cli.apply(csv_path, fingerprint)
    assert code == 1, out
    assert "Rien n'a été écrit" in out
    assert fake.peek(_tx(bench["e"]["b1"]))["kind"] == "dépense"
    assert fake.peek(_tx(bench["e"]["carte"]))["date"] == _d(8, 18)


def test_a_lost_answer_is_reread_and_reported_written(fake, bench, cli, monkeypatch):
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]

    def lost(commit, *a, **k):
        commit(*a, **k)
        raise gexc.DeadlineExceeded("réponse perdue")

    _wrap_commit(monkeypatch, lost)
    code, out, report = cli.apply(csv_path, fingerprint)
    assert code == 0, out
    assert "la réponse du serveur s'était perdue (DeadlineExceeded)" in out
    assert fake.peek(_tx(bench["e"]["b1"]))["kind"] == "prélèvement"
    assert "Contrôles après réel : conformes" in report


def test_an_outcome_the_reread_cannot_settle_is_uncertain(fake, bench, cli, monkeypatch):
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]

    def blind(_commit, *a, **k):
        def unreadable(*_a, **_k):
            raise gexc.ServiceUnavailable("relecture impossible")

        monkeypatch.setattr(fake._fake_server, "batch_get_documents", unreadable)
        raise gexc.ServiceUnavailable("commit sans réponse")

    _wrap_commit(monkeypatch, blind)
    code, out, _report = cli.apply(csv_path, fingerprint)
    assert code == 1, out
    assert "Issue INCERTAINE (ServiceUnavailable)" in out


def test_a_lost_answer_with_nothing_landed_says_nothing_was_written(
    fake, bench, cli, monkeypatch
):
    """The third outcome of the settle: the commit raised, the re-read finds
    none of the migration's etags — « Rien n'a été écrit », exit 1."""
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]

    def dropped(_commit, *_a, **_k):
        raise gexc.DeadlineExceeded("délai dépassé")

    _wrap_commit(monkeypatch, dropped)
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(csv_path, fingerprint)
    assert code == 1, out
    assert "Rien n'a été écrit (DeadlineExceeded)" in out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before
    assert "Contrôles après réel" not in report


def test_the_backup_is_on_disk_and_verified_when_the_batch_starts(
    fake, bench, cli, monkeypatch
):
    """Rule 6: the backup comes BEFORE the write. When the batch is entered,
    nothing is written yet, and the backup file is there, re-reads whole and
    names the etag each document of the batch is about to get."""
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]
    real_commit, commits, entered = rla.commit, len(fake.commits), []

    def after_the_backup(writes, payloads):
        assert len(fake.commits) == commits
        (backup,) = cli.backups.iterdir()
        body = json.loads(backup.read_text(encoding="utf-8"))
        assert body["contenu_sha256"] == rla._digest(body["documents"])
        assert {d["chemin"]: d["etag_migration"] for d in body["documents"]} == {
            write.path: payloads[write.path]["etag"] for write in writes}
        entered.append(len(writes))
        return real_commit(writes, payloads)

    monkeypatch.setattr(rla, "commit", after_the_backup)
    code, out, _report = cli.apply(csv_path, fingerprint)
    assert code == 0, out
    assert entered == [12]


@pytest.mark.parametrize("failure, error", [
    ("dossier disparu", "FileNotFoundError"),
    ("octets", "TypeError"),
    ("clé réservée", "TypeError"),
    ("relecture altérée", "OSError"),
])
def test_a_failed_backup_writes_nothing(fake, bench, cli, monkeypatch, failure, error):
    """No backup, no batch: a folder gone since the path check, a value the
    typed JSON cannot hold faithfully (bytes; a map whose « $ts » key would
    come back as an instant), a file that does not re-read as written — exit
    1, no commit, the store unchanged."""
    if failure == "octets":
        _set(fake, _tx(bench["e"]["b1"]), piece=b"\x00\x01")
    elif failure == "clé réservée":
        _set(fake, _tx(bench["e"]["b1"]), meta={"$ts": "2027-07-03T00:00:00+00:00"})
    csv_path = cli.csv(bench["lines"])
    fingerprint = cli.dry(csv_path)[3]
    if failure == "dossier disparu":
        real_prepare = rla.prepare

        def then_gone(source):
            plan = real_prepare(source)
            cli.backups.rmdir()
            return plan

        monkeypatch.setattr(rla, "prepare", then_gone)
    elif failure == "relecture altérée":
        real_read = pathlib.Path.read_bytes

        def altered(self):
            data = real_read(self)
            if self.name.startswith("reclassement-administration-"):
                return data.replace(b'"ligne": 1,', b'"ligne": 99,', 1)
            return data

        monkeypatch.setattr(pathlib.Path, "read_bytes", altered)
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(csv_path, fingerprint)
    assert code == 1, out
    assert f"sauvegarde impossible ({error}) — rien n'a été écrit" in out
    assert "Application refusée — rien n'a été écrit" in report
    assert len(fake.commits) == commits and _store_snapshot(fake) == before


def test_the_backup_is_typed_reread_and_carries_the_migration_etag(
    fake, bench, cli
):
    stored = fake.peek(_tx(bench["e"]["b1"]))
    csv_path = cli.migrate(bench["lines"])
    (backup,) = cli.backups.iterdir()
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rla.BACKUP_FORMAT
    assert body["projet"] == fake.project
    assert body["csv"]["sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    documents = {d["chemin"]: d for d in body["documents"]}
    assert len(documents) == 12
    entry = documents[_tx(bench["e"]["b1"])]
    assert entry["ligne"] == 1 and entry["role"] == "écriture"
    assert entry["champs"] == ["category", "kind"]
    assert entry["etag_migration"] == fake.peek(_tx(bench["e"]["b1"]))["etag"]
    assert entry["avant"]["date"] == {"$ts": "2027-07-03T00:00:00+00:00"}
    assert rla._untyped(entry["avant"]) == stored
    assert entry["update_time_lu"].endswith("Z")
    assert documents[f"admin_accounts/{bench['card']['id']}"]["role"] == "compte"


def test_applying_requires_a_backup_directory_and_a_well_formed_fingerprint(fake, bench, cli):
    csv_path = cli.csv(bench["lines"])
    code, out = cli.run(csv_path, "--rapport", cli.report(), "--appliquer", "0" * 16)
    assert code == 1 and "--appliquer exige --sauvegarde" in out
    code, out = cli.run(csv_path, "--rapport", cli.report(), "--sauvegarde", cli.backups,
                        "--appliquer", "pas-une-empreinte")
    assert code == 1 and "16 caractères hexadécimaux" in out


# ══════════════════════════════════════════════════════════════════════
# 8. The restoration
# ══════════════════════════════════════════════════════════════════════


def test_the_restoration_round_trips(fake, bench, cli):
    plan = rla.prepare(cli.csv(bench["lines"]))
    paths = [w.path for w in plan.writes]
    original = {path: fake.peek(path) for path in paths}
    cli.migrate(bench["lines"])
    migrated = {path: fake.peek(path) for path in paths}
    (backup,) = cli.backups.iterdir()

    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out
    assert "Mode : restauration à blanc" in report
    assert {path: fake.peek(path) for path in paths} == migrated
    code, out, report = cli.restore(backup, fingerprint)[:3]
    assert code == 0, out + report
    # The results line reports the REAL column too once written (it showed
    # only the simulation).
    assert "- §6.5 (après (simulé), pour information) :" in report
    assert "- §6.5 (après (réel), pour information) :" in report

    for write in plan.writes:
        now, was = fake.peek(write.path), original[write.path]
        if write.role == "compte":
            assert now["ledger_balance"] == was["ledger_balance"], write.path
            assert now["etag"] != migrated[write.path]["etag"], write.path
            continue
        for name in write.business:
            assert now.get(name) == was.get(name), (write.path, name)
        assert now["etag"] != migrated[write.path]["etag"]
        assert len(now["revisions"]) == len(was.get("revisions") or []) + 2
        motif = now["revisions"][-1]["motif"]
        assert motif.startswith("Restauration de la sauvegarde du ")
        assert motif.endswith(f" — ligne {write.ligne}")
        assert now["revisions"][-1]["source_sha256"] == \
            hashlib.sha256(backup.read_bytes()).hexdigest()
    assert vai.main() == 0


def test_a_restoration_refuses_everything_when_an_etag_moved(fake, bench, cli):
    """All or nothing: one entry modified since the migration, and nothing is
    restored — both card legs included."""
    cli.migrate(bench["lines"])
    (backup,) = cli.backups.iterdir()
    _set(fake, _tx(bench["e"]["volet"]), etag="modifiée-ailleurs")
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.restore(backup)
    assert code == 1, out
    assert "etag_déplacé" in report and "Restauration refusée en entier" in report
    code, out, _report, _fp = cli.restore(backup, fingerprint)
    assert code == 1, out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before
    assert fake.peek(_tx(bench["e"]["carte"]))["date"] == _d(8, 19)


def _restore_refused(fake, cli, backup, code: str) -> None:
    """The restoration of *backup* is refused whole with *code*, dry and
    applied alike, and writes nothing."""
    before, commits = _store_snapshot(fake), len(fake.commits)
    status, out, report, fingerprint = cli.restore(backup)
    assert status == 1, out
    assert f"`{code}`" in report and "Restauration refusée en entier" in report
    status, out, _report, _fp = cli.restore(backup, fingerprint)
    assert status == 1, out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before


def test_a_restoration_refuses_a_value_changed_outside_the_model(fake, bench, cli):
    """The etag stayed the migration's, but a business field it wrote was
    changed behind the model's back (the console): restoring over it would
    erase what the etag cannot see — refused whole."""
    cli.migrate(bench["lines"])
    (backup,) = cli.backups.iterdir()
    path = _tx(bench["e"]["b2"])
    etag = fake.peek(path)["etag"]
    _set(fake, path, kind="recette_autre")
    assert fake.peek(path)["etag"] == etag
    _restore_refused(fake, cli, backup, "valeur_déplacée")


def test_a_restoration_takes_the_last_place_in_a_trail_and_refuses_a_full_one(
    fake, bench, cli
):
    """« Rien ne se supprime »: one place left in the trail is enough for the
    restoration's entry; with none left it is refused, never trimmed."""
    cli.migrate(bench["lines"])
    (backup,) = cli.backups.iterdir()
    path = _tx(bench["e"]["b2"])
    _set(fake, path, revisions=_trail(al._REVISIONS_CAP - 1))
    code, out, report, _fp = cli.restore(backup)
    assert code == 0, out + report
    _set(fake, path, revisions=_trail(al._REVISIONS_CAP))
    _restore_refused(fake, cli, backup, "fil_plein")


def test_a_tampered_backup_is_refused(fake, bench, cli):
    """A backup whose documents no longer match their recorded digest would
    restore what nobody saved: refused before any read of the registers."""
    cli.migrate(bench["lines"])
    (backup,) = cli.backups.iterdir()
    body = json.loads(backup.read_text(encoding="utf-8"))
    entry = next(d for d in body["documents"] if d["chemin"] == _tx(bench["e"]["b1"]))
    entry["avant"]["kind"] = "recette_autre"
    tampered = cli.tmp / "sauvegarde-modifiée.json"
    tampered.write_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    before, commits, reads = _store_snapshot(fake), len(fake.commits), len(fake.reads)
    for fingerprint in ("", "0" * 16):
        code, out, report, shown = cli.restore(tampered, fingerprint)
        assert code == 1, out
        assert "Refusé — rien n'a été écrit : sauvegarde altérée" in out
        assert shown == ""
    assert len(fake.reads) == reads
    assert len(fake.commits) == commits and _store_snapshot(fake) == before


def test_the_restoration_replays_the_day_guards_backwards(fake, bench, cli):
    cli.migrate(bench["lines"])
    (backup,) = cli.backups.iterdir()
    # A card reconciliation ending on the 18th, opened after the migration:
    # moving the leg back from the 19th would cross it.
    _ok(al.create_reconciliation(bench["card"]["id"], _d(8, 18), 0))
    code, out, report, _fp = cli.restore(backup)
    assert code == 1, out
    assert "date_conciliation" in report


# ══════════════════════════════════════════════════════════════════════
# 9. Paths, the CSV's form, group E
# ══════════════════════════════════════════════════════════════════════


def test_a_path_inside_the_repository_is_refused_before_anything_is_created(fake, bench, cli):
    csv_path = cli.csv(bench["lines"])
    inside = rla.REPO / "athena" / "tests" / "rapport-interdit.md"
    code, out = cli.run(csv_path, "--rapport", inside)
    assert code == 1 and "dans le dépôt" in out
    assert not inside.exists()
    sneaky = rla.REPO / "athena" / ".." / "rapport-interdit.md"
    code, out = cli.run(csv_path, "--rapport", sneaky)
    assert code == 1 and "dans le dépôt" in out
    assert not (rla.REPO / "rapport-interdit.md").exists()
    report = cli.report()
    code, out = cli.run(csv_path, "--rapport", report, "--sauvegarde", rla.REPO / "athena",
                        "--appliquer", "0" * 16)
    assert code == 1 and "dans le dépôt" in out
    assert not report.exists()


@pytest.mark.skipif(os.name != "nt", reason="casse et noms courts 8.3 : Windows seulement")
def test_a_repository_path_in_another_case_or_short_name_is_refused(fake, bench, cli):
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(rla.REPO), buffer, 1024)
    csv_path = cli.csv(bench["lines"])
    for spelled in (pathlib.Path(str(rla.REPO).upper()), pathlib.Path(buffer.value)):
        target = spelled / "rapport-interdit.md"
        code, out = cli.run(csv_path, "--rapport", target)
        assert code == 1 and "dans le dépôt" in out, spelled
        assert not target.exists()


def test_group_E_stays_untouched_and_is_reported_verbatim(fake, bench, cli):
    # Excluded lines are set aside BEFORE any judgment: not even their form.
    rows = bench["lines"] + [dict(bench["lines"][-1], ligne="11", entry_id="pas-un-uuid",
                                  montant_cents="n/a")]
    stored = fake.peek(_tx(bench["e"]["e"]))
    csv_path = cli.csv(rows)
    code, out, report, _fp = cli.dry(csv_path)
    assert code == 0, out
    section = report.split("## Lignes exclues (groupe E)")[1].split("## ")[0]
    raw = csv_path.read_text(encoding="utf-8").splitlines()
    for physical in raw[-2:]:
        assert physical in section
    cli.migrate(rows)
    assert fake.peek(_tx(bench["e"]["e"])) == stored


@pytest.mark.parametrize("key, spelled, said", [
    ("b1", "tel quel", "ligne 1 : l'écriture {id} est exclue à la ligne 11"),
    ("b1", "majuscules et espaces", "ligne 1 : l'écriture {id} est exclue à la ligne 11"),
    ("volet", "tel quel", "ligne 9 (volet de carte) : l'écriture {id} est exclue à la ligne 11"),
])
def test_an_excluded_entry_is_never_written(fake, bench, cli, key, spelled, said):
    """An « exclu » line is set aside unjudged, but its entry_id is recorded:
    an « appliquer » line naming the same entry, or a card payment whose
    card leg it is, would write what the lawyer excluded — the whole run is
    refused and nothing is written."""
    entry_id = bench["e"][key]["id"]
    cell = entry_id if spelled == "tel quel" else f" {entry_id.upper()} "
    rows = bench["lines"] + [dict(bench["lines"][-1], ligne="11", entry_id=cell)]
    csv_path = cli.csv(rows)
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report, _fp = cli.dry(csv_path)
    assert code == 1, out
    assert f"Refusé — rien n'a été écrit : {said.format(id=entry_id)}" in out
    assert "Refusé — rien n'a été écrit" in report
    code, out, _report = cli.apply(csv_path, "0" * 16)
    assert code == 1 and said.format(id=entry_id) in out, out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before
    assert not list(cli.backups.iterdir())


def test_a_stray_quote_refuses_the_whole_run(fake, bench, cli):
    """Read leniently, '"Banque"X' would be REPAIRED into « BanqueX » and
    written; read strictly, it is an error of form that refuses everything."""
    csv_path = cli.csv(_lines(bench, l8={"counterparty_cible": "@@CIBLE@@"}))
    csv_path.write_bytes(csv_path.read_bytes().replace(b"@@CIBLE@@", b'"Banque"X'))
    before, commits = _store_snapshot(fake), len(fake.commits)
    code, out, report, _fp = cli.dry(csv_path)
    assert code == 1, out
    assert "Refusé — rien n'a été écrit : CSV mal formé" in out
    assert "Refusé — rien n'a été écrit" in report
    code, out, _report = cli.apply(csv_path, "0" * 16)
    assert code == 1 and "CSV mal formé" in out, out
    assert len(fake.commits) == commits and _store_snapshot(fake) == before
    assert fake.peek(_tx(bench["e"]["c"]))["counterparty"] == "Banqe Exemple"


@pytest.mark.parametrize("change, said", [
    ({"l1": {"statut": "peut-être"}}, "statut « peut-être »"),
    ({"l1": {"entry_id": "pas-un-uuid"}}, "un identifiant (UUID) est attendu"),
    ({"l1": {"kind_cible": rla.NONE_MARK}}, "nature « (aucune) » inconnue"),
    ({"l8": {"counterparty_cible": "<b>Banque</b>"}}, "serait altéré"),
    ({"l8": {"counterparty_cible": "Banque "}}, "espaces en tête ou en fin"),
    ({"l7": {"category_cible": "inconnue"}}, "catégorie « inconnue » inconnue"),
    ({"l2": {"date_cible": "2026-13-01"}}, "une date AAAA-MM-JJ est attendue"),
    ({"l7": {"dossier_id_cible": ""}}, "vont ensemble"),
    ({"l8": {"counterparty_cible": ""}}, "aucune cible"),
    ({"l1": {"kind_cible": "dépense"}}, "la cible est la valeur actuelle"),
    ({"l2": {"ligne": "1"}}, "numéro de ligne en double"),
    ({"l8": {"counterparty_actuel": ""}}, "valeur requise"),
])
def test_an_error_of_form_refuses_the_whole_run(fake, bench, cli, change, said):
    csv_path = cli.csv(_lines(bench, **change))
    commits = len(fake.commits)
    code, out, report, _fp = cli.dry(csv_path)
    assert code == 1, out
    assert said in out, out
    assert "Refusé — rien n'a été écrit" in report
    assert len(fake.commits) == commits


def test_an_unexpected_header_refuses_the_whole_run(fake, bench, cli):
    csv_path = cli.csv(bench["lines"])
    text = csv_path.read_text(encoding="utf-8").replace("kind_cible", "nature_cible", 1)
    csv_path.write_text(text, encoding="utf-8")
    code, out, _report, _fp = cli.dry(csv_path)
    assert code == 1 and "en-tête du CSV inattendu" in out


def test_near_identical_spellings_are_observed_and_left_alone(fake, bench, cli):
    rows = _lines(bench, l1={"counterparty_cible": "Fonds de réserves du cabinet"},
                  l8={"counterparty_cible": "Fonds de réserve du cabinet"})
    csv_path = cli.csv(rows)
    report = cli.dry(csv_path)[2]
    assert ("Graphies voisines d'une contrepartie : « Fonds de réserve du cabinet » "
            "(ligne(s) 8) et « Fonds de réserves du cabinet » (ligne(s) 1) — le script n'y "
            "touche pas.") in report
    cli.migrate(rows)
    assert fake.peek(_tx(bench["e"]["b1"]))["counterparty"] == "Fonds de réserves du cabinet"


def test_a_utf8_bom_is_accepted(fake, bench, cli):
    csv_path = cli.csv(bench["lines"])
    csv_path.write_bytes(b"\xef\xbb\xbf" + csv_path.read_bytes())
    assert cli.dry(csv_path)[0] == 0
