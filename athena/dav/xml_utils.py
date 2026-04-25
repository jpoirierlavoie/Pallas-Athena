"""Shared XML helpers for constructing DAV multistatus responses."""

import xml.etree.ElementTree as ET
from typing import Optional

from defusedxml.ElementTree import fromstring as _safe_fromstring

# ── Namespace URIs ────────────────────────────────────────────────────────
DAV_NS = "DAV:"
CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CS_NS = "http://calendarserver.org/ns/"

# Register namespace prefixes so ET serializes them readably.
ET.register_namespace("D", DAV_NS)
ET.register_namespace("C", CARDDAV_NS)
ET.register_namespace("CAL", CALDAV_NS)
ET.register_namespace("CS", CS_NS)


def dav_tag(local: str) -> str:
    """Return a Clark-notation tag in the DAV: namespace."""
    return f"{{{DAV_NS}}}{local}"


def carddav_tag(local: str) -> str:
    """Return a Clark-notation tag in the CardDAV namespace."""
    return f"{{{CARDDAV_NS}}}{local}"


def caldav_tag(local: str) -> str:
    """Return a Clark-notation tag in the CalDAV namespace."""
    return f"{{{CALDAV_NS}}}{local}"


def cs_tag(local: str) -> str:
    """Return a Clark-notation tag in the CalendarServer namespace."""
    return f"{{{CS_NS}}}{local}"


def make_multistatus() -> ET.Element:
    """Create the root <D:multistatus> element."""
    return ET.Element(dav_tag("multistatus"))


def add_response(
    multistatus: ET.Element,
    href: str,
    status_code: int = 200,
) -> ET.Element:
    """Add a <D:response> child with <D:href> to a multistatus element.

    Returns the <D:response> element so callers can append propstat blocks.
    """
    response = ET.SubElement(multistatus, dav_tag("response"))
    ET.SubElement(response, dav_tag("href")).text = href
    return response


def add_propstat(
    response: ET.Element,
    status_code: int = 200,
) -> ET.Element:
    """Add a <D:propstat> block with a <D:prop> and <D:status> to a response.

    Returns the <D:prop> element for callers to populate.
    """
    propstat = ET.SubElement(response, dav_tag("propstat"))
    prop = ET.SubElement(propstat, dav_tag("prop"))
    ET.SubElement(propstat, dav_tag("status")).text = (
        f"HTTP/1.1 {status_code} {'OK' if status_code == 200 else 'Not Found'}"
    )
    return prop


def add_status_response(
    multistatus: ET.Element,
    href: str,
    status_code: int,
    reason: str = "",
) -> None:
    """Add a <D:response> with just <D:href> and <D:status> (no propstat).

    Used for deleted resources in sync-collection reports.
    """
    response = ET.SubElement(multistatus, dav_tag("response"))
    ET.SubElement(response, dav_tag("href")).text = href
    status_text = reason or ("OK" if status_code == 200 else "Not Found")
    ET.SubElement(response, dav_tag("status")).text = (
        f"HTTP/1.1 {status_code} {status_text}"
    )


def serialize_multistatus(multistatus: ET.Element) -> str:
    """Serialize a multistatus element to an XML string with declaration."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + ET.tostring(multistatus, encoding="unicode")
    )


def parse_propfind_body(body: bytes) -> Optional[ET.Element]:
    """Parse PROPFIND request body XML.  Returns root element or None."""
    if not body or not body.strip():
        return None
    try:
        return _safe_fromstring(body)
    except ET.ParseError:
        return None


def propfind_requests_prop(body_root: Optional[ET.Element], tag: str) -> bool:
    """Check whether a PROPFIND body requests a specific property.

    If body_root is None (allprop), returns True.
    """
    if body_root is None:
        return True
    # <D:allprop/> means return everything
    if body_root.find(dav_tag("allprop")) is not None:
        return True
    prop_el = body_root.find(dav_tag("prop"))
    if prop_el is None:
        return True  # No <prop> specified — treat as allprop
    return prop_el.find(tag) is not None


def parse_report_body(body: bytes) -> Optional[ET.Element]:
    """Parse REPORT request body XML.  Returns root element or None."""
    if not body or not body.strip():
        return None
    try:
        return _safe_fromstring(body)
    except ET.ParseError:
        return None
