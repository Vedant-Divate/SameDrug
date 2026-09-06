"""Phase-5.6 additive tests: stats strip, bars, JS hooks, theme hook.

New files only — every pre-existing assertion lives untouched in
test_ui.py / tests/api. All expectations are computed from the DB under
test (fixture or real), never hardcoded, except the real-DB locks that
follow the Phase-4 skip pattern.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from samedrug.app import queries
from samedrug.app.main import create_app

REAL_DB = Path(__file__).parent.parent.parent / "data" / "processed" / "drugs.db"
needs_real_db = pytest.mark.skipif(
    not REAL_DB.exists(), reason="real drugs.db absent (CI skips)"
)

APP_JS = Path(__file__).parent.parent.parent / "samedrug" / "app" / "static" / "app.js"


def _stats_for(db_path: Path) -> dict:
    with queries.connect_ro(db_path) as conn:
        counts = queries.table_counts(conn)
    stats = queries.get_stats(db_path)
    return {"counts": counts, "stats": stats}


def test_home_stats_strip_renders_four_real_numbers(client, fixture_db):
    expected = _stats_for(fixture_db)
    html = client.get("/").text
    assert str(expected["counts"]["jap_products"]) in html
    assert "Jan Aushadhi products" in html
    assert str(expected["stats"]["equivalents_count"]) in html
    assert "verified comparisons" in html
    assert f"{expected['stats']['savings']['median']:.1f}%" in html
    assert "median savings" in html
    assert expected["stats"]["data_as_on"] in html
    assert "data as on" in html


def test_card_comparison_bars_present_and_proportional(client, fixture_db):
    key = "syntheticamycin:250mg|tablet"
    html = client.get(f"/d/{client.app.state.key_to_slug[key]}").text
    assert 'class="micro-label"' in html
    assert 'role="img"' in html
    widths = [float(w) for w in re.findall(r'class="bar bar-\w+" style="width: ([\d.]+)%', html)]
    assert len(widths) == 2
    conn = sqlite3.connect(fixture_db)
    row = conn.execute(
        "SELECT nppa_ceiling_per_unit, jap_per_unit FROM equivalents WHERE match_key = ?",
        (key,),
    ).fetchone()
    conn.close()
    ceiling, jap = row
    assert widths[0] == pytest.approx(ceiling / max(ceiling, jap) * 100, abs=0.11)
    assert widths[1] == pytest.approx(jap / max(ceiling, jap) * 100, abs=0.11)
    assert max(widths) == pytest.approx(100.0)


def test_copy_link_degrades_without_js(client):
    """Button + hint are server-rendered (hidden); TestClient runs no JS,
    so every assertion here is also the no-JS functionality proof."""
    key = "syntheticamycin:250mg|tablet"
    html = client.get(f"/d/{client.app.state.key_to_slug[key]}").text
    assert "data-copy-link" in html
    assert re.search(r'<button[^>]*data-copy-link[^>]*hidden[^>]*>', html)
    assert "Copy link to share" in html
    assert '<script src="/static/app.js" defer></script>' in html
    assert client.get("/static/app.js").status_code == 200


def test_dark_mode_toggle_markup_present(client):
    html = client.get("/").text
    assert "data-theme-toggle" in html
    assert re.search(r'<button[^>]*data-theme-toggle[^>]*hidden[^>]*>', html)
    css = client.get("/static/style.css").text
    assert "prefers-color-scheme" in css
    assert '[data-theme="dark"]' in css


def test_search_suggestion_hooks_present(client):
    for url in ("/", "/search"):
        html = client.get(url, params={"q": "syntheti"} if url == "/search" else None).text
        assert 'data-suggest="/api/search"' in html
        assert 'role="combobox"' in html
        assert 'aria-expanded="false"' in html
        assert 'aria-controls="search-suggest-list"' in html


def test_app_js_budget_and_features():
    src = APP_JS.read_text(encoding="utf-8")
    assert len(src.splitlines()) < 200
    for token in (
        "localStorage",
        "navigator.clipboard",
        "250",
        "aria-expanded",
        "aria-activedescendant",
        "prefers-reduced-motion",
        "scrollIntoView",
        "hidden",
    ):
        assert token in src


@pytest.mark.integration
@needs_real_db
class TestRealDbPolishLocks:
    @pytest.fixture()
    def real_client(self):
        from starlette.testclient import TestClient

        return TestClient(create_app(REAL_DB))

    def test_home_stats_strip_real_numbers(self, real_client):
        html = real_client.get("/").text
        assert "2439" in html and "Jan Aushadhi products" in html
        assert "302" in html and "verified comparisons" in html
        assert "61.0%" in html and "median savings" in html
        assert "2026-09-06T07:39:17+00:00" in html

    def test_paracetamol_bars_proportional(self, real_client):
        html = real_client.get("/d/paracetamol-500mg-tablet").text
        widths = [
            float(w) for w in re.findall(r'class="bar bar-\w+" style="width: ([\d.]+)%', html)
        ]
        assert len(widths) == 2
        assert widths[0] == pytest.approx(100.0)
        assert widths[1] == pytest.approx(0.6559999999999999 / 0.93 * 100, abs=0.11)
