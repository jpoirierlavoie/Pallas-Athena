"""Les tickets de téléversement du connecteur (lot 2A, étape T5 ; D4).

``models/upload_ticket.py`` est l'enregistrement de l'échange
``begin_upload`` → PUT du bac à sable → ``finalize_upload`` (les outils
arrivent avec la version MCP du lot). Tout tourne sur le faux Firestore
PARTAGÉ (``tests/_fake_firestore.py`` — le client est le vrai, le serveur
refuse ce que Firestore refuse), et l'on relit ce qui est STOCKÉ.

Épinglé :

1. La création LIE tout ce que la finalisation utilisera (destination,
   taille et MD5 déclarés, métadonnées), frappe les identifiants RÉSERVÉS,
   nomme un objet de staging NEUTRE sous l'uid du propriétaire — que le
   finaliseur web refuse —, n'écrase jamais un ticket, et REFUSE tout ce
   qu'elle ne reconnaît pas plutôt que de le retoucher. Aucune URL, jamais.
2. La machine d'états : en_attente → en_cours (réservation transactionnelle,
   claimed_at) → versé | refusé ; l'expiration au-delà d'une heure, appliquée
   par le CODE à la réservation ; une réservation de plus de 5 minutes
   reprise (les identifiants réservés rendent la reprise sûre) ; deux
   réservations concurrentes, une seule gagne.
3. Le TTL : ``expire_at`` porte +24 h sur un ticket ouvert et +7 jours sur un
   ticket réglé, pour que la réponse d'une finalisation survive à ses
   rejeux ; le fieldOverride est déclaré, aucun index composite.
4. Les points de validation : création et versement notent leur commit ;
   réservation, libération, empreinte et refus NON — une finalisation qui
   refuse après eux doit rester un refus, jamais « ENREGISTRÉE ».
5. Échec FERMÉ : un ticket illisible n'est jamais « absent ».
"""

import ast
import base64
import hashlib
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import models.upload_ticket as ut
    import routes.documents as rd
    from mcp.write_support import capability_in
    from models import provenance

from tests._fake_firestore import install  # noqa: E402
from utils import storage_identity as si  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
REAL_UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
DOSSIER_ID = "9d1c2b3a-4e5f-4a6b-8c7d-0e1f2a3b4c5d"
TEMPLATE_ID = "1b2c3d4e-5f60-4718-8a9b-0c1d2e3f4a5b"
ATHENA = pathlib.Path(__file__).resolve().parent.parent
PDF = b"%PDF-1.7 synthetic body"
MD5 = base64.b64encode(hashlib.md5(PDF).digest()).decode()


@pytest.fixture()
def fake(monkeypatch):
    return install(monkeypatch, ut)


def _document_data(**over) -> dict:
    data = {
        "purpose": "document",
        "dossier_id": DOSSIER_ID,
        "filename": "Lettre du client.pdf",
        "declared_size": len(PDF),
        "declared_md5_b64": MD5,
        "bound_metadata": {"category": "correspondance",
                           "display_name": "Lettre du 12 septembre",
                           "tags": ["client"], "document_date": NOW},
    }
    data.update(over)
    return data


def _gabarit_data(mode="create", **over) -> dict:
    params = ({"mode": "create", "name": "Lettre type", "category": "autre",
               "kind": "gabarit", "description": "", "accept_residual": [],
               "scrub_properties": True}
              if mode == "create" else
              {"mode": "replace", "template_id": TEMPLATE_ID,
               "expected_version": 3})
    data = {
        "purpose": "gabarit",
        "dossier_id": DOSSIER_ID,
        "filename": "lettre.docx",
        "declared_size": 4096,
        "declared_md5_b64": MD5,
        "template_params": params,
    }
    data.update(over)
    return data


def _open(fake, data=None, *, now=NOW) -> dict:
    ticket, errors = ut.create_ticket(data or _document_data(),
                                      user_id=REAL_UID, now=now)
    assert errors == [] and ticket is not None, errors
    return ticket


def _stored(fake, ticket_id: str) -> dict:
    return fake.peek(f"{ut.COLLECTION}/{ticket_id}")


def _writes(fake) -> list:
    """Every write op the server applied since the last reset. A read-write
    transaction that writes nothing still COMMITS (empty), as the real
    client does — so « no write » is « no op », not « no commit »."""
    return [op for c in fake.commits for op in c.ops]


def _claimed(fake, *, now=NOW) -> tuple[dict, ut.TicketClaim]:
    ticket = _open(fake, now=now)
    claim = ut.claim_ticket(ticket["id"], now=now + timedelta(minutes=1))
    assert claim.state == ut.CLAIMED, claim
    return ticket, claim


# ══════════════════════════════════════════════════════════════════════
# 1. Création
# ══════════════════════════════════════════════════════════════════════


