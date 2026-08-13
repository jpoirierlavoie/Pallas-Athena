"""Archive ZIP d'un dossier de classement (décision 2026-08-13).

L'archive est composée DANS GCS (flux ; App Engine plafonne toute réponse à
32 Mo) puis remise par URL signé. Les octets capturés par le faux writer
sont RELUS au zipfile — le contenu, l'arborescence, le dédoublonnage et les
horodatages sont vérifiés sur l'archive réelle, pas sur des mocks. Les deux
pièges vérifiés empiriquement sur la pile épinglée sont épinglés ici :
ignore_flush=True (zipfile appelle flush(), BlobWriter.flush() lève sans
lui) et la terminaison propre sur exception (aucun objet partiel).
"""

import io
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from google.cloud.exceptions import NotFound  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import models.document as doc
    import models.dossier as dossier_model
    import models.folder as folder_model
    import routes.documents as rd

from flask import Flask  # noqa: E402


# ── Faux GCS ─────────────────────────────────────────────────────────────


class _FauxWriter(io.BytesIO):
    """Writer qui capture les octets à la fermeture et note la terminaison
    (le vrai BlobWriter.__exit__ TERMINE la session sur exception)."""

    def __init__(self, registre):
        super().__init__()
        self._registre = registre

    def close(self):
        if not self.closed:
            self._registre["octets"] = self.getvalue()
        super().close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._registre["terminated"] = True
            io.BytesIO.close(self)     # rien n'est finalisé
            return False
        self.close()
        return False


class _FauxZipBlob:
    def __init__(self, path, registre):
        self.path = path
        self.content_disposition = None
        self._registre = registre
        registre["zip_path"] = path

    def open(self, mode, **kwargs):
        self._registre["open_mode"] = mode
        self._registre["open_kwargs"] = kwargs
        self._registre["disposition_avant_open"] = self.content_disposition
        return _FauxWriter(self._registre)


class _FauxSrcBlob:
    def __init__(self, contenu):
        self._contenu = contenu

    def download_to_file(self, fh):
        if self._contenu is None:
            raise NotFound("objet absent")
        fh.write(self._contenu)


class _FauxBucket:
    def __init__(self, sources, registre):
        self._sources = sources
        self._registre = registre

    def blob(self, path):
        if path.startswith("staging/"):
            return _FauxZipBlob(path, self._registre)
        return _FauxSrcBlob(self._sources.get(path))


def _quand(j=1):
    return datetime(2026, 8, j, 15, 30, 0, tzinfo=timezone.utc)


def _doc(nom, chemin, taille=4, folder_id=None, file_type="application/pdf",
         display=None):
    return {
        "display_name": display if display is not None else nom,
        "original_filename": nom, "filename": nom,
        "storage_path": chemin, "file_size": taille,
        "folder_id": folder_id, "file_type": file_type,
        "created_at": _quand(),
    }


@pytest.fixture
def bac(monkeypatch):
    """Monte le faux GCS + les modèles ; retourne (registre, poser)."""
    registre: dict = {"terminated": False}
    etat = {"sources": {}, "tree": [], "docs": []}

    monkeypatch.setattr(
        doc.storage, "bucket",
        lambda: _FauxBucket(etat["sources"], registre),
    )
    monkeypatch.setattr(
        dossier_model, "get_dossier",
        lambda did: {"id": did, "file_number": "2026-001"} if did == "d1" else None,
    )
    monkeypatch.setattr(
        folder_model, "get_folder",
        lambda did, fid: next(
            (dict(n) for n in _aplatir(etat["tree"]) if n["id"] == fid), None
        ),
    )
    monkeypatch.setattr(folder_model, "get_folder_tree",
                        lambda did: etat["tree"])
    monkeypatch.setattr(doc, "list_documents",
                        lambda dossier_id=None: etat["docs"])
    signatures: dict = {}
    registre["signatures"] = signatures

    def _signer(blob, params, expiry_minutes=15):
        signatures["params"] = params
        signatures["blob_path"] = blob.path
        return "https://signed.example/archive"

    monkeypatch.setattr(doc, "sign_blob_url", _signer)
    return registre, etat


