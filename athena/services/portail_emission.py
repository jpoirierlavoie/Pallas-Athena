"""Émission et renvoi des invitations du portail client (spec L1 §6.2).

MAIN SERVICE ONLY — composes Firebase Auth (user + custom claim), the
« portail » invitation model, the Firebase email-link generation, and the
Graph outbound email. The portal service never imports this module.

Return convention: ``lien_manuel`` is non-empty when the sign-in link could
NOT be emailed (Graph unconfigured, or a send failure) — the invitation
exists and the juriste hands the link over manually. This keeps the feature
usable before the Entra registration (§12.7) is done.
"""

import logging
from typing import Optional

from firebase_admin import auth as fb_auth
from flask import render_template

from client.config import PORTAIL_HOST, PORTAIL_MAX_FILE_MB
from config import Config
from models import portail_invitation as inv_model
from tz import to_mtl
from utils import courriel
from utils.format_fr import format_date_fr
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_portail_event
from utils.validators import normalize_email

logger = logging.getLogger(__name__)


def _generer_lien(email: str, inv_id: str) -> str:
    """Firebase email-link (oobCode) — single-use, bound to the email.

    Raises on failure (e.g. the « email link » provider not enabled in the
    Firebase console, ops checklist §12.8).
    """
    acs = fb_auth.ActionCodeSettings(
        url=f"https://{PORTAIL_HOST}/entree?i={inv_id}",
        handle_code_in_app=True,
    )
    return fb_auth.generate_sign_in_with_email_link(email, acs)


# Objet + gabarit, par type d'invitation. Les deux objets se terminent par la
# désignation vue du client (décision 2026-07-29, symétrie assumée).
_COURRIELS = {
    "documents": (
        "Transmission de documents",
        "reception/_invitation_documents.html",
    ),
    "intake": (
        "Ouverture de votre dossier",
        "reception/_invitation_ouverture.html",
    ),
}


def _corps_invitation(invitation: dict, lien: str) -> tuple[str, str]:
    """Corps de l'invitation, selon le type. Rend (objet, corps_html).

    Prend l'invitation ENTIÈRE plutôt que quatre valeurs éparses : le type
    voyage ainsi avec le reste, et les deux appelants — emettre_invitation et
    renvoyer_invitation — l'ont déjà en main. C'est ce qui règle d'un coup les
    quatre points d'appel de ``routes/`` : avant, le renvoi d'une invitation
    « intake » expédiait le texte « documents », y compris lorsque c'était le
    CLIENT qui redemandait un lien depuis le portail.

    Partage des responsabilités, calqué sur ``taches_portail._corps_accuse`` :
    le Python possède l'objet et tout ce qui se dérive (la date formatée,
    l'URL de secours avec son ``?i=``) ; le gabarit ne possède que le libellé
    et la mise en page, et ne reçoit que des chaînes déjà prêtes. Le juriste
    qui retouche le texte ne peut donc casser ni la date ni le lien.

    Les deux DÉROGATIONS aux annexes A.1 des specs (2026-07-27) vivent
    désormais dans ``_invitation_pied.html``, avec le commentaire qui interdit
    de les « corriger » : la durée du lien distinguée de celle de
    l'invitation, et l'URL de secours.
    """
    type_ = invitation.get("type") or "documents"
    prefixe, gabarit = _COURRIELS.get(type_, _COURRIELS["documents"])
    display_label = invitation.get("display_label", "")
    # Variante « de phrase » : le gabarit documents écrit « Dans le cadre du
    # dossier {label} », et le libellé PAR DÉFAUT commence lui-même par
    # « Dossier » — ce qui donnait « du dossier Dossier 2026-001 ». On retire
    # un tel préfixe pour la phrase SEULEMENT ; l'objet du courriel, le
    # portail et Réception gardent le libellé intégral, qui s'y lit bien.
    # Dérivé en Python, pas dans le gabarit : la règle maison veut que le
    # gabarit ne possède que le libellé, jamais une transformation.
    label_phrase = display_label
    if label_phrase.lower().startswith("dossier "):
        label_phrase = label_phrase[len("dossier "):].strip() or display_label
    corps = render_template(
        gabarit,
        display_label=display_label,
        display_label_phrase=label_phrase,
        lien=lien,
        max_file_mb=PORTAIL_MAX_FILE_MB,
        date_expiration=format_date_fr(to_mtl(invitation["expires_at"]).date()),
        # Jamais un littéral : un environnement de développement ne doit pas
        # pouvoir renvoyer un client vers la production. Le « ?i= » fait
        # prendre à /api/renvoi la branche par identifiant exact plutôt que le
        # balayage par courriel.
        url_secours=f"https://{PORTAIL_HOST}/entree?i={invitation['id']}",
        firm_name=Config.FIRM_NAME,
        firm_phone=Config.FIRM_PHONE,
        firm_email=Config.FIRM_EMAIL,
    )
    return f"{prefixe} — {display_label}", corps


