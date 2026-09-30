"""Routes for document templates ("gabarits") and docx generation — Phase H.

Lifecycle (list / upload / edit / replace / delete / download) plus the
two-step HTMX generation popup:

* ``GET /gabarits/generer`` — modal shell: template select (or fixed),
  dossier picker (locked when launched from a dossier page), and — once a
  template is known — the field form.
* ``GET /gabarits/generer/champs`` — the field form partial, re-rendered
  whenever a slot selection changes (server owns the selection state; the
  ``set_*`` query params carry a NEW selection from a clicked search
  result, plain params carry the current state via ``hx-include``).
* ``POST /gabarits/generer`` — fill and either save into the dossier's
  documents (HTMX partial response) or stream the .docx download.

Never log field values (client PII) — placeholder names, counts and IDs
only (§11 of the Phase H spec).
"""

import io

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import escape

from auth import login_required
from models import concurrency
from models.audit_event import record_deletion
from models.doc_template import (
    ACTIVE_KIND_NAMES,
    CATEGORY_LABELS,
    DOCX_MIME,
    KIND_LABELS,
    SPECIAL_KINDS,
    VALID_CATEGORIES,
    VALID_KINDS,
    TemplateReadError,
    clear_active_template,
    create_template,
    delete_template,
    get_active_template,
    get_signed_url,
    get_template,
    get_template_bytes,
    get_version_signed_url,
    is_active,
    list_templates,
    list_versions,
    restore_template_version,
    set_active_template,
    update_template,
)
from models.dossier import get_dossier
from models.partie import ROLE_LABELS as PARTIE_ROLE_LABELS
from models.partie import display_name, list_parties
from services import gabarits
from utils import storage_identity
from utils.deadlines import today_mtl
from utils.logging_setup import log_template_event
from utils.template_fields import classify_placeholders
from utils.tracing_setup import add_attributes
from routes import edit_conflict
from routes._helpers import dossier_search_fragment, is_htmx
from security import sanitize

doc_templates_bp = Blueprint("doc_templates", __name__, url_prefix="/gabarits")

# The generation logic (slots, values and their ceilings, fill, save into
# « Projets ») lives in services/gabarits.py since lot 2A, step T4 — one
# assembly for this popup and for the connector. What stays here is the
# request adapter.


_is_htmx = is_htmx


def _champs_error(message: str) -> str:
    """Error fragment that keeps the #gabarit-champs swap zone valid.

    Returned with status 200: htmx 2.0.4's default responseHandling only
    swaps 2xx responses, so a 422 fragment would silently never render."""
    return (
        '<div id="gabarit-champs">'
        f'<div class="text-red-600 text-sm p-3">{escape(message)}</div>'
        "</div>"
    )


def _template_context() -> dict:
    return {
        "valid_categories": VALID_CATEGORIES,
        "category_labels": CATEGORY_LABELS,
        "kind_labels": KIND_LABELS,
        "special_kinds": SPECIAL_KINDS,
    }


def _signer() -> str:
    """Who acts: the signed-in lawyer's email (``auth.py`` sets
    ``session["email"]``) — the same key the document routes read."""
    return str(session.get("email") or "")


def _detail_redirect(template_id: str, *, message: str = "",
                     erreur: str = "") -> Response:
    """Back to the template page with a banner. A refusal travels on a 2xx
    page (the redirect target): htmx only swaps 2xx, and every form here is
    a plain POST anyway."""
    params = {}
    if message:
        params["message"] = message
    if erreur:
        params["erreur"] = erreur
    return redirect(url_for(
        "doc_templates.template_detail", template_id=template_id, **params))


# The page a stale button was on no longer shows the template as it is.
_ACTIVATE_STALE = (
    "Ce gabarit a été modifié depuis l'affichage de la page. Rien n'a été "
    "désigné : relisez-le ci-dessous, puis désignez-le de nouveau."
)
_WITHDRAW_STALE = (
    "Ce gabarit a été modifié depuis l'affichage de la page. Sa désignation "
    "n'a PAS été retirée : relisez-le ci-dessous, puis recommencez."
)
_RESTORE_STALE = (
    "Ce gabarit a été modifié depuis l'affichage de la page. Rien n'a été "
    "rétabli : relisez ses versions ci-dessous, puis recommencez."
)


