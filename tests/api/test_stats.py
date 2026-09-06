"""Stats reconciliation, JAP lookup, health, read-only, integration locks."""

from __future__ import annotations

import sqlite3
import statistics
import urllib.parse
from pathlib import Path

import pytest

from samedrug.app import queries
from samedrug.app.main import create_app

REAL_DB = Path(__file__).parent.parent.parent / "data" / "processed" / "drugs.db"
needs_real_db = pytest.mark.skipif(
    not REAL_DB.exists(), reason="real drugs.db absent (CI skips)"
)


def _get(client, match_key: str):
    return client.get(f"/api/equivalents/{urllib.parse.quote(match_key, safe='')}")


def test_health_counts_and_envelope(client, fixture_db):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["status"] == "ok"
    conn = sqlite3.connect(fixture_db)
    expected = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in (
            "jap_products",
            "nppa_ceiling_prices",
            "canonical_formulations",
            "equivalents",
        )
    }
    conn.close()
    assert body["db"] == expected
    assert body["db"]["jap_products"] == 12
    assert body["db"]["equivalents"] == 5
    assert isinstance(body["db_stats_cached"], bool)
    assert body["generated_at"] and body["data_as_on"]


def test_stats_shape_and_reconciliation(client, fixture_db):
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["equivalents_count"] == 5
    # Phase-3 reconciliation trap, guarded forever: distribution sums to count.
    assert sum(body["method_distribution"].values()) == body["equivalents_count"]
    conn = sqlite3.connect(fixture_db)
    savings = sorted(
        row[0] for row in conn.execute("SELECT savings_pct FROM equivalents")
    )
    negatives = conn.execute(
        "SELECT COUNT(*) FROM equivalents WHERE savings_pct < 0"
    ).fetchone()[0]
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    conn.close()
    assert body["negative_count"] == negatives == 2
    assert body["savings"]["median"] == pytest.approx(statistics.median(savings))
    assert body["savings"]["min"] == pytest.approx(min(savings))
    assert body["savings"]["max"] == pytest.approx(max(savings))
    assert body["match_ladder"]["matched_keys"] == int(meta["matched_nppa_keys"])
    assert body["match_ladder"]["total_nppa_keys"] == int(meta["nppa_formulation_keys"])
    assert body["data_as_on"] == meta["match_computed_at"]
    assert body["generated_at"]


def test_stats_cached_after_first_call(client):
    client.get("/api/stats")
    assert client.get("/health").json()["db_stats_cached"] is True


def test_jap_lookup_matched_product(client):
    r = client.get("/api/jap/403")
    assert r.status_code == 200
    body = r.json()
    assert body["product_id"] == 403
    assert body["mrp"] == pytest.approx(28.13)
    assert body["match_key"] == "syntheticamycin:250mg|tablet"
    assert len(body["equivalents"]) == 1
    assert body["equivalents"][0]["savings_pct"] > 0
    assert "excluded_reason" not in body
    assert body["generated_at"] and body["data_as_on"]


def test_jap_lookup_canon_but_no_equivalents(client):
    """Product 1 has a canonical key but no equivalence rows."""
    r = client.get("/api/jap/1")
    assert r.status_code == 200
    body = r.json()
    assert body["match_key"] == "aceclofenac:100mg+paracetamol:325mg|tablet"
    assert body["equivalents"] == []
    assert "excluded_reason" not in body


def test_jap_lookup_zero_mrp_returns_excluded_reason(client):
    r = client.get("/api/jap/1493")
    assert r.status_code == 200
    body = r.json()
    assert body["mrp"] == 0
    assert body["excluded_reason"] == "price_not_published"


def test_jap_lookup_unknown_id_404(client):
    r = client.get("/api/jap/999999")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_product_id"


def test_app_connection_is_read_only(fixture_db):
    """HARD rule: a write through the app's connection must fail."""
    with queries.connect_ro(fixture_db) as conn:
        # sqlite reports "attempt to write a readonly database".
        with pytest.raises(sqlite3.OperationalError, match=r"(?i)read.?only"):
            conn.execute("UPDATE jap_products SET mrp = 1.0 WHERE product_id = 403")
    with sqlite3.connect(fixture_db) as conn:
        assert conn.execute(
            "SELECT mrp FROM jap_products WHERE product_id = 403"
        ).fetchone()[0] == pytest.approx(28.13)


