"""FastAPI application: multi-user REST + WebSocket API for Tranding-track.

Multi-tenant design: anyone can self-sign-up, but an account must be LICENSED by
the administrator before it can configure exchange keys or trade. Each user
brings their OWN trade-only Binance keys (stored encrypted at rest) and gets
their own in-memory :class:`TradingEngine` and a unique TradingView webhook URL.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import __version__
from app.ai import strip_action_tag
from app.backtest import run_backtest, summarize_backtest
from app.config import get_settings
from app.ict import analyze_ict
from app.database import get_db, init_db
from app.deps import get_current_user, require_admin, require_licensed_user
from app.ratelimit import client_ip, limiter
from app.models import (
    LicenseKey,
    LicenseKeyStatus,
    LicenseStatus,
    PriceAlert,
    SignalLog,
    Trade,
    TradeStatus,
    User,
    UserRole,
    WebhookDelivery,
    _as_utc,
    _utcnow,
)
from app.schemas import (
    AddLicenseDays,
    AlertCreate,
    AlertOut,
    AutoConfirmationOut,
    BotStatus,
    CloseAllResult,
    ConfirmationResolveResult,
    CredentialsUpdate,
    ExecutionResult,
    LicenseKeyCreate,
    LicenseKeyCreated,
    LicenseKeyOut,
    LicenseUpdate,
    LoginRequest,
    ManualOrder,
    MeOut,
    OrderBookOut,
    PerformanceOut,
    RedeemLicenseKey,
    ScaledOrder,
    ScaledResult,
    SettingsOut,
    SettingsUpdate,
    SignalOut,
    SignupRequest,
    TickerOut,
    TokenResponse,
    TradeOut,
    TradingViewSignal,
    UserOut,
)
from app.security import (
    create_access_token,
    dummy_verify,
    encrypt_secret,
    hash_license_key,
    hash_password,
    new_license_key,
    new_webhook_token,
    secrets_enabled,
    verify_password,
)
from app.usermgr import get_manager
from app.state import load_monitor_interval, purge_user_state, save_monitor_interval
from app.learn import PARAM_GRIDS, train
from app.news import fetch_market_news
from app.performance import compute_performance
from app.strategies import STRATEGY_REGISTRY, build_strategy
from app.tasks import monitor_loop
from app.ws import Broadcaster
from app.logging_config import configure_logging

configure_logging(get_settings().log_format, get_settings().log_level)
logger = logging.getLogger("tranding_track")


def _upstream_error(
    message: str, exc: Exception, *, status_code: int = 502
) -> HTTPException:
    """Log a full upstream (exchange/ccxt) exception server-side, return a clean
    HTTPException for the client.

    Raw exchange/ccxt error text can leak internals to the caller — request URLs,
    ccxt/exchange class names, account-permission hints, occasionally fragments of
    the outgoing request. The operator still needs the real cause, so the full
    exception (with its type) goes to the server log; the client only ever sees
    the stable, generic ``message``. Never embeds the raw ``exc`` in the response.
    """
    logger.warning("%s: %s: %s", message, type(exc).__name__, exc)
    return HTTPException(status_code=status_code, detail=message)


# Per-user webhook path template. The unguessable token in the URL is what
# authenticates the alert to a specific user's account.
WEBHOOK_PATH_TEMPLATE = "/api/webhook/tradingview/{token}"

broadcaster = Broadcaster()


def _webhook_path(token: str) -> str:
    return WEBHOOK_PATH_TEMPLATE.format(token=token)


def _engine_for(db: Session, user: User):
    return get_manager().get(db, user)


def _analysis_for(engine, symbol: str, timeframe: str = "1h", limit: int = 200):
    """Fetch candles via a user's connector and run deterministic analysis.

    Returns ``(analysis, ict)`` where ``ict`` is a real ICT/smart-money read on
    the SAME closed frame (or ``None`` when the ICT lens is turned off for this
    user). Both are computed from one candle fetch so the ICT structure lines up
    exactly with the deterministic verdict the operator sees.
    """
    try:
        raw = engine.connector.fetch_ohlcv(symbol.upper(), timeframe, max(1, min(limit, 1000)))
    except Exception as exc:
        raise _upstream_error("OHLCV unavailable", exc)
    if not raw:
        raise HTTPException(status_code=502, detail="No candle data returned")
    df = pd.DataFrame(
        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    # Use THIS user's analyzer so the verdict honours their configured
    # min_signal_confidence, not a module-global default. analyze_live decides
    # on CLOSED bars only (dropping the still-forming candle) so the verdict
    # cannot repaint, and stamps the live price back on for display.
    analysis, closed = engine.analyzer.analyze_live(df, symbol.upper())
    # The ICT read runs on the SAME closed frame (no repaint) and reuses the
    # live price the analyzer stamped on, so premium/discount is measured against
    # where price actually is. Best-effort: a read failure must never break the
    # deterministic verdict — degrade to no ICT rather than 500.
    ict = None
    if getattr(engine.settings, "ict_enabled", True):
        try:
            ict = analyze_ict(closed, symbol=symbol.upper(), price=analysis.price)
        except Exception:
            logger.exception("ICT read failed for %s", symbol)
            ict = None
    return analysis, ict


def _bootstrap_admin(db: Session) -> None:
    """Ensure an admin exists: promote ADMIN_EMAIL, else the first user."""
    admin_email = (get_settings().admin_email or "").strip().lower()
    if admin_email:
        user = db.scalars(select(User).where(User.email == admin_email)).first()
        if user and user.role != UserRole.admin.value:
            user.role = UserRole.admin.value
            user.license_status = LicenseStatus.active.value
            db.commit()
            logger.info("Promoted %s to admin", admin_email)
    else:
        has_admin = db.scalars(
            select(User).where(User.role == UserRole.admin.value)
        ).first()
        if not has_admin:
            first = db.scalars(select(User).order_by(User.id.asc())).first()
            if first:
                first.role = UserRole.admin.value
                first.license_status = LicenseStatus.active.value
                db.commit()
                logger.info("Promoted first user %s to admin", first.email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    loop = asyncio.get_running_loop()
    get_manager().attach_broadcaster(broadcaster, loop)
    from app.database import SessionLocal

    _db = SessionLocal()
    try:
        _bootstrap_admin(_db)
    finally:
        _db.close()
    if not get_settings().secret_key:
        logger.warning(
            "\u26a0\ufe0f SECRET_KEY is not set: login is disabled and per-user API "
            "keys cannot be stored. Set SECRET_KEY before going live."
        )
    monitor_task = asyncio.create_task(monitor_loop(get_manager(), broadcaster))
    logger.info("Tranding-track backend started (multi-user)")
    try:
        yield
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Tranding-track", version=__version__, lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Security headers ----------------------------------------------
# Defence-in-depth for a dashboard that holds a bearer token in the browser:
# a strict Content-Security-Policy plus anti-clickjacking/MIME-sniffing headers.
# ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` make the app
# un-embeddable (no clickjacking). The built SPA loads only same-origin hashed
# JS/CSS and connects to the same origin over ws/wss, so 'self' is sufficient
# for scripts; inline styles are allowed because the charting library injects
# them. If a future build needs inline scripts, prefer nonces over widening this.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    # HSTS only when the request actually arrived over TLS (behind the edge
    # proxy this shows up as x-forwarded-proto=https). Never send it over plain
    # HTTP, which would wrongly pin http visitors.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return resp


# ---- Health ---------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


# ---- Auth -----------------------------------------------------------


def _issue_token(user: User) -> str:
    s = get_settings()
    return create_access_token(
        s.secret_key,
        user_id=user.id,
        email=user.email,
        role=user.role,
        ttl_minutes=s.access_token_ttl_minutes,
    )


def _me_out(user: User) -> MeOut:
    """Build the authenticated-user payload (own state + capability flags).

    AI is app-wide/inbuilt: report the OPERATOR's global config, not per-user,
    so every licensed user sees the same built-in AI availability.
    """
    gs = get_settings()
    return MeOut(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        license_status=user.license_status,
        license_active=user.license_active,
        license_expires_at=user.license_expires_at,
        license_days_left=user.license_days_left,
        webhook_path=_webhook_path(user.webhook_token),
        binance_keys_set=bool(user.binance_api_key_enc and user.binance_api_secret_enc),
        binance_testnet=bool(user.binance_testnet),
        ai_key_set=bool(gs.ai_api_key or gs.ai_fallback_api_key),
        ai_model=gs.ai_model or gs.ai_fallback_model or "",
        secrets_storage_enabled=secrets_enabled(gs.secret_key),
    )


# Usernames: letters, digits, dot, dash, underscore; 3–64 chars. Keeps them
# URL/display-safe and unambiguous against emails (which always contain "@").
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


def _enforce_rate_limit(
    request: Request, bucket: str, *, limit: int, window_seconds: float,
    subject: str | None = None,
) -> None:
    """Reject with HTTP 429 once ``limit`` hits for this bucket are exceeded.

    Keyed by ``subject`` when given (e.g. an authenticated user id) so a shared
    NAT/office IP can't exhaust one member's budget for everyone; falls back to
    the source IP for pre-auth endpoints (signup/login) where there is no user.
    """
    if not get_settings().rate_limit_enabled:
        return
    who = subject if subject else client_ip(request)
    key = f"{bucket}:{who}"
    if not limiter.hit(key, limit=limit, window_seconds=window_seconds):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please wait a minute and try again.",
        )


@app.post("/api/auth/signup", response_model=TokenResponse)
def signup(req: SignupRequest, request: Request, db: Session = Depends(get_db)):
    # Throttle account creation per source IP to blunt mass-signup abuse.
    _enforce_rate_limit(request, "signup", limit=5, window_seconds=3600)
    s = get_settings()
    if not s.secret_key:
        raise HTTPException(
            status_code=503, detail="Signups are disabled (SECRET_KEY unset)."
        )
    email = req.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    username = req.username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail=(
                "Username must be 3–64 characters: letters, numbers, dot, dash "
                "or underscore only."
            ),
        )
    if db.scalars(select(User).where(func.lower(User.email) == email)).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    if db.scalars(
        select(User).where(func.lower(User.username) == username.lower())
    ).first():
        raise HTTPException(status_code=409, detail="That username is taken")
    try:
        pw_hash = hash_password(req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    is_admin = bool(s.admin_email) and email == s.admin_email.strip().lower()
    first_user = db.scalars(select(User)).first() is None
    make_admin = is_admin or (not s.admin_email and first_user)
    # The operator (admin) and auto-license mode don't need a key; everyone else
    # activates by redeeming the licence key you sold them — right here at signup.
    key_exempt = make_admin or s.auto_license_new_users

    now = _utcnow()
    expires_at: dt.datetime | None = None
    lk: LicenseKey | None = None
    if not key_exempt:
        key = (req.license_key or "").strip()
        if not key:
            raise HTTPException(
                status_code=400,
                detail="A licence key is required to sign up. Ask your provider for one.",
            )
        lk = db.scalars(
            select(LicenseKey).where(LicenseKey.key_hash == hash_license_key(key))
        ).first()
        if lk is None or lk.status != LicenseKeyStatus.unused.value:
            raise HTTPException(
                status_code=400,
                detail="That licence key is invalid or has already been used.",
            )
        if lk.duration_days:
            expires_at = now + dt.timedelta(days=int(lk.duration_days))

    user = User(
        email=email,
        username=username,
        password_hash=pw_hash,
        role=UserRole.admin.value if make_admin else UserRole.user.value,
        license_status=LicenseStatus.active.value,  # key/admin/auto → live now
        license_expires_at=expires_at,
        webhook_token=new_webhook_token(),
        binance_testnet=1,
        licensed_at=now,
    )
    db.add(user)
    db.flush()  # assign user.id so we can bind the redeemed key to this account

    if lk is not None:
        # Atomically claim the key (only if still unused) and bind it to this
        # user — a per-client, single-use key prevents any data mix-up.
        claimed = db.execute(
            update(LicenseKey)
            .where(
                LicenseKey.key_hash == lk.key_hash,
                LicenseKey.status == LicenseKeyStatus.unused.value,
            )
            .values(
                status=LicenseKeyStatus.redeemed.value,
                redeemed_by=user.id,
                redeemed_at=now,
            )
        ).rowcount
        if claimed != 1:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail="That licence key is invalid or has already been used.",
            )

    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=_issue_token(user))


@app.post("/api/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    # Throttle password guessing per source IP.
    _enforce_rate_limit(request, "login", limit=10, window_seconds=300)
    if not get_settings().secret_key:
        raise HTTPException(
            status_code=503, detail="Login is disabled (SECRET_KEY unset)."
        )
    ident = req.identifier.strip().lower()
    # Match on username OR email (both stored/compared lower-case).
    user = db.scalars(
        select(User).where(
            or_(func.lower(User.email) == ident, func.lower(User.username) == ident)
        )
    ).first()
    # Verify in constant-ish time whether or not the account exists: on a miss we
    # still run one PBKDF2 (dummy_verify) so response time can't reveal which
    # usernames/emails are real. Same 401 message either way.
    if user is None:
        dummy_verify(req.password)
        raise HTTPException(
            status_code=401, detail="Invalid username/email or password"
        )
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=401, detail="Invalid username/email or password"
        )
    return TokenResponse(access_token=_issue_token(user))


@app.get("/api/auth/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return _me_out(user)


# ---- Per-user credentials (own trade-only keys) --------------------


@app.put("/api/credentials", response_model=MeOut)
def update_credentials(
    body: CredentialsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    s = get_settings()
    if not secrets_enabled(s.secret_key):
        raise HTTPException(
            status_code=503,
            detail=(
                "Key storage is disabled because SECRET_KEY is not set. The "
                "operator must set SECRET_KEY before keys can be stored securely."
            ),
        )
    data = body.model_dump(exclude_unset=True)
    if "binance_api_key" in data and data["binance_api_key"] is not None:
        user.binance_api_key_enc = encrypt_secret(s.secret_key, data["binance_api_key"])
    if "binance_api_secret" in data and data["binance_api_secret"] is not None:
        user.binance_api_secret_enc = encrypt_secret(
            s.secret_key, data["binance_api_secret"]
        )
    if "binance_testnet" in data and data["binance_testnet"] is not None:
        user.binance_testnet = 1 if data["binance_testnet"] else 0
    # NOTE: AI credentials are intentionally NOT accepted here. The AI/LLM is an
    # app-wide, operator-configured capability (see build_settings_for_user);
    # users only ever manage their own exchange keys.
    db.commit()
    db.refresh(user)
    # Rebuild the user's engine so new keys take effect immediately.
    get_manager().refresh(db, user)
    return _me_out(user)


@app.post("/api/webhook/rotate", response_model=MeOut)
def rotate_webhook_token(
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Mint a fresh webhook token, invalidating the old URL immediately.

    Use this the moment the old webhook URL might have leaked — a shared
    screenshot, an alert config committed to a repo, a compromised TradingView
    account. The previous URL stops working the instant this returns (any alert
    still pointing at it gets a 404), so re-paste the new ``webhook_path`` into
    your alert(s) right after rotating. This is the only way to revoke a leaked
    webhook without deleting the account.
    """
    user.webhook_token = new_webhook_token()
    db.commit()
    db.refresh(user)
    return _me_out(user)


