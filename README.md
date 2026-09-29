# Trading-track

A professional crypto trading bot that turns TradingView alerts into real (or
paper) Binance orders, with a deterministic "smart" market analyzer, autonomous
trading, risk controls, and a clean React dashboard.

> **Trading risk.** No bot can guarantee profits or "never lose money." The goal
> here is disciplined risk management — small position sizing, stop-losses,
> exposure caps and a capital-preservation analyzer that would rather sit out
> than force a bad trade. **Paper mode is the default. Test on Binance testnet
> before ever going live.**

## How it works

```
TradingView alert ──▶ /webhook ──▶ risk checks ──▶ Binance order (or paper fill)
                                        │
           deterministic analyzer ──────┘ (autonomous mode: analyses & trades itself)
```

- **Execution path:** a TradingView alert hits the webhook, passes risk checks
  (sizing, max positions, daily loss limit, total-exposure cap), then either
  places a real Binance market order or a simulated paper fill. Paper is the
  default.
- **The "brain" is deterministic** (`app/analysis.py`): a transparent,
  multi-signal analyzer (EMA trend stack, RSI + MACD momentum, ATR
  volatility, recent return) that produces one confidence-scored verdict with a
  human-readable reason for every component. When signals disagree or
  volatility is extreme it returns **HOLD** — "no trade" is a valid decision.
- **Optional LLM narration + assessment** (`app/ai.py`) can *explain* the
  analysis and produce a deeper risk-first assessment (signal quality, risks,
  bull/bear scenarios, position-sizing sanity check), but it **never decides
  trades**. Works with either an OpenAI-compatible or an Anthropic-native
  (Claude) provider — pick via `AI_API_STYLE` (auto-detected by default).
- **Autonomous trading** is off by default, long-only, and confidence-gated.

## Multi-user, licensing & admin

Trading-track is **multi-tenant**: each client signs up with a **username,
email, password and the licence key you gave them** — redeeming the key
activates the account **instantly**, no manual approval step. Each user brings
their **own trade-only Binance keys** (stored encrypted at rest), gets their
**own isolated trading engine** (separate positions, paper wallet and settings),
and a **unique, unguessable TradingView webhook URL** that authenticates alerts
to their account. The LLM assistant is **app-wide** — one operator-provided key
shared by every user — not per-user.

- **Auth:** log in with **username _or_ email** + password. Passwords are hashed
  with PBKDF2-HMAC-SHA256 (stdlib). Sessions use HS256 JWTs signed with
  `SECRET_KEY`; the frontend sends `Authorization: Bearer <token>`.
- **Licence keys:** mint single-use keys in the **Admin** dashboard — give each
  a client label and a **duration in days** (blank = lifetime). The full key is
  shown **once** (only its SHA-256 hash is stored), so copy it immediately. A
  client pastes the key at signup to go live at once; a redeemed key **cannot be
  reused**, and keys are random per client so no two clients' data can mix.
  Time-limited licences count down and expire automatically; **+ Days** extends
  (or restarts) them. `AUTO_LICENSE_NEW_USERS=true` bypasses keys entirely (every
  signup auto-activates) — handy for a single-tenant or dev server.
