"""Le calendrier par le connecteur (lot 1b, étape L7).

Trois outils — ``update_hearing``, ``create_hearing_series``,
``decide_rendez_vous`` — et deux changements additifs : ``create_hearing``
gagne modalité, lien de visioconférence, rappel et statut ; ``list_hearings``
gagne deux modes (``serie_id`` : une chaîne entière ; ``bookings``
« pending » : les demandes Bookings en attente de décision). Tout passe ici
par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), les vrais modèles, le vrai service
(``services/rendez_vous.py``, la porte de la Réception), les vrais
gestionnaires et le vrai protocole d'écriture (``run_write``) ; on relit ce
qui est STOCKÉ. Seul Graph est remplacé : l'annulation Outlook est
enregistrée (identifiant, texte) au lieu d'être envoyée.

On épingle, outil par outil :
* le report d'un événement garde son heure murale de Montréal et sa durée,
  y compris par-dessus un changement d'heure ;
* la barrière de confirmation : une demande Bookings en attente ou refusée
  ne se modifie jamais par update_hearing ;
* l'avertissement D10 sur un rendez-vous Bookings CONFIRMÉ : Outlook, le
  client et la disponibilité ne sont pas mis à jour ;
* une série exige sa clé d'idempotence, s'écrit en UN lot atomique qui
  porte son propre bump, et garde « 9 h » à 9 h de part et d'autre du
  changement d'heure ;
* la décision : l'etag vérifié AVANT Outlook, le texte fixe envoyé au
  client, et l'échec local APRÈS l'annulation rendu comme un succès averti
  que le rejeu de la même clé ne renvoie jamais à Outlook.
"""

import ast
import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support  # noqa: F401
    from models import hearing as hearing_model
    from models import partie as partie_model  # noqa: F401
    from services import rendez_vous

from tests._fake_firestore import install  # noqa: E402
from tz import mtl_to_utc  # noqa: E402
from utils import recurrence  # noqa: E402
from utils.graph import GraphError  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    for did, num, status in (("d1", "2026-001", "actif"),
                             ("d2", "2026-002", "actif"),
                             ("d3", "2026-003", "fermé")):
        fake.seed(f"dossiers/{did}", {"id": did, "file_number": num,
                                      "title": f"Dossier {num}",
                                      "status": status})
        fake.seed(f"dav_sync/dossier:{did}", {"ctag": "c0", "sync_token": "c0",
                                             "updated_at": WHEN})
    fake.seed("dav_sync/general", {"ctag": "g0", "sync_token": "g0",
                                   "updated_at": WHEN})
    # The series and a future « terminée » are judged against a FROZEN day.
    monkeypatch.setattr(handlers.deadlines, "today_mtl",
                        lambda: date(2026, 10, 1))
    return fake


@pytest.fixture
def graph(monkeypatch):
    """Graph configured; every cancellation recorded as (event id, text)."""
    calls = []
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)
    monkeypatch.setattr(
        rendez_vous.graph_calendrier, "annuler_reservation",
        lambda gid, motif="": calls.append((gid, motif)),
    )
    return calls


def _mtl(y, m, d, hh=0, mm=0) -> datetime:
    return mtl_to_utc(datetime(y, m, d, hh, mm))


def _dt(value) -> datetime:
    return datetime.fromtimestamp(value.timestamp(), tz=UTC)


