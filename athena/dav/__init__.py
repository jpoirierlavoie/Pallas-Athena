"""DAV protocol endpoints — CardDAV, CalDAV, RFC-5545.

This module registers a top-level ``dav_bp`` blueprint that:
1. Provides ``/.well-known/carddav`` and ``/.well-known/caldav`` redirects.
2. Handles PROPFIND on the DAV root (``/dav/``) for principal/collection
   discovery so DavX5 can locate all four sub-collections.
3. Imports and registers the sub-blueprints for each protocol.
"""

import xml.etree.ElementTree as ET

from flask import Blueprint, Response, redirect, request

from dav.dav_auth import dav_auth_required
from dav.xml_utils import (
    add_propstat,
    add_response,
    caldav_tag,
    cs_tag,
    dav_tag,
    make_multistatus,
    parse_propfind_body,
    propfind_requests_prop,
    serialize_multistatus,
)
from utils.logging_setup import log_dav_operation, log_unexpected
from utils.tracing_setup import add_attributes, firestore_span

dav_bp = Blueprint("dav", __name__)


def _root_read_unavailable(check: str) -> Response:
    """503 — the root could not read what discovery must list.

    The answer the dossier collections give on a failed strict read since
    lot 1a (``dav.dossier_collections._read_unavailable``): 503 +
    ``Retry-After``, so DavX5 keeps its collection list and retries. Never
    a 207 listing fewer collections than exist — DavX5 reads a collection
    missing from discovery as a collection that is GONE, and may drop it
    from the phone (fixes of lot 4: the fail-open ``list_dossiers`` turned a
    Firestore blip into a 207 advertising zero dossier collections).

    Called from inside the ``except``, so the ERROR line carries the
    traceback. *check* is ``dossiers`` (the per-status queries) or
    ``ctags`` (the batched CTag read). Never a name or a title.
    """
    log_unexpected("dav root propfind read failed", check=check)
    log_dav_operation("propfind", "root", status_code=503,
                      reason="lecture_indisponible")
    resp = Response("Service Unavailable", status=503)
    resp.headers["Retry-After"] = "30"
    return resp


# ── Well-known redirects (DavX5 discovery) ────────────────────────────────

@dav_bp.route("/.well-known/carddav", methods=["GET", "PROPFIND"])
def well_known_carddav() -> Response:
    return redirect("/dav/", code=301)


@dav_bp.route("/.well-known/caldav", methods=["GET", "PROPFIND"])
def well_known_caldav() -> Response:
    return redirect("/dav/", code=301)


# ── DAV root (principal discovery) ────────────────────────────────────────

@dav_bp.route("/dav/", methods=["OPTIONS"])
@dav_auth_required
def dav_root_options() -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, PROPFIND"
    resp.headers["DAV"] = "1, 2, 3, addressbook, calendar-access"
    return resp


