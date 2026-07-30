"""Miroir Outlook — charge d'événement, marqueur, diff (utils/graph_miroir).

Pins the silent-failure zones of the mirror payload and diff:
- NO ``attendees`` key ever (an attendee makes Exchange send an invitation);
- ``transactionId`` on CREATE only (a PATCH carrying it is refused by Graph);
- all-day events anchored at local (America/Toronto) midnight — UTC midnight
  would render the day on TWO days in Eastern-time Outlook;
- the diff compares all-day boundaries as DATES, so it is stable whichever
  representation Graph returns (UTC midnight or converted local midnight);
- graph_patch/graph_delete honour the GraphError status-code-only contract.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
from utils import graph, graph_miroir as gm  # noqa: E402
from utils.graph_calendrier import MIROIR_CATEGORIE, MIROIR_PROP_ID  # noqa: E402

UTC = timezone.utc
UPN = "juriste@example.com"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", UPN)


def _h(**over):
    start = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    base = {
        "id": "h-1",
        "etag": "e-1",
        "title": "Audience CS",
        "start_datetime": start,
        "end_datetime": start + timedelta(hours=2),
        "all_day": False,
        "location": "Palais de justice de Montréal",
        "reminder_minutes": 1440,
        "dossier_file_number": "2026-001",
        "modalite": "présentiel",
        "conference_uri": "",
        "confirmation": "",
        "source": "",
        "status": "confirmée",
    }
    base.update(over)
    return base


def _ev_conforme(h=None, **over):
    """L'événement Graph tel qu'il revient de calendarView APRÈS notre écriture
    (heures rendues en UTC, fractions à 7 chiffres, marqueur porté)."""
    h = h or _h()
    ev = {
        "id": "EVT-1",
        "subject": h["title"],
        "start": {
            "dateTime": h["start_datetime"].strftime("%Y-%m-%dT%H:%M:%S")
            + ".0000000",
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": h["end_datetime"].strftime("%Y-%m-%dT%H:%M:%S")
            + ".0000000",
            "timeZone": "UTC",
        },
        "isAllDay": False,
        "location": {"displayName": h["location"]},
        "isReminderOn": True,
        "reminderMinutesBeforeStart": h["reminder_minutes"],
        "showAs": "busy",
        "categories": [MIROIR_CATEGORIE],
        "singleValueExtendedProperties": [
            {"id": MIROIR_PROP_ID, "value": f"{h['id']}|{h['etag']}"}
        ],
    }
    ev.update(over)
    return ev


# ── construire_charge ─────────────────────────────────────────────────────


def test_charge_evenement_minute():
    charge = gm.construire_charge(_h())
    assert charge["subject"] == "Audience CS"
    assert charge["start"] == {"dateTime": "2026-09-01T13:30:00", "timeZone": "UTC"}
    assert charge["end"] == {"dateTime": "2026-09-01T15:30:00", "timeZone": "UTC"}
    assert charge["isAllDay"] is False
    assert charge["showAs"] == "busy"
    assert charge["isReminderOn"] is True
    assert charge["reminderMinutesBeforeStart"] == 1440
    assert charge["location"] == {"displayName": "Palais de justice de Montréal"}
    assert charge["categories"] == [MIROIR_CATEGORIE]
    prop = charge["singleValueExtendedProperties"][0]
    assert prop == {"id": MIROIR_PROP_ID, "value": "h-1|e-1"}


def test_charge_jamais_d_attendees():
    """Un attendee sur un événement créé par l'application fait envoyer une
    invitation de réunion par Exchange — le miroir doit rester une copie
    silencieuse. Ni attendees, ni organizer, jamais."""
    for h in (_h(), _h(all_day=True), _h(client_email="client@ex.com")):
        charge = gm.construire_charge(h, avec_transaction=True)
        assert "attendees" not in charge
        assert "organizer" not in charge


def test_charge_all_day_minuit_local():
    """Minuit UTC s'afficherait la veille au soir en heure de l'Est — la
    journée déborderait sur DEUX jours dans Outlook."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    charge = gm.construire_charge(
        _h(all_day=True, start_datetime=start,
           end_datetime=start + timedelta(hours=1))
    )
    assert charge["isAllDay"] is True
    assert charge["start"] == {
        "dateTime": "2026-09-01T00:00:00", "timeZone": "America/Toronto"
    }
    # Fin exclusive : même jour → +1 jour (la convention DTEND du DAV).
    assert charge["end"] == {
        "dateTime": "2026-09-02T00:00:00", "timeZone": "America/Toronto"
    }


def test_charge_all_day_plusieurs_jours():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    charge = gm.construire_charge(
        _h(all_day=True, start_datetime=start,
           end_datetime=datetime(2026, 9, 3, tzinfo=UTC))
    )
    assert charge["end"]["dateTime"] == "2026-09-03T00:00:00"


