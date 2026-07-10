"""Tests for models.document.projet_document_name (Phase H.2 naming).

CI-only: importing models.document pulls in the google-cloud libraries
(present in the deploy-gate install).
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.document import projet_document_name


def test_projet_name_full_convention():
    assert projet_document_name(
        "2026-042", "Correspondance avec avocat", date(2026, 7, 15)
    ) == "2026-042 - 2026-07-15 - Projet Correspondance avec avocat"


def test_projet_name_drops_empty_reference():
    assert projet_document_name(
        "", "Note d'honoraires", date(2026, 7, 15)
    ) == "2026-07-15 - Projet Note d'honoraires"
