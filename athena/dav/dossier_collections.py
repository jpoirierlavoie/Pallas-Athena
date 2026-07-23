"""Per-dossier CalDAV collections -- VEVENT hearings, VTODO tasks, VJOURNAL notes.

Each active dossier is exposed as a CalDAV collection at:
    /dav/dossier-{dossierId}/

Resources within the collection:
    /dav/dossier-{dossierId}/{resourceId}.ics

VEVENT resources map to hearings, VTODO to tasks, VJOURNAL to notes. A
dossier-linked hearing lives ONLY here; /dav/calendar/ keeps the standalone
ones, exactly as /dav/tasks/ keeps only dossier-less tasks. Serving the same
hearing from both would have DavX5 import it twice and let a write through
one collection bump the other's CTag not at all.

:data:`DOSSIER_COMPONENTS` is the single source of truth for the advertised
component set -- ``dav/__init__.py`` imports it for the root Depth:1 listing
so the two advertisements cannot drift. A collection that serves VEVENTs
without advertising VEVENT is a silent no-op on the client.
"""

import logging
import xml.etree.ElementTree as ET

from flask import Blueprint, Response, request

from dav.dav_auth import dav_auth_required
from dav.sync import (
    bump_ctag,
    get_ctag,
    get_sync_token,
    get_tombstones,
    record_tombstone,
    remove_tombstone,
)
from dav.xml_utils import (
    add_propstat,
    add_response,
    add_status_response,
    caldav_tag,
    cs_tag,
    dav_tag,
    make_multistatus,
    parse_propfind_body,
    parse_report_body,
    propfind_requests_prop,
    serialize_multistatus,
)
from models.dossier import get_dossier
from models.hearing import (
    create_hearing,
    delete_hearing,
    get_hearing,
    hearing_to_vevent,
    list_hearings,
    update_hearing,
    vevent_to_hearing,
)
from models.note import (
    create_note,
    delete_note,
    get_note,
    list_notes,
    note_to_vjournal,
    update_note,
    vjournal_to_note,
)
from models.task import (
    create_task,
    delete_task,
    get_task,
    list_tasks,
    task_to_vtodo,
    update_task,
    vtodo_to_task,
)
from utils.logging_setup import sanitize_log_value
from utils.tracing_setup import add_attributes, firestore_span, span

logger = logging.getLogger(__name__)

dossier_dav_bp = Blueprint("dossier_dav", __name__)

_PAYLOAD_TOO_LARGE = "Corps de requête trop volumineux."

# A dossier's per-collection resources are exposed to DavX5 only while it is
# active. Closed/archived dossiers are *drained*, not abruptly removed: the
# collection still responds (so an enabled client can sync it down without a
# hard 404) but lists no live resources — only the tombstones recorded on the
# close transition, which tell DavX5 to delete its local copies. See
# routes.dossiers._sync_dossier_dav_visibility.
_ACTIVE_DOSSIER_STATUSES = ("actif", "en_attente")

# The collection's advertised supported-calendar-component-set. Imported by
# dav/__init__.py for the root Depth:1 listing: two hard-coded literals that
# disagree are a classic silent desync (discovery promises one capability,
# the collection PROPFIND contradicts it on the next refresh).
DOSSIER_COMPONENTS: tuple[str, ...] = ("VEVENT", "VTODO", "VJOURNAL")


def _dossier_is_active(dossier: dict | None) -> bool:
    """True when the dossier's DAV collection should expose live resources."""
    return bool(dossier) and dossier.get("status") in _ACTIVE_DOSSIER_STATUSES


# -- OPTIONS -----------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["OPTIONS"])
@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["OPTIONS"])
@dav_auth_required
def options(dossier_id: str, resource_id: str = None) -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, GET, PUT, DELETE, PROPFIND, REPORT"
    resp.headers["DAV"] = "1, 2, 3, calendar-access"
    return resp


