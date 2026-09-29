"""Document management routes — upload, list, detail, edit, delete, download.

Includes folder management routes for hierarchical document organization.
"""

import logging
import uuid

from firebase_admin import storage
from google.cloud.exceptions import NotFound
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import escape
from werkzeug.utils import secure_filename

from auth import login_required
from config import Config
from models.audit_event import record_deletion
from security import safe_internal_redirect, sanitize
from pagination import paginate
from models.dossier import get_dossier, list_dossiers
from models import document as document_model
from models.document import (
    ALLOWED_EXTENSIONS,
    CATEGORY_CHOICES,
    CATEGORY_LABELS,
    MAX_FILE_SIZE,
    VALID_CATEGORIES,
    build_folder_zip_url,
    delete_document,
    format_file_size,
    get_document,
    get_file_icon,
    get_signed_url,
    UPLOAD_DEFAULT_CATEGORY,
    ingest_blob_as_document,
    list_documents,
    move_document,
    move_documents_bulk,
    update_metadata,
    update_analyse,
    ANALYSE_EDITABLE,
)
from models import concurrency
from routes import edit_conflict
from routes._helpers import is_htmx
from utils import storage_identity

logger = logging.getLogger(__name__)
from models.folder import (
    create_folder,
    delete_folder,
    get_folder,
    get_folder_breadcrumb,
    get_folder_tree,
    list_folders,
    move_folder,
    rename_folder,
)

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")

# The only types detail.html renders inline (PDF iframe, image <img>).
# Everything else — ZIP, .eml, .msg included — is served exclusively as
# an attachment through the download route: the inline signed URL is
# simply never minted for them (which also saves an IAM signBlob call).
_PREVIEWABLE_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}


_is_htmx = is_htmx


def _attach_computed_fields(documents: list[dict]) -> None:
    """Attach display helpers to document dicts."""
    for d in documents:
        d["_file_size_fmt"] = format_file_size(d.get("file_size", 0))
        d["_file_icon"] = get_file_icon(d.get("file_type", ""))


def _attach_folder_counts(folders: list[dict], dossier_id: str) -> None:
    """Attach the row count AND the subtree counts the delete dialog needs.

    ONE pass over two queries (``subtree_index``) instead of one query per
    folder — the N+1 the July 2026 note removed from the dossier tab still
    lived here. The subtree figures are what the destructive confirmation
    announces, so they must count the whole tree, not the top level. Fails
    to zeros rather than breaking the listing; the dialog then offers no
    « tout supprimer » (see the template's guard on ``_subtree_documents``),
    and since lot 2A (T2) it POSTS those zeros back — so the model refuses
    a folder that is not really empty instead of emptying it unseen.
    """
    from models.folder import is_system_folder, subtree_index

    try:
        index = subtree_index(dossier_id)
    except Exception:
        logger.warning("folder counts unavailable for the browser")
        index = {}
    for f in folders:
        counts = index.get(f["id"]) or {}
        f["_item_count"] = counts.get("direct", 0)
        f["_subtree_documents"] = counts.get("documents", 0)
        f["_subtree_folders"] = counts.get("folders", 0)
        # WHICH records the dialog announces, not only how many (review of
        # T2): posted back, it refuses a swap the counts cannot see. Absent
        # when the index failed — the zero counts already refuse then.
        f["_subtree_fingerprint"] = counts.get("fingerprint", "")
        # « Projets » / « Reçus du portail » do not rename (lot 2A, T2) —
        # the menu hides the action; the model refuses it anyway. The
        # siblings are the context: a system folder sits at the root, and
        # the root listing holds every candidate (models.folder).
        f["_system"] = is_system_folder(f, folders)


# ── List / Browser ───────────────────────────────────────────────────────


@documents_bp.route("/")
@login_required
def document_list() -> str:
    """Render the document browser with folder navigation."""
    dossier_id = request.args.get("dossier_id", "").strip()
    folder_id = request.args.get("folder_id", "").strip() or None
    category_filter = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "created_at")
    page = request.args.get("page", 1, type=int)

    # When a dossier is selected and not searching, filter by folder
    if dossier_id and not search:
        documents = list_documents(
            dossier_id=dossier_id,
            folder_id=folder_id,
            category=category_filter or None,
            sort_by=sort_by,
        )
        folders_list = list_folders(dossier_id, parent_folder_id=folder_id)
        _attach_folder_counts(folders_list, dossier_id)
        breadcrumb = get_folder_breadcrumb(dossier_id, folder_id)
    elif dossier_id and search:
        # Search across all folders
        documents = list_documents(
            dossier_id=dossier_id,
            category=category_filter or None,
            search=search,
            sort_by=sort_by,
        )
        folders_list = []
        breadcrumb = []
    else:
        # No dossier selected — show all documents flat
        documents = list_documents(
            dossier_id=None,
            category=category_filter or None,
            search=search or None,
            sort_by=sort_by,
        )
        folders_list = []
        breadcrumb = []

    _attach_computed_fields(documents)

    documents, pagination = paginate(documents, page)
    pagination["url"] = url_for("documents.document_list")
    pagination["target"] = "#browser-content"
    if folder_id:
        pagination["extra_vals"] = {"folder_id": folder_id}

    ctx = {
        "documents": documents,
        "folders": folders_list,
        "breadcrumb": breadcrumb,
        "dossier_id": dossier_id,
        "folder_id": folder_id,
        "category_filter": category_filter,
        "search": search,
        "sort_by": sort_by,
        "category_labels": CATEGORY_LABELS,
        "valid_categories": VALID_CATEGORIES,
        "pagination": pagination,
        # Rebond des routes qui redirigent vers le navigateur avec un
        # message d'erreur (archive zip…) — motif reception.
        "erreur": sanitize(request.args.get("erreur", ""), max_length=300),
        # …et son pendant affirmatif : une suppression destructive dit ce
        # qui a disparu, sans quoi elle ne laisse aucune trace à l'écran.
        "message": sanitize(request.args.get("message", ""), max_length=300),
    }

    if _is_htmx():
        return render_template("documents/_browser.html", **ctx)

    ctx["dossiers"] = list_dossiers()
    return render_template("documents/list.html", **ctx)


