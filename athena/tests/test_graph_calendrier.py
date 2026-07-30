"""Graph calendar glue for the Bookings sync (spec L2 §4.3-4.4).

Pins the deterministic prefix predicate, the UTC parse (Graph's 7-digit
fractional seconds + default-UTC), the client-attendee extraction, and the
cancel call shape (Calendars.ReadWrite).
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
from utils import graph_calendrier as gc  # noqa: E402

UTC = timezone.utc
UPN = "juriste@example.com"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", UPN)
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS", ("Consultation",))


def _ev(**over):
    base = {
        "id": "EVT1", "iCalUId": "ical-1",
        # Real « Bookings with me » format: « {Customer} - {Service} », so the
        # service keyword is a SUFFIX (does NOT start the subject).
        "subject": "Jason Poirier Lavoie - Consultation",
        "start": {"dateTime": "2026-09-01T13:30:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-01T14:30:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Bureau"},
        "isOnlineMeeting": True,
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/x?a=1,2"},
        "attendees": [
            {"emailAddress": {"address": "CLIENT@ex.com", "name": "Client X"}},
            {"emailAddress": {"address": UPN, "name": "Juriste"}},
        ],
        "organizer": {"emailAddress": {"address": UPN}},
        "isCancelled": False,
        "lastModifiedDateTime": "2026-07-25T10:00:00Z",
    }
    base.update(over)
    return base


# ── est_reservation ───────────────────────────────────────────────────────

def test_predicate_matches_keyword_as_suffix():
    """Regression: the keyword « Consultation » is at the END of the real
    Bookings subject, so a startswith predicate would MISS it — the substring
    match is what detects it."""
    ev = _ev()
    assert not ev["subject"].startswith("Consultation")  # not a prefix
    assert gc.est_reservation(ev) is True


def test_predicate_matches_case_insensitively():
    assert gc.est_reservation(_ev(subject="Dupont - consultation")) is True


def test_predicate_rejects_other_organizer():
    assert gc.est_reservation(
        _ev(organizer={"emailAddress": {"address": "someone@else.com"}})
    ) is False


def test_predicate_rejects_subject_without_keyword():
    assert gc.est_reservation(_ev(subject="Réunion interne")) is False


def test_predicate_false_when_upn_unset(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", "")
    assert gc.est_reservation(_ev()) is False


# ── extraire ──────────────────────────────────────────────────────────────

def test_extraire_parses_utc_and_trims_fractional():
    r = gc.extraire(_ev())
    assert r["start_datetime"] == datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    assert r["end_datetime"] == datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def test_extraire_maps_teams_link_onto_native_fields():
    r = gc.extraire(_ev())
    assert r["modalite"] == "visioconférence"
    assert r["conference_uri"] == "https://teams.microsoft.com/l/x?a=1,2"


def test_extraire_presentiel_when_not_online():
    r = gc.extraire(_ev(isOnlineMeeting=False, onlineMeeting={}))
    assert r["modalite"] == "présentiel"
    assert r["conference_uri"] == ""


def test_extraire_picks_the_client_attendee_lowercased():
    r = gc.extraire(_ev())
    assert r["client_email"] == "client@ex.com"
    assert r["client_nom"] == "Client X"


def test_extraire_carries_uid_and_cancel_flag():
    r = gc.extraire(_ev(isCancelled=True))
    assert r["graph_ical_uid"] == "ical-1"
    assert r["graph_event_id"] == "EVT1"
    assert r["is_cancelled"] is True


def test_parse_graph_dt_handles_trailing_z_no_fraction():
    dt = gc._parse_graph_dt({"dateTime": "2026-09-01T13:30:00Z", "timeZone": "UTC"})
    assert dt == datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def test_parse_graph_dt_none():
    assert gc._parse_graph_dt(None) is None
    assert gc._parse_graph_dt({}) is None


# ── lister_reservations / annuler_reservation ─────────────────────────────

def test_lister_reservations_calls_calendarview():
    with mock.patch.object(gc.graph, "graph_get",
                           return_value={"value": [_ev(), _ev(id="EVT2")]}) as g:
        rows = gc.lister_reservations(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        )
    assert len(rows) == 2
    path = g.call_args.args[0]
    params = g.call_args.kwargs["params"]
    assert path == f"/users/{UPN}/calendarView"
    assert "$select" in params and "$top" in params
    assert params["startDateTime"].startswith("2026-08-01")


def test_annuler_reservation_posts_cancel():
    with mock.patch.object(gc.graph, "graph_post", return_value=None) as p:
        gc.annuler_reservation("EVT1", "Refusé par le juriste")
    path = p.call_args.args[0]
    body = p.call_args.args[1]
    assert path == f"/users/{UPN}/events/EVT1/cancel"
    assert body["Comment"] == "Refusé par le juriste"


# ── Ancrage du mot-clé sur la fin du sujet (2026-07-28) ──────────────────


def test_le_mot_cle_doit_terminer_le_sujet():
    """Le prédicat ne peut PAS distinguer une réservation Bookings d'un
    événement que le juriste crée lui-même : dans les deux cas il est
    l'organisateur. Le mot-clé est donc le seul discriminant, et une simple
    sous-chaîne capturerait un titre interne — importé comme rendez-vous
    client, avec un refus qui annulerait la vraie réunion Outlook."""
    assert gc.est_reservation(_ev(subject="Jean Tremblay - Consultation"))
    assert not gc.est_reservation(_ev(subject="Consultation interne dossier X"))
    assert not gc.est_reservation(_ev(subject="Préparation consultation CA"))


def test_le_separateur_est_exige():
    """Un événement intitulé du seul nom du service ne mord pas : Bookings
    écrit toujours « {Client} - {Service} »."""
    assert not gc.est_reservation(_ev(subject="Consultation"))


@pytest.mark.parametrize("tiret", ["-", "–", "—"])
def test_les_trois_tirets_sont_admis(tiret):
    """Outlook substitue parfois un cadratin au trait d'union saisi."""
    assert gc.est_reservation(_ev(subject=f"Jean Tremblay {tiret} Consultation"))


