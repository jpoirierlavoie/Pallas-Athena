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


def _shape_ascii_token(
    minimum: int, maximum: int
) -> Callable[[str], Optional[ShapeVerdict]]:
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

    return check


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
    refusal: str = ""
    shape: Optional[Callable[[str], Optional[ShapeVerdict]]] = None

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
        shape=_shape_bcrypt,
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
        shape=_shape_ascii_token(32, 128),
    ),
    Secret(
        secret_id="graph-client-secret",
        env_var="GRAPH_CLIENT_SECRET",
        label="Secret client Microsoft Graph",
        required_for=frozenset(),
        consequence="le courriel sortant est désactivé",
        writable=True,
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
    "WRITABLE_SECRET_IDS",
    "secret_by_id",
    "stray_whitespace",
]