def _aplatir(nodes):
    for n in nodes:
        yield n
        yield from _aplatir(n.get("children", []))


def _noeud(fid, nom, children=()):
    return {"id": fid, "name": nom, "children": list(children)}


def _relire(registre):
    return zipfile.ZipFile(io.BytesIO(registre["octets"]))


# ── Contenu et arborescence ──────────────────────────────────────────────


def test_zip_contient_l_arborescence_et_le_contenu(bac):
    registre, etat = bac
    etat["tree"] = [_noeud("fA", "Projets", [_noeud("fB", "Annexes")])]
    etat["sources"] = {"s/un.pdf": b"%PDF-un", "s/deux.pdf": b"%PDF-deux"}
    etat["docs"] = [
        _doc("un.pdf", "s/un.pdf", folder_id="fA"),
        _doc("deux.pdf", "s/deux.pdf", folder_id="fB"),
    ]
    url, erreurs = doc.build_folder_zip_url("d1", "fA", "u1")
    assert erreurs == [] and url == "https://signed.example/archive"
    zf = _relire(registre)
    assert set(zf.namelist()) == {"Annexes/", "un.pdf", "Annexes/deux.pdf"}
    assert zf.read("un.pdf") == b"%PDF-un"
    assert zf.read("Annexes/deux.pdf") == b"%PDF-deux"
    info = zf.getinfo("un.pdf")
    assert info.compress_type == zipfile.ZIP_STORED
    # Horodatage de created_at (heure de Montréal — 15:30 UTC = 11:30 EDT).
    assert info.date_time[:5] == (2026, 8, 1, 11, 30)
    assert zf.testzip() is None


def test_zip_parametres_du_writer(bac):
    registre, etat = bac
    etat["sources"] = {"s/a.pdf": b"%PDF"}
    etat["docs"] = [_doc("a.pdf", "s/a.pdf")]
    url, erreurs = doc.build_folder_zip_url("d1", None, "u1")
    assert erreurs == []
    kw = registre["open_kwargs"]
    assert registre["open_mode"] == "wb"
    # zipfile appelle flush() — BlobWriter.flush() LÈVE sans ce drapeau.
    assert kw["ignore_flush"] is True
    assert kw["chunk_size"] % (256 * 1024) == 0
    assert kw["content_type"] == "application/zip"
    # Disposition posée sur l'OBJET avant l'ouverture (portée par
    # l'initiation de la session recomposable).
    assert registre["disposition_avant_open"] == "attachment"


def test_zip_nom_extension_garantie(bac):
    registre, etat = bac
    etat["sources"] = {"s/p.pdf": b"%PDF", "s/ph.jpg": b"\xff\xd8"}
    etat["docs"] = [
        _doc("p.pdf", "s/p.pdf", display="Pièce P-1.2"),
        _doc("ph.jpg", "s/ph.jpg", display="photo.jpeg",
             file_type="image/jpeg"),
    ]
    doc.build_folder_zip_url("d1", None, "u1")
    noms = set(_relire(registre).namelist())
    assert "Pièce P-1.2.pdf" in noms          # extension ajoutée
    assert "photo.jpeg" in noms               # jamais doublée en .jpeg.jpg


def test_zip_noms_hostiles_windows_assainis(bac):
    registre, etat = bac
    etat["tree"] = [_noeud("fA", "Pièces <2024>")]
    etat["sources"] = {"s/r.pdf": b"%PDF", "s/c.pdf": b"%PDF"}
    etat["docs"] = [
        _doc("r.pdf", "s/r.pdf", folder_id="fA",
             display='Rapport: "final"? *v2*'),
        _doc("c.pdf", "s/c.pdf", display="CON.pdf"),
    ]
    doc.build_folder_zip_url("d1", None, "u1")
    noms = _relire(registre).namelist()
    for nom in noms:
        interieur = nom.rstrip("/").replace("/", "")
        assert not any(c in interieur for c in '<>:"\\|?*'), nom
        assert not nom.rstrip("/").endswith((".", " ")), nom
    assert any(n.startswith("_CON") for n in noms)     # nom DOS réservé


