"""L'inventaire de déploiement — la table PARTAGÉE et ses prédicats purs.

Trois épingles portent ce fichier, et chacune répare une classe de défaut
plutôt qu'une instance :

* **La pureté, par l'AST.** `deployment_inventory` et `deployment_report`
  n'importent que la bibliothèque standard. C'est cette propriété qui les rend
  importables par une route Flask, par un script CLI et par un test sans
  identifiants — le précédent `mcp/coverage.py`.
* **La complétude de `SCAN_FILES`, par DÉRIVATION.** On parcourt les fichiers
  de configuration du dépôt et on échoue si l'un d'eux porte un littéral du
  propriétaire sans figurer dans la liste. C'est le test qui aurait attrapé
  l'omission de `portail.yaml` — et il a effectivement attrapé, à l'écriture,
  un `athena/dispatch.yaml` inexistant (le fichier est à la RACINE) que
  `check_config` sautait en silence.
* **`REQUIRED_ENV`, par dérivation contre `config.py`.** Les trois variables
  obligatoires sont celles que `config.py` lit par `os.environ[...]` —
  l'accès par crochets, qui lève à l'import. Les recopier à la main serait
  reproduire le défaut que ce module existe pour supprimer.
"""

import ast
import io
import os
import re
import sys

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_ATHENA)
sys.path.insert(0, _ATHENA)

from utils.deployment_inventory import (  # noqa: E402
    FAIL_OPEN_ENV,
    OWNER_FINGERPRINT_RE,
    OWNER_LITERALS,
    REQUIRED_ENV,
    SCAN_FILES,
    SECRET_BACKED_ENV,
    SECRET_IDS,
    SECRETS,
    SHAPE_FAIL,
    SHAPE_WARN,
    WRITABLE_SECRET_IDS,
    secret_by_id,
    stray_whitespace,
)
from utils.deployment_report import (  # noqa: E402
    FAIL,
    OK,
    WARN,
    Report,
    render_json,
    render_text,
)

_STDLIB_OK = {"re", "dataclasses", "typing", "enum"}


