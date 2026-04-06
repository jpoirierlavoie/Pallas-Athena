"""Per-dossier CalDAV collections -- VTODO tasks and VJOURNAL notes.

Each active dossier is exposed as a CalDAV collection at:
    /dav/dossier-{dossierId}/

Resources within the collection:
    /dav/dossier-{dossierId}/{resourceId}.ics

VTODO resources map to tasks, VJOURNAL resources map to notes.
"""

import xml.etree.ElementTree as ET

from flask import Blueprint, Response, request

from dav.dav_auth import dav_auth_required
from dav.sync import bump_ctag, get_ctag, get_sync_token, get_tombstones, record_tombstone
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

dossier_dav_bp = Blueprint("dossier_dav", __name__)


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
    dossier = get_dossier(dossier_id)
    if not dossier or dossier.get("status") not in ("actif", "en_attente"):
        return Response("Not Found", status=404)

    depth = request.headers.get("Depth", "0")
    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()

    _add_collection_props(multistatus, dossier, body)

    if depth == "1":
        tasks = list_tasks(dossier_id=dossier_id)
        for task in tasks:
            _add_task_resource(multistatus, dossier_id, task, body)

        notes = list_notes(dossier_id=dossier_id)
        for note in notes:
            _add_note_resource(multistatus, dossier_id, note, body)

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
        comp_todo = ET.SubElement(sccs, caldav_tag("comp"))
        comp_todo.set("name", "VTODO")
        comp_journal = ET.SubElement(sccs, caldav_tag("comp"))
        comp_journal.set("name", "VJOURNAL")

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


def _add_task_resource(
    multistatus: ET.Element,
    dossier_id: str,
    task: dict,
    body: ET.Element | None,
) -> None:
    """Add a single VTODO resource <D:response>."""
    href = f"/dav/dossier-{dossier_id}/{task['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = (
            "text/calendar; charset=utf-8"
        )

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))  # Empty for non-collection

    if body is not None and propfind_requests_prop(body, caldav_tag("calendar-data")):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)


def _add_note_resource(
    multistatus: ET.Element,
    dossier_id: str,
    note: dict,
    body: ET.Element | None,
) -> None:
    """Add a single VJOURNAL resource <D:response>."""
    href = f"/dav/dossier-{dossier_id}/{note['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{note.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = (
            "text/calendar; charset=utf-8"
        )

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))  # Empty for non-collection

    if body is not None and propfind_requests_prop(body, caldav_tag("calendar-data")):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = note_to_vjournal(note)


# -- PROPFIND (Resource) -----------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["PROPFIND"])
@dav_auth_required
def propfind_resource(dossier_id: str, resource_id: str) -> Response:
    """PROPFIND on a single resource within a dossier collection."""
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        body = parse_propfind_body(request.get_data())
        multistatus = make_multistatus()
        _add_task_resource(multistatus, dossier_id, task, body)
        xml = serialize_multistatus(multistatus)
        return Response(xml, status=207, content_type="application/xml; charset=utf-8")

    note = get_note(resource_id)
    if note and note.get("dossier_id") == dossier_id:
        body = parse_propfind_body(request.get_data())
        multistatus = make_multistatus()
        _add_note_resource(multistatus, dossier_id, note, body)
        xml = serialize_multistatus(multistatus)
        return Response(xml, status=207, content_type="application/xml; charset=utf-8")

    return Response("Not Found", status=404)


# -- REPORT ------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["REPORT"])
@dav_auth_required
def report_collection(dossier_id: str) -> Response:
    """Handle REPORT requests on a dossier collection.

    Supported reports: sync-collection, calendar-multiget, calendar-query.
    """
    dossier = get_dossier(dossier_id)
    if not dossier:
        return Response("Not Found", status=404)

    body_root = parse_report_body(request.get_data())
    if body_root is None:
        return Response("Bad Request", status=400)

    local = body_root.tag.split("}")[-1] if "}" in body_root.tag else body_root.tag

    if local == "sync-collection":
        return _handle_sync_collection(dossier_id, body_root)
    elif local == "calendar-multiget":
        return _handle_multiget(dossier_id, body_root)
    elif local == "calendar-query":
        return _handle_calendar_query(dossier_id, body_root)

    return Response("Report type not supported", status=501)


