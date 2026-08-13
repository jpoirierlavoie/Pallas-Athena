"""Réception — versement restreint, traitement de lot, pastille (spec L1 §9).

CI-only style (imports models under the mocked Firestore client). The GET
views render the full base template and are exercised end-to-end manually;
these tests pin the LOGIC: the versement pre-check (restriction au
vocabulaire documents — 9 types depuis la décision 2026-08-11), the
freshness + SHA-512 guards of « Verser », the explicit-decision guard
before purging a lot, the provenance fields, and the badge cache fail-open.
"""

import hashlib
import io
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
    import routes.reception as rc

from flask import Flask  # noqa: E402


_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


@pytest.fixture()
def web():
    # template_folder set so the partie-search partial renders.
    app = Flask(__name__, template_folder=_TEMPLATES)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    from utils.icons import ms as _ms
    app.jinja_env.globals["ms"] = _ms  # icônes Material (global posé par create_app en prod)
    app.register_blueprint(rc.reception_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _entree(**over) -> dict:
    base = {
        "objet": "submissions/inv1/b1/files/001_piece.pdf",
        "name": "piece.pdf", "size_declared": 100, "size_gcs": 100,
        "content_type": "application/pdf",
        "sha512": "cafe" * 32, "etat": "reçu", "divergence": None,
    }
    base.update(over)
    return base


def _manifeste(*fichiers) -> dict:
    return {"batch": "b1", "invitation_id": "inv1",
            "files": list(fichiers), "etat_lot": "soumis"}


# Octets servis par le blob mocké des chemins nominaux de « Verser » — le
# durcissement 2026-08-11 vérifie l'empreinte du manifeste contre eux.
_OCTETS_PDF = b"%PDF-1.4 data"
_SHA_PDF = hashlib.sha512(_OCTETS_PDF).hexdigest()


def _blob_quarantaine(octets=_OCTETS_PDF, size=None):
    """Blob GCS mocké : métadonnées + lecture EN FLUX (jamais download_as_bytes
    entier — le versement par copie de 2026-08-12 n'en fait plus)."""
    blob = mock.MagicMock()
    blob.size = len(octets) if size is None else size
    blob.open.return_value.__enter__.return_value = io.BytesIO(octets)
    return blob


# ── Pré-contrôle du versement (restriction au vocabulaire actuel) ────────


def test_versable_pdf_recu():
    assert rc._versable(_entree()) is True


@pytest.mark.parametrize("nom", ["archive.zip", "courriel.eml", "message.msg",
                                 "classeur.xls", "classeur.xlsx"])
def test_versable_nouveaux_types(nom):
    # Décisions utilisateur 2026-08-11 (ZIP + courriels) et 2026-08-13
    # (Excel) : versables au dossier.
    assert rc._versable(_entree(name=nom)) is True


def test_versable_gros_fichier_200mo():
    # Décision 2026-08-12 : ≤ 200 Mo — le versement par copie côté serveur
    # ne fait plus transiter les octets par l'application.
    assert rc._versable(_entree(name="archive.zip",
                                size_gcs=130 * 1024 * 1024)) is True
    assert rc._versable(_entree(size_gcs=200 * 1024 * 1024)) is True


@pytest.mark.parametrize("entree", [
    _entree(name="photo.heic", content_type="image/heic"),
    _entree(name="video.mp4"),
    _entree(name="diapos.pptx"),
    _entree(size_gcs=201 * 1024 * 1024),          # > 200 Mo
    _entree(name="archive.zip", size_gcs=201 * 1024 * 1024),  # zip > 200 Mo
    _entree(size_gcs=0),
    _entree(etat="versé"),
    _entree(etat="refusé"),
    _entree(etat="manquant"),
])
def test_versable_refuse(entree):
    assert rc._versable(entree) is False


# ── Verser ───────────────────────────────────────────────────────────────


def _dossier():
    return {"id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie"}


def test_verser_ingere_avec_provenance(web, monkeypatch):
    manifeste = _manifeste(_entree(sha512=_SHA_PDF))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    ecrits = []
    monkeypatch.setattr(rc, "_ecrire_manifeste",
                        lambda i, b, m: ecrits.append(m))
    blob = _blob_quarantaine()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    monkeypatch.setattr(rc, "get_dossier",
                        lambda d: _dossier() if d == "d1" else None)
    monkeypatch.setattr(rc, "get_or_create_folder",
                        lambda d, nom: {"id": "f-portail", "name": nom})
    ingest = mock.Mock(return_value=({"id": "doc9"}, []))
    monkeypatch.setattr(rc, "ingest_blob_as_document", ingest)

    reponse = web.post(
        "/reception/lots/inv1/b1/fichiers/0/verser",
        data={"dossier_id": "d1", "category": "pièce", "display_name": ""},
    )
    assert reponse.status_code == 302
    assert "message=" in reponse.headers["Location"]

    args = ingest.call_args.args
    # Copie GCS→GCS : le BLOB passe tel quel, jamais d'octets en RAM.
    assert args[0] is blob
    assert args[1] == "d1" and args[2] == "2026-001"
    assert args[3] == "piece.pdf" and args[5] == "u1"
    metadata = args[4]
    assert metadata["tags"] == ["portail"]
    assert "invitation inv1" in metadata["description"]
    assert _SHA_PDF in metadata["description"]
    assert metadata["folder_id"] == "f-portail"
    blob.download_as_bytes.assert_not_called()
    # Manifest entry flipped to « versé » and persisted.
    assert manifeste["files"][0]["etat"] == "versé"
    assert ecrits


def test_verser_non_versable_refuse_sans_ingestion(web, monkeypatch):
    manifeste = _manifeste(_entree(name="photo.heic"))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    ingest = mock.Mock()
    monkeypatch.setattr(rc, "ingest_blob_as_document", ingest)

    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert reponse.status_code == 302
    assert "erreur=" in reponse.headers["Location"]
    ingest.assert_not_called()
    assert manifeste["files"][0]["etat"] == "reçu"  # untouched


def test_verser_deja_traite_refuse(web, monkeypatch):
    monkeypatch.setattr(rc, "_lire_manifeste",
                        lambda i, b: _manifeste(_entree(etat="versé")))
    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert "erreur=" in reponse.headers["Location"]


def test_verser_refuse_blob_regonfle_sans_lecture(web, monkeypatch):
    # Fraîcheur (revue 2026-08-11) : la taille jugée par _versable est celle
    # du MANIFESTE, figée à la prise d'empreintes — si le blob VIVANT a
    # grossi au-delà du plafond depuis, refuser AVANT toute lecture.
    manifeste = _manifeste(_entree())
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    blob = _blob_quarantaine(size=201 * 1024 * 1024)
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    monkeypatch.setattr(rc, "get_dossier", lambda d: _dossier())
    ingest = mock.Mock()
    monkeypatch.setattr(rc, "ingest_blob_as_document", ingest)

    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert "erreur=" in reponse.headers["Location"]
    blob.open.assert_not_called()
    ingest.assert_not_called()
    assert manifeste["files"][0]["etat"] == "reçu"  # untouched


def test_verser_refuse_divergence_sha512(web, monkeypatch):
    # Intégrité probante : la description du document cite l'empreinte du
    # manifeste — des octets qui ne la confirment pas ne sont jamais versés.
    # Le recalcul se fait EN FLUX (l'objet peut faire 200 Mo).
    manifeste = _manifeste(_entree())          # sha512 = "cafe" * 32
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    blob = _blob_quarantaine()                 # streams _OCTETS_PDF
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    monkeypatch.setattr(rc, "get_dossier", lambda d: _dossier())
    ingest = mock.Mock()
    monkeypatch.setattr(rc, "ingest_blob_as_document", ingest)

    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert "erreur=" in reponse.headers["Location"]
    ingest.assert_not_called()
    assert manifeste["files"][0]["etat"] == "reçu"  # untouched


# ── Télécharger (redirection URL signé — plafond 32 Mo App Engine) ───────


def _preparer_telechargement(monkeypatch, entree):
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: _manifeste(entree))
    blob = mock.Mock()
    blob.exists.return_value = True
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    captures: dict = {}
    monkeypatch.setattr(
        rc, "sign_blob_url",
        lambda b, params, expiry_minutes=15: captures.update(params=params)
        or "https://signed.example/quarantaine",
    )
    return captures


