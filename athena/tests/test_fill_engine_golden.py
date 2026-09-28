"""The fill engine's existing callers produce the SAME document (lot 2A, T4).

Change Impact item 3. Step T4 touches the engine twice — ``numPr`` is no
longer inherited by the rich (Markdown) path (SPEC H.4 §13 L1), and
``fill_docx`` gains an optional ``report=`` channel (SPEC H.4 B9) — and
neither may move one byte of what the three callers of today print:

* Phase H — a gabarit letter/procedure (``routes/doc_templates.generate``):
  scalars, a multi-paragraph block in a NUMBERED host (the ``values`` path
  keeps Word's numbering — B4), a repeated field, a split run to heal,
  header and footer, a passthrough left verbatim;
* Phase H.2 — the note d'honoraires (``services/note_honoraires`` since
  lot 3a, ``routes/invoices`` before): repeating rows +
  conditional regions, built by the real ``build_invoice_context``;
* Phase H.3 — the note print (``routes/notes``): the rich path over a
  plain (un-numbered) host, every Markdown construct, an EMPTY note, and a
  host that shares its paragraph (the demotion path).

The digests below are SHA-256 of each entry's DECOMPRESSED bytes, recorded
on the base commit ``c81a467`` before the engine was touched. Decompressed,
not the archive: deflate output belongs to the zlib build (CI's Debian image
and a Windows CPython need not agree byte for byte), while the parts are
what Word reads. The archive itself is pinned in-process instead — the same
call with and without ``report=`` must return identical BYTES.

A failure here means an existing document changed. That is never an
acceptable side effect of this lot: re-derive a digest only for a change
that is DECIDED, and say which in the commit.
"""

import hashlib
import io
import os
import sys
import zipfile
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from defusedxml import ElementTree as ET  # noqa: E402

from utils.docx_fill import extract_placeholders, fill_docx  # noqa: E402
from utils.invoice_docx import build_invoice_context  # noqa: E402
from utils.note_docx import assemble_note_print_values, build_note_context  # noqa: E402
from utils.template_fields import classify_placeholders, fallback_value, manual_value  # noqa: E402

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
_STAMP = (2026, 9, 27, 12, 0, 0)          # fixed: deterministic archives
_NUM = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr>'
_TODAY = date(2026, 9, 27)
_FIRM = {
    "nom": "Me Jason Poirier Lavoie",
    "organisation": "Poirier Lavoie, avocat",
    "adresse_civique": "1 rue Test",
    "ville": "Montréal",
    "province": "Québec",
    "code_postal": "H1H 1H1",
    "telephone": "(514) 555-1234",
    "telecopieur": "(514) 555-9999",
    "courriel": "info@example.com",
}


def _p(text: str, ppr: str = "", rpr: str = "") -> str:
    ppr_xml = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    rpr_xml = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f"<w:p>{ppr_xml}<w:r>{rpr_xml}<w:t>{text}</w:t></w:r></w:p>"


def _tc(text: str) -> str:
    return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def _tr(*cells: str) -> str:
    return f"<w:tr>{''.join(cells)}</w:tr>"


def _tbl(*rows: str) -> str:
    return f"<w:tbl><w:tblPr/>{''.join(rows)}</w:tbl>"


def _body(*parts: str, sect: str = "") -> str:
    return (f'<?xml version="1.0"?><w:document {_W}><w:body>'
            + "".join(parts) + sect + "</w:body></w:document>")


def _part(tag: str, *paragraphs: str) -> str:
    return f'<?xml version="1.0"?><w:{tag} {_W}>' + "".join(paragraphs) + f"</w:{tag}>"


