"""Le ticket de téléversement du connecteur (lot 2A, étape T9 ; décision D4) :
begin_upload → PUT du bac à sable → finalize_upload.

Tout passe par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), le faux Cloud Storage qui garde les octets
et REFUSE ce que le service refuse (``tests/_fake_gcs.py`` : une session
reprenable plafonnée à sa taille, créatrice seulement, qui vérifie l'MD5
déclaré à l'ouverture), les vrais modèles (ticket, document, gabarit), le
vrai contrôle des identifiants et le vrai protocole d'écriture
(``run_write``) ; on relit ce qui est STOCKÉ.

Épinglé :

1. begin_upload LIE tout à l'ouverture (taille, MD5, métadonnées jugées
   comme le modèle les jugera), ouvre la session AVANT d'écrire le ticket
   (une session impossible n'écrit rien), la session est créatrice, sans
   ``origin``, plafonnée, porte l'MD5 déclaré ; l'URL — la SEULE capacité
   qu'une sortie porte — n'atteint jamais ``mcp_idempotency`` ; un rejeu
   rouvre une session pour le MÊME ticket, ou refuse ;
2. finalize_upload ne verse que les octets dont la taille ET l'MD5 sont
   ceux déclarés (``hmac.compare_digest``) — d'autres octets sont refusés et
   effacés ; « pas encore reçu » rend le ticket, qui reste ouvert ; un
   ticket expiré est refusé, ses octets effacés ; une réservation périmée
   est reprise sans jamais produire un second document ; un ticket versé
   rend son résultat de nouveau, sans clé ;
3. un gabarit : le contrôle des identifiants du dossier source, FERMÉ ;
   un résidu non accepté refuse en le nommant ; le nom de fichier est celui
   du gabarit, jamais celui du fichier ; un type spécial n'est jamais
   désigné actif ; un remplacement garde la version précédente et refuse
   une version périmée ;
4. les refus d'ouverture n'écrivent rien.
"""

import base64
import hashlib
import io
import json
import os
import pathlib
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import doc_template as tpl_model
    from models import document as document_model
    from models import upload_ticket as ut
    from utils import storage_identity

ToolArgumentError = tools.ToolArgumentError
from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync,)

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
PDF = b"%PDF-1.7\n" + bytes(range(256)) * 8
_CORE = (
    '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties '
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
    'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:creator>{creator}</dc:creator></cp:coreProperties>"
)


def _docx(body_text: str, *, creator: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>'
            f"<w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body></w:document>")
        if creator:
            zf.writestr("docProps/core.xml", _CORE.format(creator=creator))
    return buf.getvalue()


CLEAN_LETTER = _docx("Objet : {{objet_lettre}} — {{dossier.titre}}")
LEAKY_LETTER = _docx("Monsieur Jean Tremblay, votre dossier {{objet_lettre}}")


def _md5(data: bytes) -> str:
    # GCS's content MD5 — an integrity check, not a security hash.
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


def _individual(pid, first, last):
    return {"id": pid, "type": "individual", "contact_role": "client",
            "first_name": first, "last_name": last,
            "address_street": "1 rue A", "address_unit": "",
            "address_city": "Laval", "address_province": "Québec",
            "address_postal_code": "H1A 1A1", "address_country": "Canada"}


@pytest.fixture
def world(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)
    clock = {"now": NOW}
    monkeypatch.setattr(ut, "_now", lambda: clock["now"])
    db.seed("parties/c1", _individual("c1", "Jean", "Tremblay"))
    db.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Alpha",
        "status": "actif", "forum_type": "judiciaire", "created_at": DT,
        "clients": [{"id": "c1", "name": "Jean Tremblay",
                     "roles": ["demandeur"], "avocat_id": "",
                     "avocat_name": ""}],
        "client_ids": ["c1"], "opposing_parties": [],
        "opposing_party_ids": []})
    db.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces",
                           "parent_folder_id": None, "order": 0,
                           "system_role": "", "etag": "e-f1",
                           "created_at": DT, "updated_at": DT})
    existing = _docx("Version un : {{objet_lettre}}")
    tpl, errors = tpl_model.create_template(
        io.BytesIO(existing), "base.docx", len(existing),
        {"name": "Lettre de base", "category": "correspondance",
         "kind": "gabarit"}, UID)
    assert errors == [], errors
    db.reset_logs()
    return {"db": db, "bucket": bucket, "clock": clock, "template": tpl["id"]}


def _begin_document(world, data: bytes = PDF, **over) -> dict:
    args = {"purpose": "document", "dossier_id": "d1",
            "filename": "Lettre du client.pdf", "size_bytes": len(data),
            "md5_base64": _md5(data)}
    args.update(over)
    return handlers.begin_upload(args)


def _begin_gabarit(world, data: bytes, **over) -> dict:
    args = {"purpose": "gabarit", "template_mode": "create",
            "name": "Lettre type", "filename": "Lettre à M. Tremblay.docx",
            "size_bytes": len(data), "md5_base64": _md5(data)}
    # Fixups of lot 2A: a gabarit names its source dossier OR declares it
    # comes from none — the helper declares it when the test names none.
    if "dossier_id" not in over:
        args["aucun_dossier_source"] = True
    args.update(over)
    return handlers.begin_upload(args)


def _ticket(world, ticket_id: str) -> dict:
    return world["db"].peek(f"{ut.COLLECTION}/{ticket_id}")


def _staging(world, ticket_id: str) -> str:
    return _ticket(world, ticket_id)["staging_object"]


def _put(world, opened: dict, data: bytes) -> None:
    world["bucket"].complete_session(opened["upload_url"], data)


def _documents(world) -> dict:
    return world["db"].peek_collection("documents")


def _templates(world) -> dict:
    return world["db"].peek_collection("doc_templates")


# ══════════════════════════════════════════════════════════════════════
# 1. begin_upload
# ══════════════════════════════════════════════════════════════════════