def test_telecharger_redirige_vers_un_url_signe(web, monkeypatch):
    # App Engine Standard PLAFONNE toute réponse à 32 Mo (« Response size
    # was too large », 2026-08-12 — un lot de 68 et 128 Mo répondait 500) :
    # les octets ne transitent JAMAIS par l'app — redirection vers un URL
    # signé, attachment forcé + type déclaré (§7.5 intact).
    captures = _preparer_telechargement(monkeypatch, _entree())
    reponse = web.get("/reception/lots/inv1/b1/fichiers/0")
    assert reponse.status_code == 302
    assert reponse.headers["Location"] == "https://signed.example/quarantaine"
    disposition = captures["params"]["response-content-disposition"]
    assert disposition.startswith('attachment; filename="piece.pdf"')
    assert captures["params"]["response-content-type"] == "application/pdf"


def test_telecharger_nom_falsifie_nettoye(web, monkeypatch):
    # Le nom d'origine reste VERBATIM dans le manifeste (valeur probante) —
    # les caractères de contrôle sont retirés AU POINT D'USAGE seulement.
    captures = _preparer_telechargement(
        monkeypatch, _entree(name="pi\r\nece.pdf"),
    )
    reponse = web.get("/reception/lots/inv1/b1/fichiers/0")
    assert reponse.status_code == 302
    assert '"piece.pdf"' in captures["params"]["response-content-disposition"]