def _docx(entries: dict[str, str]) -> bytes:
    """A .docx whose entries carry a FIXED timestamp — so two fills of it
    can be compared as archives, byte for byte."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def _fingerprint(docx: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(docx)) as zf:
        return {
            f"{i.filename}|{i.compress_type}":
                hashlib.sha256(zf.read(i.filename)).hexdigest()
            for i in zf.infolist()
        }


# ── Phase H — gabarit ──────────────────────────────────────────────────


def _phase_h():
    split = (
        "<w:p><w:r><w:rPr><w:lang w:val=\"fr-CA\"/></w:rPr><w:t>Dossier {{dossier</w:t></w:r>"
        "<w:r><w:rPr><w:lang w:val=\"en-US\"/></w:rPr><w:t>.numero_cour}}</w:t></w:r></w:p>"
    )
    document = _body(
        _p("{{TRIBUNAL}}", ppr='<w:jc w:val="center"/>', rpr="<w:b/>"),
        _p("Réf. : {{référence_interne}} — {{tribunal}}"),
        split,
        _p("{{dossier.defendeurs_avec_adresse}}", ppr=_NUM),
        _p("{{civilité}}, {{objet_lettre}}"),
        _tbl(_tr(_tc("Objet"), _tc("{{objet_lettre}}"))),
        _p("{{tribunal}} &amp; fin"),
        sect='<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>',
    )
    docx = _docx({
        "[Content_Types].xml": _CT,
        "word/document.xml": document,
        "word/header1.xml": _part("hdr", _p("N/R {{dossier.numero_cour}}")),
        "word/footer1.xml": _part("ftr", _p("{{cabinet.nom}} — {{cabinet.telephone}}")),
        "word/styles.xml": _part("styles"),
        "docProps/core.xml": '<?xml version="1.0"?><cp:coreProperties '
                             'xmlns:cp="urn:x"/>',
    })
    values = {
        "TRIBUNAL": "COUR SUPÉRIEURE",
        "tribunal": "Cour supérieure",
        "référence_interne": "2026-001",
        "dossier.numero_cour": "500-17-123456-261",
        "dossier.defendeurs_avec_adresse": (
            "Alpha inc.\n1 rue A, Montréal (Québec) H1A 1A1"
            "\n\nBêta ltée\n2 rue B, Laval (Québec) H7A 2B2"
        ),
        "objet_lettre": "Mise en demeure <urgente> & \\g<0>",
        "cabinet.nom": "Me Jason Poirier Lavoie",
        "cabinet.telephone": "(514) 555-1234",
    }
    return docx, values, {}


# ── Phase H.2 — note d'honoraires ──────────────────────────────────────


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _phase_h2():
    honoraires = (
        _p("{{?si_honoraires}}")
        + _tbl(
            _tr(_tc("Date"), _tc("Description"), _tc("Temps")),
            _tr(_tc("{{#ligne_honoraire}}{{h.date}}"),
                _tc("{{h.description}}"), _tc("{{h.temps}}")),
        )
        + _p("{{/si_honoraires}}")
    )
    tx = (
        _p("{{?si_debours_tx}}")
        + _tbl(_tr(_tc("{{#ligne_debours_tx}}{{d.date}}"),
                   _tc("{{d.description}}"), _tc("{{d.cout}}")))
        + _p("{{/si_debours_tx}}")
    )
    ntx = (
        _p("{{?si_debours_ntx}}")
        + _tbl(_tr(_tc("{{#ligne_debours_ntx}}{{d.date}}"),
                   _tc("{{d.description}}"), _tc("{{d.cout}}")))
        + _p("{{/si_debours_ntx}}")
    )
    document = _body(
        _p("Facture {{facture.numero}} — {{destinataire.nom_complet}}"),
        _p("{{numero_dossier}} {{dossier.titre}}"),
        honoraires, tx, ntx,
        _p("Total {{facture.total_apres_taxes}} TPS {{facture.tps_montant}} "
           "TVQ {{facture.tvq_montant}} Solde {{facture.solde}}"),
        _p("{{privilège}} {{pièces_jointes}}"),
    )
    docx = _docx({"[Content_Types].xml": _CT, "word/document.xml": document})
    invoice = {
        "invoice_number": "2026-F031", "date": _dt(2026, 9, 1),
        "due_date": _dt(2026, 10, 1), "client_id": "c1",
        "billing_address": {"name": "Jean Tremblay", "street": "12 rue Principale",
                            "unit": "", "city": "Montréal", "province": "Québec",
                            "postal_code": "H2X 1Y6"},
        "subtotal_fees": 100000, "subtotal_expenses": 5000, "subtotal": 105000,
        "gst_rate": 500, "gst_amount": 5250, "qst_rate": 9975,
        "qst_amount": 10474, "total": 120724, "retainer_applied": 0,
        "amount_due": 120724, "gst_number": "123456789 RT0001",
        "qst_number": "1234567890 TQ0001",
    }
    items = [
        {"type": "fee", "date": _dt(2026, 8, 3), "description": "Recherche",
         "hours": 2.5, "rate": 25000, "amount": 62500, "taxable": True},
        {"type": "fee", "date": _dt(2026, 8, 4), "description": "Rédaction",
         "hours": 1.5, "rate": 25000, "amount": 37500, "taxable": True},
        {"type": "expense", "date": _dt(2026, 8, 5), "description": "Signification",
         "hours": None, "rate": None, "amount": 5000, "taxable": True},
    ]
    dossier = {"id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
               "clients": [], "opposing_parties": []}
    ctx = build_invoice_context(invoice, items, firm=_FIRM, destinataire=None,
                                dossier=dossier, today=_TODAY)
    # The service's assembly (services.note_honoraires.assemble_note_values
    # since lot 3a — the route's before), inline: importing it would pull
    # in the Firestore client.
    placeholders = extract_placeholders(docx)
    classification = classify_placeholders(placeholders)
    values = {}
    for name in placeholders:
        if name in ctx.values:
            values[name] = ctx.values[name]
        elif name in classification.auto:
            values[name] = fallback_value(name, is_auto=True)
        elif name in classification.manual:
            values[name] = manual_value(name)
    return docx, values, {"rows_by_region": ctx.rows, "conditions": ctx.conditions}


# ── Phase H.3 — note print (rich path, un-numbered host) ───────────────

_MARKDOWN = (
    "# Théorie de la cause\n\n"
    "Un paragraphe **gras**, *italique* et `code`, avec [un lien](https://canlii.ca).\n\n"
    "## Faits\n\n"
    "- premier fait\n- second fait\n  - sous-point\n\n"
    "1. un\n2. deux\n\n"
    "> une citation\n\n"
    "| Pièce | Date |\n|:---|---:|\n| P-1 | 2026-01-02 |\n| P-2 | 2026-02-03 |\n\n"
    "---\n\n"
    "```\nbloc de code\nsur deux lignes\n```\n"
)


def _note(content: str) -> dict:
    return {
        "id": "n1", "title": "Stratégie", "content": content,
        "category": "stratégie", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "Tremblay c. Lavoie",
        "created_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 6, 18, 0, tzinfo=timezone.utc),
    }


def _phase_h3(content: str = _MARKDOWN, *, shared_host: bool = False):
    host = (
        _p("Contenu : {{note.contenu}}")
        if shared_host else
        _p("{{note.contenu}}",
           ppr='<w:pStyle w:val="Corps"/><w:spacing w:after="120"/><w:jc w:val="both"/>',
           rpr='<w:rFonts w:ascii="Garamond" w:hAnsi="Garamond"/><w:sz w:val="24"/>')
    )
    document = _body(
        _p("{{note.titre}} ({{note.categorie}}) — {{note.date}} / {{note.date_maj}}"),
        host,
        _p("Dossier : {{note.dossier}} — {{cabinet.nom}}"),
        sect=('<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
              '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
              "</w:sectPr>"),
    )
    docx = _docx({"[Content_Types].xml": _CT, "word/document.xml": document})
    ctx = build_note_context(_note(content), dossier=None, firm=_FIRM, today=_TODAY)
    values = assemble_note_print_values(
        {"placeholders": extract_placeholders(docx)}, ctx)
    return docx, values, {"rich_values": ctx.rich_values}


SCENARIOS = {
    "phase_h_gabarit": _phase_h,
    "h2_note_honoraires": _phase_h2,
    "h3_note_print": _phase_h3,
    "h3_note_print_empty": lambda: _phase_h3(""),
    "h3_note_print_shared_host": lambda: _phase_h3(shared_host=True),
}

# Recorded on c81a467 (before T4). See the module docstring.
_CT_SHA = "36686deb401095cf0bc2b3271969129be531a72c53cd52e16a3a0c1ab3ef362b"
GOLDEN: dict[str, dict[str, str]] = {
    "h2_note_honoraires": {
        "[Content_Types].xml|8": _CT_SHA,
        "word/document.xml|8":
            "9befc3dcda0d7ad3e163d5ce5ea0775a13c0a1212f1a3e2327d2cfb679528762",
    },
    "h3_note_print": {
        "[Content_Types].xml|8": _CT_SHA,
        "word/document.xml|8":
            "cde22f197a7a8b69fac7f4e080f4899a1c9d820ba7b24994266273c692d83ff0",
    },
    "h3_note_print_empty": {
        "[Content_Types].xml|8": _CT_SHA,
        "word/document.xml|8":
            "3d79a8fc27d0777459fbe6415c229b6ac0821f5e2e60c016905ae4fee2be80a6",
    },
    "h3_note_print_shared_host": {
        "[Content_Types].xml|8": _CT_SHA,
        "word/document.xml|8":
            "31fab601fa0eb0d4199b1889e5a69f299a871771195a9403813847b34b2cf499",
    },
    "phase_h_gabarit": {
        "[Content_Types].xml|8": _CT_SHA,
        "word/document.xml|8":
            "f234fc5d57c4aae3eb602609d22434ece4bb9b865141690934dbdde74b2c0960",
        "word/header1.xml|8":
            "a0daa3e658be42961556d40ea513089005c0396f52460e6bff7bfb12f79702d8",
        "word/footer1.xml|8":
            "9268a6e021c5b6418ecb4e8caeee9ace291f0beedd21421f05c30ecdcc1735d2",
        "word/styles.xml|8":
            "a31d3e432bef683f43af3fe1b367ef025dc7550b6d7555623044ae1a1553c2bb",
        "docProps/core.xml|8":
            "497f227ab97ef96d57135f085fad259f3e1479a20add40e5277342bd55d463ac",
    },
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_existing_callers_print_the_same_document(name):
    docx, values, kwargs = SCENARIOS[name]()
    out = fill_docx(docx, values, **kwargs)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        ET.fromstring(zf.read("word/document.xml"))   # well-formed
    assert _fingerprint(out) == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_report_channel_never_moves_a_byte(name):
    """``report=`` is an OUTPUT channel only: the archive is byte-identical
    whether the caller asks for a report or not."""
    docx, values, kwargs = SCENARIOS[name]()
    plain = fill_docx(docx, values, **kwargs)
    assert fill_docx(docx, values, report=None, **kwargs) == plain
    report: dict = {}
    assert fill_docx(docx, values, report=report, **kwargs) == plain
    assert isinstance(report.get("demoted"), list)
