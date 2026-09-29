# Trading-track backend (FastAPI + CCXT)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY pytest.ini .

# Data dir for the SQLite DB (mount a volume here to persist).
RUN mkdir -p /data
ENV DATABASE_URL=sqlite:////data/tranding_track.db

EXPOSE 8000

# Single worker: the in-process monitor loop and paper wallet are per-process
# state, so do NOT scale this to multiple workers without externalising them.
# Railway (and most PaaS) inject $PORT at runtime; fall back to 8000 locally.
# Shell form so ${PORT} is expanded at runtime.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
