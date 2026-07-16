"""Seed Firestore with Quebec court reference data.

Usage:
    python -m scripts.seed_reference_data

Requires GOOGLE_APPLICATION_CREDENTIALS or running within App Engine context.

The tables are SOURCED FROM models/reference.py — the in-memory tables the
running app actually reads — rather than re-listed here. They used to be
duplicated as literals in both files, and had already drifted (SHARED_GREFFES
existed only here, so get_greffe() never returned other_locations). Seeding
from the single source keeps ref_greffes/ref_juridictions/ref_palais a
faithful mirror. Nothing reads these collections today; they exist for a
future admin UI.
"""

from datetime import datetime, timezone

import firebase_admin
from firebase_admin import firestore

from models.reference import _FORUMS, _GREFFES, _JURIDICTIONS, _PALAIS

# Itinerant circuit greffes serve several communities from one file number.
# Not part of the in-memory table (nothing in the app reads them yet), so
# they stay here, attached at seed time.
SHARED_GREFFES = {
    "614": [
        "Eastmain", "Mistissini", "Nemiscau",
        "Oujé-Bougoumou", "Waskaganish", "Waswanipi",
        "Wemindji", "Whapmagoostui",
    ],
    "640": [
        "Inukjuak", "Ivujivik", "Kuujjuaraapik",
        "Puvirnituq", "Salluit", "Umiujaq",
    ],
    "635": [
        "Kangiqsualujjuaq", "Kangiqsujuaq", "Kangirsuk",
        "Quaqtaq", "Tasiujaq",
    ],
    "652": [
        "Fermont", "Havre-Saint-Pierre", "Kawawachikamach",
        "La Romaine", "Natashquan", "Port-Cartier",
        "Saint-Augustin", "Schefferville",
    ],
}


def _commit_all(db, collection: str, rows: dict[str, dict]) -> int:
    """Write every row to `collection`, keyed by dict key, in batches."""
    batch = db.batch()
    count = 0
    now = datetime.now(timezone.utc)

    for doc_id, data in rows.items():
        doc_ref = db.collection(collection).document(doc_id)
        batch.set(doc_ref, {**data, "updated_at": now}, merge=True)
        count += 1

        if count % 400 == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    return count


def seed_greffes(db) -> None:
    """Write all greffe documents to the ref_greffes collection."""
    rows = {
        number: {
            "greffe_number": number,
            **greffe,
            **({"other_locations": SHARED_GREFFES[number]}
               if number in SHARED_GREFFES else {}),
        }
        for number, greffe in _GREFFES.items()
    }
    print(f"Seeded {_commit_all(db, 'ref_greffes', rows)} greffe documents.")


def seed_juridictions(db) -> None:
    """Write all juridiction documents to the ref_juridictions collection."""
    rows = {
        number: {"juridiction_number": number, **juridiction}
        for number, juridiction in _JURIDICTIONS.items()
    }
    print(
        f"Seeded {_commit_all(db, 'ref_juridictions', rows)} "
        "juridiction documents."
    )


def seed_palais(db) -> None:
    """Write all court-location documents to the ref_palais collection."""
    rows = {
        key: {"palais_key": key, **palais}
        for key, palais in _PALAIS.items()
    }
    print(f"Seeded {_commit_all(db, 'ref_palais', rows)} court locations.")


def seed_forums(db) -> None:
    """Write all non-judicial-forum documents to the ref_forums collection."""
    rows = {
        key: {"forum_key": key, **forum}
        for key, forum in _FORUMS.items()
    }
    print(f"Seeded {_commit_all(db, 'ref_forums', rows)} forums.")


if __name__ == "__main__":
    firebase_admin.initialize_app()
    db = firestore.client()
    seed_greffes(db)
    seed_juridictions(db)
    seed_palais(db)
    seed_forums(db)
    print("Reference data seeding complete.")