# -- PROPFIND (Collection) ---------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["PROPFIND"])
@dav_auth_required
def propfind_collection(dossier_id: str) -> Response:
    """PROPFIND on a dossier collection.

    Depth:0 -> return collection properties only.
    Depth:1 -> return collection properties + all resources in the collection.
    """
    add_attributes(
        **{
            "dav.collection_type": "dossier",
            "dav.dossier_id": dossier_id,
            "dav.operation": "propfind",
        }
    )

    with firestore_span("get", "dossiers", doc_id=dossier_id):
        dossier = get_dossier(dossier_id)
    # A deleted dossier is truly gone (404). A closed/archived one still
    # responds but as an empty, draining collection (no live resources) so an
    # enabled DavX5 client can sync it down cleanly instead of erroring.
    if not dossier:
        return Response("Not Found", status=404)
    active = _dossier_is_active(dossier)

    depth = request.headers.get("Depth", "0")
    add_attributes(**{"dav.depth": depth, "dav.dossier_active": active})
    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
    multistatus = make_multistatus()

    _add_collection_props(multistatus, dossier, body)

    if depth == "1" and active:
        hearings, tasks, notes = _collection_members(dossier_id)
        total = len(hearings) + len(tasks) + len(notes)
        with span(
            "dav.serialize_objects",
            **{
                "dav.hearing_count": len(hearings),
                "dav.task_count": len(tasks),
                "dav.note_count": len(notes),
                "dav.object_count": total,
            },
        ):
            for hearing in hearings:
                _add_hearing_resource(multistatus, dossier_id, hearing, body)
            for task in tasks:
                _add_task_resource(multistatus, dossier_id, task, body)
            for note in notes:
                _add_note_resource(multistatus, dossier_id, note, body)
        add_attributes(**{"dav.object_count": total})

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _add_collection_props(
    multistatus: ET.Element, dossier: dict, body: ET.Element | None
) -> None:
    """Add the dossier collection's own <D:response>."""
    href = f"/dav/dossier-{dossier['id']}/"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))
        ET.SubElement(rt, caldav_tag("calendar"))

    if propfind_requests_prop(body, dav_tag("displayname")):
        display = f"{dossier.get('file_number', '')} \u2014 {dossier.get('title', '')}"
        ET.SubElement(prop, dav_tag("displayname")).text = display

    if propfind_requests_prop(body, cs_tag("getctag")):
        ET.SubElement(prop, cs_tag("getctag")).text = get_ctag(
            f"dossier:{dossier['id']}"
        )

    if propfind_requests_prop(body, dav_tag("sync-token")):
        sync_name = f"dossier:{dossier['id']}"
        ET.SubElement(prop, dav_tag("sync-token")).text = (
            f"data:,{get_sync_token(sync_name)}"
        )

    if propfind_requests_prop(body, caldav_tag("supported-calendar-component-set")):
        sccs = ET.SubElement(prop, caldav_tag("supported-calendar-component-set"))
        for component in DOSSIER_COMPONENTS:
            ET.SubElement(sccs, caldav_tag("comp")).set("name", component)

    if propfind_requests_prop(body, dav_tag("supported-report-set")):
        srs = ET.SubElement(prop, dav_tag("supported-report-set"))
        sr = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr, dav_tag("report")).append(
            ET.Element(dav_tag("sync-collection"))
        )
        sr2 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr2, dav_tag("report")).append(
            ET.Element(caldav_tag("calendar-multiget"))
        )
        sr3 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr3, dav_tag("report")).append(
            ET.Element(caldav_tag("calendar-query"))
        )


