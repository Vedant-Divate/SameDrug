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
templates/ and static/ stay empty until the Phase 5 UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from samedrug.app import queries

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

    return app


app = create_app()
