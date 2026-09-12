"""Deployment inventory — the ONE table of what an instance needs.

PURE DATA AND PURE PREDICATES. Stdlib imports only: no ``google.*``, no
``subprocess``, no file I/O, no Flask. ``tests/test_deployment_inventory.py``
asserts that on the module's AST, and that property is what makes this module
safe to import from a Flask route, from a CLI script, and from a test with no
credentials — the ``mcp/coverage.py`` precedent.

WHY IT EXISTS. The same facts were kept by hand in several places and drifted,
which CLAUDE.md predicted in writing before it happened: it names three
hand-maintained inventories that do not update themselves, one of them being
``scripts/check_config.py``'s ``_SCAN_FILES``. That list then omitted
``portail.yaml`` and ``client/config.py``, both of which carry owner literals —
not an oversight in kind, but the expected behaviour of a hand-kept list. And
``DEPLOYMENT.md`` §4.2 said "the four Secret Manager secrets" from the day
``portail-secret-key`` shipped as a fifth and ``graph-client-secret`` as a
sixth.

So: one table here, and every reader imports it — the CLI checker, the
« Paramètres → Configuration » page, and the provisioning script.
``utils/template_fields.py``'s ``CATALOG`` is the existing precedent (pure data
in ``utils/``, consumed by routes AND pinned against a markdown doc by a test).

Paths are stored RELATIVE to the repo root, as plain strings. The caller joins
them, which is what keeps this module free of ``os`` and therefore trivially
pure.
"""

import re
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

# ── Services ─────────────────────────────────────────────────────────────
# Two App Engine services share this project. A value required by one is not
# necessarily required by the other, and GRAPH_* must NEVER reach the portal
# (spec L1 §8.1) — which is exactly the kind of fact a table can carry and a
# prose paragraph cannot.

SERVICE_DEFAULT = "default"
SERVICE_PORTAIL = "portail"
SERVICES: tuple[str, ...] = (SERVICE_DEFAULT, SERVICE_PORTAIL)


# ── Shape predicates ─────────────────────────────────────────────────────
# Each takes the DECODED value and returns a ShapeVerdict, or None when the
# shape is acceptable. Pure; no I/O; never logs; never returns the value —
# only COUNTS and categories computed from it.

SHAPE_FAIL = "fail"
SHAPE_WARN = "warn"


class ShapeVerdict(NamedTuple):
    """Une objection sur la forme, et SA GRAVITÉ.

    Le classement en deux crans porte le lot plutôt qu'il ne le décore : un
    contrôle qui fait rougir un déploiement qui FONCTIONNE est un contrôle
    qu'on cesse de lire, et un contrôle qu'on ne lit plus est exactement
    l'état dans lequel `cf-origin-secret` a passé des mois. FAIL est donc
    réservé à ce qui est cassé AUJOURD'HUI — la requête échoue, à chaque
    fois — et tout ce qui n'est qu'inhabituel répond WARN.
    """

    severity: str
    message: str


@dataclass(frozen=True)
class Shape:
    """Un prédicat de forme QUI PORTE SES BORNES.

    Les deux nombres ne sont pas décoratifs : la recette `gcloud` rendue à
    l'écran et la §6.4 de `DEPLOYMENT.md` disent toutes deux « le compte doit
    valoir tant », et ce compte doit être DÉRIVÉ du contrôle qui le vérifie.
    Une longueur recopiée à la main dans un mode d'emploi est exactement la
    dérive que cette table existe pour supprimer — c'est ce qui est arrivé à
    « les quatre secrets » de §4.2.

    Appelable, donc `secret.shape(valeur)` ne change de forme pour aucun
    appelant.
    """

    minimum: int
    maximum: int
    check: Callable[[str], Optional[ShapeVerdict]]

    def __call__(self, value: str) -> Optional[ShapeVerdict]:
        return self.check(value)


# \Z, never $ — Python's `$` ALSO matches just before a trailing newline,
# so `^…$` would accept the 61-character value this predicate exists to
# refuse. That is the same defect `normalize_email` was documented for, and
# the explicit length check below is the belt to this braces.
#
# `[0-9][0-9]`, never `\d\d` — Python's `\d` is UNICODE-aware, so
# `"$2b$" + "١٢" + "$" + "a"*53` is 60 characters long and MATCHED (measured
# 2026-09-11). It then fails `bcrypt.checkpw`, which `dav/dav_auth.py`
# swallows in a bare `except` — which is to say it walked straight through
# the predicate and into the silent-DavX5 failure the predicate exists to
# eliminate.
_BCRYPT_RE = re.compile(r"^\$2[aby]\$[0-9][0-9]\$[./A-Za-z0-9]{53}\Z")
_BCRYPT_LENGTH = 60


