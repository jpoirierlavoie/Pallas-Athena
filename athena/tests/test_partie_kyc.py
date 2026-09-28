"""Sémantique des dates KYC (PA-D07).

``identity_verified_date`` / ``conflict_check_date`` répondent à « quand la
décision a-t-elle été prise », jamais à « quand le champ a-t-il bougé ». Le
défaut d'origine : l'estampille se posait sur TOUT changement de statut, dans
les deux sens — une rétrogradation vers « non_vérifié » laissait un statut
non vérifié à côté d'un horodatage frais, l'incohérence exacte relevée par
l'audit MCP.

Invariant épinglé ici : date présente ⇔ statut décidé. L'estampille ne se pose
que sur une transition VERS un statut décidé (vérifié / exempté, et pour le
contrôle des conflits vérifié / conflit_détecté — le contrôle A eu lieu) ; un
statut soumis « non_vérifié » force la date à None, ce qui auto-répare les
documents antérieurs au correctif à leur prochaine édition KYC. Le tout est
conditionné à la PRÉSENCE de la clé : une mise à jour partielle qui ne porte
pas le champ ne touche jamais la date (sur un set plein-document, injecter un
défaut EST une suppression).

Tests purs : modèle importé, Firestore bouchonné (motif de
test_partie_naissance.py).

Lot 4a (D7) — la PROVENANCE. Une transition de statut nomme désormais qui
décide (``kyc_source`` : « juriste » | « mcp », SANS défaut) : les tests
d'origine passent donc « juriste », la source du formulaire web — changement
délibéré (une transition sans source est refusée). La seconde moitié du
fichier épingle les règles de la provenance.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from models import partie as pm  # noqa: E402

ANCIEN = datetime(2026, 6, 19, 14, 51, tzinfo=timezone.utc)


def _partie(**over) -> dict:
    base = pm._default_doc()
    base.update({
        "id": "p1", "type": "individual", "contact_role": "client",
        "first_name": "Marco", "last_name": "Andreoli",
    })
    base.update(over)
    return base


def _maj(monkeypatch, stocke: dict, data: dict, source: str = "juriste") -> dict:
    """Run update_partie against a stubbed store; return the written doc.

    Lot 4a: the lawyer's source by default — the web form's (changed
    deliberately: a status transition without a source is now refused)."""
    monkeypatch.setattr(pm, "get_partie", lambda pid: dict(stocke))
    ecrit: dict = {}

    class _Doc:
        def set(self, payload):
            ecrit.update(payload)

    class _Col:
        def document(self, _pid):
            return _Doc()

    monkeypatch.setattr(pm, "db", mock.Mock(collection=lambda _n: _Col()))
    _, erreurs = pm.update_partie("p1", data, kyc_source=source)
    assert erreurs == []
    return ecrit


def test_transition_vers_verifie_estampille(monkeypatch):
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="non_vérifié", identity_verified_date=None),
        {"identity_verified": "vérifié"},
    )
    assert ecrit["identity_verified_date"] is not None
    assert ecrit["identity_verified_date"] != ANCIEN


def test_transition_vers_exempte_estampille(monkeypatch):
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="non_vérifié"),
        {"identity_verified": "exempté"},
    )
    assert ecrit["identity_verified_date"] is not None


def test_retrogradation_efface_la_date(monkeypatch):
    """LE bogue PA-D07 : vérifié → non_vérifié RE-estampillait, produisant
    « non_vérifié » à côté d'un horodatage frais."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="vérifié", identity_verified_date=ANCIEN),
        {"identity_verified": "non_vérifié"},
    )
    assert ecrit["identity_verified"] == "non_vérifié"
    assert ecrit["identity_verified_date"] is None


