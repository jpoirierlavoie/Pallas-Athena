"""DAV — les tâches (VTODO) créées et rééditées sur le téléphone (lot 0b).

Deux défauts silencieux, reproduits ici contre le VRAI client Firestore
(``tests/_fake_firestore.py`` : seul le serveur est faux) et la vraie route
PUT/GET de ``dav/dossier_collections.py`` :

1. **L'identifiant de création.** Un VTODO créé sur le téléphone arrive par
   ``PUT /dav/dossier-<id>/<nouvel-uuid>.ics``. ``create_task`` fabriquait
   TOUJOURS un nouvel id et un nouvel UID : la tâche était rangée sous un id
   que le client n'apprend jamais, chaque GET/PUT ultérieur de son href
   répondait 404, et une copie d'UID différent redescendait au synchro
   suivant (doublon, RELATED-TO de jtx cassé). Le défaut que CLAUDE.md
   décrit comme réparé pour les audiences survivait pour les tâches.
2. **Le suffixe de DESCRIPTION.** ``task_to_vtodo`` ajoute « Dossier: … »
   à la description qu'il émet ; ``vtodo_to_task`` le relisait comme du
   texte. Chaque édition au téléphone ajoutait donc un bloc de plus à la
   description stockée, jusqu'à ce que le plafond de 2000 caractères
   tronque le texte du juriste.

DavX5 échoue en silence : ces tests épinglent ce que la porte de
déploiement peut vérifier ; le reste se vérifie au ``curl`` puis sur
l'appareil (CLAUDE.md, composant nº 2).
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
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # its db is patched below
    from models import task as task_model

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync,)

AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
RID = "3f0c9a52-8e7b-4a51-9d0e-2b6c1a7e4f10"
HREF = f"/dav/dossier-d1/{RID}.ics"
SUFFIX = "Dossier: 2026-001 - Tremblay c. Lavoie"


def _vtodo(uid: str = "phone-uid-7f3a", summary: str = "Appeler le greffe",
           description: str | None = None) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//jtx Board//FR",
        "BEGIN:VTODO", f"UID:{uid}",
        "DTSTAMP:20260926T120000Z", "CREATED:20260926T120000Z",
        f"SUMMARY:{summary}", "STATUS:NEEDS-ACTION",
    ]
    if description is not None:
        escaped = description.replace("\\", "\\\\").replace("\n", "\\n")
        lines.append(f"DESCRIPTION:{escaped}")
    lines += ["END:VTODO", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model the
    DAV layer starts to read tomorrow cannot reach the mocked client."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001",
        "title": "Tremblay c. Lavoie", "status": "actif",
    })
    return fake


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


def _put(client, href: str, body: str, **headers):
    return client.put(href, data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar",
                               **headers})


# ══════════════════════════════════════════════════════════════════════
# 1. L'identifiant de création
# ══════════════════════════════════════════════════════════════════════


def test_a_phone_created_task_is_served_back_at_its_own_href(fake, client):
    """THE defect: the href the phone PUT to must answer the GET that
    follows. On the old code the task was stored under a server-minted id
    and this GET was a 404."""
    resp = _put(client, HREF, _vtodo())
    assert resp.status_code == 201

    got = client.get(HREF, headers=AUTH)
    assert got.status_code == 200
    body = got.get_data(as_text=True)
    assert "UID:phone-uid-7f3a" in body
    assert "SUMMARY:Appeler le greffe" in body
    # The ETag the PUT answered is the one the GET serves: DavX5 compares
    # them on its next sync.
    assert got.headers["ETag"] == resp.headers["ETag"]


def test_the_stored_task_keeps_the_url_id_and_the_phone_uid(fake, client):
    """A regenerated UID reads as a different event to the client — the
    jtx RELATED-TO link to a note breaks, and a duplicate syncs down."""
    _put(client, HREF, _vtodo())
    stored = fake.peek(f"tasks/{RID}")
    assert stored is not None, fake.peek_collection("tasks")
    assert stored["id"] == RID
    assert stored["vtodo_uid"] == "phone-uid-7f3a"
    assert stored["dossier_id"] == "d1"
    assert list(fake.peek_collection("tasks")) == [RID]


def test_a_create_racing_an_existing_task_is_refused_never_overwritten(
    fake, client, monkeypatch
):
    """A racing PUT can store the task between the PUT path's read and its
    create, so the create branch must not be allowed to overwrite a
    document it did not see: ``document(id).create()`` refuses, and DAV
    answers 412. (Since lot 1a the read is STRICT — a read error answers
    503, test_a_failed_read_is_a_503_never_a_create — so the stand-in for
    « the read did not see it » is now the strict reader itself.)"""
    fake.seed(f"tasks/{RID}", {"id": RID, "title": "Existante",
                               "status": "à_faire", "vtodo_uid": "u0",
                               "dossier_id": "d1", "etag": "e0"})
    before = fake.peek(f"tasks/{RID}")
    monkeypatch.setattr(dc, "get_task_strict", lambda i: None)  # the race
    resp = _put(client, HREF, _vtodo())
    assert resp.status_code == 412
    assert fake.peek(f"tasks/{RID}") == before


@pytest.mark.parametrize("bad", ["__reserved__", "x" * 129])
def test_an_unusable_resource_name_is_refused_before_any_write(
    fake, client, bad
):
    resp = _put(client, f"/dav/dossier-d1/{bad}.ics", _vtodo())
    assert resp.status_code == 400
    assert fake.peek_collection("tasks") == {}


def test_a_task_cannot_be_created_over_a_note_of_the_same_id(fake, client):
    """Tasks, notes and hearings share one href space per collection, and
    ``_resolve_resource`` tries the task FIRST — a task created under a
    note's id would hide the note from every later GET. Honouring the URL
    id opened that collision (minted ids could never collide)."""
    fake.seed(f"notes/{RID}", {"id": RID, "title": "Note", "dossier_id": "d1",
                               "vjournal_uid": "n-uid", "etag": "n0"})
    resp = _put(client, HREF, _vtodo())
    assert resp.status_code == 412
    assert fake.peek_collection("tasks") == {}


def test_create_task_without_a_dav_id_never_honours_a_caller_id(fake):
    """The connector and the web form call ``create_task`` with a dict:
    an ``id``/``vtodo_uid`` smuggled in it must never pick the document —
    only the explicit ``dav_id`` keyword of the DAV create path can."""
    fake.seed("tasks/victim", {"id": "victim", "title": "Ne pas écraser",
                               "etag": "v0"})
    doc, errors = task_model.create_task(
        {"id": "victim", "vtodo_uid": "forged", "title": "T"})
    assert errors == []
    assert doc["id"] != "victim" and doc["vtodo_uid"] != "forged"
    assert fake.peek("tasks/victim")["title"] == "Ne pas écraser"


@pytest.mark.parametrize("rid, ok", [
    ("3f0c9a52-8e7b-4a51-9d0e-2b6c1a7e4f10", True),
    ("abc@example.com", True),
    ("x" * 128, True),
    ("x" * 129, False),
    ("", False),
    (".", False),
    ("..", False),
    ("a/b", False),
    ("__x__", False),
    ("tab\there", False),
    (None, False),
])
def test_the_resource_id_rule(rid, ok):
    assert task_model.valid_resource_id(rid) is ok


# ══════════════════════════════════════════════════════════════════════
# 2. Le suffixe de DESCRIPTION
# ══════════════════════════════════════════════════════════════════════


def _stored_task(fake, description: str) -> dict:
    doc, errors = task_model.create_task({
        "title": "Préparer la requête", "description": description,
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
    })
    assert errors == []
    return doc


def test_putting_back_an_unchanged_vtodo_does_not_grow_the_description(
    fake, client
):
    """The round trip the phone performs on every edit of ANY field: GET,
    change nothing in the description, PUT back. On the old code the stored
    description gained « \\n\\nDossier: … » each time."""
    doc = _stored_task(fake, "Rédiger le projet")
    href = f"/dav/dossier-d1/{doc['id']}.ics"
    for _ in range(3):
        got = client.get(href, headers=AUTH)
        assert SUFFIX in got.get_data(as_text=True).replace("\r\n ", "")
        resp = _put(client, href, got.get_data(as_text=True),
                    **{"If-Match": got.headers["ETag"]})
        assert resp.status_code == 204
    assert fake.peek(f"tasks/{doc['id']}")["description"] == "Rédiger le projet"


def test_a_phone_edit_of_the_description_keeps_the_edit_and_drops_the_suffix(
    fake, client
):
    doc = _stored_task(fake, "Rédiger le projet")
    href = f"/dav/dossier-d1/{doc['id']}.ics"
    body = _vtodo(uid=doc["vtodo_uid"], summary="Préparer la requête",
                  description=f"Rédiger le projet ce soir\n\n{SUFFIX}")
    assert _put(client, href, body).status_code == 204
    assert (fake.peek(f"tasks/{doc['id']}")["description"]
            == "Rédiger le projet ce soir")


def test_an_empty_description_round_trips_to_empty(fake, client):
    """With no description the serializer emits the suffix ALONE."""
    doc = _stored_task(fake, "")
    href = f"/dav/dossier-d1/{doc['id']}.ics"
    got = client.get(href, headers=AUTH)
    _put(client, href, got.get_data(as_text=True))
    assert fake.peek(f"tasks/{doc['id']}")["description"] == ""


def test_the_serializer_and_the_stripper_share_one_suffix():
    task = {"description": "X", "dossier_file_number": "2026-001",
            "dossier_title": "Tremblay c. Lavoie", "title": "T",
            "vtodo_uid": "u"}
    assert task_model.dav_description_suffix(task) == SUFFIX
    assert SUFFIX in task_model.task_to_vtodo(task).replace("\r\n ", "")
    assert task_model.dav_description_suffix(
        {"dossier_file_number": "", "dossier_title": "x"}) == ""


@pytest.mark.parametrize("incoming, expected", [
    (f"Texte\n\n{SUFFIX}", "Texte"),
    (f"Texte\r\n\r\n{SUFFIX}", "Texte"),
    (SUFFIX, ""),
    # Legacy damage: blocks accumulated before the fix peel off too — every
    # one of them is the serializer's own output, never the lawyer's text.
    (f"Texte\n\n{SUFFIX}\n\n{SUFFIX}", "Texte"),
    # Anything that is not EXACTLY the suffix is the lawyer's: left alone.
    (f"Texte\n\n{SUFFIX} (voir)", f"Texte\n\n{SUFFIX} (voir)"),
    (f"Texte {SUFFIX}", f"Texte {SUFFIX}"),
    ("Dossier: 2026-002 - Autre", "Dossier: 2026-002 - Autre"),
])
def test_only_the_exact_serializer_suffix_is_stripped(incoming, expected):
    existing = {"dossier_file_number": "2026-001",
                "dossier_title": "Tremblay c. Lavoie"}
    data = {"description": incoming}
    task_model.strip_dav_description_suffix(data, existing)
    assert data["description"] == expected


def test_no_description_key_stays_no_description_key():
    """Non-effacement: a VTODO without DESCRIPTION must not gain an empty
    one (update_task merges, and a present-but-empty key erases)."""
    data = {"title": "T"}
    task_model.strip_dav_description_suffix(
        data, {"dossier_file_number": "2026-001", "dossier_title": "T c. L"})
    assert "description" not in data


@pytest.mark.parametrize("incoming, expected", [
    # Finitions, sync-7: text typed AFTER the served line on the phone.
    (f"Texte\n\n{SUFFIX}\nAjout", "Texte\nAjout"),
    (f"Texte\n\n{SUFFIX}\n\nAjout", "Texte\n\nAjout"),
    (f"Texte\r\n\r\n{SUFFIX}\r\nAjout", "Texte\r\nAjout"),
    (f"{SUFFIX}\nAjout", "Ajout"),
    (f"Texte\n\n{SUFFIX} (voir)\nAjout", f"Texte\n\n{SUFFIX} (voir)\nAjout"),
])
def test_the_dossier_line_goes_wherever_it_stands_as_a_whole_line(
        incoming, expected):
    existing = {"dossier_file_number": "2026-001",
                "dossier_title": "Tremblay c. Lavoie"}
    data = {"description": incoming}
    task_model.strip_dav_description_suffix(data, existing)
    assert data["description"] == expected
