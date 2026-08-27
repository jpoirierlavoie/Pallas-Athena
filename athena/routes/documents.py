"""Document management routes — upload, list, detail, edit, delete, download.

Includes folder management routes for hierarchical document organization.
"""

import logging
import uuid

from firebase_admin import storage
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
    ingest_blob_as_document,
    list_documents,
    move_document,
    move_documents_bulk,
    update_metadata,
    update_analyse,
    ANALYSE_EDITABLE,
)
from routes._helpers import is_htmx

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
    « tout supprimer » (see the template's guard on ``_subtree_documents``).
    """
    from models.folder import subtree_index

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
    url, errors = build_folder_zip_url(
        dossier_id, folder_id, session.get("user_id", "unknown")
    )
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

    # Type déclaré par le navigateur — indicatif seulement (l'ingestion
    # re-sniffe les octets) ; réduit à l'ASCII imprimable.
    ct = "".join(
        c for c in str(donnees.get("content_type") or "")
        if 32 <= ord(c) < 127
    )[:100] or "application/octet-stream"
    user_id = session.get("user_id", "unknown")
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

    user_id = session.get("user_id", "unknown")
    if not objet.startswith(f"staging/{user_id}/"):
        # Le client ne nomme jamais que SES objets staging — tout autre
        # chemin est une charge forgée.
        return jsonify({"erreur": "Requête invalide."}), 400
    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        return jsonify({"erreur": "Veuillez sélectionner un dossier."}), 422

    try:
        blob = storage.bucket().blob(objet)
        blob.reload()
    except Exception:
        logger.exception("documents: staging blob reload failed")
        return jsonify({
            "erreur": "Fichier téléversé introuvable. Réessayez."
        }), 422

    tags_raw = str(donnees.get("tags") or "")
    metadata = {
        "category": str(donnees.get("category") or "autre").strip(),
        "description": str(donnees.get("description") or "").strip(),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
        "display_name": str(donnees.get("display_name") or "").strip(),
        "folder_id": str(donnees.get("folder_id") or "").strip() or None,
        # The document's OWN date (PV, jugement…) — optional, distinct
        # from the upload instant.
        "document_date": str(donnees.get("document_date") or "").strip(),
    }
    document, errors = ingest_blob_as_document(
        blob,
        dossier_id,
        dossier.get("file_number", ""),
        nom,
        metadata,
        user_id,
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
        **_analyse_form_context(),
    )


@documents_bp.route("/<document_id>/edit", methods=["POST"])
@login_required
def document_update(document_id: str) -> str:
    """Handle metadata edit form submission."""
    f = request.form
    tags_raw = f.get("tags", "").strip()
    return_to = f.get("return_to", "")

    data = {
        "display_name": f.get("display_name", "").strip(),
        "category": f.get("category", "autre").strip(),
        "description": f.get("description", "").strip(),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else [],
        # Le champ du juriste. Toujours porté, comme la date : un champ vidé
        # l'efface, sans quoi une note ne pourrait jamais être retirée.
        "notes_internes": f.get("notes_internes", "").strip(),
        # Always carried by this form — an emptied input clears the date.
        "document_date": f.get("document_date", "").strip(),
    }

    doc, errors = update_metadata(document_id, data)

    # L'analyse, si le formulaire la porte. Le drapeau `analyse_presente`
    # distingue « le juriste n'a pas ouvert la section » de « il l'a vidée » :
    # sans lui, tout enregistrement des seules métadonnées de base
    # effacerait l'analyse entière, puisque le contrat de `update_analyse`
    # est qu'une clé présente et vide efface.
    if not errors and f.get("analyse_presente") == "1":
        champs = _analyse_from_form(f)
        _, erreurs_analyse = update_analyse(
            document_id, champs, par=session.get("user_email", "")
        )
        errors = erreurs_analyse or []

    if errors:
        existing = get_document(document_id) or {}
        existing.update(data)
        return render_template(
            "documents/edit.html",
            document=existing,
            category_labels=CATEGORY_LABELS,
            errors=errors,
            return_to=return_to,
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


@documents_bp.route("/<document_id>/move", methods=["POST"])
@login_required
def document_move(document_id: str) -> str:
    """Move a document to a different folder."""
    doc = get_document(document_id)
    if not doc:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Document introuvable.</div>', 404
        return redirect(url_for("documents.document_list"))

    dossier_id = doc["dossier_id"]
    target_folder_id = request.form.get("target_folder_id", "").strip() or None

    updated_doc, errors = move_document(dossier_id, document_id, target_folder_id)

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{escape(errors[0])}</div>', 422
        resp = redirect(url_for("documents.document_detail", document_id=document_id))
        resp.headers["HX-Redirect"] = url_for("documents.document_detail", document_id=document_id)
        return resp

    return redirect(url_for("documents.document_detail", document_id=document_id))


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

    moved, errors = move_documents_bulk(dossier_id, doc_ids, target_folder_id)

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
    folder, errors = rename_folder(dossier_id, folder_id, new_name)

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

    folder, errors = move_folder(dossier_id, folder_id, new_parent_folder_id)

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

    success, error, rapport = delete_folder(
        dossier_id, folder_id, contents=contents,
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
    return phrase


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

@documents_bp.route("/<document_id>/analyse/confirmer", methods=["POST"])
@login_required
def analyse_confirmer(document_id: str):
    """Confirme la classification présumée (SPEC Phase K §7).

    Le SEUL chemin levant `confirme`. Aucun automatisme, jamais : une
    qualification de « public » ou d'« acte authentique » a des
    conséquences, et une supposition de modèle ne doit pas se présenter
    avec l'autorité d'une détermination de l'avocat.
    """
    _, erreurs = document_model.confirmer_analyse(
        document_id, session.get("user_email") or ""
    )
    # Un refus voyage sur une redirection 2xx : htmx n'échange que les 2xx,
    # et un fragment rendu en 4xx ne paraîtrait jamais.
    return redirect(url_for(
        "documents.document_detail", document_id=document_id,
        **({"erreur": erreurs[0]} if erreurs else {}),
    ))