def test_begin_binds_everything_and_opens_a_create_only_session(world):
    opened = _begin_document(
        world, category="correspondance", display_name="Lettre du 12",
        tags=["client"], document_date="2026-09-12", folder_id="f1")
    ticket = _ticket(world, opened["ticket_id"])
    assert ticket["status"] == ut.STATUS_OPEN
    assert ticket["declared_size"] == len(PDF)
    assert ticket["declared_md5_b64"] == _md5(PDF)
    assert ticket["bound_metadata"]["category"] == "correspondance"
    assert ticket["bound_metadata"]["folder_id"] == "f1"
    assert ticket["bound_metadata"]["document_date"].date().isoformat() == "2026-09-12"
    assert ticket["staging_object"] == (
        f"staging/{UID}/mcp/{opened['ticket_id']}/upload.pdf")
    session = world["bucket"].sessions[opened["upload_url"]]
    assert session == {
        "name": ticket["staging_object"], "content_type": "application/pdf",
        "size": len(PDF), "origin": None, "if_generation_match": 0,
        "md5_hash": _md5(PDF),
    }
    assert opened["method"] == "PUT"
    assert opened["headers"] == {"Content-Type": "application/pdf",
                                 "Content-Length": str(len(PDF))}
    assert opened["max_bytes"] == len(PDF)
    assert opened["expires_at"].startswith("2026-09-27T12:00")   # 16h UTC, Montréal
    assert "never repeat or show it" in opened["instructions"]
    assert opened["entity"] == {"id": opened["ticket_id"], "dossier_id": "d1"}
    assert any("PRÉSUMÉE" in w for w in opened["warnings"])


def test_the_session_is_what_the_service_will_hold_the_put_to(world):
    """The declared MD5 travels in the initiation metadata: the service
    refuses a PUT of other bytes — and of more bytes — and a completed
    session never takes a second PUT (create-only)."""
    opened = _begin_document(world)
    forged = bytes(len(PDF))
    with pytest.raises(AssertionError, match="MD5"):
        world["bucket"].complete_session(opened["upload_url"], forged)
    with pytest.raises(AssertionError, match="size"):
        world["bucket"].complete_session(opened["upload_url"], PDF + b"x")
    _put(world, opened, PDF)
    with pytest.raises(AssertionError, match="completed"):
        world["bucket"].complete_session(opened["upload_url"], PDF)


@pytest.mark.parametrize("over, fragment", [
    # GCS's content MD5 — an integrity check, not a security hash.
    ({"md5_base64": hashlib.md5(PDF, usedforsecurity=False).hexdigest()}, "HEXADÉCIMALE"),
    ({"md5_base64": "A" * 24}, "md5_base64"),
    ({"filename": "script.exe"}, "type de fichier"),
    ({"filename": "a/b.pdf"}, "barre oblique"),
    ({"size_bytes": 0}, "size_bytes"),
    ({"dossier_id": ""}, "dossier_id"),
    ({"dossier_id": "inconnu"}, "Dossier introuvable"),
    ({"folder_id": "nulle-part"}, "folder_id"),
    ({"category": "facture", "tags": ["a,b"]}, "virgule"),
    ({"template_mode": "create"}, "ne s'applique pas"),
    ({"display_name": "<b>x</b>"}, "chevrons"),
    ({"document_date": "12/09/2026"}, "document_date"),
])
def test_a_document_opening_refuses_and_writes_nothing(world, over, fragment):
    with pytest.raises(ToolArgumentError, match=fragment):
        _begin_document(world, **over)
    assert world["db"].peek_collection(ut.COLLECTION) == {}
    assert world["bucket"].sessions == {}


_GABARIT_BASE = {"purpose": "gabarit", "template_mode": "create",
                 "name": "Lettre type", "filename": "lettre.docx",
                 "size_bytes": len(CLEAN_LETTER),
                 "md5_base64": _md5(CLEAN_LETTER),
                 # Fixups of lot 2A: every refusal below is reached PAST the
                 # source-dossier rule, which has its own tests.
                 "aucun_dossier_source": True}


@pytest.mark.parametrize("over, fragment", [
    ({"filename": "lettre.pdf"}, ".docx"),
    ({"size_bytes": tpl_model.MAX_TEMPLATE_SIZE + 1}, "10 Mo"),
    ({"template_mode": None}, "template_mode"),
    ({"name": ""}, "name"),
    ({"kind": "autre"}, "kind"),
    ({"category": "facture"}, "procédure"),
    ({"template_id": "x"}, "ne s'applique pas"),
    ({"accept_residual": ["Jean Tremblay"]}, "dossier_id"),
    ({"folder_id": "f1"}, "ne s'applique pas"),
])
def test_a_gabarit_opening_refuses_and_writes_nothing(world, over, fragment):
    args = {**_GABARIT_BASE, **over}
    args = {k: v for k, v in args.items() if v is not None}
    with pytest.raises(ToolArgumentError, match=fragment):
        handlers.begin_upload(args)
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def _replace_args(world, **over) -> dict:
    args = {"purpose": "gabarit", "template_mode": "replace",
            "template_id": world["template"], "expected_version": 1,
            "filename": "x.docx", "size_bytes": len(CLEAN_LETTER),
            "md5_base64": _md5(CLEAN_LETTER)}
    # Fixups of lot 2A: the source dossier, or the declaration of none.
    if "dossier_id" not in over:
        args["aucun_dossier_source"] = True
    args.update(over)
    return args


def test_a_replacement_opening_checks_the_version_read(world):
    with pytest.raises(ToolArgumentError, match="version 1, pas 3") as exc:
        handlers.begin_upload(_replace_args(world, expected_version=3))
    assert exc.value.reason == "stale_etag"
    with pytest.raises(ToolArgumentError, match="ne s'applique pas"):
        handlers.begin_upload(_replace_args(world, name="Autre nom"))
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def test_a_session_that_cannot_open_writes_no_ticket(world, monkeypatch):
    from tests._fake_gcs import FakeBlob

    def broken(self, **_kw):
        raise RuntimeError("the service said no")

    monkeypatch.setattr(FakeBlob, "create_resumable_upload_session", broken)
    with pytest.raises(ToolArgumentError) as exc:
        _begin_document(world)
    assert exc.value.reason == "upload_retry"
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def _stored_texts(world) -> list[str]:
    texts = []
    for collection in (ut.COLLECTION, write_support.COLLECTION):
        for doc in world["db"].peek_collection(collection).values():
            texts.append(json.dumps(doc, default=str, ensure_ascii=False))
    return texts


