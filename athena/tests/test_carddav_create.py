"""CardDAV — un contact créé sur le téléphone garde l'identifiant de son URL
(lot 0b).

Le défaut : un contact créé dans DavX5 arrive par
``PUT /dav/addressbook/<nouvel-uuid>.vcf``. ``carddav.put_resource`` posait
bien ``data["id"] = <id de l'URL>``, mais ``create_partie`` fabriquait
TOUJOURS un nouvel id et un nouvel UID : le contact était rangé sous un id
que le téléphone n'apprend jamais, chaque GET/PUT ultérieur de son href
répondait 404, et une copie d'UID différent redescendait au synchro
suivant — un doublon dans le carnet d'adresses de l'appareil. Le défaut
que CLAUDE.md décrit comme réparé pour les audiences, et que le lot 0b a
réparé pour les tâches, survivait pour les contacts.

Reproduit ici contre le VRAI client Firestore (``tests/_fake_firestore.py``
: seul le serveur est faux) et les vraies routes PUT/GET de
``dav/carddav.py``. DavX5 échoue en silence : le reste se vérifie au
``curl`` puis sur l'appareil (CLAUDE.md, composant nº 2).
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
    import dav.carddav as carddav
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    from models import partie as pm

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
RID = "5b2d8c1e-4f3a-4e7b-9c0d-1a2b3c4d5e6f"
HREF = f"/dav/addressbook/{RID}.vcf"


def _vcard(uid: str = "phone-vcard-uid-42", last: str = "Tremblay",
           first: str = "Jean") -> str:
    return "\r\n".join([
        "BEGIN:VCARD", "VERSION:4.0", f"UID:{uid}",
        f"FN:{first} {last}", f"N:{last};{first};;;",
        "EMAIL:jean@exemple.ca", "END:VCARD", "",
    ])


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def _put(client, href: str, body: str, **headers):
    return client.put(href, data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/vcard",
                               **headers})


def test_a_phone_created_contact_is_served_back_at_its_own_href(fake, client):
    """THE defect: the href the phone PUT to must answer the GET that
    follows. On the old code the contact was stored under a server-minted
    id and this GET was a 404."""
    resp = _put(client, HREF, _vcard())
    assert resp.status_code == 201

    got = client.get(HREF, headers=AUTH)
    assert got.status_code == 200
    body = got.get_data(as_text=True)
    assert "UID:phone-vcard-uid-42" in body
    assert "Tremblay" in body
    # The ETag the PUT answered is the one the GET serves: DavX5 compares
    # them on its next sync.
    assert got.headers["ETag"] == resp.headers["ETag"]


def test_the_stored_contact_keeps_the_url_id_and_the_phone_uid(fake, client):
    """A regenerated UID reads as a DIFFERENT contact to the phone — a
    duplicate in its address book."""
    _put(client, HREF, _vcard())
    stored = fake.peek(f"parties/{RID}")
    assert stored is not None, fake.peek_collection("parties")
    assert stored["id"] == RID
    assert stored["vcard_uid"] == "phone-vcard-uid-42"
    assert stored["dav_href"] == HREF
    assert list(fake.peek_collection("parties")) == [RID]


def test_a_second_put_on_the_new_href_updates_instead_of_duplicating(
    fake, client
):
    first = _put(client, HREF, _vcard())
    second = _put(client, HREF, _vcard(last="Tremblay-Roy"),
                  **{"If-Match": first.headers["ETag"]})
    assert second.status_code == 204
    assert list(fake.peek_collection("parties")) == [RID]
    assert fake.peek(f"parties/{RID}")["last_name"] == "Tremblay-Roy"


def test_a_create_racing_an_existing_contact_is_refused_never_overwritten(
    fake, client, monkeypatch
):
    """``get_partie`` fails OPEN (a read error reads as « absent »), so the
    create branch must never overwrite a document it did not see:
    ``document(id).create()`` refuses, and CardDAV answers 412."""
    fake.seed(f"parties/{RID}", {**pm._default_doc(), "id": RID,
                                 "type": "individual",
                                 "contact_role": "client",
                                 "last_name": "Existant",
                                 "vcard_uid": "u0", "etag": "e0"})
    before = fake.peek(f"parties/{RID}")
    monkeypatch.setattr(carddav, "get_partie", lambda i: None)
    resp = _put(client, HREF, _vcard())
    assert resp.status_code == 412
    assert fake.peek(f"parties/{RID}") == before


@pytest.mark.parametrize("bad", ["__reserved__", "x" * 129, "."])
def test_an_unusable_resource_name_is_refused_before_any_write(
    fake, client, bad
):
    resp = _put(client, f"/dav/addressbook/{bad}.vcf", _vcard())
    assert resp.status_code == 400
    assert fake.peek_collection("parties") == {}


def test_create_partie_without_a_dav_id_never_honours_a_caller_id(fake):
    """The web form, Réception and the connector call ``create_partie``
    with a dict: an ``id``/``vcard_uid`` smuggled in it must never pick the
    document — only the explicit ``dav_id`` keyword of CardDAV can."""
    fake.seed("parties/victim", {**pm._default_doc(), "id": "victim",
                                 "type": "individual",
                                 "contact_role": "client",
                                 "last_name": "Ne pas écraser", "etag": "v0"})
    doc, errors = pm.create_partie({
        "id": "victim", "vcard_uid": "forged", "type": "individual",
        "contact_role": "client", "last_name": "Autre",
    })
    assert errors == []
    assert doc["id"] != "victim" and doc["vcard_uid"] != "forged"
    assert fake.peek("parties/victim")["last_name"] == "Ne pas écraser"


def test_an_unusable_phone_uid_is_replaced_not_stored_altered(fake, client):
    """Verbatim or not at all: a UID the store would alter would read to the
    phone as a different contact — the very duplicate this path prevents —
    so a fresh one is minted, and the URL id still holds."""
    resp = _put(client, HREF, _vcard(uid="<b>uid</b>"))
    assert resp.status_code == 201
    stored = fake.peek(f"parties/{RID}")
    assert stored["id"] == RID
    assert stored["vcard_uid"] != "<b>uid</b>" and stored["vcard_uid"]


def test_one_rule_for_every_dav_created_id():
    """Tasks (VTODO) and contacts (vCard) accept a client-chosen id under
    ONE rule: a per-model copy would drift, and the two DAV layers would
    then refuse different names."""
    from models import dav_ids
    from models import task as task_model

    assert task_model.valid_resource_id is dav_ids.valid_resource_id
    assert task_model.DAV_ID_TAKEN == dav_ids.DAV_ID_TAKEN
    assert pm.dav_ids is dav_ids
    assert carddav.valid_resource_id is dav_ids.valid_resource_id
