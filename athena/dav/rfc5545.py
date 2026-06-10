"""RFC-5545 CalDAV endpoints for standalone tasks (VTODO).

Endpoints
---------
/dav/tasks/                      Task collection (CalDAV with VTODO) -- standalone only
/dav/tasks/<task_id>.ics         Individual task resource

Dossier-linked tasks are served by dav/dossier_collections.py.
The former /dav/journals/ collection was removed in Phase D1.
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
from models.task import (
    create_task,
    delete_task,
    get_task,
    list_tasks,
    task_to_vtodo,
    update_task,
    vtodo_to_task,
)
from utils.tracing_setup import add_attributes, firestore_span

logger = logging.getLogger(__name__)

rfc5545_bp = Blueprint("rfc5545", __name__)

_PAYLOAD_TOO_LARGE = "Corps de requête trop volumineux."


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
    add_attributes(
        **{
            "dav.collection_type": "tasks",
            "dav.operation": "propfind",
            "dav.depth": depth,
        }
    )
    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
    multistatus = make_multistatus()

    _add_tasks_collection_response(multistatus, body)

    if depth == "1":
        with firestore_span("query", "tasks", filter="standalone"):
            tasks = list_tasks()
            standalone_tasks = [t for t in tasks if not t.get("dossier_id")]
        for task in standalone_tasks:
            _add_task_resource_response(multistatus, task, body)
        add_attributes(**{"dav.object_count": len(standalone_tasks)})

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


@rfc5545_bp.route("/dav/tasks/<task_id>.ics", methods=["PROPFIND"])
@dav_auth_required
def tasks_propfind_resource(task_id: str) -> Response:
    task = get_task(task_id)
    # Scope check: dossier-linked tasks live in /dav/dossier-{id}/ collections
    if not task or task.get("dossier_id"):
        return Response("Not Found", status=404)

    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
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
    try:
        body_root = parse_report_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
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
        live_ids: set[str] = set()
        for task in standalone_tasks:
            live_ids.add(task["id"])
            resp = add_response(multistatus, f"/dav/tasks/{task['id']}.ics")
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{task.get("etag", "")}"'
            )

        # Never report a tombstone for a live resource (RFC 6578)
        tombstones = get_tombstones(TASKS_COLLECTION)
        for ts in tombstones:
            if ts["id"] in live_ids:
                continue
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
        # Scope check: dossier-linked tasks live in /dav/dossier-{id}/ collections
        if not task or task.get("dossier_id"):
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
    # Scope check: dossier-linked tasks live in /dav/dossier-{id}/ collections
    if not task or task.get("dossier_id"):
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

    # Scope check: dossier-linked tasks live in /dav/dossier-{id}/ collections
    # and must not be touched (nor have this collection's CTag bumped) here.
    if existing and existing.get("dossier_id"):
        return Response("Not Found", status=404)

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

    # This collection holds standalone tasks ONLY. The payload can carry
    # X-PALLAS-DOSSIER-ID (clients round-trip unknown X- properties), which
    # would create/convert a dossier-linked task here while bumping the wrong
    # collection's CTag — force the standalone scope from the URL instead.
    data["dossier_id"] = None
    data["dossier_file_number"] = ""
    data["dossier_title"] = ""

    if existing:
        updated, errors = update_task(task_id, data)
        if errors:
            logger.warning(
                "Tasks PUT validation failed for %s: %s", task_id, errors
            )
            return Response("Données invalides.", status=422)
        bump_ctag(TASKS_COLLECTION)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = task_id
        created, errors = create_task(data)
        if errors:
            logger.warning(
                "Tasks PUT validation failed for %s: %s", task_id, errors
            )
            return Response("Données invalides.", status=422)
        # Resource (re)enters the collection — drop any stale tombstone
        remove_tombstone(TASKS_COLLECTION, task_id)
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
    # Scope check: dossier-linked tasks live in /dav/dossier-{id}/ collections
    # — deleting one here would corrupt the wrong collection's CTag/tombstones.
    if not existing or existing.get("dossier_id"):
        return Response("Not Found", status=404)

    if_match = request.headers.get("If-Match")
    if if_match and if_match != f'"{existing.get("etag", "")}"':
        return Response("Precondition Failed", status=412)

    success, error = delete_task(task_id)
    if not success:
        logger.error("Tasks DELETE failed for %s: %s", task_id, error)
        return Response("Erreur serveur.", status=500)

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
