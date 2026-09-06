"""Phase-5.7 about-page tests: ladder chart + savings histogram (additive only).

All expectations are computed from the DB under test (fixture or real),
never hardcoded — except the real-DB locks that follow the Phase-4
skip pattern in tests/ui/test_ui.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from samedrug.app import queries
from samedrug.app import ui as ui_module
from samedrug.app.main import create_app

REAL_DB = Path(__file__).parent.parent.parent / "data" / "processed" / "drugs.db"
needs_real_db = pytest.mark.skipif(
    not REAL_DB.exists(), reason="real drugs.db absent (CI skips)"
)


def _ladder_counts(html: str) -> list[int]:
    return [int(v) for v in re.findall(r'data-ladder-count="(\d+)"', html)]


def _hist_counts(html: str) -> list[int]:
    return [int(v) for v in re.findall(r'data-hist-count="(\d+)"', html)]


def test_about_ladder_renders_fixture_counts(client, fixture_db):
    stats = queries.get_stats(fixture_db)
    cfg = stats["match_ladder"]
    bars, _ = ui_module.match_ladder(stats)
    html = client.get("/about").text
    for label in ("Naive baseline", "Exact", "+ Alias", "+ Fuzzy", "+ Form family"):
        assert label in html
    assert _ladder_counts(html) == [b["value"] for b in bars]
    assert f"0 \u2013 {cfg['total_nppa_keys']}" in html


def test_about_histogram_buckets_sum_to_equivalents_total(client, fixture_db):
    html = client.get("/about").text
    counts = _hist_counts(html)
    assert len(counts) == 5
    (total,) = re.findall(r'data-hist-total="(\d+)"', html)
    assert sum(counts) == int(total)
    with queries.connect_ro(fixture_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM equivalents").fetchone()[0]
    assert int(total) == n


@pytest.mark.integration
@needs_real_db
class TestRealDbAboutLocks:
    @pytest.fixture()
    def real_client(self):
        from starlette.testclient import TestClient

        return TestClient(create_app(REAL_DB))

    def test_ladder_measured_values(self, real_client):
        html = real_client.get("/about").text
        assert _ladder_counts(html) == [0, 243, 304, 309, 321]
        assert "0 \u2013 867" in html

    def test_histogram_measured_values(self, real_client):
        html = real_client.get("/about").text
        assert _hist_counts(html) == [2, 10, 133, 92, 65]

    def test_known_exceptions_render(self, real_client):
        html = real_client.get("/about").text
        assert "Pheniramine" in html
        assert "Dexamethasone" in html
        assert "above the ceiling" in html
        assert "tax" in html.lower()