def test_zip_deduplication_par_repertoire(bac):
    registre, etat = bac
    etat["tree"] = [_noeud("fA", "Annexes"), _noeud("fD", "Rapport.pdf")]
    etat["sources"] = {"s/1": b"a", "s/2": b"b", "s/3": b"c",
                       "s/4": b"d", "s/5": b"e"}
    etat["docs"] = [
        _doc("x.pdf", "s/1", display="X"),
        _doc("x2.pdf", "s/2", display="X"),               # même nom, même rép.
        _doc("x3.pdf", "s/3", folder_id="fA", display="X"),   # autre rép.
        # PAS une collision : après la garantie d'extension, le fichier
        # « Annexes.pdf » et le dossier « Annexes/ » sont distincts.
        _doc("annexes.pdf", "s/4", display="Annexes"),
        # Collision EXACTE avec un dossier frère (« Rapport.pdf ») — le
        # dossier réclame le nom d'abord, le fichier est suffixé.
        _doc("r.pdf", "s/5", display="Rapport"),
    ]
    doc.build_folder_zip_url("d1", None, "u1")
    noms = set(_relire(registre).namelist())
    assert "X.pdf" in noms and "X (2).pdf" in noms
    assert "Annexes/X.pdf" in noms                        # intact ailleurs
    assert "Annexes.pdf" in noms                          # pas de faux positif
    assert "Rapport.pdf/" in noms
    assert "Rapport (2).pdf" in noms


# ── Refus AVANT tout octet ───────────────────────────────────────────────


def test_zip_refus_plafond_octets(bac):
    registre, etat = bac
    etat["docs"] = [_doc("a.pdf", "s/a", taille=doc.MAX_ZIP_TOTAL_BYTES + 1)]
    url, erreurs = doc.build_folder_zip_url("d1", None, "u1")
    assert url is None and erreurs and "400 Mo" in erreurs[0]
    assert "octets" not in registre                       # rien déplacé


def test_zip_refus_plafond_fichiers(bac):
    registre, etat = bac
    etat["sources"] = {}
    etat["docs"] = [
        _doc(f"f{i}.pdf", f"s/{i}") for i in range(doc.MAX_ZIP_FILES + 1)
    ]
    url, erreurs = doc.build_folder_zip_url("d1", None, "u1")
    assert url is None and erreurs and "sous-dossier" in erreurs[0]
    assert "octets" not in registre


def test_zip_refus_dossier_vide(bac):
    registre, etat = bac
    etat["tree"] = [_noeud("fA", "Vide")]
    url, erreurs = doc.build_folder_zip_url("d1", "fA", "u1")
    assert url is None and erreurs
    assert "aucun document" in erreurs[0]


# ── Tout-ou-rien ─────────────────────────────────────────────────────────


def test_zip_blob_manquant_annule_tout(bac):
    registre, etat = bac
    etat["sources"] = {"s/ok.pdf": b"%PDF"}               # s/absent → NotFound
    etat["docs"] = [
        _doc("ok.pdf", "s/ok.pdf"),
        _doc("perdu.pdf", "s/absent"),
    ]
    url, erreurs = doc.build_folder_zip_url("d1", None, "u1")
    assert url is None
    assert erreurs and "aucun fichier partiel" in erreurs[0]
    # Le writer a été TERMINÉ (session annulée — aucun objet, même partiel).
    assert registre["terminated"] is True
    assert "params" not in registre["signatures"]         # jamais signé


# ── Sous-arbres, orphelins, dossiers vides ───────────────────────────────