def _expedier(email: str, objet: str, corps_html: str, lien: str,
              invitation_id: str) -> str:
    """Send the invitation email; return the manual link on any failure."""
    try:
        courriel.envoyer(email, objet, corps_html)
    except GraphNotConfigured:
        log_portail_event(
            "courriel_echec", "refused",
            invitation_id=invitation_id, reason="graph_not_configured",
        )
        return lien
    except GraphError:
        logger.exception("portail invitation email send failed")
        log_portail_event(
            "courriel_echec", "failure",
            invitation_id=invitation_id, reason="graph_error",
        )
        return lien
    log_portail_event("courriel_envoye", invitation_id=invitation_id)
    return ""


def emettre_invitation(
    type_: str,
    email: str,
    *,
    dossier_id: Optional[str] = None,
    partie_id: Optional[str] = None,
    client_name: str = "",
    display_label: str = "",
    jours: Optional[int] = None,
    prefill: Optional[dict] = None,
) -> tuple[Optional[dict], list[str], str]:
    """Create + send an invitation. Returns (invitation, errors, lien_manuel).

    ``prefill`` (L3, type « intake » seulement) doit venir de
    ``inv_model.prefill_depuis_partie`` — jamais d'un document de partie brut.
    """
    email_n = normalize_email(email or "")
    if not email_n:
        return None, ["Adresse courriel invalide."], ""

    # Garde-fou §1.3: inviting the juriste's own email would stamp the
    # portail claim on the authorized account and lock it out of the main
    # service (§1.2 refuses the claim at login). Never allowed — test with
    # an alias.
    if email_n == (Config.AUTHORIZED_USER_EMAIL or "").strip().lower():
        return None, [
            "Impossible d'inviter le courriel du juriste : la revendication "
            "« portail » verrouillerait ce compte hors du service principal. "
            "Utilisez un alias pour les essais."
        ], ""

    # Firebase user: get-or-create, then MERGE the portail claim into any
    # existing claims (never replace the dict — a future claim would be
    # silently wiped).
    try:
        try:
            user = fb_auth.get_user_by_email(email_n)
        except fb_auth.UserNotFoundError:
            user = fb_auth.create_user(email=email_n)
        claims = dict(user.custom_claims or {})
        if claims.get("portail") is not True:
            claims["portail"] = True
            fb_auth.set_custom_user_claims(user.uid, claims)
    except Exception:
        logger.exception("portail invitation firebase user setup failed")
        return None, ["Erreur lors de la préparation du compte client."], ""

    invitation, errors = inv_model.creer_invitation(
        type_, email_n,
        dossier_id=dossier_id, partie_id=partie_id, client_name=client_name,
        display_label=display_label, jours=jours, prefill=prefill,
    )
    if errors or invitation is None:
        return None, errors or ["Erreur lors de la création de l'invitation."], ""

    try:
        lien = _generer_lien(email_n, invitation["id"])
    except Exception:
        logger.exception("portail sign-in link generation failed")
        # The invitation exists but no usable link was ever produced —
        # revoke it so a dead invitation never lingers as « envoyée ».
        inv_model.revoquer(invitation["id"])
        return None, [
            "Impossible de générer le lien de connexion. Vérifier que la "
            "connexion par lien courriel est activée dans la console Firebase "
            "(checklist §12.8)."
        ], ""

    objet, corps = _corps_invitation(invitation, lien)
    lien_manuel = _expedier(email_n, objet, corps, lien, invitation["id"])
    log_portail_event(
        "invitation_emise",
        invitation_id=invitation["id"], dossier_id=dossier_id,
        emailed=not bool(lien_manuel),
    )
    return invitation, [], lien_manuel


def renvoyer_invitation(inv_id: str) -> tuple[bool, str, str]:
    """Regenerate + resend the link (§6.2 steps 4-5).

    Allowed while the invitation may still receive files — statut
    envoyée/ouverte/**soumise** (D-2: a submitted lot stays open until the
    lawyer processes it, so a forgotten page is recoverable), not expired, and
    under the per-invitation resend cap (D-4). MUST agree with the portal's
    ``peut_relancer`` or a renvoi the portal enqueues gets dropped here with a
    200 while the client is told a link was sent. Returns (ok, message,
    lien_manuel).
    """
    invitation = inv_model.lire_invitation(inv_id)
    if invitation is None or not inv_model.peut_relancer(invitation):
        # Generic wording — this path also serves the portal-initiated
        # anti-enumeration flow, which must not learn why.
        return False, "Invitation invalide ou expirée.", ""

    try:
        lien = _generer_lien(invitation["email"], inv_id)
    except Exception:
        logger.exception("portail resend link generation failed")
        return False, "Impossible de générer le lien de connexion.", ""

    objet, corps = _corps_invitation(invitation, lien)
    lien_manuel = _expedier(invitation["email"], objet, corps, lien, inv_id)
    inv_model.incrementer_resend(inv_id)
    log_portail_event(
        "renvoi_demande", invitation_id=inv_id, emailed=not bool(lien_manuel)
    )
    return True, "", lien_manuel
