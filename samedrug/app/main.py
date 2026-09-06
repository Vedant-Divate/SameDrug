"""SameDrug read API (Phase 4) — pure read path over data/processed/drugs.db.

Endpoints (JSON only; ``match_key`` path params must be URL-encoded since
keys contain "|" and ":"):

  GET /health                  service + table counts (deploy healthcheck)
  GET /api/search?q=&limit=    tiered molecule-safe search
                               (exact | prefix | substring | fuzzy)
  GET /api/equivalents/{key}   ceiling-vs-JAP provenance + caveats
  GET /api/stats               match ladder, savings, reconciliation-safe
  GET /api/jap/{product_id}    single JAP product + equivalence summary

Every success response carries ``generated_at`` (ISO-8601 UTC) and
``data_as_on`` (equivalents.computed_at). Errors are structured
``{"detail": {...}}`` bodies. The DB is opened read-only (mode=ro URI);
HTML pages (/, /search, /d/{slug}, /about) are served by samedrug.app.ui
over the same query layer; /static serves the single CSS file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from samedrug.app import queries
from samedrug.app import ui as ui_module

DEFAULT_DB_PATH = queries.DEFAULT_DB_PATH


def _envelope(payload: dict[str, Any], data_as_on: str | None) -> dict[str, Any]:
    return {**payload, "generated_at": queries.utcnow(), "data_as_on": data_as_on}


def create_app(db_path: str | Path = DEFAULT_DB_PATH) -> FastAPI:
    """App factory — tests inject a fixture-built DB via ``db_path``."""
    db_path = Path(db_path)
    app = FastAPI(title="SameDrug", version="0.1.0")

    async def _structured_http_exception(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        try:
            with queries.connect_ro(db_path) as conn:
                data_as_on = queries.get_data_as_on(conn)
        except Exception:
            data_as_on = None
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope({"detail": detail}, data_as_on),
        )

    app.add_exception_handler(HTTPException, _structured_http_exception)  # type: ignore[arg-type]

    # Phase 5 UI: slug map built once at startup from canonical keys; the
    # DB may be absent in tooling contexts, so tolerate that with empty maps
    # (JSON endpoints report their own errors per request).
    try:
        with queries.connect_ro(db_path) as _conn:
            _keys = [
                r[0]
                for r in _conn.execute(
                    "SELECT match_key FROM canonical_formulations"
                ).fetchall()
            ]
        _slug_to_key, _key_to_slug, _collisions = ui_module.build_slug_map(_keys)
    except Exception:
        _slug_to_key, _key_to_slug, _collisions = {}, {}, 0
    app.state.slug_to_key = _slug_to_key
    app.state.key_to_slug = _key_to_slug
    app.state.slug_collisions = _collisions
    app.state.ui_db_path = db_path
    app.state.templates = ui_module.make_templates()
    app.mount("/static", StaticFiles(directory=ui_module.STATIC_DIR), name="static")
    app.include_router(ui_module.router)

    @app.get("/api/search")
    def search(
        q: str = Query(default=""),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Tiered search; 400 below 2 chars (wrong answers worse than 404s)."""
        if len(q.strip()) < 2:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "query_too_short",
                    "message": "q must be at least 2 characters",
                },
            )
        with queries.connect_ro(db_path) as conn:
            results = queries.search(conn, q, limit)
            return _envelope(
                {"query": q, "limit": limit, "results": results},
                queries.get_data_as_on(conn),
            )

    @app.get("/api/equivalents/{match_key:path}")
    def equivalents(match_key: str) -> dict[str, Any]:
        """Ceiling-vs-JAP provenance for one canonical key (URL-encoded).

        Cheapest equivalents row wins when a key has several. 404 with a
        structured detail body when the key is unknown.
        """
        with queries.connect_ro(db_path) as conn:
            payload = queries.get_equivalent(conn, match_key)
            if payload is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "unknown_match_key", "match_key": match_key},
                )
            return _envelope(payload, queries.get_data_as_on(conn))

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        """Match ladder, method distribution, and savings (cached in-process).

        The method_distribution sum always equals equivalents_count; the
        process-start cache means a DB rebuild requires an app restart.
        """
        stats_payload = queries.get_stats(db_path)
        return _envelope(dict(stats_payload), stats_payload["data_as_on"])

    @app.get("/api/jap/{product_id}")
    def jap_lookup(product_id: int) -> dict[str, Any]:
        """Single JAP product + match/equivalence summary (404 if unknown).

        Zero-MRP rows ("price not yet published") are returned with
        ``excluded_reason``, never silently omitted.
        """
        with queries.connect_ro(db_path) as conn:
            payload = queries.get_jap_product(conn, product_id)
            if payload is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "unknown_product_id", "product_id": product_id},
                )
            return _envelope(payload, queries.get_data_as_on(conn))

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Deploy healthcheck: status + per-table counts (one query each)."""
        with queries.connect_ro(db_path) as conn:
            counts = queries.table_counts(conn)
            return _envelope(
                {
                    "status": "ok",
                    "db": counts,
                    "db_stats_cached": queries.stats_cache_populated(db_path),
                },
                queries.get_data_as_on(conn),
            )

    return app


app = create_app()