# ── Detail ────────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>")
@login_required
def document_detail(document_id: str) -> str:
    """Render the document detail/viewer page."""
    doc = get_document(document_id)
    if not doc:
        return redirect(url_for("documents.document_list"))

    doc["_file_size_fmt"] = format_file_size(doc.get("file_size", 0))
    doc["_file_icon"] = get_file_icon(doc.get("file_type", ""))

    if doc.get("file_type", "") in _PREVIEWABLE_TYPES:
        signed_url = get_signed_url(document_id)
    else:
        signed_url = None

    # Folder breadcrumb for context
    dossier_id = doc.get("dossier_id", "")
    folder_breadcrumb = get_folder_breadcrumb(dossier_id, doc.get("folder_id"))

    # Folder tree for the move modal
    folder_tree = get_folder_tree(dossier_id) if dossier_id else []

    # Le journal des analyses (SPEC Phase K §8.2). Échoue OUVERT — c'est
    # un historique d'affichage, jamais une garde.
    analyses = document_model.list_analyses(document_id)

    return render_template(
        "documents/detail.html",
        analyses=analyses,
        document=doc,
        # D25 : la catégorie que l'analyse dérive, quand la catégorie du
        # juriste — gardée — en diffère ; "" sinon (la note ambre).
        categorie_suggeree=document_model.analysis_category_divergence(doc),
        # D25 : sa confirmation, gardée par une analyse enregistrée APRÈS
        # elle, ne couvre pas le passage affiché — la carte ne le présente
        # pas comme confirmé (alertes montrées, « Confirmer » offert).
        confirmation_anterieure=(
            document_model.analysis_confirmation_predates_run(doc)),
        # Le refus de « Confirmer » voyage sur une redirection 2xx
        # (?erreur=) ; la page ne le lisait pas, si bien qu'un refus — y
        # compris « Aucune analyse à confirmer. » — ne paraissait jamais.
        erreur=sanitize(request.args.get("erreur", ""), max_length=300),
        signed_url=signed_url,
        category_labels=CATEGORY_LABELS,
        folder_breadcrumb=folder_breadcrumb,
        folder_tree=folder_tree,
        return_to=request.args.get("return_to", ""),
    )


# ── Download ──────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/download")
@login_required
def document_download(document_id: str) -> str:
    """Redirect to a signed download URL."""
    signed_url = get_signed_url(document_id, download=True)
    if not signed_url:
        return redirect(url_for("documents.document_list"))
    return redirect(signed_url)


@documents_bp.route("/zip")
@login_required
def folder_zip():
    """Compose le zip du dossier de classement courant dans GCS puis
    redirige vers l'URL signée (les octets ne transitent jamais par
    l'application — plafond de 32 Mo par réponse). Sans folder_id :
    tout le dossier."""
    dossier_id = request.args.get("dossier_id", "").strip()
    folder_id = request.args.get("folder_id", "").strip() or None
    if not dossier_id:
        return redirect(url_for("documents.document_list"))
    try:
        uid = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        url, errors = None, [str(exc)]
    else:
        url, errors = build_folder_zip_url(dossier_id, folder_id, uid)
    if not url:
        return redirect(url_for(
            "documents.document_list",
            dossier_id=dossier_id, folder_id=folder_id or "",
            erreur=" ".join(errors) or "Archive impossible.",
        ))
    return redirect(url)


# ── Upload ────────────────────────────────────────────────────────────────


@documents_bp.route("/upload", methods=["GET"])
@login_required
def document_upload_form() -> str:
    """Render the upload form."""
    dossier_id = request.args.get("dossier_id", "").strip()
    folder_id = request.args.get("folder_id", "").strip() or None
    dossier = get_dossier(dossier_id) if dossier_id else None

    # Folder breadcrumb for context
    folder_breadcrumb = []
    if dossier_id and folder_id:
        folder_breadcrumb = get_folder_breadcrumb(dossier_id, folder_id)

    return render_template(
        "documents/upload.html",
        dossier=dossier,
        dossiers=list_dossiers(),
        # CHOICES, not LABELS: this is an INPUT form, and the legacy
        # « procès_verbal » is no longer offered at creation. The edit form
        # and the list filter keep the complete map — see the constant.
        category_choices=CATEGORY_CHOICES,
        folder_id=folder_id,
        folder_breadcrumb=folder_breadcrumb,
        errors=[],
        # return_to est rejoué par le JS dans window.location à la fin du
        # téléversement : validé DÈS LE RENDU — le POST multipart supprimé
        # le passait par safe_internal_redirect, et sa disparition ne doit
        # pas rouvrir la redirection ouverte (revue 2026-08-12).
        return_to=safe_internal_redirect(
            request.args.get("return_to", ""), ""
        ),
    )


def _upload_metadata(donnees: dict) -> dict:
    """The upload form's metadata, as both upload endpoints read it — one
    builder, so the session-open check judges exactly what the
    finalization will write (the folder aside: it needs a read)."""
    tags_raw = str(donnees.get("tags") or "")
    return {
        "category": str(donnees.get("category") or "autre").strip(),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
        "display_name": str(donnees.get("display_name") or "").strip(),
        # The document's OWN date (PV, jugement…) — optional, distinct
        # from the upload instant.
        "document_date": str(donnees.get("document_date") or "").strip(),
    }


