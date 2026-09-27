"""Les règles du registre en fidéicommis que le MODÈLE porte (lot 0b, B5).

Chaque règle vit dans ``models/trust.py`` — là où le web, et demain le
connecteur, passent tous deux — et chaque test ci-dessous échoue sur le code
antérieur (vérifié en le rétablissant, commit par commit) :

1. **L'horloge de Montréal partout.** Une contre-passation, les deux volets
   d'un virement, le contrôle « compensation future » et la fin de période
   d'une conciliation lisaient la date UTC — celle de DEMAIN dès 20 h (HAE).
   Une contre-passation du soir tombait donc au lendemain, et la garde
   d'antidatage refusait ensuite toute écriture saisie le même soir avec la
   date du jour. La création refuse aussi, désormais, une date FUTURE.
2. **Le plancher de conciliation sur TOUTE écriture** (D14). Une écriture
   créée, une compensation datée, une contre-passation ou un virement portés
   au plus tard à la fin de période d'une conciliation complétée changent le
   solde aux livres à cette date ou ses ensembles de résurrection : la
   conciliation close cessait, en silence, de se prouver. Le plancher se lit
   DANS la transaction et le refus nomme la conciliation.
3. **Art. 57 / 72 et art. 58** (RLRQ c. B-1, r. 5, vérifiés le 2026-09-25).
   Aucun retrait en espèces, sauf le remboursement en espèces de tout ou
   partie d'une somme de 7 500 $ ou plus REÇUE en espèces — vérifié contre
   la recette citée (``cash_receipt_id``), le seuil portant sur la somme
   reçue et non sur le remboursement. Un paiement d'honoraires ne sort que
   par chèque ou par virement.
4. **Pas de paiement d'honoraires sur une facture à provision** (D14). Son
   solde dû est déjà net de la provision : un paiement tiré du fidéicommis
   la compterait deux fois.
5. **Un virement entre deux clients d'un même dossier** débite enfin la
   source : les deux côtés partagent un seul document et une seule mise à
   jour.
6. **Un virement à deux volets se contre-passe en entier**, dans une seule
   transaction, les deux corrections nées compensées ; un volet isolé hérité
   reste contre-passable seul. **Une correction ne se contre-passe jamais.**

Le banc est le faux Firestore partagé (``tests/_fake_firestore.py``) : le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais ; on relit ce qui est STOCKÉ.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import trust
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model a
    route starts to read tomorrow cannot reach the mocked client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


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


def _evening(monkeypatch, iso: str = "2026-09-26T01:30:00+00:00") -> None:
    """Freeze the ONE clock read (``utils.deadlines.today_mtl``) at 21:30 HAE
    on 25 September — the UTC date is already the 26th. Every module that
    reads Montréal's calendar goes through that function, so this freezes
    them all; the old code, which read ``datetime.now(timezone.utc)`` in its
    own module, keeps the REAL clock and lands on a different day."""
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


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


def _funded(amount: int = 100000, day: int = 2, client=("dos1", "c1")) -> dict:
    """A cleared receipt: funds the overdraft control will release."""
    r = _create(amount=amount, date=_d(2026, 9, day),
                dossier_id=client[0], client_id=client[1])
    _, errs = trust.clear_transaction(r["id"], _d(2026, 9, day))
    assert errs == []
    return r


def _stored_date(fake, tx_id: str, field: str = "date"):
    return fake.peek(f"trust_transactions/{tx_id}")[field]


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (trust_routes.trust_bp, invoices_routes.invoices_bp,
               admin_ledger_routes.admin_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _erreur(resp) -> str:
    return parse_qs(urlparse(resp.location).query)["erreur"][0]


# ══════════════════════════════════════════════════════════════════════
# 1. L'horloge de Montréal — la bande du soir
# ══════════════════════════════════════════════════════════════════════


def test_une_contre_passation_du_soir_est_datee_du_jour_a_montreal(fake, monkeypatch):
    """21 h 30 le 25 : la contre-passation porte le 25, et l'écriture que
    l'avocate saisit ensuite avec la date du jour passe la garde
    d'antidatage. (Avant : la contre-passation portait la date UTC — le 26,
    ou le jour réel de l'exécution — et l'écriture suivante était refusée
    « antérieure à la dernière écriture ».)"""
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 25))
    rev, errs = trust.reverse_transaction(r["id"], "erreur de saisie")
    assert errs == []
    assert _stored_date(fake, rev["id"]) == _d(2026, 9, 25)

    same_evening, errs = trust.create_transaction(_entry(date=_d(2026, 9, 25)))
    assert errs == [] and same_evening is not None


def test_la_creation_refuse_une_date_future_au_calendrier_de_montreal(fake, monkeypatch):
    """Le 26 est déjà la date UTC à 21 h 30 le 25 — mais c'est DEMAIN à
    Montréal : refusé. Une date future bloquait, par la garde d'antidatage,
    toute écriture ordinaire jusqu'à ce qu'elle passe."""
    _evening(monkeypatch)
    before = fake.peek("trust_accounts/acc1")
    _, errs = trust.create_transaction(_entry(date=_d(2026, 9, 26)))
    assert errs == [trust._ABORT_MESSAGES["date_future"]]
    assert fake.peek_collection("trust_transactions") == {}
    assert fake.peek("trust_accounts/acc1") == before
    assert trust.create_transaction(_entry(date=_d(2026, 9, 25)))[1] == []


def test_les_volets_d_un_virement_du_soir_sont_dates_du_jour(fake, monkeypatch):
    _evening(monkeypatch)
    _funded(day=20)
    leg, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos2", "c2", 40000, "instruction", "virement", ""
    )
    assert errs == []
    for tx_id in (leg["id"], leg["related_transaction_id"]):
        assert _stored_date(fake, tx_id) == _d(2026, 9, 25)
        assert _stored_date(fake, tx_id, "cleared_date") == _d(2026, 9, 25)


def test_la_compensation_future_se_juge_au_calendrier_de_montreal(fake, monkeypatch):
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 24))
    _, errs = trust.clear_transaction(r["id"], _d(2026, 9, 26))
    assert errs == [trust._ABORT_MESSAGES["compensation_future"]]
    assert fake.peek(f"trust_transactions/{r['id']}")["status"] == "en_circulation"
    cleared, errs = trust.clear_transaction(r["id"], _d(2026, 9, 25))
    assert errs == [] and cleared["status"] == "compensée"


def test_la_fin_de_periode_se_juge_au_calendrier_de_montreal(fake, monkeypatch):
    _evening(monkeypatch)
    _, errs = trust.create_reconciliation("acc1", _d(2026, 9, 26), 0)
    assert errs == [trust._ABORT_MESSAGES["conciliation_période_future"]]
    rec, errs = trust.create_reconciliation("acc1", _d(2026, 9, 25), 0)
    assert errs == [] and rec is not None


def test_les_formulaires_proposent_la_date_de_montreal(fake, monkeypatch):
    """La date par défaut des formulaires (écriture, compensation,
    conciliation) — une valeur UTC aurait fait rebondir le formulaire sur
    son propre défaut chaque soir."""
    _evening(monkeypatch)
    assert trust_routes._labels()["today"] == "2026-09-25"


def test_une_compensation_sans_date_prend_le_jour_de_montreal(fake, client, monkeypatch):
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 24))
    resp = client.post(f"/fideicommis/{r['id']}/compenser", data={"cleared_date": ""})
    assert resp.status_code == 302
    assert "erreur" not in resp.location
    assert _stored_date(fake, r["id"], "cleared_date") == _d(2026, 9, 25)


def test_un_refus_de_compensation_n_est_plus_perdu_en_silence(fake, client, monkeypatch):
    """Avant : la route jetait le refus et redirigeait comme si de rien
    n'était — la fiche revenait inchangée, rien ne disait que l'écriture
    n'avait pas été compensée."""
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 24))
    resp = client.post(f"/fideicommis/{r['id']}/compenser",
                       data={"cleared_date": "2026-09-26"})
    assert resp.status_code == 302
    assert _erreur(resp) == trust._ABORT_MESSAGES["compensation_future"]
    page = client.get(resp.location).get_data(as_text=True)
    assert "La date de compensation ne peut être dans le futur." in page