- **Admin:** the account whose email equals `ADMIN_EMAIL` is auto-promoted to
  admin (or, if `ADMIN_EMAIL` is empty, the first registered user). The admin is
  **key-exempt** and gets an **Admin dashboard** to mint/revoke keys, list users,
  grant/revoke/extend licences (revoking immediately stops that user's engine)
  and delete accounts.
- **Key encryption:** users' Binance secrets are encrypted with Fernet, keyed
  off `SECRET_KEY`. **If `SECRET_KEY` is unset, key storage is disabled** (we
  refuse to store secrets we cannot encrypt) and login/signup are disabled — so
  set `SECRET_KEY` in any real deployment.
- **Per-user webhook:** `POST /api/webhook/tradingview/{token}`. The token in the
  URL is the secret — no separate shared secret is needed.

## Features

- **Paper & live modes** — live places REAL Binance orders; testnet supported.
- **Risk management** — per-trade risk %, max open positions, daily loss limit,
  and a portfolio-wide **max total exposure %** cap across all open positions.
- **Exchange-side stop-loss orders** — in live mode a `stop_loss_limit` order is
  placed on Binance so the position is protected even if the bot process is
  down. The in-process SL/TP monitor is the fallback.
- **Trailing stops** — an open long's stop ratchets up as price makes new highs
  (never loosened), locking in gains.
- **Limit orders** — manual orders and TradingView alerts can carry a
  `limit_price`: the order rests until the market reaches it (a buy fills at or
  below, a sell at or above) instead of filling immediately at market. Pending
  orders reserve capital and a position slot, and can be cancelled from the
  dashboard.
- **Multi-timeframe confirmation** — in autonomous mode, set a higher
  "confirm timeframe" (e.g. `4h` while trading `1h`) and the bot refuses to buy
  when the higher timeframe reads bearish, or exit when it reads bullish.
- **Realistic backtesting** — event-driven, orders fill at the *next* bar's open
  (no look-ahead), with configurable fees and slippage; reports total fees.
- **Trainable** — grid-search optimiser with an in-sample/validation split to
  guard against overfitting.
- **Restart-safe** — settings overrides and paper balance persist across
  restarts; on startup in live mode positions are **auto-reconciled** against the
  exchange: a DB long the exchange no longer holds is closed, and a partial
  mismatch shrinks the tracked amount to match (every heal is logged and
  broadcast).
- **Schema migrations** — Alembic is set up (`backend/alembic`) for non-additive
  changes; the app also applies lightweight additive column migrations on startup
  so existing SQLite databases keep working out of the box.
- **API auth** — every REST/WebSocket endpoint requires a valid JWT (see
  "Multi-user" above); users only ever see their own trades, signals and status.
- **Structured logging** — `LOG_FORMAT=json` for log aggregators.
- **Retry/backoff** — transient Binance network errors are retried with
  exponential backoff; auth/insufficient-funds errors fail fast.
- **Geo-resilient market data** — if the primary exchange geo-blocks the server
  (the Binance HTTP 451 "restricted location"), prices, candles, the analyzer and
  paper trading automatically fall back to a public read-only venue
  (`MARKET_DATA_FALLBACK_ID`, default KuCoin) so the dashboard keeps working with
  real data. Orders, balances and reconciliation always stay on the real exchange
  — the fallback never trades. Every price is labelled with the venue that served
  it, so nothing is faked.
- **Telegram notifications** — optional trade/event alerts.

## Project layout

```
backend/    FastAPI + CCXT + SQLAlchemy trading engine and REST/WebSocket API
frontend/   React 18 + TypeScript + Vite dashboard
Dockerfile           Single-service image (builds UI + serves it from FastAPI) — used by Railway
railway.json         Railway build/deploy config (Dockerfile builder, healthcheck)
docker-compose.yml   Full stack (API on :8000, dashboard on :8080)
```

## Running locally

### Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt
# Create backend/.env with your settings (see "Configuration" below).
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev        # dev server
npm run build      # production build
```

### Docker (full stack)

```bash
# Put your keys in backend/.env first.
docker compose up -d --build
# Dashboard: http://localhost:8080   API: http://localhost:8000
```

There is also a **root `Dockerfile`** that builds the dashboard and serves it
from the FastAPI process as ONE service (same origin, no CORS) — this is what
Railway uses (see below). Run it locally with:

```bash
docker build -t trading-track . && docker run -p 8000:8000 --env-file backend/.env trading-track
# Everything on http://localhost:8000
```

## Deploy on Railway

This repo deploys to [Railway](https://railway.app) as a **single service** —
the root `Dockerfile` builds the React dashboard and the FastAPI backend serves
it from the same process, so REST, WebSocket and the UI all share one domain
(no CORS, one always-warm container = no cold-start lag).

**Why the GitHub pull was failing:** Railway's default (Nixpacks) builder
inspects the repo root and finds *both* a Python backend and a Node frontend in
subfolders with **no root build config**, so it can't decide how to build and
errors out. The added root `Dockerfile` + `railway.json` tell Railway exactly
how to build, which fixes the pull/build.

Steps:

1. Push this repo to GitHub (already the `origin`).
2. Railway → **New Project → Deploy from GitHub repo** → pick this repo. It
   detects `railway.json` and builds the root `Dockerfile` (builder =
   `DOCKERFILE`). No root/subfolder selection needed.
3. In the service **Variables** tab set at least:
   - `SECRET_KEY` (**required** — signs JWTs and encrypts users' API keys; use a
     long random value. Without it, login and key storage are disabled.)
   - `ADMIN_EMAIL` (the account promoted to admin; grants licences to others)
   - `TRADING_MODE` (`paper` to start; `live` only when ready)
   - `AUTO_LICENSE_NEW_USERS` (`false` — the default — makes clients redeem a
     licence key at signup; `true` auto-activates every signup, no key needed)
   - Optional global fallbacks: `BINANCE_TESTNET`, AI vars (`AI_API_KEY`,
     `AI_BASE_URL`, `AI_MODEL`, `AI_API_STYLE`). Each user supplies their own
     Binance keys via the dashboard; the AI key is app-wide (operator-set).
   - Do **not** set `PORT` — Railway injects it and the app binds to it.
4. (Recommended) Add a **Volume** mounted at `/data` so the SQLite DB (users,
   encrypted keys, trades, settings) survives redeploys. The image already
   points `DATABASE_URL` there.
5. Deploy. Healthcheck is `GET /api/health`; the dashboard is the service root
   `/`. On startup in live mode the logs run the exchange trading-access
   self-check and warn loudly if the key can't trade.

Notes:
- The bot runs a **single worker** (in-process monitor loop + paper wallet are
  per-process state) — do not scale replicas without externalising that state.
- SQLite on a volume is fine for one instance; move `DATABASE_URL` to managed
  Postgres if you later need multiple instances.

## Configuration

All settings load from environment / `backend/.env`. Key variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `paper` | `paper` (simulated) or `live` (REAL orders) |
| `SECRET_KEY` | – | **Required in production.** Signs JWTs and encrypts users' API keys. Empty = login & key storage disabled |
| `ADMIN_EMAIL` | – | Account auto-promoted to admin (empty = first registered user) |
| `AUTO_LICENSE_NEW_USERS` | `false` | `false` = clients redeem a licence key at signup; `true` = auto-activate every signup |
| `ACCESS_TOKEN_TTL_MINUTES` | `10080` | JWT lifetime (default 7 days) |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | – | Optional global fallback (users normally set their own) |
| `BINANCE_TESTNET` | `true` | Use Binance testnet |
| `EXCHANGE_ID` | `binance` | ccxt exchange id for trading + primary data (e.g. `binanceus` for US) |
| `EXCHANGE_HTTP_PROXY` | – | Route exchange traffic through a proxy in a supported region (fixes geo-block for LIVE trading too) |
| `MARKET_DATA_FALLBACK_ID` | `kucoin` | Read-only venue for prices/candles used **only** when the primary is geo-blocked/unreachable; empty = off. Never used for orders |
| `MAX_OPEN_POSITIONS` | `5` | Max concurrent positions |
| `RISK_PER_TRADE_PCT` | `1.0` | Equity risked per trade |
| `DAILY_LOSS_LIMIT_PCT` | `5.0` | Halt new trades after this daily loss |
| `DEFAULT_STOP_LOSS_PCT` / `DEFAULT_TAKE_PROFIT_PCT` | `2.0` / `4.0` | Default SL/TP |
| `MAX_TOTAL_EXPOSURE_PCT` | `0` (off) | Cap on total open notional vs equity |
| `TRAILING_STOP_PCT` | `0` (off) | Trailing-stop distance |
| `MIN_SIGNAL_CONFIDENCE` | `0.5` | Autonomous trades require this confidence |
| `AUTO_TRADE_ENABLED` | `false` | Enable autonomous trading |
| `AUTO_SYMBOLS` | `BTC/USDT` | Symbols the bot analyses/trades |
| `AUTO_TIMEFRAME` | `1h` | Timeframe for autonomous analysis |
| `AUTO_CONFIRM_TIMEFRAME` | – | Higher timeframe that must agree (blank = off) |
| `AI_API_KEY` | – | Enables the optional LLM commentary/assessment layer |
| `AI_BASE_URL` | `https://api.openai.com/v1` | Provider base URL (OpenAI- or Anthropic-style) |
| `AI_MODEL` | `gpt-4o-mini` | Model name (e.g. `gpt-4o-mini` or `claude-opus-4-8`) |
| `AI_API_STYLE` | `auto` | `auto` / `openai` / `anthropic` — auto infers from the model/URL |
| `AI_MAX_TOKENS` | `1024` | Token budget for AI replies (more = deeper reasoning) |
| `LOG_FORMAT` / `LOG_LEVEL` | `text` / `INFO` | `json` for structured logs |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Optional notifications |

## TradingView alert format

Set the alert webhook URL to `http(s)://<host>/api/webhook/tradingview/<your-token>`
(shown in your dashboard Settings; unique to your account). The token in the URL
authenticates the alert, so the message body needs no secret:

```json
{ "action": "buy", "symbol": "BTC/USDT", "amount": 0.001 }
```

`action` is `buy`, `sell`, or `close`. `amount` is optional (falls back to
risk-based sizing). Add `"limit_price": 61000` to rest a limit order that only
fills when the market reaches that price. Keep your webhook URL private — treat it like a password.

## Database migrations (Alembic)

The app creates tables and applies additive column migrations automatically on
startup, so nothing is required for normal use. For schema changes that additive
migrations can't handle, use Alembic from the `backend` directory (it reads
`DATABASE_URL` from your `.env`):

```bash
cd backend
.venv/Scripts/python -m alembic upgrade head              # apply migrations
.venv/Scripts/python -m alembic revision --autogenerate -m change   # create one
```

## Testing

```bash
cd backend && .venv/Scripts/python -m pytest -q     # backend unit/API tests
cd frontend && npm run build                        # type-check + build
```

## Security notes

- **Set `SECRET_KEY`** before exposing the service — it signs JWTs and encrypts
  every user's exchange keys at rest. Without it, login and key storage are
  disabled by design. All endpoints require a valid JWT; users are isolated to
  their own data.
- **Custodial risk:** storing other people's exchange keys is sensitive. Keys
  are encrypted with Fernet (never logged or returned), and users are told to
  create **trade-only** keys (no withdrawal permission). Always serve over HTTPS.
- Never commit `backend/.env` or real API keys.
- Live mode places real orders — start on testnet and with small size.