def _hearing(fake, hid="h1", *, start=None, end=None, **over) -> dict:
    start = start or _mtl(2026, 10, 15, 9)
    end = end or start + timedelta(hours=1)
    doc = {
        "id": hid, "title": "Rencontre", "hearing_type": "rencontre",
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Dossier 2026-001",
        "start_datetime": start, "end_datetime": end, "all_day": False,
        "location": "", "court": "", "judge": "", "notes": "Notes initiales.",
        "reminder_minutes": 1440, "status": "confirmée",
        "modalite": "présentiel", "conference_uri": "",
        "source": "", "confirmation": "", "serie_id": "", "serie_rule": None,
        "vevent_uid": f"uid-{hid}", "dav_href": f"/dav/dossier-d1/{hid}.ics",
        "etag": f"e-{hid}", "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    fake.seed(f"hearings/{hid}", doc)
    return doc


def _booking(fake, hid="b1", *, confirmation="à_confirmer", **over) -> dict:
    return _hearing(
        fake, hid, title="Consultation", hearing_type="consultation",
        dossier_id="", dossier_file_number="", dossier_title="",
        dav_href=f"/dav/general/{hid}.ics", source="bookings",
        confirmation=confirmation, graph_event_id=f"EVT-{hid}",
        graph_ical_uid=f"UID-{hid}", client_email="client@ex.com",
        client_nom="Jean Tremblay", **over)


def _partie(fake, pid="p1", email="client@ex.com"):
    fake.seed(f"parties/{pid}", {
        "id": pid, "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay", "email": email,
        "updated_at": WHEN, "etag": f"e-{pid}"})


def _stored(fake, hid="h1") -> dict:
    return fake.peek(f"hearings/{hid}")


def _ctag(fake, did="d1") -> str:
    name = f"dossier:{did}" if did else "general"
    return fake.peek(f"dav_sync/{name}")["ctag"]


def _hearing_commits(fake, hid="h1") -> list:
    return [c for c in fake.commits
            if any(p == f"hearings/{hid}" for _k, p in c.ops)]


def _refused(call, *args, match=None) -> tools.ToolArgumentError:
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        call(*args)
    if match is not None:
        assert match in str(excinfo.value), str(excinfo.value)
    return excinfo.value


def _fail_hearing_commits(fake) -> list:
    failed = []

    def hook(info):
        if any(p.startswith("hearings/") for _, p in info.ops):
            failed.append(1)
            raise gexc.ServiceUnavailable("injected commit failure")

    fake.add_commit_hook(hook)
    return failed


def _fail_queries_on(monkeypatch, fake, collection: str) -> None:
    server = fake._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if sq.from_ and sq.from_[0].collection_id == collection:
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


# ══════════════════════════════════════════════════════════════════════
# 0. Les vocabulaires recopiés sont ceux du modèle
# ══════════════════════════════════════════════════════════════════════


def test_the_calendar_vocabularies_are_the_models():
    assert tools._HEARING_MODALITES == list(hearing_model.VALID_MODALITES)
    assert tools._HEARING_STATUSES == list(hearing_model.VALID_STATUSES)
    assert tools._HEARING_REMINDERS == list(hearing_model.VALID_REMINDER_MINUTES)
    assert set(tools._HEARING_CREATE_STATUSES) < set(hearing_model.VALID_STATUSES)
    assert tools._SERIES_FREQUENCIES == list(recurrence.VALID_FREQUENCIES)
    assert tools._SERIES_MAX == recurrence.MAX_SERIE_OCCURRENCES
    props = {n: tools.TOOLS[n]["input_schema"]["properties"]
             for n in ("create_hearing", "create_hearing_series",
                       "update_hearing")}
    for name, p in props.items():
        assert p["modalite"]["enum"] == tools._HEARING_MODALITES, name
        assert p["reminder_minutes"]["enum"] == tools._HEARING_REMINDERS, name
    assert props["update_hearing"]["status"]["enum"] == tools._HEARING_STATUSES
    assert props["create_hearing"]["status"]["enum"] == ["à_confirmer",
                                                          "confirmée"]
    series = props["create_hearing_series"]
    assert series["count"]["maximum"] == recurrence.MAX_SERIE_OCCURRENCES
    assert series["frequency"]["enum"] == list(recurrence.VALID_FREQUENCIES)


def test_the_policies_are_the_plans():
    """Series and outbound tools demand their key (plan rule 7); the
    outbound decision demands the etag (rule 3); update_hearing accepts one."""
    assert tools.idempotency_policy("create_hearing_series") == "required"
    assert tools.idempotency_policy("decide_rendez_vous") == "required"
    assert tools.idempotency_policy("update_hearing") == "optional"
    assert tools.TOOLS["decide_rendez_vous"]["concurrency"] == "required"
    assert tools.TOOLS["update_hearing"]["concurrency"] == "optional"
    assert {"update_hearing", "decide_rendez_vous"} <= tools.EDIT_TOOLS
    assert "create_hearing_series" not in tools.EDIT_TOOLS
    assert tools.OUTBOUND_TOOLS == frozenset({"decide_rendez_vous"})


# ══════════════════════════════════════════════════════════════════════
# 1. update_hearing — le report
# ══════════════════════════════════════════════════════════════════════


def test_a_new_day_keeps_the_montreal_hour_and_the_duration_across_dst(fake):
    """30 octobre 09:00-10:30 (HAE, UTC-4) reporté au 6 novembre (HNE,
    UTC-5) : 09:00-10:30 à Montréal, soit 14:00Z — jamais 13:00Z, ce qu'un
    ajout de timedelta à la valeur UTC stockée donnerait."""
    _hearing(fake, start=_mtl(2026, 10, 30, 9), end=_mtl(2026, 10, 30, 10, 30))
    fake.reset_logs()
    before = _ctag(fake)

    payload = handlers.update_hearing({"hearing_id": "h1",
                                       "date": "2026-11-06"})

    stored = _stored(fake)
    assert _dt(stored["start_datetime"]) == datetime(2026, 11, 6, 14, tzinfo=UTC)
    assert _dt(stored["end_datetime"]) == datetime(2026, 11, 6, 15, 30, tzinfo=UTC)
    assert stored["updated_via"] == "mcp" and stored["etag"] != "e-h1"
    assert payload["entity"]["date"] == "2026-11-06T09:00:00-05:00"
    assert payload["entity"]["end"] == "2026-11-06T10:30:00-05:00"
    assert payload["entity"]["etag"] == stored["etag"]
    assert set(payload["changed_fields"]) == {"start_datetime", "end_datetime"}
    assert payload["outlook_mirror"] == "follows"
    assert payload["ctag_bumped"] is True and _ctag(fake) != before
    (commit,) = _hearing_commits(fake)
    assert commit.transaction is not None   # compare-and-set on its own read


def test_a_new_start_hour_keeps_the_duration(fake):
    _hearing(fake)   # 09:00-10:00
    handlers.update_hearing({"hearing_id": "h1", "start_time": "14:00"})
    stored = _stored(fake)
    assert _dt(stored["start_datetime"]) == _mtl(2026, 10, 15, 14)
    assert _dt(stored["end_datetime"]) == _mtl(2026, 10, 15, 15)


def test_an_end_hour_alone_moves_only_the_end_and_is_refused_before_the_start(
    fake,
):
    _hearing(fake)
    handlers.update_hearing({"hearing_id": "h1", "end_time": "11:30"})
    stored = _stored(fake)
    assert _dt(stored["start_datetime"]) == _mtl(2026, 10, 15, 9)
    assert _dt(stored["end_datetime"]) == _mtl(2026, 10, 15, 11, 30)

    before = _stored(fake)
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "end_time": "08:00"}, match="end_time")
    assert _stored(fake) == before


