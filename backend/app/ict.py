"""ICT / Smart-Money market-structure engine — a real, honest ICT read.

This is the piece the bot was missing: instead of *refusing* an ICT read, it
now COMPUTES the core Inner-Circle-Trader concepts from real candles and hands
them to the UI (to draw) and to the AI (to reason about). Everything here is
deterministic, explainable, and — like the rest of the brain — computed on
CLOSED bars only so it never repaints and never look-aheads.

Concepts computed (standard definitions, cross-checked against the SMC
literature):
  • Swing points — Williams/ICT fractals: a bar whose high (low) is strictly
    above (below) the ``left`` bars before and ``right`` bars after it. A swing
    is only CONFIRMED ``right`` bars later, so it can never repaint.
  • BOS (Break of Structure) — a CLOSE beyond the most recent swing IN the
    prevailing trend's direction = continuation.
  • CHoCH (Change of Character) — the FIRST close that breaks structure AGAINST
    the trend (breaks the swing that was protecting it) = possible reversal.
  • MSS (Market-Structure Shift) — a CHoCH backed by displacement (a strong,
    impulsive breaking bar). Reported as ``displacement=True`` on a CHoCH.
  • Liquidity sweep (stop hunt) — a wick THROUGH a prior swing high/low that
    CLOSES back inside the range: buy-side (above a high) or sell-side (below a
    low) liquidity taken, implying a reaction the other way.
  • Order block — the last opposite-colour candle before the impulsive move
    that broke structure (bullish OB = last down candle before an up-break).
  • Fair-value gap (FVG / imbalance) — a 3-candle gap where candle 1 and candle
    3 do not overlap (bullish: c3.low > c1.high; bearish: c3.high < c1.low).
  • Premium / discount — the current dealing range's 50% equilibrium; above is
    premium (favour selling), below is discount (favour buying), with the ICT
    OTE (0.62–0.79 retracement) bands marked.

Honesty rules (same as the rest of the bot): never fabricate a level; if there
isn't enough data for a read, say so and return a neutral, empty analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd

from .analysis import atr

Side = Literal["bull", "bear"]

@dataclass
class SwingPoint:
    """A confirmed structural pivot."""

    index: int
    time: Optional[int]  # unix seconds (for the chart), or None if no timestamps
    price: float
    kind: Literal["high", "low"]

    def as_dict(self) -> dict:
        return {"index": self.index, "time": self.time,
                "price": self.price, "kind": self.kind}


@dataclass
class StructureEvent:
    """A close that broke structure — BOS (continuation) or CHoCH (reversal)."""

    index: int          # bar whose CLOSE broke the level
    time: Optional[int]
    kind: Literal["BOS", "CHoCH"]
    direction: Side     # "bull" = broke a high upward, "bear" = broke a low
    level: float        # the swing price that was broken
    from_index: int     # index of the swing that was broken
    displacement: bool  # strong impulsive break (a CHoCH+displacement is an MSS)

    def as_dict(self) -> dict:
        return {"index": self.index, "time": self.time, "kind": self.kind,
                "direction": self.direction, "level": self.level,
                "from_index": self.from_index, "displacement": self.displacement}


@dataclass
class LiquiditySweep:
    """A wick past a prior swing that closed back inside — a stop hunt."""

    index: int
    time: Optional[int]
    side: Literal["buy-side", "sell-side"]  # which pool was swept
    level: float        # the swept swing level
    extreme: float      # the wick extreme that ran the stops
    reaction: Side      # implied reaction (sweep buy-side -> bear; sell-side -> bull)

    def as_dict(self) -> dict:
        return {"index": self.index, "time": self.time, "side": self.side,
                "level": self.level, "extreme": self.extreme,
                "reaction": self.reaction}


@dataclass
class Zone:
    """A rectangular price zone (order block, FVG, breaker, rejection, BPR…)."""

    kind: Literal["bullish", "bearish"]
    top: float
    bottom: float
    index: int          # origin bar (left edge)
    time: Optional[int]
    mitigated: bool      # price has since traded back into it
    subtype: str = "order-block"  # order-block | breaker | rejection | fvg | bpr | volume-imbalance
    inverted: bool = False   # (FVG) price closed through it, so its role flipped
    void: bool = False       # (FVG) oversized gap = liquidity void / inefficiency

    def as_dict(self) -> dict:
        return {"kind": self.kind, "top": self.top, "bottom": self.bottom,
                "index": self.index, "time": self.time,
                "mitigated": self.mitigated, "subtype": self.subtype,
                "inverted": self.inverted, "void": self.void,
                # consequent encroachment: the 50% of the zone (a key ICT level)
                "ce": (self.top + self.bottom) / 2.0}


@dataclass
class LiquidityPool:
    """Resting liquidity: buy-side above a swing high, sell-side below a low.

    ``equal`` marks an EQH/EQL cluster (2+ swings at ~the same level = a stronger
    pool). ``swept`` means price has since run through it (the stops were taken).
    """

    kind: Literal["buy-side", "sell-side"]
    price: float
    index: int
    time: Optional[int]
    equal: bool
    swept: bool

    def as_dict(self) -> dict:
        return {"kind": self.kind, "price": self.price, "index": self.index,
                "time": self.time, "equal": self.equal, "swept": self.swept}



@dataclass
class DealingRange:
    """The current premium/discount range and where price sits in it."""

    high: float
    low: float
    equilibrium: float
    position_pct: float               # 0 at the low, 1 at the high
    zone: Literal["premium", "discount", "equilibrium"]
    ote_discount: tuple[float, float]  # long "optimal trade entry" band (price)
    ote_premium: tuple[float, float]   # short OTE band (price)
    in_ote: bool                       # price is inside the side-appropriate OTE

    def as_dict(self) -> dict:
        return {
            "high": self.high, "low": self.low, "equilibrium": self.equilibrium,
            "position_pct": round(self.position_pct, 4), "zone": self.zone,
            "ote_discount": [self.ote_discount[0], self.ote_discount[1]],
            "ote_premium": [self.ote_premium[0], self.ote_premium[1]],
            "in_ote": self.in_ote,
        }


@dataclass
class IctAnalysis:
    symbol: str
    price: float
    trend: Literal["bull", "bear", "none"]
    bias: Literal["bullish", "bearish", "neutral"]
    summary: str
    swings: list[SwingPoint] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)
    order_blocks: list[Zone] = field(default_factory=list)
    fvgs: list[Zone] = field(default_factory=list)
    breakers: list[Zone] = field(default_factory=list)
    rejection_blocks: list[Zone] = field(default_factory=list)
    bpr: list[Zone] = field(default_factory=list)
    volume_imbalances: list[Zone] = field(default_factory=list)
    liquidity: list[LiquidityPool] = field(default_factory=list)
    draw_on_liquidity: Optional[dict] = None
    key_levels: Optional[dict] = None
    dealing_range: Optional[DealingRange] = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "trend": self.trend,
            "bias": self.bias,
            "summary": self.summary,
            "swings": [s.as_dict() for s in self.swings],
            "events": [e.as_dict() for e in self.events],
            "sweeps": [s.as_dict() for s in self.sweeps],
            "order_blocks": [z.as_dict() for z in self.order_blocks],
            "fvgs": [z.as_dict() for z in self.fvgs],
            "breakers": [z.as_dict() for z in self.breakers],
            "rejection_blocks": [z.as_dict() for z in self.rejection_blocks],
            "bpr": [z.as_dict() for z in self.bpr],
            "volume_imbalances": [z.as_dict() for z in self.volume_imbalances],
            "liquidity": [p.as_dict() for p in self.liquidity],
            "draw_on_liquidity": self.draw_on_liquidity,
            "key_levels": self.key_levels,
            "dealing_range": self.dealing_range.as_dict() if self.dealing_range else None,
        }


# ---- tuning constants -----------------------------------------------

SWING_LEFT = 2            # Williams fractal: bars required on each side
SWING_RIGHT = 2           # a swing is confirmed this many bars later (no repaint)
MIN_BARS = 30             # below this there isn't enough structure for a read
DISPLACEMENT_ATR_MULT = 1.5  # a breaking bar bigger than this × ATR = displacement
OB_LOOKBACK = 12          # how far back to hunt the order block before a break
EQ_BAND = 0.02            # ±2% of the range around 50% counts as "equilibrium"
EQ_LEVEL_ATR = 0.10       # swings within this × ATR are "equal" (EQH/EQL pool)
EQ_LEVEL_PCT = 0.0005     # …or within this fraction of price, whichever is larger
REJ_WICK_MULT = 2.0       # rejection block: wick at least this × the candle body
REJ_WICK_FRAC = 0.5       # …and the wick must be this fraction of the whole range
VOID_ATR_MULT = 2.0       # an FVG bigger than this × ATR is a liquidity void
BPR_MAX_GAP = 5           # opposite FVGs within this many bars can form a BPR

# How many of each item to keep (most recent) so the chart/AI stay clean.
_KEEP_SWINGS = 12
_KEEP_EVENTS = 8
_KEEP_SWEEPS = 6
_KEEP_OBS = 5
_KEEP_FVGS = 8
_KEEP_BREAKERS = 5
_KEEP_REJECTIONS = 5
_KEEP_BPR = 4
_KEEP_VI = 6
_KEEP_LIQUIDITY = 10


def _times(df: pd.DataFrame) -> Optional[list[int]]:
    """Unix-seconds per bar for the chart, or None when there are no timestamps."""
    if "timestamp" not in df.columns:
        return None
    try:
        return [int(t) // 1000 for t in df["timestamp"].to_numpy()]
    except Exception:
        return None


def find_swings(
    df: pd.DataFrame, left: int = SWING_LEFT, right: int = SWING_RIGHT
) -> list[SwingPoint]:
    """Confirmed swing highs/lows (strict fractals).

    A swing high at ``i`` has ``high[i]`` strictly greater than the ``left`` highs
    before and the ``right`` highs after it; a swing low mirrors that on lows.
    Only bars with ``right`` bars of history AFTER them are eligible, so a swing
    is only ever reported once fully confirmed — it cannot repaint.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = _times(df)
    n = len(df)
    out: list[SwingPoint] = []
    for i in range(left, n - right):
        h = highs[i]
        if all(h > highs[j] for j in range(i - left, i)) and all(
            h > highs[j] for j in range(i + 1, i + right + 1)
        ):
            out.append(SwingPoint(i, times[i] if times else None, float(h), "high"))
        lo = lows[i]
        if all(lo < lows[j] for j in range(i - left, i)) and all(
            lo < lows[j] for j in range(i + 1, i + right + 1)
        ):
            out.append(SwingPoint(i, times[i] if times else None, float(lo), "low"))
    out.sort(key=lambda s: s.index)
    return out