def test_a_document_ticket_binds_everything_finalize_will_use(fake):
    with provenance.writing_via("mcp", tool="begin_upload"):
        ticket = _open(fake)
        commits = provenance.committed_writes()
    stored = _stored(fake, ticket["id"])
    assert stored["status"] == "en_attente"
    assert stored["purpose"] == "document"
    assert stored["dossier_id"] == DOSSIER_ID
    assert stored["original_filename"] == "Lettre du client.pdf"
    assert stored["ext"] == ".pdf"
    assert stored["declared_size"] == len(PDF)
    assert stored["declared_md5_b64"] == MD5
    assert stored["bound_metadata"]["category"] == "correspondance"
    assert stored["bound_metadata"]["tags"] == ["client"]
    assert stored["template_params"] == {}
    # the ids are MINTED, canonical, and distinct
    assert ut.is_canonical_uuid4(stored["id"])
    assert ut.is_canonical_uuid4(stored["reserved_document_id"])
    assert stored["reserved_document_id"] != stored["id"]
    assert stored["reserved_template_id"] == ""
    # a NEUTRAL staging object under the owner's uid — no client file name
    assert stored["staging_object"] == (
        f"staging/{REAL_UID}/mcp/{stored['id']}/upload.pdf")
    assert "Lettre" not in stored["staging_object"]
    # the window, and the TTL field one idempotency window past it
    assert stored["open_until"] == NOW + ut.OPEN_WINDOW
    assert stored["expire_at"] == NOW + ut.OPEN_WINDOW + timedelta(hours=24)
    assert stored["claim_id"] == "" and stored["claimed_at"] is None
    # stamped (Rule 7 + provenance) and its commit noted
    assert stored["created_via"] == "mcp" and stored["updated_via"] == "mcp"
    assert stored["etag"]
    assert commits == ((ut.COLLECTION, stored["id"]),)
    # what the caller got IS what was stored
    assert ticket["id"] == stored["id"]
    assert not capability_in(stored)


def test_a_template_ticket_reserves_a_template_id_only_to_create_one(fake):
    created = _open(fake, _gabarit_data("create"))
    assert ut.is_canonical_uuid4(created["reserved_template_id"])
    assert created["reserved_document_id"] == ""
    assert created["template_params"]["name"] == "Lettre type"
    assert created["template_params"]["scrub_properties"] is True
    assert created["staging_object"].endswith("/upload.docx")

    replacing = _open(fake, _gabarit_data("replace"))
    assert replacing["reserved_template_id"] == ""
    assert replacing["template_params"] == {
        "mode": "replace", "template_id": TEMPLATE_ID, "expected_version": 3,
        "accept_residual": [], "scrub_properties": False}


def test_the_declared_md5_is_stored_in_its_canonical_spelling(fake):
    """Two spellings of one digest (the trailing bits base64 ignores) must
    compare equal to what GCS reports."""
    zeros = base64.b64encode(bytes(16)).decode()        # "AAAA…AA=="
    assert zeros.endswith("A==")
    odd = zeros[:-3] + "B=="                            # same 16 bytes
    assert base64.b64decode(odd) == bytes(16)
    ticket = _open(fake, _document_data(declared_md5_b64=odd))
    assert ticket["declared_md5_b64"] == zeros


@pytest.mark.parametrize("over, fragment", [
    ({"id": "x"}, "Champ non reconnu"),
    ({"upload_url": "https://x"}, "Champ non reconnu"),
    ({"purpose": "photo"}, "document"),
    ({"filename": ""}, "nom du fichier"),
    ({"filename": "../a.pdf"}, "barre oblique"),
    ({"filename": "a<b>.pdf"}, "chevrons"),
    ({"filename": "a" * 201 + ".pdf"}, "200"),
    ({"filename": "script.exe"}, "type de fichier"),
    ({"declared_size": True}, "entier"),
    ({"declared_size": 0}, "entier"),
    ({"declared_size": 200 * 1024 * 1024 + 1}, "200 Mo"),
    ({"declared_md5_b64": "abc"}, "MD5"),
    ({"declared_md5_b64": "!" * 24}, "MD5"),
    ({"declared_md5_b64": base64.b64encode(bytes(17)).decode()[:24]}, "MD5"),
    ({"dossier_id": ""}, "dossier"),
    ({"dossier_id": "a/b"}, "dossier"),
    ({"template_params": {"mode": "create", "name": "x"}}, "paramètres de gabarit"),
    ({"bound_metadata": {"storage_path": "users/x"}}, "Métadonnée non reconnue"),
    ({"bound_metadata": {"document_date": "2026-09-01"}}, "date"),
    ({"bound_metadata": {"tags": ["a"] * 51}}, "50"),
    ({"bound_metadata": {"display_name": "<b>x</b>"}}, "chevrons"),
])
def test_a_document_ticket_refuses_rather_than_mangles(fake, over, fragment):
    ticket, errors = ut.create_ticket(_document_data(**over),
                                      user_id=REAL_UID, now=NOW)
    assert ticket is None
    assert any(fragment in e for e in errors), errors
    assert fake.peek_collection(ut.COLLECTION) == {}