def test_all_day_and_timed_flips(fake):
    _hearing(fake)   # timed, 15 October 09:00 Montréal
    handlers.update_hearing({"hearing_id": "h1", "all_day": True})
    stored = _stored(fake)
    assert stored["all_day"] is True
    assert _dt(stored["start_datetime"]) == datetime(2026, 10, 15, tzinfo=UTC)

    # Back to timed: the hour is REQUIRED, never guessed.
    before = _stored(fake)
    _refused(handlers.update_hearing, {"hearing_id": "h1", "all_day": False},
             match="start_time")
    assert _stored(fake) == before
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "all_day": True, "start_time": "09:00"},
             match="all_day")

    handlers.update_hearing({"hearing_id": "h1", "start_time": "10:00"})
    stored = _stored(fake)
    assert stored["all_day"] is False
    assert _dt(stored["start_datetime"]) == _mtl(2026, 10, 15, 10)
    assert _dt(stored["end_datetime"]) == _mtl(2026, 10, 15, 11)


def test_values_already_stored_write_nothing(fake):
    _hearing(fake)
    fake.reset_logs()
    before = _ctag(fake)
    payload = handlers.update_hearing(
        {"hearing_id": "h1", "title": "Rencontre", "start_time": "09:00"})
    assert payload["changed_fields"] == []
    assert payload["outlook_mirror"] == "unchanged"
    assert payload["ctag_bumped"] is False and _ctag(fake) == before
    assert _hearing_commits(fake) == []
    assert any("rien n'a été modifié" in w for w in payload["warnings"])


def test_a_stale_etag_writes_nothing(fake):
    _hearing(fake)
    before = _stored(fake)
    err = _refused(handlers.update_hearing,
                   {"hearing_id": "h1", "title": "X", "expected_etag": "vieux"})
    assert err.reason == "stale_etag"
    assert "list_hearings" in str(err) and "get_agenda" in str(err)
    assert _stored(fake) == before


def test_an_unknown_event_and_an_unreadable_one_are_told_apart(
    fake, monkeypatch,
):
    _refused(handlers.update_hearing, {"hearing_id": "absent", "title": "X"},
             match="introuvable")

    def broken(_hid):
        raise gexc.ServiceUnavailable("blip")

    monkeypatch.setattr(hearing_model, "get_hearing_strict", broken)
    err = _refused(handlers.update_hearing, {"hearing_id": "h1", "title": "X"})
    assert "Lecture" in str(err) and "introuvable" not in str(err)


# ══════════════════════════════════════════════════════════════════════
# 2. update_hearing — la barrière Bookings (D10)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("confirmation", ["à_confirmer", "annulée_client",
                                          "refusée"])
def test_an_undecided_or_refused_booking_is_never_edited(fake, confirmation):
    _booking(fake, confirmation=confirmation)
    before = _stored(fake, "b1")
    err = _refused(handlers.update_hearing,
                   {"hearing_id": "b1", "title": "Détour"})
    if confirmation == "refusée":
        assert "refusée" in str(err)
    else:
        assert "decide_rendez_vous" in str(err)
    assert _stored(fake, "b1") == before


def test_a_confirmed_booking_is_edited_with_the_d10_warning_every_time(fake):
    _booking(fake, confirmation="")
    payload = handlers.update_hearing({"hearing_id": "b1",
                                       "start_time": "15:00"})
    assert _dt(_stored(fake, "b1")["start_datetime"]) == _mtl(2026, 10, 15, 15)
    assert handlers._BOOKINGS_NOT_UPDATED in payload["warnings"]
    assert payload["outlook_mirror"] == "not_mirrored"
    # Even a call that writes nothing says it.
    again = handlers.update_hearing({"hearing_id": "b1",
                                     "start_time": "15:00"})
    assert again["changed_fields"] == []
    assert handlers._BOOKINGS_NOT_UPDATED in again["warnings"]


def test_an_unseen_bookings_divergence_is_said_never_blocked(fake):
    """The client moved the confirmed rendez-vous on the Bookings side (the
    sync records a divergence, never overwrites): the edit still lands, and
    the result says to look at Réception first. A divergence already SEEN
    is not repeated."""
    _booking(fake, confirmation="", bookings_divergence={
        "motif": "modifie", "detail": "", "vu": False})
    payload = handlers.update_hearing({"hearing_id": "b1",
                                       "location": "Bureau"})
    assert _stored(fake, "b1")["location"] == "Bureau"
    assert handlers._BOOKINGS_DIVERGENCE_UNSEEN in payload["warnings"]

    _booking(fake, "b2", confirmation="", bookings_divergence={
        "motif": "modifie", "detail": "", "vu": True})
    seen = handlers.update_hearing({"hearing_id": "b2", "location": "Bureau"})
    assert handlers._BOOKINGS_DIVERGENCE_UNSEEN not in seen["warnings"]


# ══════════════════════════════════════════════════════════════════════
# 3. update_hearing — statut, notes, modalité
# ══════════════════════════════════════════════════════════════════════


def test_cancelling_says_the_outlook_copy_goes_and_keeps_it_in_dav(fake):
    _hearing(fake)
    before = _ctag(fake)
    payload = handlers.update_hearing({"hearing_id": "h1",
                                       "status": "annulée"})
    assert _stored(fake)["status"] == "annulée"
    assert payload["outlook_mirror"] == "removed"
    assert handlers._HEARING_CANCELLED in payload["warnings"]
    assert payload["previous_status"] == "confirmée"
    assert _ctag(fake) != before
    # CANCELLED stays in the collection: no tombstone.
    assert fake.peek("dav_sync/dossier:d1/tombstones/h1") is None