def _kind_from_form(form) -> str:
    """Template kind from the upload/edit form.

    Form control: a ``kind`` radio (gabarit | note_honoraires | note).
    Legacy fallback: the pre-radio ``is_note_honoraires`` checkbox, so a
    stale cached form still maps correctly. An unknown radio value falls
    through the legacy path to « gabarit ».
    """
    kind = (form.get("kind") or "").strip()
    if kind in VALID_KINDS:
        return kind
    return "note_honoraires" if form.get("is_note_honoraires") else "gabarit"


# ── Lifecycle ───────────────────────────────────────────────────────────

@doc_templates_bp.route("/")
@login_required
def template_list() -> str:
    category = request.args.get("category", "").strip() or None
    search = request.args.get("q", "").strip() or None
    templates = list_templates(category=category, search=search)
    ctx = _template_context()
    ctx.update(
        templates=templates,
        category_filter=category or "",
        search_query=search or "",
    )
    if _is_htmx():
        return render_template("gabarits/_template_rows.html", **ctx)
    return render_template("gabarits/list.html", **ctx)


@doc_templates_bp.route("/new")
@login_required
def template_new() -> str:
    ctx = _template_context()
    ctx.update(template=None, errors=[])
    return render_template("gabarits/form.html", **ctx)


@doc_templates_bp.route("/", methods=["POST"])
@login_required
def template_create() -> Response | str:
    metadata = {
        "name": request.form.get("name", "").strip(),
        "description": request.form.get("description", "").strip(),
        "category": request.form.get("category", "autre").strip(),
        "kind": _kind_from_form(request.form),
    }
    file = request.files.get("file")
    if not file or not file.filename:
        ctx = _template_context()
        ctx.update(
            template=metadata, errors=["Veuillez sélectionner un fichier .docx."]
        )
        return render_template("gabarits/form.html", **ctx)

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        template, errors = None, [storage_identity.public_message(exc)]
    else:
        template, errors = create_template(
            file_stream=file,
            filename=file.filename,
            file_size=file_size,
            metadata=metadata,
            user_id=user_id,
        )
    if errors:
        ctx = _template_context()
        ctx.update(template=metadata, errors=errors)
        return render_template("gabarits/form.html", **ctx)

    log_template_event(
        "template_uploaded",
        template_id=template["id"],
        placeholder_count=len(template.get("placeholders", [])),
        warning_count=len(template.get("validation_warnings", [])),
    )
    # The detail page IS the upload result: full field inventory +
    # split-run warnings, verifiable before first use.
    target = url_for("doc_templates.template_detail", template_id=template["id"])
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


def _designation_context(template: dict) -> dict:
    """What the page says about the « actif » designation (D11).

    ``designated`` — this template is the one its kind uses; otherwise
    ``designated_other`` is the template the kind uses now (``None`` when
    none is designated) and ``designation_unreadable`` says the store
    could not tell — never shown as « aucun n'est désigné ».
    """
    kind = template.get("kind") or "gabarit"
    ctx = {
        "special": kind in SPECIAL_KINDS,
        "designated": is_active(template),
        "designated_other": None,
        "designation_unreadable": False,
        "active_kind_name": ACTIVE_KIND_NAMES.get(kind, ""),
    }
    if ctx["special"] and not ctx["designated"]:
        try:
            ctx["designated_other"] = get_active_template(kind)
        except TemplateReadError:
            ctx["designation_unreadable"] = True
    return ctx


@doc_templates_bp.route("/<template_id>")
@login_required
def template_detail(template_id: str) -> Response | str:
    template = get_template(template_id)
    if not template:
        return redirect(url_for("doc_templates.template_list"))
    # Re-classify for display so the inventory is correct even for templates
    # uploaded before this taxonomy (stored *_fields lists may be stale).
    placeholders = template.get("placeholders", [])
    classification = classify_placeholders(placeholders)
    ctx = _template_context()
    ctx.update(
        template=template,
        auto_fields=[n for n in placeholders if n in classification.auto],
        manual_fields=classification.manual,
        passthrough_fields=classification.passthrough,
        versions=list_versions(template_id),
        current_version=int(template.get("version") or 1),
        erreur=sanitize(request.args.get("erreur", ""), max_length=300),
        message=sanitize(request.args.get("message", ""), max_length=300),
        **_designation_context(template),
    )
    return render_template("gabarits/detail.html", **ctx)