@pytest.mark.parametrize("over, fragment", [
    ({"filename": "lettre.pdf"}, ".docx"),
    ({"declared_size": 10 * 1024 * 1024 + 1}, "10 Mo"),
    ({"bound_metadata": {"category": "x"}}, "métadonnées de document"),
    ({"template_params": None}, "objet"),
    ({"template_params": {"mode": "edit"}}, "create"),
    ({"template_params": {"mode": "create"}}, "name"),
    ({"template_params": {"mode": "create", "name": "x",
                          "template_id": TEMPLATE_ID}}, "existant"),
    ({"template_params": {"mode": "replace", "expected_version": 1}}, "remplacer"),
    ({"template_params": {"mode": "replace", "template_id": TEMPLATE_ID,
                          "expected_version": 0}}, "expected_version"),
    ({"template_params": {"mode": "replace", "template_id": TEMPLATE_ID,
                          "expected_version": True}}, "expected_version"),
    ({"template_params": {"mode": "replace", "template_id": TEMPLATE_ID,
                          "expected_version": 2, "name": "x"}}, "métadonnées"),
    ({"template_params": {"mode": "create", "name": "x",
                          "accept_residual": ["a"] * 51}}, "50"),
    ({"template_params": {"mode": "create", "name": "x",
                          "scrub_properties": "oui"}}, "scrub_properties"),
    ({"template_params": {"mode": "create", "name": "x", "signed_url": "u"}},
     "non reconnu"),
])
def test_a_template_ticket_refuses_rather_than_mangles(fake, over, fragment):
    ticket, errors = ut.create_ticket(_gabarit_data(**over),
                                      user_id=REAL_UID, now=NOW)
    assert ticket is None
    assert any(fragment in e for e in errors), errors
    assert fake.peek_collection(ut.COLLECTION) == {}


def test_a_template_ticket_may_have_no_dossier(fake):
    """Without a dossier the leak scan cannot run — the handler decides
    what that means; the record allows it."""
    ticket = _open(fake, _gabarit_data(dossier_id=""))
    assert ticket["dossier_id"] == ""


@pytest.mark.parametrize("bad", ["", None, "unknown", "a/b", 42])
def test_an_unusable_owner_uid_refuses_and_writes_nothing(fake, bad):
    ticket, errors = ut.create_ticket(_document_data(), user_id=bad, now=NOW)
    assert ticket is None and errors == [si.INVALID_UID_MESSAGE]
    assert fake.peek_collection(ut.COLLECTION) == {}


def test_a_ticket_is_never_overwritten(fake, monkeypatch):
    clash = "2f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f"
    fake.seed(f"{ut.COLLECTION}/{clash}", {"id": clash, "status": "versé"})
    monkeypatch.setattr(ut.uuid, "uuid4", lambda: uuid.UUID(clash))
    ticket, errors = ut.create_ticket(_document_data(), user_id=REAL_UID, now=NOW)
    assert ticket is None and errors
    assert _stored(fake, clash) == {"id": clash, "status": "versé"}


def test_a_store_failure_on_create_refuses(monkeypatch):
    broken = mock.Mock()
    broken.collection.return_value.document.return_value.create.side_effect = (
        RuntimeError("down"))
    monkeypatch.setattr(ut, "db", broken)
    ticket, errors = ut.create_ticket(_document_data(), user_id=REAL_UID, now=NOW)
    assert ticket is None and "rien n'a été ouvert" in errors[0]


# ══════════════════════════════════════════════════════════════════════
# 2. La machine d'états
# ══════════════════════════════════════════════════════════════════════


def test_a_claim_takes_an_open_ticket_transactionally(fake):
    ticket = _open(fake)
    at = NOW + timedelta(minutes=3)
    fake.reset_logs()
    claim = ut.claim_ticket(ticket["id"], now=at)
    assert claim.state == ut.CLAIMED and claim.holds
    assert ut.is_canonical_uuid4(claim.claim_id)
    stored = _stored(fake, ticket["id"])
    assert stored["status"] == "en_cours"
    assert stored["claim_id"] == claim.claim_id
    assert stored["claimed_at"] == at
    assert stored["claim_count"] == 1
    assert claim.ticket == stored
    # the read was made INSIDE the transaction that wrote
    assert fake.reads and all(r.transactional for r in fake.reads)


def test_a_young_claim_refuses_a_second_finalizer(fake):
    ticket, first = _claimed(fake)
    second = ut.claim_ticket(ticket["id"],
                             now=NOW + timedelta(minutes=1, seconds=299))
    assert second.state == ut.REFUSED and second.reason == ut.REASON_BUSY
    assert "en cours" in second.message
    assert _stored(fake, ticket["id"])["claim_id"] == first.claim_id


def test_a_stale_claim_is_reclaimed_and_the_reserved_ids_stay(fake):
    ticket, first = _claimed(fake)
    late = NOW + timedelta(minutes=1) + ut.STALE_CLAIM_AFTER
    second = ut.claim_ticket(ticket["id"], now=late)
    assert second.state == ut.RECLAIMED and second.holds
    assert second.claim_id != first.claim_id
    stored = _stored(fake, ticket["id"])
    assert stored["claim_id"] == second.claim_id
    assert stored["claim_count"] == 2
    # the reservation is what makes a reclaim safe: the same ids
    assert stored["reserved_document_id"] == ticket["reserved_document_id"]
    assert stored["staging_object"] == ticket["staging_object"]
    # the stale holder can no longer act on it
    assert ut.release_ticket(ticket["id"], claim_id=first.claim_id) is False
    assert _stored(fake, ticket["id"])["status"] == "en_cours"