def test_terminee_is_refused_on_a_future_day_only(fake):
    _hearing(fake)   # 15 October; today is frozen at 1 October
    before = _stored(fake)
    _refused(handlers.update_hearing, {"hearing_id": "h1",
                                       "status": "terminée"}, match="terminée")
    assert _stored(fake) == before
    _hearing(fake, "h2", start=_mtl(2026, 9, 20, 9))
    handlers.update_hearing({"hearing_id": "h2", "status": "terminée"})
    assert _stored(fake, "h2")["status"] == "terminée"


def test_notes_replace_append_and_their_ceiling(fake):
    _hearing(fake)
    handlers.update_hearing({"hearing_id": "h1",
                             "notes_append": "Salle 2.08."})
    notes = _stored(fake)["notes"]
    assert notes.startswith("Notes initiales.\n\n*Ajouté par Claude le ")
    assert notes.endswith("Salle 2.08.")

    handlers.update_hearing({"hearing_id": "h1", "notes": "Remplacées."})
    assert _stored(fake)["notes"] == "Remplacées."

    before = _stored(fake)
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "notes_append": "x" * 1990}, match="2000")
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "notes": "a", "notes_append": "b"},
             match="s'excluent")
    assert _stored(fake) == before


def test_an_append_repeats_so_the_tool_never_claims_idempotence(fake):
    """Two identical notes_append calls append twice — which is why
    update_hearing, unlike update_task, does NOT advertise idempotentHint:
    a client trusts the hint before a blind retry. The key is the retry
    armour, and a same-key retry appends once."""
    _hearing(fake)
    handlers.update_hearing({"hearing_id": "h1", "notes_append": "Ajout."})
    handlers.update_hearing({"hearing_id": "h1", "notes_append": "Ajout."})
    assert _stored(fake)["notes"].count("Ajout.") == 2
    descriptor = next(d for d in tools.list_tool_descriptors()
                      if d["name"] == "update_hearing")
    assert descriptor["annotations"]["idempotentHint"] is False

    _hearing(fake, "h2")
    args = {"hearing_id": "h2", "notes_append": "Une fois.",
            "idempotency_key": "cle-ajout-001"}
    handlers.update_hearing(dict(args))
    handlers.update_hearing(dict(args))
    assert _stored(fake, "h2")["notes"].count("Une fois.") == 1


def test_an_unsafe_link_is_refused_naming_the_field(fake):
    _hearing(fake)
    before = _stored(fake)
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "conference_uri": "javascript:alert(1)"},
             match="conference_uri")
    assert _stored(fake) == before
    payload = handlers.update_hearing(
        {"hearing_id": "h1", "conference_uri": "https://ex.com/v"})
    assert any("visioconférence" in w for w in payload["warnings"])
    payload = handlers.update_hearing(
        {"hearing_id": "h1", "modalite": "visioconférence",
         "reminder_minutes": 60})
    stored = _stored(fake)
    assert stored["modalite"] == "visioconférence"
    assert stored["reminder_minutes"] == 60
    assert not any("visioconférence" in w for w in payload["warnings"])


def test_a_reminder_outside_the_vocabulary_is_refused(fake):
    _hearing(fake)
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "reminder_minutes": 45},
             match="reminder_minutes")


# ══════════════════════════════════════════════════════════════════════
# 4. update_hearing — déplacement et séries
# ══════════════════════════════════════════════════════════════════════


def test_a_move_relocates_and_resnapshots_the_labels(fake):
    _hearing(fake)
    old, new = _ctag(fake, "d1"), _ctag(fake, "d2")
    payload = handlers.update_hearing({"hearing_id": "h1", "dossier_id": "d2"})
    stored = _stored(fake)
    assert stored["dossier_id"] == "d2"
    # What the phone prints, and what the Outlook mirror writes as « N/R ».
    assert stored["dossier_file_number"] == "2026-002"
    assert stored["dossier_title"] == "Dossier 2026-002"
    assert stored["dav_href"] == "/dav/dossier-d2/h1.ics"
    assert payload["moved"] is True
    assert payload["previous_collection_cleared"] is True
    assert fake.peek("dav_sync/dossier:d1/tombstones/h1") is not None
    assert _ctag(fake, "d1") != old and _ctag(fake, "d2") != new


def test_a_move_to_an_unknown_dossier_is_refused_never_downgraded(fake):
    _hearing(fake)
    before = _stored(fake)
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "dossier_id": "absent"}, match="introuvable")
    assert _stored(fake) == before


def test_a_series_occurrence_moves_only_detached_in_the_same_write(
    fake, monkeypatch,
):
    _hearing(fake, serie_id="s1", serie_rule={"freq": "hebdomadaire"})
    before = _stored(fake)
    _refused(handlers.update_hearing,
             {"hearing_id": "h1", "dossier_id": "d2"}, match="detach_from_series")
    assert _stored(fake) == before

    logged = []
    monkeypatch.setattr(handlers, "log_hearing_series_event",
                        lambda event, sid, **kw: logged.append((event, sid, kw)))
    fake.reset_logs()
    payload = handlers.update_hearing({"hearing_id": "h1", "dossier_id": "d2",
                                       "detach_from_series": True})
    stored = _stored(fake)
    assert stored["serie_id"] == "" and stored["serie_rule"] is None
    assert stored["dossier_id"] == "d2"
    assert payload["detached"] is True and payload["previous_serie_id"] == "s1"
    assert len(_hearing_commits(fake)) == 1     # ONE write, never two
    assert logged == [("series_unlinked", "s1",
                       {"hearing_id": "h1", "dossier_id": "d2",
                        "ctag_bumped": True, "via": "mcp"})]