@doc_templates_bp.route("/<template_id>/edit")
@login_required
def template_edit(template_id: str) -> Response | str:
    template = get_template(template_id)
    if not template:
        return redirect(url_for("doc_templates.template_list"))
    ctx = _template_context()
    ctx.update(template=template, errors=[], edit_mode=True)
    return render_template("gabarits/form.html", **ctx)


@doc_templates_bp.route("/<template_id>", methods=["POST"])
@login_required
def template_update(template_id: str) -> Response | str:
    existing = get_template(template_id)
    if not existing:
        return redirect(url_for("doc_templates.template_list"))

    data = {
        "name": request.form.get("name", "").strip(),
        "description": request.form.get("description", "").strip(),
        "category": request.form.get("category", "autre").strip(),
        "kind": _kind_from_form(request.form),
    }
    file = request.files.get("file")
    file_stream = None
    filename = None
    file_size = None
    if file and file.filename:
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        file_stream = file
        filename = file.filename

    # The version the form was rendered from (plan rule 11, D9): a save
    # over a template another writer changed since is refused, never a
    # silent overwrite. Absent (a page older than the field) → no check.
    submitted = edit_conflict.submitted_etag()
    template, errors, changed = update_template(
        template_id,
        data,
        file_stream=file_stream,
        filename=filename,
        file_size=file_size,
        expected_etag=submitted,
    )
    if errors:
        errors, conflict, etag = edit_conflict.resolve_refusal(
            errors,
            submitted=submitted,
            reread=lambda: get_template(template_id),
            compare_url=url_for(
                "doc_templates.template_detail", template_id=template_id),
            # A browser can never re-fill a file input: say so, rather than
            # let « conservés ci-dessous » cover a file that is not.
            note=("Le fichier choisi n'a pas été conservé : sélectionnez-le "
                  "de nouveau." if file_stream is not None else ""),
        )
        if conflict:
            log_template_event("template_edit_conflict", template_id=template_id)
        ctx = _template_context()
        merged = {**existing, **data, "etag": etag}
        ctx.update(template=merged, errors=errors, conflict=conflict,
                   edit_mode=True)
        return render_template("gabarits/form.html", **ctx)

    version_before = int(existing.get("version") or 1)
    version_after = int(template.get("version") or 1)
    file_replaced = file_stream is not None and version_after != version_before
    if changed:
        log_template_event(
            "template_updated",
            template_id=template_id,
            file_replaced=file_replaced,
            version=version_after,
        )
    params = {}
    if file_stream is not None and not file_replaced:
        # The same bytes as the file in force: no new version (lot 2A, T3).
        # Said, so the lawyer does not look for a version that was not made.
        params["message"] = (
            "Le fichier envoyé est identique au fichier en vigueur : aucune "
            "nouvelle version n'a été créée."
        )
    target = url_for("doc_templates.template_detail", template_id=template_id,
                     **params)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@doc_templates_bp.route("/<template_id>/delete", methods=["POST"])
@login_required
def template_delete(template_id: str) -> Response | str:
    existing = get_template(template_id)
    success, error = delete_template(template_id)
    if success:
        log_template_event("template_deleted", template_id=template_id)
        record_deletion(
            "doc_template", template_id,
            title=(existing or {}).get("name", ""),
            status=(existing or {}).get("category", ""),
        )
        target = url_for("doc_templates.template_list")
        if _is_htmx():
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return redirect(target)
    # Failure: keep the user on the template (it still exists) rather than
    # silently redirecting to the list as if the delete had worked. The
    # detail page's delete is a plain POST form → the non-HTMX branch,
    # which now CARRIES the refusal (T3 review): it used to redirect to the
    # page with no word at all, so a failed delete read as a dialog that
    # had merely closed.
    if _is_htmx():
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>'  # 200 — htmx swaps
    return _detail_redirect(template_id, erreur=error)


# No signed URL could be minted: the file is gone, OR the signing call
# (IAM signBlob) failed. The two are indistinguishable here, so the message
# names both rather than assert « introuvable » on a transient failure.
_DOWNLOAD_FAILED = (
    "Le fichier n'a pas pu être téléchargé : il est introuvable, ou le lien "
    "de téléchargement n'a pas pu être créé. Réessayez dans un instant."
)


