"""Date de naissance (L3 C1) + non-effacement sur mise à jour partielle.

Deux sujets liés par le même mécanisme : ``update_partie`` fusionne
``{**existing, **data}`` puis écrit le document ENTIER. Une clé présente
écrase, une clé absente survit — d'où deux invariants à épingler :

1. ``birth_date`` est une DATE SEULE à minuit UTC, sérialisée en BDAY et
   RELUE (sans la relecture, le premier PUT DavX5 l'effacerait).
2. ``_normalize`` ne doit pas injecter de clé que l'appelant n'a pas fournie —
   il injectait ``mandataires: []`` sans condition, donc toute mise à jour
   partielle détruisait la liste des mandataires. C'est le mécanisme même de
   la fusion champ par champ du portail (L3 §5.3).

Tests purs : le modèle est importé, mais Firestore est bouchonné.
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

from models import partie as pm  # noqa: E402


def _partie(**over) -> dict:
    base = pm._default_doc()
    base.update({
        "id": "p1", "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay",
    })
    base.update(over)
    return base


# ── Normalisation de la date ─────────────────────────────────────────────


def test_coerce_accepte_les_trois_formes():
    attendu = datetime(1985, 3, 17, tzinfo=timezone.utc)
    assert pm._coerce_birth_date("1985-03-17") == attendu
    assert pm._coerce_birth_date(datetime(1985, 3, 17, 14, 30)) == attendu
    assert pm._coerce_birth_date(attendu.date()) == attendu


def test_coerce_refuse_le_charabia():
    for brut in ("", "  ", "hier", "1985-13-45", None, 42, []):
        assert pm._coerce_birth_date(brut) is None


def test_validate_refuse_une_date_future():
    demain = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    erreurs = pm._validate(_partie(birth_date=demain))
    assert any("futur" in e for e in erreurs)


def test_validate_normalise_a_minuit_utc():
    data = _partie(birth_date="1985-03-17")
    assert pm._validate(data) == []
    assert data["birth_date"] == datetime(1985, 3, 17, tzinfo=timezone.utc)


def test_validate_chaine_vide_efface_la_date():
    """Vider le champ du formulaire doit effacer la date, pas la refuser."""
    data = _partie(birth_date="")
    assert pm._validate(data) == []
    assert data["birth_date"] is None


# ── vCard : aller-retour ─────────────────────────────────────────────────


def test_vcard_emet_bday_compact():
    carte = pm.partie_to_vcard(
        _partie(birth_date=datetime(1985, 3, 17, tzinfo=timezone.utc))
    )
    assert "BDAY:19850317" in carte


def test_vcard_omet_bday_pour_une_personne_morale():
    carte = pm.partie_to_vcard(_partie(
        type="organization", organization_name="9123-4567 Québec inc.",
        last_name="", birth_date=datetime(1985, 3, 17, tzinfo=timezone.utc),
    ))
    assert "BDAY" not in carte


def test_vcard_aller_retour():
    carte = pm.partie_to_vcard(
        _partie(birth_date=datetime(1985, 3, 17, tzinfo=timezone.utc))
    )
    assert pm.vcard_to_partie(carte)["birth_date"] == datetime(
        1985, 3, 17, tzinfo=timezone.utc
    )


def test_vcard_accepte_la_forme_tiretee_de_la_version_3():
    carte = pm.partie_to_vcard(_partie()).replace(
        "END:VCARD", "BDAY:1985-03-17\r\nEND:VCARD"
    )
    assert pm.vcard_to_partie(carte)["birth_date"] == datetime(
        1985, 3, 17, tzinfo=timezone.utc
    )


def test_vcard_sans_bday_omet_la_cle_plutot_que_de_l_effacer():
    """NON-EFFACEMENT (même règle que CONFERENCE côté hearings).

    update_partie fusionne {**existing, **data} : une clé présente-mais-None
    EFFACERAIT la date stockée. Un client CardDAV qui ignore BDAY ne doit pas
    pouvoir la supprimer au premier PUT.
    """
    carte = pm.partie_to_vcard(_partie())
    assert "birth_date" not in pm.vcard_to_partie(carte)


def test_vcard_ignore_une_date_partielle():
    """« --0317 » (anniversaire sans année) n'est pas une date de naissance."""
    carte = pm.partie_to_vcard(_partie()).replace(
        "END:VCARD", "BDAY:--0317\r\nEND:VCARD"
    )
    assert "birth_date" not in pm.vcard_to_partie(carte)