def _shape_bcrypt(value: str) -> Optional[ShapeVerdict]:
    """A bcrypt hash, exactly 60 characters.

    The single highest-value validation in the inventory. ``bcrypt.checkpw``
    recomputes 60 bytes; a 61-byte value (one stray newline) can never match,
    and DAV Basic Auth then fails with **no error anywhere** — not on the
    phone, not in the logs. It is the quietest failure mode in the
    application, and a shape check removes it by construction.
    """
    if len(value) != _BCRYPT_LENGTH or not _BCRYPT_RE.match(value):
        return ShapeVerdict(
            SHAPE_FAIL,
            f"Ce champ attend une EMPREINTE bcrypt de {_BCRYPT_LENGTH} "
            f"caractères (« $2b$… »), jamais un mot de passe en clair "
            f"(reçu : {len(value)} caractères).",
        )
    return None


# Ce dans quoi `secrets.token_urlsafe` puise, plus les deux marques non
# réservées restantes de la RFC 3986. Chaque secret de cette table est
# ENGENDRÉ — un jeton, une empreinte —, donc ce jeu est ce dont une valeur
# correcte est FAITE, et non une préférence de style.
_TOKEN_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789_.~-"
)


def _shape_ascii_token(minimum: int, maximum: int) -> Shape:
    """La longueur, ET le jeu de caractères que le nom promettait.

    `_shape_urlsafe` était une borne de LONGUEUR portant un nom de jeu de
    caractères, et l'écart n'était pas cosmétique.
    `security._enforce_origin_secret` appelle
    `hmac.compare_digest(supplied, secret)` sur **chaque** requête, avec deux
    `str` — et cela LÈVE `TypeError` sur une chaîne non ASCII (mesuré). Une
    seule lettre accentuée, une apostrophe typographique collée depuis Word
    ou une espace insécable dans `cf-origin-secret` répond donc **500 à
    chaque requête**, avec une trace par requête : strictement pire que le
    403 du saut de ligne déjà documenté — et l'ancien prédicat appelait cela
    « ok ».

    Un caractère de contrôle est la même espèce, d'un cran en dessous : un
    en-tête HTTP ne peut pas le transporter, donc la valeur reçue ne peut
    jamais égaler la valeur stockée et chaque requête répond 403.

    Tout le reste hors de `_TOKEN_SAFE` — une espace, « + », « / », « = »,
    une ponctuation — est ASCII et transmissible : ce peut très bien être un
    déploiement qui FONCTIONNE, dont le générateur a simplement produit du
    base64 standard. C'est donc un avertissement, jamais un échec.

    Aucun message ne rend la valeur : seulement des DÉCOMPTES et des
    catégories calculés sur elle.
    """

    def check(value: str) -> Optional[ShapeVerdict]:
        if not (minimum <= len(value) <= maximum):
            return ShapeVerdict(
                SHAPE_FAIL,
                f"Longueur inattendue : {len(value)} caractères "
                f"(attendu entre {minimum} et {maximum}).",
            )
        hors_ascii = sum(1 for c in value if ord(c) > 127)
        if hors_ascii:
            return ShapeVerdict(
                SHAPE_FAIL,
                f"{hors_ascii} caractère(s) NON ASCII — lettre accentuée, "
                "apostrophe typographique, espace insécable : la signature "
                "d'un copier-coller. `hmac.compare_digest` LÈVE sur une "
                "chaîne non ASCII, donc pour le secret d'origine c'est une "
                "erreur 500 à chaque requête, et non un 403.",
            )
        controles = sum(1 for c in value if ord(c) < 32 or ord(c) == 127)
        if controles:
            return ShapeVerdict(
                SHAPE_FAIL,
                f"{controles} caractère(s) de contrôle. Un en-tête HTTP ne "
                "peut pas en transporter : la valeur reçue ne pourra jamais "
                "égaler celle qui est stockée.",
            )
        espaces = sum(1 for c in value if c == " ")
        autres = sum(1 for c in value if c != " " and c not in _TOKEN_SAFE)
        if espaces or autres:
            parts = []
            if espaces:
                parts.append(f"{espaces} espace(s) à l'intérieur")
            if autres:
                parts.append(f"{autres} caractère(s) hors du jeu habituel")
            return ShapeVerdict(
                SHAPE_WARN,
                f"Inhabituel : {' et '.join(parts)}. Ce n'est pas forcément "
                "une panne — un générateur en base64 standard produit « + », "
                "« / » et « = » —, mais une espace est invisible dans une "
                "console et ne s'est probablement pas retrouvée là exprès.",
            )
        return None

    return Shape(minimum, maximum, check)