def test_a_stale_claim_is_reclaimed_even_past_the_window(fake):
    """The first claim was made in time; the bytes may already be filed
    under the reserved id — expiring now would contradict a committed
    document."""
    ticket, _first = _claimed(fake)
    much_later = NOW + ut.OPEN_WINDOW + timedelta(hours=2)
    assert ut.claim_ticket(ticket["id"], now=much_later).state == ut.RECLAIMED


def test_an_open_ticket_past_its_window_expires_at_the_claim(fake):
    ticket = _open(fake)
    at = NOW + ut.OPEN_WINDOW
    claim = ut.claim_ticket(ticket["id"], now=at)
    assert claim.state == ut.REFUSED and claim.reason == ut.REASON_EXPIRED
    assert "expiré" in claim.message
    stored = _stored(fake, ticket["id"])
    assert stored["status"] == "expiré" and stored["refusal_reason"] == "expire"
    assert stored["finalized_at"] == at
    assert stored["expire_at"] == at + ut.FINAL_RETENTION
    # and it stays expired, without another write
    fake.reset_logs()
    again = ut.claim_ticket(ticket["id"], now=at + timedelta(minutes=1))
    assert again.reason == ut.REASON_EXPIRED and _writes(fake) == []


def test_settled_tickets_answer_without_writing(fake):
    ticket, claim = _claimed(fake)
    ut.complete_ticket(ticket["id"], claim_id=claim.claim_id,
                       result={"document_id": ticket["reserved_document_id"]},
                       now=NOW + timedelta(minutes=2))
    fake.reset_logs()
    done = ut.claim_ticket(ticket["id"], now=NOW + timedelta(minutes=3))
    assert done.state == ut.DONE and not done.holds
    assert done.ticket["result"] == {"document_id": ticket["reserved_document_id"]}
    assert _writes(fake) == []

    other, other_claim = _claimed(fake)
    ut.refuse_ticket(other["id"], claim_id=other_claim.claim_id,
                     reason="empreinte_differente", now=NOW + timedelta(minutes=2))
    refused = ut.claim_ticket(other["id"], now=NOW + timedelta(minutes=3))
    assert refused.state == ut.REFUSED and refused.reason == ut.REASON_REFUSED


def test_an_unknown_ticket_is_introuvable_and_a_foreign_id_is_never_read(fake):
    missing = ut.claim_ticket("3f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f", now=NOW)
    assert missing.state == ut.REFUSED and missing.reason == ut.REASON_NOT_FOUND
    fake.reset_logs()
    for foreign in ("..", "../x", "ABC", "", None, 3):
        assert ut.claim_ticket(foreign, now=NOW).reason == ut.REASON_NOT_FOUND
    assert fake.reads == []


def test_two_racing_claims_yield_exactly_one_holder(fake):
    """A concurrent claim lands between this claim's read and its commit:
    the transaction aborts, re-reads, and sees a YOUNG claim — refused."""
    ticket = _open(fake)
    path = f"{ut.COLLECTION}/{ticket['id']}"
    rival = str(uuid.uuid4())
    at = NOW + timedelta(minutes=1)
    fired = []

    def racer(info):
        # The FIRST commit that updates the ticket is the claim's: the rival
        # lands just before it, after the claim's transactional read.
        if not fired and ("update", path) in info.ops:
            fired.append(info.index)
            fake.external_write(path, {**_stored(fake, ticket["id"]),
                                       "status": "en_cours", "claim_id": rival,
                                       "claimed_at": at})

    remove = fake.add_commit_hook(racer)
    try:
        claim = ut.claim_ticket(ticket["id"], now=at)
    finally:
        remove()
    assert fired, "the race never happened"
    assert claim.state == ut.REFUSED and claim.reason == ut.REASON_BUSY
    assert _stored(fake, ticket["id"])["claim_id"] == rival


def test_a_claim_on_an_unreadable_store_raises_never_absent(monkeypatch):
    broken = mock.MagicMock()
    broken.collection.side_effect = RuntimeError("down")
    monkeypatch.setattr(ut, "db", broken)
    with pytest.raises(ut.TicketStoreUnavailable) as excinfo:
        ut.claim_ticket("3f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f", now=NOW)
    assert "rien n'a été versé" in str(excinfo.value)


def test_a_ticket_with_an_unknown_status_is_unreadable(fake):
    ticket = _open(fake)
    fake.external_write(f"{ut.COLLECTION}/{ticket['id']}",
                        {**_stored(fake, ticket["id"]), "status": "bizarre"})
    with pytest.raises(ut.TicketStoreUnavailable):
        ut.claim_ticket(ticket["id"], now=NOW)


