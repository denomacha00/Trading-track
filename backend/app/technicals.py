"""TradingView-style "Technicals" summary — a real, computed rating gauge.

This reproduces the *methodology* of TradingView's Technical Analysis summary:
a basket of classic oscillators and a ladder of moving averages, each casting a
Buy / Sell / Neutral vote from real indicator math, aggregated into three gauges
(Oscillators, Moving Averages, and an overall Summary) rated on the
Strong Sell → Strong Buy scale.

Honesty (per the project rule — never fabricate a number):
- Every value is computed from the REAL candles handed in. Nothing is invented.
- An indicator that lacks enough history returns ``None`` and is SKIPPED from the
  vote counts rather than defaulted to a neutral 0 — a thin frame simply rates on
  fewer inputs, and the response says how many voted.
- The buy/sell/neutral rule for each indicator is the standard, documented one
  (close to TradingView's own Technical Ratings), and is stated in code so the
  read is fully explainable — not a black box.

Unlike the deterministic verdict in :mod:`app.analysis` (which drives trades and
therefore decides on CLOSED bars only, to never repaint), this is a *live
snapshot* the operator reads — exactly like the TradingView gauge — so it is
computed on the frame as given, including the still-forming bar.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from app.analysis import ema, macd, rsi

# Moving-average ladder shown in the gauge (period list matches TradingView).
_MA_PERIODS = [10, 20, 30, 50, 100, 200]


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _wma(series: pd.Series, period: int) -> pd.Series:
    """Linearly weighted moving average (weights 1..period, newest heaviest)."""
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(
        lambda x: float(np.dot(x, weights) / weights.sum()), raw=True
    )


def _hull(series: pd.Series, period: int = 9) -> pd.Series:
    """Hull moving average — WMA(2*WMA(n/2) - WMA(n)) smoothed over sqrt(n)."""
    half = max(1, period // 2)
    sqrt_n = max(1, int(round(math.sqrt(period))))
    return _wma(2 * _wma(series, half) - _wma(series, period), sqrt_n)


def _vwma(close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume-weighted moving average."""
    pv = (close * volume).rolling(period).sum()
    vol = volume.rolling(period).sum()
    return pv / vol.replace(0.0, np.nan)


def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA), as TradingView uses for RSI/ATR/ADX."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _stoch(high, low, close, k_period=14, smooth_k=3, smooth_d=3):
    ll = low.rolling(k_period).min()
    hh = high.rolling(k_period).max()
    raw_k = 100.0 * (close - ll) / (hh - ll).replace(0.0, np.nan)
    k = raw_k.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return k, d


def _cci(high, low, close, period=20):
    tp = (high + low + close) / 3.0
    sma_tp = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(
        lambda x: float(np.abs(x - x.mean()).mean()), raw=True
    )
    return (tp - sma_tp) / (0.015 * mad.replace(0.0, np.nan))


def _adx(high, low, close, period=14):
    """Average Directional Index with +DI/-DI (Wilder). Returns (adx, di+, di-)."""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = _rma(tr, period)
    plus_di = 100.0 * _rma(plus_dm, period) / atr.replace(0.0, np.nan)
    minus_di = 100.0 * _rma(minus_dm, period) / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = _rma(dx, period)
    return adx, plus_di, minus_di


def _awesome(high, low):
    hl2 = (high + low) / 2.0
    return hl2.rolling(5).mean() - hl2.rolling(34).mean()


def _williams_r(high, low, close, period=14):
    hh = high.rolling(period).max()
    ll = low.rolling(period).min()
    return -100.0 * (hh - close) / (hh - ll).replace(0.0, np.nan)


def _ultimate(high, low, close, s=7, m=14, l=28):
    prev_close = close.shift(1)
    true_low = pd.concat([low, prev_close], axis=1).min(axis=1)
    true_high = pd.concat([high, prev_close], axis=1).max(axis=1)
    bp = close - true_low
    tr = true_high - true_low
    tr = tr.replace(0.0, np.nan)
    avg_s = bp.rolling(s).sum() / tr.rolling(s).sum()
    avg_m = bp.rolling(m).sum() / tr.rolling(m).sum()
    avg_l = bp.rolling(l).sum() / tr.rolling(l).sum()
    return 100.0 * (4 * avg_s + 2 * avg_m + avg_l) / 7.0


def _stoch_rsi(close, rsi_period=14, stoch_period=14, smooth_k=3, smooth_d=3):
    r = rsi(close, rsi_period)
    ll = r.rolling(stoch_period).min()
    hh = r.rolling(stoch_period).max()
    stoch = 100.0 * (r - ll) / (hh - ll).replace(0.0, np.nan)
    k = stoch.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return k, d


