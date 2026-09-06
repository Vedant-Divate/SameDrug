"""UI fixtures: reuse the Phase-4 fixture DB untouched (move, don't change)."""

from __future__ import annotations

import shutil
import sqlite3

import pytest
from starlette.testclient import TestClient

from samedrug.app.main import create_app

# Re-exported (not modified): session fixture DB + TestClient factory.
from tests.api.conftest import client as client  # noqa: F401
from tests.api.conftest import fixture_db as fixture_db  # noqa: F401


@pytest.fixture()
def xss_client(fixture_db, tmp_path):
    """Copy of the fixture DB plus an HTML-injection canonical row.

    Proves autoescape on the card and search pages without touching
    tests/api/ (the source fixture stays byte-identical for Phase-4 tests).
    """
    db_path = tmp_path / "xss.db"
    shutil.copy(fixture_db, db_path)
    conn = sqlite3.connect(db_path)
    nppa_id = conn.execute(
        "SELECT id FROM nppa_ceiling_prices WHERE unit_type = 'tablet'"
        " ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO canonical_formulations(match_key, molecule_set, strength_set,"
        " form, form_family, modifiers, nppa_ids, jap_product_ids,"
        " match_method, confidence) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "xss<script>alert(1)</script>:10mg|tablet",
            "xss<script>alert(1)</script>",
            "10mg",
            "tablet",
            "tablet",
            "",
            f"[{nppa_id}]",
            "[403]",
            "exact",
            "high",
        ),
    )
    computed_at = conn.execute(
        "SELECT value FROM meta WHERE key = 'match_computed_at'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO equivalents(match_key, nppa_row_id, jap_product_id,"
        " nppa_ceiling_per_unit, nppa_unit_basis, nppa_variant_basis,"
        " nppa_nlem_version, jap_per_unit, jap_pack_parsed, savings_pct,"
        " match_method, confidence, computed_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "xss<script>alert(1)</script>:10mg|tablet",
            nppa_id,
            403,
            4.00,
            "count",
            "same_unit_type",
            "2022",
            2.813,
            "count:10",
            29.675,
            "exact",
            "high",
            computed_at,
        ),
    )
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path))
