"""Audiences — les fenêtres de lecture jugent une audience par son JOUR CIVIL
(lot 1a, étape L1).

Une audience « toute la journée » est rangée à minuit UTC de son jour — 20 h
(HAE) ou 19 h (HNE) la VEILLE à Montréal. Quatre fenêtres de lecture
s'ouvraient à un instant qui la manquait :

* le ``get_agenda`` du connecteur (le breffage de 7 h) s'ouvrait à minuit
  MONTRÉAL (04 h/05 h UTC), APRÈS elle : les audiences d'une journée entière
  du jour n'y figuraient jamais ;
* la liste ``/audiences`` et le tableau de bord s'ouvraient à l'instant
  « maintenant » : elles disparaissaient dès 20 h la veille ;
* l'onglet « Calendrier » du dossier comparait ``to_mtl(début)`` au jour :
  elle disparaissait LE JOUR MÊME.

La règle unique est ``models.hearing.occurrence_day`` (date UTC pour une
journée entière, date de Montréal pour une audience horodatée) : lire depuis
``civil_day_floor`` (minuit UTC, le plus précoce des deux débuts d'une
journée) et garder les lignes dont le jour civil est aujourd'hui ou plus
tard. Épinglé contre le vrai client Firestore (``tests/_fake_firestore.py``),
« aujourd'hui » gelé, avec une audience horodatée de 21 h (dont la date UTC
est le lendemain) et les deux changements d'heure.
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

from flask import Flask  # noqa: E402

from tz import MTL, mtl_to_utc, to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import models.hearing as hm
    import routes.dashboard as dashboard
    import routes.dossiers as rd
    import routes.hearings as rh

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = date(2026, 10, 15)          # a Thursday, EDT (UTC-4)


def _hearing(hid: str, start: datetime, *, all_day: bool = False,
             dossier_id: str = "d1", status: str = "confirmée") -> dict:
    end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    return {
        "id": hid, "title": f"T-{hid}", "hearing_type": "audience",
        "status": status, "all_day": all_day,
        "start_datetime": start, "end_datetime": end,
        "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "confirmation": "",
        "source": "", "etag": f"e-{hid}", "vevent_uid": f"u-{hid}",
    }


def _mtl(y, m, d, hh, mm=0) -> datetime:
    return mtl_to_utc(datetime(y, m, d, hh, mm))


# The world every surface below reads, around TODAY = 15 October.
WORLD = [
    # Today's all-day hearing: 00:00 UTC on the 15th = 20:00 on the 14th in
    # Montréal. THE row every surface lost.
    _hearing("allday-today", datetime(2026, 10, 15, tzinfo=UTC), all_day=True),
    # Yesterday 21:00 Montréal = 01:00 UTC on the 15th: its UTC date is
    # today and the widened read brings it back — its civil day is the
    # 14th, so it must NOT be listed as today's.
    _hearing("timed-yesterday-21h", _mtl(2026, 10, 14, 21)),
    # Today 09:00 and 21:00 Montréal (the latter is 01:00 UTC on the 16th).
    _hearing("timed-today-9h", _mtl(2026, 10, 15, 9)),
    _hearing("timed-today-21h", _mtl(2026, 10, 15, 21)),
    # Yesterday's and tomorrow's all-day hearings.
    _hearing("allday-yesterday", datetime(2026, 10, 14, tzinfo=UTC),
             all_day=True),
    _hearing("allday-tomorrow", datetime(2026, 10, 16, tzinfo=UTC),
             all_day=True),
]
TODAY_OR_LATER = {"allday-today", "timed-today-9h", "timed-today-21h",
                  "allday-tomorrow"}
BEFORE_TODAY = {"timed-yesterday-21h", "allday-yesterday"}


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, hm)
    fake.seed_collection("hearings", {h["id"]: h for h in WORLD})
    return fake


# ══════════════════════════════════════════════════════════════════════
# 1. The rule itself — pure
# ══════════════════════════════════════════════════════════════════════


def test_the_floor_is_midnight_utc_before_midnight_montreal():
    floor = hm.civil_day_floor(TODAY)
    assert floor == datetime(2026, 10, 15, tzinfo=UTC)
    assert floor < datetime(2026, 10, 15, tzinfo=MTL)


@pytest.mark.parametrize("day, expected", [
    # Fall back: 1 November 2026 02:00 EDT → 01:00 EST.
    (date(2026, 10, 31), datetime(2026, 11, 1, 4, tzinfo=UTC)),   # EDT
    (date(2026, 11, 1), datetime(2026, 11, 2, 5, tzinfo=UTC)),    # EST
    # Spring forward: 14 March 2027 02:00 EST → 03:00 EDT.
    (date(2027, 3, 13), datetime(2027, 3, 14, 5, tzinfo=UTC)),    # EST
    (date(2027, 3, 14), datetime(2027, 3, 15, 4, tzinfo=UTC)),    # EDT
])
def test_the_ceiling_is_midnight_montreal_across_both_dst_changes(day, expected):
    assert hm.civil_day_ceiling(day) == expected


def test_a_21h_hearing_belongs_to_its_montreal_day_not_its_utc_date():
    h = _hearing("x", _mtl(2026, 10, 14, 21))
    assert h["start_datetime"].date() == date(2026, 10, 15)
    assert hm.occurrence_day(h) == date(2026, 10, 14)


def test_on_or_after_and_before_partition_the_dated_rows():
    after = {h["id"] for h in hm.on_or_after_day(WORLD, TODAY)}
    before = {h["id"] for h in hm.before_day(WORLD, TODAY)}
    assert after == TODAY_OR_LATER
    assert before == BEFORE_TODAY
    assert not after & before


def test_a_row_without_a_start_is_kept_upcoming_never_past():
    row = {"id": "nostart"}
    assert hm.on_or_after_day([row], TODAY) == [row]
    assert hm.before_day([row], TODAY) == []


# ══════════════════════════════════════════════════════════════════════
# 2. MCP get_agenda — the 07:00 briefing
# ══════════════════════════════════════════════════════════════════════


def _agenda_stubs(monkeypatch, today: date) -> None:
    """Everything get_agenda reads EXCEPT the hearings, which come from the
    fake store through the real list_hearings_in_range."""
    monkeypatch.setattr(handlers.deadlines, "today_mtl", lambda: today)
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk",
                        lambda ids: {})
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 0)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 0.0, "amount": 0})
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda **kw: {"amount": 0})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 0)


class _SameClass(type):
    """Keeps ``isinstance(real_datetime, datetime)`` true inside the module
    whose ``datetime`` name is swapped (the handlers test values with it)."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL_DATETIME)