def _ichimoku(high, low):
    conv = (high.rolling(9).max() + low.rolling(9).min()) / 2.0
    base = (high.rolling(26).max() + low.rolling(26).min()) / 2.0
    lead1 = (conv + base) / 2.0
    lead2 = (high.rolling(52).max() + low.rolling(52).min()) / 2.0
    return conv, base, lead1, lead2


# ---- per-indicator ratings (standard, documented rules) -------------
#
# Each returns "buy" | "sell" | "neutral". A NaN input yields "neutral" only
# when the value exists but is undecided; when the whole series is too short the
# caller passes value=None and the indicator is dropped from the vote entirely.


def _val(series: pd.Series, i: int = -1) -> Optional[float]:
    """Last (or i-th) finite value of a series, or None if unavailable/NaN."""
    try:
        v = float(series.iloc[i])
    except (IndexError, ValueError, TypeError):
        return None
    return None if (v != v or v in (float("inf"), float("-inf"))) else v


_SIGNAL_NUM = {"buy": 1, "sell": -1, "neutral": 0}


def _rate_value(v: float) -> str:
    """TradingView's own bucketing of a mean-signal value into a rating label."""
    if v >= 0.5:
        return "strong_buy"
    if v >= 0.1:
        return "buy"
    if v > -0.1:
        return "neutral"
    if v > -0.5:
        return "sell"
    return "strong_sell"


def _group(items: list[dict]) -> dict:
    """Aggregate a list of {name,value,signal} items into a rated gauge."""
    voted = [it for it in items if it["signal"] in _SIGNAL_NUM]
    buy = sum(1 for it in voted if it["signal"] == "buy")
    sell = sum(1 for it in voted if it["signal"] == "sell")
    neutral = sum(1 for it in voted if it["signal"] == "neutral")
    score = (buy - sell) / len(voted) if voted else 0.0
    return {
        "rating": _rate_value(score) if voted else "neutral",
        "score": round(score, 4),
        "buy": buy,
        "sell": sell,
        "neutral": neutral,
        "items": items,
    }


def compute_technicals(
    df: pd.DataFrame, symbol: str = "", timeframe: str = "1h"
) -> dict:
    """Compute the full Technicals gauge from real OHLCV.

    ``df`` columns: open/high/low/close/volume (a leading ``timestamp`` column is
    fine). Returns a dict with ``summary``/``oscillators``/``moving_averages``
    gauges (each rated Strong Sell→Strong Buy) plus the per-indicator table.
    """
    n = len(df)
    price = _val(df["close"]) if n else None
    if n < 2 or price is None:
        empty = {"rating": "neutral", "score": 0.0, "buy": 0, "sell": 0,
                 "neutral": 0, "items": []}
        return {"symbol": symbol.upper(), "timeframe": timeframe, "price": price,
                "bars": n, "summary": dict(empty), "oscillators": dict(empty),
                "moving_averages": dict(empty),
                "note": "not enough candles to compute technicals"}

    high, low, close = df["high"], df["low"], df["close"]
    volume = df["volume"] if "volume" in df else pd.Series([0.0] * n, index=df.index)

    oscillators = _oscillator_items(high, low, close)
    mas = _moving_average_items(high, low, close, volume, price)

    osc = _group(oscillators)
    ma = _group(mas)
    # Overall = mean of the two group means (TradingView's Summary methodology),
    # averaging only groups that actually had voting indicators.
    parts = [g["score"] for g in (osc, ma) if (g["buy"] + g["sell"] + g["neutral"])]
    overall = sum(parts) / len(parts) if parts else 0.0
    all_items = oscillators + mas
    voted_all = [it for it in all_items if it["signal"] in _SIGNAL_NUM]
    summary = {
        "rating": _rate_value(overall) if voted_all else "neutral",
        "score": round(overall, 4),
        "buy": sum(1 for it in voted_all if it["signal"] == "buy"),
        "sell": sum(1 for it in voted_all if it["signal"] == "sell"),
        "neutral": sum(1 for it in voted_all if it["signal"] == "neutral"),
    }
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "price": price,
        "bars": n,
        "summary": summary,
        "oscillators": osc,
        "moving_averages": ma,
    }


def _item(name: str, value: Optional[float], signal: str) -> dict:
    return {"name": name, "value": value, "signal": signal}