def test_meme_statut_ne_reestampille_pas(monkeypatch):
    """Re-sauvegarder un contact déjà vérifié garde la date d'origine —
    « quand a-t-il été vérifié », pas « quand a-t-on sauvegardé »."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="vérifié", identity_verified_date=ANCIEN),
        {"identity_verified": "vérifié", "email": "a@b.ca"},
    )
    assert ecrit["identity_verified_date"] == ANCIEN


def test_conflit_detecte_est_une_decision(monkeypatch):
    """Un contrôle qui DÉTECTE un conflit a bien eu lieu : sa date est la
    date du contrôle."""
    ecrit = _maj(
        monkeypatch,
        _partie(conflict_check="non_vérifié", conflict_check_date=None),
        {"conflict_check": "conflit_détecté"},
    )
    assert ecrit["conflict_check_date"] is not None


def test_retrogradation_du_controle_efface_aussi(monkeypatch):
    ecrit = _maj(
        monkeypatch,
        _partie(conflict_check="vérifié", conflict_check_date=ANCIEN),
        {"conflict_check": "non_vérifié"},
    )
    assert ecrit["conflict_check_date"] is None


def test_mise_a_jour_partielle_ne_touche_pas_les_dates(monkeypatch):
    """Une clé absente ne touche rien — le portail L3 et les PUT CardDAV
    passent par ici sans jamais porter les champs KYC."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="vérifié", identity_verified_date=ANCIEN,
                conflict_check="vérifié", conflict_check_date=ANCIEN),
        {"email": "nouveau@exemple.com"},
    )
    assert ecrit["identity_verified_date"] == ANCIEN
    assert ecrit["conflict_check_date"] == ANCIEN


def test_document_incoherent_pre_correctif_s_autorepare(monkeypatch):
    """Un document d'avant le correctif (non_vérifié + date) se répare à la
    prochaine édition KYC qui re-soumet non_vérifié — l'invariant
    date-présente ⇔ statut-décidé redevient vrai sans migration."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="non_vérifié", identity_verified_date=ANCIEN),
        {"identity_verified": "non_vérifié"},
    )
    assert ecrit["identity_verified_date"] is None


# ── update_kyc_status ne vide plus les notes de conformité (lot 0b) ──────
#
# Le défaut : ``notes: str = ""`` par défaut, toujours écrit. Comme
# update_partie fusionne {**existing, **data} puis fait un set() du document
# entier, un changement de statut SANS notes effaçait les notes du juriste.
# Ces tests passent par le VRAI client Firestore (seul le serveur est faux) :
# c'est le document STOCKÉ qui est relu, jamais un dict remis à un bouchon.

import pytest  # noqa: E402

from tests._fake_firestore import install  # noqa: E402


@pytest.fixture
def magasin(monkeypatch):
    fake = install(monkeypatch, pm)
    fake.seed("parties/p1", _partie(
        identity_verified="non_vérifié",
        identity_verified_notes="Pièce vue le 3 mars — permis de conduire.",
        conflict_check="non_vérifié",
        conflict_check_notes="Recherche au registre : aucun homonyme.",
        etag="e0",
    ))
    return fake


def test_un_statut_sans_notes_garde_les_notes(magasin):
    """LE défaut : sur l'ancien code, ce changement de statut vidait les
    notes (default ``""`` toujours écrit)."""
    doc, erreurs = pm.update_kyc_status(
        "p1", "identity_verified", "vérifié", source="juriste")
    assert erreurs == []
    stocke = magasin.peek("parties/p1")
    assert stocke["identity_verified"] == "vérifié"
    assert stocke["identity_verified_notes"] == (
        "Pièce vue le 3 mars — permis de conduire."
    )
    # L'autre bloc n'est pas touché non plus.
    assert stocke["conflict_check_notes"] == (
        "Recherche au registre : aucun homonyme."
    )
    assert doc["identity_verified_notes"] == stocke["identity_verified_notes"]


def test_des_notes_fournies_remplacent(magasin):
    pm.update_kyc_status(
        "p1", "conflict_check", "vérifié", notes="Aucun conflit (3 dossiers).",
        source="juriste",
    )
    assert magasin.peek("parties/p1")["conflict_check_notes"] == (
        "Aucun conflit (3 dossiers)."
    )


def test_une_chaine_vide_efface_explicitement(magasin):
    """Effacer reste possible — mais seulement en le DEMANDANT."""
    pm.update_kyc_status("p1", "identity_verified", "vérifié", notes="",
                         source="juriste")
    assert magasin.peek("parties/p1")["identity_verified_notes"] == ""


def test_les_notes_fournies_sont_assainies(magasin):
    pm.update_kyc_status(
        "p1", "identity_verified", "vérifié", notes="<b>vu</b> " + "x" * 3000,
        source="juriste",
    )
    notes = magasin.peek("parties/p1")["identity_verified_notes"]
    assert "<b>" not in notes and len(notes) <= 2000


# ══════════════════════════════════════════════════════════════════════
# Lot 4a (D7) — la provenance d'une vérification de conformité
# ══════════════════════════════════════════════════════════════════════
#
# Une inscription de Claude (source « mcp ») reste PRÉSUMÉE jusqu'au
# « Confirmer » du juriste ; une transition sans source est REFUSÉE (jamais
# de défaut qui nommerait quelqu'un) ; seule une TRANSITION déplace la
# provenance ; le modèle seul pose date, source et confirmation.

import inspect  # noqa: E402

from models import concurrency, provenance  # noqa: E402
from utils import kyc  # noqa: E402


def _stored(fake):
    return fake.peek("parties/p1")


def test_a_status_transition_without_a_source_is_refused(magasin):
    """Fail closed: an omitted source must never stamp a Claude write as
    the lawyer's attestation — there is no default to fall back on."""
    before = _stored(magasin)
    doc, erreurs = pm.update_partie("p1", {"identity_verified": "vérifié"})
    assert doc is None and erreurs == [kyc.PROVENANCE_REQUIRED]
    assert _stored(magasin) == before


