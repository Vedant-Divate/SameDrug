"""Phase-5.7 motion tests: count-up static fallback (additive only).

TestClient executes no JavaScript, so every assertion here doubles as the
no-JS degradation proof: the final savings value must already be in the
HTML, with the JS hook present as pure decoration.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).parent.parent.parent / "samedrug" / "app" / "static" / "app.js"


def test_countup_has_static_server_rendered_fallback(client, fixture_db):
    import sqlite3

    key = "syntheticamycin:250mg|tablet"
    html = client.get(f"/d/{client.app.state.key_to_slug[key]}").text
    conn = sqlite3.connect(fixture_db)
    row = conn.execute(
        "SELECT savings_pct FROM equivalents WHERE match_key = ?", (key,)
    ).fetchone()
    conn.close()
    # Static fallback: the exact final hero text, visible with JS disabled.
    assert f"~{row[0]:.1f}% less at Jan Aushadhi" in html
    # Decoration hook: the raw value JS animates from.
    (raw,) = re.findall(r'data-countup="([^"]+)"', html)
    assert float(raw) == row[0]


def test_countup_js_is_600ms_reduced_motion_safe():
    src = APP_JS.read_text(encoding="utf-8")
    assert "data-countup" in src
    assert "600" in src
    assert "reduceMotion" in src
    assert "requestAnimationFrame" in src
    assert "tabular-nums" in src or "tabular" in src
    assert len(src.splitlines()) < 200