def _oscillator_items(high, low, close) -> list[dict]:
    """The 11-oscillator basket, each rated by its standard buy/sell rule."""
    items: list[dict] = []

    # RSI(14): buy when oversold & turning up, sell when overbought & turning down.
    r = rsi(close, 14)
    rn, rp = _val(r, -1), _val(r, -2)
    if rn is None:
        sig = "n/a"
    elif rn < 30 and rp is not None and rn > rp:
        sig = "buy"
    elif rn > 70 and rp is not None and rn < rp:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("RSI(14)", rn, sig))

    # Stochastic %K(14,3,3): buy when %K>%D in oversold, sell when %K<%D overbought.
    k, d = _stoch(high, low, close)
    kn, dn = _val(k, -1), _val(d, -1)
    if kn is None or dn is None:
        sig = "n/a"
    elif kn < 20 and dn < 20 and kn > dn:
        sig = "buy"
    elif kn > 80 and dn > 80 and kn < dn:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Stochastic %K(14,3,3)", kn, sig))

    # CCI(20): buy below -100 & rising, sell above +100 & falling.
    c = _cci(high, low, close, 20)
    cn, cp = _val(c, -1), _val(c, -2)
    if cn is None:
        sig = "n/a"
    elif cn < -100 and cp is not None and cn > cp:
        sig = "buy"
    elif cn > 100 and cp is not None and cn < cp:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("CCI(20)", cn, sig))

    # ADX(14): directional trend — DI+ over DI- (and widening) buys, the reverse sells.
    adx, pdi, mdi = _adx(high, low, close, 14)
    a, pn, mn = _val(adx), _val(pdi), _val(mdi)
    pnp, mnp = _val(pdi, -2), _val(mdi, -2)
    if a is None or pn is None or mn is None:
        sig = "n/a"
    else:
        diff = pn - mn
        diffp = (pnp - mnp) if (pnp is not None and mnp is not None) else None
        if a > 20 and diff > 0 and (diffp is None or diff > diffp):
            sig = "buy"
        elif a > 20 and diff < 0 and (diffp is None or diff < diffp):
            sig = "sell"
        else:
            sig = "neutral"
    items.append(_item("ADX(14)", a, sig))

    items.extend(_oscillator_items_2(high, low, close))
    return items


def _oscillator_items_2(high, low, close) -> list[dict]:
    """Second half of the oscillator basket (AO, Momentum, MACD, StochRSI,
    Williams %R, Bull Bear Power, Ultimate Oscillator)."""
    items: list[dict] = []

    # Awesome Oscillator: zero-line cross or saucer.
    ao = _awesome(high, low)
    a0, a1, a2 = _val(ao, -1), _val(ao, -2), _val(ao, -3)
    if a0 is None:
        sig = "n/a"
    elif a0 > 0 and a1 is not None and (a1 <= 0 or (a2 is not None and a0 > a1 and a1 < a2)):
        sig = "buy"
    elif a0 < 0 and a1 is not None and (a1 >= 0 or (a2 is not None and a0 < a1 and a1 > a2)):
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Awesome Oscillator", a0, sig))

    # Momentum(10): rising buys, falling sells.
    mom = close - close.shift(10)
    m0, m1 = _val(mom, -1), _val(mom, -2)
    if m0 is None or m1 is None:
        sig = "n/a" if m0 is None else "neutral"
    elif m0 > m1:
        sig = "buy"
    elif m0 < m1:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Momentum(10)", m0, sig))

    # MACD(12,26,9): MACD line above signal buys, below sells.
    macd_line, signal_line, _ = macd(close)
    ml, sl = _val(macd_line), _val(signal_line)
    if ml is None or sl is None:
        sig = "n/a"
    elif ml > sl:
        sig = "buy"
    elif ml < sl:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("MACD(12,26)", ml, sig))

    # Stochastic RSI Fast(3,3,14,14).
    srk, srd = _stoch_rsi(close)
    kn, dn = _val(srk), _val(srd)
    if kn is None or dn is None:
        sig = "n/a"
    elif kn < 20 and dn < 20 and kn > dn:
        sig = "buy"
    elif kn > 80 and dn > 80 and kn < dn:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Stochastic RSI Fast(3,3,14,14)", kn, sig))

    # Williams %R(14).
    wr = _williams_r(high, low, close, 14)
    w0, w1 = _val(wr, -1), _val(wr, -2)
    if w0 is None:
        sig = "n/a"
    elif w0 < -80 and w1 is not None and w0 > w1:
        sig = "buy"
    elif w0 > -20 and w1 is not None and w0 < w1:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Williams %R(14)", w0, sig))

    # Bull Bear Power(13): in an uptrend a recovering bear power buys; in a
    # downtrend a fading bull power sells.
    e13 = ema(close, 13)
    e0, e1 = _val(e13, -1), _val(e13, -2)
    bull, bear = high - e13, low - e13
    bp0, bp1 = _val(bull, -1), _val(bull, -2)
    br0, br1 = _val(bear, -1), _val(bear, -2)
    bbp = _val(bull + bear, -1)
    if None in (e0, e1, bp0, bp1, br0, br1):
        sig = "n/a"
    elif e0 > e1 and br0 < 0 and br0 > br1:
        sig = "buy"
    elif e0 < e1 and bp0 > 0 and bp0 < bp1:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Bull Bear Power(13)", bbp, sig))

    # Ultimate Oscillator(7,14,28).
    uo = _ultimate(high, low, close)
    u = _val(uo)
    if u is None:
        sig = "n/a"
    elif u > 70:
        sig = "buy"
    elif u < 30:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Ultimate Oscillator(7,14,28)", u, sig))
    return items