def test_le_renvoi_fusionne_la_requete_au_lieu_de_l_ajouter(fake, client, monkeypatch):
    """Un return_to qui porte déjà une requête (un journal filtré) garde ses
    paramètres ; l'erreur s'y fusionne au lieu d'un second « ? »."""
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 24))
    resp = client.post(f"/fideicommis/{r['id']}/compenser", data={
        "cleared_date": "2026-09-26",
        "return_to": "/fideicommis/?account_id=acc1&erreur=vieille",
    })
    parsed = urlparse(resp.location)
    assert parsed.path == "/fideicommis/"
    query = parse_qs(parsed.query)
    assert query["account_id"] == ["acc1"]
    assert query["erreur"] == [trust._ABORT_MESSAGES["compensation_future"]]


# ══════════════════════════════════════════════════════════════════════
# 2. Le plancher de conciliation — sur toute écriture
# ══════════════════════════════════════════════════════════════════════


def _completed(fake, period_end: datetime, rec_id: str = "rec1") -> None:
    """A COMPLETED reconciliation of acc1 — the lock floor."""
    fake.seed(f"trust_reconciliations/{rec_id}", {
        "id": rec_id, "account_id": "acc1", "period_end": period_end,
        "status": "complétée", "statement_balance": 0, "variance": 0,
        "cleared_transaction_ids": [],
    })


def test_une_ecriture_datee_dans_une_periode_conciliee_est_refusee(fake, monkeypatch):
    _evening(monkeypatch)
    _completed(fake, _d(2026, 8, 31))
    _completed(fake, _d(2026, 7, 31), rec_id="rec0")
    fake.seed("trust_reconciliations/draft", {
        "id": "draft", "account_id": "acc1", "period_end": _d(2026, 9, 20),
        "status": "brouillon",
    })
    before = fake.peek("trust_accounts/acc1")
    for day in (_d(2026, 8, 31), _d(2026, 8, 15)):
        _, errs = trust.create_transaction(_entry(date=day))
        assert len(errs) == 1
        assert "conciliation complétée au 2026-08-31" in errs[0]
    assert fake.peek("trust_accounts/acc1") == before
    assert fake.peek_collection("trust_transactions") == {}
    assert fake.peek("counters/trust-acc1") is None
    # The day after the floor is open — a brouillon is not a lock.
    assert trust.create_transaction(_entry(date=_d(2026, 9, 1)))[1] == []


def test_le_plancher_se_lit_dans_la_transaction(fake, monkeypatch):
    """Une conciliation complétée PENDANT l'écriture fait avorter puis
    rejouer la transaction, qui relit le plancher et refuse : la lecture
    appartient à l'ensemble lu de la transaction, pas à une pré-lecture."""
    _evening(monkeypatch)
    fired = []

    def _reconciliation_completes(info):
        if not fired and any(p.startswith("trust_transactions/") for _k, p in info.ops):
            fired.append(True)
            fake.external_write("trust_reconciliations/late", {
                "id": "late", "account_id": "acc1", "period_end": _d(2026, 9, 10),
                "status": "complétée",
            })

    remove = fake.add_commit_hook(_reconciliation_completes)
    try:
        _, errs = trust.create_transaction(_entry(date=_d(2026, 9, 5)))
    finally:
        remove()
    assert fired
    assert len(errs) == 1 and "2026-09-10" in errs[0]
    assert fake.peek_collection("trust_transactions") == {}
    floor_reads = [r for r in fake.reads
                   if r.rpc == "run_query"
                   and any(p.startswith("trust_reconciliations/") for p in r.paths)]
    assert floor_reads and all(r.transactional for r in floor_reads)