def test_the_url_is_never_stored_and_a_replay_reopens_the_same_ticket(world):
    args = {"purpose": "document", "dossier_id": "d1", "filename": "a.pdf",
            "size_bytes": len(PDF), "md5_base64": _md5(PDF),
            "idempotency_key": "televersement-cle-0001"}
    first = handlers.begin_upload(dict(args))
    assert "upload_id=" in first["upload_url"]
    world["clock"]["now"] = NOW + timedelta(minutes=10)
    replay = handlers.begin_upload(dict(args))
    assert replay["idempotent_replay"] is True
    assert replay["ticket_id"] == first["ticket_id"]
    assert replay["upload_url"] != first["upload_url"]       # a FRESH session
    assert world["bucket"].sessions[replay["upload_url"]]["name"] == (
        _staging(world, first["ticket_id"]))
    assert len(world["db"].peek_collection(ut.COLLECTION)) == 1
    # DERIVED: no stored write — the ticket, the replay record — carries a
    # capability, whatever key or marker it would hide under.
    for text in _stored_texts(world):
        for marker in ("upload_id=", "googleapis", "X-Goog", "storage.example"):
            assert marker not in text, marker
    # Once the bytes are in, a replay refuses: finalize it instead.
    _put(world, replay, PDF)
    with pytest.raises(ToolArgumentError, match="finalize_upload") as exc:
        handlers.begin_upload(dict(args))
    assert exc.value.reason == "upload_already_received"
    # Once FILED, a replay refuses — and sends the caller to finalize_upload,
    # never to a new ticket (review of T9: it named a « NOUVELLE
    # idempotency_key », the instruction that files the same file twice).
    handlers.finalize_upload({"ticket_id": first["ticket_id"]})
    with pytest.raises(ToolArgumentError, match="déjà été versé") as exc:
        handlers.begin_upload(dict(args))
    assert exc.value.reason == "upload_already_received"
    assert "finalize_upload" in str(exc.value)
    assert "idempotency_key" not in str(exc.value)


def test_a_replayed_opening_never_leads_to_a_second_document(world):
    """Review of T9. The replay a task re-run makes — the SAME key, say one
    derived from the file's digest — once the file is filed: followed to
    the letter, the refusal must lead back to the ONE document, and never
    to a second ticket filing the same bytes again."""
    args = {"purpose": "document", "dossier_id": "d1", "filename": "a.pdf",
            "size_bytes": len(PDF), "md5_base64": _md5(PDF),
            "idempotency_key": f"televersement-{_md5(PDF)}"}
    opened = handlers.begin_upload(dict(args))
    _put(world, opened, PDF)
    filed = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    with pytest.raises(ToolArgumentError) as exc:
        handlers.begin_upload(dict(args))
    # What the refusal says to do: finalize_upload with this ticket_id.
    assert "finalize_upload" in str(exc.value)
    again = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert again["already_finalized"] is True
    assert again["entity"]["id"] == filed["entity"]["id"]
    assert len(_documents(world)) == 1
    assert len(world["db"].peek_collection(ut.COLLECTION)) == 1


def test_a_replayed_opening_during_a_finalization_says_wait(world):
    """Review of T9: a ticket held by a finalization is not « closed » — the
    claim is released « not received », or the ticket files. The replay
    says wait and retry, never « open a new one »."""
    args = {"purpose": "document", "dossier_id": "d1", "filename": "a.pdf",
            "size_bytes": len(PDF), "md5_base64": _md5(PDF),
            "idempotency_key": "televersement-cle-0002"}
    opened = handlers.begin_upload(dict(args))
    ut.claim_ticket(opened["ticket_id"], now=NOW)
    with pytest.raises(ToolArgumentError, match="en cours") as exc:
        handlers.begin_upload(dict(args))
    assert exc.value.reason == "upload_busy"
    assert "idempotency_key" not in str(exc.value)


# ══════════════════════════════════════════════════════════════════════
# 2. finalize_upload — purpose document
# ══════════════════════════════════════════════════════════════════════


def test_a_document_upload_is_filed_as_a_new_document(world):
    opened = _begin_document(world, category="correspondance",
                             display_name="Lettre du 12", folder_id="f1",
                             tags=["client"])
    _put(world, opened, PDF)
    staging = _staging(world, opened["ticket_id"])
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    ticket = _ticket(world, opened["ticket_id"])
    doc_id = ticket["reserved_document_id"]
    assert done["entity"]["id"] == doc_id and done["already_finalized"] is False
    stored = world["db"].peek(f"documents/{doc_id}")
    assert stored["dossier_id"] == "d1" and stored["folder_id"] == "f1"
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "mcp"            # D15: presumed
    assert stored["created_via"] == "mcp"
    assert stored["display_name"] == "Lettre du 12"
    assert stored["file_type"] == "application/pdf"
    assert world["bucket"].objects[stored["storage_path"]].data == PDF
    assert staging not in world["bucket"].objects         # consumed
    assert ticket["status"] == ut.STATUS_DONE
    assert ticket["result"] == {"document_id": doc_id}
    assert done["entity"]["category_presumee"] is True


def test_a_document_without_a_category_is_not_presumed(world):
    opened = _begin_document(world)
    _put(world, opened, PDF)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["entity"]["category_source"] == "juriste"
    assert not any("PRÉSUMÉE" in w for w in done["warnings"])


def test_bytes_that_are_not_the_declared_md5_are_refused_and_discarded(world):
    """The leaked-URL defence: an object of the declared SIZE but not the
    declared MD5 — what a holder of the URL could PUT through a service
    that did not check it — is refused, deleted, and the ticket closed."""
    opened = _begin_document(world)
    staging = _staging(world, opened["ticket_id"])
    world["bucket"].put(staging, bytes(len(PDF)))
    with pytest.raises(ToolArgumentError, match="empreinte MD5") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_bytes_mismatch"
    assert staging not in world["bucket"].objects
    ticket = _ticket(world, opened["ticket_id"])
    assert ticket["status"] == ut.STATUS_REFUSED
    assert ticket["refusal_reason"] == "empreinte_differente"
    assert _documents(world) == {}
    with pytest.raises(ToolArgumentError, match="déjà été refusé"):
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})