def test_detaching_twice_is_a_safe_no_op(fake):
    _hearing(fake, serie_id="s1")
    handlers.update_hearing({"hearing_id": "h1", "detach_from_series": True})
    fake.reset_logs()
    again = handlers.update_hearing({"hearing_id": "h1",
                                     "detach_from_series": True})
    assert again["changed_fields"] == [] and again["detached"] is False
    assert any("aucune série" in w for w in again["warnings"])
    assert _hearing_commits(fake) == []


def test_a_same_key_retry_replays_without_writing_twice(fake):
    _hearing(fake)
    args = {"hearing_id": "h1", "title": "Révisé",
            "idempotency_key": "cle-update-1"}
    first = handlers.update_hearing(dict(args))
    fake.reset_logs()
    second = handlers.update_hearing(dict(args))
    assert second["idempotent_replay"] is True
    assert second["entity"]["etag"] == first["entity"]["etag"]
    assert _hearing_commits(fake) == []


# ══════════════════════════════════════════════════════════════════════
# 5. create_hearing — l'ajout additif
# ══════════════════════════════════════════════════════════════════════


def test_create_hearing_stores_and_echoes_the_new_fields(fake):
    payload = handlers.create_hearing({
        "dossier_id": "d1", "title": "Audience", "hearing_type": "audience",
        "date": "2026-11-03", "start_time": "09:30",
        "modalite": "visioconférence", "conference_uri": "https://ex.com/v",
        "reminder_minutes": 60, "status": "confirmée",
    })
    entity = payload["entity"]
    stored = _stored(fake, entity["id"])
    assert stored["modalite"] == "visioconférence"
    assert stored["conference_uri"] == "https://ex.com/v"
    assert stored["reminder_minutes"] == 60 and stored["status"] == "confirmée"
    assert (entity["status"], entity["modalite"], entity["reminder_minutes"]) == (
        "confirmée", "visioconférence", 60)
    assert entity["etag"] == stored["etag"]


def test_create_hearing_defaults_are_echoed(fake):
    payload = handlers.create_hearing({"title": "Rendez-vous",
                                       "date": "2026-11-03"})
    entity = payload["entity"]
    assert (entity["status"], entity["modalite"], entity["reminder_minutes"],
            entity["conference_uri"], entity["serie_id"], entity["source"]) == (
        "à_confirmer", "présentiel", 1440, "", "", "")


def test_create_hearing_refuses_what_a_creation_cannot_record(fake):
    _refused(handlers.create_hearing,
             {"title": "X", "date": "2026-11-03", "status": "annulée"},
             match="status")
    _refused(handlers.create_hearing,
             {"title": "X", "date": "2026-11-03",
              "conference_uri": "data:text/html,x"}, match="conference_uri")
    assert fake.peek_collection("hearings") == {}


# ══════════════════════════════════════════════════════════════════════
# 6. create_hearing_series
# ══════════════════════════════════════════════════════════════════════


def _series_args(**over) -> dict:
    args = {"dossier_id": "d1", "title": "Suivi", "date": "2026-10-26",
            "start_time": "09:00", "frequency": "hebdomadaire", "count": 3,
            "idempotency_key": "cle-serie-001"}
    args.update(over)
    return args


def test_a_series_demands_its_key_and_writes_nothing_without_it(fake):
    args = _series_args()
    del args["idempotency_key"]
    err = _refused(handlers.create_hearing_series, args,
                   match="idempotency_key")
    assert err.reason == "idempotency_required"
    assert fake.peek_collection("hearings") == {}


def test_a_series_is_one_atomic_batch_that_carries_its_own_bump(
    fake, monkeypatch,
):
    def _no_second_bump(*_a, **_k):
        raise AssertionError("the series bumps INSIDE its batch")

    monkeypatch.setattr(handlers, "bump_ctag", _no_second_bump)
    logged = []
    monkeypatch.setattr(handlers, "log_hearing_series_event",
                        lambda event, sid, **kw: logged.append((event, sid, kw)))
    fake.reset_logs()
    before = _ctag(fake)

    payload = handlers.create_hearing_series(_series_args())

    stored = fake.peek_collection("hearings")
    assert len(stored) == 3
    serie_ids = {doc["serie_id"] for doc in stored.values()}
    assert serie_ids == {payload["serie_id"]} == {payload["entity"]["id"]}
    # ONE commit: the three occurrences AND the CTag bump (the other commits
    # are the idempotency claim and its finalization).
    (commit,) = [c for c in fake.commits
                 if any(p.startswith("hearings/") for _k, p in c.ops)]
    paths = {p for _k, p in commit.ops}
    assert {f"hearings/{i}" for i in stored} | {"dav_sync/dossier:d1"} <= paths
    assert _ctag(fake) != before
    assert payload["ctag_bumped"] is True and payload["dav_synced"] is True
    assert payload["occurrences_count"] == 3
    assert [o["etag"] for o in payload["occurrences"]] == [
        stored[o["id"]]["etag"] for o in payload["occurrences"]]
    assert payload["rule_label"] == "Chaque semaine — 3 occurrences"
    # Every occurrence says the connector wrote it (models/provenance).
    assert {(d["created_via"], d["updated_via"]) for d in stored.values()} == {
        ("mcp", "mcp")}
    assert all(d.get("mcp_updated_at") for d in stored.values())
    assert logged == [("series_created", payload["serie_id"],
                       {"occurrences": 3, "dossier_id": "d1",
                        "frequence": "hebdomadaire", "ctag_bumped": True,
                        "via": "mcp"})]