@documents_bp.route("/api/televersement", methods=["POST"])
@login_required
def api_televersement():
    """Ouvre une session GCS reprenable pour un téléversement DIRECT.

    Les octets vont du navigateur à GCS sans transiter par l'application —
    App Engine Standard plafonne toute requête à 32 Mo, et le plafond
    documents est à 200 Mo (décision 2026-08-12). L'objet naît sous
    staging/{uid}/ ; api_finaliser le vérifie (sniff des octets) puis
    l'ingère par copie côté serveur et consomme le staging.
    """
    donnees = request.get_json(silent=True) or {}
    nom = str(donnees.get("name") or "")
    try:
        size = int(donnees.get("size"))
    except (TypeError, ValueError):
        size = -1

    ext = "." + nom.rsplit(".", 1)[1].lower() if "." in nom else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"erreur": (
            "Type de fichier non autorisé. Formats acceptés : PDF, "
            "Word (DOC/DOCX), Excel (XLS/XLSX), JPG, PNG, TIFF, ZIP, "
            "courriels (EML/MSG)."
        )}), 422
    if size <= 0 or size > MAX_FILE_SIZE:
        return jsonify({
            "erreur": "Chaque fichier doit faire entre 1 octet et 200 Mo."
        }), 422
    # Les métadonnées du formulaire sont jugées ICI, avant qu'un octet ne
    # parte (lot 2A, revue de T1). Le modèle refuse désormais un nom
    # d'affichage ou des étiquettes que `sanitize` retoucherait ; refusés
    # seulement à la finalisation, ils coûtaient le téléversement entier
    # (jusqu'à 200 Mo), le staging étant consommé sur refus. Une page
    # d'avant ce contrôle n'envoie pas ces clés : leurs défauts passent, et
    # la finalisation, qui refait le même jugement, reste l'autorité.
    erreurs_meta = document_model.record_metadata_errors(
        _upload_metadata(donnees)
    )
    if erreurs_meta:
        return jsonify({"erreur": " ".join(erreurs_meta)}), 422

    # Type déclaré par le navigateur — indicatif seulement (l'ingestion
    # re-sniffe les octets) ; réduit à l'ASCII imprimable.
    ct = "".join(
        c for c in str(donnees.get("content_type") or "")
        if 32 <= ord(c) < 127
    )[:100] or "application/octet-stream"
    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return jsonify({"erreur": str(exc)}), 503
    printable = "".join(ch for ch in nom if ch.isprintable())
    safe = secure_filename(printable) or "document"
    objet = f"staging/{user_id}/{uuid.uuid4()}/{safe}"
    # Origine CORS de la session = l'origine de la PAGE. En production le
    # TLS se termine en amont de gunicorn et il n'y a pas de ProxyFix —
    # request.scheme lirait « http » et le navigateur refuserait les PUT ;
    # https est donc FORCÉ (le motif du portail : origin=f"https://{HOST}").
    scheme = "https" if Config.ENV == "production" else (request.scheme or "http")
    try:
        blob = storage.bucket().blob(objet)
        # size= : GCS refuse tout octet au-delà du déclaré (le vrai plafond,
        # non contournable) ; origin= : la politique CORS vit sur la
        # SESSION reprenable, pas sur le bucket.
        url = blob.create_resumable_upload_session(
            content_type=ct, size=size, origin=f"{scheme}://{request.host}",
        )
    except Exception:
        logger.exception("documents: resumable-session open failed")
        return jsonify({
            "erreur": "Erreur lors de l'ouverture du téléversement. Réessayez."
        }), 503
    return jsonify({"url": url, "objet": objet})


@documents_bp.route("/api/finaliser", methods=["POST"])
@login_required
def api_finaliser():
    """Ingère un objet staging téléversé en direct → document du dossier.

    Le staging est CONSOMMÉ dans les deux issues : copié au chemin
    canonique (réussite) ou supprimé (refus — des octets non conformes
    n'ont rien à faire en staging non plus). Un staging jamais finalisé
    (navigateur fermé en plein transfert) est un orphelin inerte que la
    règle de cycle de vie du bucket balaie (préfixe staging/, 7 jours).
    """
    donnees = request.get_json(silent=True) or {}
    objet = str(donnees.get("objet") or "")
    nom = str(donnees.get("name") or "").strip() or "document"
    dossier_id = str(donnees.get("dossier_id") or "").strip()

    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return jsonify({"erreur": str(exc)}), 503
    if not objet.startswith(f"staging/{user_id}/"):
        # Le client ne nomme jamais que SES objets staging — tout autre
        # chemin est une charge forgée.
        return jsonify({"erreur": "Requête invalide."}), 400
    # staging/{uid}/{uuid4}/{nom} — le segment uuid4, frappé par
    # api_televersement, devient l'identifiant RÉSERVÉ du document : deux
    # finalisations du même téléversement (double clic, requête rejouée
    # dont la réponse s'est perdue) aboutissent à UN document, jamais deux
    # (lot 2A, T1). Tout autre segment — `exports/` des archives zip, une
    # charge forgée — est refusé ici, avant de toucher l'objet.
    segments = objet.split("/")
    if (len(segments) != 4 or not segments[3]
            or not document_model.is_canonical_uuid4(segments[2])):
        return jsonify({"erreur": "Requête invalide."}), 400
    document_id = segments[2]
    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        return jsonify({"erreur": "Veuillez sélectionner un dossier."}), 422

    try:
        blob = storage.bucket().blob(objet)
        blob.reload()
    except NotFound:
        # The same finalization, arriving AGAIN after the first one
        # consumed the staging object (a request replayed by the network).
        # The reserved id says whether the upload landed: answer with its
        # document rather than « introuvable », which would invite the
        # lawyer to upload the file a second time.
        deja = get_document(document_id)
        if deja and deja.get("dossier_id") == dossier_id:
            return jsonify({"ok": True, "document_id": deja["id"]})
        logger.exception("documents: staging blob reload failed")
        return jsonify({
            "erreur": "Fichier téléversé introuvable. Réessayez."
        }), 422
    except Exception:
        logger.exception("documents: staging blob reload failed")
        return jsonify({
            "erreur": "Fichier téléversé introuvable. Réessayez."
        }), 422

    metadata = {
        **_upload_metadata(donnees),
        "folder_id": str(donnees.get("folder_id") or "").strip() or None,
    }
    document, errors = ingest_blob_as_document(
        blob,
        dossier_id,
        dossier.get("file_number", ""),
        nom,
        metadata,
        user_id,
        document_id=document_id,
        # D18: a category the lawyer moved OFF the form's pre-selected
        # default is his choice — the connector never replaces it.
        lawyer_set_category=(
            metadata.get("category") != UPLOAD_DEFAULT_CATEGORY),
    )
    try:
        blob.delete()
    except Exception:
        logger.warning("documents: staging cleanup failed")
    if errors or document is None:
        return jsonify({
            "erreur": " ".join(errors) or "Téléversement impossible."
        }), 422
    return jsonify({"ok": True, "document_id": document["id"]})


