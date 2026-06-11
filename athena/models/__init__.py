"""Firestore data-access layer — shared client singleton and query utilities."""

from google.cloud import firestore

# Module-level singleton; initialised once on first import.
# In App Engine the project ID is inferred from the environment.
db: firestore.Client = firestore.Client()


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
