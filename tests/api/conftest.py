"""Shared fixture: throwaway DB built from tests/fixtures/* via the real pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from samedrug.app.main import create_app
from samedrug.pipeline.build_db import build_db

FIXTURES = Path(__file__).parent.parent / "fixtures"
JAP_FIXTURE = FIXTURES / "jap_products_sample.json"
NPPA_ALL_FIXTURE = FIXTURES / "nppa_all_sample.csv"
NPPA_SPECIAL_FIXTURE = FIXTURES / "nppa_special_sample.csv"


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


def _add_synthetic_equivalences(db_path: Path) -> None:
    """The 12x30-row fixtures yield zero natural equivalents, so equivalence
    rows are synthesized (molecules labeled ``synthetic*``) covering every
    caveat state: positive, negative+tax-hint, negative-genuine,
    modifier_relaxed/low, form_family/low. jap_per_unit values equal the
    real JAP mrp/pack so payloads stay internally consistent.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    computed_at = _one(
        conn, "SELECT value FROM meta WHERE key = 'match_computed_at'"
    )[0]
    count_nppa = _one(
        conn,
        "SELECT id, price_value FROM nppa_ceiling_prices"
        " WHERE unit_type = 'tablet' ORDER BY id LIMIT 1",
    )
    ml_nppa = _one(
        conn,
        "SELECT id FROM nppa_ceiling_prices WHERE unit_type = 'ml'"
        " ORDER BY id LIMIT 1",
    )
    jap403 = _one(conn, "SELECT product_id, mrp FROM jap_products WHERE product_id = 403")
    jap38 = _one(conn, "SELECT product_id, mrp FROM jap_products WHERE product_id = 38")
    jap675 = _one(conn, "SELECT product_id, mrp FROM jap_products WHERE product_id = 675")
    jap1656 = _one(
        conn, "SELECT product_id, mrp FROM jap_products WHERE product_id = 1656"
    )
    jap121 = _one(conn, "SELECT product_id, mrp FROM jap_products WHERE product_id = 121")

    def per_unit(mrp: float, pack: float) -> float:
        return mrp / pack

    canon_rows = [
        (
            "syntheticamycin:250mg|tablet", "syntheticamycin", "250mg",
            "tablet", "tablet", "", f"[{count_nppa['id']}]", f"[{jap403['product_id']}]",
            "synthetic", "high",
        ),
        (
            "syntheticbactin:500mg|tablet", "syntheticbactin", "500mg",
            "tablet", "tablet", "", f"[{count_nppa['id']}]", f"[{jap38['product_id']}]",
            "synthetic", "high",
        ),
        (
            "syntheticcillin:250mg|tablet", "syntheticcillin", "250mg",
            "tablet", "tablet", "", f"[{count_nppa['id']}]", f"[{jap675['product_id']}]",
            "synthetic", "high",
        ),
        (
            "syntheticdazole:20mg|tablet", "syntheticdazole", "20mg",
            "tablet", "tablet", "enteric_coated",
            f"[{count_nppa['id']}]", f"[{jap1656['product_id']}]",
            "modifier_relaxed", "low",
        ),
        (
            "syntheticemycin:33.75mg/ml|suspension", "syntheticemycin", "33.75mg/ml",
            "suspension", "liquid", "", f"[{ml_nppa['id']}]", f"[{jap121['product_id']}]",
            "form_family", "low",
        ),
    ]
    conn.executemany(
        "INSERT INTO canonical_formulations(match_key, molecule_set, strength_set,"
        " form, form_family, modifiers, nppa_ids, jap_product_ids,"
        " match_method, confidence) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        canon_rows,
    )
    pu403 = per_unit(jap403["mrp"], 10.0)  # 10's pack
    pu38 = per_unit(jap38["mrp"], 10.0)
    pu675 = per_unit(jap675["mrp"], 10.0)
    pu1656 = per_unit(jap1656["mrp"], 10.0)
    pu121 = per_unit(jap121["mrp"], 200.0)  # 200 ml pack
    equiv_rows = [
        # positive savings, high confidence, no caveats
        (
            "syntheticamycin:250mg|tablet", count_nppa["id"], jap403["product_id"],
            4.00, "count", "same_unit_type", "2022", pu403,
            "count:10", (1.0 - pu403 / 4.00) * 100.0, "synthetic", "high",
            computed_at,
        ),
        # negative savings, ratio 1.079 in [1.05, 1.15] -> tax-basis hint
        (
            "syntheticbactin:500mg|tablet", count_nppa["id"], jap38["product_id"],
            1.39, "count", "same_unit_type", "2022", pu38,
            "count:10", (1.0 - pu38 / 1.39) * 100.0, "synthetic", "high",
            computed_at,
        ),
        # negative savings, ratio 1.406 outside window -> genuine, no tax hint
        (
            "syntheticcillin:250mg|tablet", count_nppa["id"], jap675["product_id"],
            10.00, "count", "same_unit_type", "2022", pu675,
            "count:10", (1.0 - pu675 / 10.00) * 100.0, "synthetic", "high",
            computed_at,
        ),
        # modifier_relaxed + low confidence -> both caveats
        (
            "syntheticdazole:20mg|tablet", count_nppa["id"], jap1656["product_id"],
            4.00, "count", "same_unit_type", "2022", pu1656,
            "count:10", (1.0 - pu1656 / 4.00) * 100.0,
            "modifier_relaxed", "low", computed_at,
        ),
        # form_family + low confidence, ml-aligned suspension
        (
            "syntheticemycin:33.75mg/ml|suspension", ml_nppa["id"],
            jap121["product_id"], 0.50, "ml", "same_unit_type", "2022", pu121,
            "volume_ml:200", (1.0 - pu121 / 0.50) * 100.0,
            "form_family", "low", computed_at,
        ),
    ]
    conn.executemany(
        "INSERT INTO equivalents(match_key, nppa_row_id, jap_product_id,"
        " nppa_ceiling_per_unit, nppa_unit_basis, nppa_variant_basis,"
        " nppa_nlem_version, jap_per_unit, jap_pack_parsed, savings_pct,"
        " match_method, confidence, computed_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        equiv_rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('equivalents_count', '5')"
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="session")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session DB: real build_db + match pipeline, then labeled synthetics."""
    db_path = tmp_path_factory.mktemp("api_fixture") / "drugs.db"
    build_db(
        JAP_FIXTURE,
        db_path,
        db_path.parent / "jap_rejects.csv",
        nppa_all_path=NPPA_ALL_FIXTURE,
        nppa_special_path=NPPA_SPECIAL_FIXTURE,
        nppa_rejects_path=db_path.parent / "nppa_rejects.csv",
    )
    _add_synthetic_equivalences(db_path)
    return db_path


@pytest.fixture()
def client(fixture_db: Path) -> TestClient:
    return TestClient(create_app(fixture_db))