# ── Edit metadata ─────────────────────────────────────────────────────────


def _analyse_form_context() -> dict:
    """Les vocabulaires FERMÉS que le formulaire d'analyse propose.

    Ils viennent des modules PURS (`utils/analyse_taxonomies`,
    `utils/analyse_protection`), jamais d'un littéral recopié : le juriste
    choisit exactement dans la table que le code valide, et une entrée
    ajoutée à la table paraît au formulaire sans qu'on y touche.
    """
    from utils import analyse_protection as prot
    from utils import analyse_taxonomies as tax

    sous_natures = sorted(
        (
            {
                "code": code,
                "libelle": entree.libelle,
                "nature": entree.nature,
                "famille": entree.famille,
            }
            for code, entree in tax.SOUS_NATURES.items()
        ),
        key=lambda r: (r["famille"], r["libelle"]),
    )
    return {
        "analyse_sous_natures": sous_natures,
        "analyse_privileges": [
            {"code": code, "libelle": getattr(p, "portee", "") or code,
             "niveau": getattr(p, "niveau", None)}
            for code, p in prot.PRIVILEGES.items()
        ],
        "analyse_niveaux": [
            (n, tax.NIVEAU_LABELS.get(n, str(n))) for n in (0, 1, 2, 3)
        ],
        # Annexe C : deux vocabulaires FERMÉS que le formulaire offrait en
        # texte libre — donc invérifiables, et de toute façon vides parce
        # que l'outil ne pouvait pas les fournir.
        "analyse_moyens_preuve": [
            (code, libelle, ancrage)
            for code, (libelle, ancrage) in tax.MOYENS_PREUVE.items()
        ],
        "analyse_qualifications": [
            (code, libelle, ancrage)
            for code, (libelle, ancrage) in tax.QUALIFICATIONS_ECRIT.items()
        ],
        "analyse_qualites": list(tax.QUALITES_RECONNAISSANCE),
    }


def _analyse_from_form(f) -> dict:
    """Ce que le formulaire porte de l'analyse, champ par champ.

    Tout est TOUJOURS porté quand la section est présente — un champ vidé
    efface, comme la date du document et les notes internes. C'est ce qui
    permet de retirer une mention que l'analyse avait inventée.
    """
    from models.document import (
        _ANALYSE_BOOLEENS, _ANALYSE_LISTES, _ANALYSE_TEXTES,
    )

    champs: dict = {
        "sous_nature": f.get("analyse_sous_nature", "").strip(),
        "privileges": f.getlist("analyse_privileges"),
        "niveau_protection": f.get("analyse_niveau_protection", "").strip(),
    }
    for cle in _ANALYSE_TEXTES:
        champs[cle] = f.get(f"analyse_{cle}", "").strip()
    for cle in _ANALYSE_LISTES:
        champs[cle] = f.get(f"analyse_{cle}", "").strip()
    for cle in _ANALYSE_BOOLEENS:
        champs[cle] = f.get(f"analyse_{cle}") == "1"
    return champs


@documents_bp.route("/<document_id>/edit")
@login_required
def document_edit(document_id: str) -> str:
    """Render the metadata edit form."""
    doc = get_document(document_id)
    if not doc:
        return redirect(url_for("documents.document_list"))

    return render_template(
        "documents/edit.html",
        document=doc,
        category_labels=CATEGORY_LABELS,
        errors=[],
        return_to=request.args.get("return_to", ""),
        # D25 : le résumé d'analyse du formulaire ne tait pas qu'une
        # confirmation gardée précède le passage affiché.
        confirmation_anterieure=(
            document_model.analysis_confirmation_predates_run(doc)),
        **_analyse_form_context(),
    )


def _analyse_for_display(champs: dict) -> dict:
    """The submitted analysis, in the SHAPE the form renders from.

    ``_analyse_from_form`` hands the model what the model parses — the four
    list fields as comma-separated strings, the level as a string. The form
    renders from the stored shape (lists joined, the level compared with an
    int), so a re-render fed the raw submission would print « A, ,, B » and
    lose the selected level. A refused save must show what was typed.
    """
    from models.document import _ANALYSE_LISTES

    shown = dict(champs)
    for cle in _ANALYSE_LISTES:
        brut = shown.get(cle)
        if isinstance(brut, str):
            shown[cle] = [x.strip() for x in brut.split(",") if x.strip()]
    niveau = str(shown.get("niveau_protection") or "").strip()
    shown["niveau_protection"] = int(niveau) if niveau.isdigit() else None
    return shown


def _metadata_for_display(data: dict) -> dict:
    """The submitted metadata, in the shape the form renders from — the
    date input submits a string, and the form calls ``strftime`` on it."""
    from models.document import _coerce_document_date

    return {**data,
            "document_date": _coerce_document_date(data.get("document_date"))}


# The banner's outcome when the metadata write committed and only the
# analysis — the SECOND of the form's two writes — was refused.
_ANALYSE_NOT_SAVED = (
    "Les renseignements de base ont été enregistrés, mais pas l'analyse : "
    "vos valeurs d'analyse sont conservées ci-dessous."
)
# The same fact when the analysis was refused for a FIELD (no banner then:
# the error list says why, and this line says what did land).
_METADATA_SAVED_ANALYSE_REFUSED = (
    "Les renseignements de base ont été enregistrés ; l'analyse ne l'a pas "
    "été, pour la raison ci-dessous — vos valeurs d'analyse sont conservées."
)
_ANALYSE_WITHOUT_SUB_NATURE = (
    "Choisissez une sous-nature pour enregistrer une analyse : les autres "
    "champs d'analyse en dépendent, et ils n'ont pas été enregistrés."
)

# The two analysis fields rendered as a <textarea>; every other text field
# is an <input type="text">, whose value the browser posts WITHOUT line
# breaks (the HTML value sanitization algorithm strips them).
_ANALYSE_TEXTAREAS = ("resume", "dispositif")


