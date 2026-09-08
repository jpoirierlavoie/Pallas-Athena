"""« Paramètres » — profil du cabinet + sécurité du compte.

Le profil est un formulaire ordinaire : POST puis redirection, jamais htmx.
Ce n'est pas une préférence de style — htmx 2.0.4 n'échange QUE les réponses
2xx, donc un fragment d'erreur rendu en 4xx ne paraît jamais à l'écran, et
un formulaire de validation est par nature une surface de messages d'erreur.
La forme POST + redirection supprime le piège au lieu de le contourner : une
erreur se rend en re-rendant la page en **200**, un succès redirige.

La section « Sécurité » a son PROPRE URL, et pas un onglet du même document.
Deux raisons, deux modes de panne : cette page charge 139 Ko de SDK d'auth et
instancie un widget reCAPTCHA vivant, qu'une page ouverte pour corriger un
numéro de télécopieur n'a pas à porter ; et sur un document unique, un clic
sur « Enregistrer » au milieu d'un envoi de code détruirait le
`verificationId` en cours, brûlant un SMS payé et consommant le jeton
reCAPTCHA. Le fil entre les deux est un `<a href>` ordinaire.

Le blueprint est le SIEN, et il n'est JAMAIS ajouté à un `csrf.exempt` :
c'est un POST de navigateur. `/parametres` ne tombe sous aucun préfixe
exempté (`/_ah/`, `UPLOAD_PATHS`, `_is_template_upload_path`, les préfixes
exempts d'App Check) — contrairement à `/auth/`, ce qui est précisément
pourquoi la page de sécurité ne porte aucun attribut `hx-*`.
"""

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required
from models.settings import (
    changed_field_names,
    get_cabinet,
    update_cabinet,
)
from security import limiter, sanitize
from utils import config_checks
from utils.cabinet import display_phone
from utils.logging_setup import log_auth_event, log_settings_event

settings_bp = Blueprint("settings", __name__, url_prefix="/parametres")

# Ce que le formulaire soumet, dans l'ordre où il l'affiche. Le formulaire
# poste TOUJOURS le bloc entier : c'est ce qui rend l'effacement d'un champ
# possible (une clé présente et vide efface) et ce qui rend inoffensif le
# complètement d'adresse de `apply_address_defaults`.
_FORM_FIELDS: tuple[str, ...] = (
    "nom",
    "organisation",
    "address_street",
    "address_unit",
    "address_city",
    "address_province",
    "address_postal_code",
    "address_country",
    "telephone",
    "telecopieur",
    "courriel",
    "gst_number",
    "qst_number",
)


def _prefill(cab: dict) -> dict:
    """Le profil stocké, préparé pour le formulaire.

    Les téléphones sont stockés en E.164 et affichés en forme locale — la
    même que celle qu'imprime chaque document. `normalize_phone` accepte les
    deux, donc l'aller-retour du formulaire est sûr.
    """
    form = {key: cab.get(key, "") or "" for key in _FORM_FIELDS}
    form["telephone"] = display_phone(cab.get("telephone", "") or "")
    form["telecopieur"] = display_phone(cab.get("telecopieur", "") or "")
    return form


@settings_bp.route("/")
@login_required
def settings_index() -> str:
    return render_template(
        "settings/index.html",
        onglet="profil",
        form=_prefill(get_cabinet()),
        errors=[],
        enregistre=request.args.get("ok") == "1",
    )


@settings_bp.route("/cabinet", methods=["POST"])
@login_required
def cabinet_update():
    f = request.form
    data = {key: sanitize(f.get(key, ""), max_length=2000) for key in _FORM_FIELDS}

    before = get_cabinet()
    doc, errors = update_cabinet(data)
    if errors:
        log_settings_event("cabinet_refused", error_count=len(errors))
        # 200, pas 4xx : c'est un message destiné à être LU. On réaffiche ce
        # qui a été soumis plutôt que le stocké, sinon la correction se fait
        # à l'aveugle.
        return render_template(
            "settings/index.html",
            onglet="profil",
            form={**_prefill(before), **data},
            errors=errors,
            enregistre=False,
        )

    log_settings_event(
        "cabinet_updated",
        fields_changed=changed_field_names(before, doc or {}),
    )
    return redirect(url_for("settings.settings_index", ok="1"))