_REAL_DATETIME = datetime


def _freeze_now(monkeypatch, module, instant: datetime) -> None:
    """Freeze ``datetime.now`` as *module* sees it (get_agenda's upper bound
    is the instant `now + days_ahead`)."""
    class _Frozen(_REAL_DATETIME, metaclass=_SameClass):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(module, "datetime", _Frozen)


def test_get_agenda_lists_todays_all_day_hearing(fake, monkeypatch):
    """THE defect: at 07:00 the window opened at 04:00 UTC, after today's
    all-day hearing (00:00 UTC) — the briefing never listed it."""
    _agenda_stubs(monkeypatch, TODAY)
    _freeze_now(monkeypatch, handlers, _mtl(2026, 10, 15, 7))
    payload = handlers.get_agenda({"days_ahead": 7})
    ids = [h["id"] for h in payload["hearings"]]
    assert set(ids) == TODAY_OR_LATER
    # Chronological by stored instant: the all-day one first.
    assert ids[0] == "allday-today"
    row = next(h for h in payload["hearings"] if h["id"] == "allday-today")
    assert row["all_day"] is True


def test_get_agenda_at_the_evening_before_does_not_list_tomorrow_as_today(
    fake, monkeypatch
):
    """At 21:00 on the 14th (01:00 UTC on the 15th) Montréal is still on the
    14th: the 15th's all-day hearing is tomorrow's, the 14th's is today's."""
    _agenda_stubs(monkeypatch, date(2026, 10, 14))
    _freeze_now(monkeypatch, handlers, _mtl(2026, 10, 14, 21))
    payload = handlers.get_agenda({"days_ahead": 7})
    ids = {h["id"] for h in payload["hearings"]}
    assert "allday-yesterday" in ids            # the 14th's, seen on the 14th
    assert "timed-yesterday-21h" in ids
    assert payload["window"]["from"] == "2026-10-14"