def test_release_gives_the_ticket_back_to_its_holder_only(fake):
    ticket, claim = _claimed(fake)
    assert ut.release_ticket(ticket["id"], claim_id="autre") is False
    assert ut.release_ticket(ticket["id"], claim_id=claim.claim_id,
                             now=NOW + timedelta(minutes=2)) is True
    stored = _stored(fake, ticket["id"])
    assert stored["status"] == "en_attente"
    assert stored["claim_id"] == "" and stored["claimed_at"] is None
    # and it can be finalized later, within its window
    assert ut.claim_ticket(ticket["id"],
                           now=NOW + timedelta(minutes=10)).state == ut.CLAIMED


def test_a_failed_release_is_best_effort(monkeypatch, fake):
    ticket, claim = _claimed(fake)
    remove = fake.add_commit_hook(lambda info: (_ for _ in ()).throw(
        RuntimeError("down")))
    try:
        assert ut.release_ticket(ticket["id"], claim_id=claim.claim_id) is False
    finally:
        remove()
    assert _stored(fake, ticket["id"])["status"] == "en_cours"


def test_the_staged_digest_is_recorded_once_by_the_holder(fake):
    ticket, claim = _claimed(fake)
    digest = hashlib.sha256(b"x").hexdigest()
    assert ut.record_staged_digest(ticket["id"], claim_id="autre",
                                   sha256=digest) is False
    assert ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                   sha256=digest.upper()) is True
    assert _stored(fake, ticket["id"])["staged_sha256"] == digest
    fake.reset_logs()
    assert ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                   sha256=digest) is True
    assert _writes(fake) == []                      # same digest: no write
    other = hashlib.sha256(b"y").hexdigest()
    assert ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                   sha256=other) is False
    assert _stored(fake, ticket["id"])["staged_sha256"] == digest
    for bad in ("", "xyz", "g" * 64, None):
        assert ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                       sha256=bad) is False


def test_a_digest_that_cannot_be_recorded_raises(fake):
    ticket, claim = _claimed(fake)
    remove = fake.add_commit_hook(lambda info: (_ for _ in ()).throw(
        RuntimeError("down")))
    try:
        with pytest.raises(ut.TicketStoreUnavailable):
            ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                    sha256=hashlib.sha256(b"x").hexdigest())
    finally:
        remove()


def test_completion_settles_the_ticket_and_notes_its_commit(fake):
    ticket, claim = _claimed(fake)
    at = NOW + timedelta(minutes=4)
    result = {"document_id": ticket["reserved_document_id"]}
    with provenance.writing_via("mcp", tool="finalize_upload"):
        done, errors = ut.complete_ticket(ticket["id"], claim_id=claim.claim_id,
                                          result=result, now=at)
        commits = provenance.committed_writes()
    assert errors == []
    stored = _stored(fake, ticket["id"])
    assert stored["status"] == "versé" and stored["result"] == result
    assert stored["finalized_at"] == at
    assert stored["expire_at"] == at + ut.FINAL_RETENTION
    assert commits == ((ut.COLLECTION, ticket["id"]),)
    assert done == stored
    # replayed with the same result: the same answer, no write, no commit
    fake.reset_logs()
    with provenance.writing_via("mcp", tool="finalize_upload"):
        again, errors = ut.complete_ticket(ticket["id"], claim_id="n-importe",
                                           result=result, now=at)
        assert provenance.committed_writes() == ()
    assert errors == [] and again["status"] == "versé" and _writes(fake) == []
    # with ANOTHER result: refused
    _none, errors = ut.complete_ticket(
        ticket["id"], claim_id=claim.claim_id,
        result={"document_id": str(uuid.uuid4())}, now=at)
    assert errors and "autre résultat" in errors[0]


def test_completion_needs_the_claim(fake):
    ticket, claim = _claimed(fake)
    result = {"document_id": ticket["reserved_document_id"]}
    _t, errors = ut.complete_ticket(ticket["id"], claim_id="autre",
                                    result=result, now=NOW)
    assert errors == [ut.message_for(ut.REASON_BUSY)]
    opened = _open(fake)
    _t, errors = ut.complete_ticket(opened["id"], claim_id=claim.claim_id,
                                    result=result, now=NOW)
    assert errors == [ut.message_for(ut.REASON_CLOSED)]
    assert _stored(fake, opened["id"])["status"] == "en_attente"


@pytest.mark.parametrize("result", [
    {},
    {"document_id": "pas-un-uuid"},
    {"version": 2},
    {"template_id": TEMPLATE_ID, "version": True},
    {"template_id": TEMPLATE_ID, "version": 0},
    {"document_id": str(uuid.uuid4()), "upload_url": "https://x?upload_id=1"},
    None,
])
def test_a_malformed_result_is_refused_before_any_read(fake, result):
    ticket, claim = _claimed(fake)
    fake.reset_logs()
    _t, errors = ut.complete_ticket(ticket["id"], claim_id=claim.claim_id,
                                    result=result, now=NOW)
    assert errors == ["Résultat de versement invalide."]
    assert fake.reads == [] and _writes(fake) == []


