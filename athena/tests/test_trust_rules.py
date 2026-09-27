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