def test_a_series_keeps_nine_o_clock_across_the_dst_change(fake):
    """26 octobre, 2 et 9 novembre 2026 : le passage à l'heure normale (1er
    novembre) tombe au milieu — « 9 h » reste 9 h à Montréal."""
    payload = handlers.create_hearing_series(_series_args())
    starts = [o["start"] for o in payload["occurrences"]]
    assert starts == ["2026-10-26T09:00:00-04:00",
                      "2026-11-02T09:00:00-05:00",
                      "2026-11-09T09:00:00-05:00"]


@pytest.mark.parametrize("over, fragment", [
    ({"count": None}, "EXACTEMENT"),
    ({"until": "2026-12-01"}, "EXACTEMENT"),
    ({"count": 61}, "count"),
])
def test_the_bounds_are_refused_naming_them(fake, over, fragment):
    args = _series_args(**over)
    if args.get("count") is None:
        args.pop("count")
    _refused(handlers.create_hearing_series, args, match=fragment)
    assert fake.peek_collection("hearings") == {}


def test_a_series_past_the_ceiling_is_refused_never_truncated(fake):
    args = _series_args(until="2028-12-31")
    args.pop("count")
    _refused(handlers.create_hearing_series, args, match="60 occurrences")
    assert fake.peek_collection("hearings") == {}


def test_a_same_key_series_retry_is_the_first_series(fake):
    first = handlers.create_hearing_series(_series_args())
    second = handlers.create_hearing_series(_series_args())
    assert second["idempotent_replay"] is True
    assert second["serie_id"] == first["serie_id"]
    assert len(fake.peek_collection("hearings")) == 3


def test_an_until_series_on_a_closed_dossier_says_so(fake):
    args = _series_args(dossier_id="d3", until="2026-11-09")
    args.pop("count")
    payload = handlers.create_hearing_series(args)
    assert payload["occurrences_count"] == 3
    assert payload["dav_synced"] is False
    assert any("fermé" in w for w in payload["warnings"])


# ══════════════════════════════════════════════════════════════════════
# 7. decide_rendez_vous — confirmer
# ══════════════════════════════════════════════════════════════════════


def _decide(hid="b1", *, key="cle-decision-1", etag=None, **over) -> dict:
    args = {"hearing_id": hid, "action": "confirmer",
            "expected_etag": etag if etag is not None else f"e-{hid}",
            "idempotency_key": key}
    args.update(over)
    return args


def test_confirming_links_the_contact_and_enters_dav(fake, graph):
    _booking(fake)
    _partie(fake)
    before = _ctag(fake, "")
    payload = handlers.decide_rendez_vous(_decide())
    stored = _stored(fake, "b1")
    assert stored["confirmation"] == "" and stored["partie_id"] == "p1"
    assert stored["updated_via"] == "mcp"
    assert payload["changed"] is True and payload["partie_liee"] is True
    assert payload["partie_id"] == "p1"
    assert payload["ctag_bumped"] is True and _ctag(fake, "") != before
    assert payload["entity"]["confirmation"] == ""
    assert payload["entity"]["etag"] == stored["etag"]
    assert payload["client_notified"] is False and graph == []


def test_confirming_without_a_matching_contact_is_refused_then_allowed(
    fake, graph,
):
    _booking(fake)
    before = _stored(fake, "b1")
    _refused(handlers.decide_rendez_vous, _decide(), match="lier_partie")
    assert _stored(fake, "b1") == before
    # The key was released by the refusal: the corrected call may reuse it.
    payload = handlers.decide_rendez_vous(_decide(lier_partie=False))
    assert _stored(fake, "b1")["confirmation"] == ""
    assert payload["partie_liee"] is False
    assert handlers._NO_CONTACT_LINKED in payload["warnings"]


def test_a_client_cancelled_request_cannot_be_confirmed(fake, graph):
    _booking(fake, confirmation="annulée_client")
    _refused(handlers.decide_rendez_vous, _decide(lier_partie=False),
             match="refuser")


def test_the_key_and_the_etag_are_both_demanded(fake, graph):
    _booking(fake)
    args = _decide()
    del args["idempotency_key"]
    assert _refused(handlers.decide_rendez_vous, args).reason == (
        "idempotency_required")
    args = _decide()
    del args["expected_etag"]
    _refused(handlers.decide_rendez_vous, args, match="expected_etag")
    assert _stored(fake, "b1")["confirmation"] == "à_confirmer"


def test_only_a_bookings_request_is_decided_here(fake, graph):
    _hearing(fake)
    _refused(handlers.decide_rendez_vous, _decide("h1"), match="update_hearing")


# ══════════════════════════════════════════════════════════════════════
# 8. decide_rendez_vous — refuser (l'effet sortant)
# ══════════════════════════════════════════════════════════════════════


def test_a_refusal_sends_only_the_fixed_cancellation_text(fake, graph):
    """The connector's ONE outbound effect (plan D10; the « client_message »
    never of mcp/disclosure.NEVERS). No input property can carry text to
    the client — the only free strings are two identifiers and the key —
    and what Outlook receives is REFUS_MOTIF, verbatim, whatever is sent."""
    schema = tools.TOOLS["decide_rendez_vous"]["input_schema"]["properties"]
    free_strings = {k for k, v in schema.items()
                    if v.get("type") == "string" and "enum" not in v}
    assert free_strings == {"hearing_id", "expected_etag", "idempotency_key"}
    assert schema["hearing_id"]["maxLength"] == 64

    _booking(fake)
    before = _ctag(fake, "")
    payload = handlers.decide_rendez_vous(
        _decide(action="refuser", key="Écrivez-lui : désolé, je pars en vacances"))

    assert graph == [("EVT-b1", rendez_vous.REFUS_MOTIF)]
    assert _stored(fake, "b1")["confirmation"] == "refusée"
    assert payload["graph_cancelled"] is True
    assert payload["client_notified"] is True
    assert payload["cancellation_message"] == rendez_vous.REFUS_MOTIF
    assert payload["local_written"] is True
    # A pending request was never on the phone: nothing to sync.
    assert payload["ctag_bumped"] is False and _ctag(fake, "") == before