def test_get_agenda_keeps_an_all_day_hearing_of_the_day_after_to_out(
    fake, monkeypatch
):
    """The read runs to civil_day_ceiling(`to`) (finitions, sync-5 — it
    stopped at the instant `now + days_ahead` before), which admits the next
    day's all-day hearing (stored at midnight UTC, before midnight
    Montréal). A row whose civil day is after the window's own `to` is not
    listed under it."""
    _agenda_stubs(monkeypatch, date(2026, 10, 14))
    _freeze_now(monkeypatch, handlers, _mtl(2026, 10, 14, 21))
    payload = handlers.get_agenda({"days_ahead": 1})
    assert payload["window"]["to"] == "2026-10-15"
    ids = {h["id"] for h in payload["hearings"]}
    assert "allday-tomorrow" not in ids         # the 16th: after `to`
    assert "allday-today" in ids                # the 15th: inside


def test_get_agenda_lists_every_hearing_of_its_last_day(fake, monkeypatch):
    """Finitions, sync-5. The window reports `to` = the 16th; the read used
    to stop at the INSTANT now + days_ahead (07:00 on the 16th), dropping a
    09:30 hearing of the day it claimed to cover whole."""
    fake.seed("hearings/timed-tomorrow-930",
              _hearing("timed-tomorrow-930", _mtl(2026, 10, 16, 9, 30)))
    fake.seed("hearings/timed-day-after-8h",
              _hearing("timed-day-after-8h", _mtl(2026, 10, 17, 8)))
    _agenda_stubs(monkeypatch, TODAY)
    _freeze_now(monkeypatch, handlers, _mtl(2026, 10, 15, 7))
    payload = handlers.get_agenda({"days_ahead": 1})
    assert payload["window"]["to"] == "2026-10-16"
    ids = {h["id"] for h in payload["hearings"]}
    assert "timed-tomorrow-930" in ids          # on `to`, after 07:00
    assert "allday-tomorrow" in ids
    assert "timed-day-after-8h" not in ids      # the 17th: after `to`


@pytest.mark.parametrize("hour", [7, 21])
def test_get_agenda_bounds_tasks_and_steps_by_the_window_s_last_day(
        monkeypatch, hour):
    """A task or step deadline is DATE-ONLY (midnight UTC). Bounded by the
    instant now + days_ahead, the evening band (21:00 on the 15th is 01:00
    UTC on the 16th) let in a deadline of the 17th under `to` = the 16th."""
    seen = {}
    _agenda_stubs(monkeypatch, TODAY)
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [])
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: seen.setdefault("tasks", cutoff) and [])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: seen.setdefault("steps", cutoff) and [])
    _freeze_now(monkeypatch, handlers, _mtl(2026, 10, 15, hour))
    payload = handlers.get_agenda({"days_ahead": 1})
    assert payload["window"]["to"] == "2026-10-16"
    bound = seen["tasks"]
    assert seen == {"tasks": bound, "steps": bound}
    # A deadline of `to` is inside — at its midnight UTC, and LATER that
    # UTC day too: a jtx client can give a task a time (review of the
    # finitions — a bound at midnight dropped a 14:00 UTC deadline from the
    # day its row reports). One of the day after is not.
    assert datetime(2026, 10, 16, tzinfo=UTC) <= bound
    assert datetime(2026, 10, 16, 14, tzinfo=UTC) <= bound
    assert datetime(2026, 10, 16, 23, 59, 59, tzinfo=UTC) <= bound
    assert datetime(2026, 10, 17, tzinfo=UTC) > bound