def _form_text(key: str, value: object) -> str:
    """*value* as the edit form round-trips it: CRLF folded, a text input's
    line breaks dropped, outer whitespace stripped."""
    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if key not in _ANALYSE_TEXTAREAS:
        text = text.replace("\n", "")
    return text.strip()


def _analyse_changes(stored: dict, champs: dict) -> dict:
    """The submitted analysis fields that DIFFER from the stored analysis.

    The form posts all 25 fields every time; a field the lawyer did not
    touch comes back as the form rendered it. Each is compared in THAT
    shape — a list against its ``", "``-joined rendering (a comma inside an
    item, like the non-downgrade motif, must not read as an edit), the level
    against the stored int, a checkbox against the stored flag — so that
    only a real edit reaches ``update_analyse``, which confirms and journals
    whatever it receives. Pure; the returned values are the SUBMITTED ones,
    for ``update_analyse`` to parse.
    """
    from models.document import _ANALYSE_BOOLEENS, _ANALYSE_LISTES

    changes: dict = {}
    for key, value in champs.items():
        if key == "privileges":
            if set(value or []) != set(stored.get("privileges") or []):
                changes[key] = value
        elif key == "niveau_protection":
            text = str(value or "").strip()
            submitted = int(text) if text.isdigit() else (text or None)
            if submitted != stored.get("niveau_protection"):
                changes[key] = value
        elif key in _ANALYSE_LISTES:
            shown = ", ".join(str(x) for x in (stored.get(key) or []))
            if _form_text(key, value) != _form_text(key, shown):
                changes[key] = value
        elif key in _ANALYSE_BOOLEENS:
            if bool(value) != bool(stored.get(key)):
                changes[key] = value
        elif _form_text(key, value) != _form_text(key, stored.get(key)):
            changes[key] = value
    return changes


@documents_bp.route("/<document_id>/edit", methods=["POST"])
@login_required
def document_update(document_id: str) -> str:
    """Handle metadata edit form submission.

    Two model writes, ONE etag. The form was rendered from one version of
    the document and carries its etag once. ``update_metadata`` commits
    against that etag; ``update_analyse`` then commits against the etag the
    metadata write just produced — never against the form's, which the
    first write has replaced (the second write would refuse every save),
    and never against nothing (a write landing between the two — the
    connector's ``record_document_analysis`` — would be overwritten under
    the lawyer's name, through the one path that can LOWER a protection
    level). A stale form is therefore refused before anything is written;
    the rare race between the two writes is refused at the second, and the
    banner says the first was saved — only when it was: a metadata write
    that changed nothing writes nothing (lot 2A, T1).

    The second write happens only when an analysis value was actually
    edited (``_analyse_changes``), and receives only those values: the
    section posts every field on every save, and ``update_analyse``
    confirms and journals what it gets.
    """
    f = request.form
    tags_raw = f.get("tags", "").strip()
    return_to = f.get("return_to", "")
    expected = edit_conflict.submitted_etag()

    data = {
        "display_name": f.get("display_name", "").strip(),
        "category": f.get("category", "autre").strip(),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else [],
        # Le champ du juriste. Toujours porté, comme la date : un champ vidé
        # l'efface, sans quoi une note ne pourrait jamais être retirée.
        "notes_internes": f.get("notes_internes", "").strip(),
        # Always carried by this form — an emptied input clears the date.
        "document_date": f.get("document_date", "").strip(),
    }
    # L'analyse, si le formulaire la porte. Le drapeau `analyse_presente`
    # distingue « le juriste n'a pas ouvert la section » de « il l'a vidée » :
    # sans lui, tout enregistrement des seules métadonnées de base
    # effacerait l'analyse entière, puisque le contrat de `update_analyse`
    # est qu'une clé présente et vide efface.
    champs = (
        _analyse_from_form(f) if f.get("analyse_presente") == "1" else None
    )

    saved, errors, metadata_written = update_metadata(
        document_id, data, expected_etag=expected
    )
    # The etag the form stands for from here on: the one the metadata
    # write produced, once it committed (None stays None — a page that
    # carried no etag keeps the legacy, unchecked path on both writes).
    form_etag = (
        concurrency.etag_of(saved)
        if saved is not None and expected is not None else expected
    )

    # The analysis is written ONLY when the lawyer changed one of its
    # values (lot 2A, T1). The section always posts every field, and
    # `update_analyse` confirms what it writes (« éditer, c'est
    # confirmer ») and re-derives the category: called on every save, it
    # turned a tag edit into the confirmation of an analysis the lawyer
    # never touched, and — on a document never analysed — refused the empty
    # sub-nature AFTER the metadata was saved.
    analyse_refused = False
    if saved is not None and champs is not None:
        stored = saved.get("analyse") or {}
        changes = _analyse_changes(stored, champs)
        if changes and (stored.get("sous_nature") or "sous_nature" in changes):
            _, erreurs_analyse = update_analyse(
                document_id, changes, par=_signer(),
                expected_etag=form_etag,
            )
            errors = erreurs_analyse or []
        elif changes:
            # Values typed into the analysis of a document never analysed,
            # without the sub-nature everything else derives from.
            errors = [_ANALYSE_WITHOUT_SUB_NATURE]
        analyse_refused = bool(errors)

    if errors:
        errors, conflict, etag = edit_conflict.resolve_refusal(
            errors,
            submitted=form_etag,
            reread=lambda: get_document(document_id),
            compare_url=url_for(
                "documents.document_detail", document_id=document_id
            ),
            # Exactly what was saved: the metadata write committed only
            # when it CHANGED something — a save that changed nothing
            # wrote nothing, and « enregistrés » would be false.
            outcome=(
                _ANALYSE_NOT_SAVED if metadata_written
                else edit_conflict.NOTHING_SAVED
            ),
        )
        if analyse_refused and metadata_written and conflict is None:
            errors = [_METADATA_SAVED_ANALYSE_REFUSED] + errors
        existing = get_document(document_id) or {}
        existing.update(_metadata_for_display(data))
        if champs is not None:
            existing["analyse"] = {
                **(existing.get("analyse") or {}),
                **_analyse_for_display(champs),
            }
        existing["etag"] = etag
        return render_template(
            "documents/edit.html",
            document=existing,
            category_labels=CATEGORY_LABELS,
            errors=errors,
            conflict=conflict,
            return_to=return_to,
            confirmation_anterieure=(
                document_model.analysis_confirmation_predates_run(existing)),
            **_analyse_form_context(),
        )

    fallback = url_for("documents.document_detail", document_id=document_id)
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Move document ─────────────────────────────────────────────────────────