# ── Non-effacement des mandataires sur mise à jour partielle ─────────────


def test_normalize_ne_fabrique_pas_de_mandataires():
    """Le défaut : ``data["mandataires"] = cleaned`` s'exécutait sans
    condition, donc la fusion écrasait la liste stockée par [] à CHAQUE mise
    à jour partielle."""
    data = pm._normalize({"email": "a@b.ca"})
    assert "mandataires" not in data


def test_normalize_nettoie_encore_quand_la_cle_est_fournie():
    data = pm._normalize({"mandataires": [
        {"id": "m1", "kind": "tuteur"},
        {"id": "m1", "kind": "tuteur"},      # doublon
        {"id": "", "kind": "autre"},         # vide
        "pas un dict",
    ]})
    assert data["mandataires"] == [{"id": "m1", "kind": "tuteur", "notes": ""}]


def test_mise_a_jour_partielle_preserve_les_mandataires(monkeypatch):
    """Le scénario complet, jusqu'au document écrit.

    C'est exactement ce que fait la fusion champ par champ de L3 — et ce que
    faisaient déjà update_kyc_status et link_kyc_document.
    """
    stocke = _partie(
        mandataires=[{"id": "m1", "kind": "tuteur", "notes": "curatelle"}],
        email="ancien@exemple.com",
    )
    monkeypatch.setattr(pm, "get_partie", lambda pid: dict(stocke))
    ecrit = {}

    class _Doc:
        def set(self, payload):
            ecrit.update(payload)

    class _Col:
        def document(self, _pid):
            return _Doc()

    monkeypatch.setattr(pm, "db", mock.Mock(collection=lambda _n: _Col()))

    maj, erreurs = pm.update_partie("p1", {"email": "nouveau@exemple.com"})
    assert erreurs == []
    assert maj["email"] == "nouveau@exemple.com"
    assert ecrit["mandataires"] == [
        {"id": "m1", "kind": "tuteur", "notes": "curatelle"}
    ]


# ── prefill : liste blanche ──────────────────────────────────────────────


def test_prefill_est_une_liste_blanche():
    """Le document d'invitation est lu par le service PUBLIC (§5) : rien de
    sensible ne doit y entrer, pas même la date de naissance."""
    from models import portail_invitation as pi

    source = _partie(
        birth_date=datetime(1985, 3, 17, tzinfo=timezone.utc),
        email="jean@exemple.com", phone_cell="+15145551234",
        address_street="10 rue Principale",
        notes="Mémo interne — ne jamais exposer",
        identity_verified="vérifié",
        conflict_check_notes="Conflit possible avec X",
        kyc_document_ids=["d1"],
    )
    prefill = pi.prefill_depuis_partie(source)

    assert prefill["email"] == "jean@exemple.com"
    assert prefill["first_name"] == "Jean"
    assert prefill["address_street"] == "10 rue Principale"
    for interdit in ("birth_date", "notes", "identity_verified",
                     "conflict_check_notes", "kyc_document_ids",
                     "mandataires", "id"):
        assert interdit not in prefill


def test_prefill_omet_les_valeurs_vides_et_tolere_none():
    from models import portail_invitation as pi

    assert pi.prefill_depuis_partie(None) is None
    prefill = pi.prefill_depuis_partie(_partie(email="", phone_cell=""))
    assert "email" not in prefill and "phone_cell" not in prefill
    assert prefill["last_name"] == "Tremblay"
