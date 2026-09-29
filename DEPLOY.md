# Deployment Guide — Trading-track (Railway, single service)

Operator checklist to take the app live. The FastAPI backend serves the built
React UI from **one** Railway service, so there is a single public URL and no
CORS to fight. Every setting below was taken from `backend/app/config.py`.

> **This is a real-money app.** Two items are unforgiving: the persistent
> **volume** (§1) and **`SECRET_KEY`** (§2). Get either wrong and you lose user
> data or make every stored API key undecryptable.

---

## 1. Add a persistent volume — do this FIRST

Railway → your service → **Volumes** → add a volume with mount path:

```
/data
```

SQLite lives on this volume. The container filesystem is ephemeral, so without
the volume **every redeploy wipes all users, trades, and encrypted keys.**

---

## 2. Environment variables

Railway → **Variables** → **Raw Editor**, then paste the blocks below.

### Tier 1 — Required (the authenticated app 503s without `SECRET_KEY`)

```
SECRET_KEY=<paste your existing key — DO NOT rotate it>
ADMIN_EMAIL=<the email you will log in with>
DATABASE_URL=sqlite:////data/tranding_track.db
AUTO_LICENSE_NEW_USERS=false
TRADING_MODE=paper
```

- `SECRET_KEY` signs logins **and** derives the Fernet key that encrypts every
  user's Binance keys. **Never rotate on a live deploy** — it logs everyone out
  and makes stored keys undecryptable. Need a fresh one for a brand-new deploy?
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `ADMIN_EMAIL` — sign up with this exact email → you become admin → the
  **Admin** tab appears, where you mint licence keys and grant/revoke access.
- `DATABASE_URL` — the four slashes = absolute `/data` path (the volume in §1).
- `AUTO_LICENSE_NEW_USERS=false` (the default) makes clients redeem a licence
  key you minted at signup; `true` auto-activates every signup (no key needed).
- `TRADING_MODE=paper` for testing. New users start in paper regardless.

### Tier 2 — TradingView webhook (set before wiring TradingView)

```
TRADINGVIEW_WEBHOOK_SECRET=<a long random string, NOT "change-me">
CORS_ORIGINS=https://<your-app>.up.railway.app
```

The default secret is literally `change-me`; change it before exposing the
webhook. Single-service means same-origin, but set `CORS_ORIGINS` to your real
URL for correctness.

### Tier 3 — AI (app-wide "inbuilt": users never enter an AI key)

One operator key powers everyone; it is **advisory-only and never trades**.

```
AI_API_KEY=<your one operator key>
AI_API_STYLE=auto
```

- **OpenAI key** → done. Defaults apply (`AI_MODEL=gpt-4o-mini`,
  `AI_BASE_URL=https://api.openai.com/v1`).
- **Anthropic/Claude key** → also add `AI_BASE_URL=https://api.anthropic.com/v1`
  and `AI_MODEL=<a current Claude model>`.
- Omit `AI_API_KEY` entirely and the bot still works — it falls back to the
  deterministic analysis instead of narration.

### Do NOT set these

- **`BINANCE_API_KEY` / `BINANCE_API_SECRET`** — leave unset. Each user enters
  their own keys in the Settings tab, stored encrypted per-user. Global keys
  are ignored per-user.
- **`API_KEY`** — leave unset. Legacy single-user guard; JWT auth protects the
  app in multi-user mode.

### Optional (sensible defaults; also editable per-user in the Settings tab)

Risk: `RISK_PER_TRADE_PCT`, `DEFAULT_STOP_LOSS_PCT`, `DEFAULT_TAKE_PROFIT_PCT`,
`DAILY_LOSS_LIMIT_PCT`, `MAX_OPEN_POSITIONS`, `MAX_TOTAL_EXPOSURE_PCT`,
`TRAILING_STOP_PCT`, `MIN_SIGNAL_CONFIDENCE`.
Autonomous: `AUTO_TRADE_ENABLED` (keep `false` for now), `AUTO_SYMBOLS`,
`AUTO_TIMEFRAME`, `AUTO_CONFIRM_TIMEFRAME`.
Other: `LOG_FORMAT=json`, `LOG_LEVEL=INFO`, `PAPER_STARTING_BALANCE`,
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, `ACCESS_TOKEN_TTL_MINUTES`.

### If Binance geo-blocks your server (HTTP 451)

Binance refuses requests from some server regions (this includes
`testnet.binance.vision`), which otherwise shows up as `502 Bad Gateway` on
`/api/ticker` and a dead dashboard. The app now handles this automatically:

- **Read-only market data** (prices, candles, the analyzer, paper trading) falls
  back to **`MARKET_DATA_FALLBACK_ID`** (default `kucoin`) whenever the primary is
  geo-blocked/unreachable, so the dashboard keeps working with **real** data. Set
  it empty to disable the fallback. Prices are labelled with the venue that
  served them — nothing is faked.
- **Live trading** still needs Binance itself reachable (the fallback never places
  orders or reads balances). For live orders from a blocked region set
  `EXCHANGE_HTTP_PROXY=<proxy URL in a supported region>`, or — US only —
  `EXCHANGE_ID=binanceus`.

---

## 3. Deploy & verify (order matters)

1. Add the `/data` volume (§1).
2. Set the Tier 1 variables (§2), plus Tier 2/3 as needed.
3. Redeploy.
4. Open the site → **Sign up** with the exact `ADMIN_EMAIL` (the admin is
   key-exempt) → confirm the **Admin** tab appears.
5. In **Admin → Licence keys**, mint a key → **Sign up** a second account with
   it → confirm that account is **live instantly** (no manual grant needed).
6. Log in as the test account (by username **or** email) → Settings → add
   Binance **testnet** keys → place a **paper** trade and confirm it opens/closes
   with real prices.

## 4. Licensing model (single-use keys + admin control)

Clients activate themselves with a **licence key** you mint — no manual approval
step in the common case:

- In **Admin → Licence keys**, mint a key: give it a client label and a
  **duration in days** (blank = lifetime). The full key shows **once** (only its
  SHA-256 hash is stored) — copy it and send it to the client.
- The client signs up with **username + email + password + that key** and goes
  **live instantly**. A key is single-use and random per client (so no two
  clients' data can mix), and cannot be redeemed twice.
- Time-limited licences expire on their own; **+ Days** on the user's row extends
  or restarts access. **Revoke** withdraws access immediately (and stops that
  user's engine); **Grant** hands a lifetime licence manually.
- `AUTO_LICENSE_NEW_USERS=true` skips keys entirely — every signup auto-activates
  (single-tenant / dev only).

If `ADMIN_EMAIL` is blank, the **first** account to sign up becomes admin. The
admin is key-exempt. Clients log in afterwards with **either their username or
their email** + password.

## 5. Going live (per user, later)

Live trading is an explicit per-user opt-in in Settings — no one trades real
funds just by being licensed. Before a user flips to live, the app runs a
trading-access self-check (surfaces geo-block / key-permission problems). Keep
`TRADING_MODE=paper` at the server level while validating.

