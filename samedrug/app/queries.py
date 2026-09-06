"""Read-only query layer over data/processed/drugs.db (Phase 4).

All SQL lives here; FastAPI handlers in samedrug.app.main stay thin.
Every connection is opened with ``mode=ro`` URI so the app can never
write the build artifact (a write attempt raises sqlite3.OperationalError).
Only /api/stats results are cached, in-process, at startup/first-call;
a DB rebuild therefore requires an app restart (see /health note).

Search tier design (molecule-boundary safety):
  exact     - query equals the full molecule_set or one molecule token,
              directly or via the pipeline alias table (alias-keyed exact,
              e.g. acetylsalicylic acid -> aspirin).
  prefix    - query is a strict prefix of a molecule token.
  substring - query is a substring of a molecule token, or of a JAP/NPPA
              source row directly linked to this key (fallback substrings).
  fuzzy     - rapidfuzz partial_ratio >= 80, always last, never mixed above.

Boundary rule: a non-fuzzy tier is only assigned when the query is
token-contained in the canonical molecule_set, or text-contained in a
linked source row (JAP generic_name / NPPA formulation_raw). A query for
molecule X therefore never silently returns a key whose molecules and
linked rows have nothing to do with X (e.g. "dolo" never returns
paracetamol; it matches dolutegravir only as an honestly labeled prefix
because the token genuinely starts with "dolo").
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import UTC, datetime
from pathlib import Path

from rapidfuzz import fuzz

from samedrug.pipeline.normalize import canonical_molecules, fold

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "drugs.db"

FUZZY_CUTOFF = 80.0

# GST-rate factor hint window for the tax-basis caveat (see build_caveats).
# Ceilings are tax-exclusive, JAP MRPs tax-inclusive; a JAP-above-ceiling
# ratio inside this window *may* be a tax artifact (measured case:
# dexamethasone 2.815/2.605 = 1.0806). It is a hint, never a claim.
_TAX_RATIO_LO = 1.05
_TAX_RATIO_HI = 1.15

# jap_pack_parsed kind -> equivalents nppa_unit_basis class.
_JAP_KIND_TO_BASIS = {
    "count": "count",
    "volume_ml": "ml",
    "dose_count": "dose",
    "weight_g": "g",
    "vial": "vial",
}

_stats_cache: dict[str, dict] = {}


def utcnow() -> str:
    """ISO-8601 UTC timestamp for response envelopes."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect_ro(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a strictly read-only SQLite connection (mode=ro URI)."""
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_data_as_on(conn: sqlite3.Connection) -> str | None:
    """Freshness marker: equivalents.computed_at (all rows share one value)."""
    row = conn.execute(
        "SELECT computed_at FROM equivalents LIMIT 1"
    ).fetchone()
    if row is not None:
        return row["computed_at"]
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'match_computed_at'"
    ).fetchone()
    return row["value"] if row is not None else None


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """One COUNT(*) query per table (used by /health)."""
    out: dict[str, int] = {}
    for table in (
        "jap_products",
        "nppa_ceiling_prices",
        "canonical_formulations",
        "equivalents",
    ):
        (out[table],) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    return out


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _alias_resolved(query: str) -> str | None:
    """Alias-table resolution of a raw query (None when unchanged/unusable)."""
    try:
        resolved = canonical_molecules([query], use_alias=True)
    except Exception:
        return None
    if not resolved:
        return None
    out = resolved[0].strip().casefold()
    if not out or out == query:
        return None
    return out


def _equiv_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT match_key, COUNT(*) AS n FROM equivalents GROUP BY match_key"
    ).fetchall()
    return {r["match_key"]: r["n"] for r in rows}


