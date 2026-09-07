# SameDrug — multi-stage image. The DB is baked in at build time, so a
# data refresh means rebuilding the image (see deploy.md, refresh note).
# No .venv, no tests, no data/raw in the final image (see .dockerignore).

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY samedrug/ samedrug/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /install /usr/local
COPY samedrug/ samedrug/
COPY data/processed/drugs.db data/processed/drugs.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"
CMD ["sh", "-c", "uvicorn samedrug.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