def test_charge_transaction_id_creation_seulement():
    """transactionId est immuable : un PATCH qui le porte est refusé par
    Graph. Unique par version d'audience, il dédoublonne un retry HTTP."""
    creation = gm.construire_charge(_h(), avec_transaction=True)
    correction = gm.construire_charge(_h())
    assert creation["transactionId"] == "pallas-h-1-e-1"
    assert "transactionId" not in correction


def test_charge_corps_nr_et_visio():
    h = _h(modalite="visioconférence",
           conference_uri="https://teams.microsoft.com/l/x?a=1,2")
    corps = gm.construire_charge(h)["body"]
    assert corps["contentType"] == "text"
    assert "N/R : 2026-001" in corps["content"]
    assert "Visioconférence: https://teams.microsoft.com/l/x?a=1,2" in corps["content"]


def test_charge_corps_minimal_sans_dossier_ni_visio():
    h = _h(dossier_file_number="", modalite="présentiel")
    corps = gm.construire_charge(h)["body"]["content"]
    assert corps == ""


def test_charge_lieu_toujours_present_meme_vide():
    """Un PATCH qui omet location laisserait l'ancien lieu dans Outlook, que
    le diff re-signalerait à chaque cycle — un PATCH perpétuel sans effet."""
    charge = gm.construire_charge(_h(location=""))
    assert charge["location"] == {"displayName": ""}


# ── Marqueur ──────────────────────────────────────────────────────────────


def test_marqueur_aller_retour():
    ev = _ev_conforme()
    assert gm.lire_marqueur(ev) == ("h-1", "e-1")
    assert gm.valeur_marqueur(_h()) == "h-1|e-1"


def test_marqueur_malforme_vaut_absence():
    """Un marqueur corrompu ne doit jamais piloter une suppression."""
    for valeur in ("", "sans-pipe", "|etag-seul"):
        ev = _ev_conforme(
            singleValueExtendedProperties=[
                {"id": MIROIR_PROP_ID, "value": valeur}
            ]
        )
        assert gm.lire_marqueur(ev) == ("", "")
    assert gm.lire_marqueur({"singleValueExtendedProperties": []}) == ("", "")
    assert gm.lire_marqueur({}) == ("", "")


def test_marqueur_id_casse_pliee():
    ev = _ev_conforme(
        singleValueExtendedProperties=[
            {"id": MIROIR_PROP_ID.upper(), "value": "h-9|e-9"}
        ]
    )
    assert gm.lire_marqueur(ev) == ("h-9", "e-9")


# ── doit_corriger ─────────────────────────────────────────────────────────


def test_doit_corriger_noop_cycle_stable():
    """Un événement conforme (fractions à 7 chiffres incluses) ne déclenche
    RIEN — sinon chaque cycle émettrait un PATCH sans effet."""
    assert gm.doit_corriger(_h(), _ev_conforme()) is False


def test_microsecondes_ne_condamnent_pas_a_un_patch_perpetuel():
    """La charge écrit à la seconde (strftime %S) : une audience portant des
    microsecondes divergerait sinon À CHAQUE cycle de ce que Graph renvoie —
    un PATCH perpétuel sans effet. Les cibles tronquent comme la charge."""
    start = datetime(2026, 9, 1, 13, 30, 0, 123456, tzinfo=UTC)
    h = _h(start_datetime=start, end_datetime=start + timedelta(hours=2))
    ev = _ev_conforme(_h())  # ce que Graph renvoie après NOTRE écriture
    assert gm.doit_corriger(h, ev) is False


def test_doit_corriger_sur_etag():
    """L'audience a changé dans Athéna (notes → body inclus) : l'etag stampé
    est en retard, même si tous les champs visibles coïncident."""
    h = _h(etag="e-2")
    assert gm.doit_corriger(h, _ev_conforme(_h())) is True


def test_doit_corriger_sur_edition_outlook():
    """Athéna écrase (décision 2026-07-29) : une heure ou un titre modifiés
    dans Outlook divergent des champs cibles alors que l'etag stampé est à
    jour."""
    assert gm.doit_corriger(_h(), _ev_conforme(subject="Titre modifié")) is True
    deplace = _ev_conforme(
        start={"dateTime": "2026-09-01T15:00:00.0000000", "timeZone": "UTC"}
    )
    assert gm.doit_corriger(_h(), deplace) is True


def test_doit_corriger_sur_categorie_retiree():
    """La catégorie est la moitié du garde anti-boucle : retirée à la main,
    le PATCH la restaure."""
    assert gm.doit_corriger(_h(), _ev_conforme(categories=[])) is True


