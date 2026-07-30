"""Miroir unidirectionnel des audiences Athéna → calendrier Outlook du juriste.

Pure Graph glue over ``utils/graph`` — no Firestore, fully unit-testable. The
sweep itself (diff, counters, cron guard) lives in ``routes/taches_outlook.py``;
this module owns the event payload, the marker, and the three Graph calls.

Design invariants (see CLAUDE.md → Known Gotchas):

- **Firestore-read-only by design.** The hearing ↔ Outlook mapping lives in
  the mirrored event itself (extended property ``MIROIR_PROP_ID``, value
  ``"{hearing_id}|{etag}"``), never on the hearing document — storing an
  outlook_event_id there would regenerate ``etag``/``updated_at`` (DavX5
  re-sync churn) and entangle with the Bookings import's ``graph_*`` fields.
- **Loop prevention.** Every mirrored event carries the extended property AND
  the « Pallas Athéna » category; ``graph_calendrier.mot_cle_correspondant``
  refuses marked events before its keyword logic, so the Bookings sync can
  never re-import a mirror. Destructive mirror operations key on the
  **property alone** (an event merely categorized by hand is never touched).
- **Never a key ``attendees``.** An attendee on an app-created event makes
  Exchange send a meeting invitation — the mirror must stay a silent copy.

MAIN SERVICE ONLY — a call from the portal process raises GraphNotConfigured.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from config import Config
from utils import graph
from utils.graph_calendrier import (
    EXPAND_MIROIR,
    MIROIR_CATEGORIE,
    MIROIR_PROP_ID,
    _parse_graph_dt,
)

logger = logging.getLogger(__name__)

# All-day events: Graph exige minuit DANS LE FUSEAU DÉCLARÉ (fin exclusive,
# ≥ 1 jour). Minuit UTC s'afficherait la veille au soir en heure de l'Est —
# la journée déborderait sur DEUX jours dans Outlook. America/Toronto est la
# zone IANA canonique de l'Est canadien (America/Montreal n'est qu'un alias) ;
# si Graph la refusait un jour, le repli documenté est le nom Windows
# « Eastern Standard Time ».
_TZ_JOURNEE = "America/Toronto"

# Les champs que le diff compare ; rien de plus. Le corps (body) est
# volontairement absent : Outlook réécrit texte → HTML à sa guise, et le
# comparer condamnerait chaque cycle à un PATCH sans effet.
_SELECT = (
    "id,subject,start,end,isAllDay,location,"
    "reminderMinutesBeforeStart,isReminderOn,showAs,categories"
)


def valeur_marqueur(h: dict) -> str:
    """La valeur de la propriété étendue : ``"{hearing_id}|{etag}"``."""
    return f"{h.get('id') or ''}|{h.get('etag') or ''}"


def lire_marqueur(ev: dict) -> tuple[str, str]:
    """(hearing_id, etag) du marqueur porté par *ev*, ou ``("", "")``.

    L'id de propriété est comparé casse pliée (Graph normalise la casse des
    GUID) ; une valeur malformée (sans « | » ou sans id) vaut absence — un
    événement au marqueur corrompu ne doit jamais piloter une suppression.
    """
    prop_id = MIROIR_PROP_ID.casefold()
    for prop in ev.get("singleValueExtendedProperties") or []:
        if (prop.get("id") or "").casefold() == prop_id:
            valeur = prop.get("value") or ""
            hid, sep, etag = valeur.partition("|")
            if sep and hid:
                return hid, etag
            return ("", "")
    return ("", "")


def _dates_journee(h: dict) -> tuple[date, date]:
    """(début, fin exclusive) d'une audience all-day.

    La convention du modèle : start/end à minuit UTC, et ``create_hearing``
    pose ``end = start + 1 h`` quand le formulaire n'en donne pas — d'où la
    règle : fin exclusive = ``end.date()`` quand elle dépasse le jour de
    début, sinon ``start.date() + 1 jour`` (l'exclusivité DTEND du DAV).
    """
    debut = h["start_datetime"].date()
    end = h.get("end_datetime")
    if isinstance(end, datetime) and end.date() > debut:
        return debut, end.date()
    return debut, debut + timedelta(days=1)


def _corps(h: dict) -> str:
    """Le corps texte minimal — les conventions DESCRIPTION du DAV.

    Jamais les notes : le corps est renvoyé en entier à chaque PATCH, on n'y
    met que ce qui identifie l'audience.
    """
    lignes = []
    if h.get("dossier_file_number"):
        lignes.append(f"N/R : {h['dossier_file_number']}")
    if h.get("modalite") == "visioconférence" and h.get("conference_uri"):
        lignes.append(f"Visioconférence: {h['conference_uri']}")
    return "\n".join(lignes)


def construire_charge(h: dict, avec_transaction: bool = False) -> dict:
    """La charge Graph d'un événement miroir (création ET correction).

    ``avec_transaction`` n'est vrai qu'à la CRÉATION : ``transactionId`` est
    immuable et un PATCH qui le porte est refusé par Graph. Unique par
    version d'audience (id + etag), il dédoublonne un retry HTTP — un rejeu
    répond 409, compté en erreur, auto-réparé au cycle suivant.
    """
    charge: dict = {
        "subject": h.get("title") or "",
        "body": {"contentType": "text", "content": _corps(h)},
        "isAllDay": bool(h.get("all_day")),
        "showAs": "busy",
        "isReminderOn": bool(h.get("reminder_minutes")),
        "reminderMinutesBeforeStart": int(h.get("reminder_minutes") or 0),
        # displayName TOUJOURS présent, même vide : un PATCH qui omet la clé
        # laisserait l'ancien lieu dans Outlook, que le diff re-signalerait à
        # chaque cycle — un PATCH perpétuel sans effet.
        "location": {"displayName": h.get("location") or ""},
        "categories": [MIROIR_CATEGORIE],
        "singleValueExtendedProperties": [
            {"id": MIROIR_PROP_ID, "value": valeur_marqueur(h)}
        ],
    }
    if h.get("all_day"):
        debut, fin = _dates_journee(h)
        charge["start"] = {
            "dateTime": f"{debut.isoformat()}T00:00:00",
            "timeZone": _TZ_JOURNEE,
        }
        charge["end"] = {
            "dateTime": f"{fin.isoformat()}T00:00:00",
            "timeZone": _TZ_JOURNEE,
        }
    else:
        start = h["start_datetime"].astimezone(timezone.utc)
        end = h.get("end_datetime")
        if not isinstance(end, datetime):
            end = start + timedelta(hours=1)
        charge["start"] = {
            "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "UTC",
        }
        charge["end"] = {
            "dateTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "UTC",
        }
    if avec_transaction:
        charge["transactionId"] = f"pallas-{h.get('id') or ''}-{h.get('etag') or ''}"
    return charge


def champs_cibles(h: dict) -> dict:
    """L'instantané normalisé de ce que l'événement miroir DOIT montrer.

    Événement minuté : des INSTANTS UTC, jamais des chaînes (calendarView
    rend l'heure en UTC quelle que soit la zone d'écriture). Journée entière :
    des DATES — que Graph renvoie minuit UTC ou minuit local converti (04:00Z),
    la date calendaire est la même, et comparer l'instant fabriquerait un
    PATCH perpétuel selon la représentation que Graph choisit.
    """
    rappel_actif = bool(h.get("reminder_minutes"))
    if h.get("all_day"):
        debut, fin = _dates_journee(h)
        start: object = debut
        end: object = fin
    else:
        s = h.get("start_datetime")
        e = h.get("end_datetime")
        if not isinstance(e, datetime) and isinstance(s, datetime):
            e = s + timedelta(hours=1)
        # Tronqué à la seconde, comme la charge l'écrit (strftime %S) : une
        # audience portant des microsecondes divergerait sinon À CHAQUE cycle
        # de ce que Graph renvoie — un PATCH perpétuel sans effet.
        start = (
            s.astimezone(timezone.utc).replace(microsecond=0)
            if isinstance(s, datetime)
            else None
        )
        end = (
            e.astimezone(timezone.utc).replace(microsecond=0)
            if isinstance(e, datetime)
            else None
        )
    return {
        "subject": h.get("title") or "",
        "start": start,
        "end": end,
        "all_day": bool(h.get("all_day")),
        "location": h.get("location") or "",
        "rappel_actif": rappel_actif,
        "rappel_minutes": int(h.get("reminder_minutes") or 0) if rappel_actif else 0,
        "show_as": "busy",
        # La catégorie est la moitié du garde anti-boucle : retirée à la main
        # dans Outlook, le diff la voit et le PATCH la restaure.
        "categorie": True,
    }


def champs_observes(ev: dict) -> dict:
    """Le même instantané, lu d'un événement Graph."""
    rappel_actif = bool(ev.get("isReminderOn"))
    start: object = _parse_graph_dt(ev.get("start"))
    end: object = _parse_graph_dt(ev.get("end"))
    if ev.get("isAllDay"):
        start = start.date() if isinstance(start, datetime) else None
        end = end.date() if isinstance(end, datetime) else None
    return {
        "subject": ev.get("subject") or "",
        "start": start,
        "end": end,
        "all_day": bool(ev.get("isAllDay")),
        "location": ((ev.get("location") or {}).get("displayName")) or "",
        "rappel_actif": rappel_actif,
        # Rappel éteint → Graph renvoie des minutes arbitraires ; on les
        # neutralise des deux côtés pour ne pas fabriquer une divergence.
        "rappel_minutes": int(ev.get("reminderMinutesBeforeStart") or 0)
        if rappel_actif
        else 0,
        "show_as": ev.get("showAs") or "",
        "categorie": MIROIR_CATEGORIE in (ev.get("categories") or []),
    }


def doit_corriger(h: dict, ev: dict) -> bool:
    """PATCH requis ? Etag stampé en retard (l'audience a changé dans Athéna)
    OU champ visible divergent (édition faite dans Outlook — Athéna écrase,
    décision utilisateur 2026-07-29)."""
    _hid, etag_stampe = lire_marqueur(ev)
    if etag_stampe != (h.get("etag") or ""):
        return True
    return champs_cibles(h) != champs_observes(ev)


def lister_miroirs(debut: datetime, fin: datetime) -> list[dict]:
    """Les événements miroir (propriété présente) dans [debut, fin].

    Filtre CLIENT sur la propriété : un ``$filter`` serveur d'existence sur
    une propriété étendue n'est pas fiable dans Graph, et le calendarView
    complet est déjà le coût de la fenêtre. La catégorie seule ne suffit PAS
    ici — elle qualifie pour le garde d'import, jamais pour une suppression.
    """
    params = {
        "startDateTime": debut.astimezone(timezone.utc).isoformat(),
        "endDateTime": fin.astimezone(timezone.utc).isoformat(),
        "$select": _SELECT,
        "$expand": EXPAND_MIROIR,
        "$top": 100,
    }
    data = graph.graph_get(
        f"/users/{Config.BOOKINGS_JURISTE_UPN}/calendarView", params=params
    )
    return [ev for ev in data.get("value") or [] if lire_marqueur(ev)[0]]


def creer_miroir(h: dict) -> None:
    """POST l'événement miroir dans le calendrier principal du juriste."""
    graph.graph_post(
        f"/users/{Config.BOOKINGS_JURISTE_UPN}/events",
        construire_charge(h, avec_transaction=True),
    )


def corriger_miroir(graph_event_id: str, h: dict) -> None:
    """PATCH l'événement miroir sur les valeurs d'Athéna (etag stampé inclus)."""
    graph.graph_patch(
        f"/users/{Config.BOOKINGS_JURISTE_UPN}/events/{graph_event_id}",
        construire_charge(h),
    )


def supprimer_miroir(graph_event_id: str) -> None:
    """DELETE un événement miroir (orphelin ou doublon)."""
    graph.graph_delete(
        f"/users/{Config.BOOKINGS_JURISTE_UPN}/events/{graph_event_id}"
    )