def test_zip_sous_arbre_et_orphelins(bac):
    registre, etat = bac
    etat["tree"] = [
        _noeud("fA", "Projets", [_noeud("fB", "Annexes")]),
        _noeud("fC", "Cousin"),
    ]
    etat["sources"] = {"s/1": b"a", "s/2": b"b", "s/3": b"c"}
    etat["docs"] = [
        _doc("a.pdf", "s/1", folder_id="fA"),
        _doc("b.pdf", "s/2", folder_id="fC"),
        _doc("orphelin.pdf", "s/3", folder_id="fantome"),  # réf. pendante
    ]
    # Sous-arbre fA : ni le cousin ni l'orphelin.
    doc.build_folder_zip_url("d1", "fA", "u1")
    assert set(_relire(registre).namelist()) == {"Annexes/", "a.pdf"}
    # Dossier entier : l'orphelin atterrit à la RACINE — jamais omis.
    doc.build_folder_zip_url("d1", None, "u1")
    noms = set(_relire(registre).namelist())
    assert "orphelin.pdf" in noms and "Cousin/b.pdf" in noms


def test_zip_dossier_vide_conserve(bac):
    registre, etat = bac
    etat["tree"] = [_noeud("fA", "Projets", [_noeud("fB", "Vide")])]
    etat["sources"] = {"s/1": b"a"}
    etat["docs"] = [_doc("a.pdf", "s/1", folder_id="fA")]
    doc.build_folder_zip_url("d1", "fA", "u1")
    assert "Vide/" in _relire(registre).namelist()


def test_zip_signature_et_disposition(bac):
    registre, etat = bac
    etat["tree"] = [_noeud("fA", "Projets")]
    etat["sources"] = {"s/1": b"a"}
    etat["docs"] = [_doc("a.pdf", "s/1", folder_id="fA")]
    url, erreurs = doc.build_folder_zip_url("d1", "fA", "u1")
    assert url and erreurs == []
    sig = registre["signatures"]
    disposition = sig["params"]["response-content-disposition"]
    assert disposition.startswith('attachment; filename="')
    assert "2026-001 - Projets.zip" in disposition
    assert sig["params"]["response-content-type"] == "application/zip"
    assert sig["blob_path"].startswith("staging/u1/exports/")
    assert sig["blob_path"].endswith("/2026-001 - Projets.zip")


# ── Route ────────────────────────────────────────────────────────────────


@pytest.fixture()
def web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def test_route_zip_redirige_vers_l_url_signee(web, monkeypatch):
    appel = {}

    def _construire(dossier_id, folder_id, user_id):
        appel.update(d=dossier_id, f=folder_id, u=user_id)
        return "https://signed.example/archive", []

    monkeypatch.setattr(rd, "build_folder_zip_url", _construire)
    reponse = web.get("/documents/zip?dossier_id=d1&folder_id=f1")
    assert reponse.status_code == 302
    assert reponse.headers["Location"] == "https://signed.example/archive"
    assert appel == {"d": "d1", "f": "f1", "u": "u1"}


def test_route_zip_rebondit_avec_erreur(web, monkeypatch):
    monkeypatch.setattr(rd, "build_folder_zip_url",
                        lambda d, f, u: (None, ["Trop volumineux."]))
    reponse = web.get("/documents/zip?dossier_id=d1&folder_id=f1")
    assert reponse.status_code == 302
    cible = reponse.headers["Location"]
    assert "dossier_id=d1" in cible and "folder_id=f1" in cible
    assert "erreur=" in cible


def test_route_zip_sans_dossier_id(web, monkeypatch):
    construire = mock.Mock()
    monkeypatch.setattr(rd, "build_folder_zip_url", construire)
    reponse = web.get("/documents/zip")
    assert reponse.status_code == 302
    construire.assert_not_called()


def test_liste_transmet_l_erreur_au_gabarit(web, monkeypatch):
    monkeypatch.setattr(rd, "list_documents", lambda **k: [])
    monkeypatch.setattr(rd, "list_dossiers", lambda: [])
    captures: dict = {}
    monkeypatch.setattr(
        rd, "render_template",
        lambda gabarit, **ctx: captures.update(ctx) or "ok",
    )
    reponse = web.get("/documents/?erreur=Boom")
    assert reponse.status_code == 200
    assert captures["erreur"] == "Boom"
