"""Phase-5 UI tests: HTML routes over the fixture DB (TestClient, no ports).

The fixture DB comes from tests/api/conftest.py unchanged; all expected
numbers are read back from that DB (never hardcoded), except the real-DB
integration locks at the bottom, which follow the Phase-4 skip pattern.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path

import pytest

from samedrug.app import queries
from samedrug.app import ui as ui_module
from samedrug.app.main import create_app

REAL_DB = Path(__file__).parent.parent.parent / "data" / "processed" / "drugs.db"
needs_real_db = pytest.mark.skipif(
    not REAL_DB.exists(), reason="real drugs.db absent (CI skips)"
)

HTML = "text/html"


def _card(client, slug: str):
    return client.get(f"/d/{slug}")


def _equiv_row(fixture_db: Path, match_key: str) -> sqlite3.Row:
    conn = sqlite3.connect(fixture_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT nppa_ceiling_per_unit, jap_per_unit, savings_pct, mrp"
        " FROM equivalents JOIN jap_products"
        " ON equivalents.jap_product_id = jap_products.product_id"
        " WHERE equivalents.match_key = ?",
        (match_key,),
    ).fetchone()
    conn.close()
    assert row is not None
    return row


# ---------------------------------------------------------------- home ---


def test_home_has_search_pitch_and_disclaimers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(HTML)
    assert '<form' in r.text and 'action="/search"' in r.text
    assert "every price traceable to source" in r.text
    assert "NOT medical advice" in r.text
    assert "not affiliated" in r.text


# -------------------------------------------------------------- search ---


def test_search_exact_renders_badge_and_card_link(client):
    r = client.get("/search", params={"q": "syntheticamycin"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(HTML)
    slug = client.app.state.key_to_slug["syntheticamycin:250mg|tablet"]
    assert f'href="/d/{slug}"' in r.text
    assert "equivalent available (1)" in r.text
    # Keys without equivalents render the honest badge instead of a link.
    r = client.get("/search", params={"q": "aspirin"})
    assert r.status_code == 200
    assert "no equivalent yet" in r.text


def test_search_reuses_api_query_functions(client):
    """HTML lists exactly the keys the JSON search returns (same fn)."""
    q = "mycin"
    api_keys = [
        i["match_key"]
        for i in client.get("/api/search", params={"q": q}).json()["results"]
    ]
    assert api_keys, "fixture query must return rows"
    html = client.get("/search", params={"q": q}).text
    slugs = client.app.state.key_to_slug
    for key in api_keys:
        with queries.connect_ro(client.app.state.ui_db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM equivalents WHERE match_key = ?", (key,)
            ).fetchone()[0]
        if n:
            assert f'href="/d/{slugs[key]}"' in html
        else:
            item = next(
                i
                for i in client.get("/api/search", params={"q": q}).json()["results"]
                if i["match_key"] == key
            )
            assert item["molecules"] in html


def test_fuzzy_section_is_separate_and_distinct(client):
    r = client.get("/search", params={"q": "synteticamycin"})
    assert r.status_code == 200
    assert "Similar names \u2014 check spelling" in r.text
    assert "Results (" not in r.text  # no main-tier hits for the misspelling
    body = client.get("/api/search", params={"q": "synteticamycin"}).json()["results"]
    assert body and all(i["tier"] == "fuzzy" for i in body)


def test_fuzzy_never_mixed_when_both_sections_present(client, fixture_db):
    """Any fuzzy link renders strictly below the 'Similar names' heading."""
    with queries.connect_ro(fixture_db) as conn:
        found = None
        for cand in ["syntheticamycin", "syntheti", "mycin", "synthetic"]:
            res = queries.search(conn, cand, 50)
            if any(i["tier"] != "fuzzy" for i in res) and any(
                i["tier"] == "fuzzy" for i in res
            ):
                found = cand
                break
    if found is None:
        pytest.skip("fixture has no mixed-tier query")
    html = client.get("/search", params={"q": found}).text
    cut = html.index("Similar names \u2014 check spelling")
    slugs = client.app.state.key_to_slug
    with queries.connect_ro(fixture_db) as conn:
        res = queries.search(conn, found, 50)
    for item in res:
        link = f'href="/d/{slugs[item["match_key"]]}"'
        if item["tier"] == "fuzzy" and item["has_equivalents"]:
            assert html.index(link) > cut
        elif item["tier"] != "fuzzy" and item["has_equivalents"]:
            assert html.index(link) < cut


def test_search_empty_is_friendly_200(client):
    r = client.get("/search", params={"q": "zzzzzqqqq"})
    assert r.status_code == 200
    assert "No matches" in r.text


def test_search_short_query_is_friendly_200(client):
    r = client.get("/search", params={"q": "a"})
    assert r.status_code == 200
    assert "at least 2 characters" in r.text
    r = client.get("/search")
    assert r.status_code == 200


# ---------------------------------------------------------------- card ---


def test_card_header_sides_hero_and_provenance(client, fixture_db):
    key = "syntheticamycin:250mg|tablet"
    html = _card(client, client.app.state.key_to_slug[key]).text
    row = _equiv_row(fixture_db, key)
    assert "Syntheticamycin 250mg tablet" in html
    assert f"\u20b9{row['nppa_ceiling_per_unit']:.2f}" in html
    assert f"\u20b9{row['jap_per_unit']:.2f}" in html
    assert f"\u20b9{row['mrp']:.2f}" in html
    assert f"~{row['savings_pct']:.1f}% less at Jan Aushadhi" in html
    with queries.connect_ro(fixture_db) as conn:
        payload = queries.get_equivalent(conn, key)
    assert f"{payload['match_method']} match" in html
    assert f"{payload['confidence']} confidence" in html
    assert "source_row" not in html  # label, not the column name
    assert payload["nppa_side"]["source_row"] in html
    assert payload["nppa_side"]["so_number"] in html
    assert f"product_id {payload['jap_side']['product_id']}" in html
    assert f"drug_code {payload['jap_side']['drug_code']}" in html
    assert payload["jap_side"]["generic_name"] in html
    assert "Data as on" in html


def test_card_display_rounding_with_full_precision_data_attrs(client, fixture_db):
    """Display strings are rounded; data-* keeps full float precision."""
    key = "syntheticamycin:250mg|tablet"
    html = _card(client, client.app.state.key_to_slug[key]).text
    row = _equiv_row(fixture_db, key)
    assert f"\u20b9{row['jap_per_unit']:.2f}" in html
    assert f"{row['savings_pct']:.1f}%" in html
    assert f'data-jap-per-unit="{row["jap_per_unit"]!r}"' in html
    assert f'data-ceiling="{row["nppa_ceiling_per_unit"]!r}"' in html
    assert f'data-savings="{row["savings_pct"]!r}"' in html
    assert f'data-mrp="{row["mrp"]!r}"' in html


def test_card_caveats_plain_english(client):
    bactin = _card(
        client, client.app.state.key_to_slug["syntheticbactin:500mg|tablet"]
    ).text
    assert "Jan Aushadhi price is ABOVE the ceiling" in bactin
    assert "may not be directly comparable (tax basis" in bactin
    cillin = _card(
        client, client.app.state.key_to_slug["syntheticcillin:250mg|tablet"]
    ).text
    assert "Jan Aushadhi price is ABOVE the ceiling" in cillin
    assert "may not be directly comparable (tax basis" not in cillin
    dazole = _card(
        client, client.app.state.key_to_slug["syntheticdazole:20mg|tablet"]
    ).text
    assert "Dosage form may differ" in dazole
    assert "Lower-confidence match" in dazole
    emycin = _card(
        client,
        client.app.state.key_to_slug["syntheticemycin:33.75mg/ml|suspension"],
    ).text
    assert "Dosage form may differ" in emycin
    assert "Lower-confidence match" in emycin


def test_card_clean_row_has_no_warning_section(client):
    html = _card(
        client, client.app.state.key_to_slug["syntheticamycin:250mg|tablet"]
    ).text
    assert "warning-list" not in html


def test_card_opengraph_tags(client, fixture_db):
    key = "syntheticamycin:250mg|tablet"
    html = _card(client, client.app.state.key_to_slug[key]).text
    row = _equiv_row(fixture_db, key)
    assert (
        f'og:title" content="Syntheticamycin 250mg tablet \u2014 '
        f"\u20b9{row['nppa_ceiling_per_unit']:.2f} ceiling vs "
        f"\u20b9{row['jap_per_unit']:.2f} Jan Aushadhi" in html
    )
    assert 'property="og:description"' in html
    assert f"{row['savings_pct']:.1f}%" in html


def test_unknown_slug_404_with_search_box(client):
    r = client.get("/d/no-such-slug")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(HTML)
    assert 'action="/search"' in r.text


def test_slug_without_equivalents_is_404(client):
    """Slug exists (built from canonical keys) but has no card rows."""
    key = "aspirin:300mg|tablet"
    slug = client.app.state.key_to_slug[key]
    r = client.get(f"/d/{slug}")
    assert r.status_code == 404
    assert 'action="/search"' in r.text


# ------------------------------------------------------------ escaping ---


def test_synthetic_script_name_is_escaped_on_card_and_search(xss_client):
    slug = xss_client.app.state.key_to_slug["xss<script>alert(1)</script>:10mg|tablet"]
    card = xss_client.get(f"/d/{slug}")
    assert card.status_code == 200
    assert "<script>alert(1)</script>" not in card.text
    assert "&lt;script&gt;" in card.text
    found = xss_client.get("/search", params={"q": "xss"})
    assert found.status_code == 200
    assert "<script>alert(1)</script>" not in found.text
    assert "&lt;script&gt;" in found.text


# ---------------------------------------------------------------- about ---


def test_about_disclaimers_sources_and_dates(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert "NOT medical advice" in r.text
    assert "verify with a pharmacist" in r.text
    assert "NPPA" in r.text and "PMBJP" in r.text
    assert "not affiliated" in r.text
    assert "tax-exclusive" in r.text
    assert "Data as on" in r.text or "data as on" in r.text


# ---------------------------------------------------------- unit cases ---


def test_slug_map_disambiguates_deterministically():
    # ':' and ' ' both fold to '-' — these two genuinely collide.
    s2k, k2s, n = ui_module.build_slug_map(["a:1mg|tablet", "a 1mg|tablet"])
    assert n == 1
    assert k2s["a 1mg|tablet"] == "a-1mg-tablet"  # sorted first claims the base
    assert k2s["a:1mg|tablet"] == "a-1mg-tablet-2"
    s2k, k2s, n = ui_module.build_slug_map(
        ["povidone iodine:10%wv|solution", "povidone-iodine:10%wv|solution"]
    )
    assert n == 1
    assert k2s["povidone iodine:10%wv|solution"] == "povidone-iodine-10-wv-solution"
    assert k2s["povidone-iodine:10%wv|solution"] == "povidone-iodine-10-wv-solution-2"
    # Deterministic regardless of input order.
    rev = ui_module.build_slug_map(
        ["povidone-iodine:10%wv|solution", "povidone iodine:10%wv|solution"]
    )
    assert rev == (s2k, k2s, n)


def test_display_filters_and_caveat_mapping():
    assert ui_module.format_inr(0.6559999999999999) == "\u20b90.66"
    assert ui_module.format_inr(0.93) == "\u20b90.93"
    assert ui_module.format_pct1(29.462365591397866) == "29.5%"
    assert (
        ui_module.plain_caveat("form_or_modifier_relaxed: verify dosage form")
        == "Dosage form may differ \u2014 verify with your pharmacist"
    )
    assert (
        ui_module.plain_caveat("low_match_confidence")
        == "Lower-confidence match \u2014 verify with your pharmacist"
    )
    assert "ABOVE the ceiling" in ui_module.plain_caveat("jap_price_above_ceiling")
    assert "tax basis" in ui_module.plain_caveat("possible_tax_basis_difference")


def test_autoescape_explicit_for_html_templates():
    env = ui_module.make_templates().env
    assert env.autoescape is not False
    assert env.autoescape("detail.html") is True


# -------------------------------------------------- real-DB integration ---


@pytest.mark.integration
@needs_real_db
class TestRealDbUiLocks:
    @pytest.fixture()
    def real_client(self):
        from starlette.testclient import TestClient

        return TestClient(create_app(REAL_DB))

    def test_slug_collision_count_measured(self, real_client):
        assert real_client.app.state.slug_collisions == 2
        assert len(real_client.app.state.slug_to_key) == 2709

    def test_paracetamol_card_values(self, real_client):
        html = real_client.get("/d/paracetamol-500mg-tablet").text
        assert "Paracetamol 500mg tablet" in html
        assert "\u20b90.93" in html and "\u20b90.66" in html
        assert 'data-jap-per-unit="0.6559999999999999"' in html
        assert 'data-savings="29.462365591397866"' in html
        assert "~29.5% less at Jan Aushadhi" in html
        assert (
            'og:title" content="Paracetamol 500mg tablet \u2014 '
            "\u20b90.93 ceiling vs \u20b90.66 Jan Aushadhi" in html
        )
        assert "nppa_all sl_no=706" in html
        assert "product_id 982" in html

    def test_genuine_negatives_render_caveats(self, real_client):
        phen = real_client.get(
            f"/d/{urllib.parse.quote('pheniramine-22-75mg-injection', safe='')}"
        ).text
        assert "Jan Aushadhi price is ABOVE the ceiling" in phen
        assert "may not be directly comparable (tax basis" not in phen
        dexa = real_client.get("/d/dexamethasone-4mg-ml-injection").text
        assert "Jan Aushadhi price is ABOVE the ceiling" in dexa
        assert "may not be directly comparable (tax basis" in dexa

    def test_search_and_about_on_real_db(self, real_client):
        r = real_client.get("/search", params={"q": "paracetamol"})
        assert r.status_code == 200
        assert 'href="/d/paracetamol-500mg-tablet"' in r.text
        about = real_client.get("/about").text
        assert "2709 formulation slugs" in about