def _detect_structure(
    df: pd.DataFrame, swings: list[SwingPoint], right: int = SWING_RIGHT
) -> tuple[list[StructureEvent], Literal["bull", "bear", "none"]]:
    """Walk the closes and label every structural break as BOS or CHoCH.

    Reference levels are the MOST RECENT confirmed swing high and swing low that
    price has not yet closed through. A close above the reference high is a
    bullish break; below the reference low, a bearish break. A break in the
    prevailing trend's direction is a BOS (continuation); the first break against
    it is a CHoCH (character change / possible reversal). Each swing is only
    "known" ``right`` bars after it forms, so nothing here look-aheads.
    """
    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = _times(df)
    n = len(df)

    # Map the bar at which each swing becomes CONFIRMED -> that swing.
    conf_high: dict[int, SwingPoint] = {}
    conf_low: dict[int, SwingPoint] = {}
    for s in swings:
        c = s.index + right
        if c < n:
            (conf_high if s.kind == "high" else conf_low)[c] = s

    atr_series = atr(df, 14).to_numpy() if n >= 15 else None

    trend: Literal["bull", "bear", "none"] = "none"
    ref_high: Optional[SwingPoint] = None
    ref_low: Optional[SwingPoint] = None
    events: list[StructureEvent] = []

    for t in range(n):
        if t in conf_high:
            ref_high = conf_high[t]
        if t in conf_low:
            ref_low = conf_low[t]
        c = closes[t]
        # Displacement: is THIS breaking bar an impulsive candle?
        disp = False
        if atr_series is not None and t < len(atr_series):
            a = atr_series[t]
            if a and a == a:  # not NaN
                disp = bool((highs[t] - lows[t]) > DISPLACEMENT_ATR_MULT * a)
        if ref_high is not None and c > ref_high.price:
            kind = "CHoCH" if trend == "bear" else "BOS"
            events.append(StructureEvent(
                t, times[t] if times else None, kind, "bull",
                ref_high.price, ref_high.index, disp))
            trend = "bull"
            ref_high = None  # consumed; wait for the next confirmed swing high
        elif ref_low is not None and c < ref_low.price:
            kind = "CHoCH" if trend == "bull" else "BOS"
            events.append(StructureEvent(
                t, times[t] if times else None, kind, "bear",
                ref_low.price, ref_low.index, disp))
            trend = "bear"
            ref_low = None
    return events, trend


