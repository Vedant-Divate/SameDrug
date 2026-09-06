"""Matcher goldens, wrong-match guards, and the zero-MRP gate (Phase 3).

Cross-source pairs use REAL rows (NPPA id / JAP productId cited); the
wrong-match guards use small synthetic fixtures labeled as such.
"""

import sqlite3
from pathlib import Path

import pytest

from samedrug.pipeline.build_db import build_db
from samedrug.pipeline.match import (
    build_jap_canonical,
    build_nppa_canonical,
    compute_equivalences,
    run_match,
)

ROOT = Path(__file__).parent.parent.parent
JAP_SEED = ROOT / "data" / "raw" / "jap_products.json"
NPPA_ALL_SEED = ROOT / "data" / "raw" / "nppa_ceiling_all.csv"
NPPA_SPECIAL_SEED = ROOT / "data" / "raw" / "nppa_ceiling_special.csv"
SEEDS_PRESENT = JAP_SEED.exists() and NPPA_ALL_SEED.exists()
needs_seeds = pytest.mark.skipif(not SEEDS_PRESENT, reason="raw seeds absent")


def _real_db():
    with sqlite3.connect(ROOT / "data" / "processed" / "drugs.db") as conn:
        return conn


def _jap_rows(conn, *pids):
    rows = []
    for pid in pids:
        r = conn.execute(
            "SELECT product_id, generic_name, unit_size, mrp FROM jap_products"
            " WHERE product_id = ?",
            (pid,),
        ).fetchone()
        assert r is not None, f"JAP product {pid} missing from seed DB"
        rows.append((r[0], r[1], r[2], r[3]))
    return rows


def _nppa_rows(conn, *ids):
    rows = []
    for i in ids:
        r = conn.execute(
            "SELECT id, formulation_raw, form_raw, strength_raw, unit_type,"
            " unit_count, pack_volume_ml, price_value"
            " FROM nppa_ceiling_prices WHERE id = ?",
            (i,),
        ).fetchone()
        assert r is not None, f"NPPA row {i} missing from seed DB"
        rows.append(tuple(r))
    return rows


def _match_pair(jap_ids, nppa_ids):
    with _real_db() as conn:
        jap, _ = build_jap_canonical(_jap_rows(conn, *jap_ids))
        nppa, _ = build_nppa_canonical(_nppa_rows(conn, *nppa_ids))
    assert all(j.ok for j in jap)
    assert all(n.ok for n in nppa)
    matches, _ = run_match(jap, nppa)
    return jap, nppa, matches


@needs_seeds
def test_match_exact_single_atorvastatin():
    # NPPA id 220 ATORVASTATIN TABLET 10MG <-> JAP 1365, no alias involved.
    _, _, matches = _match_pair([1365], [220])
    assert list(matches) == ["atorvastatin:10mg|tablet"]
    m = matches["atorvastatin:10mg|tablet"]
    assert (m.method, m.confidence) == ("exact", "high")
    assert m.nppa_ids == [220] and m.jap_ids == [1365]


@needs_seeds
def test_match_alias_salt_metformin():
    # NPPA id 625 METFORMIN 500MG <-> JAP 200 Metformin Hydrochloride
    # (salt-strip + alias table, stage 2).
    _, _, matches = _match_pair([200], [625])
    m = matches["metformin:500mg|tablet"]
    assert (m.method, m.confidence) == ("alias", "high")


@needs_seeds
def test_match_alias_combo_formoterol_per_dose():
    # NPPA ids 464/465 FORMOTERAL+BUDESONIDE 6/200 MCG (typo alias +
    # fumarate salt-strip) <-> JAP 361 Formoterol Fumarate 6mcg and
    # Budesonide 200mcg Inhaler, 120 MD. Per-dose alignment is asserted
    # on the Equivalences below via the metered_dose variant.
    jap, nppa, matches = _match_pair([361], [464, 465])
    key = "budesonide:0.2mg+formoterol:0.006mg|inhaler"
    m = matches[key]
    assert (m.method, m.confidence) == ("alias", "high")
    assert sorted(m.nppa_ids) == [464, 465]
    rows, _ = compute_equivalences(
        matches, {j.product_id: j for j in jap},
        {n.row_id: n for n in nppa}, "2026-01-01T00:00:00+00:00")
    assert len(rows) == 1
    (out_key, nppa_id, jap_id, npu, basis, variant, _nlem, jap_pu, pack,
     savings, method, conf, _) = rows[0]
    assert out_key == key and basis == "dose"
    assert nppa_id == 464  # Per Metered Dose row preferred over Per Dose
    assert variant == "same_unit_type"
    assert jap_pu == pytest.approx(154.69 / 120.0)
    assert npu == pytest.approx(3.03)
    assert savings == pytest.approx((1 - (154.69 / 120.0) / 3.03) * 100.0)


