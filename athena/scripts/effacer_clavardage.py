"""Effacer les données du clavardage (Phase N) — usage UNIQUE, 2026-09-02.

Le cabinet est passé à un compte Claude for Work couvert par une entente de
traitement des données. Le clavardage interne n'existait que pour une
raison — que le matériel privilégié ne transite pas par un produit IA grand
public — et cette raison tombe. Décision du praticien : retrait total, et
les données s'effacent sans être exportées.

Pourquoi un script, alors que `DEPLOYMENT.md` §15 dit « never script this
into the app » : cette consigne vise l'effacement Loi 25 d'UNE conversation,
un geste que l'avocat doit poser à la main pour qu'il reste un geste. Ici
c'est la désinstallation d'un sous-système entier, à exécuter une fois — et
un script relu vaut mieux que trente suppressions à la console, dont
n'importe laquelle oubliée laisserait du privilégié orphelin. Ce que §15
interdit est un chemin d'effacement DANS l'application ; celui-ci vit dans
`scripts/` et se supprime avec le lot.

⛔ IRRÉVERSIBLE — à terme. Firestore garde une récupération à un instant
donné (7 jours) et des sauvegardes à 90 jours ; le seau, une suppression
douce de 7 jours sans versionnement. La date d'irréversibilité réelle est
donc dans trois mois, pas le soir de l'exécution : le dire plutôt que
laisser croire à un point de non-retour immédiat.

## Ce qui rend cet effacement délicat

**Les sous-collections NE CASCADENT PAS.** Supprimer un document de
conversation laisse ses `turns` vivants et inatteignables — du privilégié
devenu NON ATTRIBUABLE : le seul document qui disait à quel dossier il
appartenait est parti, on ne peut donc plus ni finir de l'effacer avec
certitude, ni répondre à un client qui demande ce qui le concerne. Les
enfants d'abord, toujours ; et si un enfant échoue, la tête est CONSERVÉE.
Une tête retenue coûte une exécution de plus ; l'inverse est irréparable en
information.

**L'énumération passe par `list_documents()`, pas `stream()`.** `stream()`
ne rend que les documents qui EXISTENT ; une tête déjà supprimée au-dessus
d'enfants vivants — précisément l'état qu'une exécution interrompue laisse —
lui est invisible. `list_documents()` rend aussi ces parents fantômes.

**Storage se sonde AVANT de toucher à Firestore.** Les blocs déchargés
portent la sortie de modèle VERBATIM, et leur unique index est le
`storage_ref.path` inscrit dans les tours. Effacer Firestore d'abord puis
échouer sur le seau laisserait 57 Mo de privilégié sans plus rien pour le
retrouver. La sonde est donc une condition d'entrée.

**Le motif Storage exige le SEGMENT, pas la sous-chaîne.** `"/chat/" in nom`
serait sûr aujourd'hui, mais sa sûreté est le résultat d'un audit et un
audit se périme ; un dossier de classement nommé « chat » suffirait à faire
correspondre une pièce du cabinet, et un faux positif ici détruit une
preuve. Le segment est sûr par construction.

**Le balayage du seau est COMPLET, sans préfixe** : le déchargement
téléverse AVANT le commit qui le référence, donc un objet peut exister
qu'aucun tour ne nomme. Un balayage préfixé par conversation les manquerait.

**La charte part avec le reste**, contre l'exemption de §15 (« a prior
version is the proof of what governed a given turn »). Cette exemption
protège une preuve tant que le clavardage TOURNE ; il ne tournera plus.

**Rien n'est journalisé DANS l'application**, et ce serait l'erreur :
`models/audit_event.list_recent` lit une fenêtre dure de 200 documents et
filtre en Python APRÈS la lecture. Le lot fait 200 entités — une ligne par
entité évincerait la TOTALITÉ de l'historique de suppression du cabinet,
après quoi `list_deletions(entity_type="invoice")` répondrait vide avec
`truncated: false`, une affirmation de complétude fausse servie au
connecteur. Le compte rendu sur stdout est le document contemporain (le
rediriger vers un fichier) ; la preuve est le journal Data Access de la
plateforme.

    python -m scripts.effacer_clavardage            # inventaire, rien n'est écrit
    python -m scripts.effacer_clavardage --apply    # efface
    python -m scripts.effacer_clavardage            # 3ᵉ passage : doit lire RESTE : 0
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass


_load_env()

# (collection, sous-collections à vider AVANT la tête).
#
# Les noms sont ÉCRITS ICI plutôt qu'importés des modules `models/chat_*` :
# ce script doit rester exécutable APRÈS leur suppression, et il n'a de sens
# qu'une fois. Relevés sur ces modules avant d'être transcrits (COLLECTION /
# TURNS_SUBCOLLECTION / VERSIONS_SUBCOLLECTION / FILES_SUBCOLLECTION), et la
# couverture a été vérifiée contre Firestore par `doc_ref.collections()` —
# une sous-collection oubliée ne serait pas COMPTÉE, donc son absence du
# décompte ne prouverait rien.
#
# L'ORDRE est l'ordre d'exécution : les conversations d'abord, parce
# qu'elles portent le plus de privilégié et qu'un échec doit s'y voir tôt ;
# les compteurs en dernier, parce qu'ils n'en portent aucun.
_CIBLES = (
    ("chat_conversations", ("turns",)),
    ("chat_drafts", ("versions",)),
    ("chat_skills", ("versions", "fichiers")),
    ("chat_charter", ("versions", "fichiers")),
    ("chat_scheduled_tasks", ()),
    ("chat_usage_dossier", ()),
)

_STORAGE_RACINE = "users"
_STORAGE_SEGMENT = "chat"


def _est_bloc_de_clavardage(nom: str) -> bool:
    """`users/{uid}/chat/{conv}/{turn}/{uuid}.json` — le SEGMENT, pas la
    sous-chaîne. Couvre gratuitement le cas dégénéré `users//chat/…` qu'un
    `owner_uid` vide aurait produit."""
    parts = nom.split("/")
    return (
        len(parts) > 3
        and parts[0] == _STORAGE_RACINE
        and parts[2] == _STORAGE_SEGMENT
    )


def _ouvrir_seau():
    """Le seau, ou une exception. Sondé AVANT Firestore — voir la docstring.

    Le nom vient de la configuration, jamais d'une devinette : le seau App
    Engine par défaut `<projet>.appspot.com` EXISTE et ne contient aucune
    donnée de clavardage, si bien qu'un script qui l'aurait deviné listerait
    zéro objet et imprimerait « 0 objet supprimé » — un bulletin de succès
    sur 57 Mo intacts.
    """
    import firebase_admin
    from firebase_admin import storage

    from config import Config

    try:
        firebase_admin.initialize_app(
            options={"storageBucket": Config.FIREBASE_STORAGE_BUCKET}
        )
    except ValueError:
        pass
    seau = storage.bucket()
    seau.reload()          # prouve l'existence et l'accès, pas seulement le nom
    return seau


def _blocs(seau) -> list:
    return [b for b in seau.list_blobs() if _est_bloc_de_clavardage(b.name)]


def _effacer_collection(db, nom: str, sous: tuple, *, apply: bool) -> tuple:
    """(têtes, enfants, échecs). Les enfants d'abord — ils ne cascadent pas."""
    tetes = enfants = echecs = 0
    for ref in db.collection(nom).list_documents():
        pour_ce_doc = 0
        casse = False
        for sc in sous:
            try:
                for enfant in ref.collection(sc).list_documents():
                    if apply:
                        enfant.delete()
                    pour_ce_doc += 1
            except Exception as exc:
                casse = True
                print(f"    ÉCHEC {nom}/{ref.id}/{sc} : "
                      f"{type(exc).__name__} — tête CONSERVÉE")
        enfants += pour_ce_doc
        if casse:
            echecs += 1
            continue
        detail = f"  (+{pour_ce_doc} enfant{'s' if pour_ce_doc > 1 else ''})"
        print(f"    {ref.id}{detail if pour_ce_doc else ''}")
        if apply:
            # La suppression de la TÊTE a son propre garde : sans lui, son
            # exception remontait au `except` de `main`, qui imprimait
            # « ÉCHEC de lecture … rien n'y a été supprimé » — faux sur les
            # deux points —, abandonnait le reste de la collection, et
            # perdait le compte des enfants DÉJÀ détruits. Le compte rendu
            # d'un acte irréversible sous-estimait ce qui avait disparu.
            try:
                ref.delete()
            except Exception as exc:
                echecs += 1
                print(f"    ÉCHEC de suppression {nom}/{ref.id} : "
                      f"{type(exc).__name__} — ses {pour_ce_doc} enfant(s) "
                      f"sont DÉJÀ partis; relancer")
                continue
        tetes += 1
    return tetes, enfants, echecs


