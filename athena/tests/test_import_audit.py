"""Les contrôles de reprise (mcp/import_audit.py) — couche PURE.

Aucun import de modèle, aucun Firestore, aucun mock : chaque prédicat prend
des dicts nus. Même discipline que tests/test_coverage.py.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from mcp import import_audit as ia


def _ctx(**over):
    base = {
        "dossier": {"id": "d1", "status": "actif", "closed_date": None},
        "time_entries": [],
        "expenses": [],
        "invoices": [],
    }
    base.update(over)
    return base


def _codes(ctx, **kw):
    return {f["code"] for f in ia.run_checks(ctx, **kw)}


def _entry(eid, *, amount=45000, invoiced=False, invoice_id="",
           description="Rédaction", date="2019-11-04"):
    return {"id": eid, "amount": amount, "invoiced": invoiced,
            "invoice_id": invoice_id, "description": description, "date": date}


def _invoice(iid="i1", *, number="2019-F014", status="payée", subtotal=45000,
             items=None):
    return {
        "invoice": {"id": iid, "invoice_number": number, "status": status,
                    "subtotal": subtotal, "total": subtotal},
        "line_items": items if items is not None else [
            {"id": "li1", "source_id": "e1", "amount": subtotal},
        ],
    }


# ── Le registre ────────────────────────────────────────────────────────────


def test_le_registre_est_complet_et_sans_doublon():
    assert len(ia.ALL_CODES) == len(set(ia.ALL_CODES)) == 7
    assert set(ia.SEVERITY_BY_CODE) == set(ia.ALL_CODES)
    assert set(ia.LABEL_BY_CODE) == set(ia.ALL_CODES)
    for code in ia.ALL_CODES:
        assert ia.SEVERITY_BY_CODE[code] in (ia.MANQUEMENT, ia.SIGNALEMENT)
        assert ia.LABEL_BY_CODE[code]


def test_un_dossier_propre_ne_declenche_rien():
    assert _codes(_ctx()) == set()


def test_chaque_detail_renvoie_a_l_application():
    """Un constat est une OBSERVATION : le connecteur ne peut ni supprimer un
    doublon, ni annuler une facture, ni la sortir du brouillon."""
    ctx = _ctx(
        dossier={"id": "d1", "status": "fermé", "closed_date": None},
        time_entries=[_entry("e1"), _entry("e2")],
        invoices=[_invoice(status="brouillon")],
    )
    findings = ia.run_checks(ctx)
    assert findings
    for f in findings:
        assert f["detail"]
        assert "application" in f["detail"].lower() or "facturez" in f["detail"].lower()


# ── IMP-01 ────────────────────────────────────────────────────────────────


def test_imp01_travail_non_facture_sur_dossier_ferme():
    ctx = _ctx(dossier={"id": "d1", "status": "fermé", "closed_date": "x"},
               time_entries=[_entry("e1")])
    assert "IMP-01" in _codes(ctx)


def test_imp01_se_tait_sur_un_dossier_actif():
    assert "IMP-01" not in _codes(_ctx(time_entries=[_entry("e1")]))


def test_imp01_se_tait_quand_tout_est_facture():
    ctx = _ctx(dossier={"id": "d1", "status": "fermé", "closed_date": "x"},
               time_entries=[_entry("e1", invoiced=True, invoice_id="i1")],
               invoices=[_invoice()])
    assert "IMP-01" not in _codes(ctx)


# ── IMP-02 ────────────────────────────────────────────────────────────────


def test_imp02_total_incoherent():
    ctx = _ctx(invoices=[_invoice(subtotal=45000, items=[
        {"id": "li1", "source_id": "e1", "amount": 40000}])])
    assert "IMP-02" in _codes(ctx)


def test_imp02_se_tait_quand_les_postes_sont_illisibles():
    """Des postes vides sur une facture non nulle, c'est une LECTURE qui a
    échoué, pas un total faux. Accuser la donnée d'un problème de transport
    est précisément ce qu'un rapport ne doit jamais faire."""
    ctx = _ctx(invoices=[_invoice(subtotal=45000, items=[])])
    assert "IMP-02" not in _codes(ctx)


def test_imp02_tolere_un_poste_d_ajustement():
    ctx = _ctx(invoices=[_invoice(subtotal=40000, items=[
        {"id": "li1", "source_id": "e1", "amount": 45000},
        {"id": "li2", "source_id": "", "amount": -5000},
    ])])
    assert "IMP-02" not in _codes(ctx)