def _handle_sync_collection(dossier_id: str, body_root: ET.Element) -> Response:
    """sync-collection REPORT -- return all resources + tombstones."""
    sync_name = f"dossier:{dossier_id}"
    token_el = body_root.find(dav_tag("sync-token"))
    client_token = ""
    if token_el is not None and token_el.text:
        client_token = token_el.text.replace("data:,", "")

    multistatus = make_multistatus()
    current_token = get_sync_token(sync_name)

    if not client_token or client_token != current_token:
        tasks = list_tasks(dossier_id=dossier_id)
        for task in tasks:
            resp = add_response(
                multistatus, f"/dav/dossier-{dossier_id}/{task['id']}.ics"
            )
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{task.get("etag", "")}"'
            )

        notes = list_notes(dossier_id=dossier_id)
        for note in notes:
            resp = add_response(
                multistatus, f"/dav/dossier-{dossier_id}/{note['id']}.ics"
            )
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{note.get("etag", "")}"'
            )

        tombstones = get_tombstones(sync_name)
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
    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_multiget(dossier_id: str, body_root: ET.Element) -> Response:
    """calendar-multiget REPORT -- return specific resources by href."""
    multistatus = make_multistatus()

    for href_el in body_root.findall(dav_tag("href")):
        href = href_el.text or ""
        resource_id = _extract_resource_id(href)
        if not resource_id:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        task = get_task(resource_id)
        if task and task.get("dossier_id") == dossier_id:
            resp = add_response(multistatus, href)
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{task.get("etag", "")}"'
            )
            ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)
            continue

        note = get_note(resource_id)
        if note and note.get("dossier_id") == dossier_id:
            resp = add_response(multistatus, href)
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{note.get("etag", "")}"'
            )
            ET.SubElement(prop, caldav_tag("calendar-data")).text = note_to_vjournal(note)
            continue

        add_status_response(multistatus, href, 404, "Not Found")

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_calendar_query(dossier_id: str, body_root: ET.Element) -> Response:
    """calendar-query REPORT -- return all matching resources."""
    multistatus = make_multistatus()

    tasks = list_tasks(dossier_id=dossier_id)
    for task in tasks:
        href = f"/dav/dossier-{dossier_id}/{task['id']}.ics"
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'
        ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)

    notes = list_notes(dossier_id=dossier_id)
    for note in notes:
        href = f"/dav/dossier-{dossier_id}/{note['id']}.ics"
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{note.get("etag", "")}"'
        ET.SubElement(prop, caldav_tag("calendar-data")).text = note_to_vjournal(note)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# -- GET ---------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["GET"])
@dav_auth_required
def get_resource(dossier_id: str, resource_id: str) -> Response:
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        ical = task_to_vtodo(task)
        resp = Response(ical, status=200, content_type="text/calendar; charset=utf-8")
        resp.headers["ETag"] = f'"{task.get("etag", "")}"'
        return resp

    note = get_note(resource_id)
    if note and note.get("dossier_id") == dossier_id:
        ical = note_to_vjournal(note)
        resp = Response(ical, status=200, content_type="text/calendar; charset=utf-8")
        resp.headers["ETag"] = f'"{note.get("etag", "")}"'
        return resp

    return Response("Not Found", status=404)


# -- PUT ---------------------------------------------------------------------

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["PUT"])
@dav_auth_required
def put_resource(dossier_id: str, resource_id: str) -> Response:
    """Create or update a resource in a dossier collection.

    Parses the iCalendar body to determine component type (VTODO or VJOURNAL).
    """
    dossier = get_dossier(dossier_id)
    if not dossier:
        return Response("Not Found", status=404)

    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")

    ical_str = request.get_data(as_text=True)
    if not ical_str:
        return Response("Bad Request", status=400)

    component_type = _detect_component_type(ical_str)

    if component_type == "VTODO":
        return _put_task(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match)
    elif component_type == "VJOURNAL":
        return _put_note(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match)
    else:
        return Response("Unsupported component type", status=400)


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
        # If task was previously in a different dossier, record tombstone there
        old_dossier = existing.get("dossier_id")
        if old_dossier and old_dossier != dossier_id:
            record_tombstone(f"dossier:{old_dossier}", resource_id)
            bump_ctag(f"dossier:{old_dossier}")

        updated, errors = update_task(resource_id, data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_task(data)
        if errors:
            return Response("\n".join(errors), status=422)
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
            return Response("\n".join(errors), status=422)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_note(data)
        if errors:
            return Response("\n".join(errors), status=422)
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
    existing = get_task(resource_id)
    if existing and existing.get("dossier_id") == dossier_id:
        if_match = request.headers.get("If-Match")
        if if_match and if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)

        success, error = delete_task(resource_id)
        if not success:
            return Response(error, status=500)

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
            return Response(error, status=500)

        sync_name = f"dossier:{dossier_id}"
        record_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        return Response("", status=204)

    return Response("Not Found", status=404)


# -- Helpers -----------------------------------------------------------------

def _detect_component_type(ical_str: str) -> str | None:
    """Detect whether the iCalendar body contains VTODO or VJOURNAL."""
    if "BEGIN:VTODO" in ical_str:
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