def test_telecharger_echec_de_signature_degrade_en_erreur(web, monkeypatch):
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: _manifeste(_entree()))
    blob = mock.Mock()
    blob.exists.return_value = True
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    monkeypatch.setattr(rc, "sign_blob_url",
                        mock.Mock(side_effect=RuntimeError("iam down")))
    reponse = web.get("/reception/lots/inv1/b1/fichiers/0")
    assert reponse.status_code == 302
    assert "erreur=" in reponse.headers["Location"]


# ── Refuser + traiter le lot ─────────────────────────────────────────────


def test_refuser_marque_le_fichier(web, monkeypatch):
    manifeste = _manifeste(_entree())
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    monkeypatch.setattr(rc, "_ecrire_manifeste", lambda i, b, m: None)
    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/refuser")
    assert "message=" in reponse.headers["Location"]
    assert manifeste["files"][0]["etat"] == "refusé"


def test_traiter_bloque_si_fichier_recu(web, monkeypatch):
    monkeypatch.setattr(
        rc, "_lire_manifeste",
        lambda i, b: _manifeste(_entree(etat="versé"), _entree()),
    )
    bucket = mock.Mock()
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    reponse = web.post("/reception/lots/inv1/b1/traiter")
    assert "erreur=" in reponse.headers["Location"]
    bucket.copy_blob.assert_not_called()  # never purge unexamined files


def _bucket_traiter(monkeypatch, freres=()):
    """Mock bucket: files/ objects to purge, source JSONs, sibling scan."""
    fichiers_restants = [mock.Mock(), mock.Mock()]
    sources = {}

    def _blob(nom):
        src = sources.setdefault(nom, mock.Mock())
        src.exists.return_value = not nom.startswith("archive/")
        return src

    def _list(prefix=""):
        if prefix.endswith("files/"):
            return fichiers_restants
        # Sibling scan over submissions/{inv}/ — envelope.json of OTHER lots.
        return [mock.Mock(name=n, **{"name": n}) for n in freres]

    bucket = mock.Mock()
    bucket.blob.side_effect = _blob
    bucket.list_blobs.side_effect = _list
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    return bucket, sources, fichiers_restants


def test_traiter_archive_et_purge(web, monkeypatch):
    manifeste = _manifeste(_entree(etat="versé"), _entree(etat="refusé"))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    monkeypatch.setattr(rc, "_ecrire_manifeste", lambda i, b, m: None)
    bucket, sources, fichiers_restants = _bucket_traiter(monkeypatch)
    statuts = []
    monkeypatch.setattr(rc.pi, "maj_statut",
                        lambda i, s: statuts.append((i, s)) or True)

    reponse = web.post("/reception/lots/inv1/b1/traiter")
    assert "message=" in reponse.headers["Location"]
    assert manifeste["etat_lot"] == "traité"
    # envelope + manifeste copiés sous archive/ puis supprimés.
    copies = [c.args for c in bucket.copy_blob.call_args_list]
    assert {c[2] for c in copies} == {
        "archive/inv1/b1/envelope.json", "archive/inv1/b1/manifeste.json",
    }
    for nom, src in sources.items():
        if not nom.startswith("archive/"):
            src.delete.assert_called_once()
    for restant in fichiers_restants:
        restant.delete.assert_called_once()
    assert statuts == [("inv1", "traitée")]


