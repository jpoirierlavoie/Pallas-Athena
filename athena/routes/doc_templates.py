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
from datetime import datetime
from typing import Optional

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
from werkzeug.utils import secure_filename

from auth import login_required
from config import Config
from models.doc_template import (
    CATEGORY_LABELS,
    DOCX_MIME,
    VALID_CATEGORIES,
    create_template,
    delete_template,
    get_signed_url,
    get_template,
    get_template_bytes,
    list_templates,
    update_template,
)
from models.document import upload_document
from models.dossier import get_dossier, list_dossiers
from models.partie import ROLE_LABELS as PARTIE_ROLE_LABELS
from models.partie import display_name, get_partie, list_parties
from security import safe_internal_redirect
from tz import MTL
from utils.docx_fill import DocxFillError, fill_docx
from utils.logging_setup import log_template_event, log_unexpected
from utils.template_fields import (
    MANUAL_FIELDS,
    fallback_value,
    resolve_values,
    salutations_default,
)
from utils.tracing_setup import add_attributes, span
from utils.validators import format_phone_display

doc_templates_bp = Blueprint("doc_templates", __name__, url_prefix="/gabarits")

_SCALAR_MAX_CHARS = 2000
_BLOCK_MAX_CHARS = 50000
_FIELD_PREFIX = "champ__"


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


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
    }


