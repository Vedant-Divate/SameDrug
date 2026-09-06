"""Phase-5.7 empty-state + print tests (additive only).

The print block is asserted in the served detail HTML and in the served
stylesheet; the "Try:" chips and the 404 illustration are asserted on
their pages. No existing test file is touched.
"""

from __future__ import annotations


def test_print_block_present_in_detail_template(client):
    key = "syntheticamycin:250mg|tablet"
    html = client.get(f"/d/{client.app.state.key_to_slug[key]}").text
    assert "print-only" in html
    assert "verify with your pharmacist" in html
    css = client.get("/static/style.css").text
    assert "@media print" in css
    assert ".print-only" in css


def test_no_results_page_has_try_chips(client):
    html = client.get("/search", params={"q": "zzzzzqqqq"}).text
    assert "No matches" in html
    assert "empty-art" in html
    assert "Try:" in html
    for molecule in ("paracetamol", "atorvastatin", "amoxicillin"):
        assert f'href="/search?q={molecule}"' in html


def test_404_page_has_illustration_and_search_box(client):
    r = client.get("/d/no-such-slug")
    assert r.status_code == 404
    assert "empty-art" in r.text
    assert 'action="/search"' in r.text