# ---- TradingView webhook (per-user token in the URL) ----------------


@app.post(WEBHOOK_PATH_TEMPLATE, response_model=ExecutionResult)
async def tradingview_webhook(
    token: str, request: Request, db: Session = Depends(get_db)
):
    """Execute a TradingView alert against the token-owner's account.

    The unguessable ``token`` in the URL identifies AND authenticates the target
    user, so alerts only ever touch that one account. The account must be
    licensed and running for the alert to act.
    """
    # Per-token flood guard: a stuck/duplicated alert source can't hammer the
    # execution path. Keyed by token (the account), not IP, since alert
    # providers use rotating egress IPs.
    if get_settings().rate_limit_enabled and not limiter.hit(
        f"webhook:{token}", limit=60, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="Webhook rate limit exceeded")
    user = db.scalars(select(User).where(User.webhook_token == token)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Unknown webhook token")
    raw = (await request.body()).decode("utf-8", errors="replace")
    # Never persist a raw webhook body verbatim: it may carry a shared secret /
    # token. Redact sensitive fields before it touches the signal log or any UI.
    safe_raw = _redact_raw(raw)
    try:
        data = json.loads(raw)
        signal = TradingViewSignal(**data)
    except Exception as exc:
        _log_signal(db, user.id, "tradingview", None, None, safe_raw, False, f"parse error: {exc}")
        raise HTTPException(status_code=400, detail=f"Invalid signal payload: {exc}")

    if not user.license_active:
        _log_signal(db, user.id, "tradingview", signal.symbol, signal.action, safe_raw, False,
                    "account not licensed")
        raise HTTPException(status_code=403, detail="Account is not licensed or licence expired")

    # Replay guard (opt-in): reject a payload whose send-time is too far from now,
    # so a captured URL+body can't be replayed later to fire a stale trade. No-op
    # unless the operator sets webhook_max_age_seconds > 0.
    stale = _webhook_replay_stale(data)
    if stale is not None:
        _log_signal(db, user.id, "tradingview", signal.symbol, signal.action, safe_raw, False, stale)
        raise HTTPException(status_code=409, detail=stale)

    # Idempotency: when the alert carries an explicit key, CLAIM it before acting
    # so a duplicate delivery (sender retry, replay) can never open a second
    # position. Keyless payloads are not deduped (nothing identifies a repeat).
    dedup_key = _webhook_dedup_key(data)
    if dedup_key is not None and not _claim_webhook_delivery(db, user.id, dedup_key):
        msg = "duplicate ignored: this idempotency key was already processed"
        _log_signal(db, user.id, "tradingview", signal.symbol, signal.action, safe_raw, False, msg)
        return ExecutionResult(accepted=False, message=msg, trade=None)

    engine = _engine_for(db, user)
    # Respect the STOP switch. Stopping the bot halts NEW entries — that's what
    # the autonomous monitor loop does (tasks.py gates new-entry analysis on
    # ``engine.running``), and the operator reasonably expects Stop to mean "open
    # nothing new." A webhook BUY is an unattended new entry, so it must not open
    # a position while the bot is stopped. An exit is never blocked: a ``close``
    # (and a live sell, which can only close) still runs so a protective exit can
    # always fire and open positions keep their SL/TP protection.
    if signal.action == "buy" and not engine.running:
        msg = (
            "Bot is stopped — start it to accept new webhook entries. "
            "(Exits still run while stopped.)"
        )
        _log_signal(db, user.id, "tradingview", signal.symbol, signal.action, safe_raw, False, msg)
        raise HTTPException(status_code=409, detail=msg)
    accepted, message, trade = await asyncio.to_thread(
        engine.execute_signal,
        db,
        action=signal.action,
        symbol=signal.symbol,
        amount=signal.amount,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        source="tradingview",
        note=signal.note,
        limit_price=signal.limit_price,
    )
    _log_signal(db, user.id, "tradingview", signal.symbol, signal.action, safe_raw, accepted, message)
    return ExecutionResult(
        accepted=accepted, message=message,
        trade=TradeOut.model_validate(trade) if trade else None,
    )


def _log_signal(db, user_id, source, symbol, action, raw, accepted, message) -> None:
    db.add(
        SignalLog(
            user_id=user_id, source=source, symbol=symbol, action=action, raw=raw,
            accepted=1 if accepted else 0, message=message,
        )
    )
    db.commit()


# ---- Webhook idempotency & replay protection ------------------------
# A webhook is an at-least-once channel: the sender may deliver the same alert
# more than once (a retry after a timeout, a duplicated rule, a replay). On a
# money bot each duplicate that reaches execution is a duplicate position, so we
# make the endpoint idempotent on an explicit key and (optionally) reject stale
# replays. See models.WebhookDelivery for the storage rationale.

# How long an idempotency key is remembered. Comfortably longer than any real
# sender's retry window, so a genuine retry is still recognised as a duplicate;
# older claims are pruned so the ledger stays small.
_WEBHOOK_DEDUP_RETENTION_DAYS = 7.0

# Payload fields (compared with separators/case ignored) that carry an explicit
# idempotency token. First match wins, in this order. Deliberately a NARROW set:
# only names that unambiguously mean "unique per event" — so a sender opts IN to
# dedup by choosing one. We pointedly EXCLUDE generic names like ``id`` /
# ``order_id`` / ``alert_id`` because those are often a STATIC strategy/rule id;
# treating a static value as an idempotency key would silently ignore every alert
# after the first and quietly halt trading — worse than the duplicate it guards.
# A TradingView user gets idempotency by sending e.g. "nonce": "{{timenow}}".
_WEBHOOK_DEDUP_FIELDS = (
    "idempotencykey", "dedupkey", "dedupid", "clientorderid", "nonce", "eventid", "uuid",
)

# Payload fields that carry the alert's SEND time (TradingView's {{timenow}}).
_WEBHOOK_TS_FIELDS = ("timenow", "timestamp", "sentat", "senttime")


def _webhook_norm_fields(data: object) -> dict:
    """Map a payload's top-level keys to values, keys normalised to lower-case
    alphanumerics (so ``clientOrderId``/``client_order_id`` collide). First key
    wins on a collision. Non-dict input yields an empty map."""
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        key = "".join(ch for ch in str(k).lower() if ch.isalnum())
        if key and key not in out:
            out[key] = v
    return out


def _webhook_dedup_key(data: object) -> str | None:
    """Extract an explicit idempotency key from a parsed webhook body, or None.

    Only a genuine scalar (str/int/float, not bool/null) counts, bounded to 200
    chars. None means the payload named no key — a keyless alert is never deduped.
    """
    norm = _webhook_norm_fields(data)
    for field in _WEBHOOK_DEDUP_FIELDS:
        if field in norm:
            v = norm[field]
            if isinstance(v, bool) or v is None or not isinstance(v, (str, int, float)):
                continue
            s = str(v).strip()
            if s:
                return s[:200]
    return None


def _coerce_webhook_timestamp(val: object) -> "dt.datetime | None":
    """Parse one send-time value into an aware UTC datetime, or None.

    Accepts epoch seconds or milliseconds (int/float or a numeric string) and ISO
    8601 strings (a trailing ``Z`` and naive strings are treated as UTC). Anything
    unparseable or implausibly old (before 2001) returns None so we never reject
    on a value we didn't actually understand."""
    if isinstance(val, bool) or val is None:
        return None
    num: float | None = None
    if isinstance(val, (int, float)):
        num = float(val)
    elif isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            num = float(s)
        except ValueError:
            iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
            try:
                parsed = dt.datetime.fromisoformat(iso)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    if num is None:
        return None
    if num > 1e12:  # milliseconds since epoch
        num /= 1000.0
    if num < 978_307_200:  # < 2001-01-01: not a plausible send time
        return None
    try:
        return dt.datetime.fromtimestamp(num, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _webhook_replay_stale(data: object) -> str | None:
    """When replay protection is on (webhook_max_age_seconds > 0), return a
    rejection reason if the payload's send-time is missing or too far from now;
    otherwise None. Off by default, so a normal deployment is unaffected."""
    window = get_settings().webhook_max_age_seconds
    if not window or window <= 0:
        return None
    norm = _webhook_norm_fields(data)
    ts = None
    for field in _WEBHOOK_TS_FIELDS:
        if field in norm:
            ts = _coerce_webhook_timestamp(norm[field])
            if ts is not None:
                break
    if ts is None:
        return (
            "replay protection is on but the alert carried no readable timestamp; "
            'send TradingView\'s {{timenow}} as a "timenow" field in the alert JSON'
        )
    age = (_utcnow() - ts).total_seconds()
    if age > window:
        return f"stale webhook rejected: alert is {int(age)}s old (limit {int(window)}s)"
    if age < -window:
        return f"webhook timestamp is {int(-age)}s in the future (limit {int(window)}s)"
    return None


def _claim_webhook_delivery(db: Session, user_id: int, dedup_key: str) -> bool:
    """Atomically claim an idempotency key. True on the FIRST sighting of
    ``(user_id, dedup_key)``; False if already claimed (a duplicate).

    The UNIQUE constraint is the real guard under concurrency: two simultaneous
    deliveries race to INSERT and the DB lets exactly one commit. Old claims are
    pruned opportunistically so the ledger can't grow without bound."""
    try:
        db.add(WebhookDelivery(user_id=user_id, dedup_key=dedup_key))
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    try:
        cutoff = _utcnow() - dt.timedelta(days=_WEBHOOK_DEDUP_RETENTION_DAYS)
        db.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.user_id == user_id,
                WebhookDelivery.created_at < cutoff,
            )
        )
        db.commit()
    except Exception:  # pruning is best-effort; never fail a claimed delivery
        db.rollback()
    return True