def _collection_members(dossier_id: str) -> tuple[list, list, list]:
    """Return (hearings, tasks, notes) for a dossier, each traced.

    One helper so the four places that enumerate a collection (PROPFIND,
    sync-collection, calendar-query, and the drain check) cannot disagree
    about what the collection contains.
    """
    with firestore_span("query", "hearings", dossier_id=dossier_id):
        hearings = list_hearings(dossier_id=dossier_id)
    with firestore_span("query", "tasks", dossier_id=dossier_id):
        tasks = list_tasks(dossier_id=dossier_id)
    with firestore_span("query", "notes", dossier_id=dossier_id):
        notes = list_notes(dossier_id=dossier_id)
    return hearings, tasks, notes


def _add_calendar_resource(
    multistatus: ET.Element,
    dossier_id: str,
    obj: dict,
    body: ET.Element | None,
    serialize,
) -> None:
    """Add one calendar resource <D:response>, whatever its component type."""
    href = f"/dav/dossier-{dossier_id}/{obj['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{obj.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = (
            "text/calendar; charset=utf-8"
        )

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))  # Empty for non-collection

    if body is not None and propfind_requests_prop(body, caldav_tag("calendar-data")):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = serialize(obj)


def _add_hearing_resource(
    multistatus: ET.Element, dossier_id: str, hearing: dict, body: ET.Element | None
) -> None:
    """Add a single VEVENT resource <D:response>."""
    _add_calendar_resource(multistatus, dossier_id, hearing, body, hearing_to_vevent)


def _add_task_resource(
    multistatus: ET.Element, dossier_id: str, task: dict, body: ET.Element | None
) -> None:
    """Add a single VTODO resource <D:response>."""
    _add_calendar_resource(multistatus, dossier_id, task, body, task_to_vtodo)


def _add_note_resource(
    multistatus: ET.Element, dossier_id: str, note: dict, body: ET.Element | None
) -> None:
    """Add a single VJOURNAL resource <D:response>."""
    _add_calendar_resource(multistatus, dossier_id, note, body, note_to_vjournal)


# -- PROPFIND (Resource) -----------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["PROPFIND"])
@dav_auth_required
def propfind_resource(dossier_id: str, resource_id: str) -> Response:
    """PROPFIND on a single resource within a dossier collection."""
    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)

    resolved = _resolve_resource(dossier_id, resource_id)
    if resolved is None:
        return Response("Not Found", status=404)

    obj, serialize = resolved
    multistatus = make_multistatus()
    _add_calendar_resource(multistatus, dossier_id, obj, body, serialize)
    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# -- REPORT ------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["REPORT"])
@dav_auth_required
def report_collection(dossier_id: str) -> Response:
    """Handle REPORT requests on a dossier collection.

    Supported reports: sync-collection, calendar-multiget, calendar-query.
    """
    with firestore_span("get", "dossiers", doc_id=dossier_id):
        dossier = get_dossier(dossier_id)
    if not dossier:
        return Response("Not Found", status=404)
    # Closed/archived dossiers report as empty (drained) — see the module note.
    active = _dossier_is_active(dossier)

    try:
        body_root = parse_report_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
    if body_root is None:
        return Response("Bad Request", status=400)

    local = body_root.tag.split("}")[-1] if "}" in body_root.tag else body_root.tag
    add_attributes(
        **{
            "dav.collection_type": "dossier",
            "dav.dossier_id": dossier_id,
            "dav.operation": "report",
            "dav.report_type": local,
            "dav.dossier_active": active,
        }
    )

    if local == "sync-collection":
        return _handle_sync_collection(dossier_id, body_root, active)
    elif local == "calendar-multiget":
        return _handle_multiget(dossier_id, body_root, active)
    elif local == "calendar-query":
        return _handle_calendar_query(dossier_id, body_root, active)

    return Response("Report type not supported", status=501)


