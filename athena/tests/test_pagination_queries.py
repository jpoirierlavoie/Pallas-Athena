"""La couche requête de la pagination : le compte, le saut, et leurs refus.

L'invariant central de ce lot n'est pas une optimisation, c'est une garantie
d'index : **le compte et la lecture de page passent par LE MÊME constructeur de
requête**, `order_by` compris. C'est ce qui fait que « aucun index nouveau »
est une propriété du CODE et non un commentaire — et c'est ce qui rend une
dérive de l'ordonnancement détectable ici plutôt qu'en production, où elle
répondrait FAILED_PRECONDITION, serait avalée en liste vide, et afficherait un
journal de facturation vide avec des totaux à 0,00 $.

Mesuré contre la production le 2026-09-07 : un COUNT sans `order_by` sur
`dossier_id ==` plus une plage de `date` répond « the query requires an
index », alors que la forme ordonnée passe sur les six collections.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import dossier as m_dossier
    from models import expense as m_expense
    from models import invoice as m_invoice
    from models import partie as m_partie
    from models import time_entry as m_time
    from models import trust as m_trust

from pagination import PAGE_SIZE  # noqa: E402


# ── Un faux objet-requête qui journalise ce qu'on lui applique ─────────────

class _Agg:
    def __init__(self, alias, value):
        self.alias, self.value = alias, value


class _Q:
    """Enregistre les étages appliqués, dans l'ordre."""

    def __init__(self, journal, total=7):
        self.journal, self.total = journal, total

    def _etage(self, nom, *a):
        self.journal.append((nom, *a))
        return self

    def offset(self, n):
        return self._etage("offset", n)

    def limit(self, n):
        return self._etage("limit", n)

    def start_after(self, d):
        return self._etage("start_after", tuple(sorted(d)))

    def count(self, alias="n"):
        return self._etage("count", alias)

    def stream(self):
        return iter(())

    def get(self):
        return [[_Agg("n", self.total)]]


# (module, nom du constructeur, page_fn, count_fn, kwargs du filtre)
CAS = [
    (m_dossier, "_page_query", "list_dossiers_page", "count_dossiers_page",
     {"status_filter": "actif"}),
    (m_partie, "_page_query", "list_parties_page", "count_parties_page",
     {"role_filter": "client"}),
    (m_invoice, "_page_query", "list_invoices_page", "count_invoices_page",
     {"status_filter": "envoyée"}),
    (m_time, "_filtered_query", "list_time_entries_page",
     "count_time_entries_page", {"billable_filter": "billable"}),
    (m_expense, "_filtered_query", "list_expenses_page",
     "count_expenses_page", {"billable_filter": "non_facture"}),
]

IDS = [c[2] for c in CAS]


@pytest.mark.parametrize("mod,builder,page_fn,count_fn,filtres", CAS, ids=IDS)
def test_the_count_and_the_page_read_share_one_query_builder(
    mod, builder, page_fn, count_fn, filtres, monkeypatch
):
    """L'épingle qui rend « aucun index nouveau » vraie par construction."""
    vus = []

    def recorder(*a, **k):
        vus.append((a, k))
        return _Q([])

    monkeypatch.setattr(mod, builder, recorder)
    getattr(mod, page_fn)(**filtres)
    getattr(mod, count_fn)(**filtres)
    assert len(vus) == 2, vus
    # Mêmes arguments, donc même requête, donc même index. Une divergence
    # ici est exactement la dérive que la production punit en silence.
    assert vus[0] == vus[1], vus


@pytest.mark.parametrize("mod,builder,page_fn,count_fn,filtres", CAS, ids=IDS)
def test_the_count_aggregates_with_the_order_by_still_applied(
    mod, builder, page_fn, count_fn, filtres, monkeypatch
):
    """Le COUNT s'applique à la requête ORDONNÉE, jamais aux seuls filtres."""
    journal = []
    monkeypatch.setattr(mod, builder, lambda *a, **k: _Q(journal))
    assert getattr(mod, count_fn)(**filtres) == 7
    # Aucun étage n'a retiré l'ordonnancement, et le compte est la seule
    # chose appliquée : pas de limit, pas de start_after.
    assert journal == [("count", "n")], journal


@pytest.mark.parametrize("mod,builder,page_fn,count_fn,filtres", CAS, ids=IDS)
def test_count_returns_none_on_failure_never_zero(
    mod, builder, page_fn, count_fn, filtres, monkeypatch
):
    """0 se lirait « Page 7 / 0 » : une affirmation fausse et assurée."""
    def boom(*a, **k):
        raise RuntimeError("index absent")

    monkeypatch.setattr(mod, builder, boom)
    assert getattr(mod, count_fn)(**filtres) is None