def test_une_compensation_datee_dans_une_periode_conciliee_est_refusee(fake, client, monkeypatch):
    """Elle sortirait l'écriture de l'ensemble de résurrection de la
    période close — la conciliation cesserait de se prouver."""
    _evening(monkeypatch)
    r = _create(date=_d(2026, 8, 20))
    _completed(fake, _d(2026, 8, 31))
    before = fake.peek(f"trust_transactions/{r['id']}")
    _, errs = trust.clear_transaction(r["id"], _d(2026, 8, 31))
    assert len(errs) == 1
    assert "compensation tombe dans une période déjà conciliée" in errs[0]
    assert "2026-08-31" in errs[0]
    assert fake.peek(f"trust_transactions/{r['id']}") == before

    count, failed = trust.clear_transactions_bulk([r["id"]], _d(2026, 8, 30))
    assert (count, failed) == (0, [r["id"]])

    resp = client.post(f"/fideicommis/{r['id']}/compenser",
                       data={"cleared_date": "2026-08-25"})
    assert "2026-08-31" in _erreur(resp)
    page = client.get(resp.location).get_data(as_text=True)
    assert "conciliation complétée au 2026-08-31" in page

    cleared, errs = trust.clear_transaction(r["id"], _d(2026, 9, 2))
    assert errs == [] and cleared["status"] == "compensée"


def test_la_compensation_en_lot_dit_pourquoi(fake, client, monkeypatch):
    """La route latente /compenser-lot ne perd plus son refus non plus."""
    _evening(monkeypatch)
    r = _create(date=_d(2026, 8, 20))
    _completed(fake, _d(2026, 8, 31))
    resp = client.post("/fideicommis/compenser-lot",
                       data={"tx_ids": [r["id"]], "cleared_date": "2026-08-31"})
    assert "2026-08-31" in _erreur(resp)
    page = client.get(resp.location).get_data(as_text=True)
    assert "réécrirait une preuve" in page


def test_le_jour_meme_du_plancher_le_refus_de_creation_dit_demain(fake, monkeypatch):
    """Une conciliation complétée le jour même de sa fin de période : le
    message ordinaire conseillait « une date postérieure au » plancher —
    c'est-à-dire demain, que la garde des dates futures refuse aussitôt. Un
    refus ne prescrit jamais ce que le modèle refuse ensuite (revue, lot 0b)."""
    _evening(monkeypatch)
    _completed(fake, _d(2026, 9, 25))
    _, errs = trust.create_transaction(_entry(date=_d(2026, 9, 25)))
    assert errs == [trust._abort_message("période_verrouillée_jour", "2026-09-25")]
    assert "demain" in errs[0] and "postérieure" not in errs[0]
    # What the ordinary message would have advised is refused today:
    assert trust.create_transaction(_entry(date=_d(2026, 9, 26)))[1] == [
        trust._ABORT_MESSAGES["date_future"]
    ]
    # A floor in the PAST keeps the ordinary advice (a later date exists).
    _completed(fake, _d(2026, 8, 31), rec_id="rec0")
    fake.external_delete("trust_reconciliations/rec1")
    _, errs = trust.create_transaction(_entry(date=_d(2026, 8, 20)))
    assert errs == [trust._abort_message("période_verrouillée", "2026-08-31")]
    assert fake.peek_collection("trust_transactions") == {}


def test_une_contre_passation_refusee_quand_la_conciliation_couvre_aujourd_hui(fake, monkeypatch):
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 20))
    _completed(fake, _d(2026, 9, 25))
    before = fake.peek(f"trust_transactions/{r['id']}")
    _, errs = trust.reverse_transaction(r["id"], "erreur")
    assert len(errs) == 1
    assert "Réessayez demain" in errs[0] and "2026-09-25" in errs[0]
    assert fake.peek(f"trust_transactions/{r['id']}") == before
    assert len(fake.peek_collection("trust_transactions")) == 1


def test_une_contre_passation_passe_au_dessus_du_plancher(fake, monkeypatch):
    _evening(monkeypatch)
    r = _create(date=_d(2026, 9, 20))
    _completed(fake, _d(2026, 9, 24))
    rev, errs = trust.reverse_transaction(r["id"], "erreur")
    assert errs == [] and rev["reverses_id"] == r["id"]


def test_un_virement_refuse_quand_la_conciliation_couvre_aujourd_hui(fake, client, monkeypatch):
    _evening(monkeypatch)
    _funded(day=20)
    _completed(fake, _d(2026, 9, 25))
    _, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos2", "c2", 40000, "instruction", "virement", ""
    )
    assert len(errs) == 1 and "un virement, toujours daté du jour" in errs[0]
    assert len(fake.peek_collection("trust_transactions")) == 1

    resp = client.post("/fideicommis/virement", data={
        "account_id": "acc1", "from_dossier_id": "dos1", "from_client_id": "c1",
        "to_dossier_id": "dos2", "to_client_id": "c2", "amount": "400,00",
        "method": "virement", "description": "instruction",
    })
    assert resp.status_code == 400
    assert "conciliation au 2026-09-25" in resp.get_data(as_text=True)


def test_le_refus_de_creation_s_affiche_au_formulaire(fake, client, monkeypatch):
    _evening(monkeypatch)
    _completed(fake, _d(2026, 8, 31))
    resp = client.post("/fideicommis/", data={
        "account_id": "acc1", "direction": "recette", "amount": "1 000,00",
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1", "date": "2026-08-15",
    })
    assert resp.status_code == 400
    assert "conciliation complétée au 2026-08-31" in resp.get_data(as_text=True)
    assert fake.peek_collection("trust_transactions") == {}


# ══════════════════════════════════════════════════════════════════════
# 3. Art. 57 / 72 (espèces) et art. 58 (retrait d'honoraires)
# ══════════════════════════════════════════════════════════════════════


def _cash_receipt(amount: int = 800000, day: int = 3, **over) -> dict:
    """A CLEARED cash receipt — the only thing art. 72 lets a cash refund
    repay (and cleared, so the overdraft control releases the funds)."""
    fields = dict(amount=amount, date=_d(2026, 9, day), method="comptant")
    fields.update(over)
    r = _create(**fields)
    _, errs = trust.clear_transaction(r["id"], _d(2026, 9, day))
    assert errs == []
    return r


def _refund(receipt_id, amount: int, day: int = 10, **over):
    fields = dict(direction="déboursé", purpose="remise_client", method="comptant",
                  amount=amount, date=_d(2026, 9, day), cash_receipt_id=receipt_id)
    fields.update(over)
    return trust.create_transaction(_entry(**fields))