def test_a_stale_refusal_never_reaches_outlook(fake, graph):
    _booking(fake)
    err = _refused(handlers.decide_rendez_vous,
                   _decide(action="refuser", etag="vieille"))
    assert err.reason == "stale_etag" and "list_hearings" in str(err)
    assert graph == []
    assert _stored(fake, "b1")["confirmation"] == "à_confirmer"


def test_a_same_key_refusal_retry_never_calls_outlook_twice(fake, graph):
    _booking(fake)
    handlers.decide_rendez_vous(_decide(action="refuser"))
    replay = handlers.decide_rendez_vous(_decide(action="refuser"))
    assert replay["idempotent_replay"] is True
    assert len(graph) == 1


def test_refusing_again_with_another_key_is_a_no_op(fake, graph):
    _booking(fake)
    handlers.decide_rendez_vous(_decide(action="refuser"))
    again = handlers.decide_rendez_vous(
        _decide(action="refuser", key="cle-decision-2"))
    assert again["changed"] is False and len(graph) == 1
    assert handlers._DECISION_ALREADY_STORED in again["warnings"]


def test_a_local_failure_after_the_cancellation_is_a_warned_success(
    fake, graph,
):
    """The client HAS been notified: an error here would invite a retry
    that calls Outlook again. So: success, the warning, local_written
    false — and the SAME key replays it without a second Graph call."""
    _booking(fake)
    _fail_hearing_commits(fake)
    payload = handlers.decide_rendez_vous(_decide(action="refuser"))
    assert graph == [("EVT-b1", rendez_vous.REFUS_MOTIF)]
    assert payload["graph_cancelled"] is True
    assert payload["local_written"] is False
    assert payload["entity"]["confirmation"] == "à_confirmer"
    assert handlers._REFUS_NON_INSCRIT_MCP in payload["warnings"]
    assert _stored(fake, "b1")["confirmation"] == "à_confirmer"

    replay = handlers.decide_rendez_vous(_decide(action="refuser"))
    assert replay["idempotent_replay"] is True
    assert len(graph) == 1


def test_an_outlook_failure_still_refuses_with_the_manual_warning(
    fake, monkeypatch,
):
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)

    def failing(gid, motif=""):
        raise GraphError("down")

    monkeypatch.setattr(rendez_vous.graph_calendrier, "annuler_reservation",
                        failing)
    _booking(fake)
    payload = handlers.decide_rendez_vous(_decide(action="refuser"))
    assert _stored(fake, "b1")["confirmation"] == "refusée"
    assert payload["graph_attempted"] is True
    assert payload["graph_cancelled"] is False
    assert payload["client_notified"] is False
    assert payload["cancellation_message"] is None
    assert rendez_vous.ANNULATION_OUTLOOK_ECHOUEE in payload["warnings"]


def test_removing_a_client_cancelled_request_never_calls_outlook(fake, graph):
    _booking(fake, confirmation="annulée_client")
    payload = handlers.decide_rendez_vous(_decide(action="refuser"))
    assert graph == [] and payload["client_notified"] is False
    assert _stored(fake, "b1")["confirmation"] == "refusée"


def test_a_confirmed_rendez_vous_is_not_refused_here(fake, graph):
    _booking(fake, confirmation="")
    _refused(handlers.decide_rendez_vous, _decide(action="refuser"),
             match="update_hearing")
    assert graph == []


def test_lier_partie_belongs_to_confirmer_only(fake, graph):
    _booking(fake)
    _refused(handlers.decide_rendez_vous,
             _decide(action="refuser", lier_partie=True), match="lier_partie")
    assert graph == []


# ══════════════════════════════════════════════════════════════════════
# 9. list_hearings — les deux modes
# ══════════════════════════════════════════════════════════════════════


def test_the_pending_mode_lists_the_requests_awaiting_a_decision(fake):
    _booking(fake, "b1")
    _booking(fake, "b2", confirmation="annulée_client",
             start=_mtl(2026, 10, 16, 9))
    _booking(fake, "b3", confirmation="refusée")
    _booking(fake, "b4", confirmation="")
    _hearing(fake)
    _partie(fake)
    payload = handlers.list_hearings({"bookings": "pending"})
    assert payload["mode"] == "bookings_pending"
    assert payload["window"] == {"from": None, "to": None}
    rows = payload["items"]
    assert [r["id"] for r in rows] == ["b1", "b2"]
    assert rows[0]["confirmation"] == "à_confirmer"
    assert rows[1]["confirmation"] == "annulée_client"
    assert rows[0]["client_nom"] == "Jean Tremblay"
    assert rows[0]["client_email"] == "client@ex.com"
    assert rows[0]["partie_suggeree_id"] == "p1"
    assert rows[0]["etag"] == "e-b1"


def test_an_unreadable_pending_list_is_a_refusal_never_an_empty_list(
    fake, monkeypatch,
):
    _booking(fake)
    _fail_queries_on(monkeypatch, fake, "hearings")
    _refused(handlers.list_hearings, {"bookings": "pending"},
             match=rendez_vous.LECTURE_IMPOSSIBLE)