@doc_templates_bp.route("/<template_id>/download")
@login_required
def template_download(template_id: str) -> Response | str:
    url = get_signed_url(template_id)
    if not url:
        # Said on the page (T3 review) — it used to bounce back silently.
        return _detail_redirect(template_id, erreur=_DOWNLOAD_FAILED)
    return redirect(url)


@doc_templates_bp.route("/<template_id>/activer", methods=["POST"])
@login_required
def template_activate(template_id: str) -> Response:
    """Désigne ce gabarit comme gabarit ACTIF de son type (D11).

    Le SEUL chemin web vers ``set_active_template`` : seul le juriste
    choisit la note d'honoraires et l'impression de note qu'impriment ses
    documents — jamais le connecteur, jamais la récence. Le bouton porte
    l'etag de la version affichée ; un refus voyage sur une redirection
    vers la fiche, qui l'affiche.
    """
    expected = edit_conflict.submitted_etag()
    template, errors = set_active_template(
        template_id, par=_signer(), expected_etag=expected
    )
    if errors:
        if concurrency.is_stale(errors):
            # A double tap (a slow phone) sends the SAME etag twice: the
            # first designates, the second is stale. Its answer must not
            # tell the lawyer to designate again a template the page now
            # shows « Actif » with no button (T3 review) — when the template
            # IS the designation now, say that, and that nothing changed.
            current = get_template(template_id)
            if is_active(current):
                return _detail_redirect(template_id, message=(
                    "Ce gabarit est déjà le gabarit actif des « "
                    f"{ACTIVE_KIND_NAMES.get(current.get('kind'), '')} » : "
                    "rien n'a été modifié."
                ))
            errors = [_ACTIVATE_STALE]
        return _detail_redirect(template_id, erreur=errors[0])
    kind = template.get("kind", "")
    log_template_event("template_activated", template_id=template_id, kind=kind)
    return _detail_redirect(template_id, message=(
        f"Ce gabarit est désormais le gabarit actif des "
        f"« {ACTIVE_KIND_NAMES.get(kind, kind)} »."
    ))


@doc_templates_bp.route("/<template_id>/retirer-designation", methods=["POST"])
@login_required
def template_deactivate(template_id: str) -> Response:
    """Retire la désignation « actif » de ce gabarit (D11, correctifs du
    lot 2A).

    Le geste inverse de « Désigner », et comme lui le SEUL chemin vers
    ``clear_active_template`` : le connecteur ne l'atteint jamais. Avant lui,
    un gabarit actif ne cessait de s'imprimer qu'en en désignant un autre ou
    en le supprimant. Après lui, AUCUN gabarit du type n'est désigné et la
    génération de ce type est refusée — la bannière le dit. Le bouton porte
    l'etag de la version affichée ; un refus voyage sur une redirection vers
    la fiche.
    """
    expected = edit_conflict.submitted_etag()
    template, errors = clear_active_template(template_id, expected_etag=expected)
    if errors:
        if concurrency.is_stale(errors):
            # A double tap: the first withdrew, the second is stale. When
            # the template no longer IS the designation, say that nothing
            # changed rather than « recommencez ».
            current = get_template(template_id)
            if current and not is_active(current):
                return _detail_redirect(template_id, message=(
                    "Ce gabarit n'est déjà plus le gabarit actif de son "
                    "type : rien n'a été modifié."
                ))
            errors = [_WITHDRAW_STALE]
        return _detail_redirect(template_id, erreur=errors[0])
    kind = template.get("kind", "")
    log_template_event("template_deactivated", template_id=template_id,
                       kind=kind)
    name = ACTIVE_KIND_NAMES.get(kind, kind)
    return _detail_redirect(template_id, message=(
        f"Ce gabarit n'est plus le gabarit actif des « {name} ». Aucun "
        f"gabarit « {name} » n'est désigné : cette génération est refusée "
        "jusqu'à ce que vous en désigniez un autre."
    ))