@needs_seeds
def test_match_modifier_relaxed_aspirin():
    # NPPA id 9 Acetylsalicylic acid 75mg (plain Tablet) <-> JAP 2118
    # Aspirin Enteric Coated 75mg: alias resolves the molecule, the
    # coating mismatch downgrades to modifier_relaxed (low confidence).
    _, _, matches = _match_pair([2118], [9])
    m = matches["aspirin:75mg|tablet"]
    assert (m.method, m.confidence) == ("modifier_relaxed", "low")


@needs_seeds
def test_match_per_ml_sodium_chloride_variant_selection():
    # NPPA Sodium chloride 0.9% has only pack+volume rows (ids 123-126);
    # JAP 209 (500 ml) aligns per ml against the lowest-id same-basis row.
    jap, nppa, matches = _match_pair([209], [123, 124, 125, 126])
    key = "sodium chloride:0.9%wv|injection"
    assert matches[key].method == "exact"
    rows, _ = compute_equivalences(
        matches, {j.product_id: j for j in jap},
        {n.row_id: n for n in nppa}, "2026-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0][4] == "ml" and rows[0][3] == pytest.approx(30.17 / 250.0)
    assert rows[0][7] == pytest.approx(28.13 / 500.0)


def test_wrong_match_guards_synthetic():
    # Synthetic rows (labeled): same molecule different strengths never
    # match; a combo never fuzzy-matches a single even at high similarity.
    jap, _ = build_jap_canonical([
        (9001, "Testmol Tablets IP 10mg", "10's", 5.0),
        (9002, "Testmol 10mg and Otherdrug 5mg Tablets", "10's", 5.0),
    ])
    nppa, _ = build_nppa_canonical([
        (8001, "TESTMOL", "TABLET", "20 MG", "tablet", 1.0, None, 1.0),
        (8002, "TESTMOL", "TABLET", "10 MG", "tablet", 1.0, None, 1.0),
    ])
    assert all(j.ok for j in jap) and all(n.ok for n in nppa)
    matches, _ = run_match(jap, nppa)
    # Only the exact 10mg single matches; the 20mg row and the combo stay out.
    assert list(matches) == ["testmol:10mg|tablet"]
    m = matches["testmol:10mg|tablet"]
    assert m.jap_ids == [9001] and m.nppa_ids == [8002]


def test_cross_form_capsule_tablet_no_match_synthetic():
    # Synthetic (labeled, Phase 3.5): capsule vs tablet must NOT match even
    # with identical molecule+strength; same-form pairs still match.
    jap, _ = build_jap_canonical([
        (9011, "Testmol Tablets IP 10mg", "10's", 5.0),
        (9012, "Testmol Capsules IP 10mg", "10's", 5.0),
    ])
    nppa, _ = build_nppa_canonical([
        (8011, "TESTMOL", "TABLET", "10 MG", "tablet", 1.0, None, 1.0),
        (8012, "TESTMOL", "CAPSULE", "10 MG", "capsule", 1.0, None, 1.0),
    ])
    assert all(j.ok for j in jap) and all(n.ok for n in nppa)
    matches, _ = run_match(jap, nppa)
    assert set(matches) == {"testmol:10mg|tablet", "testmol:10mg|capsule"}
    assert matches["testmol:10mg|tablet"].jap_ids == [9011]
    assert matches["testmol:10mg|tablet"].nppa_ids == [8011]
    assert matches["testmol:10mg|capsule"].jap_ids == [9012]
    assert matches["testmol:10mg|capsule"].nppa_ids == [8012]


@needs_seeds
def test_cross_form_real_doxycycline_capsule_matches_capsule():
    # Real rows (Phase 3.5 Step-0): JAP 2392 Doxycycline Capsules must match
    # NPPA 409 CAPSULE (Rs 3.15), never NPPA 408 TABLET (Rs 1.25).
    _, _, matches = _match_pair([2392], [408, 409])
    assert list(matches) == ["doxycycline:100mg|capsule"]
    m = matches["doxycycline:100mg|capsule"]
    assert m.nppa_ids == [409] and m.jap_ids == [2392]


def _nppa_rows_full(conn, *ids):
    rows = []
    for i in ids:
        r = conn.execute(
            "SELECT id, formulation_raw, form_raw, strength_raw, unit_type,"
            " unit_count, pack_volume_ml, price_value, pack_condition_raw,"
            " nlem_version FROM nppa_ceiling_prices WHERE id = ?",
            (i,),
        ).fetchone()
        assert r is not None, f"NPPA row {i} missing from seed DB"
        rows.append(tuple(r))
    return rows


@needs_seeds
def test_variant_selection_dicyclomine_real():
    # Real rows (cited): JAP 2297 2ml ampoule (10mg per ml) with NPPA
    # id 44 Rs 0.20 (2015, generic 1 ML) vs id 383 Rs 2.15
    # (2022, 10 ML & more) vs id 384 Rs 3.40 (2022, Less than 10 ML).
    # Correct variant for 2ml (<10ml): id 384, NLEM 2022, savings positive.
    with _real_db() as conn:
        jap, _ = build_jap_canonical(_jap_rows(conn, 2297))
        nppa, _ = build_nppa_canonical(_nppa_rows_full(conn, 44, 383, 384))
    assert all(j.ok for j in jap) and all(n.ok for n in nppa)
    matches, _ = run_match(jap, nppa)
    assert list(matches) == ["dicyclomine:10mg/ml|injection"]
    rows, _ = compute_equivalences(
        matches, {j.product_id: j for j in jap},
        {n.row_id: n for n in nppa}, "2026-01-01T00:00:00+00:00")
    assert len(rows) == 1
    (key, nppa_id, jap_id, npu, basis, variant, nlem, jap_pu, pack,
     savings, _method, _conf, _) = rows[0]
    assert (nppa_id, jap_id) == (384, 2297)
    assert nlem == "2022"
    assert variant == "pack_condition_match"
    assert npu == pytest.approx(3.40)
    assert jap_pu == pytest.approx(3.75 / 2.0)
    assert savings == pytest.approx((1 - (3.75 / 2.0) / 3.40) * 100.0)
    assert savings > 0


@needs_seeds
def test_variant_selection_metoclopramide_generic_beats_mismatch():
    # Real rows (cited): JAP 688 2ml ampoule with NPPA id 634 Rs 1.67
    # (2022, 10 ML & more pack) vs id 635 Rs 2.74 (2022, generic 1 ML).
    # For 2ml (<10ml) the bulk-pack condition mismatches, so the generic
    # row wins (NLEM tie, condition decides).
    with _real_db() as conn:
        jap, _ = build_jap_canonical(_jap_rows(conn, 688))
        nppa, _ = build_nppa_canonical(_nppa_rows_full(conn, 634, 635))
    matches, _ = run_match(jap, nppa)
    assert list(matches) == ["metoclopramide:5mg/ml|injection"]
    rows, _ = compute_equivalences(
        matches, {j.product_id: j for j in jap},
        {n.row_id: n for n in nppa}, "2026-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0][1] == 635
    assert rows[0][6] == "2022"


@needs_seeds
def test_full_build_equivalences_traceable_and_zero_mrp_free(tmp_path):
    """End-to-end on real seeds: raw tables frozen, equivalents non-empty,
    every row traceable, zero mrp=0 joins, negatives kept as findings."""
    db_path = tmp_path / "drugs.db"
    stats = build_db(
        JAP_SEED, db_path, tmp_path / "j.csv",
        nppa_all_path=NPPA_ALL_SEED,
        nppa_special_path=NPPA_SPECIAL_SEED,
        nppa_rejects_path=tmp_path / "n.csv",
    )
    assert (stats.total, stats.kept) == (2439, 2439)
    assert stats.nppa_kept == 936
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jap_products").fetchone()[0] == 2439
        assert conn.execute(
            "SELECT COUNT(*) FROM jap_products WHERE mrp = 0").fetchone()[0] == 387
        assert conn.execute(
            "SELECT COUNT(*) FROM nppa_ceiling_prices").fetchone()[0] == 936
        (n_equiv,) = conn.execute("SELECT COUNT(*) FROM equivalents").fetchone()
        assert n_equiv > 0
        (zero_joins,) = conn.execute(
            "SELECT COUNT(*) FROM equivalents e JOIN jap_products j"
            " ON e.jap_product_id = j.product_id WHERE j.mrp = 0").fetchone()
        assert zero_joins == 0
        (dangling_nppa,) = conn.execute(
            "SELECT COUNT(*) FROM equivalents e LEFT JOIN nppa_ceiling_prices n"
            " ON e.nppa_row_id = n.id WHERE n.id IS NULL").fetchone()
        (dangling_jap,) = conn.execute(
            "SELECT COUNT(*) FROM equivalents e LEFT JOIN jap_products j"
            " ON e.jap_product_id = j.product_id WHERE j.product_id IS NULL"
        ).fetchone()
        assert dangling_nppa == 0 and dangling_jap == 0
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    for key in ("match_stage_1_count", "match_stage_2_count",
                "match_stage_3_count", "form_family_count",
                "modifier_relaxed_count", "unmatched_nppa_count",
                "unmatched_jap_count", "equivalents_count"):
        assert key in meta, key
