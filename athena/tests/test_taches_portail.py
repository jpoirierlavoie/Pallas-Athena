"""Gestionnaire de tâches + réconciliation du portail (spec L1 §13.d/l/m/n).

Pins: the SHA-512 vector, the queue/cron header guards (403 without them),
idempotence of a replayed « soumise » (no re-hash, NO second accusé), and
the reconciliation verdicts (orphan → re-enqueued, complete → intact).
"""

import hashlib
import io
import json
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
    import routes.taches_portail as tp

from flask import Flask  # noqa: E402


_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


@pytest.fixture()
def web():
    # template_folder set so _corps_accuse's render_template finds the
    # bordereau email template.
    app = Flask(__name__, template_folder=_TEMPLATES)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(tp.taches_portail_bp)
    return app.test_client()


def _inv(**over) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": "inv1", "type": "documents", "email": "client@exemple.com",
        "statut": "ouverte", "display_label": "Dossier 2026-001",
        "expires_at": now + timedelta(days=10),
        "soumissions": [], "accuses": {},
    }
    base.update(over)
    return base


# ── Bucket factice ───────────────────────────────────────────────────────


class _FakeBlob:
    def __init__(self, name, data=b"", content_type=""):
        self.name = name
        self._data = data  # None → the object does not exist
        self.size = len(data) if data is not None else 0
        self.content_type = content_type

    def exists(self):
        return self._data is not None

    def download_as_bytes(self):
        return self._data

    def upload_from_string(self, data, content_type=""):
        self._data = data.encode() if isinstance(data, str) else data
        self.size = len(self._data)

    def open(self, mode="rb"):
        return io.BytesIO(self._data)


class _FakeBucket:
    """Objects: {name: bytes}. blob() of an absent name → exists() False."""

    def __init__(self, objets: dict):
        self._objets = {k: _FakeBlob(k, v) for k, v in objets.items()}

    def blob(self, name):
        if name not in self._objets:
            vide = _FakeBlob(name, None)

            def _upload(data, content_type=""):
                vide._data = data.encode() if isinstance(data, str) else data
                vide.size = len(vide._data)
                self._objets[name] = vide

            vide.upload_from_string = _upload
            return vide
        return self._objets[name]

    def list_blobs(self, prefix="", delimiter=None):
        if delimiter is None:
            return [b for n, b in sorted(self._objets.items())
                    if n.startswith(prefix)]

        class _Iter(list):
            prefixes: set = set()

        res = _Iter()
        prefixes = set()
        for n in sorted(self._objets):
            if not n.startswith(prefix):
                continue
            reste = n[len(prefix):]
            if delimiter in reste:
                prefixes.add(prefix + reste.split(delimiter)[0] + delimiter)
            else:
                res.append(self._objets[n])
        res.prefixes = prefixes
        return res


ENVELOPE = {
    "type": "documents", "invitation_id": "inv1", "batch": "b1",
    "submitted_at": "2026-07-20T12:00:00+00:00",
    "http": {"ip": "203.0.113.9", "user_agent": "Mozilla/5.0 test"},
    "files": [{"objet": "submissions/inv1/b1/files/001_a.pdf",
               "name": "a.pdf", "size": 4, "content_type": "application/pdf"}],
}


def _bucket_soumis() -> _FakeBucket:
    return _FakeBucket({
        "submissions/inv1/b1/envelope.json": json.dumps(ENVELOPE).encode(),
        "submissions/inv1/b1/files/001_a.pdf": b"data",
    })


# ── §13.d — vecteur SHA-512 connu ────────────────────────────────────────


def test_sha512_flux_vecteur_connu():
    assert tp.sha512_flux(io.BytesIO(b"abc")) == hashlib.sha512(b"abc").hexdigest()
    # Streaming across slices must equal the one-shot digest.
    gros = b"x" * (tp._HASH_CHUNK + 17)
    assert tp.sha512_flux(io.BytesIO(gros)) == hashlib.sha512(gros).hexdigest()