def _detect_sweeps(
    df: pd.DataFrame, swings: list[SwingPoint], right: int = SWING_RIGHT
) -> list[LiquiditySweep]:
    """Liquidity sweeps: a bar that wicked past a prior swing then closed back in.

    Uses the most recent CONFIRMED swing high/low as of each bar as the resting
    liquidity pool. A high wicking above a swing high but closing back below it
    took buy-side liquidity (bearish reaction); a low wicking below a swing low
    but closing back above took sell-side liquidity (bullish reaction). If the
    close finishes THROUGH the level it's a break (BOS/CHoCH), not a sweep — the
    close-back-inside test keeps the two distinct.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = _times(df)
    n = len(df)

    conf_high: dict[int, SwingPoint] = {}
    conf_low: dict[int, SwingPoint] = {}
    for s in swings:
        c = s.index + right
        if c < n:
            (conf_high if s.kind == "high" else conf_low)[c] = s

    ref_high: Optional[SwingPoint] = None
    ref_low: Optional[SwingPoint] = None
    out: list[LiquiditySweep] = []
    for t in range(n):
        if t in conf_high:
            ref_high = conf_high[t]
        if t in conf_low:
            ref_low = conf_low[t]
        if ref_high is not None and highs[t] > ref_high.price > closes[t]:
            out.append(LiquiditySweep(
                t, times[t] if times else None, "buy-side",
                ref_high.price, float(highs[t]), "bear"))
        if ref_low is not None and lows[t] < ref_low.price < closes[t]:
            out.append(LiquiditySweep(
                t, times[t] if times else None, "sell-side",
                ref_low.price, float(lows[t]), "bull"))
    return out


def _detect_order_blocks(
    df: pd.DataFrame, events: list[StructureEvent], lookback: int = OB_LOOKBACK
) -> list[Zone]:
    """The last opposite-colour candle before each structure-breaking impulse."""
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = _times(df)
    n = len(df)
    out: list[Zone] = []
    for ev in events:
        want_bear_candle = ev.direction == "bull"  # bullish OB = last DOWN candle
        ob_idx: Optional[int] = None
        start = max(0, ev.index - lookback)
        for k in range(ev.index - 1, start - 1, -1):
            is_bear = closes[k] < opens[k]
            is_bull = closes[k] > opens[k]
            if (want_bear_candle and is_bear) or (not want_bear_candle and is_bull):
                ob_idx = k
                break
        if ob_idx is None:
            continue
        top = float(max(highs[ob_idx], opens[ob_idx], closes[ob_idx]))
        bottom = float(min(lows[ob_idx], opens[ob_idx], closes[ob_idx]))
        mitigated = any(
            lows[k] <= top and highs[k] >= bottom for k in range(ev.index + 1, n)
        )
        out.append(Zone(
            "bullish" if ev.direction == "bull" else "bearish",
            top, bottom, ob_idx, times[ob_idx] if times else None, mitigated))
    return out


def _detect_fvgs(df: pd.DataFrame, atr_series=None) -> list[Zone]:
    """3-candle fair-value gaps (imbalance): candle 1 & candle 3 don't overlap.

    Each gap is also tagged if it became a liquidity ``void`` (oversized vs ATR)
    or ``inverted`` (a later close ran fully through it, so its role flips — an
    Inversion FVG / IFVG).
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = _times(df)
    n = len(df)
    out: list[Zone] = []
    for i in range(1, n - 1):
        c1_hi, c1_lo = highs[i - 1], lows[i - 1]
        c3_hi, c3_lo = highs[i + 1], lows[i + 1]
        if c3_lo > c1_hi:  # bullish imbalance: gap between c1 high and c3 low
            top, bottom, kind = float(c3_lo), float(c1_hi), "bullish"
        elif c3_hi < c1_lo:  # bearish imbalance
            top, bottom, kind = float(c1_lo), float(c3_hi), "bearish"
        else:
            continue
        mitigated = any(
            lows[k] <= top and highs[k] >= bottom for k in range(i + 2, n)
        )
        # Inversion: a close fully through the far side flips the gap's role.
        if kind == "bullish":
            inverted = any(closes[k] < bottom for k in range(i + 2, n))
        else:
            inverted = any(closes[k] > top for k in range(i + 2, n))
        void = False
        if atr_series is not None and i < len(atr_series):
            a = atr_series[i]
            if a and a == a:
                void = bool((top - bottom) > VOID_ATR_MULT * a)
        out.append(Zone(kind, top, bottom, i - 1,
                        times[i - 1] if times else None, mitigated,
                        subtype="fvg", inverted=inverted, void=void))
    return out