@settings_bp.route("/securite")
@login_required
def securite() -> str:
    """La section sécurité — mot de passe et second facteur.

    Tout se joue dans le navigateur, contre Google : `firebase-admin` 7.4.0
    n'a AUCUNE surface MFA (ni inscription, ni désinscription, ni même un
    décompte), donc le serveur ne peut pas connaître l'état des facteurs et
    ne rend ici qu'un cadre.

    `user_uid` est la garde d'identité : l'état Firebase de ce navigateur
    vit dans son propre IndexedDB, indépendamment du témoin Flask, si bien
    qu'une session valide peut coexister avec une identité Firebase absente
    ou DIFFÉRENTE. Sans cette valeur, la page ne pourrait pas distinguer les
    deux et présenterait « aucun facteur » — ce que fait l'écran actuel, et
    qui est un mensonge aux conséquences de verrouillage.
    """
    return render_template(
        "settings/securite.html",
        onglet="securite",
        user_uid=session.get("user_id", ""),
        compte_courriel=current_app.config.get("AUTHORIZED_USER_EMAIL", ""),
        require_mfa=bool(current_app.config.get("REQUIRE_MFA", False)),
    )


# Vocabulaire FERMÉ du journal de sécurité. Tout le reste est refusé et
# RIEN n'est journalisé — une valeur libre venue du navigateur ne doit
# jamais atteindre un journal.
_EVENEMENTS_JOURNAL: frozenset = frozenset({
    "password_changed",
    "mfa_enrolled",
    "mfa_unenrolled",
    "mfa_unenroll_failed_zero_factors",
    "reauth_failed",
})


# Les quatre sections du rapport, dans l'ordre d'affichage, avec leur
# en-tête FRANÇAIS et ce que la section veut dire. Le module de contrôle rend
# des identifiants de section en anglais (il est développeur-facing et ses
# messages le sont aussi) ; la présentation vit ici.
_SECTIONS_FR: tuple[tuple[str, str, str], ...] = (
    (
        config_checks.SECTION_ENV,
        "Amorçage et contrôles fail-open",
        "Les trois variables sans lesquelles l'application ne démarre pas, et "
        "les protections qui se DÉSACTIVENT toutes seules quand leur clé "
        "manque — sans le dire, pour l'une d'entre elles, jusqu'à ce lot.",
    ),
    (
        config_checks.SECTION_SECRETS,
        "Secret Manager",
        "Chaque secret résout-il, et sa FORME est-elle bonne ? Un caractère "
        "parasite suffit : sur le secret d'origine il met tout le site en "
        "403, sur l'empreinte DAV il arrête DavX5 en silence. Aucune valeur "
        "n'est affichée — seulement sa longueur et son verdict.",
    ),
    (
        config_checks.SECTION_INTEGRATIONS,
        "Intégrations",
        "Microsoft Graph, la synchro Bookings et le miroir Outlook. Quand "
        "l'une n'est pas câblée, la ligne nomme le champ qui manque.",
    ),
    (
        config_checks.SECTION_FORK,
        "Hygiène de fork",
        "Les valeurs codées en dur qui désignent le déploiement d'ORIGINE. "
        "Sur ce déploiement-ci, elles sont toutes attendues : ces "
        "avertissements servent à quelqu'un qui a cloné le dépôt pour monter "
        "sa propre instance.",
    ),
)