def test_bytes_of_another_size_are_refused_and_discarded(world):
    opened = _begin_document(world)
    staging = _staging(world, opened["ticket_id"])
    world["bucket"].put(staging, PDF + b"de trop")
    with pytest.raises(ToolArgumentError, match="taille déclarée"):
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert _ticket(world, opened["ticket_id"])["refusal_reason"] == (
        "taille_differente")
    assert staging not in world["bucket"].objects
    assert _documents(world) == {}


def test_nothing_uploaded_yet_releases_the_ticket_for_later(world):
    opened = _begin_document(world)
    with pytest.raises(ToolArgumentError, match="Aucun fichier") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_not_received"
    assert _ticket(world, opened["ticket_id"])["status"] == ut.STATUS_OPEN
    _put(world, opened, PDF)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert len(_documents(world)) == 1 and done["already_finalized"] is False


def test_an_expired_ticket_is_refused_and_its_late_bytes_erased(world):
    opened = _begin_document(world)
    _put(world, opened, PDF)
    staging = _staging(world, opened["ticket_id"])
    world["clock"]["now"] = NOW + ut.OPEN_WINDOW + timedelta(minutes=1)
    with pytest.raises(ToolArgumentError, match="expiré") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_ticket_closed"
    assert _ticket(world, opened["ticket_id"])["status"] == ut.STATUS_EXPIRED
    assert staging not in world["bucket"].objects
    assert _documents(world) == {}


def test_a_young_claim_refuses_a_second_finalizer(world):
    opened = _begin_document(world)
    _put(world, opened, PDF)
    ut.claim_ticket(opened["ticket_id"], now=NOW)
    world["clock"]["now"] = NOW + timedelta(minutes=2)
    with pytest.raises(ToolArgumentError, match="en cours") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_busy"
    assert _documents(world) == {}


def test_a_stale_claim_is_reclaimed_and_files_the_document_once(world):
    """A first finalizer claimed, FILED under the reserved id, and died
    before closing the ticket. Five minutes on, a second finalization
    reclaims it, finds the reserved document, closes the ticket — and never
    files a second document."""
    opened = _begin_document(world)
    _put(world, opened, PDF)
    ticket = _ticket(world, opened["ticket_id"])
    ut.claim_ticket(opened["ticket_id"], now=NOW)
    blob = ut.staged_blob(ticket)
    first, errors = document_model.ingest_blob_as_document(
        blob, "d1", "2026-001", ticket["original_filename"], {}, UID,
        document_id=ticket["reserved_document_id"])
    assert errors == [] and first is not None
    world["clock"]["now"] = NOW + ut.STALE_CLAIM_AFTER + timedelta(minutes=1)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["already_finalized"] is True
    assert done["entity"]["id"] == ticket["reserved_document_id"]
    assert list(_documents(world)) == [ticket["reserved_document_id"]]
    settled = _ticket(world, opened["ticket_id"])
    assert settled["status"] == ut.STATUS_DONE and settled["claim_count"] == 2
    assert ticket["staging_object"] not in world["bucket"].objects


def test_a_late_upload_is_never_filed_even_on_a_stale_reclaim(world):
    """Review of T9: a stale claim is reclaimed whatever the clock — right
    when the first finalizer died holding bytes that arrived in time. But
    when it died holding NOTHING (its release failed) and the PUT came
    after the hour, the reclaim filed that late upload, where the ticket,
    the consent screen and every refusal say it never is."""
    opened = _begin_document(world)
    staging = _staging(world, opened["ticket_id"])
    # A finalizer claims at 50 min, finds nothing, and dies before its
    # release lands: the claim stays held.
    ut.claim_ticket(opened["ticket_id"], now=NOW + timedelta(minutes=50))
    # The bytes arrive at 70 min — ten minutes past the hour.
    world["bucket"].put(staging, PDF, content_type="application/pdf",
                        time_created=NOW + timedelta(minutes=70))
    world["clock"]["now"] = NOW + timedelta(minutes=80)
    with pytest.raises(ToolArgumentError, match="expiré") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_ticket_closed"
    assert _documents(world) == {}
    assert staging not in world["bucket"].objects
    ticket = _ticket(world, opened["ticket_id"])
    assert ticket["status"] == ut.STATUS_REFUSED
    assert ticket["refusal_reason"] == "expire"


def test_bytes_in_time_are_filed_by_a_reclaim_past_the_hour(world):
    """The counterpart: the bytes arrived within the hour, the finalizer
    died holding them — the reclaim past the hour files them, once."""
    opened = _begin_document(world)
    staging = _staging(world, opened["ticket_id"])
    world["bucket"].put(staging, PDF, content_type="application/pdf",
                        time_created=NOW + timedelta(minutes=55))
    ut.claim_ticket(opened["ticket_id"], now=NOW + timedelta(minutes=58))
    world["clock"]["now"] = NOW + timedelta(minutes=80)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["already_finalized"] is False
    assert len(_documents(world)) == 1


def test_a_filed_ticket_answers_again_without_a_key_and_writes_nothing(world):
    opened = _begin_document(world)
    _put(world, opened, PDF)
    first = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    world["db"].reset_logs()
    again = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert again["already_finalized"] is True
    assert again["entity"]["id"] == first["entity"]["id"]
    assert len(_documents(world)) == 1
    writes = [op for c in world["db"].commits for op in c.ops]
    assert all(not path.startswith("documents/") for _kind, path in writes)


def test_a_folder_read_that_blips_at_finalize_keeps_the_bytes(world, monkeypatch):
    """Review of T9: the ingestion's own folder check fails OPEN to
    « introuvable » (models/folder.get_folder answers None on a read
    error), and on that word the ticket was SETTLED — its bytes, up to
    200 MB, consumed — for a read that merely blipped. The handler now
    re-reads the bound folder STRICTLY first: a failure releases the claim
    and the bytes wait for the retry."""
    from models import folder as folder_model

    opened = _begin_document(world, folder_id="f1")
    _put(world, opened, PDF)
    staging = _staging(world, opened["ticket_id"])

    def blip(_dossier_id):
        raise RuntimeError("503 deadline exceeded")

    all_folders, get_folder = folder_model._all_folders, folder_model.get_folder
    monkeypatch.setattr(folder_model, "_all_folders", blip)
    monkeypatch.setattr(folder_model, "get_folder", lambda *_a: None)
    with pytest.raises(ToolArgumentError) as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_retry"
    assert _ticket(world, opened["ticket_id"])["status"] == ut.STATUS_OPEN
    assert staging in world["bucket"].objects
    monkeypatch.setattr(folder_model, "_all_folders", all_folders)
    monkeypatch.setattr(folder_model, "get_folder", get_folder)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["entity"]["folder_id"] == "f1"