def _detect_liquidity(
    df: pd.DataFrame, swings: list[SwingPoint], right: int = SWING_RIGHT
) -> list[LiquidityPool]:
    """Resting liquidity pools from swing highs (buy-side) and lows (sell-side).

    A pool is ``equal`` when another same-side swing sits within tolerance (an
    EQH/EQL cluster) and ``swept`` once a later bar has traded through its level.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = _times(df)
    n = len(df)
    atr_series = atr(df, 14).to_numpy() if n >= 15 else None

    def tol(price: float, idx: int) -> float:
        a = 0.0
        if atr_series is not None and idx < len(atr_series) and atr_series[idx] == atr_series[idx]:
            a = float(atr_series[idx])
        return max(EQ_LEVEL_ATR * a, EQ_LEVEL_PCT * abs(price))

    his = [s for s in swings if s.kind == "high"]
    los = [s for s in swings if s.kind == "low"]
    out: list[LiquidityPool] = []
    for s in his:
        equal = any(o is not s and abs(o.price - s.price) <= tol(s.price, s.index) for o in his)
        swept = any(highs[k] > s.price for k in range(s.index + 1, n))
        out.append(LiquidityPool("buy-side", s.price, s.index,
                                 times[s.index] if times else None, equal, swept))
    for s in los:
        equal = any(o is not s and abs(o.price - s.price) <= tol(s.price, s.index) for o in los)
        swept = any(lows[k] < s.price for k in range(s.index + 1, n))
        out.append(LiquidityPool("sell-side", s.price, s.index,
                                 times[s.index] if times else None, equal, swept))
    out.sort(key=lambda p: p.index)
    return out


def _draw_on_liquidity(pools: list[LiquidityPool], price: float) -> Optional[dict]:
    """Nearest UNSWEPT pool above and below price — the likely 'draw' / targets."""
    above = [p for p in pools if p.kind == "buy-side" and not p.swept and p.price > price]
    below = [p for p in pools if p.kind == "sell-side" and not p.swept and p.price < price]
    up = min(above, key=lambda p: p.price - price) if above else None
    dn = min(below, key=lambda p: price - p.price) if below else None
    if up is None and dn is None:
        return None
    return {"above": up.as_dict() if up else None,
            "below": dn.as_dict() if dn else None}


def _detect_breakers(df: pd.DataFrame, order_blocks: list[Zone]) -> list[Zone]:
    """Breaker blocks: an order block that price VIOLATED, so its role flips.

    A bullish OB closed through to the downside becomes bearish resistance (and
    vice versa). The flipped zone is what price often retests before continuing.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    out: list[Zone] = []
    for ob in order_blocks:
        start = ob.index + 1
        if ob.kind == "bullish":
            hit = next((k for k in range(start, n) if closes[k] < ob.bottom), None)
            flipped = "bearish"
        else:
            hit = next((k for k in range(start, n) if closes[k] > ob.top), None)
            flipped = "bullish"
        if hit is None:
            continue
        mitigated = any(
            lows[k] <= ob.top and highs[k] >= ob.bottom for k in range(hit + 1, n)
        )
        out.append(Zone(flipped, ob.top, ob.bottom, ob.index, ob.time,
                        mitigated, subtype="breaker"))
    return out