# Field names (normalised to lower-case alphanumerics) we must never store from
# an inbound webhook body — they can carry secrets that would otherwise rest in
# the signal log and leak through the API/UI.
_SENSITIVE_KEYS = {
    "secret", "password", "passphrase", "token", "apikey", "apisecret",
    "secretkey", "webhooksecret", "auth", "authorization", "key", "privatekey",
    "accesstoken", "bearer",
}


def _redact_obj(obj):
    """Recursively mask secret-like keys anywhere in a parsed JSON structure.

    Walks nested dicts and lists so a secret buried under e.g. ``{"data":
    {"apiKey": ...}}`` is masked too, not just top-level fields.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            norm = "".join(ch for ch in str(k).lower() if ch.isalnum())
            if norm in _SENSITIVE_KEYS:
                out[k] = "***redacted***"
            else:
                out[k] = _redact_obj(v)
        return out
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def _redact_raw(raw: str) -> str:
    """Scrub secret-like fields from a webhook body before it is persisted.

    Best-effort and fail-safe: parse JSON and mask sensitive keys at any depth;
    if the body isn't JSON we can't locate a secret inside it, so we store only
    its size rather than the bytes. We never keep a plaintext secret at rest.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return f"<non-JSON payload, {len(raw)} bytes (redacted)>"
    data = _redact_obj(data)
    try:
        return json.dumps(data)
    except Exception:
        return "<unserializable payload (redacted)>"


# ---- Manual orders -------------------------------------------------