def test_get_agenda_lists_a_timed_task_due_later_on_its_last_day(
        fake, monkeypatch):
    """Review of the finitions (sync-5): the task bound is the END of `to`'s
    UTC day, through the real list_urgent_tasks query — a task due at
    14:00 UTC on `to` (a DATE-TIME DUE from a jtx client) is listed under
    it; one due at midnight UTC the day after is not."""
    install(monkeypatch, handlers.task_model, fake=fake)
    real_urgent_tasks = handlers.task_model.list_urgent_tasks
    for tid, due in (("timed-on-to", datetime(2026, 10, 16, 14, tzinfo=UTC)),
                     ("next-day", datetime(2026, 10, 17, tzinfo=UTC))):
        fake.seed(f"tasks/{tid}", {
            "id": tid, "title": "Tâche", "status": "à_faire",
            "priority": "normale", "category": "autre", "dossier_id": None,
            "due_date": due,
        })
    _agenda_stubs(monkeypatch, TODAY)
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        real_urgent_tasks)
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: [])
    _freeze_now(monkeypatch, handlers, _mtl(2026, 10, 15, 7))
    payload = handlers.get_agenda({"days_ahead": 1})
    assert payload["window"]["to"] == "2026-10-16"
    ids = {t["id"] for t in payload["urgent_tasks"]}
    assert "timed-on-to" in ids
    assert "next-day" not in ids


def test_get_agenda_across_the_fall_back(monkeypatch):
    """2 November 2026 is EST (UTC-5): midnight Montréal is 05:00 UTC. A
    hearing at 23:30 on the 1st (04:30 UTC on the 2nd) belongs to the 1st —
    a fixed four-hour offset would have called it the 2nd's."""
    fake = install(monkeypatch, hm)
    fake.seed_collection("hearings", {h["id"]: h for h in [
        _hearing("allday-nov2", datetime(2026, 11, 2, tzinfo=UTC),
                 all_day=True),
        _hearing("late-nov1", _mtl(2026, 11, 1, 23, 30)),
        _hearing("morning-nov2", _mtl(2026, 11, 2, 8)),
    ]})
    assert _mtl(2026, 11, 1, 23, 30) == datetime(2026, 11, 2, 4, 30,
                                                 tzinfo=UTC)
    _agenda_stubs(monkeypatch, date(2026, 11, 2))
    _freeze_now(monkeypatch, handlers, _mtl(2026, 11, 2, 7))
    ids = {h["id"] for h in handlers.get_agenda({"days_ahead": 3})["hearings"]}
    assert ids == {"allday-nov2", "morning-nov2"}


# ══════════════════════════════════════════════════════════════════════
# 3. The dashboard's short-term window
# ══════════════════════════════════════════════════════════════════════


def test_dashboard_keeps_todays_all_day_hearing_from_the_evening_before(fake):
    """The short-term window opened at the instant `now`: from 20:00 on the
    14th (00:00 UTC on the 15th) the 15th's all-day hearing was gone — on
    the evening before it, and on the whole day itself."""
    now = _mtl(2026, 10, 15, 10)
    rows = dashboard._get_hearings_from_day(TODAY, now + timedelta(days=7))
    assert {h["id"] for h in rows} == TODAY_OR_LATER


def test_dashboard_short_term_excludes_cancelled(fake):
    fake.seed("hearings/cancelled", _hearing(
        "cancelled", _mtl(2026, 10, 15, 11), status="annulée"))
    rows = dashboard._get_hearings_from_day(
        TODAY, _mtl(2026, 10, 22, 10))
    assert "cancelled" not in {h["id"] for h in rows}