def _detect_rejection_blocks(
    df: pd.DataFrame, swings: list[SwingPoint]
) -> list[Zone]:
    """Rejection blocks: a swing candle whose long wick did the rejecting.

    A swing low with a dominant LOWER wick is a bullish rejection block (the wick
    is the demand that rejected price); a swing high with a dominant UPPER wick is
    a bearish one. The zone is the wick itself.
    """
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = _times(df)
    n = len(df)
    out: list[Zone] = []
    for s in swings:
        i = s.index
        o, c, hi, lo = opens[i], closes[i], highs[i], lows[i]
        body = abs(c - o)
        rng = hi - lo
        if rng <= 0:
            continue
        if s.kind == "low":
            wick = min(o, c) - lo
            if wick > REJ_WICK_MULT * body and wick >= REJ_WICK_FRAC * rng:
                top, bottom, kind = float(min(o, c)), float(lo), "bullish"
            else:
                continue
        else:
            wick = hi - max(o, c)
            if wick > REJ_WICK_MULT * body and wick >= REJ_WICK_FRAC * rng:
                top, bottom, kind = float(hi), float(max(o, c)), "bearish"
            else:
                continue
        mitigated = any(
            lows[k] <= top and highs[k] >= bottom for k in range(i + 1, n)
        )
        out.append(Zone(kind, top, bottom, i, times[i] if times else None,
                        mitigated, subtype="rejection"))
    return out


