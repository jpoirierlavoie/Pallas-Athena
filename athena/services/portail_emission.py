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
from markupsafe import escape

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


def _signature_html() -> str:
    lines = [escape(Config.FIRM_NAME or "")]
    if Config.FIRM_PHONE:
        lines.append(escape(Config.FIRM_PHONE))
    if Config.FIRM_EMAIL:
        lines.append(escape(Config.FIRM_EMAIL))
    return "<p>" + "<br>".join(str(l) for l in lines if l) + "</p>"


def _corps_invitation(display_label: str, lien: str, expires_at) -> tuple[str, str]:
    """Gabarit A.1 — invitation (type documents). Returns (objet, corps_html)."""
    objet = f"Transmission de documents — {display_label}"
    date_exp = format_date_fr(to_mtl(expires_at).date())
    corps = (
        "<p>Bonjour,</p>"
        f"<p>Dans le cadre du dossier {escape(display_label)}, vous êtes "
        "invité(e) à transmettre vos documents de façon sécurisée par le lien "
        f"suivant : <a href=\"{escape(lien)}\">transmettre mes documents</a>.</p>"
        "<p>Ce lien est <strong>strictement personnel</strong> et lié à la "
        "présente adresse courriel ; il ne doit pas être transféré. Il demeure "
        f"valide jusqu'au {date_exp}. Formats admis : PDF, images (JPEG, PNG, "
        "HEIC, TIFF), documents Office et texte ; taille maximale de "
        f"{PORTAIL_MAX_FILE_MB} Mo par fichier.</p>"
        f"{_signature_html()}"
    )
    return objet, corps


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
    display_label: str = "",
    jours: Optional[int] = None,
) -> tuple[Optional[dict], list[str], str]:
    """Create + send an invitation. Returns (invitation, errors, lien_manuel)."""
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
        dossier_id=dossier_id, partie_id=partie_id,
        display_label=display_label, jours=jours,
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

    objet, corps = _corps_invitation(invitation["display_label"], lien,
                                     invitation["expires_at"])
    lien_manuel = _expedier(email_n, objet, corps, lien, invitation["id"])
    log_portail_event(
        "invitation_emise",
        invitation_id=invitation["id"], dossier_id=dossier_id,
        emailed=not bool(lien_manuel),
    )
    return invitation, [], lien_manuel


def renvoyer_invitation(inv_id: str) -> tuple[bool, str, str]:
    """Regenerate + resend the link (§6.2 steps 4-5).

    Allowed only while the invitation is active (statut envoyée/ouverte, not
    expired). Returns (ok, message, lien_manuel).
    """
    invitation = inv_model.lire_invitation(inv_id)
    if invitation is None or not inv_model.est_active(invitation):
        # Generic wording — this path also serves the portal-initiated
        # anti-enumeration flow, which must not learn why.
        return False, "Invitation invalide ou expirée.", ""

    try:
        lien = _generer_lien(invitation["email"], inv_id)
    except Exception:
        logger.exception("portail resend link generation failed")
        return False, "Impossible de générer le lien de connexion.", ""

    objet, corps = _corps_invitation(invitation["display_label"], lien,
                                     invitation["expires_at"])
    lien_manuel = _expedier(invitation["email"], objet, corps, lien, inv_id)
    inv_model.incrementer_resend(inv_id)
    log_portail_event(
        "renvoi_demande", invitation_id=inv_id, emailed=not bool(lien_manuel)
    )
    return True, "", lien_manuel
