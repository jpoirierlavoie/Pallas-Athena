"""Routes des séries récurrentes — création, suppression scopée, détachement.

Pins the route-level guards the model cannot enforce alone:

- the delete scope re-reads the STORED serie_id, never the form. "" is a
  stored value, so a stale page whose occurrence has since been detached
  would otherwise ask to delete the chain "" — every standalone hearing.
- a chain action never reaches a past occurrence.
- a refusal travels on a 2xx redirect with ``?erreur=``: htmx only swaps 2xx,
  so a 4xx fragment would never render and the button would look dead.
- the deletion journal takes ONE row per chain, not one per occurrence.
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

from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import routes.hearings as rh

UTC = timezone.utc
TODAY = date(2026, 9, 15)


@pytest.fixture()
def client():
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    # The app factory registers these; a bare Flask() does not, and base.html
    # calls ms() on every page.
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.register_blueprint(rh.hearings_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """Une horloge gelée : un test qui dérive une date de l'horloge puis
    affirme quelque chose sur le passé échoue quatre heures par jour."""
    monkeypatch.setattr(rh, "today_mtl", lambda: TODAY)


def _occ(hid, day, serie="s1", **over):
    d = {
        "id": hid,
        "serie_id": serie,
        "serie_rule": {"freq": "hebdomadaire", "start": "2026-09-01", "count": 5},
        "dossier_id": "d1",
        "title": "Rencontre",
        "status": "confirmée",
        "all_day": False,
        "start_datetime": datetime(day.year, day.month, day.day, 13, 0, tzinfo=UTC),
    }
    d.update(over)
    return d


# ── Création ────────────────────────────────────────────────────────────
def test_a_recurrence_rule_routes_to_the_series_creator(client, monkeypatch):
    seen = {}

    def _create(data, freq, *, count=None, until=None):
        seen.update(freq=freq, count=count, until=until, title=data["title"])
        return [_occ("h1", TODAY)], []

    monkeypatch.setattr(rh, "create_hearing_series", _create)
    monkeypatch.setattr(rh, "create_hearing", lambda d: (_ for _ in ()).throw(
        AssertionError("le chemin unitaire ne doit pas servir")))
    monkeypatch.setattr(rh, "log_hearing_series_event", lambda *a, **k: None)

    r = client.post("/audiences/", data={
        "title": "Rencontre", "start_date": "2026-09-15", "start_time": "09:00",
        "end_time": "10:00", "hearing_type": "rencontre",
        "frequence": "hebdomadaire", "fin_mode": "count", "fin_count": "5",
    })
    assert r.status_code == 302
    assert seen == {
        "freq": "hebdomadaire", "count": 5, "until": None, "title": "Rencontre"
    }


def test_the_end_date_mode_passes_a_date_and_no_count(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        rh, "create_hearing_series",
        lambda data, freq, *, count=None, until=None: (
            seen.update(count=count, until=until) or ([_occ("h1", TODAY)], [])
        ),
    )
    monkeypatch.setattr(rh, "log_hearing_series_event", lambda *a, **k: None)
    client.post("/audiences/", data={
        "title": "R", "start_date": "2026-09-15", "start_time": "09:00",
        "end_time": "10:00", "frequence": "mensuelle",
        "fin_mode": "date", "fin_date": "2027-03-15",
    })
    assert seen == {"count": None, "until": date(2027, 3, 15)}


def test_no_frequency_keeps_the_single_hearing_path(client, monkeypatch):
    monkeypatch.setattr(rh, "create_hearing_series", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("pas de série demandée")))
    monkeypatch.setattr(
        rh, "create_hearing", lambda d: ({"id": "h1", "dossier_id": "d1"}, [])
    )
    monkeypatch.setattr(rh, "bump_ctag", lambda n: None)
    r = client.post("/audiences/", data={
        "title": "Une seule", "start_date": "2026-09-15",
        "start_time": "09:00", "end_time": "10:00", "frequence": "",
    })
    assert r.status_code == 302


def test_a_refused_series_re_renders_the_form_with_the_french_error(
    client, monkeypatch
):
    monkeypatch.setattr(
        rh, "create_hearing_series",
        lambda *a, **k: ([], ["Une série doit se terminer : indiquez une date."]),
    )
    r = client.post("/audiences/", data={
        "title": "R", "start_date": "2026-09-15", "start_time": "09:00",
        "end_time": "10:00", "frequence": "hebdomadaire", "fin_mode": "count",
        "fin_count": "0",
    })
    assert r.status_code == 200
    assert "Une série doit se terminer" in r.get_data(as_text=True)


def test_creation_redirects_to_the_chain_it_just_created(client, monkeypatch):
    """La seule façon de VOIR ce qui vient d'être créé — la liste ordinaire
    se coupe à 100 lignes sans commande pour aller plus loin."""
    monkeypatch.setattr(
        rh, "create_hearing_series",
        lambda *a, **k: ([_occ("h1", TODAY), _occ("h2", TODAY)], []),
    )
    monkeypatch.setattr(rh, "log_hearing_series_event", lambda *a, **k: None)
    r = client.post("/audiences/", data={
        "title": "R", "start_date": "2026-09-15", "start_time": "09:00",
        "end_time": "10:00", "frequence": "hebdomadaire",
        "fin_mode": "count", "fin_count": "2",
    })
    assert "serie=s1" in r.headers["Location"]
    assert "2+occurrences" in r.headers["Location"].replace("%20", "+")


# ── Suppression scopée ──────────────────────────────────────────────────
def test_scope_suivantes_deletes_from_this_occurrence_onward(
    client, monkeypatch
):
    calls = {}
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, date(2026, 9, 22)))
    monkeypatch.setattr(
        rh, "delete_series",
        lambda sid, *, from_date=None: (
            calls.update(sid=sid, from_date=from_date)
            or ([_occ("h2", date(2026, 9, 22))], [])
        ),
    )
    monkeypatch.setattr(rh, "record_deletion", lambda *a, **k: None)
    monkeypatch.setattr(rh, "log_hearing_series_event", lambda *a, **k: None)

    r = client.post("/audiences/h2/delete", data={"scope": "suivantes"})
    assert r.status_code == 302
    assert calls["sid"] == "s1"
    assert calls["from_date"] == date(2026, 9, 22)


def test_the_pivot_never_reaches_into_the_past(client, monkeypatch):
    """Une occurrence passée est le constat de ce qui a eu lieu : le pivot
    est le plus TARDIF entre son jour et aujourd'hui."""
    calls = {}
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, date(2026, 9, 1)))
    monkeypatch.setattr(
        rh, "delete_series",
        lambda sid, *, from_date=None: (
            calls.update(from_date=from_date) or ([], [])
        ),
    )
    client.post("/audiences/h0/delete", data={"scope": "suivantes"})
    assert calls["from_date"] == TODAY          # jamais le 1er septembre