def test_the_serie_mode_lists_the_whole_chain_past_included(fake):
    _hearing(fake, "h1", start=_mtl(2026, 9, 1, 9), serie_id="s1")
    _hearing(fake, "h2", start=_mtl(2026, 9, 8, 9), serie_id="s1")
    _hearing(fake, "h3", start=_mtl(2026, 9, 8, 9), serie_id="autre")
    payload = handlers.list_hearings({"serie_id": "s1"})
    assert payload["mode"] == "serie"
    assert [r["id"] for r in payload["items"]] == ["h1", "h2"]
    assert all(r["serie_id"] == "s1" for r in payload["items"])
    assert all("client_nom" not in r for r in payload["items"])


def test_the_serie_mode_pages_on_the_agenda_key(fake):
    for i in range(5):
        _hearing(fake, f"h{i}", start=_mtl(2026, 9, 1 + i, 9), serie_id="s1")
    first = handlers.list_hearings({"serie_id": "s1", "limit": 2})
    assert [r["id"] for r in first["items"]] == ["h0", "h1"]
    assert first["truncated"] is True
    second = handlers.list_hearings({"serie_id": "s1", "limit": 2,
                                     "cursor": first["next_cursor"]})
    assert [r["id"] for r in second["items"]] == ["h2", "h3"]


def test_an_unreadable_serie_is_a_refusal(fake, monkeypatch):
    _fail_queries_on(monkeypatch, fake, "hearings")
    _refused(handlers.list_hearings, {"serie_id": "s1"}, match="série")


@pytest.mark.parametrize("args, fragment", [
    ({"bookings": "pending", "serie_id": "s1"}, "s'excluent"),
    ({"bookings": "pending", "date_from": "2026-10-01"}, "date_from"),
    ({"serie_id": "s1", "dossier_id": "d1"}, "dossier_id"),
    ({"serie_id": ""}, "vide"),
])
def test_contradictory_modes_are_refused(fake, args, fragment):
    _refused(handlers.list_hearings, args, match=fragment)


def test_the_window_rows_carry_the_new_keys_and_never_a_requester(fake):
    _hearing(fake, start=datetime.now(UTC) + timedelta(days=3),
             serie_id="s1", reminder_minutes=60)
    _booking(fake, "b4", confirmation="",
             start=datetime.now(UTC) + timedelta(days=4))
    payload = handlers.list_hearings({})
    assert payload["mode"] == "window"
    rows = {r["id"]: r for r in payload["items"]}
    assert rows["h1"]["serie_id"] == "s1"
    assert rows["h1"]["reminder_minutes"] == 60
    assert rows["h1"]["source"] == "" and rows["b4"]["source"] == "bookings"
    for row in rows.values():
        assert not set(row) & {"confirmation", "client_nom", "client_email",
                               "partie_suggeree_id", "partie_suggeree_nom"}


# ══════════════════════════════════════════════════════════════════════
# 10. Les gardes dérivées du lot
# ══════════════════════════════════════════════════════════════════════


def _mcp_sources() -> dict[str, str]:
    root = _ATHENA / "mcp"
    return {p.name: p.read_text(encoding="utf-8")
            for p in sorted(root.glob("*.py"))}


def test_the_only_connector_path_to_pending_requests_is_the_pending_mode():
    """The MCP reads never show an unconfirmed Bookings import (the
    include_unconfirmed contract) — EXCEPT through list_hearings' pending
    mode, which reads through the service's STRICT reader. Derived: no
    connector module passes include_unconfirmed=True, and the pending
    reader is reached from exactly one handler function."""
    readers = {"lister_en_attente", "list_bookings_strict",
               "list_bookings_all"}
    reached_from: set[str] = set()
    for name, source in _mcp_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "include_unconfirmed":
                # Only the explicit default may appear; anything else opens
                # the gate the pending mode alone may open.
                assert (isinstance(node.value, ast.Constant)
                        and node.value.value is False), (
                    f"{name} passes include_unconfirmed")
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Attribute) and sub.attr in readers:
                    reached_from.add(f"{name}:{fn.name}:{sub.attr}")
    assert reached_from == {"handlers.py:_hearing_selection:lister_en_attente"}


def test_the_bookings_consent_partial_quotes_the_text_actually_sent():
    """The consent screen names the text the client receives: it must be
    REFUS_MOTIF, or the page the lawyer read would not describe the
    effect."""
    partial = (_ATHENA / "templates" / "mcp" / "families"
               / "_bookings.html").read_text(encoding="utf-8")
    rendered = " ".join(partial.replace("&nbsp;", " ").split())
    assert rendez_vous.REFUS_MOTIF in rendered


def test_series_and_decision_never_quote_notes_in_their_results(fake, graph):
    """A write result is stored verbatim for 24 h (mcp_idempotency): the
    event's notes never ride in it."""
    _booking(fake)
    decided = handlers.decide_rendez_vous(_decide(action="refuser"))
    series = handlers.create_hearing_series(_series_args(notes="Privilégié."))
    _hearing(fake)
    updated = handlers.update_hearing({"hearing_id": "h1",
                                       "notes": "Privilégié aussi."})
    for payload in (decided, series, updated):
        assert "Privilégié" not in repr(payload)


def test_the_agenda_partial_reads_the_series_ceiling_from_the_registry():
    """The consent screen states the series ceiling from utils/recurrence,
    never a typed « 60 » that a change of the ceiling would leave false."""
    import jinja2

    from mcp import disclosure

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(
        str(_ATHENA / "templates")))
    context = disclosure.consent_context(comptabilite_offered=False)
    rendered = " ".join(env.get_template("mcp/families/_agenda.html").render(
        disclosure=context).split())
    assert (f"au plus {recurrence.MAX_SERIE_OCCURRENCES} occurrences"
            in rendered)