@pytest.mark.integration
@needs_real_db
class TestRealDbLocks:
    @pytest.fixture()
    def real_client(self):
        from starlette.testclient import TestClient

        return TestClient(create_app(REAL_DB))

    def test_health_real_counts(self, real_client):
        body = real_client.get("/health").json()
        assert body["db"]["jap_products"] == 2439
        assert body["db"]["nppa_ceiling_prices"] == 936
        assert body["db"]["equivalents"] == 302

    def test_dicyclomine_2022_pack_condition_row(self, real_client):
        """Phase 3.5 fix locked at API level: Rs 3.40 (<10ml, NLEM-2022)."""
        body = _get(real_client, "dicyclomine:10mg/ml|injection").json()
        assert body["nppa_side"]["ceiling_per_unit_inr"] == pytest.approx(3.40)
        assert body["nppa_side"]["nlem_version"] == "2022"
        assert body["nppa_side"]["variant_basis"] == "pack_condition_match"
        assert body["savings_pct"] == pytest.approx(44.85294117647059)

    def test_genuine_negatives_render_with_caveats(self, real_client):
        phen = _get(real_client, "pheniramine:22.75mg|injection").json()
        assert phen["savings_pct"] == pytest.approx(-32.50883392226147)
        assert "jap_price_above_ceiling" in phen["caveats"]
        assert "possible_tax_basis_difference" not in phen["caveats"]
        dexa = _get(real_client, "dexamethasone:4mg/ml|injection").json()
        assert dexa["savings_pct"] == pytest.approx(-8.061420345489445)
        assert "jap_price_above_ceiling" in dexa["caveats"]
        assert "possible_tax_basis_difference" in dexa["caveats"]

    def test_traceability_negatives_plus_three_high_confidence(self, real_client):
        keys = [
            "pheniramine:22.75mg|injection",
            "dexamethasone:4mg/ml|injection",
            "abacavir:600mg+lamivudine:300mg|tablet",
            "acetazolamide:250mg|tablet",
            "acetylcysteine:200mg/ml|injection",
        ]
        for key in keys:
            body = _get(real_client, key).json()
            assert body["confidence"] == "high"
            assert body["nppa_side"]["source_row"]
            assert body["nppa_side"]["so_number"]
            assert body["jap_side"]["product_id"]
            assert body["jap_side"]["drug_code"]

    def test_stats_reconciliation_real_db(self, real_client):
        body = real_client.get("/api/stats").json()
        assert sum(body["method_distribution"].values()) == body["equivalents_count"]
        assert body["equivalents_count"] == 302
        assert body["negative_count"] == 2

    def test_search_dolo_never_matches_silently(self, real_client):
        """'dolo' is not a token prefix of 'dolutegravir' (d-o-l-u-), so it
        must never appear in exact/prefix/substring tiers — fuzzy only."""
        results = real_client.get("/api/search", params={"q": "dolo"}).json()["results"]
        tiers = {i["tier"] for i in results}
        assert tiers <= {"fuzzy"}, f"dolo silently matched: {tiers}"
        assert not any(
            "paracetamol" in i["molecules"] and i["tier"] != "fuzzy" for i in results
        )

    def test_search_misspelling_is_fuzzy_or_alias_exact(self, real_client):
        """'cetrizine' resolves via the pipeline alias table (alias-keyed
        exact) or falls to the labeled fuzzy tier — never a silent swap."""
        results = real_client.get("/api/search", params={"q": "cetrizine"}).json()[
            "results"
        ]
        assert results
        by_key = {i["match_key"]: i["tier"] for i in results}
        assert by_key.get("cetirizine:10mg|tablet") in ("exact", "fuzzy")
        assert set(by_key.values()) <= {"exact", "substring", "fuzzy"}
        assert any("cetirizine" in i["molecules"] for i in results)
