"""Search tier + molecule-boundary-safety tests (fixture DB only, no ports)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

RESULT_KEYS = {
    "match_key",
    "molecules",
    "strength_set",
    "form",
    "form_family",
    "has_equivalents",
    "n_equivalents",
    "tier",
}


def _linked_texts(db_path: Path, match_key: str) -> tuple[set[str], set[str]]:
    """Molecule tokens + linked JAP/NPPA row texts for one canonical key."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT molecule_set, nppa_ids, jap_product_ids FROM canonical_formulations"
        " WHERE match_key = ?",
        (match_key,),
    ).fetchone()
    assert row is not None
    tokens = set((row[0] or "").casefold().split("+"))
    texts: set[str] = set()
    for nid in json.loads(row[1] or "[]"):
        r = conn.execute(
            "SELECT formulation_raw FROM nppa_ceiling_prices WHERE id = ?", (nid,)
        ).fetchone()
        if r:
            texts.add(r[0].casefold())
    for pid in json.loads(row[2] or "[]"):
        r = conn.execute(
            "SELECT generic_name FROM jap_products WHERE product_id = ?", (pid,)
        ).fetchone()
        if r:
            texts.add(r[0].casefold())
    conn.close()
    return tokens, texts


def _assert_boundary(db_path: Path, query: str, results: list[dict]) -> None:
    """Every non-fuzzy result is token- or linked-row-contained (never silent)."""
    q = query.strip().casefold()
    for item in results:
        if item["tier"] == "fuzzy":
            continue
        tokens, texts = _linked_texts(db_path, item["match_key"])
        assert any(q in t for t in tokens) or any(q in t for t in texts), (
            f"cross-molecule leak: q={query!r} -> {item['match_key']}"
        )


def test_exact_tier_direct_molecule(client):
    r = client.get("/api/search", params={"q": "syntheticamycin"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["results"], "expected an exact hit"
    first = body["results"][0]
    assert first["match_key"] == "syntheticamycin:250mg|tablet"
    assert first["tier"] == "exact"
    assert first["has_equivalents"] is True
    assert first["n_equivalents"] == 1
    assert RESULT_KEYS <= set(first)
    assert body["generated_at"] and body["data_as_on"]


def test_exact_tier_via_alias_table(client):
    """acetylsalicylic acid (NPPA name) resolves to aspirin (alias-keyed exact)."""
    r = client.get("/api/search", params={"q": "acetylsalicylic acid"})
    assert r.status_code == 200
    keys = {item["match_key"] for item in r.json()["results"]}
    assert "aspirin:300mg|tablet" in keys
    hit = next(i for i in r.json()["results"] if i["match_key"] == "aspirin:300mg|tablet")
    assert hit["tier"] == "exact"
    assert hit["has_equivalents"] is False


def test_prefix_tier_before_substring_and_fuzzy(client):
    r = client.get("/api/search", params={"q": "syntheti"})
    assert r.status_code == 200
    tiers = [i["tier"] for i in r.json()["results"]]
    assert tiers, "expected prefix hits"
    assert all(t == "prefix" for t in tiers[:5])
    order = {"exact": 0, "prefix": 1, "substring": 2, "fuzzy": 3}
    assert [order[t] for t in tiers] == sorted(order[t] for t in tiers)


def test_substring_tier(client):
    r = client.get("/api/search", params={"q": "mycin"})
    assert r.status_code == 200
    by_key = {i["match_key"]: i["tier"] for i in r.json()["results"]}
    assert by_key.get("syntheticamycin:250mg|tablet") == "substring"
    assert by_key.get("syntheticemycin:33.75mg/ml|suspension") == "substring"
    assert "syntheticbactin:500mg|tablet" not in by_key


def test_fuzzy_tier_is_labeled_and_last(client):
    """Misspelling never sneaks into exact/prefix/substring tiers."""
    r = client.get("/api/search", params={"q": "synteticamycin"})
    assert r.status_code == 200
    tiers = [i["tier"] for i in r.json()["results"]]
    assert all(t == "fuzzy" for t in tiers), f"silent substitution: {tiers}"
    if tiers:
        assert r.json()["results"][0]["match_key"] == "syntheticamycin:250mg|tablet"


def test_misspelling_never_crosses_molecule_silently(client):
    r = client.get("/api/search", params={"q": "cetrizine"})
    assert r.status_code == 200
    tiers = {i["tier"] for i in r.json()["results"]}
    assert tiers <= {"fuzzy"}, f"cetrizine leaked into {tiers}"


def test_dolo_does_not_return_unrelated_molecules(client, fixture_db):
    """Fixture has no dolo tokens: 'dolo' must not invent an answer."""
    r = client.get("/api/search", params={"q": "dolo"})
    assert r.status_code == 200
    for item in r.json()["results"]:
        assert item["tier"] == "fuzzy", f"dolo silently matched {item['match_key']}"
    _assert_boundary(fixture_db, "dolo", r.json()["results"])


def test_boundary_safety_across_queries(client, fixture_db):
    for q in ["syntheticamycin", "mycin", "syntheti", "aspirin", "glucose", "dolo"]:
        r = client.get("/api/search", params={"q": q})
        assert r.status_code == 200
        _assert_boundary(fixture_db, q, r.json()["results"])


def test_min_length_400(client):
    r = client.get("/api/search", params={"q": "a"})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], dict)
    assert r.json()["detail"]["error"] == "query_too_short"


def test_empty_result_is_200_with_empty(client):
    r = client.get("/api/search", params={"q": "zzzzzqqqq"})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_limit_respected(client):
    r = client.get("/api/search", params={"q": "syntheti", "limit": 2})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 2