def _top_level_imports(rel_path: str) -> set:
    tree = ast.parse(io.open(os.path.join(_ATHENA, rel_path), encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# ── Pureté ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module", ["utils/deployment_inventory.py", "utils/deployment_report.py"]
)
def test_the_shared_modules_import_only_the_standard_library(module):
    """Ce qui les rend importables par une route, un script ET un test."""
    extra = _top_level_imports(module) - _STDLIB_OK
    assert not extra, f"{module} importe hors stdlib : {sorted(extra)}"


def test_the_inventory_calls_no_builtin_open():
    """L'épingle d'imports ci-dessus couvre `os`/`subprocess`/`pathlib` : ils
    ne sont pas importés, donc inutilisables. Reste `open`, qui est une
    fonction NATIVE — seul l'AST peut le voir.

    (Premier jet : une recherche TEXTUELLE de « subprocess », qui tombait sur
    le docstring du module disant qu'il n'en importe pas. Même erreur que
    chercher du code dans de la prose.)
    """
    tree = ast.parse(
        io.open(os.path.join(_ATHENA, "utils", "deployment_inventory.py"),
                encoding="utf-8").read()
    )
    appels = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "open" not in appels
    assert "eval" not in appels and "exec" not in appels


# ── Les six secrets ──────────────────────────────────────────────────────

def test_there_are_six_secrets_not_four():
    """`check_config` en vérifiait quatre et DEPLOYMENT.md §4.2 l'écrivait
    encore, alors que `portail-secret-key` et `graph-client-secret` existent."""
    assert len(SECRETS) == 6
    assert set(SECRET_IDS) == {
        "flask-secret-key",
        "portail-secret-key",
        "firebase-api-key",
        "dav-password-hash",
        "cf-origin-secret",
        "graph-client-secret",
    }


def test_secret_ids_are_unique():
    assert len(set(SECRET_IDS)) == len(SECRET_IDS)


def test_the_two_hard_secrets_are_required():
    """Chacun empêche le démarrage de SON service."""
    assert secret_by_id("flask-secret-key").required
    assert secret_by_id("portail-secret-key").required


def test_exactly_four_secrets_are_writable():
    assert set(WRITABLE_SECRET_IDS) == {
        "firebase-api-key",
        "dav-password-hash",
        "cf-origin-secret",
        "graph-client-secret",
    }


@pytest.mark.parametrize("sid", ["flask-secret-key", "portail-secret-key"])
def test_the_refused_secrets_carry_their_reason(sid):
    """Un refus sans motif se lit comme un oubli, et la main suivante le
    « répare »."""
    secret = secret_by_id(sid)
    assert secret.writable is False
    assert len(secret.refusal) > 80


def test_every_secret_names_its_consequence():
    for secret in SECRETS:
        assert secret.consequence.strip(), secret.secret_id
        assert secret.label.strip(), secret.secret_id


# ── La forme bcrypt — la validation la plus utile du lot ─────────────────

def test_a_real_bcrypt_hash_is_accepted():
    import bcrypt

    shape = secret_by_id("dav-password-hash").shape
    for _ in range(3):
        assert shape(bcrypt.hashpw(b"mot-de-passe", bcrypt.gensalt()).decode()) is None


def test_a_bcrypt_hash_with_a_trailing_newline_is_refused():
    """LA régression à épingler. Le premier jet employait `^…$`, et le `$` de
    Python matche AUSSI juste avant un saut de ligne final — il acceptait donc
    les 61 caractères qu'il existe pour refuser, exactement le défaut que
    `normalize_email` a documenté. D'où `\\Z` plus un contrôle de longueur."""
    import bcrypt

    shape = secret_by_id("dav-password-hash").shape
    bon = bcrypt.hashpw(b"x", bcrypt.gensalt()).decode()
    assert shape(bon) is None
    for suffixe in ("\n", "\r\n", " ", "\t"):
        assert shape(bon + suffixe), repr(suffixe)


@pytest.mark.parametrize(
    "mauvais", ["motdepasse", "", "$2b$12$trop-court", "x" * 60]
)
def test_a_non_bcrypt_value_is_refused(mauvais):
    assert secret_by_id("dav-password-hash").shape(mauvais)


def test_the_bcrypt_error_never_echoes_the_value():
    """Un message d'erreur est le dernier endroit où un secret doit paraître."""
    err = secret_by_id("dav-password-hash").shape("SENTINELLE-SECRETE")
    # `.message`, jamais le verdict nu : `"X" not in ShapeVerdict(...)` teste
    # l'APPARTENANCE aux deux membres du tuple, donc la version nue passait
    # quoi que dise le message. Un test qui passe pour la mauvaise raison est
    # pire qu'aucun test.
    assert err and "SENTINELLE-SECRETE" not in err.message


def test_a_bcrypt_cost_written_in_unicode_digits_is_refused():
    """Le second des deux défauts VIVANTS du 2026-09-11.

    Le `\\d` de Python est UNICODE : « ١٢ » (chiffres arabo-indiens) sont des
    chiffres pour `re`, si bien que cette chaîne de 60 caractères MATCHAIT
    `_BCRYPT_RE`. Elle passait donc `_shape_bcrypt`, puis échouait
    `bcrypt.checkpw` — que `dav/dav_auth.py` avale dans un `except` nu. C'est
    exactement la panne DavX5 silencieuse que ce prédicat existe pour
    supprimer, entrée par la porte du prédicat lui-même.
    """
    faux = "$2b$" + "\u0661\u0662" + "$" + "a" * 53
    assert len(faux) == 60, "le cas ne vaut que s'il a la BONNE longueur"
    verdict = secret_by_id("dav-password-hash").shape(faux)
    assert verdict and verdict.severity == SHAPE_FAIL


# ── Le jeu de caractères d'un jeton — l'autre défaut vivant ─────────────

def _origine():
    return secret_by_id("cf-origin-secret").shape


def test_twenty_real_tokens_are_accepted():
    """Le contrôle doit d'abord ne PAS mentir sur un déploiement qui marche.

    Un contrôle qui fait rougir une valeur correcte cesse d'être lu, et un
    contrôle qu'on ne lit plus est l'état dans lequel `cf-origin-secret` a
    passé des mois. C'est la moitié du lot qu'on oublie d'écrire.
    """
    import secrets

    shape = _origine()
    for _ in range(20):
        assert shape(secrets.token_urlsafe(32)) is None


@pytest.mark.parametrize(
    "mauvais,pourquoi",
    [
        ("é", "lettre accentuée"),
        ("\u2019", "apostrophe typographique (Word)"),
        ("\u00a0", "espace insécable"),
        ("\u200b", "espace de largeur nulle"),
    ],
)
def test_a_non_ascii_character_FAILS_because_it_is_a_500_storm(mauvais, pourquoi):
    """`hmac.compare_digest` LÈVE `TypeError` sur une chaîne non ASCII, et
    `_enforce_origin_secret` l'appelle sur CHAQUE requête : la conséquence
    n'est pas le 403 documenté du saut de ligne, c'est une 500 à chaque
    requête, avec une trace par requête. L'ancien prédicat disait « ok »."""
    valeur = "a" * 42 + mauvais
    verdict = _origine()(valeur)
    assert verdict and verdict.severity == SHAPE_FAIL, pourquoi


@pytest.mark.parametrize("mauvais", ["\x00", "\n", "\x1f", "\x7f"])
def test_a_control_character_FAILS_because_a_header_cannot_carry_it(mauvais):
    """Un caractère de contrôle INTÉRIEUR échappe à `stray_whitespace`, qui ne
    regarde que les extrémités. Un en-tête HTTP ne peut pas le transporter :
    la valeur reçue ne pourra jamais égaler celle qui est stockée."""
    verdict = _origine()("a" * 20 + mauvais + "b" * 21)
    assert verdict and verdict.severity == SHAPE_FAIL


def test_an_interior_space_WARNS_rather_than_FAILS():
    """Mesuré plutôt que supposé : `hmac.compare_digest("x", "ab c")` NE lève
    PAS, une espace est un caractère légal dans une valeur d'en-tête HTTP, et
    `IFS= read -rs` la conserve. Un tel déploiement FONCTIONNE — le rougir
    serait exactement la faute que le classement en deux crans existe pour
    éviter. Mais l'espace est invisible dans une console, donc on le dit."""
    verdict = _origine()("a" * 21 + " " + "b" * 21)
    assert verdict and verdict.severity == SHAPE_WARN
    assert "espace" in verdict.message


def test_standard_base64_WARNS_rather_than_FAILS():
    verdict = _origine()("a" * 40 + "+/=")
    assert verdict and verdict.severity == SHAPE_WARN


def test_a_length_outside_the_bounds_still_FAILS():
    assert _origine()("court").severity == SHAPE_FAIL
    assert _origine()("a" * 129).severity == SHAPE_FAIL


def test_no_token_verdict_ever_echoes_the_value():
    """Un message d'erreur est le dernier endroit où un secret doit paraître :
    il voyage dans `detail_fr`, qui est rendu à l'écran et journalisé."""
    for valeur in ("SENTINELLE\u2019SECRETE", "SENTINELLE SECRETE" + "a" * 25,
                   "SENTINELLE\x00SECRETE" + "a" * 22, "SENTINELLE"):
        verdict = _origine()(valeur)
        assert verdict, valeur
        assert "SENTINELLE" not in verdict.message, verdict.message


# ── Les blancs parasites ─────────────────────────────────────────────────

def test_a_clean_value_has_no_stray_whitespace():
    assert stray_whitespace("abc123") is None
    assert stray_whitespace("") is None


@pytest.mark.parametrize(
    "value,cote",
    [("abc\n", "fin"), ("abc ", "fin"), (" abc", "début"), ("\tabc", "début")],
)
def test_stray_whitespace_names_the_side(value, cote):
    message = stray_whitespace(value)
    assert message and cote in message


def test_stray_whitespace_reports_both_sides():
    message = stray_whitespace(" abc\n")
    assert message and "début" in message and "fin" in message


def test_stray_whitespace_never_echoes_the_value():
    message = stray_whitespace("SENTINELLE\n")
    assert message and "SENTINELLE" not in message


# ── La complétude de SCAN_FILES, DÉRIVÉE ────────────────────────────────

def _fichiers_de_configuration() -> list:
    """Les surfaces de configuration du dépôt.

    Docs et tests sont exclus À DESSEIN : ils citent les identifiants du
    propriétaire comme exemples, ce que le commentaire d'origine disait déjà.
    """
    trouves = []
    for base, dirs, files in os.walk(_ROOT):
        dirs[:] = [
            d
            for d in dirs
            if d not in (".git", ".venv", "node_modules", "__pycache__", "tests", "docs")
        ]
        for nom in files:
            if nom.endswith(".yaml") or nom in ("config.py", "main.py"):
                chemin = os.path.join(base, nom)
                trouves.append(os.path.relpath(chemin, _ROOT).replace("\\", "/"))
    return trouves


def test_every_config_file_carrying_an_owner_literal_is_scanned():
    """LE test de ce fichier. Il répare la CLASSE de défaut — une liste tenue
    à la main se périme, et CLAUDE.md l'avait prédit par écrit — au lieu de
    l'instance. Il a d'ailleurs attrapé, à l'écriture, un
    « athena/dispatch.yaml » inexistant que `check_config` sautait en silence
    alors que le vrai fichier, à la racine, porte deux littéraux."""
    manquants = {}
    for rel in _fichiers_de_configuration():
        if rel in SCAN_FILES:
            continue
        try:
            texte = io.open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        except OSError:
            continue
        porte = sorted(k for k, v in OWNER_LITERALS.items() if v in texte)
        if porte:
            manquants[rel] = porte
    assert not manquants, (
        "des fichiers de configuration portent des littéraux du propriétaire "
        f"sans figurer dans SCAN_FILES : {manquants}"
    )


def test_every_scanned_file_exists():
    """`check_config` saute un chemin absent EN SILENCE, donc une faute de
    frappe dans la liste désarme un contrôle sans rien dire."""
    absents = [
        rel for rel in SCAN_FILES if not os.path.exists(os.path.join(_ROOT, rel))
    ]
    assert not absents, absents


def test_scan_files_paths_are_relative():
    for rel in SCAN_FILES:
        assert not os.path.isabs(rel), rel


# ── REQUIRED_ENV, dérivé de config.py ───────────────────────────────────

def test_required_env_matches_what_config_reads_with_brackets():
    """Les variables obligatoires sont celles que `config.py` lit par
    `os.environ[...]` — l'accès par crochets, qui lève un KeyError à
    l'import. Dérivé, jamais recopié."""
    tree = ast.parse(
        io.open(os.path.join(_ATHENA, "config.py"), encoding="utf-8").read()
    )
    brackets = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            brackets.add(node.slice.value)
    assert set(REQUIRED_ENV) == brackets, (
        f"REQUIRED_ENV={sorted(REQUIRED_ENV)} vs config.py={sorted(brackets)}"
    )


def test_the_fail_open_controls_are_the_three_that_disable_themselves():
    assert set(FAIL_OPEN_ENV) == {
        "RECAPTCHA_ENTERPRISE_SITE_KEY",
        "CF_ORIGIN_SECRET",
        "DAV_PASSWORD_HASH",
    }


def test_secret_backed_env_maps_onto_real_secret_ids():
    """Sinon le renvoi « voir la passe 1b » pointerait vers un secret que la
    passe 1b n'examine pas."""
    for env_var, secret_id in SECRET_BACKED_ENV.items():
        assert env_var in FAIL_OPEN_ENV, env_var
        assert secret_id in SECRET_IDS, secret_id


def test_the_owner_fingerprint_is_a_usable_pattern():
    assert re.compile(OWNER_FINGERPRINT_RE)


# ── Les consommateurs partagent la table ────────────────────────────────


def _lire(*parties) -> str:
    return io.open(os.path.join(_ATHENA, *parties), encoding="utf-8").read()


def test_the_checks_import_the_shared_tables():
    """Deux inventaires de la même infrastructure EST le défaut."""
    src = _lire("utils", "config_checks.py")
    assert "from utils.deployment_inventory import" in src
    assert "from utils.deployment_report import" in src
    for redefini in (
        "OWNER_LITERALS: dict[str, str] = {",
        "REQUIRED_ENV = (",
        "FAIL_OPEN_ENV = {",
        "SECRET_BACKED_ENV = {",
        "class Report",
    ):
        assert redefini not in src, redefini


def test_the_checks_iterate_the_six_secrets():
    src = _lire("utils", "config_checks.py")
    assert "for secret in SECRETS:" in src
    # L'ancien 4-uplet littéral ne doit pas revenir.
    assert '("flask-secret-key", True,' not in src


def test_the_checks_distinguish_a_denial_from_an_absence():
    """`portail-secret-key` appartient à un autre compte de service : le lire
    depuis le service principal ÉCHOUE sur un déploiement CORRECT. Le
    rapporter comme absent serait une fausse alerte."""
    src = _lire("utils", "config_checks.py")
    assert "PermissionDenied" in src
    assert 'outcome="unreadable"' in src


def test_check_config_is_a_thin_cli_over_run_all():
    """La logique a quitté `scripts/` pour qu'une route puisse l'appeler sans
    importer une fonction privée d'un script."""
    src = _lire("scripts", "check_config.py")
    assert "from utils.config_checks import run_all" in src
    for parti in ("def check_runtime_env", "def check_prod_secrets",
                  "def check_owner_literals", "for secret in SECRETS:"):
        assert parti not in src, parti


def test_the_secret_verdicts_are_evaluated_in_the_ONE_order_that_works():
    """L'ordre des trois verdicts est load-bearing, et il n'est pas devinable.

    `stray_whitespace("")` rend `None` — c'est correct, une chaîne vide n'a
    pas de blanc parasite —, donc si le test de vacuité ne venait pas EN
    PREMIER, un secret vide traverserait sans un mot. Et la forme doit venir
    après le blanc parasite : sur `"abc\\n"`, le verdict utile est « il y a un
    saut de ligne à la fin », pas « longueur inattendue : 4 ».

    Épinglé sur l'AST plutôt qu'en prose, parce qu'un ordre qui ne vit que
    dans un commentaire est un ordre qu'une réécriture change sans le voir.
    """
    tree = ast.parse(_lire("utils", "config_checks.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "check_prod_secrets"
    )

    ligne_vide = ligne_blanc = ligne_forme = None
    for node in ast.walk(fn):
        # `if not payload:`
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "payload"
            and ligne_vide is None
        ):
            ligne_vide = node.lineno
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "stray_whitespace"
                and ligne_blanc is None
            ):
                ligne_blanc = node.lineno
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "shape"
                and ligne_forme is None
            ):
                ligne_forme = node.lineno

    assert ligne_vide and ligne_blanc and ligne_forme, (
        ligne_vide, ligne_blanc, ligne_forme
    )
    assert ligne_vide < ligne_blanc < ligne_forme, (
        "ordre attendu : vacuité < blancs parasites < forme ; obtenu "
        f"{ligne_vide} / {ligne_blanc} / {ligne_forme}"
    )