def test_a_folder_deleted_since_the_opening_refuses_saying_so(world):
    opened = _begin_document(world, folder_id="f1")
    _put(world, opened, PDF)
    world["db"].external_delete("folders/f1")
    with pytest.raises(ToolArgumentError, match="supprimé depuis") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_content_refused"
    assert _documents(world) == {}


def test_no_upload_ever_touches_an_existing_document_file(world):
    """The « document » NEVER, for the upload path: a document already
    stored under the ticket's reserved id (another dossier's — a collision
    no uuid4 will ever produce, planted here) is never overwritten, its
    record never rewritten; the upload is refused."""
    opened = _begin_document(world)
    _put(world, opened, PDF)
    reserved = _ticket(world, opened["ticket_id"])["reserved_document_id"]
    path = f"users/{UID}/dossiers/d2/documents/{reserved}/autre.pdf"
    world["bucket"].put(path, b"%PDF-1.4 the other client's file")
    world["db"].seed(f"documents/{reserved}", {
        **document_model._default_doc(), "id": reserved, "dossier_id": "d2",
        "filename": "autre.pdf", "file_type": "application/pdf",
        "file_size": 32, "storage_path": path, "etag": "e-autre",
        "created_at": DT, "updated_at": DT})
    before = world["db"].peek(f"documents/{reserved}")
    with pytest.raises(ToolArgumentError) as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_content_refused"
    assert world["db"].peek(f"documents/{reserved}") == before
    assert world["bucket"].objects[path].data == b"%PDF-1.4 the other client's file"
    assert list(_documents(world)) == [reserved]


def test_content_the_ingestion_refuses_settles_the_ticket(world):
    fake_pdf = b"not a pdf at all" + bytes(64)
    opened = _begin_document(world, data=fake_pdf)
    _put(world, opened, fake_pdf)
    with pytest.raises(ToolArgumentError, match="refusé et effacé") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_content_refused"
    assert _ticket(world, opened["ticket_id"])["refusal_reason"] == "contenu_refuse"
    assert _documents(world) == {}


# ══════════════════════════════════════════════════════════════════════
# 3. finalize_upload — purpose gabarit
# ══════════════════════════════════════════════════════════════════════


def test_a_template_is_created_under_its_reserved_id_with_a_neutral_name(world):
    opened = _begin_gabarit(world, CLEAN_LETTER, dossier_id="d1")
    _put(world, opened, CLEAN_LETTER)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    ticket = _ticket(world, opened["ticket_id"])
    template = world["db"].peek(f"doc_templates/{ticket['reserved_template_id']}")
    assert done["entity"]["id"] == template["id"]
    assert template["name"] == "Lettre type"
    # The uploaded file's name (« … M. Tremblay ») never enters the record.
    assert template["original_filename"] == "Lettre type.docx"
    assert "Tremblay" not in template["filename"]
    assert template["created_via"] == "mcp" and template["version"] == 1
    assert not tpl_model.is_active(template)
    # Fixups of lot 2A: the source dossier is RECORDED — a later rename is
    # checked against it — on the template and on its first version.
    assert template["source_dossier_id"] == "d1"
    assert world["db"].peek(
        f"doc_templates/{template['id']}/versions/1")["source_dossier_id"] == "d1"
    assert done["leak_scan"]["performed"] is True
    assert done["replaced_version"] is None
    assert ticket["status"] == ut.STATUS_DONE
    assert ticket["staging_object"] not in world["bucket"].objects


def test_a_residue_of_the_source_dossier_refuses_naming_it(world):
    opened = _begin_gabarit(world, LEAKY_LETTER, dossier_id="d1")
    _put(world, opened, LEAKY_LETTER)
    staging = _staging(world, opened["ticket_id"])
    before = set(_templates(world))
    with pytest.raises(ToolArgumentError) as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_residue"
    assert "« Jean Tremblay »" in str(exc.value) and "corps" in str(exc.value)
    assert "accept_residual" in str(exc.value)
    assert _ticket(world, opened["ticket_id"])["refusal_reason"] == (
        "identifiants_residuels")
    assert staging not in world["bucket"].objects
    assert set(_templates(world)) == before
    # Accepted by the lawyer, at a NEW ticket: filed, and said so.
    retry = _begin_gabarit(world, LEAKY_LETTER, dossier_id="d1",
                           accept_residual=["Jean Tremblay", "Tremblay"])
    _put(world, retry, LEAKY_LETTER)
    done = handlers.finalize_upload({"ticket_id": retry["ticket_id"]})
    assert done["leak_scan"]["accepted"] == 2
    assert any("acceptés" in w for w in done["warnings"])


def test_a_template_name_naming_the_source_dossier_is_refused_at_the_opening(world):
    """Review of T9: the file scan never read the template's NAME, and the
    name prints into the name of every document generated from it, for
    every future client (« 2026-014 - … - Projet Mise en demeure
    Tremblay »), and becomes its « neutral » file name. It is judged at the
    OPENING — refused, nothing opened, no bytes lost — naming the dossier's
    identifier, never quoting the name the caller sent."""
    with pytest.raises(ToolArgumentError) as exc:
        _begin_gabarit(world, CLEAN_LETTER, dossier_id="d1",
                       name="Mise en demeure Tremblay")
    assert exc.value.reason == "upload_residue"
    assert "« Tremblay »" in str(exc.value) and "`name`" in str(exc.value)
    assert "Mise en demeure" not in str(exc.value)
    assert world["db"].peek_collection(ut.COLLECTION) == {}
    assert world["bucket"].sessions == {}
    # Accepted on the lawyer's word: opened, and said so.
    opened = _begin_gabarit(world, CLEAN_LETTER, dossier_id="d1",
                            name="Mise en demeure Tremblay",
                            accept_residual=["Tremblay"])
    assert any("NOM du gabarit" in w for w in opened["warnings"])
    _put(world, opened, CLEAN_LETTER)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["entity"]["name"] == "Mise en demeure Tremblay"