@doc_templates_bp.route(
    "/<template_id>/versions/<int:version>/retablir", methods=["POST"]
)
@login_required
def template_version_restore(template_id: str, version: int) -> Response:
    """Rétablit une version antérieure du fichier — comme une NOUVELLE
    version : l'historique ne fait que croître, rien n'est réécrit (D11)."""
    expected = edit_conflict.submitted_etag()
    template, errors, changed = restore_template_version(
        template_id, version, par=_signer(), expected_etag=expected
    )
    if errors:
        if concurrency.is_stale(errors):
            errors = [_RESTORE_STALE]
        return _detail_redirect(template_id, erreur=errors[0])
    if not changed:
        return _detail_redirect(template_id, message=(
            f"La version {version} est identique au fichier en vigueur : "
            "rien n'a été rétabli."
        ))
    new_version = int(template.get("version") or 1)
    log_template_event(
        "template_version_restored", template_id=template_id,
        restored_from=version, version=new_version,
    )
    return _detail_redirect(template_id, message=(
        f"La version {version} a été rétablie : elle est maintenant la "
        f"version {new_version} du gabarit."
    ))


@doc_templates_bp.route("/<template_id>/versions/<int:version>/download")
@login_required
def template_version_download(template_id: str, version: int) -> Response:
    url = get_version_signed_url(template_id, version)
    if not url:
        return _detail_redirect(template_id, erreur=_DOWNLOAD_FAILED)
    return redirect(url)


# ── Search endpoints (HTMX autocomplete) ────────────────────────────────

def _modal_reload_url(dossier_id: str) -> str:
    """URL a dossier result row loads: the whole modal, with the new
    dossier applied (set_dossier_id) and the rest of the context carried
    by hx-include of the .gabarit-ctx inputs."""
    return url_for("doc_templates.generate_modal", set_dossier_id=dossier_id)


@doc_templates_bp.route("/dossier-search")
@login_required
def dossier_search() -> str:
    """Autocomplete for the generation popup: rows re-render the modal."""

    def _row(d: dict) -> str:
        url = escape(_modal_reload_url(d["id"]))
        file_number = escape(d.get("file_number", ""))
        title = escape(d.get("title", ""))
        return (
            f'<li><button type="button"'
            f' class="w-full text-left px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' hx-get="{url}" hx-target="#gabarit-modal" hx-swap="innerHTML"'
            f' hx-include=".gabarit-ctx,.gabarit-slot">'
            f'  <span class="font-medium text-gray-900">{file_number}</span>'
            f'  <span class="text-gray-500 ml-1">{title}</span>'
            f'</button></li>'
        )

    return dossier_search_fragment(
        request.args.get("q", ""),
        _row,
        list_class=(
            "border border-gray-200 rounded-lg overflow-hidden "
            "divide-y divide-gray-100 bg-white max-h-48 overflow-y-auto"
        ),
    )


@doc_templates_bp.route("/partie-search")
@login_required
def partie_search() -> str:
    """Autocomplete for a partie slot: rows re-render the field form.

    ``?slot=`` selects which slot the clicked result fills (destinataire
    by default; client is the spec §5 no-dossier fallback)."""
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip() or None
    slot = request.args.get("slot", "destinataire").strip()
    if slot not in ("client", "adverse", "destinataire"):
        slot = "destinataire"
    if len(q) < 2:
        return '<div class="px-3 py-2 text-sm text-gray-500">Tapez au moins 2 caractères…</div>'
    parties = list_parties(role_filter=role, search=q)[:10]
    if not parties:
        return '<div class="px-3 py-2 text-sm text-gray-500">Aucun contact trouvé</div>'

    champs_url = url_for("doc_templates.generate_fields")
    parts = [
        '<ul class="border border-gray-200 rounded-lg overflow-hidden '
        'divide-y divide-gray-100 bg-white max-h-48 overflow-y-auto">'
    ]
    for p in parties:
        url = escape(f"{champs_url}?set_{slot}_id={p['id']}")
        name = escape(display_name(p))
        role_label = escape(PARTIE_ROLE_LABELS.get(p.get("contact_role", ""),
                                                   p.get("contact_role", "")))
        parts.append(
            f'<li><button type="button"'
            f' class="w-full text-left px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' hx-get="{url}" hx-target="#gabarit-champs" hx-swap="outerHTML"'
            f' hx-include=".gabarit-ctx,.gabarit-slot">'
            f'  <span class="font-medium text-gray-900">{name}</span>'
            f'  <span class="text-gray-500 ml-1">{role_label}</span>'
            f'</button></li>'
        )
    parts.append("</ul>")
    return "\n".join(parts)


# ── Generation popup ────────────────────────────────────────────────────