def _handle_sync_collection(
    dossier_id: str, body_root: ET.Element, active: bool = True
) -> Response:
    """sync-collection REPORT -- return all resources + tombstones.

    When the dossier is not active the live set is empty, so every recorded
    tombstone is reported as a 404 and DavX5 drains its local copies.
    """
    sync_name = f"dossier:{dossier_id}"
    add_attributes(
        **{
            "dav.collection_type": "dossier",
            "dav.dossier_id": dossier_id,
            "dav.operation": "sync_collection",
        }
    )

    with span("dav.parse_sync_token") as s:
        token_el = body_root.find(dav_tag("sync-token"))
        client_token = ""
        if token_el is not None and token_el.text:
            client_token = token_el.text.replace("data:,", "")
        s.set_attribute("dav.sync_token", client_token or "initial")

    multistatus = make_multistatus()

    with firestore_span("get", "dav_sync", doc_id=sync_name):
        current_token = get_sync_token(sync_name)

    changed_count = 0
    tombstone_count = 0
    if not client_token or client_token != current_token:
        if active:
            hearings, tasks, notes = _collection_members(dossier_id)
        else:
            # Draining collection: no live resources, so all tombstones report.
            hearings, tasks, notes = [], [], []

        # RFC 6578 defines no filter element, so a sync-collection response is
        # necessarily component-blind: every member of the collection is
        # reported and the client routes by component after fetching bodies.
        members = list(hearings) + list(tasks) + list(notes)
        changed_count = len(members)
        with span(
            "dav.serialize_objects",
            **{
                "dav.hearing_count": len(hearings),
                "dav.task_count": len(tasks),
                "dav.note_count": len(notes),
                "dav.object_count": changed_count,
            },
        ):
            for obj in members:
                resp = add_response(
                    multistatus, f"/dav/dossier-{dossier_id}/{obj['id']}.ics"
                )
                prop = add_propstat(resp)
                ET.SubElement(prop, dav_tag("getetag")).text = (
                    f'"{obj.get("etag", "")}"'
                )

        with firestore_span(
            "query",
            "dav_sync.tombstones",
            dossier_id=dossier_id,
        ):
            tombstones = get_tombstones(sync_name)

        # Never report a tombstone for a live resource (RFC 6578) — a
        # resurrected id must not appear as both 200 propstat and 404.
        live_ids = {obj["id"] for obj in members}
        tombstones = [ts for ts in tombstones if ts["id"] not in live_ids]

        tombstone_count = len(tombstones)
        if tombstone_count:
            with span("dav.add_tombstones", **{"dav.tombstone_count": tombstone_count}):
                for ts in tombstones:
                    add_status_response(
                        multistatus,
                        f"/dav/dossier-{dossier_id}/{ts['id']}.ics",
                        404,
                        "Not Found",
                    )

    ET.SubElement(multistatus, dav_tag("sync-token")).text = (
        f"data:,{current_token}"
    )

    with span("dav.build_multistatus"):
        xml = serialize_multistatus(multistatus)

    add_attributes(
        **{
            "dav.changed_count": changed_count,
            "dav.tombstone_count": tombstone_count,
            "dav.response_status": 207,
        }
    )
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_multiget(
    dossier_id: str, body_root: ET.Element, active: bool = True
) -> Response:
    """calendar-multiget REPORT -- return specific resources by href.

    A drained (inactive) collection exposes no live resources, so every
    requested href is reported as 404.
    """
    multistatus = make_multistatus()

    for href_el in body_root.findall(dav_tag("href")):
        href = href_el.text or ""
        resource_id = _extract_resource_id(href)
        if not resource_id or not active:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        resolved = _resolve_resource(dossier_id, resource_id)
        if resolved is None:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        obj, serialize = resolved
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{obj.get("etag", "")}"'
        ET.SubElement(prop, caldav_tag("calendar-data")).text = serialize(obj)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def requested_components(body_root: ET.Element | None) -> set[str] | None:
    """Component names a calendar-query asks for, or None when unfiltered.

    RFC 4791 §9.7.1 nests the filter as
    ``<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT"/>``.
    A collection that carries all three component types MUST honor this:
    answering a VEVENT-scoped query with VTODO and VJOURNAL bodies leaves the
    client to sort out components it never asked for, which is precisely the
    ambiguity a mixed collection has to avoid. Returns ``None`` (meaning "no
    filter, return everything") when the body carries no usable comp-filter,
    so an absent or malformed filter degrades to the previous behavior rather
    than to an empty collection.
    """
    if body_root is None:
        return None
    filter_el = body_root.find(caldav_tag("filter"))
    if filter_el is None:
        return None
    names: set[str] = set()
    for outer in filter_el.findall(caldav_tag("comp-filter")):
        # The outer comp-filter is VCALENDAR; the components are its children.
        for inner in outer.findall(caldav_tag("comp-filter")):
            name = (inner.get("name") or "").upper()
            if name:
                names.add(name)
    return names or None


