"""dav.sync.get_ctag — une lecture RATÉE ne réinitialise plus le jeton (lot 0b).

Le défaut : ``get_ctag`` avalait toute exception de lecture, puis faisait un
``set()`` d'un jeton NEUF par-dessus le document de synchro. Un simple hoquet
de Firestore réinitialisait donc le jeton de la collection, et chaque client
DAV qui détenait le vrai était poussé vers une resynchronisation complète —
sans aucune erreur nulle part. Le même chemin passait par ``record_tombstone``
et ``record_tombstones_bulk`` (qui lisent le jeton pour l'estampiller).

La réparation distingue trois issues : document présent (son jeton), lecture
RÉUSSIE sans document (un premier jeton, créé par ``create()`` — jamais
écrit par-dessus un jeton qu'un écrivain concurrent vient de poser), lecture
RATÉE (l'exception remonte, rien n'est écrit). Les écrivains de pierres
tombales, eux, estampillent « » et écrivent quand même la pierre tombale —
le seul signal de suppression de ce modèle de synchro.

Tout passe par le VRAI client Firestore (``tests/_fake_firestore.py`` : seul
le serveur est faux) ; une panne de lecture est injectée au niveau du
transport, pour ce seul document.
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

from google.api_core import exceptions as gexc  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

NAME = "parties"
SYNC_DOC = f"dav_sync/{NAME}"
STORED = {"ctag": "ctag-0", "sync_token": "ctag-0"}
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed(SYNC_DOC, dict(STORED))
    return fake


def _fail_reads_of(monkeypatch, fake, path: str) -> None:
    """Every read that names *path* fails at the transport — a Firestore
    blip on that one document, everything else healthy."""
    server = fake._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if path in [server.doc_rel(n) for n in request["documents"]]:
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)


# ── get_ctag ────────────────────────────────────────────────────────────


def test_a_failed_read_propagates_and_never_resets_the_token(
    fake, monkeypatch
):
    """THE defect: on the old code this call returned a fresh token and
    wrote it over « ctag-0 » — every client's next sync became a full one."""
    _fail_reads_of(monkeypatch, fake, SYNC_DOC)
    before = fake.stored_update_time(SYNC_DOC)
    with pytest.raises(gexc.ServiceUnavailable):
        dav_sync.get_ctag(NAME)
    assert fake.peek(SYNC_DOC) == STORED
    assert fake.stored_update_time(SYNC_DOC) == before
    assert fake.commits == []


def test_get_sync_token_shares_the_same_rule(fake, monkeypatch):
    _fail_reads_of(monkeypatch, fake, SYNC_DOC)
    with pytest.raises(gexc.ServiceUnavailable):
        dav_sync.get_sync_token(NAME)
    assert fake.peek(SYNC_DOC) == STORED


def test_a_present_document_serves_its_token_and_writes_nothing(fake):
    assert dav_sync.get_ctag(NAME) == "ctag-0"
    assert fake.commits == []


def test_an_absent_document_gets_its_first_token_once(fake):
    token = dav_sync.get_ctag("dossier:fresh")
    stored = fake.peek("dav_sync/dossier:fresh")
    assert token and stored["ctag"] == token == stored["sync_token"]
    assert dav_sync.get_ctag("dossier:fresh") == token  # read back, not reset
    assert len(fake.commits) == 1
    assert fake.commits[0].ops == (("create", "dav_sync/dossier:fresh"),)


def test_a_racing_writer_wins_over_the_lazy_initialisation(fake):
    """Between the read that found nothing and the initialising write, a
    bump (or another initialiser) stored a token: it must be SERVED, never
    overwritten — overwriting would invalidate the token that other caller
    just handed to a client."""
    def racer(info):
        if ("create", "dav_sync/dossier:race") in info.ops:
            fake.external_write("dav_sync/dossier:race",
                                {"ctag": "racer", "sync_token": "racer"})

    remove = fake.add_commit_hook(racer)
    try:
        token = dav_sync.get_ctag("dossier:race")
    finally:
        remove()
    assert token == "racer"
    assert fake.peek("dav_sync/dossier:race")["ctag"] == "racer"


# ── get_ctags_bulk (the root PROPFIND) ──────────────────────────────────


def test_bulk_initialises_absent_documents_with_create(fake):
    ctags = dav_sync.get_ctags_bulk([NAME, "dossier:new"])
    assert ctags[NAME] == "ctag-0"
    assert fake.peek("dav_sync/dossier:new")["ctag"] == ctags["dossier:new"]
    kinds = [op for c in fake.commits for op in c.ops]
    assert kinds == [("create", "dav_sync/dossier:new")]


def test_bulk_propagates_a_failed_read_and_writes_nothing(fake, monkeypatch):
    _fail_reads_of(monkeypatch, fake, SYNC_DOC)
    with pytest.raises(gexc.ServiceUnavailable):
        dav_sync.get_ctags_bulk([NAME, "dossier:new"])
    assert fake.commits == []


# ── the tombstone writers degrade instead of resetting ──────────────────


def test_record_tombstone_still_writes_the_tombstone_on_a_failed_read(
    fake, monkeypatch
):
    """The tombstone is the ONLY removal signal: a failed token read must not
    prevent it — nor, any more, reset the stored token on the way."""
    _fail_reads_of(monkeypatch, fake, SYNC_DOC)
    dav_sync.record_tombstone(NAME, "p1")
    tomb = fake.peek(f"{SYNC_DOC}/tombstones/p1")
    assert tomb is not None and tomb["sync_token"] == ""
    assert fake.peek(SYNC_DOC) == STORED


def test_record_tombstones_bulk_degrades_the_same_way(fake, monkeypatch):
    _fail_reads_of(monkeypatch, fake, SYNC_DOC)
    dav_sync.record_tombstones_bulk(NAME, ["a", "b"])
    stored = fake.peek_collection(f"{SYNC_DOC}/tombstones")
    assert sorted(stored) == ["a", "b"]
    assert {t["sync_token"] for t in stored.values()} == {""}
    assert fake.peek(SYNC_DOC) == STORED


def test_record_tombstone_stamps_the_current_token_when_readable(fake):
    dav_sync.record_tombstone(NAME, "p1")
    assert fake.peek(f"{SYNC_DOC}/tombstones/p1")["sync_token"] == "ctag-0"


# ── a DAV endpoint answers an error, the stored token survives ──────────


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    b"<d:prop><cs:getctag/><d:sync-token/></d:prop></d:propfind>"
)


def test_a_propfind_during_a_read_blip_is_a_5xx_not_a_reset(
    fake, client, monkeypatch
):
    """DavX5 retries a 5xx on its next sync and loses nothing; a RESET token
    would have made every client re-download the whole address book."""
    ok = client.open("/dav/addressbook/", method="PROPFIND", data=_PROPFIND,
                     headers={**AUTH, "Depth": "0"})
    assert ok.status_code == 207 and b"ctag-0" in ok.data

    _fail_reads_of(monkeypatch, fake, SYNC_DOC)
    resp = client.open("/dav/addressbook/", method="PROPFIND", data=_PROPFIND,
                       headers={**AUTH, "Depth": "0"})
    assert resp.status_code >= 500
    assert fake.peek(SYNC_DOC) == STORED
