"""Build the JAP raw SQLite table with provenance metadata.

Full rebuild semantics: drops and recreates jap_products + meta on every
run. Only active (status=1) rows are stored; rejects go to a CSV audit
trail. CLI: python -m samedrug.pipeline.build_db [--source ...] [--db ...]
[--rejects ...] [--fetch].
"""

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from samedrug.pipeline.download import fetch_jap_products
from samedrug.pipeline.parsers.jap import parse_jap_products
from samedrug.pipeline.sources import JAP_PRODUCTS_URL

SCHEMA = """
DROP TABLE IF EXISTS jap_products;
CREATE TABLE jap_products(
    product_id INTEGER PRIMARY KEY,
    drug_code INTEGER NOT NULL,
    generic_name TEXT NOT NULL,
    group_name TEXT,
    unit_size TEXT,
    mrp REAL,
    status INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    source_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_jap_generic ON jap_products(generic_name);
CREATE INDEX IF NOT EXISTS idx_jap_group ON jap_products(group_name);
DROP TABLE IF EXISTS meta;
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class BuildStats:
    total: int
    kept: int
    rejected: int
    reject_reasons: dict[str, int]
    sha256: str
    build_ts: str


def build_db(
    source_path: str | Path,
    db_path: str | Path,
    rejects_path: str | Path | None = None,
    *,
    fetch_live: bool = False,
) -> BuildStats:
    """Build data/processed-style SQLite DB from the seed file or live fetch."""
    if rejects_path is None:
        rejects_path = Path("data/interim/jap_rejects.csv")
    source_path = Path(source_path)
    db_path = Path(db_path)
    rejects_path = Path(rejects_path)

    if fetch_live:
        data = fetch_jap_products()
        raw_bytes = json.dumps(data, sort_keys=True).encode("utf-8")
        source_label = f"live:{JAP_PRODUCTS_URL}"
    else:
        raw_bytes = source_path.read_bytes()
        data = json.loads(raw_bytes)
        source_label = str(source_path)
    sha256 = hashlib.sha256(raw_bytes).hexdigest().upper()
    build_ts = datetime.now(UTC).isoformat(timespec="seconds")

    kept, rejected = parse_jap_products(data)
    reasons = dict(Counter(r.reason for r in rejected))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO jap_products(product_id, drug_code, generic_name,"
            " group_name, unit_size, mrp, status, ingested_at, source_sha256)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.product_id,
                    r.drug_code,
                    r.generic_name,
                    r.group_name,
                    r.unit_size,
                    r.mrp,
                    r.status,
                    build_ts,
                    sha256,
                )
                for r in kept
            ],
        )
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            [
                ("build_ts", build_ts),
                ("source_file", source_label),
                ("source_sha256", sha256),
                ("total_rows", str(len(kept) + len(rejected))),
                ("kept_rows", str(len(kept))),
                ("rejected_rows", str(len(rejected))),
            ],
        )

    rejects_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rejects_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["product_id", "reason"])
        for r in rejected:
            writer.writerow(["" if r.product_id is None else r.product_id, r.reason])

    return BuildStats(
        total=len(kept) + len(rejected),
        kept=len(kept),
        rejected=len(rejected),
        reject_reasons=reasons,
        sha256=sha256,
        build_ts=build_ts,
    )


def main(argv: list[str] | None = None) -> BuildStats:
    parser = argparse.ArgumentParser(description="Build the JAP raw SQLite table.")
    parser.add_argument("--source", default="data/raw/jap_products.json")
    parser.add_argument("--db", default="data/processed/drugs.db")
    parser.add_argument("--rejects", default="data/interim/jap_rejects.csv")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="live-fetch via download.py instead of reading --source",
    )
    args = parser.parse_args(argv)
    stats = build_db(args.source, args.db, args.rejects, fetch_live=args.fetch)
    print(f"total={stats.total} kept={stats.kept} rejected={stats.rejected}")
    print(f"reasons={stats.reject_reasons}")
    print(f"sha256={stats.sha256}")
    print(f"build_ts={stats.build_ts}")
    return stats


if __name__ == "__main__":
    main()
