"""La porte étroite côté application : le formulaire de reclassement.

Deux choses seulement, mais ce sont celles qui échouent en silence.

1. Le gabarit ne porte AUCUN champ qui écrirait autre chose que la phase.
   Le mur `invoiced` ne tient dans l'application que parce que le seul
   formulaire atteignable depuis une ligne facturée est celui-ci ; y
   glisser un `<input name="hours">` rouvrirait tout, et le modèle
   n'aurait rien à refuser puisque la route ne lui transmet que la phase.
2. Les routes existent et ne se marchent pas dessus — ``/temps/<id>/phase``
   et ``/temps/depenses/<id>/phase`` cohabitent comme leurs jumelles
   ``/edit``, et le formulaire d'édition ordinaire pointe bien vers elles.

Le gabarit est rendu bloc ``content`` seul (Jinja compile ``{% extends %}``
paresseusement) : ni Flask, ni base.html, ni session.
"""

import os
import re
import sys
from datetime import datetime, timezone

import pytest
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from utils import phases
from utils.format_fr import format_cents_fr
from utils.icons import ms

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES_DIR = os.path.join(_ROOT, "templates")
TEMPLATE = "time_expenses/phase_form.html"


def _entry(**over) -> dict:
    doc = {
        "id": "e1", "dossier_id": "d1", "dossier_file_number": "2025-001",
        "dossier_title": "Tremblay c. Lavoie",
        "date": datetime(2026, 3, 4, tzinfo=timezone.utc),
        "description": "Rédaction de la défense", "hours": 1.5,
        "rate": 30000, "amount": 45000, "billable": True,
        "invoiced": True, "invoice_id": "i1",
        "phase": "", "sous_phase": "",
    }
    doc.update(over)
    return doc


def _expense(**over) -> dict:
    doc = {
        "id": "x1", "dossier_id": "d1", "dossier_file_number": "2025-001",
        "date": datetime(2026, 3, 4, tzinfo=timezone.utc),
        "description": "Timbre judiciaire", "category": "timbre_judiciaire",
        "amount": 5000, "taxable": True, "invoiced": True,
        "phase": "CTS", "sous_phase": "CTS-02",
    }
    doc.update(over)
    return doc


def _render(item: dict, kind: str, **over) -> str:
    env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)
    env.globals.update(
        url_for=lambda endpoint, **kw: "/" + endpoint,
        csrf_token=lambda: "tok",
        ms=ms,
    )
    env.filters["cents_fr"] = lambda c: format_cents_fr(c) if c is not None else ""
    ctx = {
        "item": item,
        "kind": kind,
        "errors": [],
        "return_to": "",
        "phase_recents": ["INT-01"],
        "phases_payload": phases.form_payload(),
        "phase_labels": phases.PHASE_LABELS,
        "sous_phase_labels": phases.SOUS_PHASE_LABELS,
        "category_labels": {"timbre_judiciaire": "Timbre judiciaire"},
        "today": "2026-08-18",
    }
    ctx.update(over)
    tpl = env.get_template(TEMPLATE)
    return "".join(tpl.blocks["content"](tpl.new_context(ctx)))


# ── 1. Le formulaire n'écrit QUE la phase ────────────────────────────────


_ALLOWED_NAMES = {"csrf_token", "return_to", "phase", "sous_phase"}


@pytest.mark.parametrize(
    "item,kind", [(_entry(), "time_entry"), (_expense(), "expense")],
    ids=["temps", "debourse"],
)
def test_le_formulaire_ne_poste_que_la_phase(item, kind):
    """Le seul rempart côté application. La route ne transmet que la phase,
    donc un champ de plus ici ne serait pas refusé : il serait ignoré —
    et la prochaine main qui « brancherait » ce champ rouvrirait le mur
    sans qu'aucun test ne tombe. D'où l'épingle sur le gabarit."""
    html = _render(item, kind)
    names = set(re.findall(r'name="([^"]+)"', html))
    assert names <= _ALLOWED_NAMES, names - _ALLOWED_NAMES