_MOVE_STALE = (
    "Ce document a été modifié depuis l'affichage de la page — reclassé ou "
    "renommé ailleurs, par exemple par le connecteur. Il n'a PAS été "
    "déplacé : vérifiez son dossier de classement ci-dessous, puis "
    "déplacez-le de nouveau."
)


@documents_bp.route("/<document_id>/move", methods=["POST"])
@login_required
def document_move(document_id: str) -> str:
    """Move a document to a different folder.

    The modal carries the version the page showed (plan rule 11, lot 2A):
    since the connector refiles documents (update_document, move_documents),
    a detail page left open must not send one back, unseen, to where it
    was. A stale move is refused on the page's 2xx-bound bounce."""
    doc = get_document(document_id)
    if not doc:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Document introuvable.</div>', 404
        return redirect(url_for("documents.document_list"))

    dossier_id = doc["dossier_id"]
    target_folder_id = request.form.get("target_folder_id", "").strip() or None

    updated_doc, errors, _moved = move_document(
        dossier_id, document_id, target_folder_id,
        expected_etag=edit_conflict.submitted_etag(),
    )
    if concurrency.is_stale(errors):
        errors = [_MOVE_STALE]

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{escape(errors[0])}</div>', 422
        resp = redirect(url_for("documents.document_detail", document_id=document_id))
        resp.headers["HX-Redirect"] = url_for("documents.document_detail", document_id=document_id)
        return resp

    # The move modal is a plain form: its refusal travels on the redirect
    # (the detail page prints ?erreur=). It used to be dropped — a move to
    # a folder deleted meanwhile answered with the page, unchanged, and no
    # word of why.
    return redirect(url_for(
        "documents.document_detail", document_id=document_id,
        **({"erreur": errors[0]} if errors else {}),
    ))


@documents_bp.route("/move-bulk", methods=["POST"])
@login_required
def document_move_bulk() -> str:
    """Move multiple documents to a folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    target_folder_id = request.form.get("target_folder_id", "").strip() or None
    doc_ids = request.form.getlist("document_ids")

    if not dossier_id or not doc_ids:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Paramètres manquants.</div>', 422
        return redirect(url_for("documents.document_list"))

    # The model refuses an id named twice (a batch naming a row twice has no
    # single outcome to report); a form that posts a checkbox twice means it
    # once — deduplicated here, in order.
    rows, errors = move_documents_bulk(
        dossier_id, list(dict.fromkeys(doc_ids)), target_folder_id)
    moved = sum(1 for r in rows if r["outcome"] == "moved")
    errors = errors or [r["reason"] for r in rows if r["outcome"] == "refused"]

    target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=target_folder_id or "")
    if _is_htmx():
        if errors and moved == 0:
            return f'<div class="text-red-600 text-sm">{escape(errors[0])}</div>', 422
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Delete ────────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/delete", methods=["POST"])
@login_required
def document_delete(document_id: str) -> str:
    """Delete a document and redirect (caller-supplied URL or document browser)."""
    doc = get_document(document_id)
    dossier_id = doc.get("dossier_id", "") if doc else ""
    folder_id = doc.get("folder_id") if doc else None
    return_to = request.form.get("return_to", "")

    success, error = delete_document(document_id)

    if success:
        # Append-only deletion trail (PA-G06) — the Storage blob is gone
        # with the delete; the trail records only that it existed.
        record_deletion(
            "document", document_id,
            dossier_id=dossier_id,
            title=(doc or {}).get("display_name", ""),
            status=(doc or {}).get("category", ""),
        )

    if dossier_id:
        fallback = url_for("documents.document_list", dossier_id=dossier_id, folder_id=folder_id or "")
    else:
        fallback = url_for("documents.document_list")
    target = safe_internal_redirect(return_to, fallback)

    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

    return redirect(target)


# ── Folder CRUD routes ───────────────────────────────────────────────────


@documents_bp.route("/folders/create", methods=["POST"])
@login_required
def folder_create() -> str:
    """Create a new folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    name = request.form.get("name", "").strip()
    parent_folder_id = request.form.get("parent_folder_id", "").strip() or None

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    folder, errors = create_folder(dossier_id, name, parent_folder_id)

    # Succès comme échec : 200 + redirection vers le navigateur, qui relit
    # ?erreur= dans sa bannière (_browser.html) — jamais un fragment 4xx,
    # htmx 2.0.4 n'échange que les 2xx (la règle de folder_delete_route
    # ci-dessous). Un « / » dans le nom ou un doublon de nom — les refus
    # ORDINAIRES de create_folder — mourait à l'écran : bouton ✓ mort,
    # l'utilisateur re-clique (audit 2026-08-26, catégorie b).
    target = url_for(
        "documents.document_list", dossier_id=dossier_id,
        folder_id=parent_folder_id or "",
        **({"erreur": errors[0]} if errors else {}),
    )
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


