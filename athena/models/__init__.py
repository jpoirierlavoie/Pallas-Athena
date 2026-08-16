"""Firestore data-access layer — shared client singleton and query utilities."""

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# Module-level singleton; initialised once on first import.
# In App Engine the project ID is inferred from the environment.
db: firestore.Client = firestore.Client()


def find_by_legacy_ref(
    collection: str, legacy_ref: str, limit: int = 5
) -> list[dict]:
    """Documents of *collection* bearing *legacy_ref* — the import anchor.

    Generic on purpose, and here rather than repeated five times: the query
    is one single-field equality with no per-collection semantics, and the
    caller (the connector's ``find_imported``) asks the same question of
    every collection in one breath. Served by Firestore's AUTOMATIC index —
    no composite, nothing to deploy.

    RAISES on query failure. Its caller's next move is « nothing came back,
    so I will create it », and the connector can never delete what a
    swallowed error would make it create twice.

    Returns raw documents: the caller needs an id and a label, not a
    migrated record, and going through each model's getter would cost a
    second read per hit.
    """
    wanted = (legacy_ref or "").strip()
    if not wanted:
        return []
    docs = (
        db.collection(collection)
        .where(filter=FieldFilter("legacy_ref", "==", wanted))
        .limit(limit)
        .stream()
    )
    return [d.to_dict() or {} for d in docs]


def aggregation_values(results: object) -> dict:
    """Flatten an ``AggregationQuery.get()`` result into ``{alias: value}``.

    ``get()`` returns a list whose items are lists of ``AggregationResult``
    (one inner list per streamed ``RunAggregationQueryResponse`` message);
    a flat list of results is tolerated for robustness.
    """
    values: dict = {}
    for item in results:  # type: ignore[attr-defined]
        batch = item if isinstance(item, (list, tuple)) else [item]
        for agg in batch:
            alias = getattr(agg, "alias", None)
            if alias is not None:
                values[alias] = agg.value
    return values