def test_traiter_ne_ferme_pas_avec_un_lot_frere(web, monkeypatch):
    # A second submitted batch of the SAME invitation must keep the statut
    # « soumise » — flipping it would make the sibling lot invisible in
    # Réception and the reconciliation would never replay it (review HIGH).
    manifeste = _manifeste(_entree(etat="versé"))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    monkeypatch.setattr(rc, "_ecrire_manifeste", lambda i, b, m: None)
    _bucket_traiter(monkeypatch,
                    freres=("submissions/inv1/b2/envelope.json",))
    maj = mock.Mock()
    monkeypatch.setattr(rc.pi, "maj_statut", maj)

    reponse = web.post("/reception/lots/inv1/b1/traiter")
    assert "message=" in reponse.headers["Location"]
    assert "examiner" in reponse.headers["Location"]
    maj.assert_not_called()


def test_traiter_reprend_un_lot_deja_archive(web, monkeypatch):
    # A previous attempt failed AFTER archiving (e.g. the statut update):
    # the retry must recover from archive/ instead of « Lot introuvable ».
    import json as _json
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: None)
    manifeste = _manifeste(_entree(etat="versé"))
    manifeste["etat_lot"] = "traité"

    def _blob(nom):
        b = mock.Mock()
        b.exists.return_value = nom == "archive/inv1/b1/manifeste.json"
        b.download_as_bytes.return_value = _json.dumps(manifeste).encode()
        return b

    bucket = mock.Mock()
    bucket.blob.side_effect = _blob
    bucket.list_blobs.return_value = []  # no sibling lot
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    statuts = []
    monkeypatch.setattr(rc.pi, "maj_statut",
                        lambda i, s: statuts.append((i, s)) or True)

    reponse = web.post("/reception/lots/inv1/b1/traiter")
    assert "message=" in reponse.headers["Location"]
    bucket.copy_blob.assert_not_called()  # nothing re-archived
    assert statuts == [("inv1", "traitée")]


def test_verser_avertit_si_le_manifeste_ne_peut_etre_ecrit(web, monkeypatch):
    # The document IS in the dossier but the quarantine state still says
    # « reçu » — a plain success message would invite a duplicate ingest.
    manifeste = _manifeste(_entree(sha512=_SHA_PDF))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    monkeypatch.setattr(rc, "_ecrire_manifeste",
                        mock.Mock(side_effect=RuntimeError("gcs down")))
    blob = _blob_quarantaine()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    monkeypatch.setattr(rc, "get_dossier", lambda d: _dossier())
    monkeypatch.setattr(rc, "get_or_create_folder", lambda d, n: None)
    monkeypatch.setattr(rc, "ingest_blob_as_document",
                        mock.Mock(return_value=({"id": "doc9"}, [])))

    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert "erreur=" in reponse.headers["Location"]
    assert "seconde+fois" in reponse.headers["Location"].replace("%20", "+") \
        or "seconde" in reponse.headers["Location"]


# ── Pastille (cache + fail-open) ─────────────────────────────────────────


def test_compteur_reception_cache_et_fail_open(monkeypatch):
    rc._badge_cache.update(at=0.0, n=None)
    compte = mock.Mock(return_value=3)
    monkeypatch.setattr(rc.pi, "compter_soumises", compte)
    assert rc.compteur_reception() == 3
    assert rc.compteur_reception() == 3
    assert compte.call_count == 1  # cached within the TTL

    rc._badge_cache.update(at=0.0)
    compte.return_value = None  # base « portail » absente → fail-open
    assert rc.compteur_reception() is None
    rc._badge_cache.update(at=0.0, n=None)


# ── Fenêtre d'archive (détail d'une invitation traitée) ──────────────────


def test_archive_modal_affiche_fichiers_et_choix(web, monkeypatch):
    inv = {"id": "inv1", "email": "client@exemple.com", "statut": "traitée",
           "display_label": "Dossier 2026-001",
           "soumissions": [{"batch": "b1", "files_count": 2}]}
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: inv)
    manifeste = {"files": [
        {"name": "requete.pdf", "size_gcs": 2048, "content_type": "application/pdf",
         "sha512": "ab" * 32, "etat": "versé", "verse_dossier": "2026-001",
         "verse_nom": "Requête introductive"},
        {"name": "photo.heic", "size_gcs": 500, "content_type": "image/heic",
         "sha512": "cd" * 32, "etat": "refusé"},
    ]}
    monkeypatch.setattr(rc, "_lire_manifeste_archive", lambda i, b: manifeste)

    html = web.get("/reception/invitations/inv1/archive").get_data(as_text=True)
    assert "Dossier 2026-001" in html
    assert "requete.pdf" in html and ("ab" * 32) in html
    assert "versé · dossier 2026-001" in html
    assert "photo.heic" in html and "refusé" in html
    assert "Requête introductive" in html
    assert 'id="reception-modal"' not in html  # c'est le CONTENU, pas le mount