def test_a_retried_completion_notes_no_commit_it_did_not_make(fake):
    """Revue de T5 — the transactional decorator RE-RUNS the body after an
    Aborted. A stale holder's completion reads « en_cours », a reclaimer
    completes the ticket (same reserved id, same result) before that
    commit lands, the transaction aborts, and the retry finds it ALREADY
    versé. The answer is the ticket, as before — but « changed » used to
    survive from the discarded attempt, so the call noted a commit it never
    made: a refusal after it would read « ENREGISTRÉE »."""
    ticket, first = _claimed(fake)
    path = f"{ut.COLLECTION}/{ticket['id']}"
    result = {"document_id": ticket["reserved_document_id"]}
    fired = []

    def reclaimer_completes(info):
        if not fired and ("update", path) in info.ops:
            fired.append(info.index)
            fake.external_write(path, {**_stored(fake, ticket["id"]),
                                       "status": "versé", "result": result,
                                       "claim_id": str(uuid.uuid4())})

    remove = fake.add_commit_hook(reclaimer_completes)
    try:
        with provenance.writing_via("mcp", tool="finalize_upload"):
            done, errors = ut.complete_ticket(
                ticket["id"], claim_id=first.claim_id, result=result,
                now=NOW + timedelta(minutes=3))
            commits = provenance.committed_writes()
    finally:
        remove()
    assert fired, "the race never happened"
    assert errors == [] and done["status"] == "versé"
    assert commits == ()


def test_a_retried_digest_record_reports_what_the_committed_attempt_did(fake):
    """Same trap in record_staged_digest: an attempt that saw ANOTHER digest
    set « ok = False », a concurrent write cleared it, and the retry recorded
    this digest — then answered False, telling its caller not to write a
    replacement whose digest IS recorded."""
    ticket, claim = _claimed(fake)
    path = f"{ut.COLLECTION}/{ticket['id']}"
    other = hashlib.sha256(b"autre").hexdigest()
    digest = hashlib.sha256(b"x").hexdigest()
    fake.external_write(path, {**_stored(fake, ticket["id"]),
                               "staged_sha256": other})
    fired = []

    def cleared(info):
        if not fired and info.ops == ():
            fired.append(info.index)
            fake.external_write(path, {**_stored(fake, ticket["id"]),
                                       "staged_sha256": ""})

    remove = fake.add_commit_hook(cleared)
    try:
        ok = ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                     sha256=digest)
    finally:
        remove()
    assert fired, "the race never happened"
    assert _stored(fake, ticket["id"])["staged_sha256"] == digest
    assert ok is True


def test_a_template_result_carries_its_version(fake):
    ticket = _open(fake, _gabarit_data("create"))
    claim = ut.claim_ticket(ticket["id"], now=NOW + timedelta(minutes=1))
    done, errors = ut.complete_ticket(
        ticket["id"], claim_id=claim.claim_id,
        result={"template_id": ticket["reserved_template_id"], "version": 1},
        now=NOW + timedelta(minutes=2))
    assert errors == [] and done["result"]["version"] == 1


def test_a_refusal_settles_the_ticket_for_its_holder_only(fake):
    ticket, claim = _claimed(fake)
    assert ut.refuse_ticket(ticket["id"], claim_id="autre",
                            reason="taille_differente") is False
    at = NOW + timedelta(minutes=5)
    assert ut.refuse_ticket(ticket["id"], claim_id=claim.claim_id,
                            reason="taille_differente", now=at) is True
    stored = _stored(fake, ticket["id"])
    assert stored["status"] == "refusé"
    assert stored["refusal_reason"] == "taille_differente"
    assert stored["expire_at"] == at + ut.FINAL_RETENTION
    with pytest.raises(ValueError):
        ut.refuse_ticket(ticket["id"], claim_id=claim.claim_id, reason="autre")


def test_a_closed_ticket_s_refusal_names_a_new_idempotency_key():
    """Revue de T5 — the write protocol tells a caller to REUSE its key on a
    retry, and a replayed opening with that key rehydrates the SAME closed
    ticket and refuses again for 24 h. « Ouvrez-en un nouveau » alone loops;
    every refusal that sends the caller to a new ticket says « nouvelle
    clé », and only the in-progress one — where waiting and retrying IS the
    answer — does not."""
    for reason in (ut.REASON_NOT_FOUND, ut.REASON_EXPIRED, ut.REASON_REFUSED,
                   ut.REASON_CLOSED):
        assert "NOUVELLE idempotency_key" in ut.message_for(reason), reason
    assert "idempotency_key" not in ut.message_for(ut.REASON_BUSY)