def test_an_unusual_shape_is_a_WARN_at_the_call_site_too():
    """Le classement en deux crans ne vaut que si l'APPELANT le respecte.

    `if shape_error:` traitait tout verdict en échec ; avec un verdict à deux
    crans, ce même code ferait rougir un déploiement qui fonctionne — et un
    contrôle qui rougit à tort cesse d'être lu. Le balayage interdit le
    retour de la forme unique.
    """
    src = _lire("utils", "config_checks.py")
    assert "verdict.severity == SHAPE_FAIL" in src
    assert "shape_error" not in src, "l'ancienne forme à un seul cran est revenue"


def test_no_emit_call_passes_the_payload():
    """Le contrat « shape only », épinglé par l'AST.

    Une recherche textuelle ne marche pas : `len(payload)` et
    `stray_whitespace(payload)` sont légitimes — ils calculent des FAITS sur
    la valeur. Ce qui est interdit, c'est de passer la valeur elle-même à
    `emit`. Seul l'arbre distingue les deux.
    """
    tree = ast.parse(_lire("utils", "config_checks.py"))
    fautifs = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
        ):
            continue
        passes = list(node.args) + [kw.value for kw in node.keywords]
        for arg in passes:
            if isinstance(arg, ast.Name) and arg.id == "payload":
                fautifs.append(node.lineno)
    assert not fautifs, f"emit() reçoit la valeur du secret aux lignes {fautifs}"
    # Et la longueur, elle, DOIT en sortir — c'est le fait qui aurait attrapé
    # le cas du saut de ligne (44 octets au lieu de 43).
    assert "len(payload)" in _lire("utils", "config_checks.py")