@pytest.mark.parametrize("method", ["traite", "dépôt_direct", "comptant"])
def test_un_paiement_d_honoraires_ne_sort_que_par_cheque_ou_virement(fake, monkeypatch, method):
    """Art. 58 : « seulement par chèque tiré à l'ordre de l'avocat ou par
    virement à un compte qui n'est pas un compte en fidéicommis »."""
    _evening(monkeypatch)
    _funded(day=2)
    before = fake.peek("dossiers/dos1")
    _, errs = trust.create_transaction(_entry(
        direction="déboursé", purpose="virement_honoraires", method=method,
        amount=10000, date=_d(2026, 9, 5), invoice_external_ref="P-12",
    ))
    assert errs == [trust._ABORT_MESSAGES["mode_retrait_honoraires"]]
    assert "art. 58" in errs[0]
    assert fake.peek("dossiers/dos1") == before


@pytest.mark.parametrize("method", ["chèque", "virement"])
def test_le_cheque_et_le_virement_restent_permis(fake, monkeypatch, method):
    _evening(monkeypatch)
    _funded(day=2)
    entry, errs = trust.create_transaction(_entry(
        direction="déboursé", purpose="virement_honoraires", method=method,
        amount=10000, date=_d(2026, 9, 5), invoice_external_ref="P-12",
    ))
    assert errs == [] and entry["method"] == method


@pytest.mark.parametrize("purpose", ["déboursé_tiers", "règlement", "autre", "remise_client"])
def test_aucun_retrait_en_especes_sans_la_recette_de_l_article_72(fake, monkeypatch, purpose):
    """Art. 57 : un déboursé en espèces est refusé — y compris une remise au
    client qui ne cite pas la recette en espèces qu'elle rembourse."""
    _evening(monkeypatch)
    _cash_receipt()
    before = fake.peek("dossiers/dos1")
    _, errs = trust.create_transaction(_entry(
        direction="déboursé", purpose=purpose, method="comptant",
        amount=10000, date=_d(2026, 9, 5),
    ))
    assert errs == [trust._ABORT_MESSAGES["retrait_espèces_interdit"]]
    assert "art. 57" in errs[0] and "art. 72" in errs[0]
    assert fake.peek("dossiers/dos1") == before


def test_une_recette_en_especes_reste_permise(fake, monkeypatch):
    """L'art. 57 vise le RETRAIT : recevoir des espèces demeure permis."""
    _evening(monkeypatch)
    r = _create(method="comptant", amount=50000, date=_d(2026, 9, 5))
    assert r["method"] == "comptant" and r["cash_receipt_id"] == ""


def test_le_remboursement_en_especes_de_l_article_72(fake, monkeypatch):
    """Tout ou partie d'une somme de 7 500 $ ou plus reçue en espèces se
    rembourse en espèces — un remboursement PARTIEL compris (le seuil porte
    sur la somme reçue, jamais sur le remboursement)."""
    _evening(monkeypatch)
    receipt = _cash_receipt(amount=800000)
    first, errs = _refund(receipt["id"], 300000)
    assert errs == []
    assert fake.peek(f"trust_transactions/{first['id']}")["cash_receipt_id"] == receipt["id"]
    second, errs = _refund(receipt["id"], 500000, day=11)
    assert errs == []
    # Cumulative refunds may not exceed what the receipt brought in.
    _, errs = _refund(receipt["id"], 1, day=12)
    assert errs == [trust._ABORT_MESSAGES["remboursement_espèces_excède"]]


def test_un_remboursement_contre_passe_ne_compte_plus(fake, monkeypatch):
    _evening(monkeypatch)
    receipt = _cash_receipt(amount=800000)
    first, errs = _refund(receipt["id"], 800000)
    assert errs == []
    _, errs = trust.reverse_transaction(first["id"], "erreur de montant")
    assert errs == []
    # Dated today: the reversal (dated today) closes the register's order.
    again, errs = _refund(receipt["id"], 800000, day=25)
    assert errs == [] and again is not None


@pytest.mark.parametrize("label,over", [
    ("moins de 7 500 $", dict(amount=749999)),
    ("reçue par chèque", dict(method="chèque")),
])
def test_la_recette_citee_doit_etre_des_especes_de_7500_ou_plus(fake, monkeypatch, label, over):
    _evening(monkeypatch)
    receipt = _cash_receipt(**over)
    _, errs = _refund(receipt["id"], 1000)
    assert errs == [trust._ABORT_MESSAGES["recette_espèces_invalide"]], label


def test_la_recette_citee_doit_etre_celle_du_meme_client(fake, monkeypatch):
    _evening(monkeypatch)
    other = _cash_receipt(amount=900000, dossier_id="dos2", client_id="c2")
    _cash_receipt(amount=900000, day=4)  # funds dos1/c1 so only the rule bites
    _, errs = _refund(other["id"], 1000)
    assert errs == [trust._ABORT_MESSAGES["recette_espèces_invalide"]]


def test_une_recette_contre_passee_ne_se_rembourse_pas(fake, monkeypatch):
    _evening(monkeypatch)
    receipt = _cash_receipt(amount=900000)
    _cash_receipt(amount=900000, day=4)
    _, errs = trust.reverse_transaction(receipt["id"], "dépôt erroné")
    assert errs == []
    _, errs = _refund(receipt["id"], 1000, day=25)
    assert errs == [trust._ABORT_MESSAGES["recette_espèces_invalide"]]


def test_une_recette_citee_introuvable_est_refusee(fake, monkeypatch):
    _evening(monkeypatch)
    _cash_receipt()
    _, errs = _refund("nope", 1000)
    assert errs == [trust._ABORT_MESSAGES["recette_espèces_introuvable"]]


