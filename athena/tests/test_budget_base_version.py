"""Un budget se versionne par un COMPTEUR transactionnel, et un enregistrement
bâti sur une version dépassée est refusé (plan, lot 3a, étape 2).

``create_budget`` numérotait la nouvelle version à 1 + le maximum d'une
lecture NON transactionnelle : deux enregistrements parallèles (un double
envoi, un second onglet, le futur ``create_budget_version`` du connecteur)
lisaient le même historique et frappaient DEUX documents sous UN numéro.
Désormais le compteur ``counters/budget-{dossier_id}`` et les versions
stockées se lisent dans la transaction qui écrit, et ``base_version`` — la
version sur laquelle le formulaire a été bâti — refuse un enregistrement
qu'une autre version a devancé.

Le banc est le faux Firestore partagé (le client, ses transactions et la
boucle de reprise de ``transactional`` sont les vrais). Deux tests
échouaient sur le code d'avant (vérifié en le rétablissant) : la course
sans base frappait deux « v1 », et le formulaire web périmé enregistrait
par-dessus la version d'un autre onglet.
"""

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import budget as budget_model
    from models import provenance
    import routes.budgets as budgets_routes
    import routes.dossiers as dossiers_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
COUNTER = "counters/budget-dos1"
_BASE_INPUT = re.compile(r'name="base_version" value="([^"]*)"')


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "hourly_rate": 30000, "clients": [], "client_ids": [],
    })
    return f


def _seed_version(fake, bid: str, version: int, day: int, **over) -> None:
    fake.seed(f"budgets/{bid}", {
        "id": bid, "dossier_id": "dos1", "version": version,
        "hourly_rate": 30000, "note": "",
        "lines": [{"sous_phase": "PRE-01", "hours": 2.0, "frais_cents": 0}],
        "created_at": datetime(2026, 9, day, tzinfo=UTC),
        "updated_at": datetime(2026, 9, day, tzinfo=UTC),
        "etag": f"e{version}", **over,
    })


def _data(**over) -> dict:
    d = {
        "dossier_id": "dos1", "hourly_rate": 30000, "note": "",
        "lines": [{"sous_phase": "PRE-01", "hours": 3, "frais_cents": 0}],
    }
    d.update(over)
    return d


def _versions(fake) -> list[int]:
    return sorted(b["version"] for b in fake.peek_collection("budgets").values())


# ══════════════════════════════════════════════════════════════════════
# 1. Le compteur
# ══════════════════════════════════════════════════════════════════════


def test_the_first_version_starts_the_counter(fake):
    doc, errors = budget_model.create_budget(_data(), base_version=0)
    assert errors == [], errors
    assert doc["version"] == 1
    assert fake.peek(COUNTER)["seq"] == 1
    assert fake.peek(f"budgets/{doc['id']}")["version"] == 1


def test_a_history_minted_before_the_counter_seeds_it(fake):
    """Versions stored without a counter (every budget before lot 3a, and
    any an old instance mints during a rolling deploy) are counted: the
    higher of the counter and the stored maximum."""
    _seed_version(fake, "b1", 1, 1)
    _seed_version(fake, "b3", 3, 3)
    doc, errors = budget_model.create_budget(_data(), base_version=3)
    assert errors == [], errors
    assert doc["version"] == 4
    assert fake.peek(COUNTER)["seq"] == 4


def test_a_counter_ahead_of_the_stored_versions_wins(fake):
    _seed_version(fake, "b1", 1, 1)
    fake.seed(COUNTER, {"seq": 5})
    doc, errors = budget_model.create_budget(_data())
    assert errors == [] and doc["version"] == 6


def test_the_counter_and_the_versions_are_read_in_the_transaction(fake):
    _seed_version(fake, "b1", 1, 1)
    fake.reset_logs()
    _doc, errors = budget_model.create_budget(_data(), base_version=1)
    assert errors == []
    relevant = [r for r in fake.reads
                if any(p == COUNTER or p.startswith("budgets/") for p in r.paths)
                or r.rpc == "run_query"]
    assert relevant and all(r.transactional for r in relevant)
    (commit,) = [c for c in fake.commits if c.ops]
    kinds = dict((path, kind) for kind, path in commit.ops)
    assert kinds[COUNTER] == "set"
    assert [k for p, k in kinds.items() if p.startswith("budgets/")] == ["create"]


