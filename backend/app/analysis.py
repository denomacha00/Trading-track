"""Market analysis "brain": a transparent, multi-signal decision engine.

This is what makes Trading-track *think* about the market instead of reacting to
a single crossover. It computes a panel of classic indicators — trend (EMA
stack), momentum (RSI + MACD), volatility (ATR), and recent return — then
combines them into ONE confidence-scored verdict with human-readable reasons
for every component.

Design principles (why this is the honest way to be "smart"):
- Explainable, not a black box: every point in the score has a stated reason.
- Capital-preservation first: when signals disagree or volatility is extreme, it
  returns HOLD with low confidence rather than forcing a trade. "No trade" is a
  valid, often correct, decision — that is how you avoid losing money.
- Confidence-gated: callers should only act above a confidence threshold.
- No look-ahead / no repaint: every indicator uses only data up to the LAST bar
  of the frame it is given, and that last bar is treated as CLOSED. Live callers
  must therefore hand it CLOSED bars only (drop the still-forming candle) — see
  MarketAnalyzer.analyze_live — so a live verdict is computed on exactly the bar
  a backtest would have decided on, and never repaints as the candle fills in.

An AI/LLM layer (see app/ai.py) can *narrate* this analysis, but the decision
itself is deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

Verdict = Literal["buy", "sell", "hold"]


@dataclass
class Factor:
    """One analysed component and its contribution to the decision."""

    name: str
    signal: Verdict
    weight: float
    detail: str


@dataclass
class MarketAnalysis:
    symbol: str
    verdict: Verdict
    confidence: float  # 0..1
    score: float  # signed: >0 bullish, <0 bearish
    price: float
    atr: float = 0.0  # latest ATR (absolute, same units as price); 0 if unknown
    factors: list[Factor] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "price": self.price,
            "atr": round(self.atr, 8),
            "summary": self.summary,
            "factors": [
                {
                    "name": f.name,
                    "signal": f.signal,
                    "weight": f.weight,
                    "detail": f.detail,
                }
                for f in self.factors
            ],
        }


# ---- indicator helpers (no look-ahead) ------------------------------


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing (RMA) so RSI matches TradingView / standard charting
    # tools rather than a plain rolling mean (which diverges from the chart).
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = candles["high"], candles["low"], candles["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    # Wilder's smoothing (RMA), matching TradingView's ATR.
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# ---- the analyser ---------------------------------------------------


MIN_BARS = 60


class MarketAnalyzer:
    """Weighs several independent signals into a single confident verdict."""

    def __init__(
        self,
        *,
        buy_threshold: float = 0.25,
        min_confidence: float = 0.4,
        max_volatility_pct: float = 8.0,
        overext_pct: float = 15.0,
        overext_rsi: float = 78.0,
        shock_atr_mult: float = 3.0,
        weak_volume_ratio: float = 0.6,
        ict_confluence: bool = True,
    ) -> None:
        # Score above +threshold => buy, below -threshold => sell, else hold.
        self.buy_threshold = buy_threshold
        # Below this confidence the verdict is forced to HOLD (preserve capital).
        self.min_confidence = min_confidence
        # If a single bar's ATR exceeds this % of price, stand aside.
        self.max_volatility_pct = max_volatility_pct
        # Chase guard: veto a BUY only when price is BOTH this far above the
        # 21-EMA AND RSI is this hot — a genuine parabolic blow-off, not a normal
        # trend. Buying euphoric extension is a top way traders give back gains.
        self.overext_pct = overext_pct
        self.overext_rsi = overext_rsi
        # A latest bar whose true range exceeds this multiple of ATR is a shock
        # candle (news/liquidation wick): stand aside one bar, don't get whipsawed.
        self.shock_atr_mult = shock_atr_mult
        # A trend/breakout bar on less than this fraction of average volume is
        # weakly backed — keep the trade allowed but discount its confidence.
        self.weak_volume_ratio = weak_volume_ratio
        # When on, a real ICT/smart-money read (computed on the same closed bars)
        # contributes weighted confluence factors to the verdict. Never overrides
        # the capital-preservation vetoes; never invents a level. See _ict_factors.
        self.ict_confluence = ict_confluence

    def min_bars(self) -> int:
        return MIN_BARS

    def analyze(
        self, candles: pd.DataFrame, symbol: str = "", *, ict=None
    ) -> MarketAnalysis:
        price = float(candles["close"].iloc[-1]) if len(candles) else 0.0
        if len(candles) < MIN_BARS:
            return MarketAnalysis(
                symbol=symbol,
                verdict="hold",
                confidence=0.0,
                score=0.0,
                price=price,
                summary="Not enough data to analyse (need ≥ %d bars)." % MIN_BARS,
            )

        close = candles["close"]
        factors: list[Factor] = []

        # 0) Long-term regime — the single most important loss-avoidance filter.
        #    Only lean long ABOVE a rising long EMA, only lean short BELOW a
        #    falling one. Buying a downtrend / shorting an uptrend is where most
        #    accounts bleed out, so counter-regime entries are vetoed outright
        #    further down (not merely down-weighted).
        reg_span = 100 if len(close) >= 120 else 50
        ema_reg = ema(close, reg_span)
        reg_now = float(ema_reg.iloc[-1])
        reg_ref = float(ema_reg.iloc[-6]) if len(close) > 6 else reg_now
        bull_regime = price > reg_now and reg_now >= reg_ref
        bear_regime = price < reg_now and reg_now <= reg_ref
        if bull_regime:
            factors.append(Factor("regime", "buy", 0.25, f"price > rising EMA{reg_span} (bull regime)"))
        elif bear_regime:
            factors.append(Factor("regime", "sell", 0.25, f"price < falling EMA{reg_span} (bear regime)"))
        else:
            factors.append(Factor("regime", "hold", 0.25, f"price ≈ EMA{reg_span} (no clear regime)"))

        # 1) Trend via EMA stack (fast>mid>slow = uptrend).
        ema_fast = ema(close, 9).iloc[-1]
        ema_mid = ema(close, 21).iloc[-1]
        ema_slow = ema(close, 50).iloc[-1]
        if ema_fast > ema_mid > ema_slow:
            trend_sig = "buy"
            factors.append(Factor("trend", "buy", 0.30, "EMA 9>21>50 (uptrend)"))
        elif ema_fast < ema_mid < ema_slow:
            trend_sig = "sell"
            factors.append(Factor("trend", "sell", 0.30, "EMA 9<21<50 (downtrend)"))
        else:
            trend_sig = "hold"
            factors.append(Factor("trend", "hold", 0.30, "EMAs tangled (no clear trend)"))

        # 2) Momentum via RSI (with slope). A reversal signal requires RSI to be
        #    *actually turning* (strict), so a value pegged at 0/100 during a
        #    strong trend reads as momentum confirmation, not a reversal.
        rsi_series = rsi(close, 14)
        rsi_now = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2])
        if rsi_now < 30 and rsi_now > rsi_prev:
            factors.append(Factor("rsi", "buy", 0.20, f"RSI {rsi_now:.0f} oversold & turning up"))
        elif rsi_now > 70 and rsi_now < rsi_prev:
            factors.append(Factor("rsi", "sell", 0.20, f"RSI {rsi_now:.0f} overbought & turning down"))
        elif rsi_now > 50:
            factors.append(Factor("rsi", "buy", 0.10, f"RSI {rsi_now:.0f} above midline"))
        elif rsi_now < 50:
            factors.append(Factor("rsi", "sell", 0.10, f"RSI {rsi_now:.0f} below midline"))
        else:
            factors.append(Factor("rsi", "hold", 0.10, "RSI neutral"))

        # 3) MACD histogram (momentum acceleration / crossover).
        _, _, hist = macd(close)
        h_now, h_prev = float(hist.iloc[-1]), float(hist.iloc[-2])
        if h_now > 0 and h_now >= h_prev:
            factors.append(Factor("macd", "buy", 0.20, "MACD histogram positive & rising"))
        elif h_now < 0 and h_now <= h_prev:
            factors.append(Factor("macd", "sell", 0.20, "MACD histogram negative & falling"))
        else:
            factors.append(Factor("macd", "hold", 0.20, "MACD histogram flattening"))

        # 4) Recent return (short-term drift over ~10 bars).
        ret = (close.iloc[-1] / close.iloc[-11] - 1.0) * 100 if len(close) > 11 else 0.0
        if ret > 1.0:
            factors.append(Factor("momentum", "buy", 0.15, f"+{ret:.1f}% over last 10 bars"))
        elif ret < -1.0:
            factors.append(Factor("momentum", "sell", 0.15, f"{ret:.1f}% over last 10 bars"))
        else:
            factors.append(Factor("momentum", "hold", 0.15, "flat over last 10 bars"))

        # 5) Volatility gate (ATR%). Extreme volatility -> stand aside.
        atr_now = float(atr(candles, 14).iloc[-1])
        vol_pct = (atr_now / price * 100) if price else 0.0
        vol_gated = vol_pct > self.max_volatility_pct
        factors.append(
            Factor(
                "volatility",
                "hold",
                0.0,
                f"ATR {vol_pct:.1f}% of price" + (" — too high, standing aside" if vol_gated else ""),
            )
        )

        # 6) Shock-bar guard: a latest true range far above ATR is a news /
        #    liquidation spike — stand aside one bar rather than chase a wick.
        prev_close = float(close.iloc[-2])
        hi, lo = float(candles["high"].iloc[-1]), float(candles["low"].iloc[-1])
        last_tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        shock = atr_now > 0 and last_tr > self.shock_atr_mult * atr_now
        if shock:
            factors.append(
                Factor("shock", "hold", 0.0,
                       f"latest bar range {last_tr / atr_now:.1f}× ATR — spike, standing aside")
            )

        # 7) Volume confirmation: a move on thin volume is weakly backed. We never
        #    force a trade off volume, but a weak-volume action is de-confidenced.
        weak_volume = False
        if "volume" in candles and len(candles) >= 20:
            vol_now = float(candles["volume"].iloc[-1])
            vol_ma = float(candles["volume"].tail(20).mean())
            weak_volume = vol_ma > 0 and vol_now < self.weak_volume_ratio * vol_ma
            factors.append(
                Factor("volume", "hold", 0.0,
                       f"vol vs 20-bar avg {(vol_now / vol_ma if vol_ma else 0):.2f}×"
                       + (" — thin, discounting" if weak_volume else ""))
            )

        # 8) ICT / smart-money confluence (optional). Structure (BOS/CHoCH/MSS),
        #    premium/discount + OTE, and a FRESH liquidity sweep vote alongside the
        #    classic signals — real levels computed on these closed bars, never a
        #    fabricated one. Passed in by analyze_live; absent in a plain analyze().
        if ict is not None and self.ict_confluence:
            factors.extend(self._ict_factors(ict, len(candles)))

        # ---- combine ----
        score = 0.0
        total_weight = 0.0
        for f in factors:
            total_weight += f.weight
            if f.signal == "buy":
                score += f.weight
            elif f.signal == "sell":
                score -= f.weight
        # Normalise to [-1, 1].
        norm = score / total_weight if total_weight else 0.0

        # Agreement strength: of the weighted factors that took a side, how much
        # weight backs the leading direction vs opposes it. A split market has no
        # edge, so conflict HARSHLY cuts confidence -> more holds -> fewer losses.
        lean = "buy" if norm > 0 else "sell" if norm < 0 else "hold"
        agree = sum(f.weight for f in factors if f.weight > 0 and f.signal == lean)
        oppose = sum(
            f.weight for f in factors
            if f.weight > 0 and f.signal in ("buy", "sell") and f.signal != lean
        )
        confidence = abs(norm)
        if agree + oppose > 0:
            confidence *= agree / (agree + oppose)
        if weak_volume:
            confidence *= 0.85  # thin participation — trust the signal less
        if vol_gated or shock:
            confidence = 0.0  # no action into extreme vol / a shock bar

        if confidence < self.min_confidence or vol_gated or shock:
            verdict: Verdict = "hold"
        elif norm >= self.buy_threshold:
            verdict = "buy"
        elif norm <= -self.buy_threshold:
            verdict = "sell"
        else:
            verdict = "hold"

        # ---- capital-preservation vetoes: never trade against the tide ----
        # These only ever turn an action into HOLD; they never invent a trade.
        stand_aside = ""
        ext_pct = (price / float(ema_mid) - 1.0) * 100 if ema_mid else 0.0
        if verdict == "buy":
            if bear_regime:
                verdict, stand_aside = "hold", "buy blocked — price is under a falling long-term EMA (don't catch a falling knife)"
            elif trend_sig == "sell":
                verdict, stand_aside = "hold", "buy blocked — the short-term trend is still down"
            elif ext_pct > self.overext_pct and rsi_now > self.overext_rsi:
                verdict, stand_aside = "hold", f"buy blocked — overextended (+{ext_pct:.0f}% over EMA21, RSI {rsi_now:.0f}); wait for a pullback"
        elif verdict == "sell":
            if bull_regime:
                verdict, stand_aside = "hold", "sell blocked — price is over a rising long-term EMA (don't short strength)"
            elif trend_sig == "buy":
                verdict, stand_aside = "hold", "sell blocked — the short-term trend is still up"

        summary = self._summarize(verdict, confidence, norm, vol_gated, shock, stand_aside)
        return MarketAnalysis(
            symbol=symbol,
            verdict=verdict,
            confidence=confidence,
            score=norm,
            price=price,
            atr=atr_now,
            factors=factors,
            summary=summary,
        )

    def _ict_factors(self, ict, n_bars: int) -> list[Factor]:
        """Turn a computed ICT read into weighted confluence factors.

        ICT is smart-money *structure*: where structure broke (BOS/CHoCH/MSS),
        whether price sits at a premium or a discount (+ the OTE band), and
        whether a fresh liquidity sweep just ran stops. Each becomes ONE factor
        that votes alongside the classic indicators. Every value is read straight
        off the closed-bar IctAnalysis — nothing is recomputed or invented here,
        so it can't repaint and can't fabricate a level. A thin/mixed ICT read
        simply adds little weight, letting the classic signals decide.
        """
        out: list[Factor] = []
        if ict is None:
            return out
        # 1) Market structure — the most recent confirmed break.
        events = getattr(ict, "events", None) or []
        if events:
            ev = events[-1]
            mss = bool(getattr(ev, "displacement", False) and ev.kind == "CHoCH")
            weight = 0.35 if mss else (0.28 if ev.kind == "CHoCH" else 0.22)
            try:
                if (n_bars - 1 - int(ev.index)) > 15:
                    weight *= 0.5  # a stale break carries less conviction
            except Exception:
                pass
            sig = "buy" if ev.direction == "bull" else "sell"
            tag = "MSS" if mss else ev.kind
            out.append(Factor("ict-structure", sig, round(weight, 3),
                              f"{ev.direction} {tag} (close through {ev.level:g})"))
        elif getattr(ict, "trend", "none") in ("bull", "bear"):
            sig = "buy" if ict.trend == "bull" else "sell"
            out.append(Factor("ict-structure", sig, 0.12, f"structure trend {ict.trend}"))
        # 2) Premium / discount (+ OTE) — where in the dealing range price is.
        dr = getattr(ict, "dealing_range", None)
        if dr is not None:
            if dr.zone in ("discount", "premium"):
                sig = "buy" if dr.zone == "discount" else "sell"
                w = 0.22 if dr.in_ote else 0.12
                out.append(Factor("ict-zone", sig, w,
                                  dr.zone + (" + OTE" if dr.in_ote else "")
                                  + (" (favours longs)" if sig == "buy" else " (favours shorts)")))
            else:
                out.append(Factor("ict-zone", "hold", 0.0, "near equilibrium (no premium/discount edge)"))
        # 3) A FRESH liquidity sweep (stop-hunt) implies a snap the other way.
        sweeps = getattr(ict, "sweeps", None) or []
        if sweeps:
            sw = sweeps[-1]
            try:
                fresh = (n_bars - 1 - int(sw.index)) <= 5
            except Exception:
                fresh = False
            if fresh:
                sig = "buy" if sw.reaction == "bull" else "sell"
                out.append(Factor("ict-sweep", sig, 0.15,
                                  f"{sw.side} sweep at {sw.level:g} — {sw.reaction}ish reaction"))
        return out

    def analyze_live(
        self, candles: pd.DataFrame, symbol: str = "", *, ict=None
    ) -> tuple[MarketAnalysis, pd.DataFrame, object]:
        """Analyse a LIVE feed honestly: decide on CLOSED bars only.

        A live OHLCV feed's most recent candle is still FORMING — its
        open/high/low/close keep moving until the period closes. Reading a
        verdict off that bar makes the signal *repaint* (it can flip as the
        candle fills in) and makes live behave differently from backtest and
        training, which only ever see closed bars. So we drop the forming bar
        for the DECISION, then stamp the live (forming) close back onto the
        result as ``price`` — the verdict, score and ATR are computed purely on
        closed data while the UI still shows the CURRENT price.

        When ICT confluence is on, a real ICT read is computed on the SAME
        closed frame and votes in the verdict (see _ict_factors); it is returned
        too so a caller (the API) can draw/narrate it without a second compute.

        Returns ``(analysis, closed_candles, ict)`` — ``ict`` is None when
        confluence is off or the read failed. The closed frame lets a caller run
        a saved strategy on the exact bars the verdict used.
        """
        n = len(candles)
        live_price = float(candles["close"].iloc[-1]) if n else 0.0
        # Drop the still-forming last bar when there is one to spare; keep the
        # frame intact at the ragged edge so a short history still analyses.
        closed = candles.iloc[:-1] if n >= 2 else candles
        # Compute ICT once (best-effort) only when it will actually vote — a read
        # failure must never break the deterministic verdict. price=None so the
        # premium/discount zone is measured against the last CLOSED price, not the
        # still-forming live tick: otherwise the ict-zone vote could flip intrabar
        # as price crosses equilibrium, reintroducing exactly the repaint we drop
        # the forming bar to avoid. The live price is for DISPLAY only (stamped
        # below); the whole verdict — ICT included — stays a function of closed bars.
        if ict is None and self.ict_confluence:
            try:
                from .ict import analyze_ict

                ict = analyze_ict(closed, symbol, price=None)
            except Exception:
                ict = None
        analysis = self.analyze(closed, symbol, ict=ict)
        if live_price > 0:
            analysis.price = live_price  # display the CURRENT price; verdict is closed-bar
        return analysis, closed, ict


    @staticmethod
    def _summarize(
        verdict: Verdict, confidence: float, norm: float,
        vol_gated: bool, shock: bool = False, stand_aside: str = "",
    ) -> str:
        if vol_gated:
            return "Volatility too high to trade safely — standing aside to protect capital."
        if shock:
            return "A volatility spike just hit — standing aside one bar to avoid a whipsaw."
        if stand_aside:
            return f"Standing aside to protect capital: {stand_aside}."
        bias = "bullish" if norm > 0 else "bearish" if norm < 0 else "neutral"
        if verdict == "hold":
            return (
                f"Signals are {bias} but weak/mixed (confidence {confidence:.0%}). "
                "Holding — no clear edge."
            )
        return (
            f"{verdict.upper()} with {confidence:.0%} confidence: {bias} across "
            "regime, trend and momentum, aligned with the higher-timeframe tide."
        )