def test_an_unchanged_status_needs_no_source(magasin):
    """The partial callers (Réception, CardDAV) and a form re-save carry
    no transition: they are never refused for the missing source."""
    _doc, erreurs = pm.update_partie(
        "p1", {"identity_verified": "non_vérifié", "email": "a@b.ca"})
    assert erreurs == []


def test_a_claude_inscription_is_presumed(magasin):
    _doc, erreurs = pm.update_kyc_status(
        "p1", "identity_verified", "vérifié", source="mcp")
    assert erreurs == []
    stored = _stored(magasin)
    assert stored["identity_verified_source"] == "mcp"
    assert stored["identity_verified_confirmed_at"] is None
    assert stored["identity_verified_date"] is not None
    assert kyc.is_presumed(stored, "identity_verified")
    assert not kyc.is_decided(stored, "identity_verified")


def test_a_web_resave_never_confirms_a_presumed_inscription(magasin):
    """LE piège de D7 : le formulaire re-soumet TOUJOURS le statut. Si un
    re-enregistrement valait décision, modifier un courriel confirmerait en
    silence l'inscription de Claude."""
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="mcp")
    inscribed = _stored(magasin)["identity_verified_date"]

    _doc, erreurs = pm.update_partie(
        "p1", {"identity_verified": "vérifié", "email": "a@b.ca"},
        kyc_source="juriste")

    assert erreurs == []
    stored = _stored(magasin)
    assert kyc.is_presumed(stored, "identity_verified")
    assert stored["identity_verified_date"] == inscribed


def test_a_web_status_change_is_the_lawyer_s_decision(magasin):
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="mcp")

    _doc, erreurs = pm.update_partie(
        "p1", {"identity_verified": "exempté"}, kyc_source="juriste")

    assert erreurs == []
    stored = _stored(magasin)
    assert stored["identity_verified_source"] == "juriste"
    assert kyc.is_decided(stored, "identity_verified")