def _detect_bpr(fvgs: list[Zone]) -> list[Zone]:
    """Balanced Price Range: a bullish and a bearish FVG that overlap.

    The overlap of two opposing imbalances close together is an especially strong
    reaction zone in ICT. The BPR zone is the overlapping band.
    """
    out: list[Zone] = []
    for a in fvgs:
        for b in fvgs:
            if b.index <= a.index or b.index - a.index > BPR_MAX_GAP:
                continue
            if a.kind == b.kind:
                continue
            lo = max(a.bottom, b.bottom)
            hi = min(a.top, b.top)
            if hi > lo:
                out.append(Zone(b.kind, hi, lo, a.index, a.time, False,
                                subtype="bpr"))
    return out


def _detect_volume_imbalances(df: pd.DataFrame) -> list[Zone]:
    """Volume imbalances: a body gap between two candles whose wicks still overlap."""
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = _times(df)
    n = len(df)
    out: list[Zone] = []
    for i in range(1, n):
        prev_c, cur_o = float(closes[i - 1]), float(opens[i])
        # wicks must overlap (otherwise it's a true price gap, not an imbalance)
        if not (lows[i] <= highs[i - 1] and highs[i] >= lows[i - 1]):
            continue
        if cur_o > prev_c:
            top, bottom, kind = cur_o, prev_c, "bullish"
        elif cur_o < prev_c:
            top, bottom, kind = prev_c, cur_o, "bearish"
        else:
            continue
        mitigated = any(
            lows[k] <= top and highs[k] >= bottom for k in range(i + 1, n)
        )
        out.append(Zone(kind, top, bottom, i - 1,
                        times[i - 1] if times else None, mitigated,
                        subtype="volume-imbalance"))
    return out


