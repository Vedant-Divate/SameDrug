"""Database build tests: fixture -> tmp db, round-trip, rebuild idempotency."""

import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest

from samedrug.pipeline.build_db import build_db, main

FIXTURE = Path(__file__).parent.parent / "fixtures" / "jap_products_sample.json"
NPPA_ALL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "nppa_all_sample.csv"
NPPA_SPECIAL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "nppa_special_sample.csv"

ROOT = Path(__file__).parent.parent.parent
JAP_SEED = ROOT / "data" / "raw" / "jap_products.json"
NPPA_ALL_SEED = ROOT / "data" / "raw" / "nppa_ceiling_all.csv"
NPPA_SPECIAL_SEED = ROOT / "data" / "raw" / "nppa_ceiling_special.csv"
SEEDS_PRESENT = JAP_SEED.exists() and NPPA_ALL_SEED.exists() and NPPA_SPECIAL_SEED.exists()
needs_seeds = pytest.mark.skipif(not SEEDS_PRESENT, reason="gitignored raw seeds absent")


def _meta(db_path: Path) -> dict[str, str]:
    with sqlite3.connect(db_path) as conn:
        return dict(conn.execute("SELECT key, value FROM meta"))


def test_build_from_fixture_writes_rows_meta_and_rejects(tmp_path):
    db_path = tmp_path / "drugs.db"
    rejects_path = tmp_path / "rejects.csv"
    stats = build_db(FIXTURE, db_path, rejects_path, include_nppa=False)

    assert stats.total == 12
    assert stats.kept == 12
    assert stats.rejected == 0

    with sqlite3.connect(db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()
        assert count == 12
        row = conn.execute(
            "SELECT product_id, drug_code, generic_name, mrp, status,"
            " ingested_at, source_sha256 FROM jap_products WHERE product_id = 1"
        ).fetchone()
        assert row[0] == 1 and row[1] == 1
        assert row[2] == "Aceclofenac 100mg and Paracetamol 325mg Tablets"
        assert row[3] == 10.32 and row[4] == 1
        assert row[5] == stats.build_ts
        assert row[6] == stats.sha256

    meta = _meta(db_path)
    expected_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest().upper()
    assert meta["source_sha256"] == expected_sha == stats.sha256
    assert meta["source_file"] == str(FIXTURE)
    assert meta["total_rows"] == "12"
    assert meta["kept_rows"] == "12"
    assert meta["rejected_rows"] == "0"
    assert meta["build_ts"] == stats.build_ts

    with open(rejects_path, encoding="utf-8") as f:
        assert list(csv.reader(f)) == [["product_id", "reason"]]


def test_rebuild_is_idempotent(tmp_path):
    db_path = tmp_path / "drugs.db"
    rejects_path = tmp_path / "rejects.csv"
    build_db(FIXTURE, db_path, rejects_path, include_nppa=False)
    build_db(FIXTURE, db_path, rejects_path, include_nppa=False)

    with sqlite3.connect(db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()
        assert count == 12
        (meta_count,) = conn.execute("SELECT COUNT(*) FROM meta").fetchone()
        assert meta_count == 6


def _full_fixture_build(tmp_path):
    db_path = tmp_path / "drugs.db"
    stats = build_db(
        FIXTURE,
        db_path,
        tmp_path / "jap_rejects.csv",
        nppa_all_path=NPPA_ALL_FIXTURE,
        nppa_special_path=NPPA_SPECIAL_FIXTURE,
        nppa_rejects_path=tmp_path / "nppa_rejects.csv",
        run_match=False,
    )
    return db_path, stats


def test_nppa_tables_populated_from_fixtures(tmp_path):
    db_path, stats = _full_fixture_build(tmp_path)

    assert stats.nppa_total == 30
    assert stats.nppa_kept == 29
    assert stats.nppa_rejected == 1
    assert stats.nppa_reject_reasons == {"duplicate_row": 1}
    assert stats.nppa_duplicate_count == 1

    with sqlite3.connect(db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()
        assert count == 29
        row = conn.execute(
            "SELECT sl_no, nlem_version, source_file, formulation_raw, form_raw,"
            " strength_raw, so_number, so_date, price_value, unit_type, unit_count,"
            " pack_volume_ml, container, qualifier_raw, source_row"
            " FROM nppa_ceiling_prices WHERE sl_no = 3 AND source_file = 'nppa_all'"
        ).fetchone()
        assert row == (
            3, "2011", "nppa_all", "Condom", "CONDOM", None, "1582(E)", "25-Mar-2026",
            11.65, "condom", 1.0, None, None, "(1 Condom)",
            "nppa_all sl_no=3 csv_record=8",
        )
        sls = [r[0] for r in conn.execute(
            "SELECT sl_no FROM nppa_ceiling_prices WHERE formulation_raw LIKE '%INSULIN%'"
        )]
        assert sls == [531]
        (special_count,) = conn.execute(
            "SELECT COUNT(*) FROM nppa_ceiling_prices WHERE source_file = 'nppa_special'"
        ).fetchone()
        assert special_count == 5
        (null_nlem,) = conn.execute(
            "SELECT COUNT(*) FROM nppa_ceiling_prices WHERE nlem_version IS NULL"
        ).fetchone()
        assert null_nlem == 5

    meta = _meta(db_path)
    assert len(meta) == 16
    assert meta["nppa_all_rows"] == "25"
    assert meta["nppa_all_kept"] == "24"
    assert meta["nppa_all_rejected"] == "1"
    assert meta["nppa_special_rows"] == "5"
    assert meta["nppa_special_kept"] == "5"
    assert meta["nppa_special_rejected"] == "0"
    assert meta["nppa_duplicate_count"] == "1"
    assert meta["nppa_unique_formulation_keys"] == str(stats.nppa_unique_formulation_keys)
    sha_paths = (
        ("nppa_all_sha256", NPPA_ALL_FIXTURE),
        ("nppa_special_sha256", NPPA_SPECIAL_FIXTURE),
    )
    for key, path in sha_paths:
        assert meta[key] == hashlib.sha256(path.read_bytes()).hexdigest().upper()

    with open(tmp_path / "nppa_rejects.csv", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [
            ["source_file", "sl_no", "reason"],
            ["nppa_all", "532", "duplicate_row"],
        ]


def test_partial_rebuilds_leave_other_side_undisturbed(tmp_path):
    db_path, _ = _full_fixture_build(tmp_path)
    with sqlite3.connect(db_path) as conn:
        jap_before = conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0]
        nppa_before = conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()[0]
    assert (jap_before, nppa_before) == (12, 29)

    build_db(
        FIXTURE, db_path, tmp_path / "j.csv",
        nppa_all_path=NPPA_ALL_FIXTURE,
        nppa_special_path=NPPA_SPECIAL_FIXTURE,
        nppa_rejects_path=tmp_path / "n.csv",
        include_jap=False,
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()[0] == 29

    build_db(
        FIXTURE, db_path, tmp_path / "j.csv",
        nppa_all_path=NPPA_ALL_FIXTURE,
        nppa_special_path=NPPA_SPECIAL_FIXTURE,
        nppa_rejects_path=tmp_path / "n.csv",
        include_nppa=False,
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()[0] == 29
        assert len(_meta(db_path)) == 16


def test_cli_scope_flags(tmp_path):
    db_path = tmp_path / "drugs.db"
    base = ["--source", str(FIXTURE), "--db", str(db_path),
            "--rejects", str(tmp_path / "j.csv"),
            "--nppa-all", str(NPPA_ALL_FIXTURE),
            "--nppa-special", str(NPPA_SPECIAL_FIXTURE),
            "--nppa-rejects", str(tmp_path / "n.csv"),
            "--no-match"]
    main([*base, "--jap-only"])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0] == 12
        assert "nppa_ceiling_prices" not in {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    main([*base, "--nppa-only"])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()[0] == 29


@needs_seeds
def test_jap_regression_full_rebuild_from_real_seeds(tmp_path):
    """Phase 1 gate: a full rebuild must leave JAP at 2,439 rows / 387 zero-MRP."""
    db_path = tmp_path / "drugs.db"
    stats = build_db(
        JAP_SEED,
        db_path,
        tmp_path / "jap_rejects.csv",
        nppa_all_path=NPPA_ALL_SEED,
        nppa_special_path=NPPA_SPECIAL_SEED,
        nppa_rejects_path=tmp_path / "nppa_rejects.csv",
    )
    assert (stats.total, stats.kept, stats.rejected) == (2439, 2439, 0)
    assert (stats.nppa_total, stats.nppa_kept, stats.nppa_rejected) == (937, 936, 1)
    assert stats.nppa_duplicate_count == 1
    with sqlite3.connect(db_path) as conn:
        (jap_count,) = conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()
        (zero_mrp,) = conn.execute("SELECT COUNT(*) FROM jap_products WHERE mrp = 0").fetchone()
        (nppa_count,) = conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()
    assert jap_count == 2439
    assert zero_mrp == 387
    assert nppa_count == 936
    # Rebuild is idempotent.
    build_db(
        JAP_SEED, db_path, tmp_path / "jap_rejects.csv",
        nppa_all_path=NPPA_ALL_SEED,
        nppa_special_path=NPPA_SPECIAL_SEED,
        nppa_rejects_path=tmp_path / "nppa_rejects.csv",
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0] == 2439
        assert conn.execute("SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()[0] == 936