def test_archive_modal_invitation_inconnue(web, monkeypatch):
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: None)
    html = web.get("/reception/invitations/fantome/archive").get_data(as_text=True)
    assert "impossible" in html.lower()


def test_archive_modal_lot_sans_manifeste(web, monkeypatch):
    inv = {"id": "inv1", "email": "c@e.com", "statut": "révoquée",
           "display_label": "X", "soumissions": []}
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: inv)
    html = web.get("/reception/invitations/inv1/archive").get_data(as_text=True)
    assert "Aucune transmission" in html


def test_archive_manifeste_prefere_archive_puis_submissions(monkeypatch):
    # _lire_manifeste_archive lit archive/ d'abord, submissions/ en repli.
    lus = []

    class _Blob:
        def __init__(self, nom, existe):
            self._nom, self._existe = nom, existe

        def exists(self):
            lus.append(self._nom)
            return self._existe

        def download_as_bytes(self):
            return b'{"files": [{"name": "a.pdf"}]}'

    bucket = mock.Mock()
    bucket.blob.side_effect = lambda nom: _Blob(
        nom, nom.startswith("archive/"))
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    m = rc._lire_manifeste_archive("inv1", "b1")
    assert m["files"][0]["name"] == "a.pdf"
    assert lus[0] == "archive/inv1/b1/manifeste.json"  # archive d'abord


# ── Sélecteur de client (autocomplétion + soumission) ────────────────────


def test_partie_search_min_chars(web):
    reponse = web.get("/reception/partie-search?q=a")
    assert reponse.status_code == 200
    assert "au moins 2" in reponse.get_data(as_text=True)


def test_partie_search_renvoie_lignes(web, monkeypatch):
    monkeypatch.setattr(rc, "list_parties", lambda search=None: [
        {"id": "p1", "contact_role": "client",
         "email": "jean@exemple.com", "first_name": "Jean", "last_name": "Tremblay"},
    ])
    monkeypatch.setattr(rc, "display_name", lambda p: "Jean Tremblay")
    html = web.get("/reception/partie-search?q=tre").get_data(as_text=True)
    assert 'data-partie-id="p1"' in html
    assert 'data-partie-name="Jean Tremblay"' in html
    assert 'data-partie-email="jean@exemple.com"' in html


def test_inviter_submit_transmet_partie_et_nom(web, monkeypatch):
    monkeypatch.setattr(rc, "get_dossier", lambda d: None)
    monkeypatch.setattr(rc, "get_partie",
                        lambda p: {"id": "p1"} if p == "p1" else None)
    captures = {}

    def _emettre(type_, email, **kw):
        captures.update(email=email, **kw)
        return {"id": "inv9"}, [], ""

    monkeypatch.setattr(rc.emission, "emettre_invitation", _emettre)
    with mock.patch("flask_wtf.csrf.validate_csrf", return_value=None):
        reponse = web.post("/reception/inviter", data={
            "email": "jean@exemple.com", "partie_id": "p1",
            "client_name": "Jean Tremblay", "display_label": "Dossier X",
            "jours": "30",
        })
    assert reponse.status_code == 302
    assert captures["partie_id"] == "p1"
    assert captures["client_name"] == "Jean Tremblay"


def test_inviter_submit_ignore_partie_inconnue(web, monkeypatch):
    monkeypatch.setattr(rc, "get_dossier", lambda d: None)
    monkeypatch.setattr(rc, "get_partie", lambda p: None)  # id ne résout pas
    captures = {}

    def _emettre(type_, email, **kw):
        captures.update(**kw)
        return {"id": "inv9"}, [], ""

    monkeypatch.setattr(rc.emission, "emettre_invitation", _emettre)
    with mock.patch("flask_wtf.csrf.validate_csrf", return_value=None):
        web.post("/reception/inviter", data={
            "email": "jean@exemple.com", "partie_id": "fantome",
            "client_name": "Jean", "display_label": "Dossier X",
        })
    # id non résolu → ignoré, jamais bloquant ; le nom manuel subsiste.
    assert captures["partie_id"] is None
    assert captures["client_name"] == "Jean"