def test_dashboard_index_reads_the_short_term_window_from_the_civil_day(
    fake, monkeypatch
):
    """The route itself, not only its helper: index() must hand the
    template the civil-day window. On the old code it read from the instant
    `now` (10:00 on the 15th), after today's all-day hearing (00:00 UTC) and
    today's 09:00 one — a helper test alone would stay green if the route
    went back to _get_hearings_in_range(now, …)."""
    _freeze_now(monkeypatch, dashboard, _mtl(2026, 10, 15, 10))
    for name, value in (("_get_urgent_tasks", []),
                        ("_get_urgent_protocol_steps", []),
                        ("_get_prescription_alerts", []),
                        ("_get_quick_stats", {})):
        monkeypatch.setattr(dashboard, name, lambda *a, _v=value: _v)
    seen = {}
    monkeypatch.setattr(dashboard, "render_template",
                        lambda name, **ctx: seen.update(ctx) or "")
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(dashboard.dashboard_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    assert c.get("/").status_code == 200
    assert {h["id"] for h in seen["short_term_hearings"]} == TODAY_OR_LATER


# ══════════════════════════════════════════════════════════════════════
# 4. The /audiences list
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def hearings_client(fake, monkeypatch):
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.register_blueprint(rh.hearings_bp)
    monkeypatch.setattr(rh, "today_mtl", lambda: TODAY)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


def _listed(client, query: str = "") -> str:
    resp = client.get(f"/audiences/{query}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_audiences_list_shows_todays_all_day_hearing(hearings_client):
    body = _listed(hearings_client)
    for hid in TODAY_OR_LATER:
        assert f"T-{hid}" in body, hid
    for hid in BEFORE_TODAY:
        assert f"T-{hid}" not in body, hid


def test_audiences_list_filtered_puts_each_row_on_exactly_one_side(
    hearings_client, monkeypatch
):
    """Under a filter the list reads BOTH windows; the rows they share
    (yesterday evening's timed hearing, today's all-day one) must land on
    exactly one side — never twice, never nowhere."""
    seen = {}
    real = rh.render_template

    def _capture(name, **ctx):
        seen.update(ctx)
        return real(name, **ctx)

    monkeypatch.setattr(rh, "render_template", _capture)
    _listed(hearings_client, "?status=confirmée")
    upcoming = [h["id"] for h in seen["upcoming"]]
    past = [h["id"] for h in seen["past"]]
    assert set(upcoming) == TODAY_OR_LATER
    assert set(past) == BEFORE_TODAY
    assert len(upcoming) == len(set(upcoming))
    assert len(past) == len(set(past))


def test_the_month_grid_places_every_row_on_its_civil_day(
    fake, hearings_client, monkeypatch
):
    """The month view read [1st 00:00 UTC, next 1st 00:00 UTC] and keyed
    its cells on to_mtl(start).day: every all-day hearing sat one cell
    early — the 1st's in cell 30 of the SAME month, the next month's 1st in
    cell 31 —, the previous month's last evening (21:00 on 30 Sep = 01:00
    UTC on 1 Oct) landed in cell 30, and this month's last evening after
    20:00 was missing."""
    fake.seed_collection("hearings", {h["id"]: h for h in [
        _hearing("allday-oct1", datetime(2026, 10, 1, tzinfo=UTC),
                 all_day=True),
        _hearing("sep30-21h", _mtl(2026, 9, 30, 21)),
        _hearing("oct31-21h", _mtl(2026, 10, 31, 21)),
        _hearing("allday-nov1", datetime(2026, 11, 1, tzinfo=UTC),
                 all_day=True),
    ]})
    seen = {}
    real = rh.render_template

    def _capture(name, **ctx):
        seen.update(ctx)
        return real(name, **ctx)

    monkeypatch.setattr(rh, "render_template", _capture)
    _listed(hearings_client, "?view=month&month=2026-10")
    cell_of = {h["id"]: day for day, rows in seen["day_hearings"].items()
               for h in rows}
    assert cell_of == {
        "allday-oct1": 1, "oct31-21h": 31,
        # The WORLD fixture, around 15 October.
        "allday-yesterday": 14, "timed-yesterday-21h": 14,
        "allday-today": 15, "timed-today-9h": 15, "timed-today-21h": 15,
        "allday-tomorrow": 16,
    }
    assert {h["id"] for h in seen["hearings"]} == set(cell_of)


# ══════════════════════════════════════════════════════════════════════
# 5. The dossier « Calendrier » tab
# ══════════════════════════════════════════════════════════════════════


def test_dossier_tab_shows_an_all_day_hearing_on_its_own_day(fake, monkeypatch):
    """The tab compared to_mtl(start) — the evening BEFORE for an all-day
    hearing — to today, and hid it on its own day."""
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.register_blueprint(rd.dossiers_bp)
    app.register_blueprint(rh.hearings_bp)
    monkeypatch.setattr(rd, "get_dossier", lambda i: {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif"})
    monkeypatch.setattr(rd, "_attach_prescription_warnings", lambda rows: None)
    monkeypatch.setattr(rd.deadlines, "today_mtl", lambda: TODAY)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    resp = c.get("/dossiers/d1/tab/audiences", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for hid in TODAY_OR_LATER:
        assert f"T-{hid}" in body, hid
    for hid in BEFORE_TODAY:
        assert f"T-{hid}" not in body, hid


# ══════════════════════════════════════════════════════════════════════
# 6. The MCP get_dossier hearing counts (hearing_model.get_hearing_summary)
# ══════════════════════════════════════════════════════════════════════


def test_the_dossier_hearing_counts_split_by_civil_day(monkeypatch):
    """A FIFTH « from today on » reader the four windows above missed: the
    counts behind get_dossier's summaries.hearings compared the start with
    the instant « now ». At 21:00 on the 14th (01:00 UTC on the 15th) the
    15th's all-day hearing — TOMORROW's — was counted « past », and the
    14th's 09:00 hearing too, while the dossier tab lists both as upcoming.
    """
    _freeze_now(monkeypatch, hm, _mtl(2026, 10, 14, 21))
    monkeypatch.setattr(hm.deadlines, "today_mtl", lambda: date(2026, 10, 14))
    fake = install(monkeypatch, hm)
    fake.seed_collection("hearings", {h["id"]: h for h in [
        _hearing("allday-15", datetime(2026, 10, 15, tzinfo=UTC),
                 all_day=True),
        _hearing("timed-14-9h", _mtl(2026, 10, 14, 9)),
        _hearing("allday-13", datetime(2026, 10, 13, tzinfo=UTC),
                 all_day=True),
        _hearing("cancelled-16", _mtl(2026, 10, 16, 9), status="annulée"),
        _hearing("done-14", _mtl(2026, 10, 14, 8), status="terminée"),
        {**_hearing("nostart", _mtl(2026, 10, 20, 9)),
         "start_datetime": None},
    ]})
    summary = hm.get_hearing_summary("d1")
    assert summary == {"total": 6, "upcoming": 2, "past": 2}


# ══════════════════════════════════════════════════════════════════════
# 7. The date LABELS — the display half of the same rule
# ══════════════════════════════════════════════════════════════════════
#
# The windows above list today's all-day hearing ON its day; every surface
# then printed its date through to_mtl — the evening BEFORE (00:00 UTC on
# the 15th = 20:00 on the 14th). Listed on the 15th, labelled the 14th.
# An all-day hearing's stored date IS its civil day (occurrence_day).

_ALLDAY_TODAY = _hearing("allday-today", datetime(2026, 10, 15, tzinfo=UTC),
                         all_day=True)


@pytest.fixture
def allday_client(monkeypatch):
    """ONE hearing in the store — today's all-day one — and an app with the
    two blueprints its pages link to."""
    fake = install(monkeypatch, hm)
    fake.seed("hearings/allday-today", dict(_ALLDAY_TODAY))
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.register_blueprint(rh.hearings_bp)
    app.register_blueprint(rd.dossiers_bp)
    monkeypatch.setattr(rh, "today_mtl", lambda: TODAY)
    monkeypatch.setattr(rd, "get_dossier", lambda i: {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif"})
    monkeypatch.setattr(rd, "_attach_prescription_warnings", lambda rows: None)
    monkeypatch.setattr(rd.deadlines, "today_mtl", lambda: TODAY)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


def _day_badge(body: str) -> set[str]:
    """The two-digit day numbers the page prints in a date badge."""
    import re
    return set(re.findall(r'leading-none">(\d\d)</span>', body))


def test_the_list_badge_prints_an_all_day_hearing_on_its_own_day(
    allday_client
):
    body = _listed(allday_client)
    assert "T-allday-today" in body
    assert _day_badge(body) == {"15"}            # the 14th on the old code
    compact = "".join(body.split())
    assert ">jeu<" in compact and ">mer<" not in compact   # Thursday 15


def test_the_detail_page_prints_an_all_day_hearing_on_its_own_day(
    allday_client
):
    resp = allday_client.get("/audiences/allday-today")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "15 octobre 2026" in body
    assert "14 octobre 2026" not in body


def test_the_dossier_tab_badge_prints_an_all_day_hearing_on_its_own_day(
    allday_client
):
    body = allday_client.get("/dossiers/d1/tab/audiences",
                             headers={"HX-Request": "true"}).get_data(
                                 as_text=True)
    assert "T-allday-today" in body
    assert _day_badge(body) == {"15"}


def test_no_template_prints_a_hearing_start_date_through_to_mtl_alone():
    """The sweep behind the renders: a hearing's start goes through to_mtl
    only to print an HOUR; every DATE alone (day, weekday, month, year) of
    it takes the all-day branch first. A new template printing
    ``(h.start_datetime|to_mtl).strftime('%d')`` would put every all-day
    hearing on the evening before, with nothing else to notice it."""
    import pathlib
    import re
    templates = pathlib.Path(__file__).resolve().parent.parent / "templates"
    use = re.compile(
        r"\((?:h|hearing)\.start_datetime\s*\|\s*to_mtl\)(\.[a-z_]+(?:\([^)]*\))?)")
    offenders = []
    for path in templates.rglob("*.html"):
        for m in use.finditer(path.read_text(encoding="utf-8")):
            # An HOUR may be printed (a timed event's, possibly with its
            # date — the Réception rendez-vous card); never a date alone.
            if "%H" not in m.group(1):
                offenders.append(f"{path.relative_to(templates)}: {m.group(0)}")
    assert offenders == [], offenders


def test_the_dashboard_hearing_labels_take_the_all_day_branch():
    """No test renders the real dashboard (its route test captures the
    context instead), so its two hearing lists are pinned here on the
    template's OWN expressions, run through Jinja with the real to_mtl:
    today's all-day hearing reads « 15 », a 21:00 hearing its Montréal day,
    and a row without a start the dash."""
    import pathlib
    import re
    import jinja2
    source = (pathlib.Path(__file__).resolve().parent.parent / "templates"
              / "dashboard" / "index.html").read_text(encoding="utf-8")
    jinja2.Environment(autoescape=True).parse(source)   # still compiles
    short = re.search(r"\{% set hday = .*? %\}", source).group(0)
    long_ = re.search(
        r"\{\{ \(h\.start_datetime if h\.all_day else "
        r"\(h\.start_datetime\|to_mtl\)\)\.strftime\('%d %b %Y'\) \}\}",
        source).group(0)
    env = jinja2.Environment(autoescape=True)    # as Flask renders .html
    env.filters["to_mtl"] = to_mtl
    tpl = env.from_string(
        "{% for h in hs %}" + short
        + "[{{ hday.strftime('%d') if hday else '—' }}"
        + "{% if h.start_datetime %}|" + long_ + "{% endif %}]{% endfor %}")
    out = tpl.render(hs=[
        _ALLDAY_TODAY,
        _hearing("timed-today-21h", _mtl(2026, 10, 15, 21)),
        {"all_day": False, "start_datetime": None},
    ])
    # %b is the process locale's month name — pin the DAYS only.
    assert re.fullmatch(r"\[15\|15 \S+ 2026\]\[15\|15 \S+ 2026\]\[—\]", out), out