def test_get_open_ticket_is_a_read_that_names_why_not(fake):
    opened = _open(fake)
    assert ut.get_open_ticket(opened["id"], now=NOW) == (opened, "")
    # past its window: « expiré » — and the READ never writes the expiry
    fake.reset_logs()
    assert ut.get_open_ticket(opened["id"],
                              now=NOW + ut.OPEN_WINDOW)[1] == ut.REASON_EXPIRED
    assert _writes(fake) == [] and _stored(fake, opened["id"])["status"] == "en_attente"
    # claimed, then filed: « fermé »
    claim = ut.claim_ticket(opened["id"], now=NOW)
    assert ut.get_open_ticket(opened["id"], now=NOW)[1] == ut.REASON_CLOSED
    ut.complete_ticket(opened["id"], claim_id=claim.claim_id,
                       result={"document_id": opened["reserved_document_id"]})
    assert ut.get_open_ticket(opened["id"], now=NOW)[1] == ut.REASON_CLOSED
    # refused, or expired by a claim: said as such
    refused, refused_claim = _claimed(fake)
    ut.refuse_ticket(refused["id"], claim_id=refused_claim.claim_id,
                     reason="contenu_refuse")
    assert ut.get_open_ticket(refused["id"], now=NOW)[1] == ut.REASON_REFUSED
    expired = _open(fake)
    ut.claim_ticket(expired["id"], now=NOW + ut.OPEN_WINDOW)
    assert ut.get_open_ticket(expired["id"], now=NOW)[1] == ut.REASON_EXPIRED
    assert ut.get_open_ticket(str(uuid.uuid4()), now=NOW) == (None, ut.REASON_NOT_FOUND)


def test_reads_fail_closed(monkeypatch):
    broken = mock.MagicMock()
    broken.collection.return_value.document.return_value.get.side_effect = (
        RuntimeError("down"))
    monkeypatch.setattr(ut, "db", broken)
    with pytest.raises(ut.TicketStoreUnavailable):
        ut.get_ticket(str(uuid.uuid4()))
    with pytest.raises(ut.TicketStoreUnavailable):
        ut.get_open_ticket(str(uuid.uuid4()))
    # a MagicMock snapshot (truthy exists, MagicMock to_dict) is unreadable
    snap = mock.MagicMock()
    broken.collection.return_value.document.return_value.get.side_effect = None
    broken.collection.return_value.document.return_value.get.return_value = snap
    with pytest.raises(ut.TicketStoreUnavailable):
        ut.get_ticket(str(uuid.uuid4()))


# ══════════════════════════════════════════════════════════════════════
# 3-4. TTL, points de validation, aucune URL
# ══════════════════════════════════════════════════════════════════════


def test_the_ttl_override_is_declared_and_no_composite_index_exists():
    root = ATHENA.parent
    spec = json.loads((root / "firestore.indexes.json").read_text(encoding="utf-8"))
    overrides = [o for o in spec.get("fieldOverrides", [])
                 if o.get("collectionGroup") == ut.COLLECTION]
    assert overrides == [{"collectionGroup": ut.COLLECTION,
                          "fieldPath": "expire_at", "ttl": True, "indexes": []}]
    assert not [i for i in spec["indexes"]
                if i.get("collectionGroup") == ut.COLLECTION]


def _module_functions() -> dict:
    tree = ast.parse(pathlib.Path(ut.__file__).read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _calls_note_commit(fn) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "note_commit" for n in ast.walk(fn))


def test_only_the_tool_s_own_writes_note_a_commit():
    """Creation and completion are the tool's writes. The claim, its
    release, the staged digest and a refusal are bookkeeping that a
    finalization REFUSES after — noted, the refusal would come back as
    « ENREGISTRÉE — NE PAS RÉESSAYER »."""
    functions = _module_functions()
    for name in ("create_ticket", "complete_ticket"):
        assert _calls_note_commit(functions[name]), name
    for name in ("claim_ticket", "release_ticket", "record_staged_digest",
                 "refuse_ticket", "_holder_update"):
        assert not _calls_note_commit(functions[name]), name


def test_a_refused_finalization_path_records_no_commit(fake):
    ticket = _open(fake)
    with provenance.writing_via("mcp", tool="finalize_upload"):
        claim = ut.claim_ticket(ticket["id"], now=NOW + timedelta(minutes=1))
        ut.record_staged_digest(ticket["id"], claim_id=claim.claim_id,
                                sha256=hashlib.sha256(PDF).hexdigest())
        ut.release_ticket(ticket["id"], claim_id=claim.claim_id)
        claim = ut.claim_ticket(ticket["id"], now=NOW + timedelta(minutes=2))
        ut.refuse_ticket(ticket["id"], claim_id=claim.claim_id,
                         reason="empreinte_differente")
        expired = _open(fake)
        ut.claim_ticket(expired["id"], now=NOW + ut.OPEN_WINDOW)
        assert provenance.committed_writes() == (
            (ut.COLLECTION, expired["id"]),)   # only the second CREATE