def test_the_name_check_fails_closed_on_an_unreadable_party(world):
    world["db"].seed("dossiers/d1", {
        **world["db"].peek("dossiers/d1"),
        "clients": [{"id": "fantome", "name": "X", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
        "client_ids": ["fantome"]})
    with pytest.raises(ToolArgumentError, match="toutes les parties") as exc:
        _begin_gabarit(world, CLEAN_LETTER, dossier_id="d1")
    assert exc.value.reason == "upload_retry"
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def test_a_replacement_keeps_its_name_and_is_not_name_checked(world):
    """A replacement changes the FILE only — its name is the lawyer's, and
    is not re-judged (the file itself still is, against the dossier)."""
    tid = world["template"]
    world["db"].seed(f"doc_templates/{tid}", {
        **world["db"].peek(f"doc_templates/{tid}"),
        "name": "Lettre Tremblay"})
    opened = handlers.begin_upload(_replace_args(world, dossier_id="d1"))
    assert opened["opened"] is True


def test_a_declared_absence_of_source_dossier_is_echoed_at_both_steps(world):
    """Rewritten deliberately (fixups of lot 2A): the scan is skipped ONLY
    on the caller's DECLARATION (`aucun_dossier_source: true`), and that
    declaration is echoed — at the opening and again at the filing — as
    the caller's word, never as a check."""
    opened = _begin_gabarit(world, LEAKY_LETTER)
    assert any("par déclaration (aucun_dossier_source)" in w
               and "PAS contrôlés" in w for w in opened["warnings"])
    assert _ticket(world, opened["ticket_id"])["template_params"][
        "aucun_dossier_source"] is True
    _put(world, opened, LEAKY_LETTER)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["leak_scan"]["performed"] is False
    assert any("par déclaration (aucun_dossier_source)" in w
               for w in done["warnings"])
    created = world["db"].peek(f"doc_templates/{done['entity']['id']}")
    assert created["source_dossier_id"] == ""


@pytest.mark.parametrize("mode", ["create", "replace"])
def test_a_gabarit_ticket_naming_no_source_dossier_is_refused_unless_declared(
    world, mode,
):
    """Fixups of lot 2A — FAILS on the old handler, which opened the ticket
    and filed the template with NO leak scan when dossier_id was simply
    left out."""
    args = (dict(_GABARIT_BASE) if mode == "create"
            else _replace_args(world))
    args.pop("aucun_dossier_source")
    with pytest.raises(ToolArgumentError,
                       match="aucun_dossier_source: true") as exc:
        handlers.begin_upload(args)
    assert "Rien n'a été ouvert" in str(exc.value)
    assert world["db"].peek_collection(ut.COLLECTION) == {}
    for bad in (False, "oui"):
        with pytest.raises(ToolArgumentError):
            handlers.begin_upload({**args, "aucun_dossier_source": bad})
    with pytest.raises(ToolArgumentError, match="se contredisent"):
        handlers.begin_upload({**args, "dossier_id": "d1",
                               "aucun_dossier_source": True})
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def test_replacing_the_active_template_s_file_always_names_its_source(world):
    """The case the critic named: the ACTIVE note-d'honoraires template's
    file, replaced through the ticket, with no dossier and no declaration —
    refused before anything opens."""
    data = _docx("Note : {{facture.numero}}")
    tpl, errors = tpl_model.create_template(
        io.BytesIO(data), "n.docx", len(data),
        {"name": "Note d'honoraires", "category": "autre",
         "kind": "note_honoraires"}, UID)
    assert errors == []
    tpl_model.set_active_template(tpl["id"], par="juriste", expected_etag=None)
    new = _docx("Note v2 : {{facture.numero}}")
    with pytest.raises(ToolArgumentError, match="aucun_dossier_source"):
        handlers.begin_upload({
            "purpose": "gabarit", "template_mode": "replace",
            "template_id": tpl["id"], "expected_version": 1,
            "filename": "n.docx", "size_bytes": len(new),
            "md5_base64": _md5(new)})
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def test_a_document_ticket_takes_no_source_declaration(world):
    with pytest.raises(ToolArgumentError, match="ne s'applique pas"):
        _begin_document(world, aucun_dossier_source=True)
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def test_a_stored_ticket_with_neither_dossier_nor_declaration_is_never_filed(
    world,
):
    """A ticket opened before the fixups (no declaration bound, no dossier)
    reaching finalize is refused and settled — fail CLOSED, never filed
    unchecked. FAILS on the old handler, which filed it."""
    opened = _begin_gabarit(world, LEAKY_LETTER)
    ticket = _ticket(world, opened["ticket_id"])
    params = dict(ticket["template_params"])
    params.pop("aucun_dossier_source")
    world["db"].seed(f"{ut.COLLECTION}/{opened['ticket_id']}",
                     {**ticket, "template_params": params})
    _put(world, opened, LEAKY_LETTER)
    before = set(_templates(world))
    with pytest.raises(ToolArgumentError, match="aucun_dossier_source") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_residue"
    after = _ticket(world, opened["ticket_id"])
    assert after["status"] == ut.STATUS_REFUSED
    assert after["refusal_reason"] == "sans_dossier_source"
    assert set(_templates(world)) == before


def test_an_unreadable_party_fails_the_scan_closed(world):
    # The party becomes unreadable AFTER the opening — rewritten at the
    # review of T9: the opening now reads the identifiers too (the name
    # check), so a party unreadable from the start refuses there; the
    # finalize-side scan must still fail closed on its own read.
    opened = _begin_gabarit(world, CLEAN_LETTER, dossier_id="d1")
    world["db"].seed("dossiers/d1", {
        **world["db"].peek("dossiers/d1"),
        "clients": [{"id": "fantome", "name": "X", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
        "client_ids": ["fantome"]})
    _put(world, opened, CLEAN_LETTER)
    with pytest.raises(ToolArgumentError, match="toutes les parties") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "upload_retry"
    ticket = _ticket(world, opened["ticket_id"])
    assert ticket["status"] == ut.STATUS_OPEN                 # released
    assert ticket["staging_object"] in world["bucket"].objects   # kept


def test_scrubbing_the_properties_clears_a_residue_there(world):
    data = _docx("Objet : {{objet_lettre}}", creator="Jean Tremblay")
    opened = _begin_gabarit(world, data, dossier_id="d1")
    _put(world, opened, data)
    with pytest.raises(ToolArgumentError, match="scrub_properties"):
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    retry = _begin_gabarit(world, data, dossier_id="d1", scrub_properties=True)
    _put(world, retry, data)
    done = handlers.finalize_upload({"ticket_id": retry["ticket_id"]})
    assert done["scrubbed_properties"] == ["dc:creator"]
    stored = world["bucket"].objects[
        world["db"].peek(f"doc_templates/{done['entity']['id']}")["storage_path"]]
    assert b"Tremblay" not in zipfile.ZipFile(io.BytesIO(stored.data)).read(
        "docProps/core.xml")


def test_a_special_kind_is_created_but_never_designated(world):
    opened = _begin_gabarit(world, CLEAN_LETTER, kind="note_honoraires",
                            category="autre")
    _put(world, opened, CLEAN_LETTER)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["entity"]["kind"] == "note_honoraires"
    assert done["entity"]["active"] is False
    assert tpl_model.get_active_template("note_honoraires") is None
    assert any("n'est PAS le gabarit actif" in w for w in done["warnings"])


def test_a_stale_template_creation_is_reclaimed_without_a_second_template(world):
    opened = _begin_gabarit(world, CLEAN_LETTER)
    _put(world, opened, CLEAN_LETTER)
    ticket = _ticket(world, opened["ticket_id"])
    ut.claim_ticket(opened["ticket_id"], now=NOW)
    first, errors = tpl_model.create_template(
        io.BytesIO(CLEAN_LETTER), "Lettre type.docx", len(CLEAN_LETTER),
        {"name": "Lettre type", "category": "autre", "kind": "gabarit"}, UID,
        template_id=ticket["reserved_template_id"])
    assert errors == []
    count = len(_templates(world))
    world["clock"]["now"] = NOW + ut.STALE_CLAIM_AFTER + timedelta(minutes=1)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["already_finalized"] is True
    assert done["entity"]["id"] == first["id"]
    assert len(_templates(world)) == count


def test_a_replacement_installs_a_new_version_and_keeps_the_old(world):
    tid = world["template"]
    before = world["db"].peek(f"doc_templates/{tid}")
    new = _docx("Version deux : {{objet_lettre}}")
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True, "template_id": tid,
        "expected_version": 1, "filename": "Lettre Tremblay.docx",
        "size_bytes": len(new), "md5_base64": _md5(new)})
    _put(world, opened, new)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    after = world["db"].peek(f"doc_templates/{tid}")
    assert after["version"] == 2 and done["replaced_version"] == 1
    assert after["original_filename"] == "Lettre de base.docx"
    assert world["bucket"].objects[after["storage_path"]].data == new
    assert world["bucket"].objects[before["storage_path"]].data  # v1 kept
    assert world["db"].peek(f"doc_templates/{tid}/versions/1") is not None
    assert _ticket(world, opened["ticket_id"])["staged_sha256"] == (
        hashlib.sha256(new).hexdigest())
    assert done["entity"]["version"] == 2
    # Declared from no dossier: the new version records none.
    assert world["db"].peek(
        f"doc_templates/{tid}/versions/2")["source_dossier_id"] == ""


def test_the_active_template_s_file_can_be_replaced_and_the_consent_says_so(world):
    """Review of T9: the consent screen promised « Le gabarit actif des
    notes d'honoraires et des notes ne change jamais sans vous » — yet a
    replacement through the ticket installs a new FILE for that very
    template, printed at once on every invoice note. What Claude never does
    is DESIGNATE it (D11). The behaviour, then the words that describe it."""
    data = _docx("Note : {{facture.numero}}")
    tpl, errors = tpl_model.create_template(
        io.BytesIO(data), "n.docx", len(data),
        {"name": "Note d'honoraires", "category": "autre",
         "kind": "note_honoraires"}, UID)
    assert errors == []
    active, errors = tpl_model.set_active_template(
        tpl["id"], par="juriste", expected_etag=None)
    assert errors == [] and tpl_model.is_active(active)
    new = _docx("Note v2 : {{facture.numero}}")
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True,
        "template_id": tpl["id"], "expected_version": 1, "filename": "n.docx",
        "size_bytes": len(new), "md5_base64": _md5(new)})
    assert any("ACTIF" in w for w in opened["warnings"])
    _put(world, opened, new)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["entity"]["version"] == 2 and done["entity"]["active"] is True
    assert tpl_model.get_active_template("note_honoraires")["id"] == tpl["id"]
    # REWRITTEN in lot 2A (T11): the active-template sentences moved with
    # the template tools to the TEMPLATES family's own partial.
    consent = (_ATHENA / "templates" / "mcp" / "families"
               / "_templates.html").read_text(encoding="utf-8")
    flat = " ".join(consent.split())
    assert "change jamais sans vous" not in flat
    assert "nouvelle version du fichier de ce gabarit actif" in flat
    assert "<strong>désigne</strong> jamais le gabarit actif" in flat


