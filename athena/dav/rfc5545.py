"""RFC-5545 CalDAV endpoints for standalone tasks (VTODO).

Endpoints
---------
/dav/tasks/                      Task collection (CalDAV with VTODO) -- standalone only
/dav/tasks/<task_id>.ics         Individual task resource

Dossier-linked tasks are served by dav/dossier_collections.py.
The former /dav/journals/ collection was removed in Phase D1.
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
from models.task import (
    create_task,
    delete_task,
    get_task,
    list_tasks,
    task_to_vtodo,
    update_task,
    vtodo_to_task,
)

rfc5545_bp = Blueprint("rfc5545", __name__)


# ═══════════════════════════════════════════════════════════════════════════
#  VTODO — Tasks
# ═══════════════════════════════════════════════════════════════════════════

TASKS_COLLECTION = "tasks"
TASKS_PATH = "/dav/tasks/"


@rfc5545_bp.route("/dav/tasks/", methods=["OPTIONS"])
@rfc5545_bp.route("/dav/tasks/<task_id>.ics", methods=["OPTIONS"])
@dav_auth_required
def tasks_options(task_id: str = None) -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, GET, PUT, DELETE, PROPFIND, REPORT"
    resp.headers["DAV"] = "1, 2, 3, calendar-access"
    return resp


# ── PROPFIND (Tasks) ───────────────────────────────────────────────────────

@rfc5545_bp.route("/dav/tasks/", methods=["PROPFIND"])
@dav_auth_required
def tasks_propfind_collection() -> Response:
    depth = request.headers.get("Depth", "0")
    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()

    _add_tasks_collection_response(multistatus, body)

    if depth == "1":
        tasks = list_tasks()
        standalone_tasks = [t for t in tasks if not t.get("dossier_id")]
        for task in standalone_tasks:
            _add_task_resource_response(multistatus, task, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


@rfc5545_bp.route("/dav/tasks/<task_id>.ics", methods=["PROPFIND"])
@dav_auth_required
def tasks_propfind_resource(task_id: str) -> Response:
    task = get_task(task_id)
    if not task:
        return Response("Not Found", status=404)

    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()
    _add_task_resource_response(multistatus, task, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _add_tasks_collection_response(
    multistatus: ET.Element, body: ET.Element | None
) -> None:
    resp = add_response(multistatus, TASKS_PATH)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))
        ET.SubElement(rt, caldav_tag("calendar"))

    if propfind_requests_prop(body, dav_tag("displayname")):
        ET.SubElement(prop, dav_tag("displayname")).text = (
            "Pallas Athena \u2014 T\u00e2ches"
        )

    if propfind_requests_prop(body, cs_tag("getctag")):
        ET.SubElement(prop, cs_tag("getctag")).text = get_ctag(TASKS_COLLECTION)

    if propfind_requests_prop(body, dav_tag("sync-token")):
        ET.SubElement(prop, dav_tag("sync-token")).text = (
            f"data:,{get_sync_token(TASKS_COLLECTION)}"
        )

    if propfind_requests_prop(
        body, caldav_tag("supported-calendar-component-set")
    ):
        sccs = ET.SubElement(prop, caldav_tag("supported-calendar-component-set"))
        comp = ET.SubElement(sccs, caldav_tag("comp"))
        comp.set("name", "VTODO")

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


def _add_task_resource_response(
    multistatus: ET.Element,
    task: dict,
    body: ET.Element | None,
) -> None:
    href = f"/dav/tasks/{task['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = (
            "text/calendar; charset=utf-8"
        )

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))

    if body is not None and propfind_requests_prop(
        body, caldav_tag("calendar-data")
    ):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = (
            task_to_vtodo(task)
        )


# ── REPORT (Tasks) ─────────────────────────────────────────────────────────

@rfc5545_bp.route("/dav/tasks/", methods=["REPORT"])
@dav_auth_required
def tasks_report_collection() -> Response:
    body_root = parse_report_body(request.get_data())
    if body_root is None:
        return Response("Bad Request", status=400)

    local = body_root.tag.split("}")[-1] if "}" in body_root.tag else body_root.tag

    if local == "sync-collection":
        return _tasks_sync_collection(body_root)
    elif local == "calendar-multiget":
        return _tasks_multiget(body_root)
    elif local == "calendar-query":
        return _tasks_calendar_query(body_root)

    return Response("Report type not supported", status=501)


def _tasks_sync_collection(body_root: ET.Element) -> Response:
    token_el = body_root.find(dav_tag("sync-token"))
    client_token = ""
    if token_el is not None and token_el.text:
        client_token = token_el.text.replace("data:,", "")

    multistatus = make_multistatus()
    current_token = get_sync_token(TASKS_COLLECTION)

    if not client_token or client_token != current_token:
        tasks = list_tasks()
        standalone_tasks = [t for t in tasks if not t.get("dossier_id")]
        for task in standalone_tasks:
            resp = add_response(multistatus, f"/dav/tasks/{task['id']}.ics")
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{task.get("etag", "")}"'
            )

        tombstones = get_tombstones(TASKS_COLLECTION)
        for ts in tombstones:
            add_status_response(
                multistatus, f"/dav/tasks/{ts['id']}.ics", 404, "Not Found"
            )

    ET.SubElement(multistatus, dav_tag("sync-token")).text = (
        f"data:,{current_token}"
    )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _tasks_multiget(body_root: ET.Element) -> Response:
    multistatus = make_multistatus()

    for href_el in body_root.findall(dav_tag("href")):
        href = href_el.text or ""
        task_id = _extract_ics_id(href)
        if not task_id:
            add_status_response(multistatus, href, 404, "Not Found")
            continue
        task = get_task(task_id)
        if not task:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'
        ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _tasks_calendar_query(body_root: ET.Element) -> Response:
    multistatus = make_multistatus()
    tasks = list_tasks()
    standalone_tasks = [t for t in tasks if not t.get("dossier_id")]

    for task in standalone_tasks:
        href = f"/dav/tasks/{task['id']}.ics"
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'
        ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# ── GET (Tasks) ────────────────────────────────────────────────────────────

@rfc5545_bp.route("/dav/tasks/<task_id>.ics", methods=["GET"])
@dav_auth_required
def tasks_get_resource(task_id: str) -> Response:
    task = get_task(task_id)
    if not task:
        return Response("Not Found", status=404)

    ical = task_to_vtodo(task)
    resp = Response(ical, status=200, content_type="text/calendar; charset=utf-8")
    resp.headers["ETag"] = f'"{task.get("etag", "")}"'
    return resp


# ── PUT (Tasks) ────────────────────────────────────────────────────────────

@rfc5545_bp.route("/dav/tasks/<task_id>.ics", methods=["PUT"])
@dav_auth_required
def tasks_put_resource(task_id: str) -> Response:
    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")
    existing = get_task(task_id)

    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        if if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    ical_str = request.get_data(as_text=True)
    if not ical_str:
        return Response("Bad Request", status=400)

    try:
        data = vtodo_to_task(ical_str)
    except Exception:
        return Response("Bad Request — invalid iCalendar", status=400)

    if existing:
        updated, errors = update_task(task_id, data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(TASKS_COLLECTION)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = task_id
        created, errors = create_task(data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(TASKS_COLLECTION)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


# ── DELETE (Tasks) ─────────────────────────────────────────────────────────

@rfc5545_bp.route("/dav/tasks/<task_id>.ics", methods=["DELETE"])
@dav_auth_required
def tasks_delete_resource(task_id: str) -> Response:
    existing = get_task(task_id)
    if not existing:
        return Response("Not Found", status=404)

    if_match = request.headers.get("If-Match")
    if if_match and if_match != f'"{existing.get("etag", "")}"':
        return Response("Precondition Failed", status=412)

    success, error = delete_task(task_id)
    if not success:
        return Response(error, status=500)

    record_tombstone(TASKS_COLLECTION, task_id)
    bump_ctag(TASKS_COLLECTION)
    return Response("", status=204)


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_ics_id(href: str) -> str | None:
    """Extract the resource ID from an .ics href."""
    href = href.rstrip("/")
    if not href.endswith(".ics"):
        return None
    segment = href.rsplit("/", 1)[-1]
    return segment.replace(".ics", "")