def search(
    conn: sqlite3.Connection, query: str, limit: int = 20
) -> list[dict]:
    """Tiered, molecule-safe search over canonical keys + fallback rows."""
    q = fold(query).strip()
    alias_q = _alias_resolved(q)
    like_q = _escape_like(q)
    patterns = [f"%{like_q}%"]
    params: list[str] = [patterns[0]]
    or_clauses = ["LOWER(molecule_set) LIKE ? ESCAPE '\\'"]
    if alias_q:
        or_clauses.append("LOWER(molecule_set) LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(alias_q)}%")
    cands = conn.execute(
        "SELECT match_key, molecule_set, strength_set, form, form_family"
        f" FROM canonical_formulations WHERE {' OR '.join(or_clauses)}",
        params,
    ).fetchall()
    cand_keys = {r["match_key"] for r in cands}

    # Fallback substrings: JAP generic_name / NPPA formulation_raw hits,
    # resolved to canonical keys via the stored id lists (provenance, not
    # silent substitution).
    jap_hits = {
        r["product_id"]
        for r in conn.execute(
            "SELECT product_id FROM jap_products"
            " WHERE LOWER(generic_name) LIKE ? ESCAPE '\\'",
            [f"%{like_q}%"],
        ).fetchall()
    }
    nppa_hits = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM nppa_ceiling_prices"
            " WHERE LOWER(formulation_raw) LIKE ? ESCAPE '\\'",
            [f"%{like_q}%"],
        ).fetchall()
    }
    fallback_keys: set[str] = set()
    if jap_hits or nppa_hits:
        for r in conn.execute(
            "SELECT match_key, nppa_ids, jap_product_ids"
            " FROM canonical_formulations"
        ).fetchall():
            try:
                n_ids = set(json.loads(r["nppa_ids"] or "[]"))
                j_ids = set(json.loads(r["jap_product_ids"] or "[]"))
            except (ValueError, TypeError):
                continue
            if (n_ids & nppa_hits) or (j_ids & jap_hits):
                fallback_keys.add(r["match_key"])
    if fallback_keys - cand_keys:
        placeholders = ",".join("?" * len(fallback_keys - cand_keys))
        extra = conn.execute(
            "SELECT match_key, molecule_set, strength_set, form, form_family"
            f" FROM canonical_formulations WHERE match_key IN ({placeholders})",
            sorted(fallback_keys - cand_keys),
        ).fetchall()
        cands = list(cands) + list(extra)
        cand_keys |= fallback_keys

    by_key = {r["match_key"]: r for r in cands}
    counts = _equiv_counts(conn)
    scored: list[tuple[tuple, dict]] = []
    for key, r in by_key.items():
        tokens = [t for t in (r["molecule_set"] or "").casefold().split("+") if t]
        tier: str | None = None
        if q == (r["molecule_set"] or "").casefold() or q in tokens:
            tier = "exact"
        elif alias_q and (alias_q == (r["molecule_set"] or "").casefold() or alias_q in tokens):
            tier = "exact"
        elif any(t.startswith(q) for t in tokens):
            tier = "prefix"
        elif any(q in t for t in tokens):
            tier = "substring"
        elif alias_q and any(alias_q in t for t in tokens):
            tier = "substring"
        elif key in fallback_keys:
            tier = "substring"
        if tier is None:
            continue
        n_eq = counts.get(key, 0)
        order = ({"exact": 0, "prefix": 1, "substring": 2}[tier], -(n_eq > 0), key)
        scored.append(
            (
                order,
                {
                    "match_key": key,
                    "molecules": r["molecule_set"],
                    "strength_set": r["strength_set"],
                    "form": r["form"],
                    "form_family": r["form_family"],
                    "has_equivalents": n_eq > 0,
                    "n_equivalents": n_eq,
                    "tier": tier,
                },
            )
        )
    scored.sort(key=lambda t: t[0])
    seen = {item["match_key"] for _, item in scored}

    # Fuzzy tier: rapidfuzz partial_ratio >= 80 over the full scan, always last.
    fuzzy: list[tuple[tuple, dict]] = []
    for r in conn.execute(
        "SELECT match_key, molecule_set, strength_set, form, form_family"
        " FROM canonical_formulations"
    ).fetchall():
        if r["match_key"] in seen:
            continue
        mol = (r["molecule_set"] or "").casefold()
        if not mol:
            continue
        best = fuzz.partial_ratio(q, mol)
        for t in mol.split("+"):
            if t:
                s = fuzz.partial_ratio(q, t)
                if s > best:
                    best = s
        if best >= FUZZY_CUTOFF:
            n_eq = counts.get(r["match_key"], 0)
            fuzzy.append(
                (
                    (-best, -(n_eq > 0), r["match_key"]),
                    {
                        "match_key": r["match_key"],
                        "molecules": r["molecule_set"],
                        "strength_set": r["strength_set"],
                        "form": r["form"],
                        "form_family": r["form_family"],
                        "has_equivalents": n_eq > 0,
                        "n_equivalents": n_eq,
                        "tier": "fuzzy",
                    },
                )
            )
    fuzzy.sort(key=lambda t: t[0])
    scored.extend(fuzzy)
    return [item for _, item in scored[: max(limit, 0)]]