def test_espaces_de_fin_toleres():
    assert gc.est_reservation(_ev(subject="Jean Tremblay - Consultation  "))


# ── Pliage des accents (le mode de défaillance né avec « Réunion ») ──────


def test_un_sujet_decompose_est_reconnu(monkeypatch):
    """LE piège que « Consultation » n'avait pas : « é » précomposé (NFC,
    U+00E9) et « é » décomposé (NFD, e + U+0301) sont des chaînes DIFFÉRENTES.
    Sans pliage, un mot-clé accentué peut ne jamais mordre, sans le moindre
    message — selon l'appareil avec lequel le service a été nommé."""
    import unicodedata

    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS", ("Réunion",))
    nfd = unicodedata.normalize("NFD", "Jean Tremblay - Réunion")
    nfc = unicodedata.normalize("NFC", "Jean Tremblay - Réunion")
    assert nfd != nfc                      # ce sont bien deux chaînes
    assert gc.est_reservation(_ev(subject=nfd))
    assert gc.est_reservation(_ev(subject=nfc))


def test_un_mot_cle_sans_accent_mord_un_sujet_accentue(monkeypatch):
    """Effet de bord heureux : BOOKINGS_SUBJECT_KEYWORDS=« Reunion » saisi
    sans accent dans app.yaml fonctionne quand même."""
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS", ("Reunion",))
    assert gc.est_reservation(_ev(subject="Jean Tremblay - Réunion"))


def test_la_casse_accentuee_est_pliee(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS", ("Réunion",))
    assert gc.est_reservation(_ev(subject="JEAN TREMBLAY - RÉUNION"))


# ── Le mot-clé détecté, et le type qu'il commande ───────────────────────


def test_le_mot_cle_detecte_est_rendu(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS",
                        ("Consultation", "Réunion"))
    assert gc.mot_cle_correspondant(
        _ev(subject="Jean - Réunion")) == "Réunion"
    assert gc.mot_cle_correspondant(
        _ev(subject="Jean - Consultation")) == "Consultation"
    assert gc.mot_cle_correspondant(_ev(subject="Réunion d'équipe")) == ""


def test_extraire_transporte_le_mot_cle(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS",
                        ("Consultation", "Réunion"))
    assert gc.extraire(_ev(subject="Jean - Réunion"))["mot_cle"] == "Réunion"


def test_un_mot_cle_vide_ne_capture_rien(monkeypatch):
    """Garde conservée de la version sous-chaîne : une chaîne vide serait
    « contenue » partout."""
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS", ("",))
    assert not gc.est_reservation(_ev(subject="Jean Tremblay - Consultation"))


def test_l_organisateur_reste_exige(monkeypatch):
    """L'ancrage resserre le sujet ; il ne relâche pas l'organisateur."""
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_KEYWORDS", ("Réunion",))
    assert not gc.est_reservation(_ev(
        subject="Jean - Réunion",
        organizer={"emailAddress": {"address": "autre@example.com"}},
    ))


# ── Garde anti-boucle du miroir Outlook (2026-07-29) ─────────────────────
# Le miroir écrit les audiences d'Athéna dans le calendrier MÊME que ce
# module interroge. Un événement miroir a le juriste pour organisateur (tout
# événement créé par l'application l'a) et peut porter un sujet qui satisfait
# l'ancrage — le marqueur est donc la SEULE chose qui le tient hors du
# pipeline d'import, où un refus annulerait l'événement réel.


def _prop_miroir(valeur="h-123|etag-1"):
    return [{"id": gc.MIROIR_PROP_ID, "value": valeur}]


def test_predicat_rejette_evenement_marque_par_propriete():
    """Organisateur correct, sujet qui matche — la propriété seule refuse."""
    ev = _ev(singleValueExtendedProperties=_prop_miroir())
    assert gc.mot_cle_correspondant(ev) == ""
    assert gc.est_reservation(ev) is False


def test_predicat_rejette_evenement_marque_par_categorie():
    """Garde LARGE : la catégorie suffit, même sans la propriété (un $expand
    perdu dans une retouche future ne rouvrirait pas la boucle à lui seul)."""
    ev = _ev(categories=[gc.MIROIR_CATEGORIE])
    assert gc.mot_cle_correspondant(ev) == ""


def test_marqueur_id_compare_casse_pliee():
    """Graph renvoie le GUID de la propriété dans une casse non garantie."""
    ev = _ev(singleValueExtendedProperties=[
        {"id": gc.MIROIR_PROP_ID.upper(), "value": "h-1|e-1"}
    ])
    assert gc.porte_marqueur_miroir(ev) is True


def test_une_autre_categorie_ne_declenche_pas_le_garde():
    ev = _ev(categories=["Important"])
    assert gc.est_reservation(ev) is True


def test_lister_reservations_expand_et_categories():
    """Sans le $expand et « categories » dans le $select, le garde est
    aveugle : la synchro recevrait des événements sans leur marqueur."""
    with mock.patch.object(gc.graph, "graph_get",
                           return_value={"value": []}) as g:
        gc.lister_reservations(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        )
    params = g.call_args.kwargs["params"]
    assert params["$expand"] == gc.EXPAND_MIROIR
    assert "categories" in params["$select"]