@pytest.mark.parametrize("mod,builder,page_fn,count_fn,filtres", CAS, ids=IDS)
def test_offset_zero_emits_the_query_we_ship_today(
    mod, builder, page_fn, count_fn, filtres, monkeypatch
):
    """Le défaut doit être OCTET-IDENTIQUE à l'existant : ni offset, ni saut."""
    journal = []
    monkeypatch.setattr(mod, builder, lambda *a, **k: _Q(journal))
    getattr(mod, page_fn)(**filtres)
    assert journal == [("limit", PAGE_SIZE + 1)], journal


@pytest.mark.parametrize("mod,builder,page_fn,count_fn,filtres", CAS, ids=IDS)
def test_list_at_page_applies_the_expected_offset(
    mod, builder, page_fn, count_fn, filtres, monkeypatch
):
    """Page 3 = sauter 2 pages, puis lire limit+1 pour savoir s'il en reste."""
    journal = []
    monkeypatch.setattr(mod, builder, lambda *a, **k: _Q(journal))
    getattr(mod, page_fn)(offset=2 * PAGE_SIZE, **filtres)
    assert journal == [("offset", 2 * PAGE_SIZE), ("limit", PAGE_SIZE + 1)], journal


@pytest.mark.parametrize("mod,builder,page_fn,count_fn,filtres", CAS, ids=IDS)
def test_offset_and_cursor_are_mutually_exclusive_and_the_refusal_ESCAPES(
    mod, builder, page_fn, count_fn, filtres, monkeypatch
):
    """La garde vit AVANT le try, sinon elle serait avalée en ([], None).

    Nommer deux fois la position est une erreur de programmation ; la rendre
    silencieuse afficherait une liste vide sans explication nulle part.
    """
    monkeypatch.setattr(mod, builder, lambda *a, **k: _Q([]))
    with pytest.raises(ValueError):
        getattr(mod, page_fn)(offset=30, cursor="Wzk5XQ", **filtres)


# ── Fidéicommis : l'asymétrie assumée ─────────────────────────────────────

def test_trust_count_fails_open_while_the_page_read_fails_closed(monkeypatch):
    """Un REGISTRE illisible ne doit jamais se lire « aucune écriture ».

    La lecture de page propage donc ; le compteur, lui, ne décide de rien —
    le perdre ne coûte que le contrôle « Fin ».
    """
    def boom(*a, **k):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(m_trust, "_journal_query", boom)
    assert m_trust.count_journal_page("acc") is None       # ouvert
    with pytest.raises(RuntimeError):                       # fermé
        m_trust.list_transactions_page("acc")


def test_trust_shares_its_builder_and_offsets_like_the_others(monkeypatch):
    vus, journal = [], []

    def recorder(*a, **k):
        vus.append((a, k))
        return _Q(journal)

    monkeypatch.setattr(m_trust, "_journal_query", recorder)
    m_trust.list_transactions_page("acc", offset=3 * PAGE_SIZE)
    m_trust.count_journal_page("acc")
    assert vus[0] == vus[1] == (("acc",), {})
    assert journal == [("offset", 3 * PAGE_SIZE), ("limit", PAGE_SIZE + 1),
                       ("count", "n")], journal


def test_trust_refuses_a_cursor_and_an_offset_together(monkeypatch):
    monkeypatch.setattr(m_trust, "_journal_query", lambda *a, **k: _Q([]))
    with pytest.raises(ValueError):
        m_trust.list_transactions_page("acc", cursor="Wzk5XQ", offset=30)


# ── Deux balayages de source ──────────────────────────────────────────────

def test_no_paged_query_is_routed_through_pipelines():
    """Les Pipelines Firestore appliquent `limit` AVANT `offset`.

    Une requête paginée qui passerait par là rendrait zéro ligne — sans
    erreur, sans indice.
    """
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    fautifs = [
        f.name for f in (racine / "models").glob("*.py")
        if ".pipeline(" in f.read_text(encoding="utf-8")
    ]
    assert not fautifs, fautifs


def test_offset_is_never_pushed_into_the_mcp_handlers():
    """`mcp/handlers.py` filtre en Python une lecture matérialisée bornée.

    Un `offset` y compterait les documents DU SERVEUR, pas les lignes
    affichées : mauvaise page, aucune erreur. La discipline curseur des
    gestionnaires reste la leur.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "mcp" / "handlers.py")
    assert ".offset(" not in src.read_text(encoding="utf-8")
