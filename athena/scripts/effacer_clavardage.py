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
n'importe laquelle oubliée laisserait du privilégié orphelin.

⚠ IRRÉVERSIBLE. Le seul filet est la récupération Firestore à un instant
donné (7 jours), que le praticien a confirmée active.

## Ce qui rend cet effacement délicat

**Les sous-collections NE CASCADENT PAS.** Supprimer un document de
conversation laisse ses `turns` vivants et inatteignables — du privilégié
orphelin, exactement le défaut du `folder_id` mort. L'ordre est donc
toujours : les enfants d'abord, la tête ensuite. Et si un enfant échoue, la
tête n'est PAS supprimée : mieux vaut un état rejouable qu'une tête absente
au-dessus d'enfants vivants.

**Storage n'a aucune règle de cycle de vie sur ce préfixe.** Les blocs
déchargés (toute charge de plus de 100 Ko, réhydratée byte-exact à
l'assemblage) sont permanents et portent du texte de document et de
courriel VERBATIM.

**La charte est délibérément HORS du périmètre de l'effacement Loi 25**
(§15 : « a prior version is the proof of what governed a given turn »).
Cette exemption protège une preuve tant que le clavardage TOURNE. Il ne
tournera plus : il n'y a plus de tour dont prouver le régime, et la charte
part avec le reste.

**Ce script ne journalise RIEN dans l'application.** La trace d'un
effacement est l'audit logging Data Access de Firestore, qui est le contrôle
compensatoire prévu par §15 — pas une ligne que l'application s'écrirait à
elle-même dans un sous-système qu'elle est en train de retirer.

    python -m scripts.effacer_clavardage            # simulation
    python -m scripts.effacer_clavardage --apply    # efface
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

from models import db  # noqa: E402

# (collection, sous-collections à vider AVANT la tête).
#
# Les noms sont ÉCRITS ICI plutôt qu'importés des modules `models/chat_*` :
# ce script doit rester exécutable après leur suppression, et il n'a de sens
# qu'une fois. Ils ont été relevés sur ces modules avant d'être transcrits
# (COLLECTION / TURNS_SUBCOLLECTION / VERSIONS_SUBCOLLECTION /
# FILES_SUBCOLLECTION).
#
# L'ORDRE de la liste est l'ordre d'exécution : les conversations d'abord,
# parce qu'elles portent le plus de privilégié et que c'est là qu'un échec
# doit être vu tôt ; les compteurs en dernier, parce qu'ils n'en portent
# aucun.
_CIBLES = (
    ("chat_conversations", ("turns",)),
    ("chat_drafts", ("versions",)),
    ("chat_skills", ("versions", "fichiers")),
    ("chat_charter", ("versions", "fichiers")),
    ("chat_scheduled_tasks", ()),
    ("chat_usage_dossier", ()),
)

# Le chemin des blocs déchargés : `users/{uid}/chat/{conv}/{turn}/{uuid}.json`.
#
# On exige le SEGMENT, pas la sous-chaîne : `"/chat/" in nom` marcherait
# aujourd'hui (les quatre préfixes du bucket sont `users/{uid}/dossiers`,
# `.../templates`, `.../chat` et `staging/{uid}/exports` — relevé le
# 2026-09-02), mais un dossier de classement nommé « chat » par le juriste
# suffirait à faire correspondre un document du cabinet. Un faux positif
# ici détruit une pièce.
_STORAGE_RACINE = "users"
_STORAGE_SEGMENT = "chat"


def _est_bloc_de_clavardage(nom: str) -> bool:
    parts = nom.split("/")
    return (
        len(parts) > 3
        and parts[0] == _STORAGE_RACINE
        and parts[2] == _STORAGE_SEGMENT
    )


def _effacer_collection(nom: str, sous: tuple, *, apply: bool) -> tuple:
    """(têtes, enfants, échecs). Les enfants d'abord — ils ne cascadent pas."""
    tetes = enfants = echecs = 0
    for snap in db.collection(nom).stream():
        ref = snap.reference
        pour_ce_doc = 0
        casse = False
        for sc in sous:
            try:
                for enfant in ref.collection(sc).stream():
                    pour_ce_doc += 1
                    if apply:
                        enfant.reference.delete()
            except Exception as exc:
                casse = True
                print(f"    ÉCHEC {nom}/{snap.id}/{sc} : "
                      f"{type(exc).__name__} — tête CONSERVÉE")
        enfants += pour_ce_doc
        if casse:
            # Fail closed. Une tête supprimée au-dessus d'enfants vivants
            # est du privilégié devenu inatteignable ; un état rejouable
            # est préférable, et la seconde exécution reprendra ici.
            echecs += 1
            continue
        tetes += 1
        detail = f"  (+{pour_ce_doc} enfant{'s' if pour_ce_doc > 1 else ''})"
        print(f"    {snap.id}{detail if pour_ce_doc else ''}")
        if apply:
            ref.delete()
    return tetes, enfants, echecs


def _effacer_storage(*, apply: bool) -> tuple:
    """Les blocs déchargés. Aucune règle de cycle de vie ne les balaie."""
    try:
        import firebase_admin
        from firebase_admin import storage

        from config import Config

        try:
            firebase_admin.initialize_app(
                options={"storageBucket": Config.FIREBASE_STORAGE_BUCKET}
            )
        except ValueError:
            pass
        bucket = storage.bucket()
    except Exception as exc:
        print(f"    Storage INACCESSIBLE ({type(exc).__name__}) — les blocs "
              f"déchargés restent à effacer.")
        return 0, 0

    n = octets = 0
    for blob in bucket.list_blobs():
        if not _est_bloc_de_clavardage(blob.name):
            continue
        n += 1
        octets += blob.size or 0
        print(f"    {blob.name}  ({(blob.size or 0) // 1024} Ko)")
        if apply:
            blob.delete()
    return n, octets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="efface (sans ce drapeau : simulation)")
    args = parser.parse_args()

    # Le compte rendu porte des accents et « » : une console Windows en
    # cp1252 lèverait UnicodeEncodeError APRÈS avoir écrit une partie des
    # lignes. Même remède que scripts/migrate_vocabulaires.py.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.apply:
        print("⚠ EFFACEMENT RÉEL — irréversible hors récupération Firestore.")
    print()

    tetes = enfants = echecs = 0
    for nom, sous in _CIBLES:
        print(f"  ── {nom}")
        try:
            t, e, x = _effacer_collection(nom, sous, apply=args.apply)
        except Exception as exc:
            # Une collection illisible est IGNORÉE, jamais devinée vide :
            # afficher « (vide) » sur une erreur de lecture ferait passer
            # un échec pour un succès.
            print(f"    ÉCHEC de lecture ({type(exc).__name__}) — collection "
                  f"IGNORÉE, rien n'y a été supprimé.")
            echecs += 1
            continue
        tetes += t
        enfants += e
        echecs += x
        if not t and not e:
            print("    (vide)")

    print("  ── Firebase Storage (blocs déchargés)")
    n_blobs, octets = _effacer_storage(apply=args.apply)
    if not n_blobs:
        print("    (aucun)")

    print()
    print(f"documents : {tetes} · enfants de sous-collection : {enfants} · "
          f"objets Storage : {n_blobs} ({octets // 1024} Ko)")
    if echecs:
        print(f"⚠ {echecs} échec(s) — RELANCER : le script reprend là où il "
              f"s'est arrêté, et une tête n'est jamais supprimée au-dessus "
              f"d'enfants vivants.")
    if not args.apply:
        print("SIMULATION — rien n'a été effacé. Relancer avec --apply.")
    elif not echecs:
        print("Effacé. Reste hors du dépôt : supprimer le service « chat », "
              "la file « chat-turns », et révoquer la portée Exchange RBAC.")
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