def _arg(name: str) -> str:
    """Read a selection param: a fresh set_<name> (from a clicked search
    result) wins over the current <name> carried by hx-include."""
    return (
        request.args.get(f"set_{name}", "").strip()
        or request.args.get(name, "").strip()
    )


def _fields_context(template: dict, destinataire_prefill: str = "") -> dict:
    """Build the field-form context: slots, resolved values, defaults.

    The popup RENDER resolves its slots LENIENTLY (``services.gabarits``):
    the dossier picker carries the previous dossier's selection by
    hx-include, and a selection that is not on the dossier is reset to its
    first party — shown to the lawyer here, before anything is generated.
    The generation POST resolves the same slots strictly and refuses."""
    slots = gabarits.resolve_slots(
        _arg("dossier_id"),
        _arg("client_id"),
        _arg("adverse_id"),
        _arg("destinataire_id") or destinataire_prefill,
        lenient=True,
    )
    resolved = gabarits.resolve_auto_values(template, slots)
    classification = classify_placeholders(template.get("placeholders", []))
    return {
        "template": template,
        "dossier": slots.dossier,
        "dossier_id": slots.dossier_id,
        "clients": slots.clients,
        "opposing_parties": slots.opposing_parties,
        "client_id": slots.client_id,
        "adverse_id": slots.adverse_id,
        "client": slots.client,
        "client_display": display_name(slots.client) if slots.client else "",
        "destinataire": slots.destinataire,
        "destinataire_id": slots.destinataire_id,
        "destinataire_display": (
            display_name(slots.destinataire) if slots.destinataire else ""),
        "partie_role_labels": PARTIE_ROLE_LABELS,
        "slots_required": sorted(classification.slots_required),
        # Only auto + manual fields become form inputs; passthrough
        # placeholders (blocks, civilité, salutations, unknowns) are left in
        # the .docx for the user to complete in Word, surfaced as a note.
        "fields": gabarits.form_fields(template, resolved),
        "passthrough_fields": classification.passthrough,
        "generated": None,
    }


@doc_templates_bp.route("/generer")
@login_required
def generate_modal() -> str:
    """Popup step 1 — modal shell (HTMX partial)."""
    template_id = _arg("template_id")
    template = get_template(template_id) if template_id else None
    template_fixed = request.args.get("fixed", "") == "1" and template is not None

    # Prefill from the entry points.
    partie_id = request.args.get("partie_id", "").strip()
    locked = request.args.get("locked", "") == "1" and bool(_arg("dossier_id"))

    # The dossier context must survive template (re)selection — resolve
    # it here too, not only inside the fields context, so the ctx hidden
    # input keeps carrying it while no template is chosen yet.
    dossier_id = _arg("dossier_id")
    dossier = get_dossier(dossier_id) if dossier_id else None

    ctx = {
        "templates": list_templates() if not template_fixed else [],
        "template": template,
        "template_fixed": template_fixed,
        "locked": locked,
        "partie_id": partie_id,
        "dossier": dossier,
        "dossier_id": dossier_id if dossier else "",
        "slots_required": [],
        "fields": None,
    }
    if template:
        # A partie prefill lands in the destinataire slot (spec §9).
        ctx.update(_fields_context(template, destinataire_prefill=partie_id))
    return render_template("gabarits/_generate_modal.html", **ctx)


@doc_templates_bp.route("/generer/champs")
@login_required
def generate_fields() -> str:
    """Popup step 2 — the field form partial (re-rendered on slot change)."""
    template = get_template(_arg("template_id"))
    if not template:
        return _champs_error("Gabarit introuvable.")
    ctx = _fields_context(template)
    return render_template("gabarits/_generate_fields.html", **ctx)


_SLOT_REASONS = frozenset({"slot_foreign", "slot_unknown", "slot_ambiguous"})


def _generation_refused(template_id: str, exc: gabarits.GenerationRefused,
                        *, dossier_id: str = "") -> Response | str:
    """Say a refused generation where the lawyer will read it.

    htmx 2.0.4 only swaps 2xx, so the popup gets a 200 fragment (a 4xx
    would never render and the button would look dead). The no-JS direct
    download is a plain POST: it lands on the template's page with the
    refusal in its banner — until lot 2A T4 it landed there with no word."""
    fields = {"template_id": template_id, "reason": exc.reason}
    if dossier_id:
        fields["dossier_id"] = dossier_id
    if exc.reason in _SLOT_REASONS and exc.field.endswith("_id"):
        fields["slot"] = exc.field[: -len("_id")]   # client|adverse|destinataire
    log_template_event("generation_failed", **fields)
    if _is_htmx():
        return _champs_error(exc.message)
    return _detail_redirect(template_id, erreur=exc.message)