@settings_bp.route("/configuration")
@login_required
def configuration() -> str:
    """« Paramètres → Configuration » — l'état du déploiement, en lecture.

    LECTURE SEULE, et GET seulement : un test épingle l'inventaire des
    méthodes de la route. Aucun JavaScript, aucun attribut `hx-*`.

    Elle rend le MÊME rapport que `python -m scripts.check_config`, par les
    mêmes tables et les mêmes prédicats (`utils/config_checks`). C'est le
    point : ce script existait déjà, savait déjà repérer un
    `cf-origin-secret` absent — et rien ne l'exécutait, ce qui est
    exactement comment le contrôle d'origine est resté éteint des mois sans
    que rien ne le signale.

    Aucun cache : une page de diagnostic qui montre un état périmé est pire
    qu'aucune page. En production cela coûte six lectures Secret Manager par
    affichage, ce qui est sans conséquence sur une page qu'on ouvre pour
    diagnostiquer.

    LE BADGE IGNORE L'HYGIÈNE DE FORK, à dessein. Sur le déploiement
    d'origine cette section porte ~25 avertissements parfaitement normaux ;
    les compter dans l'en-tête apprendrait à ignorer la page, et c'est
    précisément l'attention qu'un secret manquant réclame. Elle garde donc
    son propre compte, séparé et étiqueté.
    """
    rapport = config_checks.run_all()
    par_section = dict(rapport.sections())

    sections = []
    for identifiant, titre, explication in _SECTIONS_FR:
        lignes = par_section.get(identifiant, [])
        if not lignes:
            continue
        sections.append({
            "id": identifiant,
            "titre": titre,
            "explication": explication,
            "lignes": lignes,
            "fork": identifiant == config_checks.SECTION_FORK,
            "compte": {
                niveau: sum(1 for r in lignes if r.level == niveau)
                for niveau in ("OK", "WARN", "FAIL")
            },
        })

    operationnelles = [s for s in sections if not s["fork"]]
    niveaux = {r.level for s in operationnelles for r in s["lignes"]}
    badge = "FAIL" if "FAIL" in niveaux else ("WARN" if "WARN" in niveaux else "OK")

    return render_template(
        "settings/configuration.html",
        onglet="configuration",
        sections=sections,
        badge=badge,
        production=config_checks.is_production(),
    )


@settings_bp.route("/securite/journal", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def securite_journal() -> tuple[Response, int]:
    """Journal des changements de sécurité. 204, jamais bloquant.

    Le mot de passe et les facteurs changent DANS LE NAVIGATEUR, contre
    Google : `firebase-admin` 7.4.0 n'expose aucune surface MFA, donc le
    serveur ne peut ni les lire ni les écrire. Sans cette route, un
    changement de second facteur survenu six semaines plus tôt n'aurait
    aucune trace nulle part.

    Son propre seau de débit — JAMAIS `RATE_LIMIT_LOGIN`, dont les 5 par
    minute contraignent déjà le rafraîchissement de session qui suit un
    changement de mot de passe.

    Un 4xx est correct ici : l'appelant est un `fetch` machine, aucun htmx
    n'est en jeu, donc la règle « une erreur à lire voyage en 2xx » ne
    s'applique pas. Rien ne LIT ce journal pour décider quoi que ce soit.
    """
    evenement = (request.form.get("event") or "").strip()
    if evenement not in _EVENEMENTS_JOURNAL:
        return jsonify({"ok": False}), 400

    fields: dict = {}
    brut = (request.form.get("factor_count") or "").strip()
    if brut:
        try:
            # Borné : c'est le seul nombre qui dise, après coup, si le
            # compte était exposé au verrouillage. La provenance est
            # nommée dans la clé — elle vient du client.
            fields["factor_count_client"] = max(0, min(10, int(brut)))
        except ValueError:
            pass

    # Aucun champ libre : les clés inconnues sont ignorées ENTIÈREMENT.
    log_auth_event(evenement, "success", **fields)
    return Response(status=204), 204
