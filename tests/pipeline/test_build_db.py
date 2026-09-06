"""Database build tests: fixture -> tmp db, round-trip, rebuild idempotency."""

import csv
import hashlib
import sqlite3
from pathlib import Path

from samedrug.pipeline.build_db import build_db

FIXTURE = Path(__file__).parent.parent / "fixtures" / "jap_products_sample.json"


def _meta(db_path: Path) -> dict[str, str]:
    with sqlite3.connect(db_path) as conn:
        return dict(conn.execute("SELECT key, value FROM meta"))


def test_build_from_fixture_writes_rows_meta_and_rejects(tmp_path):
    db_path = tmp_path / "drugs.db"
    rejects_path = tmp_path / "rejects.csv"
    stats = build_db(FIXTURE, db_path, rejects_path)

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
    build_db(FIXTURE, db_path, rejects_path)
    build_db(FIXTURE, db_path, rejects_path)

    with sqlite3.connect(db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()
        assert count == 12
        (meta_count,) = conn.execute("SELECT COUNT(*) FROM meta").fetchone()
        assert meta_count == 6