def test_doit_corriger_all_day_stable_sous_les_deux_representations():
    """Graph peut rendre un all-day à minuit UTC OU à minuit local converti
    (04:00Z) : le diff compare des DATES, sinon l'une des deux représentations
    condamnerait chaque cycle à un PATCH perpétuel."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    h = _h(all_day=True, start_datetime=start,
           end_datetime=start + timedelta(hours=1))
    for s, e in (
        ("2026-09-01T00:00:00.0000000", "2026-09-02T00:00:00.0000000"),
        ("2026-09-01T04:00:00.0000000", "2026-09-02T04:00:00.0000000"),
    ):
        ev = _ev_conforme(
            h,
            isAllDay=True,
            start={"dateTime": s, "timeZone": "UTC"},
            end={"dateTime": e, "timeZone": "UTC"},
        )
        assert gm.doit_corriger(h, ev) is False


def test_champs_cibles_all_day_en_dates():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    cibles = gm.champs_cibles(
        _h(all_day=True, start_datetime=start,
           end_datetime=start + timedelta(hours=1))
    )
    assert cibles["start"] == date(2026, 9, 1)
    assert cibles["end"] == date(2026, 9, 2)


def test_rappel_eteint_neutralise_les_minutes():
    """Rappel éteint → Graph renvoie des minutes arbitraires ; sans la
    neutralisation, le diff fabriquerait une divergence permanente."""
    h = _h(reminder_minutes=0)
    ev = _ev_conforme(isReminderOn=False, reminderMinutesBeforeStart=15)
    assert gm.champs_cibles(h)["rappel_minutes"] == 0
    assert gm.champs_observes(ev)["rappel_minutes"] == 0


# ── lister_miroirs + appels Graph ─────────────────────────────────────────


def test_lister_miroirs_filtre_sur_la_propriete():
    """La catégorie seule ne qualifie PAS pour le diff (donc jamais pour une
    suppression) : un vrai événement catégorisé à la main doit rester
    intouchable."""
    marque = _ev_conforme()
    categorise_seulement = _ev_conforme(
        id="EVT-2", singleValueExtendedProperties=[]
    )
    with mock.patch.object(
        gm.graph, "graph_get",
        return_value={"value": [marque, categorise_seulement]},
    ) as g:
        rows = gm.lister_miroirs(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        )
    assert [ev["id"] for ev in rows] == ["EVT-1"]
    path = g.call_args.args[0]
    params = g.call_args.kwargs["params"]
    assert path == f"/users/{UPN}/calendarView"
    assert "singleValueExtendedProperties" in params["$expand"]
    assert MIROIR_PROP_ID in params["$expand"]


def test_appels_graph_chemins_exacts():
    h = _h()
    with mock.patch.object(gm.graph, "graph_post", return_value=None) as p:
        gm.creer_miroir(h)
    assert p.call_args.args[0] == f"/users/{UPN}/events"
    assert p.call_args.args[1]["transactionId"] == "pallas-h-1-e-1"

    with mock.patch.object(gm.graph, "graph_patch", return_value=None) as pa:
        gm.corriger_miroir("EVT-1", h)
    assert pa.call_args.args[0] == f"/users/{UPN}/events/EVT-1"
    assert "transactionId" not in pa.call_args.args[1]

    with mock.patch.object(gm.graph, "graph_delete", return_value=None) as d:
        gm.supprimer_miroir("EVT-1")
    assert d.call_args.args[0] == f"/users/{UPN}/events/EVT-1"


# ── graph_patch / graph_delete (utils/graph) ──────────────────────────────


@pytest.fixture()
def _jeton(monkeypatch):
    monkeypatch.setattr(graph, "_auth_headers", lambda: {"Authorization": "B t"})


def test_graph_patch_statuts(_jeton):
    ok = mock.Mock(status_code=200, content=b"{}")
    ok.json.return_value = {"id": "EVT-1"}
    with mock.patch.object(graph.requests, "patch", return_value=ok):
        assert graph.graph_patch("/x", {}) == {"id": "EVT-1"}

    vide = mock.Mock(status_code=204, content=b"")
    with mock.patch.object(graph.requests, "patch", return_value=vide):
        assert graph.graph_patch("/x", {}) is None

    refus = mock.Mock(status_code=404, text="corps-secret")
    with mock.patch.object(graph.requests, "patch", return_value=refus):
        with pytest.raises(graph.GraphError) as exc:
            graph.graph_patch("/x", {})
    assert "404" in str(exc.value) and "corps-secret" not in str(exc.value)


def test_graph_delete_statuts(_jeton):
    ok = mock.Mock(status_code=204)
    with mock.patch.object(graph.requests, "delete", return_value=ok):
        assert graph.graph_delete("/x") is None

    refus = mock.Mock(status_code=403, text="corps-secret")
    with mock.patch.object(graph.requests, "delete", return_value=refus):
        with pytest.raises(graph.GraphError) as exc:
            graph.graph_delete("/x")
    assert "403" in str(exc.value) and "corps-secret" not in str(exc.value)


def test_graph_patch_reseau_sans_url(_jeton):
    exc_reseau = graph.requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='graph.microsoft.com'...)"
    )
    with mock.patch.object(graph.requests, "patch", side_effect=exc_reseau):
        with pytest.raises(graph.GraphError) as info:
            graph.graph_patch("/x", {})
    assert "ConnectionError" in str(info.value)
    assert "graph.microsoft.com" not in str(info.value)
