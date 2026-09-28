"""Partie (contact/party) management routes — list, detail, create, edit, delete."""

import json
from datetime import datetime
from typing import Any, Optional

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from utils.cabinet import cabinet_dict
from dav.sync import bump_ctag, record_tombstone
from models import concurrency
from models.audit_event import record_deletion
from tz import to_mtl
from utils import kyc
from utils.format_fr import format_date_fr
from utils.logging_setup import log_partie_event
from utils.template_fields import selected_address, selected_email
from pagination import (
    PAGE_SIZE,
    cursor_pagination,
    paginate,
    parse_trail,
    resolve_page,
    total_pages_of,
)
from security import sanitize
from models.partie import (
    MANDATAIRE_KIND_LABELS,
    PARTIE_NOT_FOUND,
    ROLE_LABELS,
    VALID_CONTACT_ROLES,
    count_parties_page,
    confirm_kyc_status,
    create_partie,
    delete_partie,
    display_name,
    get_partie,
    list_parties,
    list_parties_page,
    update_partie,
)
from models.dossier import (
    STATUS_LABELS as DOSSIER_STATUS_LABELS,
    DOMAINE_LABELS as DOSSIER_DOMAINE_LABELS,
    list_dossiers_for_partie,
)
from routes import edit_conflict
from routes._helpers import is_htmx

parties_bp = Blueprint(
    "parties", __name__, url_prefix="/parties"
)


_is_htmx = is_htmx


def _form_data() -> dict:
    """Extract partie fields from the submitted form."""
    f = request.form
    return {
        "type": f.get("type", "individual"),
        "contact_role": f.get("contact_role", "client"),
        # Individual
        "prefix": f.get("prefix", ""),
        "first_name": f.get("first_name", "").strip(),
        "last_name": f.get("last_name", "").strip(),
        # <input type="date"> → « AAAA-MM-JJ » ; le modèle la fixe à minuit
        # UTC, et une chaîne vide efface la date (_validate la remet à None).
        "birth_date": f.get("birth_date", "").strip(),
        # Organization (personne morale)
        "organization_name": f.get("organization_name", "").strip(),
        "trade_name": f.get("trade_name", "").strip(),
        "governing_law": f.get("governing_law", "").strip(),
        # Demographics
        "language": f.get("language", ""),
        "gender": f.get("gender", ""),
        "pronouns": f.get("pronouns", ""),
        # Professional coordinates
        "job_title": f.get("job_title", "").strip(),
        "job_role": f.get("job_role", "").strip(),
        "organization": f.get("organization", "").strip(),
        # Personal contact
        "email": f.get("email", "").strip(),
        "phone_home": f.get("phone_home", "").strip(),
        "phone_cell": f.get("phone_cell", "").strip(),
        # Professional contact
        "email_work": f.get("email_work", "").strip(),
        "phone_work": f.get("phone_work", "").strip(),
        "fax": f.get("fax", "").strip(),
        # Personal address
        "address_street": f.get("address_street", "").strip(),
        "address_unit": f.get("address_unit", "").strip(),
        "address_city": f.get("address_city", "").strip(),
        "address_province": f.get("address_province", "Québec").strip(),
        "address_postal_code": f.get("address_postal_code", "").strip().upper(),
        "address_country": f.get("address_country", "Canada").strip(),
        # Work address
        "work_address_street": f.get("work_address_street", "").strip(),
        "work_address_unit": f.get("work_address_unit", "").strip(),
        "work_address_city": f.get("work_address_city", "").strip(),
        "work_address_province": f.get("work_address_province", "").strip(),
        "work_address_postal_code": f.get("work_address_postal_code", "").strip().upper(),
        "work_address_country": f.get("work_address_country", "Canada").strip(),
        # Legal identifiers
        "bar_number": f.get("bar_number", "").strip(),
        "company_neq": f.get("company_neq", "").strip(),
        # Compliance (only when contact_role == client)
        "identity_verified": f.get("identity_verified", "non_vérifié"),
        "identity_verified_notes": f.get("identity_verified_notes", "").strip(),
        "conflict_check": f.get("conflict_check", "non_vérifié"),
        "conflict_check_notes": f.get("conflict_check_notes", "").strip(),
        # Mandataires (parsed from a JSON-encoded list submitted by the form)
        "mandataires": _parse_mandataires_json(f.get("mandataires_json", "")),
        # Notes
        "notes": f.get("notes", "").strip(),
    }


