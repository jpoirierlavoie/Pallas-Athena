"""get_protocol_summary : la fenêtre « Prochaines » et la règle du jour même.

Premier fichier de tests du modèle protocole (PA-D05). Deux défauts épinglés :

1. « upcoming » comptait une fenêtre de 7 jours CODÉE EN DUR et documentée
   nulle part — un client MCP lisait {upcoming: 0} avec quatre étapes
   « à_venir » à 12 jours et concluait à un compteur cassé. La fenêtre
   voyage désormais dans la charge (upcoming_window_days) et
   next_deadline_date porte ce que l'appelant voulait vraiment savoir.

2. Deux définitions d'« en retard » coexistaient : le sommaire comparait à
   l'horloge murale (une étape échéant AUJOURD'HUI devenait en retard dès
   00:00 UTC) pendant que la rangée MCP appliquait la règle de la date de
   calendrier (« due today is not overdue » — épinglée par
   test_mcp_tools.test_step_and_task_due_today_are_not_overdue). Le même
   document émettait {overdue: 1} et {is_overdue: false} à la fois. La règle
   calendaire est maintenant la seule, partout (sommaire, check_overdue_steps,
   _days_remaining de la page détail, _overdue du tableau de bord).

Firestore bouchonné : seuls get_protocol_for_dossier et
list_protocols_for_dossier sont interceptés.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from models import protocol as pmod  # noqa: E402


def _midnight_utc(d) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _step(offset_days: int, status: str = "à_venir", sid: str = "s") -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "id": sid,
        "title": "Étape",
        "status": status,
        "deadline_date": _midnight_utc(today + timedelta(days=offset_days)),
    }


def _summary(monkeypatch, steps: list[dict]) -> dict:
    proto = {"id": "p1", "protocol_type": "cq_simplifié", "status": "actif",
             "steps": steps}
    monkeypatch.setattr(pmod, "get_protocol_for_dossier",
                        lambda did, active_only=True: proto)
    monkeypatch.setattr(pmod, "list_protocols_for_dossier", lambda did: [proto])
    return pmod.get_protocol_summary("d1")


def test_upcoming_compte_la_fenetre_et_la_nomme(monkeypatch):
    s = _summary(monkeypatch, [
        _step(2, sid="in-window"),
        _step(12, sid="out-of-window"),   # à_venir mais hors fenêtre
        _step(30, sid="far"),
    ])
    assert s["upcoming"] == 1
    assert s["upcoming_window_days"] == pmod.UPCOMING_WINDOW_DAYS == 7
    # La donnée que l'appelant voulait : la prochaine échéance ouverte,
    # fenêtre ou pas.
    expected = (datetime.now(timezone.utc).date() + timedelta(days=2))
    assert s["next_deadline_date"] == expected.isoformat()


def test_frontiere_de_fenetre_inclusive_a_sept_jours(monkeypatch):
    s = _summary(monkeypatch, [_step(7, sid="j7"), _step(8, sid="j8")])
    assert s["upcoming"] == 1  # J+7 inclus, J+8 exclu


def test_due_aujourdhui_est_upcoming_jamais_overdue(monkeypatch):
    """La contradiction inter-outils : le sommaire disait overdue dès 00:00
    UTC pendant que la rangée MCP disait is_overdue: false."""
    s = _summary(monkeypatch, [_step(0, sid="today")])
    assert s["overdue"] == 0
    assert s["upcoming"] == 1


def test_hier_est_overdue_meme_si_statut_pas_encore_bascule(monkeypatch):
    s = _summary(monkeypatch, [_step(-1, sid="late", status="à_venir")])
    assert s["overdue"] == 1
    assert s["upcoming"] == 0


def test_completee_ne_compte_nulle_part(monkeypatch):
    s = _summary(monkeypatch, [
        _step(-3, status="complété", sid="done-late"),
        _step(3, status="complété", sid="done-soon"),
    ])
    assert s["overdue"] == 0
    assert s["upcoming"] == 0
    assert s["completed"] == 2
    assert s["next_deadline_date"] is None


def test_branche_sans_protocole_porte_les_memes_cles(monkeypatch):
    monkeypatch.setattr(pmod, "get_protocol_for_dossier",
                        lambda did, active_only=True: None)
    monkeypatch.setattr(pmod, "list_protocols_for_dossier", lambda did: [])
    s = pmod.get_protocol_summary("d1")
    assert s["has_protocol"] is False
    assert s["upcoming_window_days"] == pmod.UPCOMING_WINDOW_DAYS
    assert s["next_deadline_date"] is None


# ── Lot 6 : le paramètre « today » optionnel qui laisse le web intact ────
#
# L'avocat a choisi de corriger le connecteur SANS déplacer le tableau de
# bord web. Le mécanisme est un paramètre facultatif dont le défaut
# reproduit exactement la règle historique. Les deux moitiés sont épinglées
# ici : une dérive du défaut est une régression web silencieuse, et une
# dérive de la branche datée remet le connecteur en contradiction avec ses
# propres rangées d'étapes.


def _patch(monkeypatch, steps: list[dict]) -> None:
    proto = {"id": "p1", "protocol_type": "cq_simplifié", "status": "actif",
             "steps": steps}
    monkeypatch.setattr(pmod, "get_protocol_for_dossier",
                        lambda did, active_only=True: proto)
    monkeypatch.setattr(pmod, "list_protocols_for_dossier", lambda did: [proto])


def test_sommaire_defaut_reste_la_regle_utc_historique(monkeypatch):
    """Argument omis = date de calendrier UTC, ce que la page web rendait
    avant ce mandat. C'est cette ligne qui garantit que le web n'a pas bougé."""
    _patch(monkeypatch, [_step(0)])          # une étape échéant AUJOURD'HUI
    s = pmod.get_protocol_summary("d1")
    assert s["overdue"] == 0                 # échéant aujourd'hui ≠ en retard
    assert s["upcoming"] == 1


def test_sommaire_accepte_un_jour_montrealais_injecte(monkeypatch):
    """Pendant la bande du soir, le jour montréalais n'a pas encore tourné :
    une étape échue « hier » en UTC n'est pas encore en retard pour l'avocat."""
    _patch(monkeypatch, [_step(-1)])         # échéance d'hier (UTC)
    utc_today = datetime.now(timezone.utc).date()
    # Sous la règle UTC, elle est déjà en retard…
    assert pmod.get_protocol_summary("d1")["overdue"] == 1
    # …sous un jour montréalais qui n'a pas encore tourné, non.
    montreal_hier = utc_today - timedelta(days=1)
    s = pmod.get_protocol_summary("d1", montreal_hier)
    assert s["overdue"] == 0
    assert s["upcoming"] == 1