def get_equivalent(conn: sqlite3.Connection, match_key: str) -> dict | None:
    """Full provenance payload for one canonical key (cheapest row wins)."""
    equiv = conn.execute(
        "SELECT * FROM equivalents WHERE match_key = ?"
        " ORDER BY savings_pct DESC, id LIMIT 1",
        (match_key,),
    ).fetchone()
    if equiv is None:
        return None
    canon = conn.execute(
        "SELECT * FROM canonical_formulations WHERE match_key = ?",
        (match_key,),
    ).fetchone()
    nppa = conn.execute(
        "SELECT * FROM nppa_ceiling_prices WHERE id = ?",
        (equiv["nppa_row_id"],),
    ).fetchone()
    jap = conn.execute(
        "SELECT * FROM jap_products WHERE product_id = ?",
        (equiv["jap_product_id"],),
    ).fetchone()
    if canon is None or nppa is None or jap is None:
        return None
    return {
        "match_key": match_key,
        "molecules": canon["molecule_set"],
        "strength_set": canon["strength_set"],
        "form": canon["form"],
        "form_family": canon["form_family"],
        "modifiers": canon["modifiers"],
        "nppa_side": {
            "ceiling_per_unit_inr": equiv["nppa_ceiling_per_unit"],
            "unit_basis": equiv["nppa_unit_basis"],
            "variant_basis": equiv["nppa_variant_basis"],
            "nlem_version": equiv["nppa_nlem_version"],
            "so_number": nppa["so_number"],
            "so_date": nppa["so_date"],
            "formulation_raw": nppa["formulation_raw"],
            "strength_raw": nppa["strength_raw"],
            "qualifier_raw": nppa["qualifier_raw"],
            "source_file": nppa["source_file"],
            "source_row": nppa["source_row"],
        },
        "jap_side": {
            "product_id": jap["product_id"],
            "generic_name": jap["generic_name"],
            "pack_raw": jap["unit_size"],
            "pack_parsed": equiv["jap_pack_parsed"],
            "mrp": jap["mrp"],
            "per_unit_inr": equiv["jap_per_unit"],
            "drug_code": jap["drug_code"],
            "group_name": jap["group_name"],
        },
        "savings_pct": equiv["savings_pct"],
        "match_method": equiv["match_method"],
        "confidence": equiv["confidence"],
        "caveats": build_caveats(
            equiv["match_method"],
            equiv["confidence"],
            equiv["savings_pct"],
            equiv["jap_per_unit"],
            equiv["nppa_ceiling_per_unit"],
        ),
    }


def build_caveats(
    match_method: str | None,
    confidence: str | None,
    savings_pct: float | None,
    jap_per_unit: float | None = None,
    nppa_per_unit: float | None = None,
) -> list[str]:
    """Honesty layer derived from real row state (pure function)."""
    caveats: list[str] = []
    if match_method in ("modifier_relaxed", "form_family"):
        caveats.append("form_or_modifier_relaxed: verify dosage form")
    if confidence == "low":
        caveats.append("low_match_confidence")
    if savings_pct is not None and savings_pct < 0:
        caveats.append("jap_price_above_ceiling")
        if jap_per_unit and nppa_per_unit:
            ratio = jap_per_unit / nppa_per_unit
            if _TAX_RATIO_LO <= ratio <= _TAX_RATIO_HI:
                caveats.append("possible_tax_basis_difference")
    return caveats