def test_deux_remboursements_concurrents_ne_depassent_pas_la_recette(fake, monkeypatch):
    """Les remboursements déjà inscrits se lisent DANS la transaction : un
    remboursement rival commis pendant l'écriture la fait rejouer, et le
    cumul refuse."""
    _evening(monkeypatch)
    receipt = _cash_receipt(amount=800000)
    fired = []

    def _rival_refund(info):
        if not fired and any(p.startswith("trust_transactions/") for _k, p in info.ops):
            fired.append(True)
            fake.external_write("trust_transactions/rival", {
                "id": "rival", "account_id": "acc1", "direction": "déboursé",
                "method": "comptant", "purpose": "remise_client", "amount": 600000,
                "cash_receipt_id": receipt["id"], "reversed_by_id": None,
                "sequence": 99, "date": _d(2026, 9, 9),
            })

    remove = fake.add_commit_hook(_rival_refund)
    try:
        _, errs = _refund(receipt["id"], 300000)
    finally:
        remove()
    assert fired
    assert errs == [trust._ABORT_MESSAGES["remboursement_espèces_excède"]]


def test_le_champ_cache_de_l_article_72_est_ignore_hors_especes(fake, monkeypatch):
    _evening(monkeypatch)
    receipt = _cash_receipt()
    entry, errs = trust.create_transaction(_entry(
        amount=1000, date=_d(2026, 9, 5), cash_receipt_id=receipt["id"],
    ))
    assert errs == []
    assert fake.peek(f"trust_transactions/{entry['id']}")["cash_receipt_id"] == ""


def _cash_form(**over) -> dict:
    form = {
        "account_id": "acc1", "direction": "déboursé", "amount": "3 000,00",
        "purpose": "remise_client", "method": "comptant", "counterparty": "Jean",
        "dossier_id": "dos1", "client_id": "c1", "date": "2026-09-10",
    }
    form.update(over)
    return form


def test_le_formulaire_resout_le_numero_de_la_recette_en_especes(fake, client, monkeypatch):
    _evening(monkeypatch)
    receipt = _cash_receipt(amount=800000)
    resp = client.post("/fideicommis/", data=_cash_form(
        cash_receipt_sequence=str(receipt["sequence"])))
    assert resp.status_code == 302, resp.get_data(as_text=True)
    refunds = [t for t in fake.peek_collection("trust_transactions").values()
               if t.get("cash_receipt_id") == receipt["id"]]
    assert len(refunds) == 1 and refunds[0]["amount"] == 300000


def test_le_formulaire_dit_chaque_refus_des_especes(fake, client, monkeypatch):
    _evening(monkeypatch)
    _cash_receipt(amount=800000)
    # No receipt cited: art. 57.
    page = client.post("/fideicommis/", data=_cash_form())
    assert page.status_code == 400
    assert "art. 57" in page.get_data(as_text=True)
    # An unknown number: named, never silently dropped.
    page = client.post("/fideicommis/", data=_cash_form(cash_receipt_sequence="42"))
    assert page.status_code == 400
    html = page.get_data(as_text=True)
    assert "Aucune écriture n° 42 dans ce compte." in html
    assert 'value="42"' in html  # the typed number survives the re-render
    page = client.post("/fideicommis/", data=_cash_form(cash_receipt_sequence="douze"))
    assert "doit être un nombre entier" in page.get_data(as_text=True)
    # Art. 58 on a fee payment by cash.
    page = client.post("/fideicommis/", data=_cash_form(
        purpose="virement_honoraires", invoice_external_ref="P-1",
        admin_account_id="ops1"))
    assert page.status_code == 400
    assert "art. 58" in page.get_data(as_text=True)
    assert len(fake.peek_collection("trust_transactions")) == 1


# ══════════════════════════════════════════════════════════════════════
# 4. Aucun paiement d'honoraires sur une facture qui impute une provision
# ══════════════════════════════════════════════════════════════════════


def _invoice(fake, inv_id: str = "inv1", **over) -> None:
    doc = {
        "id": inv_id, "invoice_number": "2026-F040", "dossier_id": "dos1",
        "status": "envoyée", "total": 114975, "retainer_applied": 0,
        "amount_due": 114975, "amount_paid": 0,
    }
    doc.update(over)
    fake.seed(f"invoices/{inv_id}", doc)


def _fee_payment(amount: int = 50000, **over):
    fields = dict(direction="déboursé", purpose="virement_honoraires",
                  method="chèque", amount=amount, date=_d(2026, 9, 5),
                  invoice_id="inv1")
    fields.update(over)
    return trust.create_transaction(_entry(**fields))


def test_un_paiement_d_honoraires_sur_une_facture_a_provision_est_refuse(fake, monkeypatch):
    """Le solde dû de la facture est déjà NET de la provision : la retirer du
    fidéicommis comme paiement d'honoraires la compterait deux fois — en
    silence tant que le dû résiduel couvre le virement (ici : 50 000 ¢ sur
    un dû de 64 975 ¢, sous toutes les autres gardes)."""
    _evening(monkeypatch)
    _funded(day=2)
    _invoice(fake, retainer_applied=50000, amount_due=64975)
    before = fake.peek("dossiers/dos1")
    _, errs = _fee_payment(50000)
    assert errs == [trust._ABORT_MESSAGES["facture_avec_provision"]]
    assert "compterait la provision deux fois" in errs[0]
    assert fake.peek("dossiers/dos1") == before
    assert len(fake.peek_collection("trust_transactions")) == 1


def test_une_facture_sans_provision_reste_payable(fake, monkeypatch):
    _evening(monkeypatch)
    _funded(day=2)
    _invoice(fake)
    entry, errs = _fee_payment(50000)
    assert errs == [] and entry["invoice_id"] == "inv1"


def test_le_selecteur_montre_la_facture_a_provision_sans_l_offrir(fake, client, monkeypatch):
    _evening(monkeypatch)
    _invoice(fake, "inv1", invoice_number="2026-F041")
    _invoice(fake, "inv2", invoice_number="2026-F042",
             retainer_applied=50000, amount_due=64975)
    rows = trust_routes._factures_emises("dos1")
    by_number = {r["invoice_number"]: r for r in rows}
    assert by_number["2026-F041"]["provision"] is False
    assert by_number["2026-F042"]["provision"] is True
    html = client.get("/fideicommis/factures-du-dossier?dossier_id=dos1").get_data(as_text=True)
    assert '<option value="2026-F041"' in html
    assert '<option value="2026-F042"' not in html
    assert "2026-F042 — provision imputée : non payable du fidéicommis" in html


