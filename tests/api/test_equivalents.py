"""Equivalents shape, caveats, and traceability tests (fixture DB, no ports)."""

from __future__ import annotations

import sqlite3
import urllib.parse

import pytest

from samedrug.app import queries

NPPA_KEYS = {
    "ceiling_per_unit_inr",
    "unit_basis",
    "variant_basis",
    "nlem_version",
    "so_number",
    "so_date",
    "formulation_raw",
    "strength_raw",
    "qualifier_raw",
    "source_file",
    "source_row",
}
JAP_KEYS = {
    "product_id",
    "generic_name",
    "pack_raw",
    "pack_parsed",
    "mrp",
    "per_unit_inr",
    "drug_code",
    "group_name",
}
TOP_KEYS = {
    "match_key",
    "molecules",
    "strength_set",
    "form",
    "form_family",
    "modifiers",
    "nppa_side",
    "jap_side",
    "savings_pct",
    "match_method",
    "confidence",
    "caveats",
    "generated_at",
    "data_as_on",
}


def _get(client, match_key: str):
    return client.get(f"/api/equivalents/{urllib.parse.quote(match_key, safe='')}")


def test_full_shape_and_provenance(client):
    r = _get(client, "syntheticamycin:250mg|tablet")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert TOP_KEYS <= set(body)
    assert NPPA_KEYS <= set(body["nppa_side"])
    assert JAP_KEYS <= set(body["jap_side"])
    # Traceability to BOTH source rows.
    assert body["nppa_side"]["source_row"]
    assert body["nppa_side"]["so_number"]
    assert body["jap_side"]["product_id"] == 403
    assert body["jap_side"]["drug_code"]
    assert body["generated_at"] and body["data_as_on"]
    # Internally consistent: per-unit == mrp / pack, savings matches ratio.
    assert body["jap_side"]["per_unit_inr"] == body["jap_side"]["mrp"] / 10.0
    nppa, jap = body["nppa_side"]["ceiling_per_unit_inr"], body["jap_side"]["per_unit_inr"]
    assert body["savings_pct"] == pytest.approx((1.0 - jap / nppa) * 100.0)


def test_no_caveats_on_clean_high_confidence_row(client):
    body = _get(client, "syntheticamycin:250mg|tablet").json()
    assert body["caveats"] == []
    assert body["savings_pct"] > 0


def test_negative_with_tax_hint_caveats(client):
    """"syntheticbactin" ratio 1.079 in [1.05, 1.15]: tax hint fires."""
    body = _get(client, "syntheticbactin:500mg|tablet").json()
    assert body["savings_pct"] < 0
    assert "jap_price_above_ceiling" in body["caveats"]
    assert "possible_tax_basis_difference" in body["caveats"]


def test_negative_without_tax_hint_caveats(client):
    """"syntheticcillin" ratio 1.406: genuine negative, no tax hint."""
    body = _get(client, "syntheticcillin:250mg|tablet").json()
    assert body["savings_pct"] < 0
    assert "jap_price_above_ceiling" in body["caveats"]
    assert "possible_tax_basis_difference" not in body["caveats"]


def test_relaxed_and_low_confidence_caveats(client):
    body = _get(client, "syntheticdazole:20mg|tablet").json()
    assert body["match_method"] == "modifier_relaxed"
    assert body["confidence"] == "low"
    assert "form_or_modifier_relaxed: verify dosage form" in body["caveats"]
    assert "low_match_confidence" in body["caveats"]


def test_form_family_caveat(client):
    body = _get(client, "syntheticemycin:33.75mg/ml|suspension").json()
    assert body["match_method"] == "form_family"
    assert "form_or_modifier_relaxed: verify dosage form" in body["caveats"]
    assert "low_match_confidence" in body["caveats"]


def test_caveat_builder_unit_cases():
    assert queries.build_caveats("exact", "high", 10.0) == []
    assert queries.build_caveats("form_family", "high", 10.0) == [
        "form_or_modifier_relaxed: verify dosage form"
    ]
    assert queries.build_caveats("exact", "low", 10.0) == ["low_match_confidence"]
    out = queries.build_caveats("exact", "high", -5.0, 1.08, 1.0)
    assert out == ["jap_price_above_ceiling", "possible_tax_basis_difference"]
    out = queries.build_caveats("exact", "high", -5.0, 1.40, 1.0)
    assert out == ["jap_price_above_ceiling"]


def test_unknown_key_404_is_structured(client):
    r = _get(client, "nosuchmolecule:10mg|tablet")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"]["error"] == "unknown_match_key"


def test_traceability_for_every_fixture_row(client, fixture_db):
    """Every equivalents row surfaces with both source provenances."""
    conn = sqlite3.connect(fixture_db)
    keys = [r[0] for r in conn.execute("SELECT match_key FROM equivalents")]
    conn.close()
    assert len(keys) >= 2
    for key in keys:
        body = _get(client, key).json()
        assert body["nppa_side"]["source_row"]
        assert body["nppa_side"]["so_number"]
        assert body["jap_side"]["product_id"]
        assert body["jap_side"]["drug_code"]


def test_no_mrp_zero_product_in_any_equivalence_payload(client, fixture_db):
    conn = sqlite3.connect(fixture_db)
    keys = [r[0] for r in conn.execute("SELECT match_key FROM equivalents")]
    zero_ids = {
        r[0] for r in conn.execute("SELECT product_id FROM jap_products WHERE mrp = 0")
    }
    conn.close()
    assert zero_ids, "fixture must contain mrp=0 products"
    for key in keys:
        body = _get(client, key).json()
        assert body["jap_side"]["mrp"] != 0
        assert body["jap_side"]["product_id"] not in zero_ids


def test_unit_basis_pre_aligned_for_every_fixture_row(fixture_db):
    """Mismatch is impossible here (equivalents pre-aligned) — assert it."""
    conn = sqlite3.connect(fixture_db)
    rows = conn.execute(
        "SELECT nppa_unit_basis, jap_pack_parsed FROM equivalents"
    ).fetchall()
    conn.close()
    assert rows
    for basis, pack in rows:
        assert queries.unit_basis_aligned(basis, pack), (basis, pack)