def _key_levels(df: pd.DataFrame) -> Optional[dict]:
    """Previous day/week high & low (PDH/PDL/PWH/PWL) — major liquidity draws.

    Uses the last COMPLETED daily/weekly period (the current one is still forming).
    Returns None when there aren't timestamps or enough history to be honest.
    """
    if "timestamp" not in df.columns:
        return None
    try:
        idx = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        s = df.set_index(idx)
        out: dict = {}

        def prev(rule: str) -> Optional[tuple[float, float]]:
            g = s.resample(rule)
            hi = g["high"].max().dropna()
            lo = g["low"].min().dropna()
            if len(hi) >= 2 and len(lo) >= 2:
                return float(hi.iloc[-2]), float(lo.iloc[-2])
            return None

        day = prev("1D")
        wk = prev("1W")
        if day:
            out["pdh"], out["pdl"] = day
        if wk:
            out["pwh"], out["pwl"] = wk
        return out or None
    except Exception:
        return None


def _dealing_range(
    df: pd.DataFrame, swings: list[SwingPoint], price: float
) -> Optional[DealingRange]:
    """Premium/discount range from the most recent confirmed swing high & low."""
    last_high = next((s for s in reversed(swings) if s.kind == "high"), None)
    last_low = next((s for s in reversed(swings) if s.kind == "low"), None)
    if last_high is None or last_low is None:
        return None
    hi, lo = last_high.price, last_low.price
    if hi <= lo:
        return None
    d = hi - lo
    pos = (price - lo) / d
    if pos > 0.5 + EQ_BAND:
        zone = "premium"
    elif pos < 0.5 - EQ_BAND:
        zone = "discount"
    else:
        zone = "equilibrium"
    ote_discount = (lo + 0.21 * d, lo + 0.38 * d)   # 0.62–0.79 retrace from high
    ote_premium = (lo + 0.62 * d, lo + 0.79 * d)    # 0.62–0.79 retrace from low
    in_ote = (
        (zone == "discount" and ote_discount[0] <= price <= ote_discount[1])
        or (zone == "premium" and ote_premium[0] <= price <= ote_premium[1])
    )
    return DealingRange(
        high=hi, low=lo, equilibrium=(hi + lo) / 2.0, position_pct=pos,
        zone=zone, ote_discount=ote_discount, ote_premium=ote_premium,
        in_ote=in_ote)


def _recent_zones(zones: list[Zone], keep: int) -> list[Zone]:
    """Keep the most recent zones (by origin bar) for a clean, current picture."""
    return sorted(zones, key=lambda z: z.index)[-keep:]


def _ict_read(
    trend: str,
    events: list[StructureEvent],
    sweeps: list[LiquiditySweep],
    drange: Optional[DealingRange],
    n: int,
) -> tuple[Literal["bullish", "bearish", "neutral"], str]:
    """Combine the structural facts into ONE honest ICT bias + a plain read.

    Nothing here is invented: every clause is backed by a computed event above.
    A thin or conflicted picture reads NEUTRAL — the honest "no clear edge".
    """
    score = 0.0
    reasons: list[str] = []

    last_ev = events[-1] if events else None
    if last_ev is not None:
        recent = (n - 1 - last_ev.index) <= 15
        mag = 2.0 if last_ev.kind == "CHoCH" else 1.0
        if last_ev.displacement and last_ev.kind == "CHoCH":
            mag = 3.0  # a CHoCH with displacement is a market-structure shift (MSS)
        if not recent:
            mag *= 0.5
        signed = mag if last_ev.direction == "bull" else -mag
        score += signed
        tag = "MSS" if (last_ev.displacement and last_ev.kind == "CHoCH") else last_ev.kind
        reasons.append(
            f"last structural break was a {last_ev.direction} {tag} "
            f"(close through {last_ev.level:g})"
        )
    elif trend != "none":
        score += 1.0 if trend == "bull" else -1.0
        reasons.append(f"structure trend is {trend}")

    if drange is not None:
        if drange.zone == "discount":
            score += 1.0
            reasons.append(
                "price is at a discount (below the 50% equilibrium) — favours longs"
                + (" and is in the OTE zone" if drange.in_ote else "")
            )
        elif drange.zone == "premium":
            score -= 1.0
            reasons.append(
                "price is at a premium (above the 50% equilibrium) — favours shorts"
                + (" and is in the OTE zone" if drange.in_ote else "")
            )
        else:
            reasons.append("price is near equilibrium (no premium/discount edge)")

    if sweeps:
        last_sw = sweeps[-1]
        if (n - 1 - last_sw.index) <= 5:
            score += 1.0 if last_sw.reaction == "bull" else -1.0
            reasons.append(
                f"a {last_sw.side} liquidity sweep just ran stops at {last_sw.level:g}"
                f" — a {last_sw.reaction}ish reaction is the classic follow-through"
            )

    if score >= 1.0:
        bias: Literal["bullish", "bearish", "neutral"] = "bullish"
    elif score <= -1.0:
        bias = "bearish"
    else:
        bias = "neutral"

    if not reasons:
        summary = "No clean ICT structure yet — standing aside for a clearer read."
    else:
        head = {
            "bullish": "ICT read: bullish.",
            "bearish": "ICT read: bearish.",
            "neutral": "ICT read: neutral / mixed.",
        }[bias]
        summary = head + " " + "; ".join(reasons) + "."
    return bias, summary