@pytest.mark.parametrize(
    "item,kind", [(_entry(), "time_entry"), (_expense(), "expense")],
    ids=["temps", "debourse"],
)
def test_le_recapitulatif_montre_ce_qui_ne_bougera_pas(item, kind):
    html = _render(item, kind)
    assert item["description"] in html
    assert "2026-03-04" in html
    assert format_cents_fr(item["amount"]) in html
    assert "2025-001" in html


def test_une_ligne_facturee_est_annoncee_comme_telle():
    html = _render(_entry(), "time_entry")
    assert "déjà portée à une facture" in html
    # …et la raison pour laquelle la phase reste corrigeable est dite.
    assert "ne figure sur aucune facture" in html


def test_une_ligne_non_facturee_n_affiche_pas_l_avertissement():
    html = _render(_entry(invoiced=False), "time_entry")
    assert "déjà portée à une facture" not in html


def test_le_selecteur_exige_une_phase():
    """Reclasser, c'est ASSIGNER un code : « Hors phase » (HOR) est la
    réponse du vocabulaire pour l'inclassable, pas un champ vide."""
    html = _render(_entry(), "time_entry")
    select = re.search(r'<select[^>]*name="phase"[^>]*>', html).group(0)
    assert "required" in select
    assert "Non renseignée" not in html.split("<select")[1].split("</select>")[0]


def test_la_phase_actuelle_est_affichee():
    assert "Contestation" in _render(_expense(), "expense")
    assert "Non renseignée" in _render(_entry(), "time_entry")


# ── 2. Le câblage ────────────────────────────────────────────────────────


def _routes_source() -> str:
    with open(os.path.join(_ROOT, "routes", "time_expenses.py"),
              encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("rule", [
    '@time_expenses_bp.route("/<entry_id>/phase")',
    '@time_expenses_bp.route("/<entry_id>/phase", methods=["POST"])',
    '@time_expenses_bp.route("/depenses/<expense_id>/phase")',
    '@time_expenses_bp.route("/depenses/<expense_id>/phase", methods=["POST"])',
])
def test_les_quatre_routes_existent(rule):
    assert rule in _routes_source()


def test_les_routes_de_reclassement_n_appellent_que_l_ecrivain_etroit():
    """Si elles passaient par update_time_entry, elles hériteraient de son
    refus — et du set() de document complet qu'il exécute."""
    src = _routes_source()
    bloc = src.split("# ── Reclassement de phase")[1].split(
        "_TIME_EXPORT_COLUMNS_CSV")[0]
    assert "set_time_entry_phase(" in bloc and "set_expense_phase(" in bloc
    assert "update_time_entry(" not in bloc
    assert "update_expense(" not in bloc


@pytest.mark.parametrize("template,endpoint", [
    ("time_expenses/time_form.html", "time_expenses.time_entry_phase_edit"),
    ("time_expenses/expense_form.html", "time_expenses.expense_phase_edit"),
])
def test_le_formulaire_d_edition_offre_la_porte_etroite(template, endpoint):
    with open(os.path.join(_TEMPLATES_DIR, template), encoding="utf-8") as fh:
        src = fh.read()
    assert endpoint in src
    # …et seulement dans la branche « facturée » du bloc d'actions : une
    # ligne encore modifiable se reclasse depuis le formulaire ordinaire.
    actions = src.split("{# Actions #}")[1]
    facturee, modifiable = actions.split("{% else %}")[:2]
    assert endpoint in facturee
    assert endpoint not in modifiable


def test_le_journal_ne_porte_que_des_codes():
    """`log_dossier_event` n'auto-caviarde ni description ni montant : la
    ligne ne doit en citer aucun."""
    src = _routes_source()
    # L'APPEL seul — un docstring qui explique ce qu'on ne journalise
    # pas cite forcément ce qu'on ne journalise pas.
    bloc = src.split("log_dossier_event(")[1].split("    )")[0]
    for interdit in ("description", "amount", "hours", "rate", "title"):
        assert interdit not in bloc, interdit
    for attendu in ("entity_type", "entity_id", "from_sous_phase",
                    "to_sous_phase", "invoiced"):
        assert attendu in bloc, attendu