def test_scope_occurrence_deletes_only_this_one(client, monkeypatch):
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY))
    monkeypatch.setattr(rh, "delete_series", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("la portée unitaire ne doit pas toucher la chaîne")))
    deleted = []
    monkeypatch.setattr(
        rh, "delete_hearing", lambda i: (deleted.append(i) or (True, ""))
    )
    monkeypatch.setattr(rh, "record_tombstone", lambda *a: None)
    monkeypatch.setattr(rh, "bump_ctag", lambda n: None)
    monkeypatch.setattr(rh, "record_deletion", lambda *a, **k: None)

    r = client.post("/audiences/h3/delete", data={"scope": "occurrence"})
    assert r.status_code == 302 and deleted == ["h3"]


def test_a_detached_occurrence_cannot_delete_the_empty_chain(
    client, monkeypatch
):
    """LE piège. « Détacher » pose serie_id = "" et un onglet resté ouvert
    affiche encore « Cette occurrence et les suivantes ». Sans la relecture
    du serie_id STOCKÉ, ce POST supprimerait toute audience autonome du
    cabinet — avec un jeton CSRF valide et la session du juriste."""
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY, serie=""))
    monkeypatch.setattr(rh, "delete_series", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("delete_series ne doit JAMAIS être appelée sur \"\"")))
    deleted = []
    monkeypatch.setattr(
        rh, "delete_hearing", lambda i: (deleted.append(i) or (True, ""))
    )
    monkeypatch.setattr(rh, "record_tombstone", lambda *a: None)
    monkeypatch.setattr(rh, "bump_ctag", lambda n: None)
    monkeypatch.setattr(rh, "record_deletion", lambda *a, **k: None)

    r = client.post("/audiences/h9/delete", data={"scope": "suivantes"})
    assert r.status_code == 302
    assert deleted == ["h9"]                    # dégradé en suppression unitaire