def _moving_average_items(high, low, close, volume, price: float) -> list[dict]:
    """SMA/EMA ladder + Ichimoku, VWMA and Hull MA. Each rates buy when it sits
    below price (price above the average = bullish), sell when above."""

    def ma_signal(v: Optional[float]) -> str:
        if v is None:
            return "n/a"
        if v < price:
            return "buy"
        if v > price:
            return "sell"
        return "neutral"

    items: list[dict] = []
    for p in _MA_PERIODS:
        sv = _val(_sma(close, p))
        items.append(_item(f"SMA{p}", sv, ma_signal(sv)))
        ev = _val(ema(close, p))
        items.append(_item(f"EMA{p}", ev, ma_signal(ev)))

    # Ichimoku Base Line (9,26,52): full cloud confirmation for buy/sell.
    conv, base, lead1, lead2 = _ichimoku(high, low)
    b, cv, l1, l2 = _val(base), _val(conv), _val(lead1), _val(lead2)
    if None in (b, cv, l1, l2):
        sig = "n/a"
    elif b < price and cv > b and l1 > l2 and l1 > price:
        sig = "buy"
    elif b > price and cv < b and l1 < l2 and l1 < price:
        sig = "sell"
    else:
        sig = "neutral"
    items.append(_item("Ichimoku Base(9,26,52)", b, sig))

    # VWMA(20) — needs real volume; skip (n/a) when the feed carries none.
    if float(volume.abs().sum()) > 0:
        vw = _val(_vwma(close, volume, 20))
        items.append(_item("VWMA(20)", vw, ma_signal(vw)))
    else:
        items.append(_item("VWMA(20)", None, "n/a"))

    hv = _val(_hull(close, 9))
    items.append(_item("Hull MA(9)", hv, ma_signal(hv)))
    return items


_RATING_LABEL = {
    "strong_buy": "STRONG BUY",
    "buy": "BUY",
    "neutral": "NEUTRAL",
    "sell": "SELL",
    "strong_sell": "STRONG SELL",
}


def summarize_technicals(result: dict) -> str:
    """A compact, honest text read of the gauge for the AI assistant to analyse.

    Feeds the assistant the SAME numbers the operator sees, so it reasons about
    real computed technicals instead of guessing. Returns "" when nothing could
    be computed (so the caller can simply skip the block)."""
    if not result:
        return ""
    summ = result.get("summary") or {}
    if not (summ.get("buy", 0) + summ.get("sell", 0) + summ.get("neutral", 0)):
        return ""
    sym = result.get("symbol", "")
    tf = result.get("timeframe", "")
    osc = result.get("oscillators") or {}
    ma = result.get("moving_averages") or {}

    def line(label: str, g: dict) -> str:
        return (f"{label}: {_RATING_LABEL.get(g.get('rating'), 'NEUTRAL')} "
                f"({g.get('buy', 0)} buy / {g.get('sell', 0)} sell / "
                f"{g.get('neutral', 0)} neutral)")

    parts = [
        f"Technicals for {sym} on {tf} (computed live from real candles, "
        "TradingView-style ratings):",
        line("Summary", summ),
        line("Oscillators", osc),
        line("Moving averages", ma),
    ]
    # Name the decisive (non-neutral) indicators so the AI can cite them.
    called = []
    for it in (osc.get("items", []) + ma.get("items", [])):
        if it.get("signal") in ("buy", "sell"):
            v = it.get("value")
            vs = f" {v:.4g}" if isinstance(v, (int, float)) else ""
            called.append(f"{it['name']}{vs} {it['signal']}")
    if called:
        parts.append("Notable: " + "; ".join(called[:14]) + ".")
    return "\n".join(parts)