def _firm_dict() -> dict:
    street = Config.FIRM_STREET
    if Config.FIRM_UNIT:
        street = f"{street}, {Config.FIRM_UNIT}" if street else Config.FIRM_UNIT
    telephone = ""
    if Config.FIRM_PHONE:
        try:
            telephone = format_phone_display(Config.FIRM_PHONE)
        except Exception:
            telephone = Config.FIRM_PHONE
    return {
        "nom": Config.FIRM_NAME,
        "adresse_civique": street,
        "ville": Config.FIRM_CITY,
        "province": Config.FIRM_PROVINCE,
        "code_postal": Config.FIRM_POSTAL_CODE,
        "telephone": telephone,
        "courriel": Config.FIRM_EMAIL,
    }


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

    template, errors = create_template(
        file_stream=file,
        filename=file.filename,
        file_size=file_size,
        metadata=metadata,
        user_id=session.get("user_id", "unknown"),
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


@doc_templates_bp.route("/<template_id>")
@login_required
def template_detail(template_id: str) -> Response | str:
    template = get_template(template_id)
    if not template:
        return redirect(url_for("doc_templates.template_list"))
    ctx = _template_context()
    ctx.update(template=template)
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

    template, errors = update_template(
        template_id,
        data,
        file_stream=file_stream,
        filename=filename,
        file_size=file_size,
    )
    if errors:
        ctx = _template_context()
        merged = {**existing, **data}
        ctx.update(template=merged, errors=errors, edit_mode=True)
        return render_template("gabarits/form.html", **ctx)

    log_template_event(
        "template_updated",
        template_id=template_id,
        file_replaced=file_stream is not None,
        version=template.get("version", 1),
    )
    target = url_for("doc_templates.template_detail", template_id=template_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@doc_templates_bp.route("/<template_id>/delete", methods=["POST"])
@login_required
def template_delete(template_id: str) -> Response | str:
    success, error = delete_template(template_id)
    if success:
        log_template_event("template_deleted", template_id=template_id)
        target = url_for("doc_templates.template_list")
        if _is_htmx():
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return redirect(target)
    # Failure: keep the user on the template (it still exists) rather than
    # silently redirecting to the list as if the delete had worked. The
    # detail page's delete is a plain POST form → the non-HTMX branch.
    if _is_htmx():
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>'  # 200 — htmx swaps
    return redirect(url_for("doc_templates.template_detail", template_id=template_id))


@doc_templates_bp.route("/<template_id>/download")
@login_required
def template_download(template_id: str) -> Response | str:
    url = get_signed_url(template_id)
    if not url:
        return redirect(url_for("doc_templates.template_detail", template_id=template_id))
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
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return '<div class="px-3 py-2 text-sm text-gray-500">Tapez au moins 2 caractères…</div>'
    dossiers = list_dossiers(search=q)[:10]
    if not dossiers:
        return '<div class="px-3 py-2 text-sm text-gray-500">Aucun dossier trouvé</div>'

    parts = [
        '<ul class="border border-gray-200 rounded-lg overflow-hidden '
        'divide-y divide-gray-100 bg-white max-h-48 overflow-y-auto">'
    ]
    for d in dossiers:
        url = escape(_modal_reload_url(d["id"]))
        file_number = escape(d.get("file_number", ""))
        title = escape(d.get("title", ""))
        parts.append(
            f'<li><button type="button"'
            f' class="w-full text-left px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' hx-get="{url}" hx-target="#gabarit-modal" hx-swap="innerHTML"'
            f' hx-include=".gabarit-ctx,.gabarit-slot">'
            f'  <span class="font-medium text-gray-900">{file_number}</span>'
            f'  <span class="text-gray-500 ml-1">{title}</span>'
            f'</button></li>'
        )
    parts.append("</ul>")
    return "\n".join(parts)


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


def _first_id(entries: Optional[list]) -> str:
    if entries:
        return entries[0].get("id", "") or ""
    return ""


def _fields_context(template: dict, destinataire_prefill: str = "") -> dict:
    """Build the field-form context: slots, resolved values, defaults."""
    dossier_id = _arg("dossier_id")
    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        dossier_id = ""

    client_id = _arg("client_id")
    adverse_id = _arg("adverse_id")
    destinataire_id = _arg("destinataire_id") or destinataire_prefill

    clients = (dossier or {}).get("clients", [])
    opposing = (dossier or {}).get("opposing_parties", [])
    if dossier:
        valid_client_ids = {c.get("id") for c in clients}
        if client_id not in valid_client_ids:
            client_id = _first_id(clients)
        valid_adverse_ids = {p.get("id") for p in opposing}
        if adverse_id not in valid_adverse_ids:
            adverse_id = _first_id(opposing)
    else:
        # No dossier: the client slot falls back to a free partie pick
        # (spec §5); the adverse slot has no fallback.
        adverse_id = ""

    client = get_partie(client_id) if client_id else None
    if client is None:
        client_id = ""
    adverse = get_partie(adverse_id) if adverse_id else None
    destinataire = get_partie(destinataire_id) if destinataire_id else None
    if destinataire is None:
        destinataire_id = ""

    today = datetime.now(MTL).date()
    placeholders = template.get("placeholders", [])
    resolved = resolve_values(
        placeholders,
        dossier=dossier,
        client=client,
        adverse=adverse,
        destinataire=destinataire,
        firm=_firm_dict(),
        today=today,
    )

    civilite = resolve_values(
        ["civilité"],
        dossier=None, client=None, adverse=None,
        destinataire=destinataire, firm={}, today=today,
    ).get("civilité")

    auto_set = set(template.get("auto_fields", []))
    block_set = set(template.get("block_fields", []))
    fields = []
    for name in placeholders:
        if name in block_set:
            kind = "block"
        elif name in auto_set:
            kind = "auto"
        else:
            kind = "manual"
        value = resolved.get(name, "")
        options = None
        if kind == "manual" and name in MANUAL_FIELDS:
            spec = MANUAL_FIELDS[name]
            options = spec["options"]
            if not value:
                if name == "salutations":
                    value = salutations_default(civilite)
                else:
                    value = spec["default"] or ""
        fields.append({"name": name, "kind": kind, "value": value, "options": options})

    return {
        "template": template,
        "dossier": dossier,
        "dossier_id": dossier_id,
        "clients": clients,
        "opposing_parties": opposing,
        "client_id": client_id,
        "adverse_id": adverse_id,
        "client": client,
        "client_display": display_name(client) if client else "",
        "destinataire": destinataire,
        "destinataire_id": destinataire_id,
        "destinataire_display": display_name(destinataire) if destinataire else "",
        "partie_role_labels": PARTIE_ROLE_LABELS,
        "slots_required": template.get("slots_required", []),
        "fields": fields,
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


def _collect_values(template: dict) -> tuple[dict[str, str], int]:
    """Pull one value per placeholder from the form; blanks become the
    visible French fallback strings (§6.7). Returns (values, missing)."""
    auto_set = set(template.get("auto_fields", []))
    block_set = set(template.get("block_fields", []))
    values: dict[str, str] = {}
    missing = 0
    for name in template.get("placeholders", []):
        raw = request.form.get(f"{_FIELD_PREFIX}{name}", "")
        cap = _BLOCK_MAX_CHARS if name in block_set else _SCALAR_MAX_CHARS
        value = raw[:cap].strip()
        if not value:
            value = fallback_value(name, is_auto=name in auto_set)
            missing += 1
        values[name] = value
    return values, missing


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
    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier_id and dossier is None:
        # A dossier was selected at render time but no longer resolves —
        # never fall through to the download branch (an HTMX submit would
        # swap raw .docx bytes into the page).
        log_template_event(
            "generation_failed", template_id=template_id,
            reason="dossier_not_found",
        )
        if _is_htmx():
            return _champs_error(
                "Le dossier sélectionné est introuvable. Fermez la fenêtre et réessayez."
            )
        return redirect(url_for("doc_templates.template_detail", template_id=template_id))

    values, missing = _collect_values(template)
    add_attributes(template_id=template_id, field_count=len(values))

    docx_bytes = get_template_bytes(template_id)
    if docx_bytes is None:
        log_template_event(
            "generation_failed", template_id=template_id,
            reason="template_file_unavailable",
        )
        if _is_htmx():
            return _champs_error(
                "Le fichier du gabarit est introuvable. Téléversez-le à nouveau."
            )
        return redirect(url_for("doc_templates.template_detail", template_id=template_id))

    try:
        with span("template.fill", template_id=template_id, field_count=len(values)):
            filled = fill_docx(docx_bytes, values)
    except DocxFillError:
        log_template_event(
            "generation_failed", template_id=template_id, reason="template_invalid"
        )
        if _is_htmx():
            return _champs_error("Le gabarit est invalide et n'a pas pu être rempli.")
        return redirect(url_for("doc_templates.template_detail", template_id=template_id))
    except Exception:
        log_unexpected("template fill failed", template_id=template_id)
        log_template_event(
            "generation_failed", template_id=template_id, reason="fill_error"
        )
        if _is_htmx():
            return _champs_error("Erreur lors de la génération. Veuillez réessayer.")
        return redirect(url_for("doc_templates.template_detail", template_id=template_id))

    today = datetime.now(MTL).date()
    reference = (dossier or {}).get("file_number") or template.get("name", "gabarit")
    out_name = secure_filename(f"{reference}_{today.isoformat()}.docx")
    if not out_name.lower().endswith(".docx"):
        out_name = f"document_{today.isoformat()}.docx"

    if dossier:
        metadata = {
            "category": template.get("category", "autre"),
            "display_name": f"{template.get('name', 'Gabarit')} — {today.isoformat()}",
            "description": (
                f"Généré depuis le gabarit «{template.get('name', '')}» "
                f"v{template.get('version', 1)}"
            ),
            "tags": ["gabarit"],
        }
        doc, errors = upload_document(
            dossier_id=dossier_id,
            dossier_file_number=dossier.get("file_number", ""),
            file_stream=io.BytesIO(filled),
            filename=out_name,
            file_size=len(filled),
            metadata=metadata,
            user_id=session.get("user_id", "unknown"),
        )
        if errors:
            log_template_event(
                "generation_failed", template_id=template_id,
                dossier_id=dossier_id, reason="save_failed",
            )
            if _is_htmx():
                return _champs_error(errors[0])
            return redirect(url_for("doc_templates.template_detail", template_id=template_id))

        log_template_event(
            "document_generated",
            template_id=template_id,
            dossier_id=dossier_id,
            saved_document_id=doc["id"],
            field_count=len(values),
            missing_count=missing,
        )
        if not _is_htmx():
            # No-JS fallback: the saved document's page, not a fragment.
            return redirect(url_for("documents.document_detail", document_id=doc["id"]))
        ctx = _fields_context(template)
        ctx["generated"] = {
            "document_id": doc["id"],
            "display_name": doc.get("display_name", out_name),
            "detail_url": url_for("documents.document_detail", document_id=doc["id"]),
            "download_url": url_for("documents.document_download", document_id=doc["id"]),
        }
        return render_template("gabarits/_generate_fields.html", **ctx)

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