def test_run_all_produces_rows_without_printing(capsys):
    from utils.config_checks import run_all

    rpt = run_all(is_prod=False)
    assert capsys.readouterr().out == ""
    assert rpt.rows
    assert {r.section for r in rpt.rows}


# ── Le rapport ───────────────────────────────────────────────────────────

def test_a_report_accumulates_without_printing(capsys):
    rpt = Report()
    rpt.section("1. Runtime")
    rpt.emit(OK, "tout va bien")
    assert capsys.readouterr().out == ""
    assert len(rpt.rows) == 1


def test_a_report_with_a_printer_reproduces_the_cli_shape(capsys):
    rpt = Report(printer=print)
    rpt.section("1. Runtime")
    rpt.emit(OK, "FIREBASE_PROJECT_ID is set")
    sortie = capsys.readouterr().out
    assert "1. Runtime\n----------\n" in sortie
    assert "  [  ok ] FIREBASE_PROJECT_ID is set" in sortie


def test_render_text_matches_the_live_printer(capsys):
    """Une seule vérité de format : sinon le CLI et le rendu divergent."""
    direct = Report(printer=print)
    accumule = Report()
    for rpt in (direct, accumule):
        rpt.section("Section A")
        rpt.emit(OK, "un")
        rpt.emit(WARN, "deux")
    assert capsys.readouterr().out.strip() == render_text(accumule).strip()


def test_counts_and_worst():
    rpt = Report()
    rpt.emit(OK, "a")
    assert rpt.worst() == OK
    rpt.emit(WARN, "b")
    assert rpt.worst() == WARN
    rpt.emit(FAIL, "c")
    assert rpt.worst() == FAIL
    assert rpt.counts() == {OK: 1, WARN: 1, FAIL: 1}


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError):
        Report().emit("CATASTROPHE", "x")


def test_sections_keep_first_seen_order():
    rpt = Report()
    rpt.section("B")
    rpt.emit(OK, "1")
    rpt.section("A")
    rpt.emit(OK, "2")
    rpt.section("B")
    rpt.emit(OK, "3")
    assert [name for name, _ in rpt.sections()] == ["B", "A"]


def test_render_json_carries_the_detail_dict():
    rpt = Report()
    rpt.section("S")
    rpt.emit(OK, "m", secret_id="cf-origin-secret", char_length=43)
    charge = render_json(rpt)
    assert charge["worst"] == OK
    ligne = charge["sections"][0]["rows"][0]
    assert ligne["detail"] == {"secret_id": "cf-origin-secret", "char_length": 43}