# ── §13.l — garde d'en-tête ──────────────────────────────────────────────


def test_evenement_sans_en_tete_queue_403(web):
    assert web.post("/taches/portail/evenement", json={}).status_code == 403
    assert web.post(
        "/taches/portail/evenement", json={},
        headers={"X-AppEngine-QueueName": "autre-file"},
    ).status_code == 403


def test_reconciliation_sans_en_tete_cron_403(web):
    assert web.get("/taches/portail/reconciliation").status_code == 403


def _post_evenement(web, payload):
    return web.post(
        "/taches/portail/evenement", json=payload,
        headers={"X-AppEngine-QueueName": "portail"},
    )


def test_charge_invalide_200_sans_reprise(web):
    # A malformed payload must not enter a retry storm.
    assert _post_evenement(web, {"event": "purge"}).status_code == 200
    assert _post_evenement(web, {}).status_code == 200


# ── « ouverte » ──────────────────────────────────────────────────────────


def test_ouverte_passe_par_le_cas_transactionnel(web, monkeypatch):
    # The handler delegates to the transactional CAS (never a plain
    # read-check-write — a « soumise » task could race and be regressed).
    cas = mock.Mock(return_value=True)
    monkeypatch.setattr(tp.pi, "marquer_ouverte", cas)
    assert _post_evenement(
        web, {"event": "ouverte", "invitation_id": "inv1"}
    ).status_code == 200
    cas.assert_called_once_with("inv1")

    # CAS failure → exception → 5xx so the queue retries (TESTING mode
    # re-raises instead of rendering the 500).
    monkeypatch.setattr(tp.pi, "marquer_ouverte",
                        mock.Mock(return_value=False))
    with pytest.raises(RuntimeError):
        _post_evenement(web, {"event": "ouverte", "invitation_id": "inv1"})


# ── « soumise » : manifeste + idempotence (§13.m) ────────────────────────


def _traiter(web, monkeypatch, bucket, inv, envoyer=None,
             partie=None, dossier=None):
    monkeypatch.setattr(tp, "_bucket", lambda: bucket)
    monkeypatch.setattr(tp.pi, "lire_invitation", lambda i: dict(inv))
    monkeypatch.setattr(tp.pi, "ajouter_soumission",
                        lambda *a, **k: True)
    monkeypatch.setattr(tp, "get_partie", lambda p: partie)
    monkeypatch.setattr(tp, "get_dossier", lambda d: dossier)
    accuses = inv.setdefault("accuses", {})

    def _poser(inv_id, batch):
        if accuses.get(batch):
            return False
        accuses[batch] = True
        return True

    monkeypatch.setattr(tp.pi, "poser_accuse", _poser)
    envoyer = envoyer or mock.Mock()
    monkeypatch.setattr(tp.courriel, "envoyer", envoyer)
    reponse = _post_evenement(
        web, {"event": "soumise", "invitation_id": "inv1", "batch": "b1"}
    )
    return reponse, envoyer


def test_soumise_ecrit_manifeste_et_envoie_accuse(web, monkeypatch):
    bucket = _bucket_soumis()
    reponse, envoyer = _traiter(web, monkeypatch, bucket, _inv())
    assert reponse.status_code == 200

    manifeste = json.loads(
        bucket.blob("submissions/inv1/b1/manifeste.json").download_as_bytes()
    )
    assert manifeste["etat_lot"] == "soumis"
    fichier = manifeste["files"][0]
    assert fichier["sha512"] == hashlib.sha512(b"data").hexdigest()
    assert fichier["size_gcs"] == 4 and fichier["divergence"] is None

    # §9.2 : horodatage + IP/UA recopiés de l'enveloppe dans le manifeste.
    assert manifeste["submitted_at"] == "2026-07-20T12:00:00+00:00"
    assert manifeste["http"]["ip"] == "203.0.113.9"

    envoyer.assert_called_once()
    destinataire, objet, corps = envoyer.call_args.args
    assert destinataire == "client@exemple.com"
    assert objet.startswith("Accusé de réception")
    assert fichier["sha512"].upper() in corps  # empreinte en MAJUSCULES
    assert "réception technique" in corps
    # L'accusé atteste la date de RÉCEPTION (enveloppe), pas de traitement.
    assert "20 juillet 2026" in corps
    # Bordereau : en-tête accusé (pas « bordereau d'envoi »), colonnes,
    # destinataire = cabinet, fichier listé.
    assert "ACCUSÉ DE RÉCEPTION" in corps
    assert "EXPÉDITEUR" in corps and "DESTINATAIRE" in corps
    assert "Me Jason Poirier Lavoie" in corps
    assert "a.pdf" in corps
    assert "110, 133" not in corps  # ligne C.p.c. retirée