def test_non_verifie_clears_date_source_and_confirmation(magasin):
    pm.update_kyc_status("p1", "conflict_check", "vérifié", source="mcp")
    etag = _stored(magasin)["etag"]
    pm.confirm_kyc_status("p1", "conflict_check", par="juriste",
                          expected_etag=etag)

    _doc, erreurs = pm.update_partie(
        "p1", {"conflict_check": "non_vérifié"}, kyc_source="juriste")

    assert erreurs == []
    stored = _stored(magasin)
    assert stored["conflict_check_date"] is None
    assert stored["conflict_check_source"] == ""
    assert stored["conflict_check_confirmed_at"] is None
    assert stored["conflict_check_confirmed_by"] == ""


def test_claude_can_never_change_the_lawyer_s_attestation(magasin):
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="juriste")
    before = _stored(magasin)

    for status, notes in (("exempté", None), ("non_vérifié", None),
                          ("vérifié", "Ajout de Claude.")):
        doc, erreurs = pm.update_kyc_status(
            "p1", "identity_verified", status, notes=notes, source="mcp")
        assert doc is None and erreurs == [kyc.LAWYER_ATTESTATION], status
    # …through update_partie too (the rule lives in the model).
    doc, erreurs = pm.update_partie(
        "p1", {"identity_verified": "exempté"}, kyc_source="mcp")
    assert doc is None and erreurs == [kyc.LAWYER_ATTESTATION]
    assert _stored(magasin) == before


def test_a_confirmed_inscription_is_the_lawyer_s_too(magasin):
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="mcp")
    pm.confirm_kyc_status("p1", "identity_verified", par="juriste",
                          expected_etag=_stored(magasin)["etag"])
    doc, erreurs = pm.update_kyc_status(
        "p1", "identity_verified", "exempté", source="mcp")
    assert doc is None and erreurs == [kyc.LAWYER_ATTESTATION]


def test_claude_may_correct_its_own_presumed_inscription(magasin):
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="mcp")
    _doc, erreurs = pm.update_kyc_status(
        "p1", "identity_verified", "exempté", source="mcp")
    assert erreurs == []
    assert kyc.is_presumed(_stored(magasin), "identity_verified")


def test_a_payload_can_never_forge_the_provenance(magasin):
    """_normalize drops every provenance key a caller sends — the web form,
    a CardDAV PUT, the connector alike."""
    _doc, erreurs = pm.update_partie("p1", {
        "identity_verified": "vérifié",
        "identity_verified_source": "juriste",
        "identity_verified_confirmed_at": ANCIEN,
        "identity_verified_confirmed_by": "juriste",
        "identity_verified_date": ANCIEN,
    }, kyc_source="mcp")
    assert erreurs == []
    stored = _stored(magasin)
    assert stored["identity_verified_source"] == "mcp"
    assert stored["identity_verified_confirmed_at"] is None
    assert stored["identity_verified_date"] != ANCIEN
    assert kyc.is_presumed(stored, "identity_verified")