def test_le_formulaire_dit_le_refus_de_la_provision(fake, client, monkeypatch):
    _evening(monkeypatch)
    _funded(day=2)
    _invoice(fake, retainer_applied=50000, amount_due=64975)
    fake.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "account_type": "opérations",
        "status": "actif", "ledger_balance": 0,
    })
    resp = client.post("/fideicommis/", data={
        "account_id": "acc1", "direction": "déboursé", "amount": "500,00",
        "purpose": "virement_honoraires", "method": "chèque", "counterparty": "Me X",
        "dossier_id": "dos1", "client_id": "c1", "date": "2026-09-05",
        "invoice_number": "2026-F040", "admin_account_id": "ops1",
    })
    assert resp.status_code == 400
    assert "compterait la provision deux fois" in resp.get_data(as_text=True)
    assert len(fake.peek_collection("trust_transactions")) == 1
    assert fake.peek_collection("admin_transactions") == {}


# ══════════════════════════════════════════════════════════════════════
# 5. Un virement entre deux clients d'UN MÊME dossier garde ses deux côtés
# ══════════════════════════════════════════════════════════════════════


def _second_client(fake) -> None:
    doc = fake.peek("dossiers/dos1")
    doc["client_ids"] = ["c1", "c3"]
    doc["clients"] = [{"id": "c1", "name": "Jean Tremblay"},
                      {"id": "c3", "name": "Luc Tremblay"}]
    fake.seed("dossiers/dos1", doc)


def test_un_virement_entre_deux_clients_du_meme_dossier_debite_la_source(fake, monkeypatch):
    """Deux mises à jour du MÊME document dans une transaction s'appliquent
    dans l'ordre : la seconde, bâtie sur la même lecture, réécrivait le
    solde de la source inchangé. La destination gagnait, la source ne
    perdait rien — de l'argent apparaissait au registre."""
    _evening(monkeypatch)
    _second_client(fake)
    _funded(amount=100000, day=2)
    _, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos1", "c3", 40000, "instruction", "virement", ""
    )
    assert errs == []
    doc = fake.peek("dossiers/dos1")
    assert doc["trust_balance_by_client"] == {"c1": 60000, "c3": 40000}
    assert doc["trust_cleared_by_client"] == {"c1": 60000, "c3": 40000}
    assert doc["trust_balance"] == 100000
    assert fake.peek("trust_accounts/acc1")["book_balance"] == 100000


# ══════════════════════════════════════════════════════════════════════
# 6. Un virement à deux volets se contre-passe en entier ; une correction
#    jamais
# ══════════════════════════════════════════════════════════════════════


def _transfer(amount: int = 40000, to=("dos2", "c2")) -> tuple[dict, dict]:
    """A funded two-leg transfer dos1/c1 → *to*; returns (source, recipient)."""
    _funded(amount=100000, day=2)
    leg, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", to[0], to[1], amount, "instruction", "virement", ""
    )
    assert errs == []
    return leg, trust.get_transaction(leg["related_transaction_id"])


def _maps(fake, did: str) -> tuple[dict, dict]:
    doc = fake.peek(f"dossiers/{did}")
    return doc["trust_balance_by_client"], doc["trust_cleared_by_client"]


@pytest.mark.parametrize("which", ["source", "recipient"])
def test_contre_passer_un_volet_contre_passe_les_deux(fake, monkeypatch, which):
    """Contre-passer un seul volet bougeait le solde d'un seul client, sans
    mouvement bancaire ni contre-volet. Désormais les deux volets, dans UNE
    transaction ; les deux corrections naissent compensées (les fonds n'ont
    jamais quitté le compte), liées l'une à l'autre."""
    _evening(monkeypatch)
    source, recipient = _transfer()
    account_before = fake.peek("trust_accounts/acc1")
    fake.reset_logs()
    target = source if which == "source" else recipient
    rev, errs = trust.reverse_transaction(target["id"], "virement erroné")
    assert errs == []
    assert rev["reverses_id"] == target["id"]

    corrections = sorted(
        (t for t in fake.peek_collection("trust_transactions").values()
         if t["purpose"] == "correction"),
        key=lambda t: t["sequence"],
    )
    assert [c["reverses_id"] for c in corrections] == [source["id"], recipient["id"]]
    assert [c["direction"] for c in corrections] == ["recette", "déboursé"]
    for c in corrections:
        assert c["status"] == "compensée"
        assert c["date"] == _d(2026, 9, 25) and c["cleared_date"] == _d(2026, 9, 25)
    assert corrections[0]["related_transaction_id"] == corrections[1]["id"]
    assert corrections[1]["related_transaction_id"] == corrections[0]["id"]
    # Exact running balances: the recette reversal lifts the book, the
    # déboursé reversal brings it back.
    assert corrections[0]["balance_after_account"] == 140000
    assert corrections[1]["balance_after_account"] == 100000
    for leg in (source, recipient):
        stored = fake.peek(f"trust_transactions/{leg['id']}")
        assert stored["status"] == "compensée" and stored["reversed_by_id"]

    assert _maps(fake, "dos1") == ({"c1": 100000}, {"c1": 100000})
    assert _maps(fake, "dos2") == ({"c2": 0}, {"c2": 0})
    account = fake.peek("trust_accounts/acc1")
    assert account["book_balance"] == account_before["book_balance"]
    assert account["bank_balance"] == account_before["bank_balance"]
    assert account["etag"] != account_before["etag"]  # the reconciliation sentinel
    assert fake.peek("counters/trust-acc1")["seq"] == 5
    # ONE commit carries both reversals and both originals.
    assert len(fake.commits) == 1
    written = {path for _kind, path in fake.commits[0].ops}
    assert {f"trust_transactions/{c['id']}" for c in corrections} <= written
    assert {f"trust_transactions/{source['id']}",
            f"trust_transactions/{recipient['id']}"} <= written