def test_accuse_sans_dossier_ni_partie_expediteur_minimal(web, monkeypatch):
    bucket = _bucket_soumis()
    inv = _inv(client_name="Jean Tremblay")
    reponse, envoyer = _traiter(web, monkeypatch, bucket, inv)  # partie/dossier None
    assert reponse.status_code == 200
    corps = envoyer.call_args.args[2]
    # Expéditeur = nom + courriel seuls ; aucun bloc dossier ni référence.
    assert "Jean Tremblay" in corps
    assert "DOSSIER JUDICIAIRE" not in corps
    assert "Notre référence" not in corps


def test_accuse_avec_dossier_bloc_judiciaire_et_reference(web, monkeypatch):
    bucket = _bucket_soumis()
    inv = _inv(dossier_id="d1", client_name="Jean Tremblay")
    dossier = {"file_number": "2026-002",
               "court_file_number": "500-22-294848-265",
               "title": "DESJARDINS c. BELANGER"}
    reponse, envoyer = _traiter(web, monkeypatch, bucket, inv, dossier=dossier)
    assert reponse.status_code == 200
    corps = envoyer.call_args.args[2]
    assert "DOSSIER JUDICIAIRE" in corps
    assert "500-22-294848-265" in corps
    assert "DESJARDINS c. BELANGER" in corps
    assert "Notre référence : 2026-002" in corps


def test_accuse_avec_partie_expediteur_complet(web, monkeypatch):
    bucket = _bucket_soumis()
    inv = _inv(partie_id="p1")
    partie = {
        "type": "individual", "prefix": "M.", "first_name": "Jean",
        "last_name": "Tremblay", "email": "jean@exemple.com",
        "phone_cell": "+15145551234",
        "address_street": "10 rue Principale", "address_city": "Montréal",
        "address_province": "QC", "address_postal_code": "H1A 1A1",
        "address_country": "CA",
    }
    reponse, envoyer = _traiter(web, monkeypatch, bucket, inv, partie=partie)
    assert reponse.status_code == 200
    corps = envoyer.call_args.args[2]
    assert "M. Jean Tremblay" in corps
    assert "10 rue Principale" in corps
    assert "Montréal (Québec) H1A 1A1" in corps
    assert "(514) 555-1234" in corps  # téléphone formaté


def test_soumise_rejouee_aucun_second_accuse_ni_rehash(web, monkeypatch):
    bucket = _bucket_soumis()
    inv = _inv()
    r1, envoyer1 = _traiter(web, monkeypatch, bucket, inv)
    assert r1.status_code == 200 and envoyer1.call_count == 1

    # Replay: the manifest already exists → hashes must NOT be recomputed,
    # and poser_accuse loses → NO second email (§13.m).
    with mock.patch.object(tp, "_sha512_blob",
                           side_effect=AssertionError("re-hash!")) as rehash:
        r2, envoyer2 = _traiter(web, monkeypatch, bucket, inv)
    assert r2.status_code == 200
    assert envoyer2.call_count == 0
    assert not rehash.called