def _handle_calendar_query(
    dossier_id: str, body_root: ET.Element, active: bool = True
) -> Response:
    """calendar-query REPORT -- return the resources matching the filter.

    A drained (inactive) collection exposes no live resources.
    """
    multistatus = make_multistatus()

    if not active:
        xml = serialize_multistatus(multistatus)
        return Response(xml, status=207, content_type="application/xml; charset=utf-8")

    wanted = requested_components(body_root)
    add_attributes(
        **{"dav.comp_filter": ",".join(sorted(wanted)) if wanted else "none"}
    )

    hearings, tasks, notes = _collection_members(dossier_id)
    groups = (
        ("VEVENT", hearings, hearing_to_vevent),
        ("VTODO", tasks, task_to_vtodo),
        ("VJOURNAL", notes, note_to_vjournal),
    )

    emitted = 0
    for component, objects, serialize in groups:
        if wanted is not None and component not in wanted:
            continue
        for obj in objects:
            href = f"/dav/dossier-{dossier_id}/{obj['id']}.ics"
            resp = add_response(multistatus, href)
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = f'"{obj.get("etag", "")}"'
            ET.SubElement(prop, caldav_tag("calendar-data")).text = serialize(obj)
            emitted += 1

    add_attributes(**{"dav.object_count": emitted})
    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# -- GET ---------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["GET"])
@dav_auth_required
def get_resource(dossier_id: str, resource_id: str) -> Response:
    resolved = _resolve_resource(dossier_id, resource_id)
    if resolved is None:
        return Response("Not Found", status=404)

    obj, serialize = resolved
    resp = Response(
        serialize(obj), status=200, content_type="text/calendar; charset=utf-8"
    )
    resp.headers["ETag"] = f'"{obj.get("etag", "")}"'
    return resp


# -- PUT ---------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["PUT"])
@dav_auth_required
def put_resource(dossier_id: str, resource_id: str) -> Response:
    """Create or update a resource in a dossier collection.

    Parses the iCalendar body to determine component type (VTODO or VJOURNAL).
    """
    with firestore_span("get", "dossiers", doc_id=dossier_id):
        dossier = get_dossier(dossier_id)
    if not dossier:
        return Response("Not Found", status=404)

    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")

    ical_str = request.get_data(as_text=True)
    if not ical_str:
        return Response("Bad Request", status=400)

    component_type = _detect_component_type(ical_str)
    add_attributes(
        **{
            "dav.collection_type": "dossier",
            "dav.dossier_id": dossier_id,
            "dav.operation": "put",
            "dav.component_type": component_type or "unknown",
            "dav.body_size": len(ical_str),
            "dav.conditional": bool(if_match or if_none_match),
        }
    )

    if component_type == "VEVENT":
        return _put_hearing(
            dossier_id, dossier, resource_id, ical_str, if_match, if_none_match
        )
    elif component_type == "VTODO":
        return _put_task(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match)
    elif component_type == "VJOURNAL":
        return _put_note(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match)
    else:
        return Response("Unsupported component type", status=400)