@dav_bp.route("/dav/", methods=["PROPFIND"])
@dav_auth_required
def dav_root_propfind() -> Response:
    """Return the principal resource and advertise available collections.

    DavX5 issues PROPFIND on /dav/ to discover which collections exist.
    We respond with the root plus child collection hrefs.
    """
    depth = request.headers.get("Depth", "0")
    add_attributes(
        **{
            "dav.collection_type": "root",
            "dav.operation": "propfind",
            "dav.depth": depth,
        }
    )
    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response("Corps de requête trop volumineux.", status=413)
    multistatus = make_multistatus()

    # Root resource
    root_resp = add_response(multistatus, "/dav/")
    prop = add_propstat(root_resp)

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))

    if propfind_requests_prop(body, dav_tag("displayname")):
        ET.SubElement(prop, dav_tag("displayname")).text = "Pallas Athena"

    if propfind_requests_prop(body, dav_tag("current-user-principal")):
        cup = ET.SubElement(prop, dav_tag("current-user-principal"))
        ET.SubElement(cup, dav_tag("href")).text = "/dav/"

    # Tell DavX5 where to find address book and calendars
    if propfind_requests_prop(body, dav_tag("addressbook-home-set")) or propfind_requests_prop(body, "{urn:ietf:params:xml:ns:carddav}addressbook-home-set"):
        from dav.xml_utils import carddav_tag
        ahs = ET.SubElement(prop, carddav_tag("addressbook-home-set"))
        ET.SubElement(ahs, dav_tag("href")).text = "/dav/addressbook/"

    if propfind_requests_prop(body, dav_tag("calendar-home-set")) or propfind_requests_prop(body, caldav_tag("calendar-home-set")):
        chs = ET.SubElement(prop, caldav_tag("calendar-home-set"))
        ET.SubElement(chs, dav_tag("href")).text = "/dav/"

    # Depth:1 — include child collections with proper resource types
    if depth == "1":
        from dav.xml_utils import carddav_tag
        from dav.carddav import ADDRESSBOOK_DISPLAY_NAME
        from dav.dossier_collections import (
            DOSSIER_COMPONENTS,
            GENERAL_DISPLAY_NAME,
            GENERAL_PATH,
            collection_display_name,
        )
        from dav.sync import (
            ACTIVE_DOSSIER_STATUSES,
            GENERAL_COLLECTION,
            get_ctags_bulk,
        )
        from models import dossier as dossier_model

        # -- Static collections (addressbook + \u00ab G\u00e9n\u00e9ral \u00bb) ----------------
        # \u00ab G\u00e9n\u00e9ral \u00bb carries every item belonging to no dossier \u2014 hearings,
        # tasks AND notes \u2014 with exactly the shape of a dossier collection.
        # It replaced the split /dav/calendar/ + /dav/tasks/ pair in July
        # 2026; both of those URLs are gone.
        static_collections = [
            ("/dav/addressbook/", ADDRESSBOOK_DISPLAY_NAME, "addressbook", None),
            (GENERAL_PATH, GENERAL_DISPLAY_NAME, "calendar", None),
        ]
        ctag_names = {
            "/dav/addressbook/": "parties",
            GENERAL_PATH: GENERAL_COLLECTION,
        }

        # Resolve active dossiers first so every collection's CTag can be
        # fetched in a single batched read instead of one get per dossier.
        # ONE query per status of dav.sync.ACTIVE_DOSSIER_STATUSES — the list
        # the collection's own handlers and the drain (services/dossier_dav)
        # read too, so discovery can never advertise a collection the drain
        # treats as closed, nor hide one it treats as open (lot 4a: this
        # listing used to hard-code its two statuses).
        #
        # STRICT on the query, TOLERANT per document (fixes of lot 4). A
        # failed read answers 503 + Retry-After and lists NOTHING — a 207
        # missing collections tells DavX5 they are gone. One document the
        # listing cannot read is skipped (logged by id) and the others are
        # listed: models.dossier.list_dossiers_by_status_strict skips what
        # the migration cannot read, the loop below what this listing cannot
        # render (a display name built from a non-string field). Everything
        # a collection's <D:response> needs is computed HERE, before any of
        # them is written, so a skipped dossier never leaves a half-built
        # response in the multistatus.
        active_dossiers: list[dict] = []
        try:
            for status in ACTIVE_DOSSIER_STATUSES:
                with firestore_span("query", "dossiers",
                                    filter=f"status={status}"):
                    active_dossiers += (
                        dossier_model.list_dossiers_by_status_strict(status))
        except Exception:
            return _root_read_unavailable("dossiers")
        add_attributes(**{"dav.dossier_count": len(active_dossiers)})

        seen_ids: set[str] = set()
        dossier_entries: list[tuple[str, str]] = []
        for dossier in active_dossiers:
            try:
                did = dossier["id"]
                if not isinstance(did, str) or not did:
                    raise ValueError("dossier without a usable id")
                if did in seen_ids:
                    continue
                # Shared with the collection's own PROPFIND: the two used to
                # build this string separately and had drifted (one prefixed
                # the product name, the other did not), so the label a
                # client showed depended on which response it had last read.
                display_name = collection_display_name(dossier)
            except Exception:
                did = dossier.get("id") if isinstance(dossier, dict) else None
                log_unexpected(
                    "dav root propfind: dossier skipped",
                    dossier_id=did if isinstance(did, str) else None,
                )
                continue
            seen_ids.add(did)
            dossier_entries.append((did, display_name))

        sync_names = list(ctag_names.values()) + [
            f"dossier:{did}" for did, _name in dossier_entries
        ]
        try:
            with firestore_span("get_all", "dav_sync"):
                ctags = get_ctags_bulk(sync_names)
        except Exception:
            return _root_read_unavailable("ctags")

        for coll_path, coll_name, coll_type, component in static_collections:
            child = add_response(multistatus, coll_path)
            child_prop = add_propstat(child)

            rt = ET.SubElement(child_prop, dav_tag("resourcetype"))
            ET.SubElement(rt, dav_tag("collection"))
            if coll_type == "addressbook":
                ET.SubElement(rt, carddav_tag("addressbook"))
            elif coll_type == "calendar":
                ET.SubElement(rt, caldav_tag("calendar"))

            # No product prefix: DavX5 already shows the account name above
            # the collection list, so "Pallas Athena \u2014 " was repeated on
            # every row and ate the width the actual label needs.
            ET.SubElement(child_prop, dav_tag("displayname")).text = coll_name

            if coll_type == "calendar":
                # « Général » is mixed-component like every dossier
                # collection, and reads the SAME constant — discovery and the
                # collection's own PROPFIND cannot promise different
                # capabilities.
                sccs = ET.SubElement(
                    child_prop,
                    caldav_tag("supported-calendar-component-set"),
                )
                names = (component,) if component else DOSSIER_COMPONENTS
                for name in names:
                    ET.SubElement(sccs, caldav_tag("comp")).set("name", name)

            sync_name = ctag_names.get(coll_path)
            if sync_name:
                ET.SubElement(child_prop, cs_tag("getctag")).text = (
                    ctags.get(sync_name, "")
                )

        # -- Dynamic per-dossier collections -------------------------------
        for did, display_name in dossier_entries:
            coll_path = f"/dav/dossier-{did}/"
            sync_name = f"dossier:{did}"

            child = add_response(multistatus, coll_path)
            child_prop = add_propstat(child)

            rt = ET.SubElement(child_prop, dav_tag("resourcetype"))
            ET.SubElement(rt, dav_tag("collection"))
            ET.SubElement(rt, caldav_tag("calendar"))

            ET.SubElement(child_prop, dav_tag("displayname")).text = display_name

            # Imported, never re-listed: discovery here and the collection's
            # own PROPFIND must advertise the identical component set, or the
            # client is promised a capability the collection then denies.
            sccs = ET.SubElement(
                child_prop,
                caldav_tag("supported-calendar-component-set"),
            )
            for component in DOSSIER_COMPONENTS:
                ET.SubElement(sccs, caldav_tag("comp")).set("name", component)

            ET.SubElement(child_prop, cs_tag("getctag")).text = (
                ctags.get(sync_name, "")
            )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")