def test_soumise_aucun_fichier_recu_aucun_accuse(web, monkeypatch):
    # Tous les fichiers déclarés sont absents de GCS → recus vide. On ne doit
    # PAS expédier un accusé attestant zéro fichier ; le marqueur poser_accuse
    # est tout de même posé (converge la réconciliation).
    bucket = _FakeBucket({
        "submissions/inv1/b1/envelope.json": json.dumps(ENVELOPE).encode(),
        # Aucun objet sous files/ → le fichier déclaré est « manquant ».
    })
    inv = _inv()
    reponse, envoyer = _traiter(web, monkeypatch, bucket, inv)
    assert reponse.status_code == 200
    envoyer.assert_not_called()          # aucun accusé pour zéro fichier
    assert inv["accuses"].get("b1") is True  # marqueur posé → pas de boucle


def test_soumise_divergence_taille_consignee_non_bloquante(web, monkeypatch):
    bucket = _FakeBucket({
        "submissions/inv1/b1/envelope.json": json.dumps(ENVELOPE).encode(),
        "submissions/inv1/b1/files/001_a.pdf": b"data-more-than-declared",
    })
    reponse, envoyer = _traiter(web, monkeypatch, bucket, _inv())
    assert reponse.status_code == 200  # divergence never blocks
    manifeste = json.loads(
        bucket.blob("submissions/inv1/b1/manifeste.json").download_as_bytes()
    )
    assert manifeste["files"][0]["divergence"] == "taille déclarée ≠ taille reçue"
    envoyer.assert_called_once()


def test_soumise_sans_enveloppe_200(web, monkeypatch):
    monkeypatch.setattr(tp, "_bucket", lambda: _FakeBucket({}))
    assert _post_evenement(
        web, {"event": "soumise", "invitation_id": "inv1", "batch": "b1"}
    ).status_code == 200


# ── §13.n — réconciliation ───────────────────────────────────────────────


def _reconcilier(web, monkeypatch, bucket, inv):
    monkeypatch.setattr(tp, "_bucket", lambda: bucket)
    monkeypatch.setattr(tp.pi, "lire_invitation",
                        lambda i: dict(inv) if inv else None)
    signale = mock.Mock()
    monkeypatch.setattr(tp.taches, "signaler", signale)
    reponse = web.get("/taches/portail/reconciliation",
                      headers={"X-Appengine-Cron": "true"})
    return reponse, signale


def test_reconciliation_enveloppe_orpheline_reenfilee(web, monkeypatch):
    # Envelope exists but the invitation never recorded the batch (§8.4.3).
    reponse, signale = _reconcilier(web, monkeypatch, _bucket_soumis(), _inv())
    assert reponse.status_code == 200
    assert reponse.get_json() == {"lots_vus": 1, "repares": 1}
    signale.assert_called_once_with("soumise", "inv1", batch="b1")


def test_reconciliation_accuse_manquant_reenfile(web, monkeypatch):
    inv = _inv(soumissions=[{"batch": "b1"}], accuses={})
    reponse, signale = _reconcilier(web, monkeypatch, _bucket_soumis(), inv)
    assert reponse.get_json()["repares"] == 1
    signale.assert_called_once()


def test_reconciliation_lot_complet_intact(web, monkeypatch):
    inv = _inv(soumissions=[{"batch": "b1"}], accuses={"b1": True})
    reponse, signale = _reconcilier(web, monkeypatch, _bucket_soumis(), inv)
    assert reponse.get_json() == {"lots_vus": 1, "repares": 0}
    signale.assert_not_called()


def test_reconciliation_ignore_lot_sans_enveloppe(web, monkeypatch):
    bucket = _FakeBucket({
        "submissions/inv1/b1/files/001_a.pdf": b"data",  # jamais finalisé
    })
    reponse, signale = _reconcilier(web, monkeypatch, bucket, _inv())
    assert reponse.get_json() == {"lots_vus": 0, "repares": 0}
    signale.assert_not_called()
