"""Staged NPPA <-> JAP matcher and equivalence computation (Phase 3).

Reads the frozen RAW tables (jap_products, nppa_ceiling_prices) from
data/processed/drugs.db and writes three additive tables:

- canonical_formulations: one row per canonical match key (matched keys
  carry nppa_ids + jap_product_ids + method + confidence; unmatched keys
  carry method NULL / confidence "unmatched" for backlog triage).
- equivalents: per-unit aligned price comparisons, every row traceable to
  (nppa_row_id, jap_product_id). JAP mrp=0 rows NEVER appear here.
- meta: the match ladder + exclusion/unmatched counts.

Stages: 0 naive baseline (report only) -> 1 exact post-normalization ->
2 alias -> 3 conservative fuzzy (single molecules, ratio >= 0.92, strength
+ form exact) -> form-family fallback. Modifier mismatches downgrade to
modifier_relaxed. Combos match only as exact canonical sets; fuzzy never
crosses molecule counts; different strengths never match.

CLI: python -m samedrug.pipeline.match [--db PATH] [--report]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rapidfuzz import fuzz

from samedrug.pipeline.normalize import (
    canonical_form,
    canonical_molecules,
    extract_jap_composition,
    extract_modifiers,
    fold,
    form_family,
    make_key,
    parse_pack,
    parse_strength_parts,
    split_nppa_molecules,
)

DB_DEFAULT = "data/processed/drugs.db"

SCHEMA_CANONICAL = """
DROP TABLE IF EXISTS canonical_formulations;
CREATE TABLE canonical_formulations(
    match_key TEXT PRIMARY KEY,
    molecule_set TEXT NOT NULL,
    strength_set TEXT NOT NULL,
    form TEXT NOT NULL,
    form_family TEXT NOT NULL,
    modifiers TEXT NOT NULL,
    nppa_ids TEXT NOT NULL,
    jap_product_ids TEXT NOT NULL,
    match_method TEXT,
    confidence TEXT NOT NULL
);
"""

SCHEMA_EQUIVALENTS = """
DROP TABLE IF EXISTS equivalents;
CREATE TABLE equivalents(
    id INTEGER PRIMARY KEY,
    match_key TEXT NOT NULL,
    nppa_row_id INTEGER NOT NULL,
    jap_product_id INTEGER NOT NULL,
    nppa_ceiling_per_unit REAL NOT NULL,
    nppa_unit_basis TEXT NOT NULL,
    nppa_variant_basis TEXT NOT NULL,
    jap_per_unit REAL NOT NULL,
    jap_pack_parsed TEXT NOT NULL,
    savings_pct REAL NOT NULL,
    match_method TEXT NOT NULL,
    confidence TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equiv_key ON equivalents(match_key);
CREATE INDEX IF NOT EXISTS idx_equiv_jap ON equivalents(jap_product_id);
CREATE INDEX IF NOT EXISTS idx_equiv_nppa ON equivalents(nppa_row_id);
"""

_COUNT_TYPES = frozenset(
    {
        "tablet", "capsule", "condom", "suppository", "lozenge", "pastille",
        "pessary", "gum", "iud", "unit",
    }
)

_FUZZY_AUTO = 92.0
_FUZZY_REVIEW_LO = 85.0


def _pct_to_mgml(strength: str) -> str | None:
    """Convert a single %wv strength to its exact mg/ml twin ("5%wv"->"50mg/ml").

    w/v percent is grams per 100 ml, so mg/ml = value * 10 — mathematically
    exact, never an estimate. %ww/%vv never convert (density-dependent).
    """
    m = re.fullmatch(r"(.+?)%wv", strength)
    if not m:
        return None
    try:
        return f"{_fmt(float(m.group(1)) * 10.0)}mg/ml"
    except ValueError:
        return None


def _fmt(x: float) -> str:
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    s = f"{x:.6g}"
    if "e" in s or "E" in s:
        s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s


def _conv_pairs(pairs: tuple[str, ...]) -> tuple[str, ...] | None:
    """Alternate pairs with %wv strengths converted to mg/ml; None if N/A."""
    if not any(p.endswith("%wv") for p in pairs):
        return None
    out = []
    for pair in pairs:
        mol, _, strength = pair.partition(":")
        conv = _pct_to_mgml(strength)
        out.append(f"{mol}:{conv}" if conv else pair)
    return tuple(sorted(out))


# ---------------------------------------------------------------------------
# Canonical records
# ---------------------------------------------------------------------------


@dataclass
class JapCanon:
    product_id: int
    generic_name: str
    mrp: float
    unit_size: str | None
    molecules_plain: list[str]
    molecules_alias: list[str]
    strengths: list[str | None]  # canonical bound strengths (None = unknown)
    form: str
    family: str
    modifiers: tuple[str, ...]
    pack_kind: str | None
    pack_value: float | None
    ok: bool
    reason: str | None
    low_confidence: bool = False
    key_plain: str = ""
    key_alias: str = ""
    conv_plain: str | None = None
    conv_alias: str | None = None


@dataclass
class NppaCanon:
    row_id: int
    formulation_raw: str
    molecules_plain: list[str]
    molecules_alias: list[str]
    strengths: list[str]  # positional canonical ("" = unknown)
    form: str
    family: str
    modifiers: tuple[str, ...]
    unit_type: str | None
    unit_count: float | None
    pack_volume_ml: float | None
    price_value: float
    ok: bool
    reason: str | None
    key_plain: str = ""
    key_alias: str = ""
    conv_plain: str | None = None
    conv_alias: str | None = None
    low_confidence: bool = False


def _finalize_keys(
    mols_plain: list[str],
    mols_alias: list[str],
    strengths: list[str | None],
    form: str,
) -> tuple[str, str, str | None, str | None]:
    kp = make_key(mols_plain, strengths, form).key()
    ka = make_key(mols_alias, strengths, form).key()
    pairs_p = tuple(sorted(
        f"{m}:{s or ''}" for m, s in zip(mols_plain, strengths, strict=True)))
    pairs_a = tuple(sorted(
        f"{m}:{s or ''}" for m, s in zip(mols_alias, strengths, strict=True)))
    cp = _conv_pairs(pairs_p)
    ca = _conv_pairs(pairs_a)
    return (
        kp,
        ka,
        "+".join(cp) + "|" + form if cp else None,
        "+".join(ca) + "|" + form if ca else None,
    )


def build_jap_canonical(
    rows: list[tuple[int, str, str | None, float]],
) -> tuple[list[JapCanon], Counter]:
    """Canonicalize JAP rows; returns (records, exclusion-reason counts)."""
    records: list[JapCanon] = []
    excluded: Counter = Counter()
    for product_id, generic_name, unit_size, mrp in rows:
        comp = extract_jap_composition(generic_name)
        pack, pack_reason = parse_pack(unit_size)
        if not comp.ok:
            excluded[comp.reason or "unknown"] += 1
            records.append(
                JapCanon(product_id, generic_name, mrp, unit_size, [], [],
                         [], "unknown", "unknown", (), None, None,
                         False, comp.reason, comp.low_confidence)
            )
            continue
        form = canonical_form(comp.form_raw)
        mods = tuple(sorted(set(comp.modifiers)
                            | set(extract_modifiers(generic_name))))
        strengths = list(comp.strengths)
        mols_plain = canonical_molecules(list(comp.molecules), use_alias=False)
        mols_alias = canonical_molecules(list(comp.molecules), use_alias=True)
        kp, ka, cp, ca = _finalize_keys(mols_plain, mols_alias, strengths, form)
        if pack is None:
            excluded[f"pack:{pack_reason}"] += 1
        records.append(
            JapCanon(product_id, generic_name, mrp, unit_size, mols_plain,
                     mols_alias, strengths, form, form_family(form), mods,
                     pack.kind if pack else None,
                     pack.value if pack else None, True, None,
                     comp.low_confidence, kp, ka, cp, ca)
        )
    return records, excluded


def _bind_nppa_strengths(
    molecules: list[str], strength_raw: str | None
) -> tuple[list[str] | None, str | None]:
    """Bind positional canonical strengths to NPPA molecules.

    Single molecule + several parts (heavy Bupivacaine/Lidocaine with
    glucose) binds the combined sorted set to the one molecule. Anything
    else with mismatched counts is excluded (never silently swapped).
    """
    parts, reason = parse_strength_parts(strength_raw)
    if parts is None:
        if reason == "absent_strength":
            return ["" for _ in molecules], None
        return None, reason
    if len(parts) == len(molecules):
        return parts, None
    if len(molecules) == 1:
        return ["+".join(sorted(parts))], None
    return None, "molecule_strength_count_mismatch"


def build_nppa_canonical(
    rows: list[tuple],
) -> tuple[list[NppaCanon], Counter]:
    """Canonicalize NPPA rows; returns (records, exclusion-reason counts)."""
    records: list[NppaCanon] = []
    excluded: Counter = Counter()
    for r in rows:
        (row_id, formulation_raw, form_raw, strength_raw, unit_type,
         unit_count, pack_volume_ml, price_value) = r
        # Strength count is needed for conditional hyphen widening
        # ("Sulphadoxine -Pyrimethamine" splits only when 3 parts demand it).
        probe, _ = parse_strength_parts(strength_raw)
        n_probe = len(probe) if probe else None
        raw_mols = split_nppa_molecules(formulation_raw, n_probe)
        if not raw_mols:
            excluded["unparseable_molecule"] += 1
            records.append(
                NppaCanon(row_id, formulation_raw, [], [], [], "unknown",
                          "unknown", (), unit_type, unit_count,
                          pack_volume_ml, price_value, False,
                          "unparseable_molecule")
            )
            continue
        bound, reason = _bind_nppa_strengths(raw_mols, strength_raw)
        if bound is None:
            excluded[reason or "unknown"] += 1
            records.append(
                NppaCanon(row_id, formulation_raw, [], [], [], "unknown",
                          "unknown", (), unit_type, unit_count,
                          pack_volume_ml, price_value, False, reason)
            )
            continue
        form = canonical_form(form_raw)
        mods = extract_modifiers(
            f"{form_raw or ''} {formulation_raw or ''} {strength_raw or ''}"
        )
        mols_plain = canonical_molecules(raw_mols, use_alias=False)
        mols_alias = canonical_molecules(raw_mols, use_alias=True)
        kp, ka, cp, ca = _finalize_keys(mols_plain, mols_alias, bound, form)
        records.append(
            NppaCanon(row_id, formulation_raw, mols_plain, mols_alias, bound,
                      form, form_family(form), mods, unit_type, unit_count,
                      pack_volume_ml, price_value, True, None, kp, ka, cp, ca)
        )
    return records, excluded


# ---------------------------------------------------------------------------
# Staged matcher
# ---------------------------------------------------------------------------


@dataclass
class Match:
    match_key: str
    molecule_set: str
    strength_set: str
    form: str
    family: str
    modifiers: tuple[str, ...]
    nppa_ids: list[int] = field(default_factory=list)
    jap_ids: list[int] = field(default_factory=list)
    method: str | None = None
    confidence: str = "unmatched"
    via_conversion: bool = False


def _modifiers_equal(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return set(a) == set(b)


def _confidence_for(nppa_ok_low: bool, strengths: list[str]) -> str:
    if nppa_ok_low:
        return "medium"
    if any(s == "" for s in strengths):
        return "medium"
    return "high"


def run_match(
    jap: list[JapCanon], nppa: list[NppaCanon]
) -> tuple[dict[str, Match], dict]:
    """Stage-gated match. Returns (matches by key, stats dict)."""
    stats: dict = {
        "stage_keys": defaultdict(set),
        "modifier_relaxed_count": 0,
        "pct_conversion_count": 0,
        "slash_mismatch_count": 0,
        "fuzzy_review": [],
    }
    matches: dict[str, Match] = {}
    matched_nppa: set[int] = set()
    matched_jap: set[int] = set()

    jap_ok = [j for j in jap if j.ok]
    nppa_ok = [n for n in nppa if n.ok]

    def _register(nrec: NppaCanon, jrec: JapCanon, method: str,
                  stage: str, via_conv: bool = False) -> None:
        # The match dict is ALWAYS keyed by alias key: for exact matches the
        # alias keys agree whenever the plain keys do (same function), and
        # the canonical table is alias-keyed. Keying exact matches by plain
        # key orphaned them (JAP 1709 vanished from canon pre-fix).
        key = nrec.key_alias
        stats["stage_keys"][stage].add(key)
        m = matches.get(key)
        if m is None:
            mods = tuple(sorted(set(nrec.modifiers) | set(jrec.modifiers)))
            m = matches[key] = Match(
                match_key=key,
                molecule_set="+".join(sorted(nrec.molecules_alias)),
                strength_set="+".join(sorted(nrec.strengths)),
                form=nrec.form,
                family=nrec.family,
                modifiers=mods,
            )
        if nrec.row_id not in m.nppa_ids:
            m.nppa_ids.append(nrec.row_id)
        if jrec.product_id not in m.jap_ids:
            m.jap_ids.append(jrec.product_id)
        downgraded = not _modifiers_equal(nrec.modifiers, jrec.modifiers)
        if method == "fuzzy":
            m.method = "fuzzy"
            m.confidence = "low" if (nrec.low_confidence
                                     or jrec.low_confidence) else "medium"
        elif method == "form_family":
            m.method = "form_family"
            m.confidence = "low"
        elif downgraded:
            if m.method != "modifier_relaxed":
                m.method = "modifier_relaxed"
                m.confidence = "low"
                stats["modifier_relaxed_count"] += 1
        elif m.method is None:
            m.method = method
            conf = _confidence_for(nrec.low_confidence
                                   or jrec.low_confidence, nrec.strengths)
            m.confidence = conf
            if via_conv:
                m.confidence = "medium"
        if via_conv and not m.via_conversion:
            m.via_conversion = True
            stats["pct_conversion_count"] += 1

    # Stage 1: exact post-normalization (no aliases).
    jap_plain: dict[str, list[JapCanon]] = defaultdict(list)
    for j in jap_ok:
        jap_plain[j.key_plain].append(j)
        if j.conv_plain:
            jap_plain.setdefault(j.conv_plain, []).append(j)
    for n in nppa_ok:
        hit = jap_plain.get(n.key_plain)
        via = False
        if not hit and n.conv_plain:
            hit = jap_plain.get(n.conv_plain)
            via = hit is not None
        if hit:
            for j in hit:
                _register(n, j, "exact", "stage_1_exact", via_conv=via)
                matched_jap.add(j.product_id)
            matched_nppa.add(n.row_id)

    # Stage 2: alias-resolved exact.
    jap_alias: dict[str, list[JapCanon]] = defaultdict(list)
    for j in jap_ok:
        if j.product_id in matched_jap:
            continue
        jap_alias[j.key_alias].append(j)
        if j.conv_alias and j.conv_alias != j.key_alias:
            jap_alias.setdefault(j.conv_alias, []).append(j)
    for n in nppa_ok:
        if n.row_id in matched_nppa:
            continue
        hit = jap_alias.get(n.key_alias)
        via = False
        if not hit and n.conv_alias:
            hit = jap_alias.get(n.conv_alias)
            via = hit is not None
        if hit:
            for j in hit:
                _register(n, j, "alias", "stage_2_alias", via_conv=via)
                matched_jap.add(j.product_id)
            matched_nppa.add(n.row_id)

    # Stage 3: conservative fuzzy (single molecules, strength+form exact).
    jap_singles: dict[tuple[str, str], list[JapCanon]] = defaultdict(list)
    for j in jap_ok:
        if j.product_id in matched_jap or len(j.molecules_alias) != 1:
            continue
        jap_singles[("+".join(sorted(s or "" for s in j.strengths)),
                       j.form)].append(j)
    for n in nppa_ok:
        if n.row_id in matched_nppa or len(n.molecules_alias) != 1:
            continue
        cands = jap_singles.get(("+".join(sorted(n.strengths)), n.form), [])
        best: JapCanon | None = None
        best_score = 0.0
        for j in cands:
            score = fuzz.ratio(n.molecules_alias[0], j.molecules_alias[0])
            if score > best_score:
                best_score, best = score, j
        if best is not None and best_score >= _FUZZY_AUTO \
                and _modifiers_equal(n.modifiers, best.modifiers):
            _register(n, best, "fuzzy", "stage_3_fuzzy")
            matched_jap.add(best.product_id)
            matched_nppa.add(n.row_id)
        elif best is not None and best_score >= _FUZZY_REVIEW_LO \
                and len(stats["fuzzy_review"]) < 200:
            stats["fuzzy_review"].append(
                (n.formulation_raw, n.row_id, best.generic_name,
                 best.product_id, round(best_score, 1))
            )

    # Form-family fallback (alias-resolved molecules+strengths, same family).
    jap_fam: dict[tuple[str, str, str], list[JapCanon]] = defaultdict(list)
    for j in jap_ok:
        if j.product_id in matched_jap:
            continue
        jap_fam[("+".join(sorted(j.molecules_alias)),
                 "+".join(sorted(s or "" for s in j.strengths)),
                 j.family)].append(j)
    for n in nppa_ok:
        if n.row_id in matched_nppa:
            continue
        cands = jap_fam.get(("+".join(sorted(n.molecules_alias)),
                             "+".join(sorted(n.strengths)), n.family), [])
        cands = [j for j in cands if j.form != n.form]
        if cands:
            for j in sorted(cands, key=lambda r: r.product_id):
                _register(n, j, "form_family", "form_family")
                matched_jap.add(j.product_id)
            matched_nppa.add(n.row_id)

    # Slash-combo positional-mismatch probe (brief section C limitation):
    # sets agree but pairs do not -> counted, never matched.
    jap_sets = {(("+".join(sorted(j.molecules_alias)),
                  "+".join(sorted(s or "" for s in j.strengths))))
                for j in jap_ok}
    for n in nppa_ok:
        if n.row_id in matched_nppa or len(n.molecules_alias) < 2:
            continue
        if ("+" in (n.formulation_raw or "") or "/" in (n.strength_raw or "")
                or "(A)" in (n.formulation_raw or "")):
            key = (("+".join(sorted(n.molecules_alias)),
                    "+".join(sorted(n.strengths))))
            if key in jap_sets and not any(
                    n.key_alias == j.key_alias for j in jap_ok):
                stats["slash_mismatch_count"] += 1

    stats["matched_nppa"] = matched_nppa
    stats["matched_jap"] = matched_jap
    return matches, stats


# ---------------------------------------------------------------------------
# Equivalences (unit-basis aligned)
# ---------------------------------------------------------------------------


def _nppa_basis(rec: NppaCanon) -> tuple[str, float] | None:
    """Return (basis_class, per_unit_price) or None when basis is unknown."""
    price = rec.price_value
    unit, count, vol = rec.unit_type, rec.unit_count, rec.pack_volume_ml
    if unit in _COUNT_TYPES:
        return ("count", price / (count or 1.0))
    if unit == "ml":
        return ("ml", price / (count or 1.0))
    if unit in ("dose", "metered_dose"):
        return ("dose", price)
    if unit == "gm":
        return ("g", price / (count or 1.0))
    if unit == "vial":
        return ("vial", price)
    if unit == "pack" and vol:
        return ("ml", price / vol)
    return None


_JAP_BASIS = {
    "count": "count",
    "volume_ml": "ml",
    "dose_count": "dose",
    "weight_g": "g",
    "vial": "vial",
}


def _select_nppa_variant(
    cands: list[tuple[NppaCanon, str, float]], jap_kind: str,
    jap_volume: float | None,
) -> tuple[NppaCanon, str, float, str]:
    """Pick one NPPA row: exact volume -> same unit type -> per-unit row."""
    scored: list[tuple[int, str, int, tuple]] = []
    for rec, basis, per_unit in cands:
        if jap_volume is not None and rec.pack_volume_ml == jap_volume:
            scored.append((0, "exact_volume", rec.row_id, (rec, basis,
                                                           per_unit)))
        elif ((jap_kind == "count" and basis == "count")
              or (jap_kind == "volume_ml" and rec.unit_type == "ml")
              or (jap_kind == "dose_count" and rec.unit_type == "metered_dose")
              or (jap_kind == "weight_g" and basis == "g")
              or (jap_kind == "vial" and basis == "vial")):
            scored.append((1, "same_unit_type", rec.row_id, (rec, basis,
                                                             per_unit)))
        elif ((jap_kind == "dose_count" and basis == "dose")
              or (jap_kind == "volume_ml" and basis == "ml")):
            scored.append((2, "per_unit_row", rec.row_id, (rec, basis,
                                                           per_unit)))
        else:
            scored.append((3, "fallback_unit", rec.row_id, (rec, basis,
                                                            per_unit)))
    scored.sort(key=lambda t: (t[0], t[2]))
    _, label, _, (rec, basis, per_unit) = scored[0]
    return rec, basis, per_unit, label


def compute_equivalences(
    matches: dict[str, Match],
    jap_by_id: dict[int, JapCanon],
    nppa_by_id: dict[int, NppaCanon],
    computed_at: str,
) -> tuple[list[tuple], Counter]:
    """Cross matched keys with per-unit alignment; returns (rows, excluded)."""
    rows: list[tuple] = []
    excluded: Counter = Counter()
    for key in sorted(matches):
        m = matches[key]
        if not m.method:
            continue
        nppa_rows = [nppa_by_id[i] for i in sorted(m.nppa_ids)]
        for jap_id in sorted(m.jap_ids):
            j = jap_by_id[jap_id]
            if j.mrp == 0:
                excluded["excluded_zero_mrp"] += 1
                continue
            if not j.pack_kind or not j.pack_value:
                excluded["excluded_unparseable_pack"] += 1
                continue
            basis = _JAP_BASIS.get(j.pack_kind)
            if basis is None:
                excluded["excluded_unparseable_pack"] += 1
                continue
            cands = []
            for n in nppa_rows:
                resolved = _nppa_basis(n)
                if resolved is None:
                    continue
                nbasis, per_unit = resolved
                if nbasis == basis:
                    cands.append((n, nbasis, per_unit))
            if not cands:
                excluded["excluded_basis_mismatch"] += 1
                continue
            jap_vol = j.pack_value if j.pack_kind == "volume_ml" else None
            n, nbasis, npu, label = _select_nppa_variant(
                cands, j.pack_kind, jap_vol)
            jap_pu = j.mrp / j.pack_value
            savings = (1.0 - jap_pu / npu) * 100.0 if npu else 0.0
            rows.append((
                key, n.row_id, jap_id, npu, nbasis, label, jap_pu,
                f"{j.pack_kind}:{_fmt(j.pack_value)}", savings,
                m.method, m.confidence, computed_at,
            ))
    return rows, excluded


# ---------------------------------------------------------------------------
# Full pipeline from DB
# ---------------------------------------------------------------------------


def _naive_key_jap(generic_name: str) -> str:
    parts = re.split(r"\s*(?:\+|&|,|\band\b)\s*", fold(generic_name))
    return "+".join(sorted(p.strip() for p in parts if p.strip()))


def _naive_key_nppa(formulation_raw: str) -> str:
    parts = formulation_raw.casefold().replace("\n", " ").split("+")
    return "+".join(sorted(p.strip() for p in parts if p.strip()))


def run_matching(db_path: str | Path) -> dict:
    """Run the full match from DB; write canonical + equivalents + meta."""
    db_path = Path(db_path)
    computed_at = datetime.now(UTC).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        jap_rows = conn.execute(
            "SELECT product_id, generic_name, unit_size, mrp FROM jap_products"
        ).fetchall()
        nppa_rows = conn.execute(
            "SELECT id, formulation_raw, form_raw, strength_raw, unit_type,"
            " unit_count, pack_volume_ml, price_value"
            " FROM nppa_ceiling_prices"
        ).fetchall()

        jap, jap_excluded = build_jap_canonical(
            [(r[0], r[1], r[2], r[3]) for r in jap_rows])
        nppa, nppa_excluded = build_nppa_canonical(list(nppa_rows))

        # Stage-0 naive baseline: plain casefolded molecule-set equality.
        naive_jap = {_naive_key_jap(r[1]) for r in jap_rows}
        naive_hits = sum(1 for r in nppa_rows
                         if _naive_key_nppa(r[1]) in naive_jap)

        matches, mstats = run_match(jap, nppa)
        jap_by_id = {j.product_id: j for j in jap}
        nppa_by_id = {n.row_id: n for n in nppa}
        equiv_rows, equiv_excluded = compute_equivalences(
            matches, jap_by_id, nppa_by_id, computed_at)

        matched_nppa: set[int] = mstats["matched_nppa"]
        matched_jap: set[int] = mstats["matched_jap"]

        # Canonical table: every key seen on either side. NPPA contributes
        # all its keys; JAP contributes only still-unmatched products (a
        # matched product lives under its NPPA-side match row, including
        # %wv<->mg/ml conversion matches whose keys differ by notation).
        canon_rows: dict[str, tuple] = {}
        for n in nppa:
            if not n.ok:
                continue
            row = canon_rows.get(n.key_alias)
            if row is None:
                canon_rows[n.key_alias] = (
                    n.key_alias, "+".join(sorted(n.molecules_alias)),
                    "+".join(sorted(n.strengths)), n.form, n.family,
                    ",".join(n.modifiers), [n.row_id], [],
                    None, "unmatched",
                )
            elif n.row_id not in row[6]:
                row[6].append(n.row_id)
        for j in jap:
            if not j.ok or j.product_id in matched_jap:
                continue
            row = canon_rows.get(j.key_alias)
            if row is None:
                canon_rows[j.key_alias] = (
                    j.key_alias, "+".join(sorted(j.molecules_alias)),
                    "+".join(sorted(s or "" for s in j.strengths)), j.form,
                    j.family, ",".join(j.modifiers), [],
                    [j.product_id], None, "unmatched",
                )
            elif j.product_id not in row[7]:
                row[7].append(j.product_id)
        for key, m in matches.items():
            if m.method and key in canon_rows:
                row = canon_rows[key]
                canon_rows[key] = (
                    row[0], m.molecule_set or row[1], m.strength_set or row[2],
                    row[3], row[4], ",".join(m.modifiers) or row[5],
                    sorted(m.nppa_ids), sorted(m.jap_ids),
                    m.method, m.confidence,
                )

        conn.executescript(SCHEMA_CANONICAL)
        conn.executemany(
            "INSERT INTO canonical_formulations(match_key, molecule_set,"
            " strength_set, form, form_family, modifiers, nppa_ids,"
            " jap_product_ids, match_method, confidence)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(k, mol, st, f, fam, mods, json.dumps(nids), json.dumps(jids),
              method, conf)
             for k, mol, st, f, fam, mods, nids, jids, method, conf
             in canon_rows.values()],
        )
        conn.executescript(SCHEMA_EQUIVALENTS)
        conn.executemany(
            "INSERT INTO equivalents(match_key, nppa_row_id, jap_product_id,"
            " nppa_ceiling_per_unit, nppa_unit_basis, nppa_variant_basis,"
            " jap_per_unit, jap_pack_parsed, savings_pct, match_method,"
            " confidence, computed_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            equiv_rows,
        )

        nppa_keys = {n.key_alias for n in nppa if n.ok}
        raw_keys = {(r[1], r[3]) for r in nppa_rows}
        triple_keys = {(r[1].casefold(), (r[2] or "").casefold(),
                        (r[3] or "").casefold()) for r in nppa_rows}
        stage_keys = mstats["stage_keys"]
        meta = {
            "match_computed_at": computed_at,
            "stage_0_baseline_count": str(naive_hits),
            "match_stage_1_count": str(len(stage_keys["stage_1_exact"])),
            "match_stage_2_count": str(len(stage_keys["stage_2_alias"])),
            "match_stage_3_count": str(len(stage_keys["stage_3_fuzzy"])),
            "form_family_count": str(len(stage_keys["form_family"])),
            "modifier_relaxed_count": str(int(mstats["modifier_relaxed_count"])),
            "pct_conversion_count": str(int(mstats["pct_conversion_count"])),
            "slash_mismatch_count": str(mstats["slash_mismatch_count"]),
            "nppa_canonical_keys": str(len(nppa_keys)),
            "nppa_raw_keys": str(len(raw_keys)),
            "nppa_formulation_keys": str(len(triple_keys)),
            "matched_nppa_keys": str(len(matches)),
            "matched_nppa_rows": str(len(matched_nppa)),
            "matched_jap_products": str(len(matched_jap)),
            "unmatched_nppa_count": str(sum(
                1 for n in nppa if n.ok and n.row_id not in matched_nppa)),
            "unmatched_jap_count": str(sum(
                1 for j in jap if j.ok and j.product_id not in matched_jap)),
            "equivalents_count": str(len(equiv_rows)),
        }
        for reason, count in (list(jap_excluded.items())
                              + list(nppa_excluded.items())
                              + list(equiv_excluded.items())):
            norm = reason.replace("pack:", "").replace(":", "_")
            if not norm.startswith("excluded_"):
                norm = f"excluded_{norm}"
            meta[norm] = str(int(meta.get(norm, "0")) + count)
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            list(meta.items()),
        )
    return {
        "meta": meta,
        "matches": matches,
        "fuzzy_review": mstats["fuzzy_review"],
        "matched_nppa": matched_nppa,
        "matched_jap": matched_jap,
        "jap": jap,
        "nppa": nppa,
        "equiv_rows": equiv_rows,
    }


def print_report(result: dict, db_path: str | Path) -> None:
    """Print the match ladder, queues, savings, and examples."""
    meta = result["meta"]
    with sqlite3.connect(db_path) as conn:
        nppa_total = 866  # brief denominator: (formulation, form, strength)
        nppa_measured = int(meta.get("nppa_formulation_keys", "866"))
        s1 = int(meta.get("match_stage_1_count", 0))
        s2 = int(meta.get("match_stage_2_count", 0))
        s3 = int(meta.get("match_stage_3_count", 0))
        fam = int(meta.get("form_family_count", 0))
        print("== SameDrug Phase 3 match ladder ==")
        print(f"stage 0 naive baseline (casefold molecule-set): "
              f"{meta.get('stage_0_baseline_count')} NPPA rows")
        cum = 0
        for label, count in (("stage 1 exact", s1), ("stage 2 alias", s2),
                             ("stage 3 fuzzy", s3), ("form-family", fam)):
            cum += count
            print(f"{label}: {count} matched keys"
                  f" (cumulative {cum}, {100.0 * cum / max(nppa_total, 1):.1f}%"
                  f" of {nppa_total} NPPA keys; measured triples: "
                  f"{nppa_measured})")
        print(f"modifier_relaxed: {meta.get('modifier_relaxed_count')}")
        print(f"pct_conversion (%wv<->mg/ml exact): "
              f"{meta.get('pct_conversion_count')}")
        print(f"slash_mismatch (positional, never matched): "
              f"{meta.get('slash_mismatch_count')}")
        print(f"equivalents: {meta.get('equivalents_count')}")
        print(f"unmatched NPPA (ok): {meta.get('unmatched_nppa_count')};"
              f" unmatched JAP (ok): {meta.get('unmatched_jap_count')}")
        print("exclusions:", {k[9:]: v for k, v in sorted(meta.items())
                              if k.startswith("excluded_")})
        print("-- top-15 unmatched NPPA formulations --")
        for (formulation, strength), count in Counter(
                (n.formulation_raw, "+".join(sorted(n.strengths)))
                for n in result["nppa"]
                if n.ok and n.row_id not in result["matched_nppa"]).most_common(15):
            print(f"  {count:3d} {formulation!r} [{strength}]")
        print("-- top-15 unmatched JAP molecule sets --")
        for (mols, form), count in Counter(
                (tuple(sorted(j.molecules_alias)), j.form)
                for j in result["jap"]
                if j.ok and j.product_id not in result["matched_jap"]).most_common(15):
            print(f"  {count:3d} {'+'.join(mols)} [{form}]")
        print(f"-- fuzzy review queue "
              f"({len(result['fuzzy_review'])} @0.85-0.92, not applied) --")
        for row in result["fuzzy_review"][:20]:
            print(f"  {row}")
        rows = conn.execute(
            "SELECT savings_pct, match_method, confidence, nppa_row_id,"
            " jap_product_id, match_key FROM equivalents").fetchall()
        if rows:
            savings = sorted(r[0] for r in rows)
            neg = sum(1 for s in savings if s < 0)
            print(f"savings: n={len(savings)} median={statistics.median(savings):.1f}"
                  f" min={min(savings):.1f} max={max(savings):.1f}"
                  f" negatives(JAP above ceiling)={neg}")
            print("method/confidence:",
                  dict(Counter((r[1], r[2]) for r in rows)))
            print("-- 5 example equivalents --")
            for r in rows[:5]:
                n = conn.execute(
                    "SELECT formulation_raw, strength_raw, qualifier_raw,"
                    " price_value FROM nppa_ceiling_prices WHERE id=?",
                    (r[3],)).fetchone()
                j = conn.execute(
                    "SELECT generic_name, unit_size, mrp FROM jap_products"
                    " WHERE product_id=?", (r[4],)).fetchone()
                print(f"  {r[5]} savings={r[0]:.1f}% [{r[1]}/{r[2]}]")
                print(f"    NPPA {n} | JAP {j}")


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Match NPPA ceilings to JAP.")
    parser.add_argument("--db", default=DB_DEFAULT)
    parser.add_argument("--report", action="store_true",
                        help="print the match ladder after writing tables")
    args = parser.parse_args(argv)
    result = run_matching(args.db)
    if args.report:
        print_report(result, args.db)
    else:
        print(f"equivalents={result['meta']['equivalents_count']}")
    return result


if __name__ == "__main__":
    main()
