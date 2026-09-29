# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Trading-track — SINGLE-SERVICE image for Railway (and any Docker host).
#
# Stage 1 builds the React dashboard. Stage 2 runs the FastAPI backend and
# serves that built dashboard from the SAME process, so the whole bot is one
# fast service: one domain, no CORS hop, same origin for REST + WebSocket, and
# one always-warm container. Railway injects $PORT at runtime.
#
# Railway: New Project -> Deploy from GitHub repo -> it detects THIS Dockerfile
# at the repo root and builds it. Set env vars in the service Variables tab
# (see README "Deploy on Railway"). No root build config is needed beyond this.
# ---------------------------------------------------------------------------

# ---- Stage 1: build the frontend ----
FROM node:20-alpine AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: backend + bundled dashboard ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps first for better layer caching.
COPY backend/requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY backend/app ./app
COPY backend/pytest.ini .
COPY backend/alembic.ini .
COPY backend/alembic ./alembic

# Copy the built dashboard so FastAPI can serve it (see app/main.py static mount).
COPY --from=frontend /web/dist ./static

# Persist the SQLite DB on a mounted volume in production (Railway: add a Volume
# mounted at /data). Falls back to this path if DATABASE_URL is unset.
RUN mkdir -p /data
ENV DATABASE_URL=sqlite:////data/tranding_track.db
ENV STATIC_DIR=/app/static

# Railway provides $PORT; default to 8000 for local runs. Shell form so the
# variable is expanded at runtime.
EXPOSE 8000
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