def get_jap_product(conn: sqlite3.Connection, product_id: int) -> dict | None:
    """Single JAP product + canonical match + equivalence summary (if any)."""
    jap = conn.execute(
        "SELECT * FROM jap_products WHERE product_id = ?", (product_id,)
    ).fetchone()
    if jap is None:
        return None
    match_key: str | None = None
    for r in conn.execute(
        "SELECT match_key, jap_product_ids FROM canonical_formulations"
    ).fetchall():
        try:
            ids = json.loads(r["jap_product_ids"] or "[]")
        except (ValueError, TypeError):
            continue
        if product_id in ids:
            match_key = r["match_key"]
            break
    equiv_rows = conn.execute(
        "SELECT match_key, nppa_ceiling_per_unit, jap_per_unit, savings_pct,"
        " match_method, confidence FROM equivalents"
        " WHERE jap_product_id = ? ORDER BY savings_pct DESC",
        (product_id,),
    ).fetchall()
    payload: dict = {
        "product_id": jap["product_id"],
        "drug_code": jap["drug_code"],
        "generic_name": jap["generic_name"],
        "group_name": jap["group_name"],
        "pack_raw": jap["unit_size"],
        "mrp": jap["mrp"],
        "status": jap["status"],
        "match_key": match_key,
        "equivalents": [dict(r) for r in equiv_rows],
    }
    if (jap["mrp"] or 0) == 0:
        # Zero-MRP = price not yet published (sources.md addendum), never
        # silently omitted and never part of any equivalence payload.
        payload["excluded_reason"] = "price_not_published"
    return payload


def unit_basis_aligned(nppa_unit_basis: str, jap_pack_parsed: str) -> bool:
    """Equivalents rows are pre-aligned; the gate test asserts this holds."""
    kind = (jap_pack_parsed or "").split(":", 1)[0]
    return _JAP_KIND_TO_BASIS.get(kind) == nppa_unit_basis


def compute_stats(conn: sqlite3.Connection) -> dict:
    """All stats measured via SQL (+ median in Python)."""
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    (equivalents_count,) = conn.execute(
        "SELECT COUNT(*) FROM equivalents"
    ).fetchone()
    method_rows = conn.execute(
        "SELECT match_method, COUNT(*) AS n FROM equivalents"
        " GROUP BY match_method ORDER BY match_method"
    ).fetchall()
    method_distribution = {r["match_method"]: r["n"] for r in method_rows}
    agg = conn.execute(
        "SELECT MIN(savings_pct) AS mn, MAX(savings_pct) AS mx"
        " FROM equivalents"
    ).fetchone()
    (negative_count,) = conn.execute(
        "SELECT COUNT(*) FROM equivalents WHERE savings_pct < 0"
    ).fetchone()
    savings = [
        r[0] for r in conn.execute("SELECT savings_pct FROM equivalents").fetchall()
    ]
    matched_keys = int(meta.get("matched_nppa_keys", "0"))
    total_nppa_keys = int(meta.get("nppa_formulation_keys", "0"))
    coverage_pct = (
        round(100.0 * matched_keys / total_nppa_keys, 1) if total_nppa_keys else 0.0
    )
    return {
        "match_ladder": {
            "stage_1_exact": int(meta.get("match_stage_1_count", "0")),
            "stage_2_alias": int(meta.get("match_stage_2_count", "0")),
            "stage_3_fuzzy": int(meta.get("match_stage_3_count", "0")),
            "form_family": int(meta.get("form_family_count", "0")),
            "matched_keys": matched_keys,
            "total_nppa_keys": total_nppa_keys,
            "coverage_pct": coverage_pct,
        },
        "equivalents_count": equivalents_count,
        "method_distribution": method_distribution,
        "savings": {
            "median": statistics.median(savings) if savings else None,
            "min": agg["mn"],
            "max": agg["mx"],
        },
        "negative_count": negative_count,
        "data_as_on": get_data_as_on(conn),
    }


def get_stats(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Process-start-cached stats (a DB rebuild requires an app restart)."""
    key = str(Path(db_path).resolve())
    if key not in _stats_cache:
        with connect_ro(db_path) as conn:
            _stats_cache[key] = compute_stats(conn)
    return _stats_cache[key]


def stats_cache_populated(db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    """Whether /api/stats is currently served from the in-process cache."""
    return str(Path(db_path).resolve()) in _stats_cache