def _verifier(db, seau) -> int:
    """La passe finale. Sans elle, la complétude n'est affirmée que par les
    compteurs du script — ceux-là mêmes qu'un échec corrompt."""
    reste = 0
    for nom, sous in _CIBLES:
        try:
            n = sum(1 for _ in db.collection(nom).list_documents())
        except Exception as exc:
            print(f"    {nom} : ILLISIBLE ({type(exc).__name__}) — "
                  f"complétude NON vérifiée")
            reste += 1
            continue
        if n:
            print(f"    {nom} : RESTE {n} document(s)")
            reste += n
    if seau is not None:
        try:
            n = len(_blocs(seau))
        except Exception as exc:
            print(f"    Storage : ILLISIBLE ({type(exc).__name__})")
            reste += 1
        else:
            if n:
                print(f"    Storage : RESTE {n} objet(s)")
                reste += n
    return reste


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="efface (sans ce drapeau : inventaire seulement)")
    args = parser.parse_args()

    # Le compte rendu porte des accents et « » : une console Windows en
    # cp1252 lèverait UnicodeEncodeError APRÈS avoir écrit une partie des
    # lignes. Même remède que scripts/migrate_vocabulaires.py.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from config import Config
    from models import db

    # Ce que l'opérateur doit LIRE avant de croire un décompte : un projet
    # ou un seau différent de celui qu'il croit produirait « 0 objet » et
    # ressemblerait à un succès.
    print("cible")
    print(f"  projet Firestore : {db.project}")
    print(f"  seau Storage     : {Config.FIREBASE_STORAGE_BUCKET}")
    print()

    try:
        seau = _ouvrir_seau()
    except Exception as exc:
        print(f"⛔ Storage INACCESSIBLE : {type(exc).__name__}: {exc}")
        print()
        print("Rien n'a été touché. Les blocs déchargés portent la sortie de")
        print("modèle verbatim, et leur unique index est le chemin inscrit")
        print("dans les tours : effacer Firestore d'abord laisserait 57 Mo de")
        print("privilégié sans plus rien pour le retrouver. Vérifier")
        print("FIREBASE_STORAGE_BUCKET puis relancer.")
        return 2

    if args.apply:
        print("⛔ EFFACEMENT RÉEL.")
        print()

    tetes = enfants = echecs = 0
    for nom, sous in _CIBLES:
        print(f"  ── {nom}")
        try:
            t, e, x = _effacer_collection(db, nom, sous, apply=args.apply)
        except Exception as exc:
            print(f"    ÉCHEC d'ÉNUMÉRATION ({type(exc).__name__}) — reste de "
                  f"la collection NON traité")
            echecs += 1
            continue
        tetes += t
        enfants += e
        echecs += x
        if not t and not e:
            print("    (vide)")

    print("  ── Firebase Storage (blocs déchargés)")
    n_blobs = octets = 0
    try:
        for blob in _blocs(seau):
            n_blobs += 1
            octets += blob.size or 0
            print(f"    {blob.name}  ({(blob.size or 0) // 1024} Ko)")
            if args.apply:
                blob.delete()
    except Exception as exc:
        echecs += 1
        print(f"    ÉCHEC ({type(exc).__name__}) — blocs NON tous supprimés")
    if not n_blobs:
        print("    (aucun)")

    print()
    print("  ── vérification")
    reste = _verifier(db, seau)
    if not reste:
        print("    RESTE : 0")

    print()
    print(f"documents : {tetes} · enfants de sous-collection : {enfants} · "
          f"objets Storage : {n_blobs} ({octets // 1024} Ko)")

    if not args.apply:
        print("INVENTAIRE — rien n'a été effacé. Relancer avec --apply.")
        return 0
    if echecs or reste:
        print(f"⚠ {echecs} échec(s), {reste} document(s)/objet(s) restant(s) — "
              f"RELANCER. Le script reprend là où il s'est arrêté, et une "
              f"tête n'est jamais supprimée au-dessus d'enfants vivants.")
        return 1
    print("Effacé, et vérifié à zéro. La récupération Firestore (7 j) et les "
          "sauvegardes (90 j) rendent ceci réversible jusqu'en décembre.")
    print("Reste hors du dépôt : supprimer le service « chat », la file "
          "« chat-turns », et révoquer la portée Exchange RBAC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