def _put_hearing(
    dossier_id: str,
    dossier: dict,
    resource_id: str,
    ical_str: str,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    """Handle PUT for a VEVENT resource (mirrors :func:`_put_task`)."""
    existing = get_hearing(resource_id)

    # Precondition checks
    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        if if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    try:
        data = vevent_to_hearing(ical_str)
    except Exception:
        return Response("Bad Request — invalid iCalendar", status=400)

    # Force dossier_id from the URL: the collection determines the dossier.
    # hearing_to_vevent emits X-PALLAS-DOSSIER-ID and vevent_to_hearing reads
    # it back, so a client round-tripping that property could otherwise write
    # a dossier_id disagreeing with the collection the PUT landed in — and
    # the CTag bump below would then target the wrong collection.
    data["dossier_id"] = dossier_id
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")

    sync_name = f"dossier:{dossier_id}"

    if existing:
        # Moved in from another collection: tombstone it there. A hearing with
        # no dossier lived in the shared "hearings" collection.
        old_dossier = existing.get("dossier_id")
        if old_dossier and old_dossier != dossier_id:
            record_tombstone(f"dossier:{old_dossier}", resource_id)
            bump_ctag(f"dossier:{old_dossier}")
        elif not old_dossier:
            record_tombstone("hearings", resource_id)
            bump_ctag("hearings")

        updated, errors = update_hearing(resource_id, data)
        if errors:
            logger.warning(
                "Dossier DAV PUT (VEVENT) validation failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(errors),
            )
            return Response("Données invalides.", status=422)
        if old_dossier != dossier_id:
            # Resource (re)enters this collection — drop any stale tombstone
            remove_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_hearing(data)
        if errors:
            logger.warning(
                "Dossier DAV PUT (VEVENT) validation failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(errors),
            )
            return Response("Données invalides.", status=422)
        # Resource (re)enters the collection — drop any stale tombstone
        remove_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


def _put_task(
    dossier_id: str,
    dossier: dict,
    resource_id: str,
    ical_str: str,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    """Handle PUT for a VTODO resource."""
    existing = get_task(resource_id)

    # Precondition checks
    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        if if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    try:
        data = vtodo_to_task(ical_str)
    except Exception:
        return Response("Bad Request \u2014 invalid iCalendar", status=400)

    # Force dossier_id from URL (the collection implies the dossier)
    data["dossier_id"] = dossier_id
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")

    sync_name = f"dossier:{dossier_id}"

    if existing:
        # If task was previously in a different collection, record a
        # tombstone there: another dossier's collection, or the standalone
        # /dav/tasks/ collection when it had no dossier at all.
        old_dossier = existing.get("dossier_id")
        if old_dossier and old_dossier != dossier_id:
            record_tombstone(f"dossier:{old_dossier}", resource_id)
            bump_ctag(f"dossier:{old_dossier}")
        elif not old_dossier:
            record_tombstone("tasks", resource_id)
            bump_ctag("tasks")

        updated, errors = update_task(resource_id, data)
        if errors:
            logger.warning(
                "Dossier DAV PUT (VTODO) validation failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(errors),
            )
            return Response("Données invalides.", status=422)
        if old_dossier != dossier_id:
            # Task (re)enters this collection — drop any stale tombstone
            remove_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_task(data)
        if errors:
            logger.warning(
                "Dossier DAV PUT (VTODO) validation failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(errors),
            )
            return Response("Données invalides.", status=422)
        # Resource (re)enters the collection — drop any stale tombstone
        remove_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


def _put_note(
    dossier_id: str,
    dossier: dict,
    resource_id: str,
    ical_str: str,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    """Handle PUT for a VJOURNAL resource."""
    existing = get_note(resource_id)

    # Precondition checks
    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        if if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    try:
        data = vjournal_to_note(ical_str)
    except Exception:
        return Response("Bad Request — invalid iCalendar", status=400)

    data["dossier_id"] = dossier_id
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")

    sync_name = f"dossier:{dossier_id}"

    if existing:
        updated, errors = update_note(resource_id, data)
        if errors:
            logger.warning(
                "Dossier DAV PUT (VJOURNAL) validation failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(errors),
            )
            return Response("Données invalides.", status=422)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_note(data)
        if errors:
            logger.warning(
                "Dossier DAV PUT (VJOURNAL) validation failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(errors),
            )
            return Response("Données invalides.", status=422)
        # Resource (re)enters the collection — drop any stale tombstone
        remove_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


# -- DELETE ------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["DELETE"])
@dav_auth_required
def delete_resource(dossier_id: str, resource_id: str) -> Response:
    add_attributes(
        **{
            "dav.collection_type": "dossier",
            "dav.dossier_id": dossier_id,
            "dav.operation": "delete",
            "dav.resource_id": resource_id,
        }
    )
    existing = get_task(resource_id)
    if existing and existing.get("dossier_id") == dossier_id:
        if_match = request.headers.get("If-Match")
        if if_match and if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)

        success, error = delete_task(resource_id)
        if not success:
            logger.error(
                "Dossier DAV DELETE (task) failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(error),
            )
            return Response("Erreur serveur.", status=500)

        sync_name = f"dossier:{dossier_id}"
        record_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        return Response("", status=204)

    existing_note = get_note(resource_id)
    if existing_note and existing_note.get("dossier_id") == dossier_id:
        if_match = request.headers.get("If-Match")
        if if_match and if_match != f'"{existing_note.get("etag", "")}"':
            return Response("Precondition Failed", status=412)

        success, error = delete_note(resource_id)
        if not success:
            logger.error(
                "Dossier DAV DELETE (note) failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(error),
            )
            return Response("Erreur serveur.", status=500)

        sync_name = f"dossier:{dossier_id}"
        record_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        return Response("", status=204)

    existing_hearing = get_hearing(resource_id)
    if existing_hearing and existing_hearing.get("dossier_id") == dossier_id:
        if_match = request.headers.get("If-Match")
        if if_match and if_match != f'"{existing_hearing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)

        success, error = delete_hearing(resource_id)
        if not success:
            logger.error(
                "Dossier DAV DELETE (hearing) failed for %s: %s",
                sanitize_log_value(resource_id), sanitize_log_value(error),
            )
            return Response("Erreur serveur.", status=500)

        sync_name = f"dossier:{dossier_id}"
        record_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        return Response("", status=204)

    return Response("Not Found", status=404)


# -- Helpers -----------------------------------------------------------------

def _resolve_resource(dossier_id: str, resource_id: str):
    """Resolve a resource id to (doc, serializer) within this collection.

    Tasks, notes and hearings share one flat id space under
    /dav/dossier-{id}/{resourceId}.ics. Ids are server-minted UUIDv4s, so a
    collision across collections is not a practical concern; the cost is up
    to three point reads on a miss, ordered cheapest-first by how often each
    type is fetched. Returns None when nothing in THIS dossier matches.
    """
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        return task, task_to_vtodo

    note = get_note(resource_id)
    if note and note.get("dossier_id") == dossier_id:
        return note, note_to_vjournal

    hearing = get_hearing(resource_id)
    if hearing and hearing.get("dossier_id") == dossier_id:
        return hearing, hearing_to_vevent

    return None


def _detect_component_type(ical_str: str) -> str | None:
    """Detect whether the body contains VEVENT, VTODO or VJOURNAL."""
    if "BEGIN:VEVENT" in ical_str:
        return "VEVENT"
    elif "BEGIN:VTODO" in ical_str:
        return "VTODO"
    elif "BEGIN:VJOURNAL" in ical_str:
        return "VJOURNAL"
    return None


def _extract_resource_id(href: str) -> str | None:
    """Extract the resource ID from a dossier collection href."""
    href = href.rstrip("/")
    if not href.endswith(".ics"):
        return None
    segment = href.rsplit("/", 1)[-1]
    return segment.replace(".ics", "")