def stray_whitespace(value: str) -> Optional[str]:
    """Name the stray bytes, or None. Applies to EVERY secret.

    Nothing in ``config.py`` strips a secret, so surrounding whitespace
    becomes part of the value. For ``cf-origin-secret`` that is site-wide
    downtime: it is compared byte for byte with ``hmac.compare_digest``
    against a header a Cloudflare Transform Rule injects, and **a Transform
    Rule cannot emit a newline** — so every request answers 403 and the edge
    cannot be corrected to compensate.

    Reports the COUNT and the side, never the value.
    """
    if not value or value == value.strip():
        return None
    lead = len(value) - len(value.lstrip())
    trail = len(value) - len(value.rstrip())
    parts = []
    if lead:
        parts.append(f"{lead} au début")
    if trail:
        parts.append(f"{trail} à la fin")
    return (
        f"Caractères parasites : {' et '.join(parts)}. Un secret est comparé "
        "octet pour octet — ils en feraient partie."
    )


# ── Secrets — SIX, not four ──────────────────────────────────────────────

# D'OÙ vient la valeur. Ce n'est pas une étiquette : c'est ce qui DÉRIVE la
# première étape de la recette `gcloud`, et donc ce qui empêche une recette
# écrite à la main de conseiller un jour de « générer » une clé que la console
# Firebase est seule à pouvoir émettre.
ORIGIN_GENERATED = "genere"    # vous la frappez — `secrets.token_urlsafe`
ORIGIN_EXTERNAL = "console"    # Firebase / Entra vous la remettent
ORIGIN_PASSWORD = "empreinte"  # calculée d'un mot de passe que vous choisissez


@dataclass(frozen=True)
class Secret:
    """One Secret Manager secret.

    ``writable`` says whether « Paramètres → Déploiement » may add a version.
    It is enforced twice on purpose: here, and by binding
    ``roles/secretmanager.secretVersionAdder`` per secret rather than at
    project level — so a refusal survives a refactor of this table.
    """

    secret_id: str
    env_var: str
    label: str
    required_for: frozenset
    consequence: str
    writable: bool
    origin: str
    refusal: str = ""
    shape: Optional[Shape] = None

    @property
    def required(self) -> bool:
        return bool(self.required_for)


SECRETS: tuple[Secret, ...] = (
    Secret(
        secret_id="flask-secret-key",
        env_var="SECRET_KEY",
        label="Clé de signature des sessions Flask",
        required_for=frozenset({SERVICE_DEFAULT}),
        consequence="l'application NE DÉMARRE PAS",
        writable=False,
        origin=ORIGIN_GENERATED,
        refusal=(
            "Écriture refusée par conception. La faire tourner invalide TOUTES "
            "les sessions, dont la vôtre — vous seriez déconnecté par l'acte "
            "même et ne pourriez pas lire le résultat de la vérification. "
            "C'est aussi le seul secret dur : une mauvaise valeur empêche le "
            "démarrage, et on ne corrige pas depuis une application qui ne "
            "démarre pas. Et c'est le seul dont l'écriture permettrait de "
            "FORGER des sessions."
        ),
        shape=_shape_ascii_token(32, 256),
    ),
    Secret(
        secret_id="portail-secret-key",
        env_var="PORTAIL_SECRET_KEY",
        label="Clé de session du service « portail »",
        required_for=frozenset({SERVICE_PORTAIL}),
        consequence="le service « portail » NE DÉMARRE PAS",
        writable=False,
        origin=ORIGIN_GENERATED,
        refusal=(
            "Écriture refusée : ce secret appartient à un AUTRE service, dont "
            "le compte de service est distinct. Une mauvaise valeur ferait "
            "répondre 500 au portail — la seule surface où il y a des tiers. "
            "Et la séparation des deux clés EST la frontière de session entre "
            "les services : une page capable d'écrire les deux moitiés "
            "affaiblirait l'argument qui la justifie."
        ),
        shape=_shape_ascii_token(32, 256),
    ),
    Secret(
        secret_id="firebase-api-key",
        env_var="FIREBASE_API_KEY",
        label="Clé d'API navigateur Firebase",
        required_for=frozenset(),
        consequence="la page de connexion ne peut pas initialiser Firebase",
        writable=True,
        origin=ORIGIN_EXTERNAL,
        shape=_shape_ascii_token(30, 60),
    ),
    Secret(
        secret_id="dav-password-hash",
        env_var="DAV_PASSWORD_HASH",
        label="Empreinte bcrypt du mot de passe DAV",
        required_for=frozenset(),
        consequence=(
            "l'authentification DAV ne peut pas réussir — DavX5 cesse de "
            "synchroniser SANS message d'erreur"
        ),
        writable=True,
        origin=ORIGIN_PASSWORD,
        shape=Shape(_BCRYPT_LENGTH, _BCRYPT_LENGTH, _shape_bcrypt),
    ),
    Secret(
        secret_id="cf-origin-secret",
        env_var="CF_ORIGIN_SECRET",
        label="Secret d'origine Cloudflare",
        required_for=frozenset(),
        consequence=(
            "le contrôle d'origine est DÉSACTIVÉ en silence (l'accès direct à "
            "App Engine n'est plus bloqué)"
        ),
        writable=True,
        origin=ORIGIN_GENERATED,
        shape=_shape_ascii_token(32, 128),
    ),
    Secret(
        secret_id="graph-client-secret",
        env_var="GRAPH_CLIENT_SECRET",
        label="Secret client Microsoft Graph",
        required_for=frozenset(),
        consequence="le courriel sortant est désactivé",
        writable=True,
        origin=ORIGIN_EXTERNAL,
        shape=_shape_ascii_token(20, 256),
    ),
)