# ── IMP-03 ────────────────────────────────────────────────────────────────


def test_imp03_poste_orphelin():
    ctx = _ctx(time_entries=[_entry("e1")],
               invoices=[_invoice(items=[
                   {"id": "li1", "source_id": "disparue", "amount": 45000}])])
    assert "IMP-03" in _codes(ctx)


def test_imp03_ignore_le_poste_d_ajustement_qui_n_a_jamais_de_source():
    """C'est le SEUL poste du système sans source_id, par construction."""
    ctx = _ctx(time_entries=[_entry("e1")],
               invoices=[_invoice(items=[
                   {"id": "li1", "source_id": "e1", "amount": 45000},
                   {"id": "li2", "source_id": "", "amount": -5000}])])
    assert "IMP-03" not in _codes(ctx)


def test_imp03_et_imp06_se_suppriment_quand_les_sources_sont_tronquees():
    """Une fenêtre de lecture tronquée ferait passer les postes d'une facture
    pour orphelins. Fabriquer « source introuvable » à partir d'une frontière
    de pagination accuserait une reprise qui va bien."""
    ctx = _ctx(invoices=[_invoice(items=[
        {"id": "li1", "source_id": "e1", "amount": 45000}])])
    assert "IMP-03" in _codes(ctx)
    assert "IMP-03" not in _codes(
        ctx, skip=frozenset(ia.NEEDS_COMPLETE_SOURCES)
    )
    assert ia.NEEDS_COMPLETE_SOURCES == ("IMP-03", "IMP-06")


# ── IMP-04 ────────────────────────────────────────────────────────────────


def test_imp04_doublons_de_reprise():
    ctx = _ctx(time_entries=[_entry("e1"), _entry("e2")])
    assert "IMP-04" in _codes(ctx)


def test_imp04_ne_confond_pas_deux_lignes_legitimes():
    ctx = _ctx(time_entries=[
        _entry("e1", description="Rédaction"),
        _entry("e2", description="Appel du client"),
    ])
    assert "IMP-04" not in _codes(ctx)


def test_imp04_ignore_les_lignes_sans_description():
    """Grouper sur une description vide rassemblerait n'importe quoi."""
    ctx = _ctx(time_entries=[_entry("e1", description=""),
                             _entry("e2", description="")])
    assert "IMP-04" not in _codes(ctx)


def test_imp04_rapproche_temps_et_debours():
    ctx = _ctx(time_entries=[_entry("e1", description="Timbre", amount=5000)],
               expenses=[_entry("x1", description="Timbre", amount=5000)])
    assert "IMP-04" in _codes(ctx)


# ── IMP-05 / IMP-06 / IMP-07 ──────────────────────────────────────────────


def test_imp05_dossier_ferme_sans_date():
    ctx = _ctx(dossier={"id": "d1", "status": "fermé", "closed_date": None})
    assert "IMP-05" in _codes(ctx)
    ctx = _ctx(dossier={"id": "d1", "status": "fermé", "closed_date": "2019-12-01"})
    assert "IMP-05" not in _codes(ctx)


def test_imp06_entree_facturee_dont_la_facture_manque():
    ctx = _ctx(time_entries=[_entry("e1", invoiced=True, invoice_id="fantome")])
    assert "IMP-06" in _codes(ctx)


def test_imp06_se_tait_quand_la_facture_est_la():
    ctx = _ctx(time_entries=[_entry("e1", invoiced=True, invoice_id="i1")],
               invoices=[_invoice("i1")])
    assert "IMP-06" not in _codes(ctx)


def test_imp07_facture_encore_au_brouillon_dit_ce_que_ca_casse():
    ctx = _ctx(invoices=[_invoice(status="brouillon")])
    findings = {f["code"]: f for f in ia.run_checks(ctx)}
    assert "IMP-07" in findings
    detail = findings["IMP-07"]["detail"]
    # Le coût que la décision D-4 laisse au juriste doit être nommé, pas
    # laissé à découvrir : le journal du Barreau et le sommaire du dossier
    # restent faux tant que la promotion manuelle n'est pas faite.
    assert "Journal des honoraires" in detail
    assert "paiement" in detail


def test_imp07_se_tait_sur_une_facture_promue():
    assert "IMP-07" not in _codes(_ctx(invoices=[_invoice(status="payée")]))
