"""Build the JAP + NPPA raw SQLite tables with provenance metadata.

Full rebuild semantics: drops and recreates jap_products + meta on every
full run. Only active (status=1) JAP rows are stored; rejects go to a CSV
audit trail. Phase 2 adds the nppa_ceiling_prices table (drop-if-exists)
built from both NPPA ceiling CSVs via samedrug.pipeline.parsers.nppa.
CLI: python -m samedrug.pipeline.build_db [--source ...] [--db ...]
[--rejects ...] [--fetch] [--nppa-all ...] [--nppa-special ...]
[--nppa-rejects ...] [--jap-only | --nppa-only].
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
from samedrug.pipeline.parsers.nppa import parse_nppa_csv
from samedrug.pipeline.sources import JAP_PRODUCTS_URL

SCHEMA_JAP = """
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
"""

SCHEMA_NPPA = """
DROP TABLE IF EXISTS nppa_ceiling_prices;
CREATE TABLE nppa_ceiling_prices(
    id INTEGER PRIMARY KEY,
    sl_no INTEGER,
    nlem_version TEXT,
    source_file TEXT NOT NULL,
    formulation_raw TEXT NOT NULL,
    form_raw TEXT,
    strength_raw TEXT,
    so_number TEXT NOT NULL,
    so_date TEXT NOT NULL,
    price_value REAL NOT NULL,
    unit_type TEXT,
    unit_count REAL,
    pack_volume_ml REAL,
    container TEXT,
    pack_condition_raw TEXT,
    qualifier_raw TEXT NOT NULL,
    source_row TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nppa_formulation ON nppa_ceiling_prices(formulation_raw);
CREATE INDEX IF NOT EXISTS idx_nppa_form ON nppa_ceiling_prices(form_raw);
"""

SCHEMA_META = """
DROP TABLE IF EXISTS meta;
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEMA = SCHEMA_JAP + SCHEMA_NPPA + SCHEMA_META

NPPA_ALL_DEFAULT = "data/raw/nppa_ceiling_all.csv"
NPPA_SPECIAL_DEFAULT = "data/raw/nppa_ceiling_special.csv"


@dataclass
class BuildStats:
    total: int
    kept: int
    rejected: int
    reject_reasons: dict[str, int]
    sha256: str
    build_ts: str
    nppa_total: int = 0
    nppa_kept: int = 0
    nppa_rejected: int = 0
    nppa_reject_reasons: dict[str, int] | None = None
    nppa_all_sha256: str = ""
    nppa_special_sha256: str = ""
    nppa_duplicate_count: int = 0
    nppa_unique_formulation_keys: int = 0


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def build_db(
    source_path: str | Path,
    db_path: str | Path,
    rejects_path: str | Path | None = None,
    *,
    fetch_live: bool = False,
    nppa_all_path: str | Path = NPPA_ALL_DEFAULT,
    nppa_special_path: str | Path = NPPA_SPECIAL_DEFAULT,
    nppa_rejects_path: str | Path | None = None,
    include_jap: bool = True,
    include_nppa: bool = True,
) -> BuildStats:
    """Build data/processed-style SQLite DB from the seed file or live fetch.

    Default: full rebuild of JAP + NPPA tables with fresh meta. Partial
    builds (--jap-only / --nppa-only) only touch their own table and upsert
    their own meta keys, leaving the other side undisturbed.
    """
    if rejects_path is None:
        rejects_path = Path("data/interim/jap_rejects.csv")
    if nppa_rejects_path is None:
        nppa_rejects_path = Path("data/interim/nppa_rejects.csv")
    source_path = Path(source_path)
    db_path = Path(db_path)
    rejects_path = Path(rejects_path)
    nppa_all_path = Path(nppa_all_path)
    nppa_special_path = Path(nppa_special_path)
    nppa_rejects_path = Path(nppa_rejects_path)
    build_ts = datetime.now(UTC).isoformat(timespec="seconds")

    stats = BuildStats(
        total=0,
        kept=0,
        rejected=0,
        reject_reasons={},
        sha256="",
        build_ts=build_ts,
        nppa_reject_reasons={},
    )

    jap_rows: list = []
    jap_meta: list[tuple[str, str]] = []
    if include_jap:
        if fetch_live:
            data = fetch_jap_products()
            raw_bytes = json.dumps(data, sort_keys=True).encode("utf-8")
            source_label = f"live:{JAP_PRODUCTS_URL}"
        else:
            raw_bytes = source_path.read_bytes()
            data = json.loads(raw_bytes)
            source_label = str(source_path)
        sha256 = _sha256_bytes(raw_bytes)
        kept, rejected = parse_jap_products(data)
        reasons = dict(Counter(r.reason for r in rejected))
        jap_rows = [
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
        ]
        jap_meta = [
            ("build_ts", build_ts),
            ("source_file", source_label),
            ("source_sha256", sha256),
            ("total_rows", str(len(kept) + len(rejected))),
            ("kept_rows", str(len(kept))),
            ("rejected_rows", str(len(rejected))),
        ]
        rejects_path.parent.mkdir(parents=True, exist_ok=True)
        with open(rejects_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["product_id", "reason"])
            for r in rejected:
                writer.writerow(["" if r.product_id is None else r.product_id, r.reason])
        stats.total = len(kept) + len(rejected)
        stats.kept = len(kept)
        stats.rejected = len(rejected)
        stats.reject_reasons = reasons
        stats.sha256 = sha256

    nppa_rows: list = []
    nppa_meta: list[tuple[str, str]] = []
    nppa_reject_rows: list[tuple[str, int | None, str]] = []
    if include_nppa:
        nppa_kept_all: list = []
        nppa_rejected_all: list = []
        per_file: list[tuple[str, Path, list, list, str]] = []
        for label, csv_path in (("nppa_all", nppa_all_path), ("nppa_special", nppa_special_path)):
            file_bytes = csv_path.read_bytes()
            file_sha = _sha256_bytes(file_bytes)
            kept_n, rejected_n = parse_nppa_csv(csv_path, label)
            per_file.append((label, csv_path, kept_n, rejected_n, file_sha))
            nppa_kept_all.extend(kept_n)
            nppa_rejected_all.extend(rejected_n)
        reasons_n = dict(Counter(r.reason for r in nppa_rejected_all))
        all_by_file = {label: (k, r, s) for label, _, k, r, s in per_file}
        all_kept, all_rej = all_by_file["nppa_all"][0], all_by_file["nppa_all"][1]
        spl_kept, spl_rej = all_by_file["nppa_special"][0], all_by_file["nppa_special"][1]
        nppa_meta = [
            ("nppa_all_rows", str(len(all_kept) + len(all_rej))),
            ("nppa_all_kept", str(len(all_kept))),
            ("nppa_all_rejected", str(len(all_rej))),
            ("nppa_all_sha256", all_by_file["nppa_all"][2]),
            ("nppa_special_rows", str(len(spl_kept) + len(spl_rej))),
            ("nppa_special_kept", str(len(spl_kept))),
            ("nppa_special_rejected", str(len(spl_rej))),
            ("nppa_special_sha256", all_by_file["nppa_special"][2]),
            ("nppa_duplicate_count", str(reasons_n.get("duplicate_row", 0))),
            (
                "nppa_unique_formulation_keys",
                str(len({r.formulation_raw for r in nppa_kept_all})),
            ),
        ]
        nppa_rows = [
            (
                r.sl_no,
                r.nlem_version,
                r.source_file,
                r.formulation_raw,
                r.form_raw,
                r.strength_raw,
                r.so_number,
                r.so_date,
                r.price_value,
                r.unit_type,
                r.unit_count,
                r.pack_volume_ml,
                r.container,
                r.pack_condition_raw,
                r.qualifier_raw,
                r.source_row,
            )
            for r in nppa_kept_all
        ]
        for (label, _, _, rejected_n, _) in per_file:
            for r in rejected_n:
                nppa_reject_rows.append((label, r.identifier, r.reason))
        nppa_rejects_path.parent.mkdir(parents=True, exist_ok=True)
        with open(nppa_rejects_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["source_file", "sl_no", "reason"])
            for label, ident, reason in nppa_reject_rows:
                writer.writerow([label, "" if ident is None else ident, reason])
        stats.nppa_total = len(nppa_kept_all) + len(nppa_rejected_all)
        stats.nppa_kept = len(nppa_kept_all)
        stats.nppa_rejected = len(nppa_rejected_all)
        stats.nppa_reject_reasons = reasons_n
        stats.nppa_all_sha256 = all_by_file["nppa_all"][2]
        stats.nppa_special_sha256 = all_by_file["nppa_special"][2]
        stats.nppa_duplicate_count = reasons_n.get("duplicate_row", 0)
        stats.nppa_unique_formulation_keys = len({r.formulation_raw for r in nppa_kept_all})

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        if include_jap and include_nppa:
            conn.executescript(SCHEMA)
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if include_jap:
                conn.executescript(SCHEMA_JAP)
            if include_nppa:
                conn.executescript(SCHEMA_NPPA)
        if include_jap:
            conn.executemany(
                "INSERT INTO jap_products(product_id, drug_code, generic_name,"
                " group_name, unit_size, mrp, status, ingested_at, source_sha256)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                jap_rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                jap_meta,
            )
        if include_nppa:
            conn.executemany(
                "INSERT INTO nppa_ceiling_prices(sl_no, nlem_version, source_file,"
                " formulation_raw, form_raw, strength_raw, so_number, so_date,"
                " price_value, unit_type, unit_count, pack_volume_ml, container,"
                " pack_condition_raw, qualifier_raw, source_row)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                nppa_rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                nppa_meta,
            )

    return stats


def main(argv: list[str] | None = None) -> BuildStats:
    parser = argparse.ArgumentParser(description="Build the JAP + NPPA raw SQLite tables.")
    parser.add_argument("--source", default="data/raw/jap_products.json")
    parser.add_argument("--db", default="data/processed/drugs.db")
    parser.add_argument("--rejects", default="data/interim/jap_rejects.csv")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="live-fetch via download.py instead of reading --source",
    )
    parser.add_argument("--nppa-all", default=NPPA_ALL_DEFAULT)
    parser.add_argument("--nppa-special", default=NPPA_SPECIAL_DEFAULT)
    parser.add_argument("--nppa-rejects", default="data/interim/nppa_rejects.csv")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--jap-only",
        action="store_true",
        help="rebuild only the JAP table, leaving NPPA data undisturbed",
    )
    scope.add_argument(
        "--nppa-only",
        action="store_true",
        help="rebuild only the NPPA table, leaving JAP data undisturbed",
    )
    args = parser.parse_args(argv)
    stats = build_db(
        args.source,
        args.db,
        args.rejects,
        fetch_live=args.fetch,
        nppa_all_path=args.nppa_all,
        nppa_special_path=args.nppa_special,
        nppa_rejects_path=args.nppa_rejects,
        include_jap=not args.nppa_only,
        include_nppa=not args.jap_only,
    )
    print(f"total={stats.total} kept={stats.kept} rejected={stats.rejected}")
    print(f"reasons={stats.reject_reasons}")
    print(f"sha256={stats.sha256}")
    print(
        f"nppa_total={stats.nppa_total} nppa_kept={stats.nppa_kept}"
        f" nppa_rejected={stats.nppa_rejected}"
    )
    print(f"nppa_reasons={stats.nppa_reject_reasons}")
    print(f"nppa_all_sha256={stats.nppa_all_sha256}")
    print(f"nppa_special_sha256={stats.nppa_special_sha256}")
    print(f"nppa_duplicates={stats.nppa_duplicate_count}")
    print(f"nppa_unique_formulations={stats.nppa_unique_formulation_keys}")
    print(f"build_ts={stats.build_ts}")
    return stats


if __name__ == "__main__":
    main()