SECRET_IDS: tuple[str, ...] = tuple(s.secret_id for s in SECRETS)
WRITABLE_SECRET_IDS: tuple[str, ...] = tuple(
    s.secret_id for s in SECRETS if s.writable
)


def secret_by_id(secret_id: str) -> Optional[Secret]:
    for s in SECRETS:
        if s.secret_id == secret_id:
            return s
    return None


# ── La recette `gcloud`, ENGENDRÉE ───────────────────────────────────────
#
# Pourquoi engendrée plutôt qu'écrite : §6.4 de `DEPLOYMENT.md` portait trois
# défauts réels, et les trois sont de la famille « recopié à la main ». Elle
# faisait `hashpw(b'YOUR_DAV_PASSWORD', …)`, ce qui dépose le mot de passe DAV
# EN CLAIR dans `~/.bash_history`. Elle disait `secrets create` sur chaque
# ligne — juste au premier déploiement, faux pour toute rotation depuis. Et
# elle finissait par `| tr -d '\n'` pour éponger un saut de ligne que
# `sys.stdout.write` n'émet jamais, ce qui enseigne une superstition au lieu
# d'une règle.
#
# Un test épingle `DEPLOYMENT.md` §6.4 contre la sortie de cette fonction :
# c'est ce qui rend la page structurellement meilleure que le document, et
# non une seconde copie de lui.
#
# ⚠ BASH SEULEMENT, et ce n'est pas une préférence — c'est MESURÉ
# (2026-09-11, sur cette machine).
#
# **Git Bash convient.** `printf '%s' "$V" | <exe natif>` livre exactement les
# octets de `$V` : 3 pour « abc », 43 pour un jeton de 43 caractères. Le
# contrôle négatif fait le travail — `echo`, un heredoc et une chaîne ici
# livrent 4 octets, le supplément étant un `\n` NU, donc le tuyau MSYS→natif
# ne traduit pas non plus LF↔CRLF. Vérifié sur les 256 valeurs d'octet, à
# 200 Ko, et jusque dans `files.ReadStdinBytes()` de gcloud lui-même. Le
# relais `~/bin/gcloud` est un script bash qui `exec` le lanceur POSIX du SDK,
# lequel `exec` un `python.exe` natif : `gcloud.cmd` n'est jamais sur ce
# chemin, donc `cmd.exe` n'entre pas dans la chaîne.
#
# **PowerShell ne convient pas, et c'est pire que ce que ce commentaire
# disait.** « abc » arrive en 8 octets à un exécutable NATIF :
# `ef bb bf 61 62 63 0d 0a`. Mais sur Windows `gcloud` est lui-même un `.ps1`
# qui RE-TUYAUTE (`gcloud.ps1:118` — `$input | & "$exe_path"`), donc la charge
# subit DEUX passages et se fait terminer deux fois. Mesuré au bout, à travers
# le vrai `gcloud`, dans un vrai Secret Manager, le 2026-09-11 : dix
# caractères ASCII y sont devenus **16 octets**,
# `efbbbf 6162636465666768696a 0d 0d 0a` — la nomenclature, la charge, puis le
# CR resté du premier passage suivi du CRLF du second. Trois mécanismes, pas
# un :
#   1. un CRLF ajouté par OBJET du pipeline — inconditionnel, dans TOUTES les
#      formes essayées, y compris natif→natif et y compris quand le programme
#      amont n'émet rien ;
#   2. `$OutputEncoding` qui RÉENCODE la charge — et Windows PowerShell 5.1 le
#      règle par défaut sur **ASCII** (page de code 20127), donc une lettre
#      accentuée devient « ? » : une corruption SILENCIEUSE du contenu, pas
#      seulement un octet de queue ;
#   3. une NOMENCLATURE d'octets (BOM) de 3 octets en TÊTE, qui vient de
#      `[Console]::InputEncoding` — et non de `$OutputEncoding`, ce que ce
#      commentaire affirmait à tort.
# PowerShell 7 n'est pas installé ici : ne rien affirmer à son sujet.
#
# Conséquence sur le CONTRÔLE, et c'est le défaut que la mesure a trouvé : une
# nomenclature est en TÊTE, donc un `xxd | tail -1` ne peut pas la voir. Seul
# le COMPTE D'OCTETS voit les deux bouts (3 devant, 2 derrière). C'est
# pourquoi la recette compte plutôt que de regarder la fin.
#
# Google Cloud Shell reste une bonne seconde voie — bash, userland Linux
# propre, aucune trace sur le disque local, joignable depuis un téléphone.
# Ce qu'elle n'apporte PAS : une meilleure attribution au journal d'audit.
# Ce commentaire l'a prétendu ; c'est faux. La version 1 de `cf-origin-secret`
# a été écrite DEPUIS CETTE MACHINE par le gcloud local le 2026-08-11, et le
# journal d'audit l'attribue à la personne. Sur 400 jours, 26 écritures de
# secret sur 26 sont attribuées à l'humain et zéro au compte de service
# d'App Engine.


