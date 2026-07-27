"""Invitations du portail — modèle (base nommée) + émission (spec L1 §5-6).

CI-only style (imports models under a mocked Firestore client, the
test_hearing_vocab pattern). Pins: expiry/active logic, the transactional
guards (accusé test-and-set, submission append-if-absent), the §1.3
self-invitation ban, claim MERGING, and the manual-link fallback when Graph
is unconfigured.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.portail_invitation as pi
    import services.portail_emission as emission

from utils.graph import GraphNotConfigured  # noqa: E402


# ── Fake named-DB plumbing (transactions included) ───────────────────────


class _FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeRef:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def get(self, transaction=None):
        return _FakeSnap(self._store.get(self._key))

    def set(self, doc):
        self._store[self._key] = dict(doc)

    def update(self, fields):
        self._store[self._key].update(fields)


class _FakeTxn:
    def update(self, ref, fields):
        ref.update(fields)


class _FakeCol:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _FakeRef(self._store, key)


class _FakeDb:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _FakeCol(self._store)

    def transaction(self):
        return _FakeTxn()


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    fake = _FakeDb(data)
    monkeypatch.setattr(pi, "_pdb", lambda: fake)
    # The real @firestore.transactional needs a real Transaction; identity
    # is enough here — atomicity is Firestore's job, the LOGIC is ours.
    monkeypatch.setattr(pi.firestore, "transactional", lambda f: f)
    return data


def _inv(**over) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": "inv1", "type": "documents", "email": "client@exemple.com",
        "statut": "envoyée", "display_label": "Dossier 2026-001",
        "created_at": now, "updated_at": now,
        "expires_at": now + timedelta(days=30),
        "resend_count": 0, "soumissions": [], "accuses": {},
    }
    base.update(over)
    return base


# ── Expiry / active logic ────────────────────────────────────────────────


def test_est_expiree_and_active():
    inv = _inv()
    assert pi.est_expiree(inv) is False
    assert pi.est_active(inv) is True
    past = _inv(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert pi.est_expiree(past) is True
    assert pi.est_active(past) is False
    for statut in ("soumise", "traitée", "refusée", "révoquée"):
        assert pi.est_active(_inv(statut=statut)) is False


def test_creer_invitation_validation(store):
    _, errors = pi.creer_invitation("intruder", "client@exemple.com",
                                    display_label="X")
    assert errors
    _, errors = pi.creer_invitation("documents", "pas-un-courriel",
                                    display_label="X")
    assert errors
    _, errors = pi.creer_invitation("documents", "client@exemple.com",
                                    display_label="")
    assert errors


def test_creer_invitation_defaults(store):
    inv, errors = pi.creer_invitation(
        "documents", "  Client@Exemple.com ",
        client_name="Jean Tremblay", display_label="Dossier 2026-001"
    )
    assert errors == []
    assert inv["email"] == "client@exemple.com"
    assert inv["statut"] == "envoyée"
    assert inv["soumissions"] == [] and inv["accuses"] == {}
    assert inv["prefill"] is None
    assert inv["client_name"] == "Jean Tremblay"
    assert store[inv["id"]]["display_label"] == "Dossier 2026-001"
    # 14-day documents expiry (user decision 2026-07-27)
    delta = inv["expires_at"] - inv["created_at"]
    assert delta.days == 14


def test_creer_invitation_client_name_defaults_empty(store):
    inv, errors = pi.creer_invitation(
        "documents", "client@exemple.com", display_label="Dossier X"
    )
    assert errors == []
    assert inv["client_name"] == ""


# ── Transactional guards ─────────────────────────────────────────────────


def test_ajouter_soumission_appends_once(store):
    store["inv1"] = _inv()
    assert pi.ajouter_soumission("inv1", "b1", 3, 999) is True
    assert pi.ajouter_soumission("inv1", "b1", 3, 999) is True  # replay: no dup
    doc = store["inv1"]
    assert len(doc["soumissions"]) == 1
    assert doc["soumissions"][0]["batch"] == "b1"
    assert doc["statut"] == "soumise"


def test_poser_accuse_test_and_set(store):
    store["inv1"] = _inv()
    assert pi.poser_accuse("inv1", "b1") is True    # won — send the accusé
    assert pi.poser_accuse("inv1", "b1") is False   # replay — NO second email
    assert pi.poser_accuse("inv1", "b2") is True    # other batch independent
    assert store["inv1"]["accuses"] == {"b1": True, "b2": True}


def test_poser_accuse_missing_invitation_fails_closed(store):
    assert pi.poser_accuse("absent", "b1") is False


def test_marquer_ouverte_cas_never_regresses(store):
    store["inv1"] = _inv(statut="envoyée")
    assert pi.marquer_ouverte("inv1") is True
    assert store["inv1"]["statut"] == "ouverte"
    # A racing « soumise » already advanced the statut: the CAS must be a
    # no-op success, never a regression that hides a processed submission.
    store["inv1"]["statut"] = "soumise"
    assert pi.marquer_ouverte("inv1") is True
    assert store["inv1"]["statut"] == "soumise"
    assert pi.marquer_ouverte("absent") is True  # nothing to open


# ── Émission (§1.3, claim merge, manual-link fallback) ───────────────────


def test_emission_refuses_juriste_email(store):
    inv, errors, lien = emission.emettre_invitation(
        "documents", "Test@Example.com", display_label="Dossier X"
    )
    assert inv is None and lien == ""
    assert any("juriste" in e for e in errors)


def test_emission_merges_claims_never_replaces(store, monkeypatch):
    user = mock.Mock(uid="u9", custom_claims={"existing": 1})
    monkeypatch.setattr(emission.fb_auth, "get_user_by_email",
                        mock.Mock(return_value=user))
    set_claims = mock.Mock()
    monkeypatch.setattr(emission.fb_auth, "set_custom_user_claims", set_claims)
    monkeypatch.setattr(emission, "_generer_lien",
                        lambda email, inv_id: "https://lien.example/x")
    monkeypatch.setattr(emission.courriel, "envoyer",
                        mock.Mock(side_effect=GraphNotConfigured("off")))

    inv, errors, lien_manuel = emission.emettre_invitation(
        "documents", "client@exemple.com",
        client_name="Jean Tremblay", display_label="Dossier 2026-001"
    )
    assert errors == []
    set_claims.assert_called_once_with("u9", {"existing": 1, "portail": True})
    # Graph unconfigured → invitation still created, link handed back.
    assert inv is not None and inv["id"] in store
    assert store[inv["id"]]["client_name"] == "Jean Tremblay"
    assert lien_manuel == "https://lien.example/x"


def test_emission_link_failure_revokes_invitation(store, monkeypatch):
    user = mock.Mock(uid="u9", custom_claims=None)
    monkeypatch.setattr(emission.fb_auth, "get_user_by_email",
                        mock.Mock(return_value=user))
    monkeypatch.setattr(emission.fb_auth, "set_custom_user_claims", mock.Mock())
    monkeypatch.setattr(
        emission, "_generer_lien",
        mock.Mock(side_effect=RuntimeError("provider disabled")),
    )
    inv, errors, _ = emission.emettre_invitation(
        "documents", "client@exemple.com", display_label="Dossier 2026-001"
    )
    assert inv is None and errors
    # The dead invitation must not linger as « envoyée ».
    assert list(store.values())[0]["statut"] == "révoquée"


def test_renvoi_refused_when_inactive(store):
    store["inv1"] = _inv(statut="révoquée")
    ok, message, lien = emission.renvoyer_invitation("inv1")
    assert ok is False and lien == ""
    assert message == "Invitation invalide ou expirée."


def test_renvoi_active_increments_and_falls_back_to_manual_link(store, monkeypatch):
    store["inv1"] = _inv()
    monkeypatch.setattr(emission, "_generer_lien",
                        lambda email, inv_id: "https://lien.example/r")
    monkeypatch.setattr(emission.courriel, "envoyer",
                        mock.Mock(side_effect=GraphNotConfigured("off")))
    # firestore.Increment is unavailable on the fake — emulate.
    monkeypatch.setattr(pi, "incrementer_resend",
                        lambda inv_id: store[inv_id].__setitem__(
                            "resend_count", store[inv_id]["resend_count"] + 1) or True)
    ok, message, lien = emission.renvoyer_invitation("inv1")
    assert ok is True and message == ""
    assert lien == "https://lien.example/r"
    assert store["inv1"]["resend_count"] == 1