# ══════════════════════════════════════════════════════════════════════
# 2. La version de référence
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("stored, base", [(0, 1), (2, 1), (2, 0), (2, 3)])
def test_a_base_that_is_not_the_latest_writes_nothing(fake, stored, base):
    for v in range(1, stored + 1):
        _seed_version(fake, f"b{v}", v, v)
    fake.reset_logs()

    doc, errors = budget_model.create_budget(_data(), base_version=base)

    assert doc is None
    assert errors == [budget_model.BASE_VERSION_CONFLICT]
    assert budget_model.is_version_conflict(errors)
    assert _versions(fake) == list(range(1, stored + 1))
    assert fake.peek(COUNTER) is None
    assert [c for c in fake.commits if c.ops] == []


def test_no_base_asserts_nothing(fake):
    _seed_version(fake, "b1", 1, 1)
    doc, errors = budget_model.create_budget(_data())
    assert errors == [] and doc["version"] == 2


@pytest.mark.parametrize("bad", [-1, True, "2", 1.0])
def test_a_malformed_base_is_refused(fake, bad):
    doc, errors = budget_model.create_budget(_data(), base_version=bad)
    assert (doc, errors) == (None, [budget_model.BASE_VERSION_INVALID])
    assert fake.peek_collection("budgets") == {}


def test_a_stale_base_is_checked_before_the_validation(fake):
    """The useful answer to an outdated view is « re-read », not the first
    field error the outdated view happens to trip."""
    _seed_version(fake, "b1", 1, 1)
    doc, errors = budget_model.create_budget(
        _data(lines=[{"sous_phase": "ADM-00", "hours": 1, "frais_cents": 0}]),
        base_version=0)
    assert errors == [budget_model.BASE_VERSION_CONFLICT]


# ══════════════════════════════════════════════════════════════════════
# 3. Deux enregistrements parallèles
# ══════════════════════════════════════════════════════════════════════


def _rival_during_next_budget_commit(fake, **rival_kw) -> dict:
    """The REAL create_budget of another caller, committed at the start of
    the next commit that creates a budget — after this save's reads, before
    its writes apply."""
    holder: dict = {}

    def _hook(info) -> None:
        if not any(kind == "create" and path.startswith("budgets/")
                   for kind, path in info.ops):
            return
        remove()
        holder["rival"] = budget_model.create_budget(_data(note="Rival"), **rival_kw)

    remove = fake.add_commit_hook(_hook)
    return holder


def test_two_saves_on_one_base_never_share_a_version_number(fake):
    """Régression — sans base, les deux lisaient le même historique et
    frappaient deux « v1 ». Le second relit le compteur à sa reprise et
    prend le numéro suivant."""
    holder = _rival_during_next_budget_commit(fake)

    doc, errors = budget_model.create_budget(_data())

    rival, rival_errors = holder["rival"]
    assert rival_errors == [] and errors == []
    assert {rival["version"], doc["version"]} == {1, 2}
    assert _versions(fake) == [1, 2]
    assert fake.peek(COUNTER)["seq"] == 2


def test_the_loser_of_a_race_on_one_base_is_refused(fake):
    holder = _rival_during_next_budget_commit(fake, base_version=0)

    doc, errors = budget_model.create_budget(_data(), base_version=0)

    rival, rival_errors = holder["rival"]
    assert rival_errors == [] and rival["version"] == 1
    assert (doc, errors) == (None, [budget_model.BASE_VERSION_CONFLICT])
    assert _versions(fake) == [1]
    assert fake.peek("budgets/" + rival["id"])["note"] == "Rival"


# ══════════════════════════════════════════════════════════════════════
# 4. Ce que le modèle prend de l'appelant — et la provenance
# ══════════════════════════════════════════════════════════════════════


def test_only_the_creation_keys_are_taken_from_the_caller(fake):
    doc, errors = budget_model.create_budget(_data(
        id="forgé", version=99, created_via="mcp", etag="forgé",
        secret="x"))
    assert errors == [], errors
    stored = fake.peek(f"budgets/{doc['id']}")
    assert doc["id"] != "forgé" and stored["version"] == 1
    assert stored["created_via"] == "script"   # the path actually writing
    assert "secret" not in stored and stored["etag"] != "forgé"


def test_a_connector_version_is_stamped_and_noted(fake):
    with provenance.writing_via("mcp", tool="create_budget_version"):
        doc, errors = budget_model.create_budget(_data(), base_version=0)
        assert errors == []
        noted = provenance.committed_writes()
    stored = fake.peek(f"budgets/{doc['id']}")
    assert stored["created_via"] == "mcp" and stored["updated_via"] == "mcp"
    assert noted == (("budgets", doc["id"]),)


