"""SameDrug server-rendered UI (Phase 5) — Jinja2 + one CSS file, zero JS.

Pure presentation over the Phase-4 query layer: every number on screen
comes from :mod:`samedrug.app.queries` (never re-implemented here), the
DB is only ever opened read-only, and all pages work with JS disabled.

Slug contract: ``slugify(match_key)`` over every canonical key at app
startup. Measured on the real DB (2,709 keys): exactly 2 collisions
(``povidone iodine`` vs ``povidone-iodine``; two silk-suture rows), so
collisions are disambiguated deterministically (``-2`` suffix, sorted
order) and the count is surfaced on /about and in tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jinja2
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from slugify import slugify

from samedrug.app import queries

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

SEARCH_LIMIT = 50

# Raw caveat codes (from queries.build_caveats) -> plain-English UI text.
# The relaxed code carries a ": verify dosage form" suffix, so lookup is
# done on the prefix before ":".
CAVEAT_TEXT = {
    "form_or_modifier_relaxed": "Dosage form may differ \u2014 verify with your pharmacist",
    "low_match_confidence": "Lower-confidence match \u2014 verify with your pharmacist",
    "jap_price_above_ceiling": (
        "Jan Aushadhi price is ABOVE the ceiling \u2014 see details below"
    ),
    "possible_tax_basis_difference": (
        "Prices may not be directly comparable (tax basis: "
        "ceilings are tax-exclusive, MRP is tax-inclusive)"
    ),
}


def slug_for(match_key: str) -> str:
    """Base slug for a canonical key (collisions handled in build_slug_map)."""
    return slugify(match_key) or "formulation"


def build_slug_map(
    match_keys: Iterable[str],
) -> tuple[dict[str, str], dict[str, str], int]:
    """Deterministic slug <-> match_key maps.

    Keys are processed in sorted order; the first key claims the base
    slug and later colliding keys get ``-2``, ``-3``, ... Returns
    ``(slug_to_key, key_to_slug, collision_count)``.
    """
    slug_to_key: dict[str, str] = {}
    key_to_slug: dict[str, str] = {}
    collisions = 0
    for key in sorted(match_keys):
        base = slug_for(key)
        slug, n = base, 2
        while slug in slug_to_key:
            slug = f"{base}-{n}"
            n += 1
        if slug != base:
            collisions += 1
        slug_to_key[slug] = key
        key_to_slug[key] = slug
    return slug_to_key, key_to_slug, collisions


def plain_caveat(code: str) -> str:
    """Map one raw caveat code to its plain-English warning text."""
    return CAVEAT_TEXT.get(code.split(":")[0], code)


def format_inr(value: float | None) -> str:
    """Display rounding: exactly 2 decimals (0.6559999 -> \u20b90.66)."""
    return f"\u20b9{float(value):.2f}"


def format_pct1(value: float | None) -> str:
    """Savings display: exactly 1 decimal."""
    return f"{float(value):.1f}%"


def display_name(molecules: str, strength: str, form: str) -> str:
    """Human header for a canonical triple (combo '+' gets spaces)."""
    mols = " + ".join(m.strip().title() for m in molecules.split("+"))
    strength_disp = " + ".join(s.strip() for s in strength.split("+"))
    return f"{mols} {strength_disp} {form}".strip()


def savings_hero(savings_pct: float) -> str:
    """The hero number: '~29.5% less at Jan Aushadhi' (or above-ceiling)."""
    if savings_pct >= 0:
        return f"~{format_pct1(savings_pct)} less at Jan Aushadhi"
    return f"~{format_pct1(abs(savings_pct))} MORE at Jan Aushadhi \u2014 above the ceiling"


def make_templates() -> Jinja2Templates:
    """Jinja env with explicit autoescape for all HTML templates + filters."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html", "htm", "xml"]),
    )
    env.filters["inr"] = format_inr
    env.filters["pct1"] = format_pct1
    return Jinja2Templates(env=env)


router = APIRouter()


def _ui_state(request: Request) -> dict[str, Any]:
    state = request.app.state
    return {
        "templates": state.templates,
        "db_path": state.ui_db_path,
        "slug_to_key": state.slug_to_key,
        "key_to_slug": state.key_to_slug,
    }


def _base_context(request: Request, data_as_on: str | None) -> dict[str, Any]:
    return {"request": request, "data_as_on": data_as_on}