@app.post("/api/order", response_model=ExecutionResult)
async def manual_order(
    order: ManualOrder,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    accepted, message, trade = await asyncio.to_thread(
        engine.execute_signal,
        db,
        action=order.action,
        symbol=order.symbol,
        amount=order.amount,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        source="manual",
        note="manual order",
        limit_price=order.limit_price,
    )
    return ExecutionResult(
        accepted=accepted, message=message,
        trade=TradeOut.model_validate(trade) if trade else None,
    )


@app.post("/api/order/scaled", response_model=ScaledResult)
async def scaled_order(
    order: ScaledOrder,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Open a BUY in scaled (DCA) legs. Places NO order until the account trades
    live; in paper it simulates the ladder against the paper wallet. The TOTAL
    size is risk-checked once, then split across legs (see engine docs)."""
    engine = _engine_for(db, user)
    accepted, message, legs = await asyncio.to_thread(
        engine.execute_scaled_entry,
        db,
        symbol=order.symbol,
        amount=order.amount,
        legs=order.legs,
        step_pct=order.step_pct,
        first_at_market=order.first_at_market,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        source="manual",
        note="scaled entry",
    )
    return ScaledResult(
        accepted=accepted,
        message=message,
        legs=[TradeOut.model_validate(t) for t in legs],
    )


@app.post("/api/positions/{symbol:path}/close-all", response_model=CloseAllResult)
async def close_all_for_symbol(
    symbol: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Close every open position and cancel every resting order for one symbol —
    the one-action exit for a multi-leg DCA entry."""
    engine = _engine_for(db, user)
    count, pnl, _msgs = await asyncio.to_thread(engine.close_symbol, db, symbol)
    if count == 0:
        return CloseAllResult(
            closed=0,
            realized_pnl=0.0,
            message=f"No open positions or resting orders for {symbol.upper()}.",
        )
    return CloseAllResult(
        closed=count,
        realized_pnl=pnl,
        message=(
            f"Closed/cancelled {count} for {symbol.upper()} "
            f"(realized PnL {pnl:.2f})."
        ),
    )


@app.post("/api/trades/{trade_id}/close", response_model=ExecutionResult)
async def close_trade(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    trade = db.get(Trade, trade_id)
    if (
        not trade
        or trade.user_id != user.id
        or trade.status not in (TradeStatus.open.value, TradeStatus.pending.value)
    ):
        raise HTTPException(status_code=404, detail="Open trade not found")
    engine = _engine_for(db, user)
    # Close the EXACT trade by id (not "an open trade for this symbol", which is
    # ambiguous with multiple DCA legs) and enforce paper/live book isolation.
    accepted, message, updated = await asyncio.to_thread(
        engine.close_by_id,
        db,
        trade_id,
    )
    return ExecutionResult(
        accepted=accepted, message=message,
        trade=TradeOut.model_validate(updated) if updated else None,
    )


# ---- Trades & signals (scoped to the caller) ------------------------


@app.get("/api/trades", response_model=list[TradeOut])
def list_trades(
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Trade).where(Trade.user_id == user.id)
    if status:
        stmt = stmt.where(Trade.status == status)
    stmt = stmt.order_by(Trade.opened_at.desc()).limit(max(1, min(limit, 500)))
    return list(db.scalars(stmt).all())


@app.delete("/api/trades/{trade_id}")
def delete_trade(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Delete ONE closed trade from this user's journal.

    This only removes a historical record (and the performance stats derived from
    it). It never touches the exchange and does not change your wallet balance —
    realized profit/loss was already banked when the trade closed. An open or
    pending position can't be deleted here; close it first. Irreversible.
    """
    trade = db.get(Trade, trade_id)
    if not trade or trade.user_id != user.id:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != TradeStatus.closed.value:
        raise HTTPException(
            status_code=409,
            detail="Only a closed trade can be deleted. Close the position first.",
        )
    db.delete(trade)
    db.commit()
    return {"deleted": 1}


@app.delete("/api/trades")
def clear_trades(
    mode: str = "paper",
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Clear this user's CLOSED trade journal.

    Never deletes an open or pending position — those are live state; close them
    first. `mode` selects which book to clear: "paper" (default), "live", or
    "all". Destructive and irreversible: it wipes the journal and the performance
    stats computed from it, but does NOT move your wallet balance or touch
    anything on the exchange.
    """
    mode = (mode or "paper").strip().lower()
    if mode not in ("paper", "live", "all"):
        raise HTTPException(
            status_code=400, detail="mode must be paper, live, or all"
        )
    stmt = delete(Trade).where(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.closed.value,
    )
    if mode != "all":
        stmt = stmt.where(Trade.mode == mode)
    result = db.execute(stmt)
    db.commit()
    return {"deleted": int(result.rowcount or 0)}


@app.get("/api/performance", response_model=PerformanceOut)
def performance(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Realized performance analytics for the current user, computed live from
    their CLOSED trades. Read-only — never places or changes an order. Paper and
    live results are split so simulated gains are not counted as real money.
    """
    stmt = select(Trade).where(
        Trade.user_id == user.id, Trade.status == TradeStatus.closed.value
    )
    return compute_performance(db.scalars(stmt).all())


@app.get("/api/signals", response_model=list[SignalOut])
def list_signals(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = (
        select(SignalLog)
        .where(SignalLog.user_id == user.id)
        .order_by(SignalLog.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(db.scalars(stmt).all())


@app.delete("/api/signals/{signal_id}")
def delete_signal(
    signal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Delete ONE entry from this user's signal log. Read-only history; removing
    it never affects any open position, order, or balance. Irreversible."""
    slog = db.get(SignalLog, signal_id)
    if not slog or slog.user_id != user.id:
        raise HTTPException(status_code=404, detail="Signal not found")
    db.delete(slog)
    db.commit()
    return {"deleted": 1}


@app.delete("/api/signals")
def clear_signals(
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Clear this user's entire signal log. It is a read-only history of
    incoming signals, so clearing it never touches positions, orders, or
    balances. Destructive and irreversible."""
    result = db.execute(delete(SignalLog).where(SignalLog.user_id == user.id))
    db.commit()
    return {"deleted": int(result.rowcount or 0)}


# ---- Status & settings ---------------------------------------------


@app.get("/api/status", response_model=BotStatus)
def status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _engine_for(db, user).status(db)


@app.get("/api/exchange/access")
def exchange_access(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Report whether THIS user's key can read the account and place orders."""
    return _engine_for(db, user).connector.check_trading_access()


@app.get("/api/symbols")
def list_symbols(
    quote: str = "USDT",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Real, tradable spot pairs from the configured exchange (via ccxt
    load_markets), so the UI's pair selector reflects what actually exists on
    Binance instead of a hardcoded list. Empty list = markets unreachable."""
    engine = _engine_for(db, user)
    try:
        symbols = engine.connector.list_symbols(quote=quote)
    except Exception as exc:
        raise _upstream_error("Could not list symbols", exc)
    return {"symbols": symbols, "quote": (quote or "USDT").upper()}


@app.post("/api/paper/reset")
def reset_paper(
    db: Session = Depends(get_db), user: User = Depends(require_licensed_user)
):
    """Clean slate for SIMULATED data: delete this user's paper trades + signal
    log and reset the paper wallet. Real (live) trades are preserved."""
    return _engine_for(db, user).reset_paper_data(db)


@app.post("/api/bot/{state}")
def set_bot_state(
    state: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    if state == "start":
        # Restarting is the operator's explicit acknowledgement: clear any tripped
        # drawdown kill-switch and reseed the equity peak from here.
        engine.reset_killswitch()
        engine.running = True
    elif state == "stop":
        engine.running = False
    else:
        raise HTTPException(status_code=400, detail="state must be 'start' or 'stop'")
    # Durably record the operator's run/stop choice (and any kill-switch re-arm) so
    # a restart, redeploy or engine rebuild can't silently flip it back.
    engine.persist_runtime(db)
    return {"running": engine.running}


def _settings_out(engine, user: User, db: Session | None = None) -> SettingsOut:
    s = engine.settings
    # Monitor cadence is a GLOBAL, server-wide value (one shared loop), so show the
    # real stored value rather than a per-engine copy when we have a db handle.
    monitor_interval = (
        load_monitor_interval(db, getattr(s, "monitor_interval_seconds", 5.0))
        if db is not None else getattr(s, "monitor_interval_seconds", 5.0)
    )
    return SettingsOut(
        trading_mode=s.trading_mode,
        binance_testnet=s.binance_testnet,
        max_open_positions=s.max_open_positions,
        risk_per_trade_pct=s.risk_per_trade_pct,
        daily_loss_limit_pct=s.daily_loss_limit_pct,
        default_stop_loss_pct=s.default_stop_loss_pct,
        default_take_profit_pct=s.default_take_profit_pct,
        trailing_stop_pct=s.trailing_stop_pct,
        max_total_exposure_pct=s.max_total_exposure_pct,
        max_position_pct=getattr(s, "max_position_pct", 0.0),
        paper_taker_fee_pct=getattr(s, "paper_taker_fee_pct", 0.0),
        min_signal_confidence=s.min_signal_confidence,
        auto_trade_enabled=s.auto_trade_enabled,
        auto_symbols=s.auto_symbols,
        auto_timeframe=s.auto_timeframe,
        auto_confirm_timeframe=s.auto_confirm_timeframe,
        auto_live_confirm=getattr(s, "auto_live_confirm", True),
        auto_confirm_ttl_minutes=getattr(s, "auto_confirm_ttl_minutes", 10.0),
        use_saved_strategy=getattr(s, "use_saved_strategy", False),
        ai_trade_confirm=getattr(s, "ai_trade_confirm", False),
        ai_pretrade_analysis=getattr(s, "ai_pretrade_analysis", False),
        ai_monitor_enabled=getattr(s, "ai_monitor_enabled", False),
        ai_autopilot_enabled=getattr(s, "ai_autopilot_enabled", False),
        ict_enabled=getattr(s, "ict_enabled", True),
        ai_enabled=bool(s.ai_api_key or getattr(s, "ai_fallback_api_key", "")),
        ai_model=s.ai_model,
        ai_style=engine.ai._style() if (s.ai_api_key or getattr(s, "ai_fallback_api_key", "")) else "",
        auto_pause_in_bear=getattr(s, "auto_pause_in_bear", True),
        require_strategy_validation=getattr(s, "require_strategy_validation", True),
        strategy_min_return_pct=getattr(s, "strategy_min_return_pct", 0.0),
        strategy_min_win_rate_pct=getattr(s, "strategy_min_win_rate_pct", 0.0),
        strategy_min_trades=getattr(s, "strategy_min_trades", 5),
        strategy_max_drawdown_pct=getattr(s, "strategy_max_drawdown_pct", 0.0),
        profit_lock_enabled=getattr(s, "profit_lock_enabled", False),
        profit_lock_trigger_pct=getattr(s, "profit_lock_trigger_pct", 1.0),
        profit_lock_floor_pct=getattr(s, "profit_lock_floor_pct", 0.3),
        take_profit_on_reversal=getattr(s, "take_profit_on_reversal", False),
        reversal_confirm_count=getattr(s, "reversal_confirm_count", 2),
        monitor_interval_seconds=monitor_interval,
        notifications_enabled=engine.notifier.enabled,
        api_key_set=bool(user.binance_api_key_enc and user.binance_api_secret_enc),
        webhook_path=_webhook_path(user.webhook_token),
        webhook_secret_set=bool(user.webhook_token),
    )


@app.get("/api/settings", response_model=SettingsOut)
def get_settings_endpoint(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return _settings_out(_engine_for(db, user), user, db)


@app.patch("/api/settings", response_model=SettingsOut)
def update_settings(
    update: SettingsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    s = engine.settings
    data = update.model_dump(exclude_unset=True)
    # SAFETY GATE: before flipping to LIVE (real money), verify this user's keys
    # actually exist and can READ the real account. Live mode then shows REAL
    # balances and market data (never fake paper numbers). Order PLACEMENT is
    # gated separately at execution time — a read-only key sees real data but
    # can't trade until Spot/IP is enabled — so "live" always means real data,
    # and never means silently-failing orders. Only runs on OFF→LIVE; paper is
    # never gated.
    if data.get("trading_mode") == "live" and s.trading_mode != "live":
        if not (user.binance_api_key_enc and user.binance_api_secret_enc):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Can't switch to LIVE: no exchange API key is set for your "
                    "account. Add Binance API keys under Keys first, then switch "
                    "to live. Trading needs Spot permission; read-only keys still "
                    "show your real account and market data."
                ),
            )
        try:
            access = engine.connector.check_trading_access()
        except Exception as exc:
            raise _upstream_error(
                "Can't verify live account access right now", exc
            )
        if not access.get("can_read_account"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Can't switch to LIVE: your API key can't read your Binance "
                    "account yet, so live mode would have no real data to show. "
                    + (access.get("detail") or "Check the key, its IP "
                       "restriction, and that testnet is off for a real key.")
                ),
            )
    for key, value in data.items():
        setattr(s, key, value)
    engine.apply_settings(s)
    # Monitor cadence is GLOBAL (one shared background loop for all users): persist
    # it to the unscoped KV the loop reads live, NOT as a per-user override.
    interval = data.pop("monitor_interval_seconds", None)
    if interval is not None:
        setattr(s, "monitor_interval_seconds", save_monitor_interval(db, interval))
    engine.persist_settings(db, data)
    return _settings_out(engine, user, db)


# ---- Price alerts ---------------------------------------------------
# User-defined "notify me when price crosses X" alerts. The background monitor
# checks each armed alert against the REAL live price and fires it once (see
# tasks.py). Deterministic and honest: nothing here fabricates a price or a
# trigger, and an alert only fires on a real crossing.


@app.get("/api/alerts", response_model=list[AlertOut])
def list_alerts(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    rows = db.scalars(
        select(PriceAlert)
        .where(PriceAlert.user_id == user.id)
        .order_by(PriceAlert.created_at.desc())
    ).all()
    return [AlertOut.model_validate(a, from_attributes=True) for a in rows]


@app.post("/api/alerts", response_model=AlertOut)
def create_alert(
    body: AlertCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    symbol = body.symbol.strip().upper()
    if "/" not in symbol:
        raise HTTPException(
            status_code=400,
            detail="Symbol must look like BASE/QUOTE, e.g. BTC/USDT.",
        )
    alert = PriceAlert(
        user_id=user.id,
        symbol=symbol,
        condition=body.condition,
        price=float(body.price),
        note=(body.note or None),
        status="armed",
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return AlertOut.model_validate(alert, from_attributes=True)


@app.delete("/api/alerts/{alert_id}")
def delete_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    alert = db.get(PriceAlert, alert_id)
    if alert is None or alert.user_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found.")
    db.delete(alert)
    db.commit()
    return {"deleted": alert_id}


# ---- Autonomous live-entry confirmations ----------------------------
# The confirm-before-live gate (Settings.auto_live_confirm): when the bot
# decides a LIVE buy on its own it queues it here and waits for the operator's
# approval instead of spending real money unattended. Exits/closes and paper are
# never gated. Approving re-runs the order fresh (re-priced/re-sized/re-checked).


@app.get("/api/auto/confirmations", response_model=list[AutoConfirmationOut])
def list_auto_confirmations(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    engine = _engine_for(db, user)
    rows = engine.list_auto_confirmations(db)
    return [AutoConfirmationOut.model_validate(r, from_attributes=True) for r in rows]


@app.post(
    "/api/auto/confirmations/{cid}/approve",
    response_model=ConfirmationResolveResult,
)
def approve_auto_confirmation(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    ok, msg, trade = engine.resolve_auto_confirmation(db, cid, approve=True)
    return ConfirmationResolveResult(
        ok=ok, message=msg, trade_id=(trade.id if trade is not None else None)
    )


@app.post(
    "/api/auto/confirmations/{cid}/reject",
    response_model=ConfirmationResolveResult,
)
def reject_auto_confirmation(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    ok, msg, _ = engine.resolve_auto_confirmation(db, cid, approve=False)
    return ConfirmationResolveResult(ok=ok, message=msg, trade_id=None)


# ---- Market data ----------------------------------------------------


@app.get("/api/ticker/{symbol:path}", response_model=TickerOut)
def ticker(
    symbol: str, db: Session = Depends(get_db), user: User = Depends(require_licensed_user)
):
    engine = _engine_for(db, user)
    try:
        t = engine.connector.fetch_ticker(symbol.upper())
    except Exception as exc:
        raise _upstream_error("Ticker unavailable", exc)
    # Never fabricate a price: if the exchange gives us no last/close, report the
    # ticker as unavailable (502) so the UI shows its stale/offline state rather
    # than a fake $0.00. A money task must not show a figure that isn't real.
    last = t.get("last") or t.get("close")
    if last is None:
        raise HTTPException(status_code=502, detail="Ticker unavailable: no price")
    return TickerOut(
        symbol=symbol.upper(),
        last=float(last),
        bid=t.get("bid"),
        ask=t.get("ask"),
        percentage=t.get("percentage"),
        base_volume=t.get("baseVolume"),
        quote_volume=t.get("quoteVolume"),
        source=getattr(engine.connector, "last_data_source", None),
    )


@app.get("/api/ohlcv/{symbol:path}")
def ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    try:
        raw = engine.connector.fetch_ohlcv(symbol.upper(), timeframe, max(1, min(limit, 1000)))
    except Exception as exc:
        raise _upstream_error("OHLCV unavailable", exc)
    return [
        {"time": int(c[0] / 1000), "open": c[1], "high": c[2],
         "low": c[3], "close": c[4], "volume": c[5]}
        for c in raw
    ]


@app.get("/api/orderbook/{symbol:path}", response_model=OrderBookOut)
def orderbook(
    symbol: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Live order book (real resting buy-side bids and sell-side asks).

    The market's ACTUAL resting liquidity, straight from the exchange (or the
    public fallback when the primary is geo-blocked) — not the user's own
    orders, and never fabricated: if the venue returns no depth the side comes
    back empty rather than invented. Note: on the Binance TESTNET the book is the
    sandbox's own thin liquidity, not the live market — flip BINANCE_TESTNET off
    to see the real book.
    """
    engine = _engine_for(db, user)
    try:
        ob = engine.connector.fetch_order_book(symbol.upper(), min(max(limit, 1), 100))
    except Exception as exc:
        raise _upstream_error("Order book unavailable", exc)

    def _levels(rows: object) -> list[dict[str, float]]:
        out: list[dict[str, float]] = []
        for r in rows or []:  # type: ignore[union-attr]
            try:
                price = float(r[0])
                amount = float(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            if price > 0 and amount > 0:
                out.append({"price": price, "amount": amount})
        return out

    return OrderBookOut(
        symbol=symbol.upper(),
        bids=_levels(ob.get("bids")),
        asks=_levels(ob.get("asks")),
        source=getattr(engine.connector, "last_data_source", None),
    )


# ---- Backtesting ----------------------------------------------------


@app.get("/api/backtest")
def backtest(
    symbol: str,
    strategy: str = "ma_cross",
    timeframe: str = "1h",
    limit: int = 500,
    starting_balance: float = 10_000.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    use_saved: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    # Default the exit rules to THIS user's live settings so a backtest reflects
    # how the bot would actually trade; explicit query params override (pass 0
    # to model a plain signal-only run with no stops).
    s = engine.settings
    sl = s.default_stop_loss_pct if stop_loss_pct is None else stop_loss_pct
    tp = s.default_take_profit_pct if take_profit_pct is None else take_profit_pct
    trail = s.trailing_stop_pct if trailing_stop_pct is None else trailing_stop_pct
    # Optionally backtest the user's SAVED, trained params for this symbol (the
    # exact config the bot trades with) rather than the strategy's defaults.
    saved_params: dict | None = None
    saved_cfg = engine.strategy_configs.get(symbol.upper()) if use_saved else None
    if saved_cfg:
        strategy = saved_cfg.get("strategy", strategy)
        timeframe = saved_cfg.get("timeframe", timeframe)
        saved_params = saved_cfg.get("params") or {}
    try:
        raw = engine.connector.fetch_ohlcv(symbol.upper(), timeframe, max(1, min(limit, 1000)))
    except Exception as exc:
        raise _upstream_error("OHLCV unavailable", exc)
    if not raw:
        raise HTTPException(status_code=502, detail="No candle data returned")
    df = pd.DataFrame(
        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    try:
        strat = build_strategy(strategy, **(saved_params or {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = run_backtest(
        df,
        strat,
        starting_balance=starting_balance,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        stop_loss_pct=sl,
        take_profit_pct=tp,
        trailing_stop_pct=trail,
    )
    # Real, explainable analytics + a plain-language read of what happened. Every
    # figure comes from the trades above — nothing here is invented.
    analytics = summarize_backtest(result, df, timeframe=timeframe)
    return {
        "symbol": symbol.upper(),
        "strategy": strategy,
        "timeframe": timeframe,
        "used_saved": bool(saved_cfg),
        "starting_balance": result.starting_balance,
        "ending_balance": round(result.ending_balance, 2),
        "total_return_pct": round(result.total_return_pct, 2),
        "num_trades": result.num_trades,
        "win_rate_pct": (
            round(result.win_rate_pct, 2) if result.win_rate_pct is not None else None
        ),
        "max_drawdown_pct": round(result.max_drawdown_pct, 2),
        "total_fees": result.total_fees,
        "stop_loss_pct": sl,
        "take_profit_pct": tp,
        "trailing_stop_pct": trail,
        "equity_curve": [round(e, 2) for e in result.equity_curve],
        "ohlc_synthetic": result.ohlc_synthetic,
        "analytics": analytics,
        "explanation": analytics["explanation"],
    }


# ---- Market analysis ("the brain") & AI ----------------------------


@app.get("/api/analyze/{symbol:path}")
def analyze(
    symbol: str,
    timeframe: str = "1h",
    explain: bool = False,
    assess: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    analysis, ict = _analysis_for(engine, symbol, timeframe)
    result = analysis.as_dict()
    # Real ICT/smart-money read on the same closed bars (None when the lens is
    # off for this user), so the chart can draw it and the operator sees the
    # structure behind the verdict. `ict_enabled` tells the UI which state it's in.
    result["ict"] = ict.as_dict() if ict is not None else None
    result["ict_enabled"] = bool(getattr(engine.settings, "ict_enabled", True))
    if explain:
        result["narration"] = engine.ai.narrate(analysis, ict=ict)
        result["ai_enabled"] = engine.ai.available
    if assess:
        result["assessment"] = engine.ai.assess(analysis, ict=ict)
        result["ai_enabled"] = engine.ai.available
    return result


@app.post("/api/ai/ask")
def ai_ask(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    # The AI key is a shared, operator-funded resource. Rate-limit per USER (not
    # per IP, so one member on a shared office/NAT IP can't starve the rest) and
    # cap the prompt length so a single account can't run up the operator's bill.
    _enforce_rate_limit(request, "ai", limit=20, window_seconds=60, subject=f"u{user.id}")
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 2000:
        raise HTTPException(
            status_code=400, detail="question is too long (max 2000 characters)"
        )
    engine = _engine_for(db, user)
    symbol = payload.get("symbol")
    timeframe = payload.get("timeframe", "1h")
    # A bad or momentarily unreachable symbol shouldn't 502 the whole ask (same
    # as /api/ai/chat): degrade gracefully to no-analysis context so the
    # assistant can still answer the question.
    analysis = None
    ict = None
    if symbol:
        try:
            analysis, ict = _analysis_for(engine, str(symbol), str(timeframe))
        except Exception:
            analysis = None
            ict = None
    answer = engine.ai.ask(question, analysis, ict=ict)
    return {"answer": answer, "ai_enabled": engine.ai.available}


@app.get("/api/news")
def market_news(
    limit: int = 8,
    user: User = Depends(require_licensed_user),
):
    """Live, REAL market headlines from the configured public feeds.

    Returns whatever real items we could fetch plus any per-feed errors. Never
    fabricates news — an empty list alongside errors means the sources were
    unreachable, not that nothing is happening.
    """
    limit = max(1, min(limit, 30))
    items, errors = fetch_market_news(get_settings().news_feed_list, limit=limit)
    return {"items": items, "errors": errors}


def _assistant_account_context(db: Session, user: User, engine) -> str:
    """NON-secret snapshot of the asking user's OWN account for the assistant.

    Privacy: only this user's own, non-secret data — never API keys, secrets,
    passwords, the raw webhook token, or any other user's rows — sent solely to
    the operator's configured AI provider so the assistant can be concrete about
    *this* account. Best-effort: any part that can't be read is simply omitted.
    """
    gs = get_settings()
    s = engine.settings
    keys_set = bool(user.binance_api_key_enc and user.binance_api_secret_enc)
    lines: list[str] = [
        f"- Account: {user.email} (role={user.role}, licence={user.license_status})",
        "- Exchange keys: "
        + ("set" if keys_set else "NOT set")
        + f"; testnet={'on' if user.binance_testnet else 'off'}; secure storage "
        + ("on" if secrets_enabled(gs.secret_key) else "OFF (operator hasn't set SECRET_KEY)"),
        "- Built-in AI: "
        + ("configured" if gs.ai_api_key else "not configured")
        + f"; TradingView webhook: {'configured (private)' if user.webhook_token else 'not set'}",
    ]
    try:
        st = engine.status(db)
        lines += [
            f"- Mode: {st.get('trading_mode')} (testnet={st.get('testnet')}, running={st.get('running')})",
            f"- Positions: {st.get('open_positions')}/{st.get('max_open_positions')}; "
            f"balance {st.get('balance')}, equity {st.get('equity')}",
            f"- PnL realized {st.get('realized_pnl')}, unrealized "
            f"{st.get('unrealized_pnl')}, today {st.get('day_pnl')}",
        ]
    except Exception:
        pass
    lines.append(
        f"- Autonomous: {'on' if s.auto_trade_enabled else 'off'} "
        f"(symbols={s.auto_symbols or 'none'}, tf={s.auto_timeframe}, "
        f"confirm_tf={s.auto_confirm_timeframe or 'off'}, min_conf={s.min_signal_confidence}); "
        f"AI trade review {'on' if getattr(s, 'ai_trade_confirm', False) else 'off'}"
    )
    lines.append(
        "- Assistant autopilot: "
        + (
            "ON — you APPLY safe actions yourself the moment you propose them "
            "(settings, bot start/stop, alerts, PAPER orders); LIVE orders and "
            "paper<->live switches still need the operator's confirmation."
            if getattr(s, "ai_autopilot_enabled", False)
            else "OFF — every action you propose only appears on a Confirm card "
            "and does nothing until the operator taps Confirm."
        )
    )
    now = _utcnow()

    def _hours_since(ts) -> float | None:
        """Real elapsed hours since a stored timestamp (UTC-safe), or None."""
        if ts is None:
            return None
        try:
            return max(0.0, (now - _as_utc(ts)).total_seconds() / 3600.0)
        except Exception:
            return None

    def _fmt_dur(hours: float | None) -> str:
        if hours is None:
            return "unknown age"
        if hours < 1:
            return f"{hours * 60:.0f}m"
        if hours < 48:
            return f"{hours:.1f}h"
        return f"{hours / 24:.1f}d"

    try:
        open_rows = db.scalars(
            select(Trade)
            .where(Trade.user_id == user.id, Trade.status == "open")
            .order_by(Trade.opened_at.desc())
            .limit(10)
        ).all()
        if open_rows:
            parts = []
            for t in open_rows:
                pnl = t.pnl or 0
                state = "up" if pnl > 0 else "down" if pnl < 0 else "flat"
                entry = float(t.entry_price or 0)
                # Exact protection read-out per trade: where the stop/target sit
                # and how far they are from entry — and, loudly, when there's NO
                # stop at all (the single biggest avoidable-loss red flag).
                guard: list[str] = []
                if t.stop_loss and entry > 0:
                    d = abs(entry - float(t.stop_loss)) / entry * 100
                    guard.append(f"SL {t.stop_loss} ({d:.1f}% from entry)")
                else:
                    guard.append("NO STOP set")
                if t.take_profit and entry > 0:
                    d = abs(float(t.take_profit) - entry) / entry * 100
                    guard.append(f"TP {t.take_profit} (+{d:.1f}%)")
                parts.append(
                    f"{t.symbol} {t.side} {t.amount}@{t.entry_price} "
                    f"(pnl {pnl} — {state}, open {_fmt_dur(_hours_since(t.opened_at))}; "
                    + ", ".join(guard) + ")"
                )
            lines.append("- Open trades: " + "; ".join(parts))
            oldest = _hours_since(
                min((t.opened_at for t in open_rows if t.opened_at), default=None)
            )
            if oldest is not None:
                lines.append(
                    f"- Oldest open position has been running {oldest:.1f} hours "
                    f"({_fmt_dur(oldest)})"
                )
        else:
            lines.append("- Open trades: none")
    except Exception:
        pass
    # Real trading span + counts — this is what answers "how many hours have I
    # been trading" honestly, from actual trade timestamps (never a made-up uptime).
    try:
        first_trade = db.scalars(
            select(Trade)
            .where(Trade.user_id == user.id)
            .order_by(Trade.opened_at.asc())
            .limit(1)
        ).first()
        total_trades = (
            db.scalar(
                select(func.count()).select_from(Trade).where(Trade.user_id == user.id)
            )
            or 0
        )
        closed_trades = (
            db.scalar(
                select(func.count())
                .select_from(Trade)
                .where(Trade.user_id == user.id, Trade.status == "closed")
            )
            or 0
        )
        if first_trade is not None:
            span = _hours_since(first_trade.opened_at)
            last_close = db.scalars(
                select(Trade)
                .where(Trade.user_id == user.id, Trade.status == "closed")
                .order_by(Trade.closed_at.desc())
                .limit(1)
            ).first()
            since_last = (
                _hours_since(last_close.closed_at)
                if last_close and last_close.closed_at
                else None
            )
            line = (
                f"- Trading activity: your first trade was {_fmt_dur(span)} ago"
                + (f" ({span:.1f} hours of history)" if span is not None else "")
                + f"; {total_trades} trades total, {closed_trades} closed, "
                f"{total_trades - closed_trades} still open"
            )
            if since_last is not None:
                line += f"; last close {_fmt_dur(since_last)} ago"
            lines.append(line)
        else:
            lines.append("- Trading activity: no trades yet on this account")
    except Exception:
        pass
    # Risk radar — deterministic, derived-from-real-numbers metrics so the
    # assistant can warn concretely about capital at risk instead of vaguely.
    # Every value is computed from the account's own stored figures; nothing is
    # fetched or invented, and any piece that can't be computed is left out.
    try:
        rst = engine.status(db)
        equity = float(rst.get("equity") or 0)
        day_pnl = float(rst.get("day_pnl") or 0)
        radar: list[str] = []
        dll = float(getattr(s, "daily_loss_limit_pct", 0) or 0)
        if dll > 0 and equity > 0:
            limit_amt = equity * dll / 100.0
            lost = max(0.0, -day_pnl)
            room = max(0.0, limit_amt - lost)
            radar.append(
                f"today's loss {lost:.2f} against a {dll}% daily limit (~{limit_amt:.2f}); "
                f"~{room:.2f} of room left"
            )
        mtx = float(getattr(s, "max_total_exposure_pct", 0) or 0)
        notional = db.scalar(
            select(func.coalesce(func.sum(Trade.amount * Trade.entry_price), 0.0))
            .where(Trade.user_id == user.id, Trade.status == "open")
        ) or 0.0
        notional = float(notional)
        if notional > 0:
            if mtx > 0 and equity > 0:
                cap = equity * mtx / 100.0
                radar.append(
                    f"open exposure ~{notional:.2f} vs {mtx}% cap (~{cap:.2f})"
                )
            else:
                radar.append(f"open exposure ~{notional:.2f} (no exposure cap set)")
        if radar:
            lines.append("- Risk radar: " + "; ".join(radar))
    except Exception:
        pass
    try:
        sig_rows = db.scalars(
            select(SignalLog)
            .where(SignalLog.user_id == user.id)
            .order_by(SignalLog.created_at.desc())
            .limit(5)
        ).all()
        if sig_rows:
            lines.append(
                "- Recent signals: "
                + "; ".join(
                    f"{x.source}:{x.symbol or '-'} {x.action or '-'} "
                    f"({'accepted' if x.accepted else 'rejected'})"
                    for x in sig_rows
                )
            )
    except Exception:
        pass
    # Realized performance from this user's CLOSED trades — the honest answer to
    # "how am I doing / is my strategy working": win rate, profit factor, total
    # P&L, expectancy and realized drawdown, all computed from real booked
    # results (never guessed). Paper and live are split so simulated gains are
    # never mistaken for real money.
    try:
        closed_rows = db.scalars(
            select(Trade).where(Trade.user_id == user.id, Trade.status == "closed")
        ).all()
        if closed_rows:
            perf = compute_performance(closed_rows)
            pf = perf.get("profit_factor")
            pf_txt = f"{pf:.2f}" if isinstance(pf, (int, float)) else "n/a (no losing trades yet)"
            lines.append(
                f"- Realized performance: {perf.get('closed_trades')} closed, "
                f"win rate {perf.get('win_rate_pct')}%, profit factor {pf_txt}, "
                f"total P&L {perf.get('total_pnl')}, expectancy {perf.get('expectancy')}/trade, "
                f"max realized drawdown {perf.get('max_drawdown')}"
            )
            paper = perf.get("paper") or {}
            live = perf.get("live") or {}
            lines.append(
                f"- Paper vs live results: paper {paper.get('closed_trades', 0)} closed "
                f"(P&L {paper.get('total_pnl', 0)}), live {live.get('closed_trades', 0)} closed "
                f"(P&L {live.get('total_pnl', 0)})"
            )
    except Exception:
        pass
    # Saved (trained) strategies this account trades with when use_saved_strategy
    # is on — so the assistant knows exactly what is trained, how it tested, and
    # which symbols fall back to the built-in analyzer. Real persisted config and
    # measured metrics only; nothing invented.
    try:
        cfgs = getattr(engine, "strategy_configs", {}) or {}
        if cfgs:
            parts: list[str] = []
            for sym in sorted(cfgs):
                cfg = cfgs[sym] or {}
                bits = f"{sym}: {cfg.get('strategy', '?')}/{cfg.get('timeframe', '?')}"
                m = cfg.get("metrics") or {}
                extra: list[str] = []
                ret = m.get("total_return_pct")
                wr = m.get("win_rate_pct")
                if isinstance(ret, (int, float)):
                    extra.append(f"tested return {ret:.1f}%")
                if isinstance(wr, (int, float)):
                    extra.append(f"win {wr:.0f}%")
                if extra:
                    bits += " (" + ", ".join(extra) + ")"
                parts.append(bits)
            active = getattr(s, "use_saved_strategy", False)
            lines.append(
                "- Saved strategies "
                + ("(ACTIVE — the bot trades these)" if active
                   else "(inactive — use_saved_strategy is OFF, so the bot ignores them)")
                + ": " + "; ".join(parts)
            )
        else:
            lines.append(
                "- Saved strategies: none trained yet (the bot uses its built-in "
                "analyzer; a strategy can be trained per symbol in Train)"
            )
    except Exception:
        pass
    # The user's own armed price alerts, so the assistant knows what they are
    # already watching and can reference them or propose sensible new ones.
    try:
        armed = db.scalars(
            select(PriceAlert)
            .where(PriceAlert.user_id == user.id, PriceAlert.status == "armed")
            .order_by(PriceAlert.created_at.desc())
            .limit(10)
        ).all()
        if armed:
            lines.append(
                "- Armed price alerts: "
                + "; ".join(f"{a.symbol} {a.condition} {a.price}" for a in armed)
            )
    except Exception:
        pass
    return "\n".join(lines)


# Fields the assistant may PROPOSE changing via chat. Deliberately EXCLUDES
# trading_mode: switching between paper and live is a conscious human step the
# operator takes in Settings, never something a chat proposal can flip. Every key
# here maps to a real, schema-validated field on the settings PATCH endpoint.
_AI_SETTINGS_FLOAT = {
    "risk_per_trade_pct",
    "daily_loss_limit_pct",
    "default_stop_loss_pct",
    "default_take_profit_pct",
    "trailing_stop_pct",
    "max_total_exposure_pct",
    "max_position_pct",
    "min_signal_confidence",
    "paper_taker_fee_pct",
    # Profit-lock + saved-strategy gate + monitor cadence (all risk-management
    # knobs, safe for the AI to propose; the schema PATCH re-validates ranges and
    # the engine clamps profit-lock to clear fees).
    "profit_lock_trigger_pct",
    "profit_lock_floor_pct",
    "strategy_min_return_pct",
    "strategy_min_win_rate_pct",
    "strategy_max_drawdown_pct",
    "monitor_interval_seconds",
    "auto_confirm_ttl_minutes",
}
_AI_SETTINGS_INT = {"max_open_positions", "strategy_min_trades", "reversal_confirm_count"}
_AI_SETTINGS_BOOL = {
    "auto_trade_enabled",
    "use_saved_strategy",
    "ai_trade_confirm",
    "ai_pretrade_analysis",
    "ai_monitor_enabled",
    "auto_pause_in_bear",
    "require_strategy_validation",
    "profit_lock_enabled",
    "take_profit_on_reversal",
    # Confirm-before-live gate. Proposable, but ALSO a risk key below so the AI
    # can propose it yet never auto-flip it OFF on real money.
    "auto_live_confirm",
    # ICT/smart-money lens is read-only (no money behaviour), so it's safe for the
    # AI to toggle and is deliberately NOT in _AI_RISK_SETTINGS below.
    "ict_enabled",
}
_AI_SETTINGS_STR = {"auto_symbols", "auto_timeframe", "auto_confirm_timeframe"}

# The subset of proposable settings that govern real-money RISK or arm AUTONOMY.
# In LIVE mode a change to ANY of these is never auto-applied by autopilot — an
# LLM must not loosen a capital-preservation rule (sizing, stops, loss limits,
# exposure, the profit-lock, the strategy-validation gate) or switch on
# autonomous execution on real money without a human confirming first. (We gate
# on "touches a risk key" rather than trying to judge loosen-vs-tighten, because
# mis-judging direction on real money is the costly error.) Paper is a safe
# sandbox, so autopilot may still auto-apply there. Note: the max-drawdown
# kill-switch and trading_mode aren't proposable at all — the AI can't touch them.
_AI_RISK_SETTINGS = {
    # position sizing & loss limits
    "risk_per_trade_pct", "daily_loss_limit_pct", "default_stop_loss_pct",
    "trailing_stop_pct", "max_total_exposure_pct", "max_position_pct",
    "min_signal_confidence", "max_open_positions",
    # profit-lock protection
    "profit_lock_enabled", "profit_lock_trigger_pct", "profit_lock_floor_pct",
    # strategy-validation gate (unproven edges driving real money)
    "require_strategy_validation", "strategy_min_return_pct",
    "strategy_min_win_rate_pct", "strategy_max_drawdown_pct", "strategy_min_trades",
    # autonomy / execution controls
    "auto_trade_enabled", "use_saved_strategy", "ai_trade_confirm",
    "auto_pause_in_bear", "take_profit_on_reversal", "reversal_confirm_count",
    # confirm-before-live safety gate: the LLM must never silently disarm the
    # human-in-the-loop on real money (nor stretch its freshness window).
    "auto_live_confirm", "auto_confirm_ttl_minutes",
}

# Chart is a VIEW-ONLY action: it changes what the operator is LOOKING AT — the
# symbol, the timeframe, which indicators/overlays are drawn, and clearing the
# hand-drawn lines — and moves no money, touches no account state. So it's always
# safe to autopilot. The indicator keys mirror the frontend IndicatorPrefs exactly
# and the timeframe set mirrors the chart's own picker; anything else is dropped.
_AI_CHART_INDICATORS = {
    "ema9", "ema21", "sma50", "sma200", "bb", "vwap",
    "rsi", "macd", "volume", "volumeProfile",
}
_AI_CHART_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
# The ICT / smart-money overlay keys the assistant may toggle on the chart. These
# mirror the frontend's IctOverlayPrefs (see ictOverlays.ts) exactly; anything else
# is dropped. Toggling an overlay only VIEWS a level the analyzer already computed —
# it never fabricates one, moves money, or touches account state, so it's as
# autopilot-safe as an indicator toggle.
_AI_CHART_ICT = {
    "swings", "structure", "sweeps", "orderBlocks", "fvg", "breakers",
    "rejection", "bpr", "volumeImbalance", "liquidity", "dealingRange", "keyLevels",
}


def _coerce_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return None


def _normalize_proposed_action(raw: dict | None, engine) -> dict | None:
    """Validate an AI-proposed action against a strict allowlist.

    The assistant only ever SUGGESTS an action; this turns its raw tag into a
    safe, typed proposal the frontend renders on a Confirm card — or None if it's
    malformed or outside what we permit. Nothing here executes: real execution
    happens only after the operator confirms and the frontend calls the normal
    authenticated endpoint (/api/order, PATCH /api/settings, /api/bot/*,
    /api/train), which re-checks licence, risk rules and live-trade permission.
    Defensive on purpose — never raises; anything unexpected yields None.
    """
    if not isinstance(raw, dict):
        return None
    try:
        atype = str(raw.get("type", "")).strip().lower()
        reason = str(raw.get("reason", "")).strip()[:280] or None
        # AUTOPILOT policy (decided server-side, never trusted from the client):
        # when the operator has turned autopilot on, the SAFE subset is applied
        # automatically; otherwise every action waits for a manual Confirm tap.
        # A LIVE (real-money) order is NEVER auto-applied, autopilot or not — that
        # always needs an explicit human confirmation. ``auto`` tells the frontend
        # which path to take; it can only ever RELAX to a confirm card, never the
        # reverse (the real endpoints re-check licence/risk/live-permission again).
        autopilot = bool(getattr(getattr(engine, "settings", None), "ai_autopilot_enabled", False))
        live = bool(getattr(getattr(engine, "settings", None), "is_live", False))

        if atype == "order":
            side = str(raw.get("side", "")).strip().lower()
            if side not in ("buy", "sell", "close"):
                return None
            symbol = str(raw.get("symbol", "")).strip().upper()
            if not symbol or "/" not in symbol:
                return None
            out: dict = {
                "type": "order",
                "side": side,
                "symbol": symbol,
                "reason": reason,
            }
            # SAFETY (M9): the assistant proposes INTENT only — never the money
            # math. Position SIZE, the protective STOP/TAKE-PROFIT and the entry
            # price are ALWAYS owned by the risk manager and deterministic
            # settings, NEVER read from the model's output. A hallucinated amount
            # would otherwise be treated as a deliberate "explicit" size and
            # BYPASS the per-position concentration cap (risk.check only clamps
            # auto-sized orders), and a bogus stop would set the trade's real
            # risk. So we take only {side, symbol}: amount=None makes risk.check
            # size it AND apply the concentration cap, and execute_signal /
            # _fill_pending apply the deterministic _auto_stop / _auto_take. An
            # operator who wants a specific size, stop or limit uses the manual
            # order form, whose numbers are genuinely operator-authored.
            out["amount"] = None  # risk manager sizes it + concentration cap
            # A live order is real money: never autopilot it. A paper order is
            # safe to auto-apply when the operator enabled autopilot.
            out["auto"] = autopilot and not live
            return out

        if atype == "settings":
            changes_in = raw.get("changes")
            if not isinstance(changes_in, dict) or not changes_in:
                return None
            changes: dict = {}
            for key, val in changes_in.items():
                k = str(key).strip()
                if k in _AI_SETTINGS_FLOAT:
                    try:
                        changes[k] = float(val)
                    except (TypeError, ValueError):
                        continue
                elif k in _AI_SETTINGS_INT:
                    try:
                        changes[k] = int(val)
                    except (TypeError, ValueError):
                        continue
                elif k in _AI_SETTINGS_BOOL:
                    b = _coerce_bool(val)
                    if b is not None:
                        changes[k] = b
                elif k in _AI_SETTINGS_STR:
                    changes[k] = str(val).strip()
                # Anything else (e.g. trading_mode, api keys) is silently dropped.
            if not changes:
                return None
            # LIVE guard: a change to any risk- or autonomy-governing setting is
            # never auto-applied on real money — it always waits for a human
            # Confirm tap, even under autopilot. Paper stays a safe sandbox.
            touches_risk = any(k in _AI_RISK_SETTINGS for k in changes)
            auto = autopilot and not (live and touches_risk)
            return {"type": "settings", "changes": changes, "reason": reason, "auto": auto}

        if atype == "bot":
            state = str(raw.get("state", "")).strip().lower()
            if state not in ("start", "stop"):
                return None
            # Stopping is always safe (it only reduces activity). STARTING live
            # autonomous trading is a real-money escalation, so in live it needs a
            # human confirm even under autopilot; a paper start may auto-apply.
            auto = autopilot and not (live and state == "start")
            return {"type": "bot", "state": state, "reason": reason, "auto": auto}

        if atype == "alert":
            symbol = str(raw.get("symbol", "")).strip().upper()
            if not symbol or "/" not in symbol:
                return None
            condition = str(raw.get("condition", "")).strip().lower()
            if condition not in ("above", "below"):
                return None
            try:
                price = float(raw.get("price"))
            except (TypeError, ValueError):
                return None
            if price <= 0:
                return None
            note = str(raw.get("note", "")).strip()[:200] or None
            return {
                "type": "alert",
                "symbol": symbol,
                "condition": condition,
                "price": price,
                "note": note,
                "reason": reason,
                "auto": autopilot,
            }

        if atype == "train":
            symbol = str(raw.get("symbol", "")).strip().upper()
            if not symbol or "/" not in symbol:
                return None
            strategy = str(raw.get("strategy", "ma_cross")).strip()
            if strategy not in STRATEGY_REGISTRY:
                return None
            timeframe = str(raw.get("timeframe", "1h")).strip() or "1h"
            return {
                "type": "train",
                "symbol": symbol,
                "strategy": strategy,
                "timeframe": timeframe,
                "reason": reason,
                "auto": autopilot,
            }

        if atype == "chart":
            # VIEW-ONLY: change what the operator is looking at. Moves no money and
            # touches no account state, so it's autopilot-safe like the other
            # non-order actions. `undo` is a standalone request the frontend maps to
            # its own view-history stack; otherwise we require at least one real
            # change so an empty tag never renders a do-nothing card.
            out: dict = {"type": "chart", "reason": reason, "auto": autopilot}
            if _coerce_bool(raw.get("undo")):
                out["undo"] = True
                return out
            changed = False
            sym = raw.get("symbol")
            if sym is not None:
                s = str(sym).strip().upper()
                if "/" in s:
                    out["symbol"] = s
                    changed = True
            tf = raw.get("timeframe")
            if tf is not None:
                t = str(tf).strip().lower()
                if t in _AI_CHART_TIMEFRAMES:
                    out["timeframe"] = t
                    changed = True
            inds_in = raw.get("indicators")
            if isinstance(inds_in, dict):
                inds: dict = {}
                for k, v in inds_in.items():
                    key = str(k).strip()
                    if key in _AI_CHART_INDICATORS:
                        b = _coerce_bool(v)
                        if b is not None:
                            inds[key] = b
                if inds:
                    out["indicators"] = inds
                    changed = True
            ict_in = raw.get("ict")
            if isinstance(ict_in, dict):
                ict_patch: dict = {}
                for k, v in ict_in.items():
                    key = str(k).strip()
                    if key in _AI_CHART_ICT:
                        b = _coerce_bool(v)
                        if b is not None:
                            ict_patch[key] = b
                if ict_patch:
                    out["ict"] = ict_patch
                    changed = True
            if _coerce_bool(raw.get("clear_drawings")):
                out["clear_drawings"] = True
                changed = True
            if not changed:
                return None
            return out
    except Exception:
        return None
    return None


# Vision attachments the assistant can read. Kept small and STRICTLY validated:
# the frontend downscales/re-encodes to JPEG, but the client is never trusted, so
# the media type is re-checked against a tight allowlist and the DECODED size is
# capped — a huge or bogus payload can't burden the provider or this process.
# Returns a clean {"data", "media_type"} dict or raises HTTPException(400/413).
_CHAT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_CHAT_IMAGE_MAX_BYTES = 6 * 1024 * 1024  # 6 MB decoded — generous for a downscaled chart


def _validate_chat_image(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="image must be an object")
    media_type = str(raw.get("media_type", "")).strip().lower()
    data = raw.get("data")
    if media_type not in _CHAT_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="image type must be one of: " + ", ".join(sorted(_CHAT_IMAGE_TYPES)),
        )
    if not isinstance(data, str) or not data:
        raise HTTPException(status_code=400, detail="image data is required")
    # Bound the base64 string BEFORE decoding so a giant payload can't cost work.
    if len(data) > _CHAT_IMAGE_MAX_BYTES * 2:
        raise HTTPException(status_code=413, detail="image is too large (max 6 MB)")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="image data is not valid base64")
    if not decoded:
        raise HTTPException(status_code=400, detail="image data is empty")
    if len(decoded) > _CHAT_IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="image is too large (max 6 MB)")
    return {"data": data, "media_type": media_type}


@app.post("/api/ai/chat")
def ai_chat(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Assistant chat grounded in the user's OWN bot state + optional live news.

    Privacy: only NON-secret context (mode, risk config, position/PnL summary) and
    public headlines are sent to the user's OWN configured AI provider — never
    exchange keys, passwords, or any other user's data. The assistant may PROPOSE
    an action (order/settings/bot/train), returned as ``proposed_action``; it never
    executes anything — the operator confirms on a card and the frontend then calls
    the normal authenticated endpoint.
    """
    _enforce_rate_limit(request, "ai", limit=20, window_seconds=60, subject=f"u{user.id}")
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 2000:
        raise HTTPException(
            status_code=400, detail="question is too long (max 2000 characters)"
        )
    engine = _engine_for(db, user)
    symbol = payload.get("symbol")
    timeframe = payload.get("timeframe", "1h")
    # An optional attached image the assistant reads (vision). Validated + size
    # capped; a bad shape is a 400, never a 500 or a silent drop.
    image = None
    raw_image = payload.get("image")
    if raw_image is not None:
        image = _validate_chat_image(raw_image)
    # Only compute analysis if a symbol was given; a bad symbol shouldn't 502 the
    # whole chat, so degrade gracefully to no-analysis context.
    analysis = None
    ict = None
    if symbol:
        try:
            analysis, ict = _analysis_for(engine, str(symbol), str(timeframe))
        except Exception:
            analysis = None
            ict = None

    # Full NON-secret snapshot of THIS user's own account so the assistant can
    # answer concretely ("how am I doing", "am I set up") — never any secret or
    # another user's data (see _assistant_account_context).
    try:
        bot_context: str | None = _assistant_account_context(db, user, engine)
    except Exception:
        bot_context = None

    # Ground the assistant in the REAL exchange-connection state so it can tell
    # the user, honestly, whether they're connected to Binance and — if not —
    # exactly why and what to do. Best-effort: a probe failure must not break chat.
    try:
        acc = engine.connector.check_trading_access()
        if acc.get("ok"):
            conn = (
                f"connected & trade-ready on {acc.get('exchange', 'binance')} "
                f"({'testnet' if acc.get('testnet') else 'live'})"
            )
        else:
            conn = (
                f"NOT trade-ready on {acc.get('exchange', 'binance')} "
                f"({'testnet' if acc.get('testnet') else 'live'}): "
                f"public_data={'ok' if acc.get('can_read_public') else 'FAIL'}, "
                f"account_read={'ok' if acc.get('can_read_account') else 'FAIL'}, "
                f"trading={'ok' if acc.get('can_trade') else 'FAIL'}. "
                f"Reason: {acc.get('detail')}"
            )
        line = f"\n- Exchange connection: {conn}"
        bot_context = (bot_context + line) if bot_context else line.strip("\n- ")
    except Exception:
        pass

    news: list[dict] = []
    used_news = False
    if payload.get("include_news"):
        try:
            news, _errors = fetch_market_news(get_settings().news_feed_list, limit=8)
        except Exception:
            news = []
        used_news = bool(news)

    # Prior conversation turns from the browser so the assistant can follow a
    # multi-turn task instead of answering each question cold. Untrusted input:
    # slice to a sane bound here (ai.chat sanitises roles/content and keeps only
    # the most recent turns); a bad shape simply yields no memory, never a 500.
    raw_history = payload.get("history")
    history = raw_history[-40:] if isinstance(raw_history, list) else None

    reply = engine.ai.chat(
        question,
        analysis=analysis,
        ict=ict,
        bot_context=bot_context,
        news=news or None,
        history=history,
        image=image,
    )
    # The reply may carry a hidden [[action:{...}]] tag proposing a real action.
    # Strip it (never shown raw) and validate it into a safe, typed proposal the UI
    # confirms before anything runs. No AI key -> chat() returned a plain string ->
    # no tag -> no action. Execution only happens when the operator confirms and the
    # frontend calls the normal authenticated endpoint.
    clean_reply, raw_action = strip_action_tag(reply)
    proposed_action = _normalize_proposed_action(raw_action, engine)
    # SAFETY (M11): a reply grounded in third-party NEWS must never AUTO-apply an
    # action. Untrusted headlines are a prompt-injection surface — even though we
    # fence them and defang action tags before they reach the model, if an
    # injection ever slipped a proposal through, acting on it would still require
    # an explicit human Confirm. Downgrade auto -> confirm here; never the reverse.
    if used_news and proposed_action is not None and proposed_action.get("auto"):
        proposed_action["auto"] = False
        proposed_action["injection_guard"] = True
    return {
        "reply": clean_reply,
        "ai_enabled": engine.ai.available,
        "used_news": used_news,
        "proposed_action": proposed_action,
    }


@app.get("/api/ai/health")
def ai_health(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Real reachability check for the app's AI provider (no secrets returned).

    Makes one tiny live request so the assistant dashboard can show a concrete
    status — "connected", "key rejected (401)", "model not found (404)",
    "unreachable" — instead of a silent failure. Rate-limited because it costs
    a provider call.
    """
    _enforce_rate_limit(request, "ai", limit=20, window_seconds=60, subject=f"u{user.id}")
    return _engine_for(db, user).ai.health()


# ---- Strategies & training -----------------------------------------


@app.get("/api/strategies")
def strategies(user: User = Depends(get_current_user)):
    return [
        {"name": name, "params": PARAM_GRIDS.get(name, {})}
        for name in STRATEGY_REGISTRY
    ]


@app.post("/api/train")
def train_strategy(
    symbol: str,
    strategy: str = "ma_cross",
    timeframe: str = "1h",
    limit: int = 500,
    starting_balance: float = 10_000.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    save: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    engine = _engine_for(db, user)
    if strategy not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown strategy '{strategy}'")
    # Optimise against the SAME exit rules the bot trades with (from this user's
    # settings unless overridden), so a "winning" config isn't one that only
    # looks good without stops.
    s = engine.settings
    sl = s.default_stop_loss_pct if stop_loss_pct is None else stop_loss_pct
    tp = s.default_take_profit_pct if take_profit_pct is None else take_profit_pct
    trail = s.trailing_stop_pct if trailing_stop_pct is None else trailing_stop_pct
    try:
        raw = engine.connector.fetch_ohlcv(symbol.upper(), timeframe, max(1, min(limit, 1000)))
    except Exception as exc:
        raise _upstream_error("OHLCV unavailable", exc)
    if not raw:
        raise HTTPException(status_code=502, detail="No candle data returned")
    df = pd.DataFrame(
        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    try:
        report = train(
            df, strategy, symbol=symbol.upper(), timeframe=timeframe,
            starting_balance=starting_balance, fee_pct=fee_pct,
            slippage_pct=slippage_pct, stop_loss_pct=sl,
            take_profit_pct=tp, trailing_stop_pct=trail,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Persist the winning config to THIS user's account so the bot can actually
    # trade with it later (Settings → "use saved strategy"). This is the answer
    # to "where do the strategies I trained go" — they are saved per-symbol and
    # survive restarts. We save real, measured params only; never a fabricated
    # winner. `save=false` lets a caller train-and-preview without persisting.
    saved = False
    if save and report.best is not None:
        b = report.best
        engine.set_strategy_config(
            db,
            symbol.upper(),
            {
                "strategy": report.strategy,
                "timeframe": report.timeframe,
                "params": b.params,
                "metrics": {
                    "total_return_pct": b.total_return_pct,
                    "win_rate_pct": b.win_rate_pct,
                    "max_drawdown_pct": b.max_drawdown_pct,
                    "num_trades": b.num_trades,
                    "score": b.score,
                    "validation_return_pct": b.validation_return_pct,
                    "validation_num_trades": b.validation_num_trades,
                    "validation_win_rate_pct": b.validation_win_rate_pct,
                    "validation_max_drawdown_pct": b.validation_max_drawdown_pct,
                    "overfit_gap_pct": b.overfit_gap_pct,
                },
                "trained_at": _utcnow().isoformat(),
            },
        )
        saved = True
    return {
        "symbol": report.symbol,
        "strategy": report.strategy,
        "timeframe": report.timeframe,
        "candles": report.candles,
        "tested": report.tested,
        "train_fraction": report.train_fraction,
        "warning": report.warning,
        "best": report.best.__dict__ if report.best else None,
        "leaderboard": [c.__dict__ for c in report.leaderboard],
        "saved": saved,
    }


@app.get("/api/strategies/saved")
def saved_strategies(
    db: Session = Depends(get_db), user: User = Depends(require_licensed_user)
):
    """The strategies THIS user has trained and saved, keyed by symbol.

    These are the configs the bot trades with when Settings
    ``use_saved_strategy`` is on. Real, persisted training results only — an
    empty list means nothing has been trained-and-saved yet, never a stub. Each
    row also carries a ``validation`` block: whether the strategy's REAL saved
    metrics clear the operator's gate (so the UI can show "validated — will
    auto-trade" vs "not validated — analyzer decides"), computed from measured
    numbers only, never fabricated.
    """
    engine = _engine_for(db, user)
    gate_on = bool(getattr(engine.settings, "require_strategy_validation", True))
    uss_on = bool(getattr(engine.settings, "use_saved_strategy", False))
    rows = []
    for sym, cfg in sorted(engine.strategy_configs.items()):
        ok, reason, detail = engine._validate_saved_strategy(cfg)
        rows.append({
            "symbol": sym,
            **cfg,
            "validation": {
                "ok": ok,
                "reason": reason,
                "gate_enabled": gate_on,
                "use_saved_strategy": uss_on,
                # Whether this strategy's BUY will actually be trusted for live
                # entries right now: the "use saved strategy" switch must be on, and
                # either the gate is off or the strategy passed it. (A SELL/exit is
                # never gated regardless.)
                "will_auto_trade": uss_on and ((not gate_on) or ok),
                **detail,
            },
        })
    return rows


@app.delete("/api/strategies/saved/{symbol:path}")
def delete_saved_strategy(
    symbol: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_licensed_user),
):
    """Forget a saved strategy for a symbol. The bot immediately stops trading
    that symbol with it (falls back to the analyzer brain)."""
    engine = _engine_for(db, user)
    removed = engine.remove_strategy_config(db, symbol.upper())
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"No saved strategy for {symbol.upper()}"
        )
    return {"removed": True, "symbol": symbol.upper()}


# ---- Admin: user & licence management ------------------------------


@app.get("/api/admin/users", response_model=list[UserOut])
def admin_list_users(
    db: Session = Depends(get_db), _admin: User = Depends(require_admin)
):
    return list(db.scalars(select(User).order_by(User.id.asc())).all())


@app.patch("/api/admin/users/{user_id}/license", response_model=UserOut)
def admin_set_license(
    user_id: int,
    body: LicenseUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id and body.status != LicenseStatus.active.value:
        raise HTTPException(status_code=400, detail="Refusing to de-license yourself")

    target.license_status = body.status
    if body.status == LicenseStatus.active.value:
        # A manual admin "Grant" is a lifetime licence: clear any expiry so the
        # user stays live until explicitly revoked. Use "add days" for a
        # time-limited grant instead.
        target.license_expires_at = None
        if target.licensed_at is None:
            target.licensed_at = _utcnow()
    db.commit()
    db.refresh(target)
    # Revoked/pending users must stop trading immediately: drop their engine.
    if body.status != LicenseStatus.active.value:
        get_manager().drop(target.id)
    return target


@app.post("/api/admin/users/{user_id}/add-days", response_model=UserOut)
def admin_add_license_days(
    user_id: int,
    body: AddLicenseDays,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Extend (or start) a user's time-limited licence by N days and make them
    live. Days stack on whatever is left: from now if lapsed/lifetime-less, or
    from the current future expiry so unused time is never lost."""
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    now = _utcnow()
    current = _as_utc(target.license_expires_at)
    base = current if (current is not None and current > now) else now
    target.license_expires_at = base + dt.timedelta(days=int(body.days))
    target.license_status = LicenseStatus.active.value
    if target.licensed_at is None:
        target.licensed_at = now
    db.commit()
    db.refresh(target)
    return target


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Refusing to delete yourself")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Tear down the account fully. user_id on these rows is a plain indexed column
    # (not a FK with ON DELETE CASCADE), so nothing is removed for us — without
    # this, a deleted user's trades/signals/alerts and their KV state (including a
    # simulated wallet) would be orphaned in the DB, and a re-used id could inherit
    # them. Free any licence key they redeemed so it can be reissued.
    get_manager().drop(target.id)
    db.execute(delete(Trade).where(Trade.user_id == target.id))
    db.execute(delete(SignalLog).where(SignalLog.user_id == target.id))
    db.execute(delete(PriceAlert).where(PriceAlert.user_id == target.id))
    db.execute(
        update(LicenseKey)
        .where(LicenseKey.redeemed_by == target.id)
        .values(redeemed_by=None)
    )
    purge_user_state(db, target.id)
    db.delete(target)
    db.commit()
    return {"deleted": user_id}


# ---- Admin: licence keys (self-service activation) -----------------


@app.get("/api/admin/license-keys", response_model=list[LicenseKeyOut])
def admin_list_license_keys(
    db: Session = Depends(get_db), _admin: User = Depends(require_admin)
):
    """Newest-first list of licence keys. Never returns plaintext keys."""
    return list(db.scalars(select(LicenseKey).order_by(LicenseKey.id.desc())).all())


@app.post("/api/admin/license-keys", response_model=LicenseKeyCreated)
def admin_create_license_key(
    body: LicenseKeyCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Mint a new licence key. The plaintext is returned ONCE here and never
    stored — only its SHA-256 hash is persisted, so copy it now."""
    plaintext = new_license_key()
    key = LicenseKey(
        key_hash=hash_license_key(plaintext),
        key_prefix=plaintext[:7],  # "TT-Ab3" — recognisable, not guessable
        label=(body.label or "").strip() or None,
        duration_days=body.duration_days,  # None = lifetime key
        status=LicenseKeyStatus.unused.value,
        created_by=admin.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return LicenseKeyCreated(
        id=key.id,
        key_prefix=key.key_prefix,
        label=key.label,
        duration_days=key.duration_days,
        status=key.status,
        created_at=key.created_at,
        redeemed_by=key.redeemed_by,
        redeemed_at=key.redeemed_at,
        key=plaintext,
    )


@app.post("/api/admin/license-keys/{key_id}/revoke", response_model=LicenseKeyOut)
def admin_revoke_license_key(
    key_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Invalidate an UNUSED key so it can no longer be redeemed. A key that was
    already redeemed can't be revoked here — revoke that user's licence instead."""
    key = db.get(LicenseKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="Licence key not found")
    if key.status == LicenseKeyStatus.redeemed.value:
        raise HTTPException(
            status_code=400,
            detail="That key was already redeemed. Revoke the user's licence in Users instead.",
        )
    key.status = LicenseKeyStatus.revoked.value
    db.commit()
    db.refresh(key)
    return key


@app.post("/api/license/redeem", response_model=MeOut)
def redeem_license_key(
    body: RedeemLicenseKey,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """A logged-in user activates (or RENEWS) their account with a valid key.

    Works for a *pending* account and for one whose time-limited licence has
    *expired* (redeeming renews it from now). A currently-live licence needs no
    key, and a *revoked* one cannot be self-restored — that is an admin action.
    Keys are single-use and claimed atomically to avoid double-redeem.
    """
    # Blunt brute-forcing of keys: cap redeem attempts per source IP.
    _enforce_rate_limit(request, "redeem", limit=10, window_seconds=600)
    if user.license_active:  # truly live (active AND not expired)
        raise HTTPException(status_code=400, detail="Your licence is already active.")
    if user.license_status == LicenseStatus.revoked.value:
        raise HTTPException(
            status_code=403,
            detail="Your licence was revoked by the administrator; a key can't restore it.",
        )
    now = _utcnow()
    key_hash = hash_license_key(body.key)
    # Atomically claim the key: only an *unused* key with this hash flips to
    # redeemed. rowcount != 1 means no such unused key (bad / used / revoked /
    # a racing redeemer won) — reject without leaking which case it was.
    claimed = db.execute(
        update(LicenseKey)
        .where(
            LicenseKey.key_hash == key_hash,
            LicenseKey.status == LicenseKeyStatus.unused.value,
        )
        .values(
            status=LicenseKeyStatus.redeemed.value,
            redeemed_by=user.id,
            redeemed_at=now,
        )
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise HTTPException(status_code=400, detail="Invalid or already-used licence key.")
    # Read the (now-claimed) key to honour its day-count: time-limited keys set
    # an expiry; a null duration means a lifetime licence.
    lk = db.scalars(
        select(LicenseKey).where(LicenseKey.key_hash == key_hash)
    ).first()
    user.license_status = LicenseStatus.active.value
    user.license_expires_at = (
        now + dt.timedelta(days=int(lk.duration_days))
        if lk and lk.duration_days
        else None
    )
    if user.licensed_at is None:
        user.licensed_at = now
    db.commit()
    db.refresh(user)
    return _me_out(user)


# ---- WebSocket ------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    """Authenticated status stream.

    The access token is read from the ``Sec-WebSocket-Protocol`` header — the
    client connects with subprotocols ``["bearer", "<token>"]`` — so the token
    never appears in the URL (and thus never in proxy/uvicorn access logs). A
    legacy ``?token=`` query param is still accepted as a fallback.
    """
    from app.security import decode_access_token
    from app.database import SessionLocal

    # Prefer the token carried as a WebSocket subprotocol; fall back to query.
    subprotocols = list(ws.scope.get("subprotocols") or [])
    accept_subprotocol: str | None = None
    if len(subprotocols) >= 2 and subprotocols[0] == "bearer":
        token = subprotocols[1]
        accept_subprotocol = "bearer"  # must echo an offered subprotocol

    # Accept FIRST (echoing the offered subprotocol) so that an auth failure can
    # be reported with a clean application close code (4401) the client can act
    # on, rather than a bare handshake rejection (which surfaces as 1006).
    await ws.accept(subprotocol=accept_subprotocol)

    secret = get_settings().secret_key
    payload = decode_access_token(secret, token or "")
    if not payload:
        await ws.close(code=4401)
        return
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        await ws.close(code=4401)
        return
    await broadcaster.connect(ws, user_id, accept=False)
    try:
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            if user:
                await ws.send_json(
                    {"event": "status", "data": get_manager().get(db, user).status(db)}
                )
        finally:
            db.close()
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await broadcaster.disconnect(ws)
    except Exception:
        await broadcaster.disconnect(ws)


# ---- Static frontend (single-service deploy, e.g. Railway) ----------
_STATIC_DIR = Path(os.getenv("STATIC_DIR", Path(__file__).resolve().parent.parent / "static"))
if _STATIC_DIR.is_dir() and (_STATIC_DIR / "index.html").is_file():
    app.mount(
        "/assets",
        StaticFiles(directory=str(_STATIC_DIR / "assets")),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    _STATIC_ROOT = _STATIC_DIR.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith(("api/", "ws")):
            raise HTTPException(status_code=404, detail="Not found")
        index = _STATIC_ROOT / "index.html"
        # Resolve the requested path and confirm it stays INSIDE the static root
        # before serving it. Without this containment check a crafted path like
        # "../../etc/passwd" could escape the static dir (path traversal).
        candidate = (_STATIC_ROOT / full_path).resolve()
        try:
            candidate.relative_to(_STATIC_ROOT)
        except ValueError:
            return FileResponse(str(index))
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index))

    logger.info("Serving dashboard from %s", _STATIC_DIR)
else:
    logger.info("No static dashboard at %s; running API-only", _STATIC_DIR)