@documents_bp.route("/folders/<folder_id>/rename", methods=["POST"])
@login_required
def folder_rename(folder_id: str) -> str:
    """Rename a folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    new_name = request.form.get("new_name", "").strip()

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    # Parent lu AVANT la mutation : sur un refus, rename_folder rend
    # folder=None et le navigateur doit revenir au MÊME niveau.
    existing = get_folder(dossier_id, folder_id)
    # La version affichée voyage avec le formulaire (lot 2A, T2 — le
    # connecteur pourra renommer ou déplacer un dossier) : un renommage
    # posé sur une page périmée est refusé, et le refus paraît dans la
    # bannière comme les autres.
    folder, errors, _changed = rename_folder(
        dossier_id, folder_id, new_name,
        expected_etag=edit_conflict.submitted_etag(),
    )

    # Même discipline 2xx + ?erreur= que folder_create ci-dessus : renommer
    # vers un nom déjà pris — l'erreur la plus banale — mourait en 422
    # silencieux (audit 2026-08-26, catégorie b).
    parent_id = (folder or existing or {}).get("parent_folder_id")
    target = url_for(
        "documents.document_list", dossier_id=dossier_id,
        folder_id=parent_id or "",
        **({"erreur": errors[0]} if errors else {}),
    )
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


@documents_bp.route("/folders/<folder_id>/move", methods=["POST"])
@login_required
def folder_move(folder_id: str) -> str:
    """Move a folder to a new parent."""
    dossier_id = request.form.get("dossier_id", "").strip()
    new_parent_folder_id = request.form.get("new_parent_folder_id", "").strip() or None

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    folder, errors, _changed = move_folder(
        dossier_id, folder_id, new_parent_folder_id,
        expected_etag=edit_conflict.submitted_etag(),
    )

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{escape(errors[0])}</div>', 422
        target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=new_parent_folder_id or "")
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(url_for("documents.document_list", dossier_id=dossier_id))


@documents_bp.route("/folders/<folder_id>/delete", methods=["POST"])
@login_required
def folder_delete_route(folder_id: str) -> str:
    """Delete a folder — moving its files out, or deleting them with it.

    ``contents`` comes from the confirmation dialog's two distinct forms.
    Anything other than « delete » (a missing field, a stale page, a forged
    post) is treated as « move » by the model — the destructive branch is
    opt-in, never a default.
    """
    dossier_id = request.form.get("dossier_id", "").strip()
    contents = request.form.get("contents", "").strip()

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    # Get parent before deleting
    folder_data = get_folder(dossier_id, folder_id)
    parent_id = folder_data.get("parent_folder_id") if folder_data else None

    # Le décompte que le dialogue a ANNONCÉ (lot 2A, T2), et l'empreinte
    # des éléments annoncés (revue de T2 : un fichier sorti et un autre
    # entré laissent le décompte intact). Le modèle refuse si le sous-arbre
    # ne les porte plus : le connecteur peut désormais déplacer des
    # fichiers et des dossiers, et « Tout supprimer » ne doit jamais
    # détruire ce qui a été glissé dedans depuis l'affichage. Absent
    # = une page rendue avant ce lot : aucune vérification (la règle de
    # routes/edit_conflict). Présent mais illisible = un POST fabriqué :
    # refus, rien n'est touché — en 2xx, la bannière du navigateur.
    expected, malformed = _submitted_subtree_counts()
    if malformed:
        success, error, rapport = False, _COUNTS_MALFORMED, {
            "folders": [], "documents": [], "moved": 0,
        }
    else:
        success, error, rapport = delete_folder(
            dossier_id, folder_id, contents=contents, **expected,
        )

    # ONE deletion event per entity — the house invariant (14 call sites).
    # Until now a single event was minted for the top folder and the
    # sub-folders vanished from the trail entirely.
    #
    # Journalled OUTSIDE the success test on purpose: two of delete_folder's
    # failure returns are NOT atomic and carry what they already destroyed
    # (a blob delete that fails on file 13 of 40 leaves 12 files gone from
    # GCS and from Firestore, irreversibly). Gating on success dropped that
    # report on the floor, so `list_deletions` — whose whole purpose is
    # « qu'est-ce qui a disparu ? » — answered that nothing had. The report
    # only ever lists COMMITTED deletes, so a run that destroyed nothing
    # still journals nothing.
    for doc in rapport.get("documents", []):
        record_deletion(
            "document", doc.get("id", ""),
            dossier_id=dossier_id,
            title=doc.get("display_name", "") or doc.get("filename", ""),
            status=doc.get("category", ""),
        )
    for folder in rapport.get("folders", []):
        record_deletion(
            "folder", folder.get("id", ""),
            dossier_id=dossier_id,
            title=folder.get("name", ""),
            status="contenu supprimé" if rapport.get("documents") else "",
        )

    message = _folder_delete_message(rapport) if success else ""
    if not success and error and _orphan_phrase(rapport):
        # A refusal AFTER the files phase (the folder records' transaction
        # saw a change) still owes what the store could not erase.
        error = f"{error} {_orphan_phrase(rapport)}"

    target = url_for(
        "documents.document_list", dossier_id=dossier_id,
        folder_id=parent_id or "",
        **({"message": message} if message else {"erreur": error} if error else {}),
    )
    if _is_htmx():
        # Succès comme échec : 200 + HX-Redirect vers le navigateur, qui
        # relit ?message= / ?erreur= dans sa bannière. Surtout PAS un
        # fragment 422 — htmx 2.0.4 n'échange que les 2xx (la règle que
        # doc_templates.py:88 documente déjà), si bien qu'un refus (« 342
        # fichiers, au-delà de la limite ») ou un aveu de destruction
        # partielle mourait à l'écran : bouton mort, rien qui bouge, et
        # l'utilisateur re-clique.
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    # La branche sans JS laissait tomber l'erreur en silence — elle rebondit
    # désormais sur le navigateur avec ?erreur=, comme l'archive zip.
    return redirect(target)


_COUNTS_MALFORMED = (
    "Requête invalide : le décompte transmis par le formulaire est "
    "illisible. Rien n'a été supprimé — rechargez la page, puis confirmez "
    "de nouveau."
)


_HEX_DIGITS = frozenset("0123456789abcdef")


def _submitted_subtree_counts() -> tuple[dict, bool]:
    """``({expected_documents?, expected_folders?, expected_fingerprint?},
    malformed)`` from the delete dialog. An absent field is left out (no
    check on it); a present count must be a non-negative integer, a present
    fingerprint the 64 lowercase hex digits of a sha256."""
    expected: dict = {}
    for field in ("expected_documents", "expected_folders"):
        raw = request.form.get(field)
        if raw is None:
            continue
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
            return {}, True
        expected[field] = int(raw)
    raw = request.form.get("expected_fingerprint")
    if raw is not None:
        raw = raw.strip()
        if len(raw) != 64 or not set(raw) <= _HEX_DIGITS:
            return {}, True
        expected["expected_fingerprint"] = raw
    return expected, False


def _folder_delete_message(rapport: dict) -> str:
    """« 4 dossiers et 23 fichiers supprimés » — ce qui a réellement disparu."""
    dossiers = len(rapport.get("folders", []))
    fichiers = len(rapport.get("documents", []))
    deplaces = int(rapport.get("moved", 0))
    parts = [f"{dossiers} dossier{'s' if dossiers != 1 else ''}"]
    if fichiers:
        parts.append(f"{fichiers} fichier{'s' if fichiers != 1 else ''}")
    phrase = " et ".join(parts) + (" supprimés" if fichiers or dossiers != 1 else " supprimé")
    if deplaces:
        phrase += (
            f" — {deplaces} fichier{'s' if deplaces != 1 else ''} "
            f"déplacé{'s' if deplaces != 1 else ''} vers le dossier parent"
        )
    orphelins = _orphan_phrase(rapport)
    if orphelins:
        phrase += ". " + orphelins.rstrip(".")
    return phrase


def _orphan_phrase(rapport: dict) -> str:
    """« Le stockage n'a pas pu effacer N fichier(s)… » — ``""`` sans
    orphelin.

    Correctifs du lot 2A : les ENREGISTREMENTS partent d'abord, dans la
    transaction qui relit le sous-arbre ; un fichier que le stockage a
    refusé d'effacer ensuite n'est plus référencé par rien — dit, pour
    qu'une suppression voulue complète ne se lise pas comme telle. Sur le
    succès comme sur un refus survenu APRÈS la phase des fichiers (revue
    des correctifs : la bannière d'erreur le taisait)."""
    orphelins = int(rapport.get("orphaned_files", 0) or 0)
    if not orphelins:
        return ""
    if orphelins == 1:
        return (
            "Le stockage n'a pas pu effacer 1 fichier : il n'est plus "
            "accessible dans l'application, mais ses octets restent en "
            "stockage."
        )
    return (
        f"Le stockage n'a pas pu effacer {orphelins} fichiers : ils ne sont "
        "plus accessibles dans l'application, mais leurs octets restent en "
        "stockage."
    )


# ── Folder tree API (for move modal) ─────────────────────────────────────


@documents_bp.route("/folder-tree")
@login_required
def folder_tree_partial() -> str:
    """Return folder tree HTML for move modal."""
    dossier_id = request.args.get("dossier_id", "").strip()
    if not dossier_id:
        return ""
    tree = get_folder_tree(dossier_id)
    return render_template(
        "documents/_folder_tree.html",
        folder_tree=tree,
        dossier_id=dossier_id,
    )

_CONFIRM_STALE = (
    "Ce document a été modifié depuis l'affichage de la page — une "
    "nouvelle analyse, par exemple. Rien n'a été confirmé : relisez "
    "l'analyse ci-dessous, puis confirmez de nouveau."
)


@documents_bp.route("/<document_id>/analyse/confirmer", methods=["POST"])
@login_required
def analyse_confirmer(document_id: str):
    """Confirme la classification présumée (SPEC Phase K §7).

    Le SEUL chemin levant `confirme`. Aucun automatisme, jamais : une
    qualification de « public » ou d'« acte authentique » a des
    conséquences, et une supposition de modèle ne doit pas se présenter
    avec l'autorité d'une détermination de l'avocat.
    """
    # La version que la page affichait (plan, règle 11) : confirmer une
    # analyse qu'un autre écrivain — le connecteur qui réanalyse, un autre
    # onglet — a remplacée depuis, ce serait signer ce qu'on n'a pas lu.
    # Absent (page d'avant) → aucun contrôle ; illisible → 400 français.
    expected = edit_conflict.submitted_etag()
    # `session["email"]` — the key auth.py sets. The route read another
    # key, one nothing ever writes, so every confirmation was recorded
    # under an empty name (revue du lot 2, 2026-09-27).
    _, erreurs = document_model.confirmer_analyse(
        document_id, _signer(), expected_etag=expected
    )
    if concurrency.is_stale(erreurs):
        erreurs = [_CONFIRM_STALE]
    # Un refus voyage sur une redirection 2xx : htmx n'échange que les 2xx,
    # et un fragment rendu en 4xx ne paraîtrait jamais.
    return redirect(url_for(
        "documents.document_detail", document_id=document_id,
        **({"erreur": erreurs[0]} if erreurs else {}),
    ))


def _signer() -> str:
    """Who is confirming: the signed-in lawyer's email (``auth.py`` sets
    ``session["email"]``). One helper, so no route reads another key."""
    return str(session.get("email") or "")


_CATEGORY_CONFIRM_STALE = (
    "Ce document a été modifié depuis l'affichage de la page. Rien n'a été "
    "confirmé : relisez sa catégorie ci-dessous, puis confirmez de nouveau."
)


@documents_bp.route("/<document_id>/categorie/confirmer", methods=["POST"])
@login_required
def categorie_confirmer(document_id: str):
    """Confirme une catégorie posée par Claude (D15, lot 2A).

    Une catégorie que le connecteur a posée HORS analyse est PRÉSUMÉE
    (``category_source == "mcp"``) : elle paraît « présumée » partout
    jusqu'à ce que le juriste la confirme ici — ou la change au formulaire,
    ce qui en fait aussi sa détermination. Le bouton porte l'etag de la
    version affichée (confirmer, c'est dire « j'ai vu CETTE version ») ; un
    refus voyage sur une redirection vers la fiche, qui l'affiche.
    """
    expected = edit_conflict.submitted_etag()
    _, erreurs = document_model.confirmer_categorie(
        document_id, _signer(), expected_etag=expected
    )
    if concurrency.is_stale(erreurs):
        erreurs = [_CATEGORY_CONFIRM_STALE]
    return redirect(url_for(
        "documents.document_detail", document_id=document_id,
        **({"erreur": erreurs[0]} if erreurs else {}),
    ))