@doc_templates_bp.route("/generer", methods=["POST"])
@login_required
def generate() -> Response | str:
    template_id = request.form.get("template_id", "").strip()
    template = get_template(template_id)
    if not template:
        log_template_event(
            "generation_failed", template_id=template_id or None,
            reason="template_not_found",
        )
        if _is_htmx():
            return _champs_error("Gabarit introuvable.")
        return redirect(url_for("doc_templates.template_list"))

    dossier_id = request.form.get("dossier_id", "").strip()
    try:
        # STRICT: a selection that is not on the dossier (a contact removed
        # since the popup rendered, a crafted form) is refused — never
        # swapped for the dossier's first party. A dossier that no longer
        # resolves is refused too: an HTMX submit must never fall through to
        # the download branch (it would swap raw .docx bytes into the page).
        slots = gabarits.resolve_slots(
            dossier_id,
            request.form.get("client_id", ""),
            request.form.get("adverse_id", ""),
            request.form.get("destinataire_id", ""),
        )
        # The server's own auto values set each auto field's real ceiling;
        # the values FILLED are still the submitted ones — the lawyer may
        # have edited a prefilled value in the popup.
        resolved = gabarits.resolve_auto_values(template, slots)
        submitted = {
            name: request.form.get(f"{gabarits.FIELD_PREFIX}{name}", "")
            for name in template.get("placeholders", [])
        }
        values, missing = gabarits.values_from_submission(
            template, submitted, resolved=resolved)
    except gabarits.GenerationRefused as exc:
        return _generation_refused(template_id, exc)
    dossier = slots.dossier
    add_attributes(template_id=template_id, field_count=len(values))

    docx_bytes = get_template_bytes(template_id)
    if docx_bytes is None:
        return _generation_refused(template_id, gabarits.GenerationRefused(
            "template_file_unavailable", gabarits.TEMPLATE_FILE_UNAVAILABLE))
    try:
        filled, _demoted = gabarits.fill(
            docx_bytes, values, template_id=template_id)
    except gabarits.GenerationRefused as exc:
        return _generation_refused(template_id, exc)

    today = today_mtl()
    display, out_name = gabarits.output_names(template, dossier, today)

    if dossier:
        try:
            # The uid first: nothing is written — not even « Projets » —
            # for a request that could not file the document anyway.
            try:
                user_id = storage_identity.request_uid()
            except storage_identity.StorageIdentityUnavailable as exc:
                raise gabarits.GenerationRefused("save_failed", storage_identity.public_message(exc)) from exc
            doc = gabarits.save_into_projets(
                template=template, dossier=dossier, filled=filled,
                uid=user_id, today=today,
            )
        except gabarits.GenerationRefused as exc:
            return _generation_refused(template_id, exc,
                                       dossier_id=slots.dossier_id)

        log_template_event(
            "document_generated",
            template_id=template_id,
            dossier_id=slots.dossier_id,
            saved_document_id=doc["id"],
            field_count=len(values),
            missing_count=missing,
        )
        if not _is_htmx():
            # No-JS fallback: the saved document's page, not a fragment.
            return redirect(url_for("documents.document_detail", document_id=doc["id"]))
        return render_template("gabarits/_generate_fields.html", generated={
            "document_id": doc["id"],
            "display_name": doc.get("display_name", out_name),
            "detail_url": url_for("documents.document_detail", document_id=doc["id"]),
            "download_url": url_for("documents.document_download", document_id=doc["id"]),
        })

    # No dossier: direct download. The form posts full-page in this state
    # (target=_blank) — an HTMX submit landing here would swap raw .docx
    # bytes into the modal, so refuse it explicitly.
    if _is_htmx():
        return _champs_error(
            "Aucun dossier sélectionné — utilisez le téléchargement direct."
        )
    log_template_event(
        "document_generated",
        template_id=template_id,
        field_count=len(values),
        missing_count=missing,
    )
    return send_file(
        io.BytesIO(filled),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=out_name,
    )