def expected_length_fr(secret: "Secret") -> str:
    """« exactement 60 » ou « entre 32 et 128 » — DÉRIVÉ, jamais tapé."""
    if secret.shape is None:
        return ""
    lo, hi = secret.shape.minimum, secret.shape.maximum
    return f"exactement {lo}" if lo == hi else f"entre {lo} et {hi}"


# La sonde d'interpréteur, et pourquoi elle ESSAIE au lieu de regarder.
# `command -v python3` ne suffit pas : sur Windows, `python3` est présent sur
# le PATH sous forme de raccourci d'exécution du Microsoft Store, qui sort en
# code 49 sans rien produire. Mesuré au pire endroit le 2026-09-11, lors d'un
# essai réel contre un secret jetable — la valeur engendrée était VIDE, et
# seule la comparaison de longueur a empêché d'écrire un `cf-origin-secret`
# vide, c'est-à-dire de DÉSACTIVER en silence tout le contrôle d'origine
# (`if not secret: return None`). Une sonde d'EXISTENCE aurait choisi le
# raccourci cassé ; une sonde d'EXÉCUTION choisit ce qui marche.
#
# `py` ferme la liste : le lanceur Windows répond là où les deux autres noms
# échouent. Sur cette machine, `python` tombe sur le venv du dépôt — où
# bcrypt est déjà installé.
_SONDE_PY = r"""PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi"""

_SONDE_FR = (
    "Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur "
    "Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur "
    "le PATH et sort en code 49 sans rien produire, si bien qu'un "
    "`command -v` le choisirait et que la valeur engendrée serait vide."
)


def _bornes(secret: "Secret") -> str:
    """Le test shell des bornes, DÉRIVÉ de `Shape`.

    Les bornes viennent du prédicat qui juge la valeur stockée, jamais d'un
    nombre recopié : l'avant-vol et l'après-vol ne peuvent donc pas diverger.
    """
    lo, hi = secret.shape.minimum, secret.shape.maximum
    if lo == hi:
        return f'[ "$N" -eq {lo} ]'
    return f'[ "$N" -ge {lo} ] && [ "$N" -le {hi} ]'


def _ecriture_gardee(secret, secret_id: str, projet: str, var: str) -> str:
    """L'écriture SOUS la garde, et la version capturée.

    Jusqu'au 2026-09-11 le contrôle affichait « REFUSER, ne rien écrire » et
    la commande d'écriture vivait EN DEHORS du `if` : coller le bloc écrivait
    quoi que dise le verdict. Le shell comparait ; il n'agissait pas. Une
    consigne de sécurité qui demande à un humain d'obéir à un mot imprimé,
    au moment précis où il tient un secret, n'est pas une garde.

    La version écrite est CAPTURÉE (`--format='value(name)'`) parce que la
    relecture doit viser celle-là et non `latest` — voir `_relecture_gardee`.
    """
    return (
        f"N=$(printf '%s' \"${var}\" | wc -c | tr -d ' ')\n"
        f"if {_bornes(secret)}; then\n"
        f"  VERSION=$(printf '%s' \"${var}\" | gcloud secrets versions add "
        f"{secret_id} \\\n"
        f"    --project={projet} --data-file=- --format='value(name)')\n"
        "  printf 'écrit : %s octets, version %s\\n' \"$N\" "
        '\"${VERSION##*/}\"\n'
        "else\n"
        '  VERSION=""\n'
        "  printf \"longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été "
        'écrit\\n\" \"$N\"\n'
        "fi"
    )