def test_a_chain_delete_journals_one_row_not_one_per_occurrence(
    client, monkeypatch
):
    """list_recent lit une fenêtre dure de 200 filtrée en Python : N lignes
    par chaîne évinceraient tout l'historique de suppression du cabinet."""
    rows = [_occ(f"h{i}", TODAY) for i in range(5)]
    journal = []
    monkeypatch.setattr(rh, "get_hearing", lambda i: rows[0])
    monkeypatch.setattr(rh, "delete_series", lambda sid, *, from_date=None: (rows, []))
    monkeypatch.setattr(
        rh, "record_deletion",
        lambda et, eid, **k: journal.append((et, eid, k.get("title"))),
    )
    monkeypatch.setattr(rh, "log_hearing_series_event", lambda *a, **k: None)

    client.post("/audiences/h0/delete", data={"scope": "suivantes"})
    assert journal == [("hearing_series", "s1", "5 occurrences")]


def test_a_chain_delete_failure_travels_on_a_2xx_redirect(client, monkeypatch):
    """htmx n'échange que les 2xx : un fragment 4xx ne paraîtrait jamais et
    le bouton semblerait mort."""
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY))
    monkeypatch.setattr(
        rh, "delete_series", lambda sid, *, from_date=None: ([], ["Erreur."])
    )
    r = client.post("/audiences/h1/delete", data={"scope": "suivantes"})
    assert r.status_code == 302
    assert "erreur=" in r.headers["Location"]


def test_deleting_nothing_says_so_rather_than_claiming_success(
    client, monkeypatch
):
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY))
    monkeypatch.setattr(
        rh, "delete_series", lambda sid, *, from_date=None: ([], [])
    )
    journal = []
    monkeypatch.setattr(rh, "record_deletion", lambda *a, **k: journal.append(a))
    r = client.post("/audiences/h1/delete", data={"scope": "suivantes"})
    assert "Aucune+occurrence" in r.headers["Location"].replace("%20", "+")
    assert journal == []                        # rien détruit, rien journalisé


# ── Détachement ─────────────────────────────────────────────────────────
def test_detacher_clears_the_link_and_bumps_the_ctag(client, monkeypatch):
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY))
    monkeypatch.setattr(
        rh, "unlink_hearing",
        lambda i: ({"id": i, "dossier_id": "d1", "serie_id": ""}, []),
    )
    bumps = []
    monkeypatch.setattr(rh, "bump_ctag", lambda n: bumps.append(n))
    monkeypatch.setattr(rh, "collection_for", lambda d: f"dossier:{d}")
    monkeypatch.setattr(rh, "log_hearing_series_event", lambda *a, **k: None)

    r = client.post("/audiences/h2/detacher", data={})
    assert r.status_code == 302 and bumps == ["dossier:d1"]


def test_detacher_a_non_series_hearing_reports_the_refusal(client, monkeypatch):
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY, serie=""))
    monkeypatch.setattr(
        rh, "unlink_hearing",
        lambda i: (None, ["Cette audience ne fait pas partie d'une série."]),
    )
    r = client.post("/audiences/h2/detacher", data={})
    assert r.status_code == 302
    assert "erreur=" in r.headers["Location"]


