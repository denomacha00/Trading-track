"""ICT / smart-money engine — real, non-repainting structure computation.

These pin the algorithmic definitions the user asked for (BOS, CHoCH, liquidity
sweeps, order blocks, FVG, premium/discount) on small hand-built frames where
every level is computable by hand, plus determinism / no-repaint / honesty
guards on a larger synthetic frame. No network, no credentials.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app import ict
from app.ict import (
    DealingRange,
    LiquidityPool,
    SwingPoint,
    Zone,
    analyze_ict,
    find_swings,
    _dealing_range,
    _detect_breakers,
    _detect_bpr,
    _detect_fvgs,
    _detect_liquidity,
    _detect_order_blocks,
    _detect_rejection_blocks,
    _detect_sweeps,
    _detect_structure,
    _detect_volume_imbalances,
    _draw_on_liquidity,
    _key_levels,
)

_HOUR_MS = 3_600_000


def _frame(rows: list[tuple], ts: bool = True) -> pd.DataFrame:
    """rows are (open, high, low, close); volume defaults to 1000."""
    data = []
    for i, r in enumerate(rows):
        o, h, l, c = r
        data.append([i * _HOUR_MS, o, h, l, c, 1000.0])
    df = pd.DataFrame(
        data, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    if not ts:
        df = df.drop(columns=["timestamp"])
    return df


# --- swing detection (Williams fractals) --------------------------------

def test_find_swings_isolates_a_strict_high():
    # high peaks at index 2; lows are flat so no swing low is reported.
    df = _frame([
        (1, 1, 1, 1),
        (2, 2, 1, 2),
        (5, 5, 1, 5),   # <- strict swing high
        (2, 2, 1, 2),
        (1, 1, 1, 1),
    ])
    swings = find_swings(df)
    assert len(swings) == 1
    s = swings[0]
    assert s.kind == "high" and s.index == 2 and s.price == 5.0
    # time is unix-seconds: ms // 1000
    assert s.time == (2 * _HOUR_MS) // 1000


def test_find_swings_isolates_a_strict_low():
    df = _frame([
        (10, 10, 5, 10),
        (10, 10, 4, 10),
        (10, 10, 1, 10),   # <- strict swing low
        (10, 10, 4, 10),
        (10, 10, 5, 10),
    ])
    swings = find_swings(df)
    assert len(swings) == 1
    assert swings[0].kind == "low" and swings[0].index == 2 and swings[0].price == 1.0


def test_ties_are_not_swings():
    # equal neighbours must NOT qualify (strict inequality only).
    df = _frame([(5, 5, 1, 5)] * 5)
    assert find_swings(df) == []


# --- BOS / CHoCH structure ----------------------------------------------

def test_first_break_is_a_bos_in_a_fresh_trend():
    # swing high of 5 at idx2 (confirmed at idx4); close of 5.5 at idx6 breaks up.
    df = _frame([
        (1, 1, 0, 1),
        (2, 2, 0, 2),
        (5, 5, 0, 4.5),   # swing high @2 = 5
        (2, 2, 0, 2),
        (1, 1, 0, 1),
        (1, 1, 0, 1),
        (5, 6, 0, 5.5),   # close 5.5 > 5 -> break
    ])
    swings = find_swings(df)
    events, trend = _detect_structure(df, swings)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "BOS" and ev.direction == "bull"
    assert ev.level == 5.0 and ev.from_index == 2 and ev.index == 6
    assert trend == "bull"


def test_bos_up_then_choch_down():
    # Bull break first (BOS), then a close under a higher-low swing = CHoCH bear.
    df = _frame([
        (1.5, 2, 1, 1.5),
        (2.5, 3, 1, 2.5),
        (4.5, 5, 2, 4.5),   # swing high @2 = 5
        (3.5, 4, 2, 3.5),
        (3.5, 4, 2, 3.5),
        (5.5, 6, 3, 5.5),   # BOS bull (close 5.5 > 5)
        (6.5, 7, 5, 6.5),   # swing high @6 = 7 (never broken)
        (5.5, 6, 4.5, 5.5),
        (4.5, 5, 4, 4.5),   # swing low @8 = 4 (higher low)
        (4.8, 5, 4.5, 4.8),
        (5.2, 6, 5, 5.2),
        (3.5, 4, 3, 3.5),   # CHoCH bear (close 3.5 < 4)
        (3.6, 4, 3, 3.6),
    ])
    swings = find_swings(df)
    events, trend = _detect_structure(df, swings)
    kinds = [(e.kind, e.direction) for e in events]
    assert kinds == [("BOS", "bull"), ("CHoCH", "bear")]
    assert events[0].level == 5.0 and events[0].index == 5
    assert events[1].level == 4.0 and events[1].index == 11
    assert trend == "bear"


# --- liquidity sweeps ---------------------------------------------------

def test_buy_side_sweep_wick_through_then_close_back_in():
    # idx5 wicks to 6 (above swing high 5) but closes at 4.8 -> buy-side sweep.
    df = _frame([
        (1.5, 2, 1, 1.5),
        (2.5, 3, 1, 2.5),
        (4.5, 5, 2, 4.5),   # swing high @2 = 5
        (3.5, 4, 2, 3.5),
        (3.5, 4, 2, 3.5),
        (4.8, 6, 2, 4.8),   # wick 6 > 5, close 4.8 < 5 -> sweep, no break
    ])
    swings = find_swings(df)
    sweeps = _detect_sweeps(df, swings)
    events, _ = _detect_structure(df, swings)
    assert events == []                       # nothing closed through -> not a break
    assert len(sweeps) == 1
    sw = sweeps[0]
    assert sw.side == "buy-side" and sw.reaction == "bear"
    assert sw.level == 5.0 and sw.extreme == 6.0 and sw.index == 5


def test_close_through_the_level_is_a_break_not_a_sweep():
    df = _frame([
        (1.5, 2, 1, 1.5),
        (2.5, 3, 1, 2.5),
        (4.5, 5, 2, 4.5),   # swing high @2 = 5
        (3.5, 4, 2, 3.5),
        (3.5, 4, 2, 3.5),
        (5.5, 6, 2, 5.5),   # close 5.5 > 5 -> a real break
    ])
    swings = find_swings(df)
    assert _detect_sweeps(df, swings) == []
    assert len(_detect_structure(df, swings)[0]) == 1


# --- order blocks -------------------------------------------------------

def test_bullish_order_block_is_last_down_candle_before_up_break():
    df = _frame([
        (1.6, 2, 1, 1.5),
        (1.5, 3, 1, 2.5),
        (3.0, 5, 2, 4.5),     # swing high @2 = 5
        (4.5, 4.6, 3.9, 4.0),  # down candle
        (4.0, 4.5, 3.5, 3.8),  # <- last down candle before the break
        (3.8, 6, 3.7, 5.6),    # BOS bull (close 5.6 > 5)
        (3.9, 4.2, 3.6, 4.0),  # trades back into the OB -> mitigated
    ])
    swings = find_swings(df)
    events, _ = _detect_structure(df, swings)
    obs = _detect_order_blocks(df, events)
    assert len(obs) == 1
    ob = obs[0]
    assert ob.kind == "bullish" and ob.index == 4
    assert ob.top == 4.5 and ob.bottom == 3.5
    assert ob.mitigated is True


# --- fair-value gaps ----------------------------------------------------

def test_bullish_fvg_detected_with_correct_edges():
    df = _frame([
        (1, 2, 1, 1.5),
        (3, 5, 3, 4),
        (5, 6, 4, 5.5),   # candle-3 low 4 > candle-1 high 2 -> bullish gap
    ])
    fvgs = _detect_fvgs(df)
    assert len(fvgs) == 1
    z = fvgs[0]
    assert z.kind == "bullish" and z.top == 4.0 and z.bottom == 2.0
    assert z.index == 0 and z.mitigated is False


def test_bearish_fvg_detected():
    df = _frame([
        (5, 6, 4, 4.5),
        (3.5, 5, 3, 3.5),
        (1.5, 2, 1, 1.5),  # candle-3 high 2 < candle-1 low 4 -> bearish gap
    ])
    fvgs = _detect_fvgs(df)
    assert len(fvgs) == 1
    assert fvgs[0].kind == "bearish" and fvgs[0].top == 4.0 and fvgs[0].bottom == 2.0


# --- premium / discount dealing range -----------------------------------

@pytest.mark.parametrize(
    "price,zone,in_ote",
    [
        (101.0, "discount", False),
        (103.0, "discount", True),    # inside the long OTE band 102.1–103.8
        (105.0, "equilibrium", False),
        (108.0, "premium", False),
        (107.0, "premium", True),     # inside the short OTE band 106.2–107.9
    ],
)
def test_dealing_range_premium_discount_and_ote(price, zone, in_ote):
    swings = [
        SwingPoint(5, None, 110.0, "high"),
        SwingPoint(8, None, 100.0, "low"),
    ]
    dr = _dealing_range(pd.DataFrame(), swings, price)
    assert dr is not None
    assert dr.high == 110.0 and dr.low == 100.0 and dr.equilibrium == 105.0
    assert dr.zone == zone
    assert dr.in_ote is in_ote


def test_dealing_range_needs_both_a_high_and_a_low():
    only_high = [SwingPoint(3, None, 50.0, "high")]
    assert _dealing_range(pd.DataFrame(), only_high, 40.0) is None


def _triangle_frame(n: int = 120, up: int = 5, down: int = 3,
                    ustep: float = 2.0, dstep: float = 1.0) -> pd.DataFrame:
    """A rising zig-zag: up-legs then shallower pull-backs, drifting up overall.

    This makes genuine higher-highs and higher-lows, so real swings form and
    later closes break prior swing highs (bullish BOS). Distinct close values
    per bar keep every fractal strict (no tied highs), and each bar's wick
    brackets its close so the candles are valid.
    """
    closes: list[float] = []
    mid = 100.0
    going_up = True
    while len(closes) < n:
        legs = up if going_up else down
        step = ustep if going_up else -dstep
        for _ in range(legs):
            if len(closes) >= n:
                break
            closes.append(round(mid, 4))
            mid += step
        going_up = not going_up
    # open == close (doji bodies) keeps highs/lows a clean peaked series.
    return _frame([(c, c + 0.3, c - 0.3, c) for c in closes])



# --- analyze_ict: honesty on thin data ----------------------------------

def test_thin_data_is_honest_not_fabricated():
    ana = analyze_ict(_triangle_frame(10), symbol="BTCUSDT")
    assert ana.trend == "none" and ana.bias == "neutral"
    assert "Not enough bars" in ana.summary
    assert ana.swings == [] and ana.events == [] and ana.sweeps == []
    assert ana.order_blocks == [] and ana.fvgs == [] and ana.dealing_range is None
    assert ana.symbol == "BTCUSDT"


# --- analyze_ict: a full read on real structure -------------------------

def test_full_read_populates_structure_and_is_serializable():
    df = _triangle_frame(120)
    ana = analyze_ict(df, symbol="ETHUSDT")
    assert ana.symbol == "ETHUSDT"
    assert ana.bias in ("bullish", "bearish", "neutral")
    assert ana.trend in ("bull", "bear", "none")
    assert len(ana.swings) > 0            # real swings were found
    assert len(ana.events) > 0            # and real BOS/CHoCH breaks
    assert ana.summary.startswith("ICT read:")
    # every kept item respects its cap
    assert len(ana.swings) <= ict._KEEP_SWINGS
    assert len(ana.events) <= ict._KEEP_EVENTS
    # fully JSON-serializable for the API/AI/chart
    blob = json.dumps(ana.as_dict())
    assert '"dealing_range"' in blob and '"swings"' in blob


def test_no_repaint_swings_never_use_the_unconfirmed_tail():
    df = _triangle_frame(120)
    swings = find_swings(df)
    n = len(df)
    # a swing needs SWING_RIGHT confirmed bars after it, so none can sit in the
    # last SWING_RIGHT bars — that's the no-repaint guarantee.
    assert swings, "expected swings on a zig-zag frame"
    assert max(s.index for s in swings) <= n - 1 - ict.SWING_RIGHT


def test_analyze_is_deterministic():
    df = _triangle_frame(120)
    assert analyze_ict(df, symbol="X").as_dict() == analyze_ict(df, symbol="X").as_dict()


def test_supplied_price_lands_in_the_dealing_range():
    df = _triangle_frame(120)
    # no price -> uses the last close
    last_close = float(df["close"].iloc[-1])
    assert analyze_ict(df).price == last_close
    # supplied price is honoured verbatim and used for premium/discount
    ana = analyze_ict(df, price=123.45)
    assert ana.price == 123.45
    if ana.dealing_range is not None:
        lo, hi = ana.dealing_range.low, ana.dealing_range.high
        expected = (123.45 - lo) / (hi - lo)
        assert abs(ana.dealing_range.position_pct - expected) < 1e-9


def test_missing_timestamps_degrade_to_none_times():
    df = _triangle_frame(120, ).drop(columns=["timestamp"])
    ana = analyze_ict(df)
    assert all(s.time is None for s in ana.swings)
    assert all(e.time is None for e in ana.events)


# --- liquidity pools (EQH/EQL) + draw on liquidity ----------------------

def test_equal_highs_form_an_unswept_buy_side_pool():
    df = _frame([
        (1, 2, 1, 1),
        (2, 3, 1, 2),
        (4, 5, 2, 4),   # swing high @2 = 5
        (3, 4, 2, 3),
        (2, 3, 1, 2),
        (3, 4, 1, 3),
        (4, 5, 2, 4),   # swing high @6 = 5 (equal high)
        (3, 4, 2, 3),
        (2, 3, 1, 2),
    ])
    pools = _detect_liquidity(df, find_swings(df))
    buy = [p for p in pools if p.kind == "buy-side"]
    assert len(buy) == 2
    assert all(p.equal and not p.swept and p.price == 5.0 for p in buy)
    draw = _draw_on_liquidity(pools, price=3.0)
    assert draw is not None and draw["above"]["price"] == 5.0
    assert draw["below"] is None


def test_rising_market_sweeps_earlier_liquidity():
    pools = _detect_liquidity(_triangle_frame(120), find_swings(_triangle_frame(120)))
    # a persistently rising market must have taken out some earlier highs
    assert any(p.kind == "buy-side" and p.swept for p in pools)


# --- breaker blocks -----------------------------------------------------

def test_violated_order_block_becomes_a_breaker():
    df = _frame([
        (1, 2, 1, 1),
        (1, 2, 1, 1),
        (4, 4.5, 3.5, 4),   # (bullish OB body here, idx2)
        (4, 5, 3, 4.5),
        (4.5, 5, 4, 4.8),
        (4, 4.2, 3.0, 3.0),  # close 3.0 < OB bottom 3.5 -> violates
        (3, 3.8, 3.4, 3.6),  # retest into the flipped zone -> mitigated
    ])
    ob = Zone("bullish", 4.5, 3.5, index=2, time=None, mitigated=False)
    breakers = _detect_breakers(df, [ob])
    assert len(breakers) == 1
    b = breakers[0]
    assert b.kind == "bearish" and b.subtype == "breaker"
    assert b.top == 4.5 and b.bottom == 3.5 and b.mitigated is True


# --- rejection blocks ---------------------------------------------------

def test_long_lower_wick_swing_low_is_a_bullish_rejection_block():
    df = _frame([
        (5, 5.2, 4, 5),
        (5, 5.2, 3, 5),
        (5, 5.2, 1, 4.8),   # swing low @2, long lower wick (rejection)
        (5, 5.2, 3, 5),
        (5, 5.2, 4, 5),
    ])
    rej = _detect_rejection_blocks(df, find_swings(df))
    assert len(rej) == 1
    z = rej[0]
    assert z.kind == "bullish" and z.subtype == "rejection"
    assert z.top == 4.8 and z.bottom == 1.0


# --- volume imbalance ---------------------------------------------------

def test_volume_imbalance_body_gap_with_overlapping_wicks():
    df = _frame([
        (1, 3.5, 1, 3),      # close 3
        (4, 5, 3.2, 4.5),    # open 4 (> prev close 3); wicks overlap 3.2<=3.5
    ])
    vis = _detect_volume_imbalances(df)
    assert len(vis) == 1
    z = vis[0]
    assert z.kind == "bullish" and z.subtype == "volume-imbalance"
    assert z.top == 4.0 and z.bottom == 3.0


# --- balanced price range (BPR) -----------------------------------------

def test_bpr_is_the_overlap_of_opposing_fvgs():
    a = Zone("bullish", 10, 8, index=0, time=None, mitigated=False, subtype="fvg")
    b = Zone("bearish", 9, 7, index=2, time=None, mitigated=False, subtype="fvg")
    bpr = _detect_bpr([a, b])
    assert len(bpr) == 1
    assert bpr[0].subtype == "bpr" and bpr[0].top == 9 and bpr[0].bottom == 8


# --- FVG void / inversion + consequent encroachment ---------------------

def test_fvg_inversion_when_a_close_runs_through_it():
    df = _frame([
        (1, 2, 1, 1.5),
        (3, 5, 3, 4),
        (5, 6, 4, 5.5),      # bullish FVG top 4 bottom 2 (index 0)
        (1, 5, 0.5, 1.0),    # close 1.0 < 2 -> inverted (role flips)
    ])
    fvgs = _detect_fvgs(df)
    assert fvgs[0].inverted is True and fvgs[0].mitigated is True


def test_fvg_marked_void_when_oversized_vs_atr():
    df = _frame([
        (1, 2, 1, 1.5),
        (3, 5, 3, 4),
        (5, 6, 4, 5.5),   # gap size = 4 - 2 = 2
    ])
    # ATR at the middle bar is 0.5 -> 2 > VOID_ATR_MULT(2)*0.5 = 1 -> void
    fvgs = _detect_fvgs(df, atr_series=[1.0, 0.5, 1.0])
    assert fvgs[0].void is True


def test_zone_reports_consequent_encroachment_midpoint():
    z = Zone("bullish", 10.0, 6.0, index=0, time=None, mitigated=False)
    assert z.as_dict()["ce"] == 8.0


# --- previous day/week levels (PDH/PDL) ---------------------------------

def test_key_levels_previous_day_high_low():
    # 50 hourly bars from the epoch => days 0,1,2; day 1 is the last COMPLETED.
    rows = [(100 + i, 100 + i, float(i), 100 + i) for i in range(50)]
    df = _frame(rows)
    kl = _key_levels(df)
    assert kl is not None
    assert kl["pdh"] == 147.0   # max high across bars 24..47 (day 1)
    assert kl["pdl"] == 24.0    # min low across day 1
    assert "pwh" not in kl      # under two weekly buckets -> honestly omitted


def test_full_read_exposes_the_whole_ict_family():
    ana = analyze_ict(_triangle_frame(160), symbol="BTCUSDT")
    d = ana.as_dict()
    for key in ("swings", "events", "sweeps", "order_blocks", "fvgs",
                "breakers", "rejection_blocks", "bpr", "volume_imbalances",
                "liquidity", "draw_on_liquidity", "key_levels", "dealing_range"):
        assert key in d
    # still fully JSON-serializable with every new field
    json.dumps(d)
    assert len(ana.liquidity) > 0