def test_la_contre_passation_d_un_virement_est_tout_ou_rien(fake, monkeypatch):
    """Une panne au commit n'applique RIEN — ni l'un ni l'autre volet.

    Le commit refusé portait les DEUX corrections et les DEUX volets
    d'origine (revue, lot 0b) : sans cette assertion, le test passait sur le
    code antérieur, qui ne contre-passait qu'un volet — une panne n'y
    appliquait rien non plus."""
    _evening(monkeypatch)
    source, recipient = _transfer()
    before = fake.peek_collection("trust_transactions")
    attempted: list[set] = []

    def _boom(info):
        attempted.append({path for _kind, path in info.ops})
        raise RuntimeError("commit refusé")

    remove = fake.add_commit_hook(_boom)
    try:
        _, errs = trust.reverse_transaction(source["id"], "virement erroné")
    finally:
        remove()
    assert errs
    assert fake.peek_collection("trust_transactions") == before
    assert len(attempted) == 1
    ops = attempted[0]
    assert {f"trust_transactions/{source['id']}",
            f"trust_transactions/{recipient['id']}"} <= ops
    assert sum(1 for p in ops if p.startswith("trust_transactions/")) == 4
    assert {"dossiers/dos1", "dossiers/dos2", "trust_accounts/acc1"} <= ops


def test_le_destinataire_qui_a_depense_les_fonds_ne_se_voit_pas_mis_a_decouvert(fake, monkeypatch):
    _evening(monkeypatch)
    source, recipient = _transfer(amount=40000)
    _, errs = trust.create_transaction(_entry(
        direction="déboursé", purpose="déboursé_tiers", amount=30000,
        dossier_id="dos2", client_id="c2", date=_d(2026, 9, 25),
    ))
    assert errs == []
    before = fake.peek_collection("trust_transactions")
    _, errs = trust.reverse_transaction(source["id"], "virement erroné")
    assert errs == [trust._ABORT_MESSAGES["contre_passation_virement_solde"]]
    assert fake.peek_collection("trust_transactions") == before


def test_un_virement_entre_clients_du_meme_dossier_se_contre_passe(fake, monkeypatch):
    _evening(monkeypatch)
    _second_client(fake)
    source, _recipient = _transfer(to=("dos1", "c3"))
    _, errs = trust.reverse_transaction(source["id"], "mauvais client")
    assert errs == []
    book, cleared = _maps(fake, "dos1")
    assert book == {"c1": 100000, "c3": 0} and cleared == {"c1": 100000, "c3": 0}
    assert fake.peek("dossiers/dos1")["trust_balance"] == 100000


def test_un_volet_isole_herite_reste_contre_passable_seul(fake, monkeypatch):
    """Le formulaire d'écriture offrait l'objet « virement inter-dossiers » :
    ces volets ISOLÉS n'ont pas de contre-volet — refuser leur
    contre-passation les rendrait incorrigibles à jamais."""
    _evening(monkeypatch)
    single = _create(purpose="virement_inter_dossiers", amount=5000, date=_d(2026, 9, 5))
    rev, errs = trust.reverse_transaction(single["id"], "saisie erronée")
    assert errs == []
    assert rev["status"] == "annulée"
    assert len(fake.peek_collection("trust_transactions")) == 2


def test_un_volet_dont_le_partenaire_fut_contre_passe_seul_se_contre_passe_seul(fake, monkeypatch):
    """Une paire à moitié contre-passée avant la règle se complète : le volet
    restant se contre-passe seul."""
    _evening(monkeypatch)
    source, recipient = _transfer()
    stored = fake.peek(f"trust_transactions/{recipient['id']}")
    stored["reversed_by_id"] = "legacy-correction"
    fake.seed(f"trust_transactions/{recipient['id']}", stored)
    rev, errs = trust.reverse_transaction(source["id"], "compléter la paire")
    assert errs == []
    corrections = [t for t in fake.peek_collection("trust_transactions").values()
                   if t["purpose"] == "correction"]
    assert [c["reverses_id"] for c in corrections] == [source["id"]]
    assert rev["status"] == "en_circulation"  # the ordinary compensée algebra


def test_un_volet_sans_son_partenaire_est_refuse(fake, monkeypatch):
    _evening(monkeypatch)
    source, recipient = _transfer()
    fake.external_delete(f"trust_transactions/{recipient['id']}")
    before = fake.peek_collection("trust_transactions")
    _, errs = trust.reverse_transaction(source["id"], "x")
    assert errs == [trust._ABORT_MESSAGES["volet_virement_introuvable"]]
    assert fake.peek_collection("trust_transactions") == before


@pytest.mark.parametrize("cleared_first", [True, False])
def test_une_correction_ne_se_contre_passe_jamais(fake, monkeypatch, cleared_first):
    """Contre-passer une correction ré-appliquait le mouvement d'origine sans
    AUCUNE de ses gardes — pour un paiement d'honoraires contre-passé, les
    honoraires ressortaient du fidéicommis sans facture ni recette."""
    _evening(monkeypatch)
    r = _funded(day=2) if cleared_first else _create(date=_d(2026, 9, 2))
    correction, errs = trust.reverse_transaction(r["id"], "erreur")
    assert errs == []
    before = fake.peek_collection("trust_transactions")
    _, errs = trust.reverse_transaction(correction["id"], "annuler la correction")
    assert errs == [trust._ABORT_MESSAGES["correction_non_contre_passable"]]
    assert fake.peek_collection("trust_transactions") == before
    assert trust.reversal_refusal(correction) == errs[0]


def test_la_page_de_contre_passation_dit_ce_que_le_modele_fera(fake, client, monkeypatch):
    _evening(monkeypatch)
    source, recipient = _transfer()
    page = client.get(f"/fideicommis/{source['id']}/contrepasser").get_data(as_text=True)
    assert "ses deux volets" in page
    resp = client.post(f"/fideicommis/{source['id']}/contrepasser",
                       data={"reason": "virement erroné"})
    assert resp.status_code == 302
    correction_id = resp.location.rsplit("/", 1)[-1]
    correction = fake.peek(f"trust_transactions/{correction_id}")
    assert correction["reverses_id"] == source["id"]

    # A correction: no form, the refusal instead — and no link on its page.
    page = client.get(f"/fideicommis/{correction_id}/contrepasser").get_data(as_text=True)
    assert "Une écriture de correction ne se contre-passe pas" in page
    assert 'name="reason"' not in page
    detail = client.get(f"/fideicommis/{correction_id}").get_data(as_text=True)
    assert f"/fideicommis/{correction_id}/contrepasser" not in detail
    resp = client.post(f"/fideicommis/{correction_id}/contrepasser", data={"reason": "x"})
    assert resp.status_code == 400
    assert "ne se contre-passe pas" in resp.get_data(as_text=True)