# ── Vue d'une chaîne ────────────────────────────────────────────────────
def test_the_serie_view_lists_the_whole_chain(client, monkeypatch):
    rows = [
        _occ("h1", date(2026, 9, 1)),           # passée
        _occ("h2", date(2026, 9, 15)),          # aujourd'hui
        _occ("h3", date(2026, 9, 22)),          # à venir
    ]
    monkeypatch.setattr(rh, "list_series", lambda sid: rows)
    r = client.get("/audiences/?serie=s1")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Série" in body
    assert "3 occurrences" in body


def test_the_serie_view_splits_on_the_montreal_day_today_counting_as_upcoming(
    client, monkeypatch
):
    seen = {}
    rows = [_occ("h1", date(2026, 9, 1)), _occ("h2", TODAY)]
    monkeypatch.setattr(rh, "list_series", lambda sid: rows)
    monkeypatch.setattr(
        rh, "render_template",
        lambda name, **ctx: seen.update(ctx) or "",
    )
    client.get("/audiences/?serie=s1")
    assert [h["id"] for h in seen["upcoming"]] == ["h2"]
    assert [h["id"] for h in seen["past"]] == ["h1"]


def test_the_serie_view_survives_a_read_failure(client, monkeypatch):
    """La vue dégrade — elle n'affirme pas que la chaîne est vide, elle ne
    500 pas non plus."""
    def _boom(sid):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(rh, "list_series", _boom)
    r = client.get("/audiences/?serie=s1")
    assert r.status_code == 200


# ── Rendu du formulaire ─────────────────────────────────────────────────
def test_the_new_form_offers_the_four_frequencies(client):
    body = client.get("/audiences/new").get_data(as_text=True)
    assert 'name="frequence"' in body
    for label in ("Ne se répète pas", "Chaque semaine", "Chaque mois",
                  "Chaque trimestre", "Chaque année"):
        assert label in body
    assert 'name="fin_count"' in body and 'name="fin_date"' in body


def test_the_edit_form_does_NOT_offer_recurrence(client, monkeypatch):
    """Rendre une audience existante récurrente est un autre geste : il
    faudrait décider du sort des occurrences déjà tenues."""
    monkeypatch.setattr(
        rh, "get_hearing",
        lambda i: _occ(i, TODAY, serie="", serie_rule=None),
    )
    body = client.get("/audiences/h1/edit").get_data(as_text=True)
    assert 'name="frequence"' not in body


def test_the_detail_page_shows_the_series_banner_and_scoped_delete(
    client, monkeypatch
):
    monkeypatch.setattr(rh, "get_hearing", lambda i: _occ(i, TODAY))
    body = client.get("/audiences/h1").get_data(as_text=True)
    assert "Chaque semaine" in body                    # describe(serie_rule)
    assert "Cette occurrence seulement" in body
    assert "Cette occurrence et les suivantes" in body
    assert "Détacher" in body
    assert "jamais supprimées" in body   # les occurrences passées


def test_a_standalone_hearing_keeps_the_plain_delete_dialog(
    client, monkeypatch
):
    monkeypatch.setattr(
        rh, "get_hearing",
        lambda i: _occ(i, TODAY, serie="", serie_rule=None),
    )
    body = client.get("/audiences/h1").get_data(as_text=True)
    assert "Cette occurrence et les suivantes" not in body
    assert "Détacher" not in body
    assert "Cette action est irréversible." in body


def test_the_serie_view_also_renders_as_an_htmx_fragment(client, monkeypatch):
    """La branche HTMX rend _hearing_rows.html, qui réémet le menu d'export
    hors zone d'échange — elle a besoin des mêmes clés de contexte."""
    monkeypatch.setattr(
        rh, "list_series", lambda sid: [_occ("h1", TODAY), _occ("h2", TODAY)]
    )
    r = client.get("/audiences/?serie=s1", headers={"HX-Request": "true"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Série" in body                     # la puce de chaque ligne
    assert "<!DOCTYPE" not in body             # bien un fragment

