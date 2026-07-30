"""Événements de prescription (WP13, PA-G02) — le modèle complet.

Décision utilisateur 2026-07-30 : le connecteur (et le dossier) portent un
registre MANUEL d'événements du C.c.Q. — interruption par demande en
justice (art. 2892/2896), reconnaissance (art. 2898), suspension
(art. 2904 s.), renonciation (art. 2883 s.) — avec une projection dérivée
{statut, date effective} calculée à la LECTURE par derive_prescription,
l'unique couture consommée par le tableau de bord, la pastille de liste et
le MCP (la règle des trois surfaces).

Ce que la décision NE renverse PAS, épinglé ailleurs et re-vérifié ici :
le prescription_date BRUT n'est jamais recalculé
(test_dossier_taxonomy.test_deadline_never_touches_prise_action_date).
La projection vit À CÔTÉ, jamais à la place.

Sémantique juridique fixée par le plan :
- dépôt → statut « interrompue », date effective NULLE : l'art. 2896 fait
  durer l'interruption jusqu'au jugement — calculer une date serait
  l'inventer. La prise_action_date HÉRITÉE se replie en dépôt implicite à
  la lecture (aucune migration).
- reconnaissance/renonciation → un NOUVEAU délai de même durée court de la
  date de l'événement (compute_date_pour_agir — l'arithmétique maison,
  report au jour juridique inclus) ; sans période confirmée → a_verifier.
- suspension → décale l'échéance effective de la durée suspendue, puis au
  jour juridique suivant.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import dossier as dmod

UTC = timezone.utc


def _d(y, m, day):
    return datetime(y, m, day, tzinfo=UTC)


def _doc(**over):
    base = {
        "prescription_type": "3_ans",
        "droit_action_date": _d(2023, 9, 21),
        "prescription_date": _d(2026, 9, 21),
        "prescription_events": [],
        "prise_action_date": None,
    }
    base.update(over)
    return base


# ── Normalisation ────────────────────────────────────────────────────────


def test_normalize_coerces_sorts_and_ids():
    doc = {"prescription_events": [
        {"type": "suspension", "date": "2025-03-01", "end_date": "2025-05-01"},
        {"type": "interruption_reconnaissance", "date": "2024-01-15"},
        {"type": "", "date": ""},                      # rangée vide → ignorée
    ]}
    errors = dmod._normalize_prescription_events(doc)
    assert errors == []
    events = doc["prescription_events"]
    assert [e["type"] for e in events] == [
        "interruption_reconnaissance", "suspension",   # trié par date
    ]
    assert events[0]["date"] == _d(2024, 1, 15)
    assert events[1]["end_date"] == _d(2025, 5, 1)
    assert all(e["id"] for e in events)                # ids frappés


def test_normalize_refuses_junk_loudly():
    doc = {"prescription_events": [
        {"type": "abrogation", "date": "2025-01-01"},          # type inconnu
        {"type": "suspension", "date": "2025-01-01"},          # sans fin
        {"type": "suspension", "date": "2025-06-01",
         "end_date": "2025-01-01"},                            # fin < début
        {"type": "interruption_depot", "date": "n'importe"},   # date invalide
    ]}
    errors = dmod._normalize_prescription_events(doc)
    assert len(errors) == 4
    assert doc["prescription_events"] == []


def test_normalize_never_injects_on_missing_key():
    """La leçon _normalize de partie : sur un set plein-document, injecter
    est effacer. Ici l'appelant opère toujours sur un doc fusionné qui
    porte déjà la clé — mais un None explicite redevient []."""
    doc = {"prescription_events": None}
    assert dmod._normalize_prescription_events(doc) == []
    assert doc["prescription_events"] == []


# ── Dérivation ───────────────────────────────────────────────────────────


def test_no_events_future_date_is_courante():
    derived = dmod.derive_prescription(_doc())
    assert derived["status"] == "courante"
    assert derived["date_effective"] == _d(2026, 9, 21)


def test_no_events_past_date_is_echue():
    derived = dmod.derive_prescription(
        _doc(prescription_date=_d(2024, 1, 15))
    )
    assert derived["status"] == "echue"


def test_depot_interrupts_with_no_effective_date():
    """Art. 2896 : l'interruption dure jusqu'au jugement — la date
    effective est NULLE, jamais inventée."""
    derived = dmod.derive_prescription(_doc(prescription_events=[
        {"type": "interruption_depot", "date": _d(2026, 5, 15)},
    ]))
    assert derived["status"] == "interrompue"
    assert derived["date_effective"] is None


def test_legacy_prise_action_date_folds_as_implicit_depot():
    """Aucune migration : le champ hérité se lit comme un dépôt."""
    derived = dmod.derive_prescription(
        _doc(prise_action_date=_d(2026, 5, 15))
    )
    assert derived["status"] == "interrompue"
    assert derived["date_effective"] is None


def test_reconnaissance_restarts_the_same_period():
    """Art. 2898 : un nouveau délai de même durée court de la
    reconnaissance — via compute_date_pour_agir (report au jour juridique
    inclus). Le brut ne bouge pas."""
    doc = _doc(prescription_events=[
        {"type": "interruption_reconnaissance", "date": _d(2025, 3, 1)},
    ])
    derived = dmod.derive_prescription(doc)
    assert derived["status"] == "courante"
    # 2025-03-01 + 3 ans = 2028-03-01 (mercredi, jour juridique).
    assert derived["date_effective"] == _d(2028, 3, 1)
    assert doc["prescription_date"] == _d(2026, 9, 21)   # brut intact


def test_reconnaissance_without_confirmed_period_is_a_verifier():
    derived = dmod.derive_prescription(_doc(
        prescription_type="",
        prescription_date=None,
        prescription_events=[
            {"type": "interruption_reconnaissance", "date": _d(2025, 3, 1)},
        ],
    ))
    assert derived["status"] == "a_verifier"
    assert derived["date_effective"] is None


def test_suspension_shifts_by_its_duration():
    """61 jours suspendus décalent l'échéance d'autant, puis au jour
    juridique suivant. 2026-09-21 + 61 j = 2026-11-21 (samedi) →
    lundi 2026-11-23."""
    derived = dmod.derive_prescription(_doc(prescription_events=[
        {"type": "suspension", "date": _d(2025, 3, 1),
         "end_date": _d(2025, 5, 1)},
    ]))
    assert derived["status"] == "courante"
    assert derived["date_effective"] == _d(2026, 11, 23)


def test_depot_wins_over_everything_after():
    derived = dmod.derive_prescription(_doc(prescription_events=[
        {"type": "suspension", "date": _d(2024, 1, 1),
         "end_date": _d(2024, 2, 1)},
        {"type": "interruption_depot", "date": _d(2025, 6, 1)},
    ]))
    assert derived["status"] == "interrompue"


def test_imprescriptible_is_its_own_status():
    """Le piège épinglé par la vérification : imprescriptible force
    prescription_date à None — un « date nulle → a_verifier » naïf
    l'étiquetterait à tort comme à vérifier."""
    derived = dmod.derive_prescription(_doc(
        prescription_type="imprescriptible", prescription_date=None,
    ))
    assert derived["status"] == "imprescriptible"
    assert derived["date_effective"] is None