def _relecture_gardee(secret, secret_id: str, projet: str) -> str:
    """Relire LA VERSION QU'ON VIENT D'ÉCRIRE, jamais `latest`.

    `latest` est un piège de fausse assurance : si l'écriture échoue, il
    désigne la version PRÉCÉDENTE — qui a toutes les chances d'avoir la bonne
    longueur, puisqu'elle fonctionnait. Le contrôle d'après-vol répondait donc
    « conforme » sur une écriture qui n'avait pas eu lieu.
    """
    return (
        'if [ -z "$VERSION" ]; then\n'
        "  printf 'aucune version écrite — rien à relire\\n'\n"
        "else\n"
        '  N=$(gcloud secrets versions access \"${VERSION##*/}\" '
        f"--secret={secret_id} \\\n"
        f"    --project={projet} | wc -c | tr -d ' ')\n"
        f"  if {_bornes(secret)}; then\n"
        "    printf 'relu : %s octets — conforme\\n' \"$N\"\n"
        "  else\n"
        "    printf 'relu : %s octets — NE CORRESPOND PAS\\n' \"$N\"\n"
        "  fi\n"
        "fi"
    )


def gcloud_recipe(secret_id: str, project_id: str = "") -> list:
    """Les commandes qui écrivent une NOUVELLE VERSION de ce secret.

    Rend une liste de couples (commentaire français, commande). Table FERMÉE :
    un `secret_id` inconnu rend une liste vide, jamais une commande
    construite autour de lui — la page qui rend cela existe pour qu'on y
    copie des lignes de shell, donc une chaîne arbitraire ne doit pas pouvoir
    y devenir une commande.

    `project_id` vide rend `$PROJECT` plutôt qu'une chaîne vide interpolée :
    une commande à `--project=` vide vise le projet actif de `gcloud`, ce qui
    est précisément comment on écrit dans le projet de quelqu'un d'autre.

    Les cinq propriétés qui font la valeur de ces lignes :

    1. `IFS= read -rs` — la valeur ne paraît pas à l'écran, n'entre pas dans
       l'historique du shell, et `IFS=` empêche que les blancs de tête et de
       queue soient mangés (ce qui masquerait le défaut qu'on cherche).
       ⚠ `read` ne retient que la PREMIÈRE LIGNE. Les six secrets de cette
       table sont tous d'une seule ligne par construction (un jeton, une clé
       d'API, une empreinte bcrypt, un secret Entra), donc un collage
       multiligne signifie qu'on a collé la mauvaise chose — et le collage
       tronqué a l'air complet à l'écran. C'est le contrôle d'avant-vol qui l'attrape, et
       seulement parce qu'elle COMPARE au lieu de montrer.
    2. `printf '%s'` — `printf` est une PRIMITIVE du shell, donc la valeur ne
       passe jamais par `/proc/*/cmdline` ; et `'%s'` n'ajoute AUCUN saut de
       ligne, ce qui est tout le piège.
    3. `--data-file=-` — jamais `--data=`, qui déposerait la valeur dans
       `ps`.
    4. L'écho entre crochets — une espace de tête ou de queue devient
       VISIBLE, ce qu'aucune console ne montre autrement.
    5. Le COMPTE D'OCTETS, comparé par le shell, aux deux bouts du vol.
       C'est ce qui rend inutile un validateur côté navigateur : `.strip()` de
       Python et `.trim()` de JavaScript ne s'accordent pas sur six
       caractères, dont la MOM d'un collage Windows. Et c'est le seul contrôle
       qui voie une nomenclature d'octets en TÊTE aussi bien qu'un
       terminateur en queue — un `xxd | tail -1` ne regarde que la fin.

    `wc -c` compte des OCTETS, jamais des caractères — `${#VALEUR}` compterait
    des caractères et dirait 11 pour « café-secret », qui en fait 12. La
    recette n'emploie donc nulle part `${#…}`.
    """
    secret = secret_by_id(secret_id)
    if secret is None:
        return []
    # DÉSORMAIS l'identifiant CANONIQUE, plus l'argument : la garde ci-dessus
    # prouve déjà qu'ils sont égaux, mais lire la table plutôt que l'entrée
    # fait de « aucune chaîne arbitraire n'entre dans une commande » une
    # propriété de STRUCTURE, qui survivrait même au retrait de la garde.
    secret_id = secret.secret_id
    projet = project_id or "$PROJECT"
    attendu = expected_length_fr(secret)

    if secret.origin == ORIGIN_PASSWORD:
        return [
            (_SONDE_FR, _SONDE_PY),
            (
                "Vérifier que bcrypt est présent DANS cet interpréteur — et "
                "non ailleurs. L'ancienne ligne `-m pip install` ne marche "
                "pas partout : le venv de ce dépôt n'a pas `pip` du tout "
                "(« No module named pip », mesuré), et bcrypt y est déjà "
                "puisque c'est une dépendance épinglée. On constate donc au "
                "lieu d'installer à l'aveugle.",
                '"$PY" -c \'import bcrypt\' 2>/dev/null'
                " && printf 'bcrypt : présent\\n'"
                " || printf 'bcrypt ABSENT de cet interpréteur — "
                "à installer avant de continuer\\n'",
            ),
            (
                "Calculer l'empreinte. Le mot de passe n'est jamais un "
                "argument ni une variable — `getpass` le lit du terminal. "
                "L'EMPREINTE, elle, peut vivre dans une variable : c'est un "
                "condensé, et c'est ce qui permet de la mesurer AVANT de "
                "l'écrire, contrôle que la forme fusionnée précédente ne "
                "pouvait pas offrir.",
                'EMPREINTE=$("$PY" -c \'import bcrypt, getpass, sys; '
                "sys.stdout.write(bcrypt.hashpw(getpass.getpass("
                '"Mot de passe DAV : ").encode(), bcrypt.gensalt())'
                ".decode())')",
            ),
            (
                f"Mesurer, puis écrire SEULEMENT si la longueur est "
                f"{attendu}. Une empreinte de 61 octets n'égalera jamais les "
                "60 que `bcrypt.checkpw` recalcule, et DavX5 cesse alors de "
                "synchroniser sans un mot.",
                _ecriture_gardee(secret, secret_id, projet, "EMPREINTE"),
            ),
            ("Effacer l'empreinte de la session.", "unset EMPREINTE"),
            (
                "Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`, "
                "qui désignerait la version précédente si l'écriture avait "
                "échoué et rassurerait donc à tort.",
                _relecture_gardee(secret, secret_id, projet),
            ),
        ]

    etapes = []
    if secret.origin == ORIGIN_GENERATED:
        etapes.append((_SONDE_FR, _SONDE_PY))
        etapes.append((
            "Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` "
            "par discipline : aucune ligne de cette recette n'émet de saut de "
            "ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : "
            "c'est le générateur cryptographique de la bibliothèque standard, "
            "ce qui vaut de dépendre d'un interpréteur.",
            """VALEUR=$("$PY" -c 'import secrets, sys; """
            """sys.stdout.write(secrets.token_urlsafe(32))')""",
        ))
    else:
        etapes.append((
            "Coller la valeur remise par la console, puis Entrée. Rien ne "
            "s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger "
            "les blancs — s'il y en a, on veut les VOIR, pas les perdre en "
            "silence. ⚠ `read` ne retient que la PREMIÈRE LIGNE : les six "
            "secrets sont d'une seule ligne par construction, donc un collage "
            "multiligne signifie qu'on a collé la mauvaise chose — et le "
            "collage tronqué a l'air complet à l'écran. C'est la mesure qui "
            "l'attrape, pas l'œil.",
            "IFS= read -rs VALEUR",
        ))

    etapes.append((
        "Voir ce qui a réellement été saisi. Les crochets rendent visible une "
        "espace de tête ou de queue, qu'aucune console ne montre autrement.",
        'printf \'[%s]\\n\' "$VALEUR"',
    ))
    etapes.append((
        f"Mesurer, puis écrire SEULEMENT si la longueur est {attendu} octets. "
        "La mesure est une GARDE, pas un avis : la commande d'écriture vit "
        "DANS le `if`, donc coller le bloc entier ne peut pas écrire une "
        "valeur que la mesure vient de refuser. `printf` est une primitive du "
        "shell, donc la valeur ne devient l'argument d'aucun processus ; "
        "`--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; "
        "et `versions add`, jamais `secrets create` — le secret existe déjà.",
        _ecriture_gardee(secret, secret_id, projet, "VALEUR"),
    ))
    etapes.append((
        "Effacer la variable de la session.",
        "unset VALEUR",
    ))
    etapes.append((
        "Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si "
        "l'écriture avait échoué, `latest` désignerait la version précédente, "
        "qui a toutes les chances d'être de bonne longueur puisqu'elle "
        "fonctionnait : l'après-vol rassurerait alors sur une écriture qui "
        "n'a pas eu lieu.",
        _relecture_gardee(secret, secret_id, projet),
    ))
    return etapes