def test_update_kyc_status_has_no_default_source():
    param = inspect.signature(pm.update_kyc_status).parameters["source"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(ValueError):
        pm.update_kyc_status("p1", "identity_verified", "vérifié",
                             source="claude")


def test_create_with_a_decided_status_stamps_the_date_and_the_source(
    monkeypatch,
):
    """LE défaut d'origine : un contact CRÉÉ avec un statut décidé n'avait
    pas de date — l'invariant date ⇔ décidé était faux dès la naissance."""
    fake = install(monkeypatch, pm)
    doc, erreurs = pm.create_partie({
        "type": "individual", "contact_role": "client", "last_name": "Roy",
        "identity_verified": "vérifié", "conflict_check": "non_vérifié",
    }, kyc_source="juriste")
    assert erreurs == []
    stored = fake.peek(f"parties/{doc['id']}")
    assert stored["identity_verified_date"] is not None
    assert stored["identity_verified_source"] == "juriste"
    assert stored["conflict_check_date"] is None

    doc, erreurs = pm.create_partie({
        "type": "individual", "contact_role": "client", "last_name": "Roy",
        "identity_verified": "vérifié",
    })
    assert doc is None and erreurs == [kyc.PROVENANCE_REQUIRED]


def test_a_carddav_put_keeps_a_presumed_inscription(magasin):
    """CardDAV never carries a KYC key: a phone edit merges onto the stored
    record and the presumed provenance survives."""
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="mcp")
    data = pm.vcard_to_partie(pm.partie_to_vcard(_stored(magasin)))
    assert not set(data) & ({"identity_verified", "conflict_check"}
                            | set(kyc.PROVENANCE_KEYS))

    _doc, erreurs = pm.update_partie("p1", data)

    assert erreurs == []
    assert kyc.is_presumed(_stored(magasin), "identity_verified")


# ── confirm_kyc_status ────────────────────────────────────────────────


_CONFIRM_KEYS = {"identity_verified_confirmed_at",
                 "identity_verified_confirmed_by",
                 "updated_at", "etag", "updated_via"}


def _presumed(fake):
    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="mcp")
    fake.reset_logs()
    return _stored(fake)["etag"]


def test_confirm_writes_only_its_keys_in_a_transaction(magasin):
    etag = _presumed(magasin)
    before = _stored(magasin)

    doc, erreurs = pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste", expected_etag=etag)

    assert erreurs == []
    after = _stored(magasin)
    moved = {k for k in set(before) | set(after)
             if before.get(k) != after.get(k)}
    # A partial update: nothing outside the confirmation and its stamp
    # moves (``updated_via`` may keep its value — the same writer path).
    assert moved <= _CONFIRM_KEYS
    assert {"identity_verified_confirmed_at", "identity_verified_confirmed_by",
            "etag", "updated_at"} <= moved
    assert [c.ops for c in magasin.commits] == [(("update", "parties/p1"),)]
    assert all(c.transaction is not None for c in magasin.commits)
    assert after["identity_verified_confirmed_by"] == "juriste"
    assert kyc.is_decided(after, "identity_verified")
    assert doc["etag"] == after["etag"]


def test_confirm_refuses_what_is_not_presumed(magasin):
    etag = _stored(magasin)["etag"]
    doc, erreurs = pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste", expected_etag=etag)
    assert doc is None and erreurs == [pm.KYC_NOTHING_TO_CONFIRM]

    pm.update_kyc_status("p1", "identity_verified", "vérifié", source="juriste")
    etag = _stored(magasin)["etag"]
    doc, erreurs = pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste", expected_etag=etag)
    assert erreurs == [pm.KYC_NOTHING_TO_CONFIRM]


def test_confirm_refuses_a_stale_or_missing_version(magasin):
    _presumed(magasin)
    before = _stored(magasin)
    assert pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste", expected_etag="perimee",
    )[1] == [concurrency.STALE_ETAG_ERROR]
    assert pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste",
    )[1] == [pm.KYC_CONFIRM_NEEDS_VERSION]
    assert _stored(magasin) == before and magasin.commits == []


def test_an_inscription_landing_after_the_page_is_never_confirmed_unseen(
    magasin,
):
    """Claude re-inscribes between the render and the click: the page's
    etag is stale, so the confirmation is refused."""
    etag = _presumed(magasin)
    pm.update_kyc_status("p1", "identity_verified", "exempté", source="mcp")

    doc, erreurs = pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste", expected_etag=etag)

    assert doc is None and erreurs == [concurrency.STALE_ETAG_ERROR]
    assert kyc.is_presumed(_stored(magasin), "identity_verified")