def test_events_only_push_dates_later():
    """L'invariant qui dispense d'un nouvel index : la requête serveur sur
    le brut SUR-capte, jamais l'inverse — reconnaissance et suspension ne
    produisent jamais une date effective antérieure au brut."""
    doc = _doc()
    for events in (
        [{"type": "interruption_reconnaissance", "date": _d(2025, 3, 1)}],
        [{"type": "suspension", "date": _d(2025, 3, 1),
          "end_date": _d(2025, 4, 1)}],
    ):
        derived = dmod.derive_prescription(_doc(prescription_events=events))
        assert derived["date_effective"] >= doc["prescription_date"]


# ── La couture des alertes ───────────────────────────────────────────────


def test_alerts_silence_and_rebase_through_the_seam(monkeypatch):
    """list_prescription_alerts : un dépôt taît, une reconnaissance qui
    pousse la date au-delà de la fenêtre sort la rangée, un a_verifier
    reste (alerté, jamais avalé)."""
    now = datetime.now(UTC)
    cutoff = now + timedelta(days=60)
    in_window = _doc(prescription_date=now + timedelta(days=30))
    interrupted = _doc(
        prescription_date=now + timedelta(days=20),
        prescription_events=[
            {"type": "interruption_depot", "date": now - timedelta(days=5)},
        ],
    )
    pushed_out = _doc(
        prescription_date=now + timedelta(days=10),
        prescription_events=[{
            "type": "interruption_reconnaissance",
            "date": now - timedelta(days=30),
        }],
    )
    # a_verifier DANS les alertes = un redémarrage sans période confirmée :
    # la date brute passe le filtre serveur, mais l'effective est
    # incalculable. (Une date manuelle sous « autre » SANS événement reste
    # « courante » — elle est calculable, juste confirmée à la main.)
    unverified = _doc(
        prescription_type="autre",
        prescription_date=now + timedelta(days=15),
        prescription_events=[{
            "type": "interruption_reconnaissance",
            "date": now - timedelta(days=10),
        }],
    )
    docs = [in_window, interrupted, pushed_out, unverified]
    for i, d in enumerate(docs):
        d["id"] = f"d{i}"

    class _Snap:
        def __init__(self, d):
            self._d = d

        def to_dict(self):
            return dict(self._d)

    query = mock.Mock()
    query.where.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.stream.return_value = [_Snap(d) for d in docs]
    monkeypatch.setattr(dmod, "db", mock.Mock(collection=lambda n: query))
    monkeypatch.setattr(dmod, "_migrate_parties", lambda d: d)

    alerts = dmod.list_prescription_alerts(cutoff)
    ids = [a["id"] for a in alerts]
    assert "d0" in ids            # rangée normale dans la fenêtre
    assert "d1" not in ids        # dépôt → tue
    assert "d2" not in ids        # reconnaissance → poussée hors fenêtre
    assert "d3" in ids            # a_verifier : alerté, drapeau posé
    by_id = {a["id"]: a for a in alerts}
    assert by_id["d3"]["prescription_status"] == "a_verifier"
    assert by_id["d0"]["prescription_status"] == "courante"
    assert by_id["d0"]["prescription_date_effective"] is not None