def analyze_ict(
    df: pd.DataFrame, symbol: str = "", price: Optional[float] = None
) -> IctAnalysis:
    """Full ICT read on a CLOSED-bar frame.

    ``price`` is the live/display price to place inside the dealing range (the
    caller passes the real current price); everything structural is computed on
    the closed bars in ``df``. Returns a neutral, empty analysis (never a
    fabricated level) when there isn't enough data.
    """
    n = len(df)
    px = float(price) if price is not None else (
        float(df["close"].iloc[-1]) if n else 0.0)
    if n < MIN_BARS:
        return IctAnalysis(
            symbol, px, "none", "neutral",
            f"Not enough bars for an ICT read (need ≥ {MIN_BARS}).")
    atr_series = atr(df, 14).to_numpy() if n >= 15 else None
    swings = find_swings(df)
    events, trend = _detect_structure(df, swings)
    sweeps = _detect_sweeps(df, swings)
    obs = _detect_order_blocks(df, events)
    fvgs = _detect_fvgs(df, atr_series)
    breakers = _detect_breakers(df, obs)
    rejections = _detect_rejection_blocks(df, swings)
    bpr = _detect_bpr(fvgs)
    vis = _detect_volume_imbalances(df)
    liquidity = _detect_liquidity(df, swings)
    draw = _draw_on_liquidity(liquidity, px)
    key_levels = _key_levels(df)
    drange = _dealing_range(df, swings, px)
    bias, summary = _ict_read(trend, events, sweeps, drange, n)
    if draw:
        bits = []
        if draw.get("above"):
            a = draw["above"]
            bits.append(f"buy-side at {a['price']:g}" + (" (equal highs)" if a["equal"] else ""))
        if draw.get("below"):
            b = draw["below"]
            bits.append(f"sell-side at {b['price']:g}" + (" (equal lows)" if b["equal"] else ""))
        if bits:
            summary += " Draw on liquidity: " + " / ".join(bits) + "."
    return IctAnalysis(
        symbol=symbol, price=px, trend=trend, bias=bias, summary=summary,
        swings=swings[-_KEEP_SWINGS:],
        events=events[-_KEEP_EVENTS:],
        sweeps=sweeps[-_KEEP_SWEEPS:],
        order_blocks=_recent_zones(obs, _KEEP_OBS),
        fvgs=_recent_zones(fvgs, _KEEP_FVGS),
        breakers=_recent_zones(breakers, _KEEP_BREAKERS),
        rejection_blocks=_recent_zones(rejections, _KEEP_REJECTIONS),
        bpr=_recent_zones(bpr, _KEEP_BPR),
        volume_imbalances=_recent_zones(vis, _KEEP_VI),
        liquidity=liquidity[-_KEEP_LIQUIDITY:],
        draw_on_liquidity=draw,
        key_levels=key_levels,
        dealing_range=drange,
    )