def test_a_counter_read_failure_refuses_and_writes_nothing(fake, monkeypatch):
    real = fake._fake_server.batch_get_documents
    refused = []

    def _batch_get(request, metadata=None, **kw):
        if any("counters/" in d for d in request["documents"]):
            refused.append(list(request["documents"]))
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(fake._fake_server, "batch_get_documents", _batch_get)
    doc, errors = budget_model.create_budget(_data(), base_version=0)
    assert (doc, errors) == (None, [budget_model.VERSION_READ_ERROR])
    assert refused                          # the COUNTER read is what failed
    assert fake.peek_collection("budgets") == {}


# ══════════════════════════════════════════════════════════════════════
# 5. Le formulaire web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (budgets_routes.budgets_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _form(**over) -> dict:
    form = {
        "dossier_id": "dos1", "hourly_rate": "275,00", "note": "Hypothèse A",
        "lines_json": json.dumps(
            [{"sous_phase": "PRE-01", "hours": 7.5, "frais": "120,00"}]),
    }
    form.update(over)
    return form


def _seed_payload(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="budget-initial">(.*?)</script>',
        html, re.S)
    assert match, "seed block missing"
    return json.loads(match.group(1))


def test_the_form_carries_the_version_it_is_built_on(fake, client):
    html = client.get("/budgets/nouveau?dossier_id=dos1").get_data(as_text=True)
    assert _BASE_INPUT.findall(html) == ["0"]
    _seed_version(fake, "b1", 1, 1)
    _seed_version(fake, "b2", 2, 2)
    html = client.get("/budgets/nouveau?dossier_id=dos1").get_data(as_text=True)
    assert _BASE_INPUT.findall(html) == ["2"]


def test_a_stale_form_is_refused_at_200_with_its_figures_kept(fake, client, monkeypatch):
    """Régression — le formulaire d'avant enregistrait une nouvelle version
    par-dessus celle qu'un autre onglet venait d'enregistrer, sans un mot."""
    _seed_version(fake, "b1", 1, 1)
    _seed_version(fake, "b2", 2, 2)
    seen = []
    monkeypatch.setattr(budgets_routes, "log_dossier_event",
                        lambda event, did, **kw: seen.append((event, did, kw)))

    resp = client.post("/budgets/", data=_form(base_version="1"))

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Cet élément a été modifié entre-temps." in html
    assert "La version en vigueur est la v2" in html
    assert _BASE_INPUT.findall(html) == ["2"]           # the next save is deliberate
    seed = _seed_payload(html)
    assert seed["rate_display"] == "275,00"
    pre = next(g for g in seed["groups"] if g["phase"] == "PRE")
    line = next(r for r in pre["lines"] if r["sous_phase"] == "PRE-01")
    assert line["hours"] == 7.5 and line["frais"] == "120,00"
    assert "Hypothèse A" in html
    assert _versions(fake) == [1, 2]
    assert seen == [("budget_version_conflict", "dos1",
                     {"base_version": 1, "current_version": 2})]


def test_the_resubmitted_form_saves_the_next_version(fake, client):
    _seed_version(fake, "b1", 1, 1)
    resp = client.post("/budgets/", data=_form(base_version="1"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    assert _versions(fake) == [1, 2]


@pytest.mark.parametrize("field", [None, "", "   "], ids=["absent", "vide", "blanc"])
def test_a_page_older_than_the_field_skips_the_check(fake, client, field):
    _seed_version(fake, "b1", 1, 1)
    form = _form() if field is None else _form(base_version=field)
    resp = client.post("/budgets/", data=form)
    assert resp.status_code == 302
    assert _versions(fake) == [1, 2]


@pytest.mark.parametrize("bad", ["abc", "-1", "1.0", "٣", "9999999"])
def test_a_malformed_base_is_a_french_400(fake, client, bad):
    resp = client.post("/budgets/", data=_form(base_version=bad))
    assert resp.status_code == 400
    assert resp.get_data(as_text=True) == budgets_routes.BASE_VERSION_MALFORMED
    assert fake.peek_collection("budgets") == {}


def test_a_validation_error_carries_the_submitted_base_back(fake, client):
    _seed_version(fake, "b1", 1, 1)
    resp = client.post("/budgets/", data=_form(
        base_version="1", lines_json="[]"))
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    assert "au moins une ligne" in html
    assert _BASE_INPUT.findall(html) == ["1"]
    # A page that carried no base re-renders without one.
    resp = client.post("/budgets/", data=_form(lines_json="[]"))
    assert _BASE_INPUT.findall(resp.get_data(as_text=True)) == []


def test_the_history_marks_a_version_the_connector_created(fake, client):
    _seed_version(fake, "b1", 1, 1, created_via="web")
    _seed_version(fake, "b2", 2, 2, created_via="mcp")
    html = client.get("/budgets/historique?dossier_id=dos1").get_data(as_text=True)
    assert html.count("créée par Claude") == 1
