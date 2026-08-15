"""Unified accounting hub — « Comptabilité ».

A PRESENTATION-ONLY composer over the two accounting modules. It reads the
two EXISTING firm snapshots (``models/trust.get_firm_trust_snapshot`` — also
under the MCP ``get_trust_snapshot`` outputSchema contract, so its shape is
read here and never altered — and ``models/admin_ledger
.get_firm_admin_snapshot``) and routes every action to the modules' own
screens. Doctrine, pinned by tests:

- READ-ONLY forever: no POST will ever live here. Opening, closing and
  reconciling an account happen on the module's own screens, where their
  guards live (trust's zero-balance close vs admin's free close).
- Fail-closed PER SECTION: a read failure on one side renders an
  « indisponible » panel for that section only — with NO creation CTA, so an
  outage can never read as « no accounts » and invite a duplicate — while
  the other section renders normally. Never a page-level 500.
- The two registers stay visually distinct sections: their balances are
  incommensurable figures (client funds held in trust, firm cash, card debt
  displayed sign-flipped as « Solde dû »), so the balance label travels PER
  ROW and no combined total is ever shown.
- No new Firestore query may be added here (the snapshots are the budget),
  and no hardcoded module path — every link goes through url_for.
"""

from flask import Blueprint, render_template

from auth import login_required
from models import admin_ledger as al
from models import trust
from utils.logging_setup import log_unexpected

comptabilite_bp = Blueprint("comptabilite", __name__, url_prefix="/comptabilite")


@comptabilite_bp.route("/")
@login_required
def hub() -> str:
    """The unified account listing — two sections, one per accounting system.

    Each snapshot is wrapped in its own try/except: the models deliberately
    fail CLOSED (a partial account list must never look complete), and it is
    this caller's job to turn that into a bounded, explicit outage for one
    section instead of a 500 for both. Log messages carry no account name —
    names can embed client names, which the redaction filter does not
    auto-scrub.
    """
    trust_snapshot: dict = {}
    trust_error = False
    try:
        trust_snapshot = trust.get_firm_trust_snapshot()
    except Exception:
        log_unexpected("comptabilite hub: trust snapshot read failed")
        trust_error = True

    admin_snapshot: dict = {}
    admin_error = False
    try:
        admin_snapshot = al.get_firm_admin_snapshot()
    except Exception:
        log_unexpected("comptabilite hub: admin snapshot read failed")
        admin_error = True

    return render_template(
        "comptabilite/index.html",
        trust_accounts=trust_snapshot.get("accounts", []),
        trust_error=trust_error,
        admin_accounts=admin_snapshot.get("accounts", []),
        admin_error=admin_error,
        trust_type_labels=trust.ACCOUNT_TYPE_LABELS,
        admin_type_labels=al.ACCOUNT_TYPE_LABELS,
    )