def _parse_mandataires_json(raw: str) -> list[dict[str, Any]]:
    """Parse the form's `mandataires_json` field into a list of dicts.

    Returns [] on any parse failure or if the value isn't a list.
    Per-entry sanitation (dedup, type coercion) happens in the model's
    `_normalize`; here we just surface what the form sent.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def _kyc_presumed(partie: Optional[dict]) -> dict[str, bool]:
    """``{field: presumed}`` from the STORED record — the edit form's note
    beside each select (a re-rendered form holds the submitted values, which
    carry no provenance, so the caller passes the stored record)."""
    return {field: kyc.is_presumed(partie, field) for field in kyc.FIELDS}


def _hydrate_mandataires(entries: Optional[list]) -> list[dict[str, Any]]:
    """Resolve each `{id, kind, notes}` entry to include the partie's display name.

    Used to enrich the data passed to detail/form templates so they can render
    each linked partie's name without doing the lookup themselves.
    """
    out: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        target = get_partie(mid)
        out.append({
            "id": mid,
            "name": display_name(target) if target else "(introuvable)",
            "kind": str(entry.get("kind") or "mandataire"),
            "notes": str(entry.get("notes") or ""),
            "found": target is not None,
        })
    return out


# ── List ──────────────────────────────────────────────────────────────────


@parties_bp.route("/")
@login_required
def partie_list() -> str:
    """Render the partie list with optional filters."""
    role_filter = request.args.get("role", "client")
    search = request.args.get("q", "").strip()

    if search:
        # Search fallback: Firestore has no full-text search, so searching
        # still streams the (role-filtered) collection, filters in Python
        # and slices via the legacy paginate(). Search is occasional; the
        # default browse path below costs ~PAGE_SIZE reads per page.
        page = request.args.get("page", 1, type=int)
        parties = list_parties(
            role_filter=role_filter if role_filter != "tous" else None,
            search=search,
        )
        parties, pagination = paginate(parties, page)
        pagination["url"] = url_for("parties.partie_list")
        pagination["target"] = "#partie-rows"
    else:
        cursor = request.args.get("cursor", "") or None
        trail = parse_trail(request.args.get("trail", ""))
        effective_role = role_filter if role_filter != "tous" else None
        # The count is issued on the CURSOR branch only. It rides the same
        # query builder as the page read, so the same index serves both,
        # and it fails to None (never 0) — which HIDES the leap controls
        # rather than asserting a total the code does not have.
        total = count_parties_page(role_filter=effective_role)
        page_no, page_offset = resolve_page(
            request.args.get("page", type=int),
            total_pages_of(total),
            has_cursor=bool(cursor),
        )
        if page_offset:
            cursor, trail = None, []
        parties, next_cursor = list_parties_page(
            role_filter=effective_role,
            limit=PAGE_SIZE,
            cursor=cursor,
            offset=page_offset,
        )
        # No extra_vals needed: the pagination links hx-include
        # "#filters input, #filters select", which carries the active
        # role tab (and the empty q) on every next/prev click.
        pagination = cursor_pagination(
            cursor=cursor,
            trail=trail,
            next_cursor=next_cursor,
            url=url_for("parties.partie_list"),
            target="#partie-rows",
            page=page_no,
            total=total,
        )

    # Attach display names (page rows only) and the identity dot: a client
    # whose identity is not DECIDED — a presumed Claude inscription counts
    # as open (D7), which the template alone could not know.
    for p in parties:
        p["_display_name"] = display_name(p)
        p["_kyc_open"] = (
            p.get("contact_role") == "client"
            and not kyc.is_decided(p, kyc.FIELD_IDENTITY)
        )
        p["_kyc_presumed"] = kyc.is_presumed(p, kyc.FIELD_IDENTITY)

    if _is_htmx():
        return render_template(
            "parties/_partie_rows.html",
            parties=parties,
            role_labels=ROLE_LABELS,
            pagination=pagination,
            role_filter=role_filter,
            search=search,
        )

    return render_template(
        "parties/list.html",
        parties=parties,
        role_filter=role_filter,
        search=search,
        role_labels=ROLE_LABELS,
        valid_roles=VALID_CONTACT_ROLES,
        pagination=pagination,
    )


# ── Search (HTMX autocomplete for other modules) ─────────────────────────


@parties_bp.route("/search")
@login_required
def partie_search() -> str:
    """Return a small HTML fragment of matching parties (for autocomplete).

    ``?boost=<contact_role>`` reorders (never filters) the results so that
    role comes first — the dossier form's avocat picker boosts
    ``avocat_adverse`` while still letting any contact be chosen (a lawyer
    filed under « notaire » or « autre » stays reachable).
    """
    q = request.args.get("q", "").strip()
    boost = request.args.get("boost", "").strip()
    results = list_parties(search=q) if q else []
    if boost:
        # sort() is stable: within each half the search order is preserved.
        results.sort(key=lambda p: p.get("contact_role") != boost)
    for p in results:
        p["_display_name"] = display_name(p)
    return render_template("parties/_search_results.html", parties=results[:10])


@parties_bp.route("/mandataire-search")
@login_required
def mandataire_search() -> str:
    """Search for a mandataire candidate.

    Restricted to personnes physiques (type=individual) of the same
    contact_role as the partie being represented. The current partie is
    excluded from results (no self-reference).
    """
    q = request.args.get("mandataire_query", "").strip() or request.args.get("q", "").strip()
    role = request.args.get("contact_role", "").strip()
    exclude_raw = request.args.get("exclude", "") or ""
    exclude_ids = {eid.strip() for eid in exclude_raw.split(",") if eid.strip()}

    if role not in VALID_CONTACT_ROLES:
        return render_template(
            "parties/_mandataire_search_results.html",
            parties=[],
            message="Sélectionnez d'abord un rôle.",
        )

    results = list_parties(
        type_filter="individual",
        role_filter=role,
        search=q or None,
    )
    if exclude_ids:
        results = [p for p in results if p.get("id") not in exclude_ids]
    for p in results:
        p["_display_name"] = display_name(p)
    return render_template(
        "parties/_mandataire_search_results.html",
        parties=results[:10],
    )


# ── Detail ────────────────────────────────────────────────────────────────


def _compliance_signer() -> str:
    """« Me X » — le nom du juriste, sans dupliquer une civilité présente."""
    nom = (cabinet_dict().get("nom") or "").strip()
    if not nom or nom.lower().startswith("me "):
        return nom
    return f"Me {nom}"


# The two checks, by their URL slug (the « Confirmer » route) and field.
_KYC_CHECKS = (("identite", kyc.FIELD_IDENTITY),
               ("conflit", kyc.FIELD_CONFLICT))
_KYC_FIELD_BY_SLUG = dict(_KYC_CHECKS)
_KYC_BADGES = {
    "non_vérifié": "bg-orange-100 text-orange-700",
    "vérifié": "bg-green-100 text-green-700",
    "exempté": "bg-blue-100 text-blue-700",
    "conflit_détecté": "bg-red-100 text-red-700",
}
# A presumed status is NOT decided: amber, never the green of a verified
# identity — at a glance the fiche must not read as verified (D7).
_KYC_PRESUMED_BADGE = "bg-amber-100 text-amber-700"


def _kyc_day(value) -> str:
    """« 25 septembre 2026 » in Montréal — ``""`` when there is no date
    (a contact created with a decided status before lot 4a has none)."""
    local = to_mtl(value) if value else None
    return format_date_fr(local.date()) if local else ""


def _kyc_view(partie: dict) -> dict:
    """Per check: the badge, its label and the ATTRIBUTION line — composed
    here, so the signer's name is never attached to a Claude inscription.

    * presumed (inscribed by Claude, not confirmed): amber « … (présumé) »,
      « inscrit par Claude le … — à confirmer », and the « Confirmer »
      button;
    * Claude's, confirmed: « confirmé le … par Me … (inscrit par Claude le
      …) »;
    * the lawyer's: « le … par Me … » (the text before lot 4a).
    A missing date drops the date, never raises.
    """
    signer = _compliance_signer()
    by = f" par {signer}" if signer else ""
    view: dict = {}
    for slug, field in _KYC_CHECKS:
        status = kyc.stored_status(partie, field)
        presumed = kyc.is_presumed(partie, field)
        label = kyc.STATUS_LABELS.get(status, status)
        badge = _KYC_BADGES.get(status, "bg-gray-100 text-gray-600")
        if presumed:
            label = f"{label} (présumé)"
            badge = _KYC_PRESUMED_BADGE
        inscribed = _kyc_day(partie.get(kyc.date_key(field)))
        attribution = ""
        if status in kyc.DECIDED[field]:
            if presumed:
                attribution = (
                    f"inscrit par Claude le {inscribed} — à confirmer"
                    if inscribed else "inscrit par Claude — à confirmer"
                )
            elif kyc.source_of(partie, field) == kyc.SOURCE_MCP:
                confirmed = _kyc_day(partie.get(kyc.confirmed_at_key(field)))
                attribution = (
                    f"confirmé le {confirmed}{by}" if confirmed
                    else f"confirmé{by}"
                )
                if inscribed:
                    attribution += f" (inscrit par Claude le {inscribed})"
            elif inscribed:
                attribution = f"le {inscribed}{by}"
        view[slug] = {
            "slug": slug,
            "field": field,
            "status": status,
            "label": label,
            "badge": badge,
            "presumed": presumed,
            "attribution": attribution,
            "notes": str(partie.get(kyc.notes_key(field)) or ""),
        }
    return view


@parties_bp.route("/<partie_id>")
@login_required
def partie_detail(partie_id: str) -> str:
    """Render the partie detail page."""
    partie = get_partie(partie_id)
    if not partie:
        return render_template(
            "parties/list.html",
            parties=[],
            role_filter="",
            search="",
            role_labels=ROLE_LABELS,
            valid_roles=VALID_CONTACT_ROLES,
            error="Contact introuvable.",
        )

    partie["_display_name"] = display_name(partie)
    dossiers = list_dossiers_for_partie(partie_id)
    mandataires = _hydrate_mandataires(partie.get("mandataires"))
    return render_template(
        "parties/detail.html",
        partie=partie,
        # Conformité is shown for a contact whose role is « client » — and
        # for any contact that is a CLIENT of a dossier whatever its role:
        # the coverage report checks every dossier client, so the fiche
        # must show what it checks.
        is_dossier_client=any(
            partie_id in (d.get("client_ids") or []) for d in dossiers
        ),
        kyc_view=_kyc_view(partie),
        # Signataire des vérifications de conformité. Le gabarit lisait
        # `config.FIRM_NAME` directement ; la valeur vit maintenant dans
        # settings/cabinet, et le libellé se compose ici (« le gabarit ne
        # possède que le libellé, jamais une transformation »).
        #
        # Il lit le nom du JURISTE, jamais celui du cabinet : une attestation
        # d'identité est signée par une personne. La mention est DÉRIVÉE À LA
        # LECTURE, sans instantané au dossier — renommer le champ
        # réattribuerait donc toutes les attestations passées, ce qui est
        # précisément pourquoi il ne doit jamais pointer sur `organisation`.
        # Bandeau d'arrivée : Réception redirige ICI après avoir créé ou mis à
        # jour une fiche depuis une ouverture du portail, et doit pouvoir dire
        # ce qui vient de se passer (« vérifiez et complétez », combien de
        # champs appliqués, combien de contacts adverses créés). Sans lui, le
        # message était construit puis silencieusement perdu.
        message=sanitize(request.args.get("message", ""), max_length=300),
        # Bandeau d'erreur : un « Supprimer » REFUSÉ revient ici avec la
        # raison (contact lié à un dossier, mandataire d'un autre contact).
        # 600 et non 300 : la raison nomme jusqu'à trois contacts.
        erreur=sanitize(request.args.get("erreur", ""), max_length=600),
        role_labels=ROLE_LABELS,
        mandataires=mandataires,
        mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
        dossiers=dossiers,
        dossier_status_labels=DOSSIER_STATUS_LABELS,
        dossier_domaine_labels=DOSSIER_DOMAINE_LABELS,
    )


# ── Create ────────────────────────────────────────────────────────────────


@parties_bp.route("/new")
@login_required
def partie_new() -> str:
    """Render the empty partie form."""
    return render_template(
        "parties/form.html",
        partie=None,
        errors=[],
        role_labels=ROLE_LABELS,
        mandataires=[],
        mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
    )


@parties_bp.route("/", methods=["POST"])
@login_required
def partie_create() -> str:
    """Handle new partie form submission."""
    data = _form_data()
    # The lawyer's form: a decided status set here is HIS attestation (D7).
    partie, errors = create_partie(data, kyc_source=kyc.SOURCE_JURISTE)

    if errors:
        return render_template(
            "parties/form.html",
            partie=data,
            errors=errors,
            role_labels=ROLE_LABELS,
            mandataires=_hydrate_mandataires(data.get("mandataires")),
            mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
            kyc_presumed=_kyc_presumed(None),
        )

    bump_ctag("parties")

    if _is_htmx():
        resp = redirect(url_for("parties.partie_detail", partie_id=partie["id"]))
        resp.headers["HX-Redirect"] = url_for(
            "parties.partie_detail", partie_id=partie["id"]
        )
        return resp

    return redirect(url_for("parties.partie_detail", partie_id=partie["id"]))


# ── Edit ──────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>/edit")
@login_required
def partie_edit(partie_id: str) -> str:
    """Render the edit form pre-filled with partie data."""
    partie = get_partie(partie_id)
    if not partie:
        return redirect(url_for("parties.partie_list"))

    return render_template(
        "parties/form.html",
        partie=partie,
        errors=[],
        role_labels=ROLE_LABELS,
        mandataires=_hydrate_mandataires(partie.get("mandataires")),
        mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
        kyc_presumed=_kyc_presumed(partie),
    )


@parties_bp.route("/<partie_id>", methods=["POST"])
@login_required
def partie_update(partie_id: str) -> str:
    """Handle edit form submission."""
    expected = edit_conflict.submitted_etag()
    data = _form_data()
    # The lawyer's form (D7): a CHANGED status becomes his decision; an
    # unchanged one — a presumed Claude inscription re-submitted by an
    # unrelated save — keeps its provenance. Only « Confirmer » confirms.
    partie, errors = update_partie(
        partie_id, data, expected_etag=expected,
        kyc_source=kyc.SOURCE_JURISTE)

    if errors:
        errors, conflict, data["etag"] = edit_conflict.resolve_refusal(
            errors,
            submitted=expected,
            reread=lambda: get_partie(partie_id),
            compare_url=url_for("parties.partie_detail", partie_id=partie_id),
        )
        data["id"] = partie_id
        return render_template(
            "parties/form.html",
            partie=data,
            errors=errors,
            conflict=conflict,
            role_labels=ROLE_LABELS,
            mandataires=_hydrate_mandataires(data.get("mandataires")),
            mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
            kyc_presumed=_kyc_presumed(get_partie(partie_id)),
        )

    bump_ctag("parties")

    if _is_htmx():
        resp = redirect(url_for("parties.partie_detail", partie_id=partie_id))
        resp.headers["HX-Redirect"] = url_for(
            "parties.partie_detail", partie_id=partie_id
        )
        return resp

    return redirect(url_for("parties.partie_detail", partie_id=partie_id))


# ── Conformité : « Confirmer » une inscription présumée (D7) ─────────────


@parties_bp.route("/<partie_id>/conformite/<check>/confirmer", methods=["POST"])
@login_required
def kyc_confirm(partie_id: str, check: str) -> Response:
    """The lawyer confirms a check Claude INSCRIBED (presumed until now).

    A plain form POST (CSRF enforced by the blueprint) carrying the etag the
    fiche was rendered from — a confirmation says « I read THIS version »,
    so an inscription that changed since is refused, never confirmed
    unseen. Answered by a redirect to the fiche, always in 2xx after it:
    ``?message=`` on success, ``?erreur=`` (the model's French refusal) on
    refusal. The contact's etag moves, so the ``parties`` CTag is bumped
    (DavX5's next If-Match PUT would 412 otherwise).
    """
    field = _KYC_FIELD_BY_SLUG.get(check)
    if field is None or not partie_id.strip():
        return redirect(url_for("parties.partie_list"))
    expected = edit_conflict.submitted_etag()
    _doc, errors = confirm_kyc_status(
        partie_id, field, par=kyc.SOURCE_JURISTE, expected_etag=expected)
    if errors:
        if errors == ["Contact introuvable."]:
            return redirect(url_for("parties.partie_list"))
        erreur = (
            "La fiche a changé depuis son affichage — rien n'a été confirmé. "
            "Relisez-la, puis confirmez de nouveau."
            if concurrency.is_stale(errors) else " ".join(errors)
        )
        target = url_for("parties.partie_detail", partie_id=partie_id,
                         erreur=erreur)
    else:
        bump_ctag("parties")
        log_partie_event("kyc_confirmed", partie_id, field=field)
        target = url_for("parties.partie_detail", partie_id=partie_id,
                         message="Vérification confirmée.")
    resp = redirect(target)
    if _is_htmx():
        resp.headers["HX-Redirect"] = target
    return resp


# ── Delete ────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>/delete", methods=["POST"])
@login_required
def partie_delete(partie_id: str) -> str:
    """Delete a partie and redirect to the list."""
    existing = get_partie(partie_id)
    success, error = delete_partie(partie_id)

    if success:
        record_tombstone("parties", partie_id)
        bump_ctag("parties")
        # Append-only deletion trail (PA-G06).
        record_deletion(
            "partie", partie_id,
            title=display_name(existing) if existing else "",
            status=(existing or {}).get("contact_role", ""),
        )

    # A REFUSED deletion lands back on the contact's own page with the
    # French reason in a red banner — it used to redirect to the LIST like a
    # success, so a contact still linked to a dossier, or still the
    # mandataire of another contact, silently stayed while the lawyer
    # believed it gone (lot 0b review). Always a redirect: htmx swaps 2xx
    # only, and the confirmation dialog is a plain form POST anyway.
    if success or error == PARTIE_NOT_FOUND:
        target = url_for("parties.partie_list")
    else:
        target = url_for(
            "parties.partie_detail", partie_id=partie_id, erreur=error
        )
    resp = redirect(target)
    if _is_htmx():
        resp.headers["HX-Redirect"] = target
    return resp


# ── Export ───────────────────────────────────────────────────────────────


# « _courriel » / « _ville » sont DÉRIVÉS (motif de _display_name) : lire
# email/address_city bruts exportait toute personne morale sans courriel ni
# ville — son adresse et son courriel vivent dans le bloc professionnel, le
# formulaire masquant le bloc personnel pour une entreprise.
_EXPORT_COLUMNS_CSV = [
    ("_display_name", "Nom"),
    ("contact_role", "Rôle"),
    ("_courriel", "Courriel"),
    ("phone_cell", "Cellulaire"),
    ("phone_work", "Tél. professionnel"),
    ("organization", "Organisation"),
    ("_ville", "Ville"),
]

_EXPORT_COLUMNS_PDF = [
    ("_display_name", "Nom", 2.0),
    ("contact_role", "Rôle", 1.0),
    ("_courriel", "Courriel", 1.5),
    ("phone_cell", "Cellulaire", 1.0),
    ("phone_work", "Tél. professionnel", 1.0),
    ("organization", "Organisation", 1.5),
    ("_ville", "Ville", 1.0),
]


def _get_export_parties() -> list[dict]:
    """Fetch and pre-process parties for export, respecting current filters."""
    role_filter = request.args.get("role", "client")
    search = request.args.get("q", "").strip()

    parties = list_parties(
        role_filter=role_filter if role_filter != "tous" else None,
        search=search or None,
    )
    for p in parties:
        p["_display_name"] = display_name(p)
        # L'adresse et le courriel RETENUS — la même autorité que les
        # gabarits, l'accusé du portail et le connecteur MCP.
        p["_ville"] = selected_address(p).get("city", "")
        p["_courriel"] = selected_email(p)
        p["contact_role"] = ROLE_LABELS.get(p.get("contact_role", ""), p.get("contact_role", ""))
    return parties


@parties_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export parties as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_parties()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"impliques_{date_str}.csv",
    )


@parties_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export parties as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_parties()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Impliqués",
        filename=f"impliques_{date_str}.pdf",
    )