def _replace_through_ticket(world, template_id: str, new: bytes) -> dict:
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True,
        "template_id": template_id, "expected_version": 1,
        "filename": "x.docx", "size_bytes": len(new), "md5_base64": _md5(new)})
    _put(world, opened, new)
    return handlers.finalize_upload({"ticket_id": opened["ticket_id"]})


def test_a_new_file_for_the_active_template_says_it_prints_now(world):
    """Critique de complétude du lot 2A (parité relevée par la revue de
    T10) : update_template disait, pour un nouveau fichier du gabarit
    ACTIF, qu'il s'imprime dès maintenant, quels champs il perd, et qu'une
    « Note (impression) » sans {{note.contenu}} imprime désormais des notes
    SANS leur texte. Le même remplacement par le ticket ne disait rien de
    tout cela. Échoue sur le gestionnaire d'avant."""
    data = _docx("Note : {{note.contenu}} {{note.titre}}")
    tpl, errors = tpl_model.create_template(
        io.BytesIO(data), "n.docx", len(data),
        {"name": "Impression", "category": "autre", "kind": "note"}, UID)
    assert errors == []
    _active, errors = tpl_model.set_active_template(
        tpl["id"], par="juriste", expected_etag=None)
    assert errors == []

    done = _replace_through_ticket(world, tpl["id"], _docx("Note : {{note.titre}}"))

    assert done["replaced_version"] == 1 and done["entity"]["active"] is True
    text = " ".join(done["warnings"])
    assert "gabarit ACTIF" in text and "s'imprime dès maintenant" in text
    assert "SANS son texte" in text and "rétablir la version 1" in text
    assert "ne figurent plus" in text and "{{note.contenu}}" in text


