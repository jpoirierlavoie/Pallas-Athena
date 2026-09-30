"""scripts/arborescence_par_defaut.py — the backfill of the default tree.

Over the shared fake Firestore (the REAL client, an in-memory server) and
the REAL engine (models/folder.py): what is asserted is what is STORED and
what is PRINTED. The script must write nothing by default, write the tree
with ``--apply`` (the old root « Projets » moved under « Interne », its
documents untouched), write nothing the second time, list the out-of-place
homonyms by folder NAME only, never print a title or a party, and exit 1
on any error or blocked folder.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.folder as folder
    import scripts.arborescence_par_defaut as script

from tests._fake_firestore import install  # noqa: E402

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
NID = folder.node_folder_id
SID = folder.system_folder_id

TITRE = "Tremblay c. Lavoie"
CLIENT = "Jean Tremblay"


@pytest.fixture
def store(monkeypatch):
    fake = install(monkeypatch, folder, script)
    fake.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "status": "actif",
        "title": TITRE, "clients": [{"id": "p1", "name": CLIENT}],
    })
    fake.seed("dossiers/d2", {
        "id": "d2", "file_number": "2026-002", "status": "fermé",
        "title": "Gagnon c. Roy",
    })
    return fake


def _seed_folder(store, fid, name, parent=None, *, dossier="d1", **extra):
    data = {
        "id": fid, "dossier_id": dossier, "name": name,
        "parent_folder_id": parent, "order": 0,
        "created_at": T0, "updated_at": T0, "etag": f"e-{fid}", **extra,
    }
    store.seed(f"folders/{fid}", data)
    return data


def _seed_legacy(store):
    """d1 as lot 2A left it: a ROOT « Projets » created by name (no role),
    a generated document in it, and a portal document at the dossier root
    (versed before « Reçus du portail » existed)."""
    _seed_folder(store, "leg", "Projets")
    docs = {
        "gen": {"id": "gen", "dossier_id": "d1", "folder_id": "leg",
                "display_name": "Projet de lettre", "category": "correspondance"},
        "recu": {"id": "recu", "dossier_id": "d1", "folder_id": None,
                 "display_name": "Pièce reçue", "tags": ["portail"]},
    }
    for doc_id, data in docs.items():
        store.seed(f"documents/{doc_id}", data)
    return docs


def _folders(store, dossier="d1") -> dict:
    return {k: v for k, v in store.peek_collection("folders").items()
            if v["dossier_id"] == dossier}


def _paths(store, dossier="d1") -> set:
    rows = _folders(store, dossier)

    def path(f):
        parent = rows.get(f.get("parent_folder_id") or "")
        return f"{path(parent)} / {f['name']}" if parent else f["name"]

    return {path(f) for f in rows.values()}


def _ops(store) -> list:
    return [op for c in store.commits for op in c.ops]


# ── La simulation ─────────────────────────────────────────────────────────


def test_la_simulation_est_le_defaut_et_n_ecrit_rien(store, capsys):
    _seed_legacy(store)
    avant = store.peek_collection("folders")

    assert script.main([]) == 0

    assert store.commits == []
    assert store.peek_collection("folders") == avant
    out = capsys.readouterr().out
    assert "SIMULATION" in out and "rien n'a été écrit" in out
    # Ce que --apply ferait : 17 dossiers à créer, l'ancien « Projets »
    # adopté (estampillé) et déplacé sous « Interne ».
    assert "2026-001 : à créer 17 · à adopter 1 · à déplacer 1 · bloqués 0" in out
    assert "2026-002 : à créer 18 · à adopter 0 · à déplacer 0 · bloqués 0" in out
    # --dry-run explicite : la même chose.
    capsys.readouterr()
    assert script.main(["--dry-run"]) == 0
    assert store.commits == []


def test_la_simulation_annonce_ce_que_l_ecriture_fait(store, capsys):
    _seed_legacy(store)
    script.main([])
    simulation = capsys.readouterr().out

    script.main(["--apply"])
    ecriture = capsys.readouterr().out

    def comptes(out, ref):
        line = next(l for l in out.splitlines() if l.strip().startswith(ref))
        return [int(tok) for tok in line.split() if tok.isdigit()]

    for ref in ("2026-001", "2026-002"):
        assert comptes(simulation, ref) == comptes(ecriture, ref)


# ── L'écriture ────────────────────────────────────────────────────────────


def test_l_ecriture_donne_l_arbre_et_deplace_l_ancien_projets(store, capsys):
    docs = _seed_legacy(store)

    assert script.main(["--apply"]) == 0

    assert "Interne / Projets" in _paths(store) and len(_folders(store)) == 18
    assert len(_folders(store, "d2")) == 18
    # L'ancien « Projets » de la racine est LE dossier système, déplacé sous
    # « Interne » — pas un second « Projets ».
    leg = store.peek("folders/leg")
    assert leg["system_role"] == "projets"
    assert leg["parent_folder_id"] == NID("d1", "interne")
    assert SID("d1", "projets") not in _folders(store)
    # Aucun document n'est touché : celui du dossier déplacé le suit par son
    # identifiant, celui versé à la racine y reste.
    for doc_id, data in docs.items():
        assert store.peek(f"documents/{doc_id}") == data
    assert not [p for _kind, p in _ops(store) if p.startswith("documents/")]
    out = capsys.readouterr().out
    assert "2026-001 : créés 17 · adoptés 1 · déplacés 1 · bloqués 0" in out
    assert "SIMULATION" not in out


def test_une_seconde_ecriture_n_ecrit_rien(store, capsys):
    _seed_legacy(store)
    assert script.main(["--apply"]) == 0
    store.reset_logs()
    capsys.readouterr()

    assert script.main(["--apply"]) == 0

    assert store.commits == []
    out = capsys.readouterr().out
    assert "2026-001 : créés 0 · adoptés 0 · déplacés 0 · bloqués 0 (à jour)" in out
    assert "2026-002 : créés 0 · adoptés 0 · déplacés 0 · bloqués 0 (à jour)" in out


# ── La sélection ──────────────────────────────────────────────────────────


def test_dossier_ne_traite_que_ce_dossier(store, capsys):
    _seed_legacy(store)

    assert script.main(["--apply", "--dossier", "d2"]) == 0

    assert len(_folders(store, "d2")) == 18
    assert set(_folders(store)) == {"leg"}            # d1 intact
    out = capsys.readouterr().out
    assert "2026-002" in out and "2026-001" not in out


def test_un_dossier_introuvable_est_une_erreur(store, capsys):
    assert script.main(["--apply", "--dossier", "nope"]) == 1
    assert store.commits == []
    assert "Dossier introuvable" in capsys.readouterr().out


def test_statuts_restreint_les_dossiers_traites(store, capsys):
    assert script.main(["--apply", "--statuts", "fermé"]) == 0

    assert _folders(store) == {} and len(_folders(store, "d2")) == 18
    out = capsys.readouterr().out
    assert "2026-002" in out and "2026-001" not in out


def test_par_defaut_tous_les_statuts_meme_absent(store, capsys):
    store.seed("dossiers/d3", {"id": "d3", "file_number": "2019-004"})
    assert script.main(["--apply"]) == 0
    assert len(_folders(store, "d3")) == 18


def test_un_statut_inconnu_refuse_sans_rien_lire_ni_ecrire(store, capsys):
    assert script.main(["--apply", "--statuts", "actif,clos"]) == 1
    assert store.commits == []
    out = capsys.readouterr().out
    assert "Statut inconnu : clos" in out and "actif, en_attente" in out


# ── Le compte rendu ───────────────────────────────────────────────────────


def test_les_homonymes_hors_place_sont_listes_par_leur_nom(store, capsys):
    """Un « Déboursés » que le juriste a fait à la RACINE n'est pas le
    dossier de l'application (celui-ci vit sous « Mandat ») : il est listé
    « à vérifier », par son nom seulement — jamais une consigne de fusion,
    et jamais déplacé ni estampillé."""
    _seed_folder(store, "mien", "Déboursés")
    _seed_folder(store, "client", CLIENT)          # un dossier ordinaire

    assert script.main(["--apply"]) == 0

    out = capsys.readouterr().out
    assert ("à vérifier : « Déboursés » (à la racine) porte le nom d'un "
            "dossier de l'arborescence sans en être un") in out
    assert "homonymes à vérifier : 1" in out
    assert "fusionner" not in out
    mien = store.peek("folders/mien")
    assert mien["parent_folder_id"] is None and not mien.get("system_role")
    assert "Mandat / Déboursés" in _paths(store)
    # Jamais un titre, jamais un nom de partie — même celui d'un dossier de
    # classement, qui n'est pas un nom de l'arborescence.
    assert TITRE not in out and CLIENT not in out


def test_un_dossier_bloque_est_dit_et_le_reste_est_ecrit(store, capsys):
    """Le juriste avait son propre « Interne / Projets » : l'ancien
    « Projets » système de la racine ne peut pas s'y déplacer (même nom à la
    destination). Il reste où il est, le reste de l'arbre est écrit, la
    raison est dite, et le code de sortie est 1."""
    _seed_folder(store, SID("d1", "projets"), "Projets", system_role="projets")
    _seed_folder(store, "int", "Interne")
    _seed_folder(store, "sien", "Projets", "int")

    assert script.main(["--apply"]) == 1

    out = capsys.readouterr().out
    assert ("[!] bloqué : « Projets » — ne peut être déplacé : un dossier du "
            "même nom existe déjà à la destination — laissé où il est") in out
    # Le « Projets » du juriste est DANS « Interne », désormais un dossier
    # de l'application : son sous-classement, jamais listé « à vérifier » —
    # la ligne « bloqué » dit déjà ce qui gêne. Le déplacement bloqué n'est
    # pas annoncé comme fait ; l'adoption d'« Interne » l'est.
    assert "à vérifier : « Projets »" not in out
    assert "« Projets » est rangé" not in out
    assert ("« Interne » existant est désormais le dossier de "
            "l'application") in out
    assert store.peek(f"folders/{SID('d1', 'projets')}")["parent_folder_id"] is None
    assert store.peek("folders/int")["system_role"] == "interne"
    assert "Mandat / Déboursés" in _paths(store)


def test_une_lecture_des_dossiers_en_echec_arrete_tout(store, capsys, monkeypatch):
    panne = mock.Mock()
    panne.collection.side_effect = RuntimeError("Firestore indisponible")
    monkeypatch.setattr(script, "db", panne)

    assert script.main(["--apply"]) == 1

    assert store.commits == []
    out = capsys.readouterr().out
    assert "Lecture des dossiers impossible" in out
    assert "rien n'a été écrit" in out
    assert "à jour" not in out and "Dossiers traités" not in out


def test_un_dossier_illisible_est_en_echec_et_les_autres_sont_traites(
    store, capsys, monkeypatch,
):
    lire = folder.list_dossier_folders

    def _lire(dossier_id):
        if dossier_id == "d1":
            raise RuntimeError("illisible")
        return lire(dossier_id)

    monkeypatch.setattr(folder, "list_dossier_folders", _lire)

    assert script.main(["--apply"]) == 1

    assert _folders(store) == {}                     # rien écrit sur d1
    assert len(_folders(store, "d2")) == 18
    out = capsys.readouterr().out
    assert f"[ECHEC] 2026-001 : {folder.READ_ERROR}" in out
    assert "Dossiers traités : 1 · en échec : 1" in out


def test_homonyms_est_pur_et_ignore_ce_que_le_plan_a_resolu():
    folders = [
        {"id": "a", "name": "pièces", "parent_folder_id": None},    # casse
        {"id": "b", "name": "Pièces", "parent_folder_id": "x"},  # NFD
        {"id": "c", "name": "Divers", "parent_folder_id": None},
        {"id": "d", "name": "Pièces", "parent_folder_id": "p"},
    ]
    report = folder.TreeReport(resolved={"pieces": {"id": "d"}})
    got = script.homonyms(folders, report)
    assert got == [
        {"name": "Pièces", "root": False, "parent": ""},
        {"name": "pièces", "root": True, "parent": ""},
    ]


# ── Revue : « (à jour) » vient des ÉCRITURES du rapport (fix 1) ───────────


def _ligne(out: str, ref: str) -> str:
    return next(l for l in out.splitlines() if l.strip().startswith(ref))


def test_un_dossier_ordinaire_repris_par_son_nom_se_lit_a_jour(store, capsys):
    """Le juriste avait sa propre « Correspondance » à la racine : l'arbre
    la REPREND par son nom (rien n'y est écrit, jamais). Une seconde
    exécution n'écrit rien et lit « (à jour) » — la reprise est comptée à
    part, « repris : 1 », sans rendre la ligne en attente."""
    _seed_folder(store, "sienne", "Correspondance")
    avant = store.peek("folders/sienne")

    assert script.main(["--apply"]) == 0
    premier = capsys.readouterr().out
    assert _ligne(premier, "2026-001").endswith(
        "créés 17 · adoptés 0 · déplacés 0 · bloqués 0 — repris : 1")
    assert "Correspondance / Courriels" in _paths(store)
    assert store.peek("folders/sienne") == avant       # jamais estampillée
    store.reset_logs()

    assert script.main(["--apply"]) == 0
    assert store.commits == []
    second = capsys.readouterr().out
    assert _ligne(second, "2026-001") == (
        "  2026-001 : créés 0 · adoptés 0 · déplacés 0 · bloqués 0 (à jour) "
        "— repris : 1")
    assert "· repris : 1 ·" in second                    # le total
    assert "Repris : dossiers ordinaires déjà présents" in second
    # La simulation dit la même chose.
    assert script.main([]) == 0
    assert "2026-001 : à créer 0 · à adopter 0 · à déplacer 0 · bloqués 0 " \
        "(à jour) — repris : 1" in capsys.readouterr().out


def test_a_jour_vient_des_ecritures_du_rapport_jamais_des_comptes(
    store, capsys, monkeypatch,
):
    """Un rapport qui a des ÉCRITURES n'est jamais « (à jour) », quels que
    soient ses comptes ; un rapport qui n'en a aucune l'est, même avec des
    dossiers repris."""
    avec_ecriture = folder.TreeReport(
        writes=[folder._Write("update", "mandat", "x", {"system_role": "mandat"})])
    sans_ecriture = folder.TreeReport(reused=["correspondance"])

    def _plan(dossier_id, folders, *, relocate):
        return avec_ecriture if dossier_id == "d1" else sans_ecriture

    monkeypatch.setattr(folder, "plan_default_tree_from", _plan)

    assert script.main([]) == 0

    out = capsys.readouterr().out
    assert "(à jour)" not in _ligne(out, "2026-001")
    assert _ligne(out, "2026-002").endswith("(à jour) — repris : 1")


def test_une_ecriture_que_le_rapport_ne_montre_pas_n_est_jamais_a_jour(
    store, capsys, monkeypatch,
):
    """La lecture faite juste avant annonçait 18 créations ; le moteur rend
    un rapport SANS écriture (son commit a levé et la relecture n'a plus
    rien trouvé à écrire, ou un autre appel l'a fait) : la ligne ne dit pas
    « (à jour) », elle le signale."""
    monkeypatch.setattr(
        folder, "ensure_default_tree",
        lambda dossier_id, *, relocate: (folder.TreeReport(), []))

    assert script.main(["--apply", "--dossier", "d2"]) == 0

    out = capsys.readouterr().out
    assert "(à jour)" not in _ligne(out, "2026-002")
    assert ("[?] la lecture faite juste avant annonçait des écritures que ce "
            "compte rendu ne montre pas") in out


# ── Revue : chaque adoption et chaque déplacement dits (fix 2) ────────────


def _seed_a_adopter(store):
    """d1 : l'ancien « Projets » et l'ancien « Reçus du portail » à la
    racine (par nom, sans rôle), et un « mandat » du juriste (casse
    différente) — trois dossiers que l'arbre ADOPTE, deux qu'il déplace."""
    _seed_folder(store, "leg", "Projets")
    _seed_folder(store, "recus", "Reçus du portail")
    _seed_folder(store, "sien", "mandat")


def test_la_simulation_dit_chaque_adoption_et_chaque_deplacement(store, capsys):
    _seed_a_adopter(store)

    assert script.main([]) == 0

    out = capsys.readouterr().out
    lignes = [l.strip() for l in out.splitlines()]
    verrou = "(ne se renommera plus, ne se déplacera plus)"
    assert f"« Mandat » existant sera le dossier de l'application {verrou}" in lignes
    assert f"« Projets » existant sera le dossier de l'application {verrou}" in lignes
    assert "« Projets » sera rangé sous « Interne »" in lignes
    assert (f"« Reçus du portail » existant sera le dossier de "
            f"l'application {verrou}") in lignes
    assert "« Reçus du portail » sera rangé sous « Autres »" in lignes
    # Les noms de l'ARBORESCENCE, jamais le nom stocké.
    assert "« mandat »" not in out
    assert store.commits == []


def test_l_ecriture_dit_chaque_adoption_et_chaque_deplacement_une_fois(
    store, capsys,
):
    _seed_a_adopter(store)

    assert script.main(["--apply"]) == 0

    out = capsys.readouterr().out
    lignes = [l.strip() for l in out.splitlines()]
    verrou = "(ne se renomme plus, ne se déplace plus)"
    assert (f"« Mandat » existant est désormais le dossier de "
            f"l'application {verrou}") in lignes
    assert "« Projets » est rangé sous « Interne »" in lignes
    assert "« Reçus du portail » est rangé sous « Autres »" in lignes
    assert not [l for l in lignes if " sera " in l]
    assert _ligne(out, "2026-001").endswith(
        "créés 15 · adoptés 3 · déplacés 2 · bloqués 0")
    # Ce qui est dit est ce qui est stocké.
    assert store.peek("folders/sien")["system_role"] == "mandat"
    assert store.peek("folders/recus")["parent_folder_id"] == NID("d1", "autres")
    # Une seconde écriture : plus rien à dire.
    capsys.readouterr()
    assert script.main(["--apply"]) == 0
    second = capsys.readouterr().out
    assert "existant est désormais" not in second and " est rangé " not in second


# ── Revue : les homonymes « à vérifier » (fix 3) ──────────────────────────


def test_un_dossier_dans_un_sous_arbre_systeme_n_est_jamais_liste(store, capsys):
    """Un « Courriels » que le juriste range sous « Reçus du portail » est
    son sous-classement du portail — et un « Pièces » sous son propre
    « Interne », que la simulation va ADOPTER, pareil : ni l'un ni l'autre
    n'est « à vérifier »."""
    _seed_folder(store, SID("d1", "portail"), "Reçus du portail",
                 system_role="portail")
    _seed_folder(store, "courriels", "Courriels", SID("d1", "portail"))
    _seed_folder(store, "int", "Interne")
    _seed_folder(store, "pieces", "Pièces", "int")

    assert script.main([]) == 0

    out = capsys.readouterr().out
    assert "à vérifier" not in out.replace("homonymes à vérifier : 0", "")
    assert "homonymes à vérifier : 0" in out


def test_un_homonyme_donne_le_nom_de_son_parent_quand_c_est_un_nom_de_l_arbre(
    store, capsys,
):
    """Un « Jugements » rangé dans la « Correspondance » du juriste : le
    parent est nommé (c'est un nom de l'arborescence). Un « Pièces » rangé
    dans un dossier au nom d'un client : le parent n'est JAMAIS nommé."""
    _seed_folder(store, "corr", "Correspondance")
    _seed_folder(store, "jug", "Jugements", "corr")
    _seed_folder(store, "client", CLIENT)
    _seed_folder(store, "pieces", "Pièces", "client")

    assert script.main(["--apply"]) == 0

    out = capsys.readouterr().out
    assert ("à vérifier : « Jugements » (dans « Correspondance ») porte le "
            "nom d'un dossier de l'arborescence sans en être un") in out
    assert ("à vérifier : « Pièces » (dans un sous-dossier) porte le nom "
            "d'un dossier de l'arborescence sans en être un") in out
    assert "homonymes à vérifier : 2" in out
    assert CLIENT not in out and "fusionner" not in out


# ── Revue : aucun dossier lu (fix 4) ──────────────────────────────────────


@pytest.fixture
def vide(monkeypatch):
    """Un projet sans AUCUN dossier — ce que lit un mauvais projet."""
    return install(monkeypatch, folder, script)


def test_aucun_dossier_lu_sans_filtre_est_un_echec(vide, capsys):
    for argv in ([], ["--apply"]):
        assert script.main(argv) == 1
        out = capsys.readouterr().out
        assert ("[ECHEC] Aucun dossier lu — vérifiez le projet visé "
                "(GOOGLE_CLOUD_PROJECT)") in out
        assert "Projet Firestore : fake-project" in out
        assert "Dossiers traités" not in out
    assert vide.commits == []


def test_un_filtre_qui_ne_retient_rien_n_est_pas_un_echec(vide, capsys):
    assert script.main(["--statuts", "actif"]) == 0
    out = capsys.readouterr().out
    assert "[!] Aucun dossier lu — vérifiez le projet visé" in out
    assert "Aucun dossier ne correspond à la sélection." in out


def test_un_statut_sans_dossier_n_est_pas_un_echec(store, capsys):
    assert script.main(["--apply", "--statuts", "archivé"]) == 0
    out = capsys.readouterr().out
    assert "Aucun dossier ne correspond à la sélection." in out
    assert "Aucun dossier lu" not in out
    assert store.commits == []


# ── Revue : ce que le script ne journalise pas, ce qu'il recrée (fix 5) ────


def test_la_docstring_dit_le_journal_la_recreation_et_le_dotenv():
    doc = " ".join(script.__doc__.split())
    assert "do NOT reach" in doc and "Cloud Logging" in doc
    assert "the ONLY record" in doc
    assert "RECREATED at the same" in doc and "--dossier" in doc
    assert "find_dotenv" in doc and "never overrides one given inline" in doc
    assert "GOOGLE_CLOUD_PROJECT" in doc


def test_chaque_execution_se_termine_par_les_deux_notes(store, capsys):
    for argv in ([], ["--apply"]):
        assert script.main(argv) == 0
        out = capsys.readouterr().out
        assert ("Note : ce compte rendu est la seule trace de cette exécution "
                "— les événements journalisés par un script n'atteignent pas "
                "Cloud Logging. Conservez-le.") in out
        assert ("Note : relancer --apply RECRÉE tout dossier de l'arborescence "
                "que le juriste a supprimé depuis") in out
        assert "--dossier ID" in out


def test_la_recreation_annoncee_est_celle_du_moteur(store, capsys):
    """Ce que la note annonce : un dossier ordinaire supprimé par le juriste
    revient, au même identifiant, à la prochaine écriture."""
    assert script.main(["--apply", "--dossier", "d1"]) == 0
    fid = NID("d1", "transcriptions")
    store.collection("folders").document(fid).delete()   # le juriste le supprime
    assert store.peek(f"folders/{fid}") is None
    capsys.readouterr()

    assert script.main(["--apply", "--dossier", "d1"]) == 0

    assert store.peek(f"folders/{fid}")["name"] == "Transcriptions"
    assert "2026-001 : créés 1 · adoptés 0" in capsys.readouterr().out


def test_le_fichier_env_lu_est_dit(store, capsys, monkeypatch):
    monkeypatch.setattr(script, "_ENV_PATH", "C:/cabinet/.env")
    assert script.main([]) == 0
    assert "Fichier .env lu : C:/cabinet/.env" in capsys.readouterr().out


def test_load_env_rend_le_fichier_lu_et_ne_remplace_rien_en_ligne(
    tmp_path, monkeypatch,
):
    (tmp_path / ".env").write_text(
        "ARBO_TEST_ABSENTE=du_fichier\nARBO_TEST_EN_LIGNE=du_fichier\n",
        encoding="utf-8")
    # setenv puis delenv : monkeypatch retire la variable après le test.
    monkeypatch.setenv("ARBO_TEST_ABSENTE", "x")
    monkeypatch.delenv("ARBO_TEST_ABSENTE")
    monkeypatch.setenv("ARBO_TEST_EN_LIGNE", "en_ligne")
    monkeypatch.chdir(tmp_path)

    path = script._load_env()

    assert os.path.samefile(path, tmp_path / ".env")
    assert os.environ["ARBO_TEST_ABSENTE"] == "du_fichier"
    assert os.environ["ARBO_TEST_EN_LIGNE"] == "en_ligne"


def test_tout_le_compte_rendu_s_ecrit_en_cp1252(store, capsys):
    """La console du juriste — et le fichier où il garde ce compte rendu,
    sa seule trace — encode cp1252 : un caractère hors de cp1252 s'y
    imprimerait « ? ». Tout ce que le script écrit doit y tenir."""
    _seed_a_adopter(store)
    _seed_folder(store, "corr", "Correspondance")
    _seed_folder(store, "jug", "Jugements", "corr")
    _seed_folder(store, "mien", "Déboursés")
    for argv in ([], ["--apply"], ["--apply"]):
        script.main(argv)
        out = capsys.readouterr().out
        out.encode("cp1252")                      # strict : lève sinon
    for text in (*script._ADOPTED.values(), *script._RELOCATED.values(),
                 script._UNREPORTED, script._NOTE_RECORD, script._NOTE_RECREATE,
                 script._NOTE_REUSED, script._NO_DOSSIER_READ,
                 *script._REASONS.values()):
        text.encode("cp1252")