def test_the_connector_can_never_confirm(magasin):
    etag = _presumed(magasin)
    with provenance.writing_via("mcp", tool="record_kyc_status"):
        doc, erreurs = pm.confirm_kyc_status(
            "p1", "identity_verified", par="juriste", expected_etag=etag)
    assert doc is None and erreurs == [pm.KYC_CONFIRM_APP_ONLY]
    assert kyc.is_presumed(_stored(magasin), "identity_verified")


def test_confirm_works_on_a_contact_with_an_invalid_legacy_field(magasin):
    """A partial update, never the merged set(): a malformed legacy phone
    (which _validate would refuse) cannot block a compliance confirmation."""
    _presumed(magasin)
    magasin.external_write("parties/p1", {**_stored(magasin),
                                          "phone_cell": "pas un numéro"})
    etag = _stored(magasin)["etag"]

    _doc, erreurs = pm.confirm_kyc_status(
        "p1", "identity_verified", par="juriste", expected_etag=etag)

    assert erreurs == []
    assert _stored(magasin)["phone_cell"] == "pas un numéro"


# ── Une écriture de Claude ne passe jamais PAR-DESSUS le juriste ────────
#
# La règle « jamais sur l'attestation du juriste » se décide sur une
# LECTURE. Sans comparaison-et-écriture contre cette lecture, le set()
# aveugle du chemin hérité écrasait une décision du juriste arrivée entre
# la lecture et l'écriture — en silence (revue du lot 4a, étape 2).


def test_a_lawyer_decision_landing_before_claude_s_commit_is_never_reverted(
    magasin,
):
    """The lawyer decides « exempté » between update_partie's read and its
    commit. On f5033ec the blind set() wrote Claude's presumed « vérifié »
    over it; now the write is compare-and-set against its read and refused."""
    rival = {**_stored(magasin), "identity_verified": "exempté",
             "identity_verified_source": "juriste",
             "identity_verified_date": ANCIEN, "etag": "e-juriste"}

    def _hook(info) -> None:
        if any(path == "parties/p1" for _op, path in info.ops):
            remove()
            magasin.external_write("parties/p1", rival)

    remove = magasin.add_commit_hook(_hook)

    doc, erreurs = pm.update_partie(
        "p1", {"identity_verified": "vérifié"}, kyc_source="mcp")

    assert doc is None and erreurs == [concurrency.STALE_ETAG_ERROR]
    stored = _stored(magasin)
    assert stored["identity_verified"] == "exempté"
    assert stored["etag"] == "e-juriste"
    assert kyc.is_decided(stored, "identity_verified")


def test_claude_s_notes_never_land_on_a_confirmation_made_in_between(
    magasin, monkeypatch,
):
    """A NOTES-only write carries no transition, so update_partie's own rule
    lets it through — the refusal lives in update_kyc_status's read. The
    lawyer confirms right after that read: on f5033ec Claude's notes were
    then written onto the confirmed check (« notes included » broken)."""
    _presumed(magasin)
    notes_before = _stored(magasin)["identity_verified_notes"]
    real_get = pm.get_partie
    calls = {"n": 0}

    def racing_get(pid):
        doc = real_get(pid)
        calls["n"] += 1
        if calls["n"] == 1:  # right after update_kyc_status's own read
            magasin.external_write("parties/p1", {
                **_stored(magasin),
                "identity_verified_confirmed_at": ANCIEN,
                "identity_verified_confirmed_by": "juriste",
                "etag": "e-confirme"})
        return doc

    monkeypatch.setattr(pm, "get_partie", racing_get)

    doc, erreurs = pm.update_kyc_status(
        "p1", "identity_verified", "vérifié", notes="Ajout de Claude.",
        source="mcp")

    assert doc is None and erreurs == [concurrency.STALE_ETAG_ERROR]
    stored = _stored(magasin)
    assert stored["identity_verified_notes"] == notes_before
    assert kyc.is_decided(stored, "identity_verified")
