"""Route-layer tests for the per-party roles + avocat rework (July 2026).

The hidden JSON fields round-trip through the browser, so the parser is the
security boundary: an explicit whitelist, junk roles dropped, avocat pair
coerced — never **entry.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from routes.dossiers import _parse_parties_json


def test_parse_accepts_the_full_shape():
    raw = ('[{"id": "p1", "name": "Jean", "roles": ["défendeur", '
           '"demandeur reconventionnel"], "avocat_id": "av1", '
           '"avocat_name": "Roy"}]')
    assert _parse_parties_json(raw) == [{
        "id": "p1", "name": "Jean",
        "roles": ["défendeur", "demandeur reconventionnel"],
        "avocat_id": "av1", "avocat_name": "Roy",
    }]


def test_parse_accepts_a_legacy_bare_entry():
    """Old Alpine state (or a stale open form) posts {id, name} only."""
    assert _parse_parties_json('[{"id": "p1", "name": "Jean"}]') == [{
        "id": "p1", "name": "Jean", "roles": [],
        "avocat_id": "", "avocat_name": "",
    }]


def test_parse_drops_junk_roles_and_foreign_keys():
    """Roles outside the vocabulary are dropped (only a crafted POST can
    produce them), and unknown keys never pass through."""
    raw = ('[{"id": "p1", "name": "J", "roles": ["demandeur", "capitaine", 7],'
           ' "avocat_id": null, "sneaky": "x"}]')
    parsed = _parse_parties_json(raw)
    assert parsed == [{
        "id": "p1", "name": "J", "roles": ["demandeur"],
        "avocat_id": "", "avocat_name": "",
    }]
    assert "sneaky" not in parsed[0]


def test_parse_tolerates_a_non_list_roles_value():
    raw = '[{"id": "p1", "name": "J", "roles": "demandeur"}]'
    assert _parse_parties_json(raw)[0]["roles"] == []
