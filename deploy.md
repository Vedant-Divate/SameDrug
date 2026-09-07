# deploy.md — SameDrug deployment runbook (human steps)

Deployment itself is a **human** action: this file documents it, you execute
it. Nothing here needs automation, secrets, or cloud CLIs on your machine.

## Pre-reqs

- Repo pushed to `main` with Actions green (`ci` lint-and-test).
- GHCR image exists: `ghcr.io/vedant-divate/samedrug:latest` (built by
  `.github/workflows/docker.yml` on the last `main` push; version tags
  `v*` on releases).

## Render (primary target — free tier, Docker runtime)

1. Create a Render account, **New → Web Service**, connect the
   `Vedant-Divate/SameDrug` repo.
2. Runtime: **Docker** (auto-detected from the repo `Dockerfile`).
3. Instance type: **Free**. Health check path: **`/health`**.
4. Deploy. No environment variables needed — all data is baked into the
   image, and the container honors Render's injected `$PORT` (falls back
   to 8000 locally).
5. Expect free-tier spin-down: after ~15 min idle the service sleeps and
   the next visit cold-starts in ~30 s. This is Render behavior, not a bug.

## Environment

No secrets. The image carries code + `data/processed/drugs.db`; there is
no database URL, API key, or config to set. `PORT` is the only variable
the container reads, and the platform provides it.

## Alternative targets

- **Fly.io:** `fly launch` in the repo (accept the detected Dockerfile),
  then `fly deploy`. Image equivalent to GHCR `:latest`.
- **Hugging Face Spaces:** new Space with the **Docker** SDK, push this
  repo content; note Spaces expects port **7860** — set `PORT=7860` in
  the Space settings (the `CMD` already honors `$PORT`).
- **Self-hosted VPS:** `docker run -d -p 8000:8000
  ghcr.io/vedant-divate/samedrug:latest` behind any reverse proxy.

## Post-deploy verification checklist

- `GET /health` → 200 with `jap_products: 2439`,
  `nppa_ceiling_prices: 936`, `equivalents: 302`.
- `/d/paracetamol-500mg-tablet` renders the card (₹0.93 ceiling,
  ₹0.66 Jan Aushadhi, ~29.5% less).
- `/about` shows the match ladder (0/243/304/309/321 of 867).
- Mobile viewport: card stacks to one column, no horizontal scroll.

## Rollback

Redeploy the previous image tag (Render: Manual Deploy → select the
earlier commit; VPS: `docker run … :<previous-tag>`). Every GHCR tag is
immutable, so rollback is just re-pointing.

## Refresh note (Phase 6.5)

Post-deploy data updates are manual for now: re-run
`samedrug/pipeline/build_db.py` locally with fresh seeds (JAP live pull
via `--fetch`, NPPA via manual IPDMS export per `docs/sources.md`),
commit the rebuilt `data/processed/drugs.db`, push — CI rebuilds the
image, then redeploy. Weekly automation with staleness alarms comes later.