def test_no_ticket_write_ever_carries_a_capability(fake):
    """DERIVED over every stored ticket after a full lifecycle of both
    purposes: no URL, no signed-URL or session marker, anywhere."""
    doc_ticket, claim = _claimed(fake)
    ut.complete_ticket(doc_ticket["id"], claim_id=claim.claim_id,
                       result={"document_id": doc_ticket["reserved_document_id"]})
    tpl = _open(fake, _gabarit_data("replace"))
    tpl_claim = ut.claim_ticket(tpl["id"], now=NOW)
    ut.record_staged_digest(tpl["id"], claim_id=tpl_claim.claim_id,
                            sha256=hashlib.sha256(b"z").hexdigest())
    ut.refuse_ticket(tpl["id"], claim_id=tpl_claim.claim_id,
                     reason="identifiants_residuels")
    stored = fake.peek_collection(ut.COLLECTION)
    assert len(stored) == 2
    for ticket in stored.values():
        assert not capability_in(ticket), ticket
        assert "upload_url" not in json.dumps(ticket, default=str)


# ══════════════════════════════════════════════════════════════════════
# 5. Le finaliseur WEB ne peut pas verser un objet de ticket
# ══════════════════════════════════════════════════════════════════════


def _web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = REAL_UID
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return client


def test_the_web_finalizer_refuses_a_ticket_staging_object(fake, monkeypatch):
    """``staging/{uid}/mcp/{ticket}/upload{ext}`` is under the SAME uid the
    web route accepts — its shape check (``staging/{uid}/{uuid4}/{name}``)
    is what keeps a browser from filing a connector ticket's object."""
    ticket = _open(fake)
    monkeypatch.setattr(rd.storage, "bucket",
                        lambda: pytest.fail("the web route touched GCS"))
    resp = _web().post("/documents/api/finaliser", json={
        "objet": ticket["staging_object"], "name": "x.pdf",
        "dossier_id": DOSSIER_ID,
    })
    assert resp.status_code == 400
    assert resp.get_json()["erreur"] == "Requête invalide."


# ══════════════════════════════════════════════════════════════════════
# 6. Avec le protocole d'écriture : l'URL n'est jamais stockée, un rejeu
#    rouvre une session pour le MÊME ticket encore ouvert — ou refuse.
# ══════════════════════════════════════════════════════════════════════


def test_a_replayed_begin_reopens_the_same_open_ticket_or_refuses(monkeypatch):
    """The shape begin_upload will take (lot 2, MCP release), on the real
    model, the real write protocol, the shared fake Firestore and the fake
    GCS: the stored result carries the ticket id and never the URL; a replay
    re-opens a session for the SAME still-open ticket; once the ticket is
    claimed, a replay refuses « fermé »."""
    from mcp import write_support as ws
    from mcp.tools import ToolArgumentError
    from tests._fake_gcs import FakeBucket

    fake = install(monkeypatch, ut, ws)
    monkeypatch.setattr(ws, "_PERSISTENCE_HOOKS", dict(ws._PERSISTENCE_HOOKS))
    bucket = FakeBucket()
    clock = {"now": NOW}

    def session_for(ticket: dict) -> str:
        blob = bucket.blob(ticket["staging_object"])
        return blob.create_resumable_upload_session(
            content_type="application/pdf", size=ticket["declared_size"],
            origin="https://claude.ai", if_generation_match=0)

    def persist(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k != "upload_url"}

    def rehydrate(stored: dict) -> dict:
        ticket, reason = ut.get_open_ticket(stored["ticket_id"],
                                            now=clock["now"])
        if reason:
            raise ToolArgumentError(ut.message_for(reason))
        return {**stored, "upload_url": session_for(ticket)}

    ws.register_persistence_hooks("create_note", persist=persist,
                                  rehydrate=rehydrate)

    def execute() -> dict:
        ticket = _open(fake)
        return {"ticket_id": ticket["id"], "upload_url": session_for(ticket),
                "entity": {"id": ticket["id"], "dossier_id": DOSSIER_ID}}

    args = {"idempotency_key": "ticket-cle-0001", "filename": "a.pdf"}
    first = ws.run_write("create_note", args, execute)
    assert "upload_id=" in first["upload_url"]
    entries = fake.peek_collection(ws.COLLECTION)
    assert len(entries) == 1
    (entry,) = entries.values()
    assert entry["result"]["ticket_id"] == first["ticket_id"]
    assert not capability_in(entry)
    assert not any(capability_in(t)
                   for t in fake.peek_collection(ut.COLLECTION).values())

    clock["now"] = NOW + timedelta(minutes=20)
    replay = ws.run_write("create_note", args,
                          lambda: pytest.fail("a replay must not execute"))
    assert replay["idempotent_replay"] is True
    assert replay["ticket_id"] == first["ticket_id"]
    assert replay["upload_url"] != first["upload_url"]     # a FRESH session
    assert bucket.sessions[replay["upload_url"]]["name"] == (
        _stored(fake, first["ticket_id"])["staging_object"])
    assert len(fake.peek_collection(ut.COLLECTION)) == 1   # the SAME ticket

    ut.claim_ticket(first["ticket_id"], now=clock["now"])
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", args, lambda: pytest.fail("no"))
    assert "fermé" in str(excinfo.value)

    clock["now"] = NOW + ut.OPEN_WINDOW + timedelta(minutes=1)
    other = _open(fake)
    with pytest.raises(ToolArgumentError):
        rehydrate({"ticket_id": other["id"]})               # past its hour