def test_le_refus_du_destinataire_s_affiche(fake, client, monkeypatch):
    _evening(monkeypatch)
    source, _recipient = _transfer(amount=40000)
    trust.create_transaction(_entry(
        direction="déboursé", purpose="déboursé_tiers", amount=30000,
        dossier_id="dos2", client_id="c2", date=_d(2026, 9, 25),
    ))
    resp = client.post(f"/fideicommis/{source['id']}/contrepasser", data={"reason": "x"})
    assert resp.status_code == 400
    assert "créerait un découvert" in resp.get_data(as_text=True)


@pytest.mark.parametrize("bad", ["a/b", 12345])
def test_une_reference_de_recette_malformee_est_un_refus_jamais_une_erreur(fake, monkeypatch, bad):
    """Un identifiant contenant « / » construirait une référence invalide, et
    un non-texte faisait lever ``.strip()`` : tous deux finissaient en
    « Erreur lors de la sauvegarde » au lieu d'un refus qui dit pourquoi."""
    _evening(monkeypatch)
    _cash_receipt()
    before = fake.peek_collection("trust_transactions")
    _, errs = _refund(bad, 1000)
    assert errs == [trust._ABORT_MESSAGES["recette_espèces_introuvable"]]
    assert fake.peek_collection("trust_transactions") == before


def test_les_refus_sans_lecture_de_la_contre_passation_et_du_virement_se_journalisent(
    fake, monkeypatch
):
    """OBSERVABILITY.md annonce qu'un refus de contre-passation ou de virement
    se journalise sous ``trust_transaction_refused`` : ses refus SANS lecture
    (motif manquant ; virement vers soi-même, montant ou mode invalide)
    sortaient pourtant sans trace (revue, lot 0b). Codes et identifiants
    seulement — jamais un nom ni un montant."""
    calls: list = []
    monkeypatch.setattr(
        trust, "log_trust_event",
        lambda event, outcome="success", **kw: calls.append((event, outcome, kw)),
    )
    assert trust.reverse_transaction("tx1", "   ")[1] == [trust._ABORT_MESSAGES["motif_requis"]]
    for args in (
        ("acc1", "dos1", "c1", "dos1", "c1", 100, "", "virement", ""),
        ("acc1", "dos1", "c1", "dos2", "c2", 0, "", "virement", ""),
        ("acc1", "dos1", "c1", "dos2", "c2", 100, "", "espèces", ""),
    ):
        assert trust.create_inter_dossier_transfer(*args)[1]
    refused = [(kw.get("operation"), kw.get("reason"))
               for event, outcome, kw in calls
               if event == "trust_transaction_refused" and outcome == "refused"]
    assert refused == [
        ("reverse", "motif_requis"),
        ("transfer", "transfert_identique"),
        ("transfer", "montant_invalide"),
        ("transfer", "mode_invalide"),
    ]
    allowed = {"transaction_id", "account_id", "dossier_id", "reason", "operation"}
    assert all(set(kw) <= allowed for _e, _o, kw in calls)
    assert fake.peek_collection("trust_transactions") == {}


def test_le_formulaire_n_ecrit_jamais_une_valeur_postee_crue_dans_le_x_data(
    fake, client, monkeypatch
):
    """Un refus (400) ré-affiche ce que le formulaire a POSTÉ. L'attribut
    x-data est décodé de ses entités AVANT qu'Alpine l'évalue : une
    apostrophe dans une valeur crue fermait la chaîne JS — formulaire mort
    et, sous 'unsafe-eval', injection d'expression. B5 a passé direction et
    method par |jsattr ; dossierId, clientId et purpose, dans le même bloc,
    restaient crus (revue, lot 0b). On épingle le RENDU, avec la sémantique
    réelle du filtre de main.py."""
    import json as _json

    from markupsafe import Markup

    def _jsattr(value):  # main.py's filter, verbatim semantics
        js = _json.dumps(str(value), ensure_ascii=False)
        return Markup(js.replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;").replace('"', "&quot;"))

    client.application.jinja_env.filters["jsattr"] = _jsattr
    _evening(monkeypatch)
    hostile = "x');alert(1);('"
    resp = client.post("/fideicommis/", data={
        "account_id": "acc1", "direction": "recette", "amount": "10,00",
        "purpose": hostile, "method": "chèque", "counterparty": "Client",
        "dossier_id": "inconnu" + hostile, "client_id": "c1" + hostile,
        "date": "2026-09-20",
    })
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    for key in ("dossierId", "clientId", "purpose", "direction", "method"):
        assert f"{key}: '" not in html, key
        assert f"{key}: &quot;" in html, key
    assert "purpose: &quot;x');alert(1);('&quot;" in html
    assert fake.peek_collection("trust_transactions") == {}


def test_un_numero_de_recette_exotique_est_un_refus_jamais_une_erreur(fake, client, monkeypatch):
    """« ² » passe str.isdigit() mais pas int() : un ValueError non rattrapé,
    donc un 500 sur une faute de frappe. Et un numéro au-delà de l'int64
    atteignait l'encodeur de Firestore, qui lève en production — une ligne
    ERROR et un « Veuillez réessayer » qui ne mène nulle part (revue, lot 0b)."""
    _evening(monkeypatch)
    _cash_receipt(amount=800000)
    page = client.post("/fideicommis/", data=_cash_form(cash_receipt_sequence="²"))
    assert page.status_code == 400
    assert "doit être un nombre entier" in page.get_data(as_text=True)

    def _encoder_refuses(account_id, sequence):  # Firestore's int64 encoder, in prod
        raise ValueError("Value out of range")

    monkeypatch.setattr(trust, "find_transaction_by_sequence", _encoder_refuses)
    page = client.post("/fideicommis/", data=_cash_form(cash_receipt_sequence="9" * 30))
    assert page.status_code == 400
    assert "Aucune écriture ne porte ce numéro dans ce compte." in page.get_data(as_text=True)
    assert len(fake.peek_collection("trust_transactions")) == 1