# ── Runtime environment ──────────────────────────────────────────────────

# Read with bracket ``os.environ[...]`` in the class body of ``config.py``, so
# the module raises at IMPORT without them. FIREBASE_PROJECT_ID is the root of
# the three: ``_from_secret_manager`` uses it to build the resource path, so
# nothing — not one secret — resolves without it.
REQUIRED_ENV: tuple[str, ...] = (
    "FIREBASE_PROJECT_ID",
    "FIREBASE_STORAGE_BUCKET",
    "AUTHORIZED_USER_EMAIL",
)

# Controls that DISABLE THEMSELVES when unset, and are not equally loud about
# it. App Check logs a warning in production; the origin-secret check says
# nothing at all — ``if not secret: return None``, no log, no metric. That
# silence is why ``cf-origin-secret`` never existed in Secret Manager for
# months with nothing signalling it.
FAIL_OPEN_ENV: dict[str, str] = {
    "RECAPTCHA_ENTERPRISE_SITE_KEY": (
        "Firebase App Check est désactivé (les requêtes HTMX ne sont pas "
        "vérifiées)."
    ),
    "CF_ORIGIN_SECRET": (
        "Le contrôle du secret d'origine Cloudflare est désactivé (l'accès "
        "direct à App Engine n'est pas bloqué)."
    ),
    "DAV_PASSWORD_HASH": (
        "L'authentification DAV ne peut pas réussir — la synchro DavX5 est "
        "indisponible."
    ),
}