@router.get("/", response_class=None)
def home(request: Request):
    """Home: search box, one-line pitch, counts, disclaimers footer."""
    ui = _ui_state(request)
    with queries.connect_ro(ui["db_path"]) as conn:
        counts = queries.table_counts(conn)
        data_as_on = queries.get_data_as_on(conn)
        n_equiv_keys = conn.execute(
            "SELECT COUNT(DISTINCT match_key) FROM equivalents"
        ).fetchone()[0]
    return ui["templates"].TemplateResponse(
        request,
        "home.html",
        {
            **_base_context(request, data_as_on),
            "counts": counts,
            "n_equiv_keys": n_equiv_keys,
        },
    )


@router.get("/search", response_class=None)
def search_page(request: Request, q: str = "", limit: int = SEARCH_LIMIT):
    """Tiered results over canonical keys (same query fn as the JSON API).

    Exact/prefix/substring render in the main section; fuzzy renders in a
    separate, visually distinct 'Similar names' section below. Empty
    results and short queries are friendly 200s, never 404s.
    """
    ui = _ui_state(request)
    query = (q or "").strip()
    context: dict[str, Any] = {**_base_context(request, None), "query": query}
    if len(query) < 2:
        with queries.connect_ro(ui["db_path"]) as conn:
            context["data_as_on"] = queries.get_data_as_on(conn)
        context["too_short"] = True
        return ui["templates"].TemplateResponse(request, "search.html", context)
    with queries.connect_ro(ui["db_path"]) as conn:
        results = queries.search(conn, query, limit)
        context["data_as_on"] = queries.get_data_as_on(conn)
    main = [r for r in results if r["tier"] != "fuzzy"]
    fuzzy = [r for r in results if r["tier"] == "fuzzy"]
    context.update(
        {
            "too_short": False,
            "main_results": main,
            "fuzzy_results": fuzzy,
            "key_to_slug": ui["key_to_slug"],
        }
    )
    return ui["templates"].TemplateResponse(request, "search.html", context)


@router.get("/d/{slug}", response_class=None)
def detail_card(request: Request, slug: str):
    """The shareable card: header, NPPA vs JAP, savings hero, caveats,
    provenance footer, OpenGraph tags. Unknown slug (or slug with no
    equivalence rows) -> friendly 404 with a search box."""
    ui = _ui_state(request)
    match_key = ui["slug_to_key"].get(slug)
    payload = None
    data_as_on = None
    with queries.connect_ro(ui["db_path"]) as conn:
        data_as_on = queries.get_data_as_on(conn)
        if match_key is not None:
            payload = queries.get_equivalent(conn, match_key)
    if match_key is None or payload is None:
        return ui["templates"].TemplateResponse(
            request,
            "not_found.html",
            {**_base_context(request, data_as_on), "slug": slug},
            status_code=404,
        )
    nppa = payload["nppa_side"]
    jap = payload["jap_side"]
    title = display_name(payload["molecules"], payload["strength_set"], payload["form"])
    og_title = (
        f"{title} \u2014 {format_inr(nppa['ceiling_per_unit_inr'])} ceiling "
        f"vs {format_inr(jap['per_unit_inr'])} Jan Aushadhi"
    )
    if payload["savings_pct"] >= 0:
        og_description = (
            f"Save {format_pct1(payload['savings_pct'])} "
            f"({savings_hero(payload['savings_pct'])}) on {title}. "
            "Every price traceable to source."
        )
    else:
        og_description = (
            f"{title} is priced {format_pct1(abs(payload['savings_pct']))} "
            "above the ceiling at Jan Aushadhi \u2014 see details. "
            "Every price traceable to source."
        )
    return ui["templates"].TemplateResponse(
        request,
        "detail.html",
        {
            **_base_context(request, data_as_on),
            "payload": payload,
            "title": title,
            "hero": savings_hero(payload["savings_pct"]),
            "caveats_plain": [plain_caveat(c) for c in payload["caveats"]],
            "ceiling_raw": repr(nppa["ceiling_per_unit_inr"]),
            "jap_raw": repr(jap["per_unit_inr"]),
            "savings_raw": repr(payload["savings_pct"]),
            "mrp_raw": repr(jap["mrp"]),
            "og_title": og_title,
            "og_description": og_description,
        },
    )


@router.get("/about", response_class=None)
def about(request: Request):
    """Disclaimers + methodology + data sources + refresh dates."""
    ui = _ui_state(request)
    with queries.connect_ro(ui["db_path"]) as conn:
        data_as_on = queries.get_data_as_on(conn)
        counts = queries.table_counts(conn)
    return ui["templates"].TemplateResponse(
        request,
        "about.html",
        {
            **_base_context(request, data_as_on),
            "counts": counts,
            "slug_collisions": request.app.state.slug_collisions,
            "n_slugs": len(request.app.state.slug_to_key),
        },
    )