def test_a_new_file_for_an_ordinary_template_names_only_what_it_loses(world):
    done = _replace_through_ticket(world, world["template"],
                                   _docx("Version deux, sans champ"))
    text = " ".join(done["warnings"])
    assert "ACTIF" not in text and "SANS son texte" not in text
    assert "ne figurent plus" in text and "{{objet_lettre}}" in text


def test_a_replayed_replacement_reports_what_it_did_not_what_came_after(world):
    """Review of T9: the replay rebuilt « replaced_version » from the
    template's CURRENT version, so a later replacement (v2 → v3) made the
    answer of this ticket (v1 → v2) read « nothing replaced »."""
    tid = world["template"]
    new = _docx("Version deux : {{objet_lettre}}")
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True, "template_id": tid,
        "expected_version": 1, "filename": "x.docx",
        "size_bytes": len(new), "md5_base64": _md5(new)})
    _put(world, opened, new)
    first = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert first["replaced_version"] == 1
    later = _docx("Version trois : {{objet_lettre}}")
    _t, errors, _c = tpl_model.update_template(
        tid, {}, io.BytesIO(later), "l.docx", len(later))
    assert errors == []
    again = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert again["already_finalized"] is True
    assert again["replaced_version"] == 1
    assert again["entity"]["version"] == 3        # the template AS STORED


def test_a_replacement_refuses_a_version_that_moved_since(world):
    tid = world["template"]
    new = _docx("Version deux : {{objet_lettre}}")
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True, "template_id": tid,
        "expected_version": 1, "filename": "x.docx",
        "size_bytes": len(new), "md5_base64": _md5(new)})
    other = _docx("Autre main : {{objet_lettre}}")
    _t, errors, _c = tpl_model.update_template(
        tid, {}, io.BytesIO(other), "o.docx", len(other))
    assert errors == []
    _put(world, opened, new)
    with pytest.raises(ToolArgumentError, match="version 2") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "stale_etag"
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert stored["version"] == 2 and stored["sha256"] == (
        hashlib.sha256(other).hexdigest())


def test_an_identical_replacement_installs_nothing(world):
    tid = world["template"]
    same = world["bucket"].objects[
        world["db"].peek(f"doc_templates/{tid}")["storage_path"]].data
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True, "template_id": tid,
        "expected_version": 1, "filename": "x.docx",
        "size_bytes": len(same), "md5_base64": _md5(same)})
    _put(world, opened, same)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["replaced_version"] is None
    assert world["db"].peek(f"doc_templates/{tid}")["version"] == 1
    assert any("identique" in w for w in done["warnings"])


def test_a_landed_replacement_is_recognised_on_reclaim(world):
    """A first finalizer recorded the staged digest, replaced the file, and
    died before closing the ticket: the reclaim recognises the version it
    installed and never installs a third."""
    tid = world["template"]
    new = _docx("Version deux : {{objet_lettre}}")
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True, "template_id": tid,
        "expected_version": 1, "filename": "x.docx",
        "size_bytes": len(new), "md5_base64": _md5(new)})
    _put(world, opened, new)
    claim = ut.claim_ticket(opened["ticket_id"], now=NOW)
    assert ut.record_staged_digest(opened["ticket_id"], claim_id=claim.claim_id,
                                   sha256=hashlib.sha256(new).hexdigest())
    _t, errors, _c = tpl_model.update_template(
        tid, {}, io.BytesIO(new), "x.docx", len(new), expected_version=1)
    assert errors == []
    world["clock"]["now"] = NOW + ut.STALE_CLAIM_AFTER + timedelta(minutes=1)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["already_finalized"] is True and done["replaced_version"] == 1
    assert world["db"].peek(f"doc_templates/{tid}")["version"] == 2


# ══════════════════════════════════════════════════════════════════════
# 4. Registry, constants, never
# ══════════════════════════════════════════════════════════════════════


def test_the_schema_literals_are_the_model_constants():
    assert tools.UPLOAD_FILENAME_MAX_CHARS == ut.MAX_FILENAME_CHARS
    assert tools.UPLOAD_DOCUMENT_MAX_BYTES == document_model.MAX_FILE_SIZE
    assert tools.UPLOAD_TEMPLATE_MAX_BYTES == tpl_model.MAX_TEMPLATE_SIZE
    assert tools.UPLOAD_ACCEPT_MAX_ITEMS == ut.MAX_LIST_ITEMS
    assert tools.TEMPLATE_NAME_MAX_CHARS == tpl_model.NAME_MAX
    assert tools.TEMPLATE_DESCRIPTION_MAX_CHARS == tpl_model.DESCRIPTION_MAX
    props = tools.TOOLS["begin_upload"]["input_schema"]["properties"]
    assert set(props["kind"]["enum"]) == set(tpl_model.VALID_KINDS)
    assert set(tpl_model.VALID_CATEGORIES) <= set(props["category"]["enum"])


def test_the_upload_tools_carry_the_hints_they_owe():
    assert {"begin_upload", "finalize_upload"} <= tools.WRITE_TOOLS
    assert "finalize_upload" in tools.EDIT_TOOLS          # replaces a file
    assert "begin_upload" not in tools.EDIT_TOOLS
    assert "begin_upload" in write_support.persistence_tools()
    spec = tools.TOOLS["finalize_upload"]
    assert spec["concurrency"] == tools.CONCURRENCY_EXEMPT
    assert spec["annotations"] == {"idempotentHint": True}