# Of those, the ones production resolves from SECRET MANAGER rather than the
# environment. Reporting them "unset in production" from ``os.environ`` is a
# FALSE NEGATIVE by construction: ``config._secret()`` never consults the env
# var when ENV=production. The secret pass is the authority for these.
SECRET_BACKED_ENV: dict[str, str] = {
    "CF_ORIGIN_SECRET": "cf-origin-secret",
    "DAV_PASSWORD_HASH": "dav-password-hash",
}


# ── Fork hygiene ─────────────────────────────────────────────────────────
# Values hardcoded to the original deployment. An adopter must replace every
# one before going live — this is the "somebody cloned the repository" check.

OWNER_LITERALS: dict[str, str] = {
    "GCP project id": "athena-pallas",
    "Storage bucket": "athena-pallas.firebasestorage.app",
    "Firebase app id": "1:1073430388324:web:6d60d0d67d74ce1b730971",
    "Authorized user email": "jason@poirierlavoie.ca",
    "Firm name": "Me Jason Poirier Lavoie",
    "reCAPTCHA site key": "6LeR9Y0sAAAAAJhkKWa_wYdbproZUQFjEO7FkfEb",
    "MCP canonical origin": "https://athena.poirierlavoie.ca",
    "Domain": "athena.poirierlavoie.ca",
    "TWA package": "ca.poirierlavoie.athena",
    # Added with the inventory extraction: these live in portail.yaml,
    # client/config.py and dispatch.yaml, none of which the scan covered.
    "Portal host": "portail.poirierlavoie.ca",
    "Portal service account": "portail-svc@athena-pallas.iam.gserviceaccount.com",
    "Firm phone": "(514) 737-2525",
    "Firm email": "reception@poirierlavoie.ca",
    "Graph tenant id": "4c5c39a5-2e63-4b04-8408-973c58cd88c7",
    "Graph client id": "988bf117-3aef-463b-8062-7dac226e50d9",
}

# The TWA signing fingerprint is a PATTERN, not a literal value.
OWNER_FINGERPRINT_RE = "47:3B:05:FB"

# Files that legitimately carry deployment config. Docs and tests are excluded
# on purpose — they cite the owner's ids as examples.
#
# ``portail.yaml``, ``client/config.py`` and ``dispatch.yaml`` were MISSING
# here until the extraction, so the owner literals they carry went unchecked.
# ``tests/test_deployment_inventory.py`` now derives this list's completeness:
# it walks the repo's config files and fails if any file containing an
# OWNER_LITERALS value is absent — which fixes the class of bug rather than
# this instance of it.
SCAN_FILES: tuple[str, ...] = (
    "athena/app.yaml",
    "athena/portail.yaml",
    # Repo ROOT, not athena/ — the first draft of this list said
    # "athena/dispatch.yaml", which does not exist, and check_config skips a
    # missing path silently. The derived completeness test below is what
    # caught it.
    "dispatch.yaml",
    "athena/config.py",
    "athena/main.py",
    "athena/client/config.py",
)

__all__ = [
    "FAIL_OPEN_ENV",
    "ORIGIN_EXTERNAL",
    "ORIGIN_GENERATED",
    "ORIGIN_PASSWORD",
    "SHAPE_FAIL",
    "SHAPE_WARN",
    "ShapeVerdict",
    "OWNER_FINGERPRINT_RE",
    "OWNER_LITERALS",
    "REQUIRED_ENV",
    "SCAN_FILES",
    "SECRETS",
    "SECRET_BACKED_ENV",
    "SECRET_IDS",
    "SERVICES",
    "SERVICE_DEFAULT",
    "SERVICE_PORTAIL",
    "Secret",
    "Shape",
    "WRITABLE_SECRET_IDS",
    "expected_length_fr",
    "gcloud_recipe",
    "secret_by_id",
    "stray_whitespace",
]
