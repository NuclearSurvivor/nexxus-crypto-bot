"""
engine.py — Trading engine using official coinbase-advanced-py SDK.

Docs & references:
  Coinbase Advanced Trade API:  https://docs.cdp.coinbase.com/advanced-trade/docs/welcome
  CDP API Key setup:            https://docs.cdp.coinbase.com/coinbase-app/docs/authentication
  coinbase-advanced-py SDK:     https://github.com/coinbase/coinbase-advanced-py
  Advanced Trade WebSocket:     https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview
  CCXT coinbaseadvanced:        https://docs.ccxt.com/#/?id=coinbaseadvanced
"""

import json
import os
import time
import logging
from collections import defaultdict
import numpy as np

# Official Coinbase SDK
from coinbase.rest import RESTClient
from coinbase.jwt_generator import build_ws_jwt

logger = logging.getLogger("engine")

# ── Constants ────────────────────────────────────────────────────────────────
TRADING_PAIRS        = ["BTC-USD", "ETH-USD", "XCN-USD"]
# Watchlist extends the chart viewer beyond active trading pairs.
# No trades are placed for watchlist-only pairs — they are view + cache only.
WATCHLIST_PAIRS      = [
    "SOL-USD", "DOGE-USD", "ADA-USD", "AVAX-USD",
    "LINK-USD", "DOT-USD", "POL-USD", "LTC-USD",
]
COINBASE_WS_URL      = "wss://advanced-trade-ws.coinbase.com"
MAX_HISTORY          = 2000
DISPLAY_CANDLES      = {'1m': 200, '5m': 500, '1h': 300, '1d': 365}
# 2× display count fetched/stored so EMA/indicators are fully warmed up
# across the entire visible window — no cold-start distortion.
FETCH_CANDLES        = {'1m': 400, '5m': 1000, '1h': 600, '1d': 730}
MA_PERIODS           = [2, 5, 14]    # EMA(2/5) scalp crossover, EMA(14) trend context
TIMEFRAMES           = ['1m', '5m', '1h', '1d']
ORDER_AMOUNT_USD     = 100
MINIMUM_RESERVE      = 50
WEBHOOK_PORT         = 8000
STOP_LOSS_PCT        = 0.02
TAKE_PROFIT_PCT      = 0.05
TRAIL_STOP_PCT       = 0.04   # trailing stop activates once in profit; 4% pullback from peak
ATR_PERIOD           = 14
COOLDOWN_SECONDS     = 300
# Surge / flash detection (real-time ticks, not candle-based)
SURGE_WINDOW         = 20    # look back N price ticks from WebSocket feed
SURGE_PCT            = 0.025 # 2.5% move within those ticks = surge event
SURGE_COOLDOWN       = 90    # seconds before another surge signal fires on same pair
MAX_EXPOSURE_PER_PAIR = 1.0
TRADE_HISTORY_FILE   = os.path.join(os.path.dirname(__file__), "trades.json")
CONFIG_FILE          = os.path.join(os.path.dirname(__file__), "config.json")
CANDLE_CACHE_FILE    = os.path.join(os.path.dirname(__file__), "candle_cache.json")

# Map our timeframe labels to Coinbase granularity strings
# https://docs.cdp.coinbase.com/advanced-trade/reference/product_getcandles
TF_TO_GRANULARITY = {
    '1m':  'ONE_MINUTE',
    '5m':  'FIVE_MINUTE',
    '1h':  'ONE_HOUR',
    '1d':  'ONE_DAY',
}

# Max candles per Coinbase API call
COINBASE_MAX_CANDLES = 300


# ── Math primitives ────────────────────────────────────────────────────────────

def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average — α = 2/(N+1).

    EMA weights recent prices geometrically more than older ones, making it
    3-5× faster to react than SMA while remaining smooth.  The first valid
    value is seeded with the SMA over the first `period` bars to avoid the
    'ramp-up' distortion of initialising with a single price.

    Returns an array of the same length as `values`; the first `period-1`
    positions are NaN (insufficient history).
    """
    n = len(values)
    if n < period:
        return np.full(n, np.nan)
    alpha = 2.0 / (period + 1)
    out = np.empty(n)
    out[:period - 1] = np.nan
    out[period - 1] = values[:period].mean()     # SMA seed
    for i in range(period, n):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Smoothed Moving Average — α = 1/N.

    The correct smoothing for Wilder's ATR and RSI formulas.  Slower than
    EMA (α = 2/(N+1)) but mathematically required for those indicators.
    """
    n = len(values)
    if n < period:
        return np.full(n, np.nan)
    alpha = 1.0 / period
    out = np.empty(n)
    out[:period - 1] = np.nan
    out[period - 1] = values[:period].mean()
    for i in range(period, n):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index — momentum oscillator, 0-100 scale.

    Uses Wilder's RMA for the gain/loss averages (matches TradingView exactly).
    Interpretation:
      > 70  overbought — avoid new longs
      < 30  oversold   — avoid new shorts
      50    neutral — trend confirmation zone

    Returns same length as `closes`; first `period` values are NaN.
    """
    n = len(closes)
    if n < period + 1:
        return np.full(n, np.nan)
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    # RS = avg_gain / avg_loss; RSI = 100 - 100/(1+RS)
    with np.errstate(divide='ignore', invalid='ignore'):
        rs      = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
        rsi_arr = np.where(avg_loss > 0, 100.0 - 100.0 / (1.0 + rs), 100.0)
    # Prepend NaN to align with original `closes` length (diff reduces by 1)
    return np.concatenate([[np.nan], rsi_arr])


def atr_series(candles: list, period: int = 14) -> np.ndarray:
    """Wilder's ATR as a full series (matches TradingView).

    True Range = max(H-L, |H-Cprev|, |L-Cprev|)
    ATR        = RMA(TR, period)

    Returns array aligned with candles[1:] (first candle has no prior close).
    """
    if len(candles) < 2:
        return np.array([0.0])
    arr = np.array(candles)
    h, l, c_prev = arr[1:, 2], arr[1:, 3], arr[:-1, 4]
    tr = np.maximum(h - l, np.maximum(np.abs(h - c_prev), np.abs(l - c_prev)))
    return rma(tr, period)


def adx(candles: list, period: int = 14) -> float:
    """Average Directional Index — trend strength, 0-100.

    ADX tells you IF a trend exists, not which direction.
    < 20 : ranging / sideways market — EMA crosses are noise, skip signals
    20-25: weak trend forming
    > 25 : confirmed trend — signals are meaningful
    > 40 : very strong trend — highest reliability

    Formula matches TradingView's ADX indicator exactly.
    """
    n = len(candles)
    if n < period * 2 + 2:
        return 0.0
    arr = np.array([[c[2], c[3], c[4]] for c in candles], dtype=float)
    hi, lo = arr[:, 0], arr[:, 1]

    # Directional Movement (raw, length n-1)
    up_move   = np.diff(hi)
    down_move = -np.diff(lo)
    plus_dm   = np.where((up_move > down_move) & (up_move   > 0), up_move,   0.0)
    minus_dm  = np.where((down_move > up_move)  & (down_move > 0), down_move, 0.0)

    # True Range (length n-1) — reuse atr_series helper
    tr_arr = atr_series(candles, period)   # length n-1, RMA-smoothed ATR

    # Smooth DM+ and DM- with Wilder's RMA
    sdp  = rma(plus_dm,  period)
    sdm  = rma(minus_dm, period)
    # Align all three arrays (all length n-1, some leading NaN)
    valid = (~np.isnan(sdp)) & (~np.isnan(sdm)) & (~np.isnan(tr_arr)) & (tr_arr > 0)
    if valid.sum() < period:
        return 0.0

    di_p = 100.0 * sdp[valid] / tr_arr[valid]
    di_m = 100.0 * sdm[valid] / tr_arr[valid]
    denom = di_p + di_m
    dx    = np.where(denom > 0, 100.0 * np.abs(di_p - di_m) / denom, 0.0)
    adx_s = rma(dx, period)
    valid2 = ~np.isnan(adx_s)
    return float(adx_s[valid2][-1]) if valid2.sum() > 0 else 0.0


# ── Credentials ──────────────────────────────────────────────────────────────
def load_credentials():
    """Load from config.json or environment variables.

    Setup guide: https://docs.cdp.coinbase.com/coinbase-app/docs/authentication
    """
    env_key    = os.environ.get("COINBASE_API_KEY", "")
    env_secret = os.environ.get("COINBASE_API_SECRET", "")
    if env_key and env_secret:
        return env_key, env_secret, ""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            return cfg.get("api_key", ""), cfg.get("api_secret", ""), cfg.get("passphrase", "")
        except Exception:
            pass
    return "", "", ""


def save_credentials(api_key: str, api_secret: str, passphrase: str = ""):
    # Preserve any existing settings block when re-saving credentials.
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg.update({"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase})
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


# ── User Settings Persistence ─────────────────────────────────────────────────
_SETTINGS_KEY = "settings"

def load_user_settings() -> dict:
    """Return persisted user settings or empty dict (caller applies defaults)."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get(_SETTINGS_KEY, {})
    except Exception:
        return {}


def save_user_settings(settings: dict):
    """Merge *settings* into the config file without touching credentials."""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg[_SETTINGS_KEY] = settings
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


# ── Coinbase REST client factory ──────────────────────────────────────────────
def make_client(api_key: str, api_secret: str) -> RESTClient:
    """Return an authenticated Coinbase Advanced Trade REST client.

    Uses the official coinbase-advanced-py SDK which handles CDP JWT auth
    automatically: https://github.com/coinbase/coinbase-advanced-py
    """
    return RESTClient(api_key=api_key, api_secret=api_secret)


def make_ws_jwt(api_key: str, api_secret: str) -> str:
    """Generate a fresh JWT for WebSocket authentication.

    WebSocket auth docs: https://docs.cdp.coinbase.com/advanced-trade/docs/ws-auth
    """
    return build_ws_jwt(api_key, api_secret)


# ── Candle normalization ──────────────────────────────────────────────────────
def normalize_candles(raw_candles: list) -> list:
    """Convert Coinbase candle dicts to [timestamp_ms, open, high, low, close, volume].

    Coinbase returns: {start, low, high, open, close, volume}
    where start is a Unix timestamp string in seconds.
    """
    out = []
    for c in raw_candles:
        try:
            out.append([
                int(c['start']) * 1000,   # → milliseconds
                float(c['open']),
                float(c['high']),
                float(c['low']),
                float(c['close']),
                float(c['volume']),
            ])
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda x: x[0])


# ── Heikin Ashi ───────────────────────────────────────────────────────────────
def heikin_ashi(candles: list) -> list:
    """Convert standard OHLCV candles to Heikin Ashi.

    Formula reference: https://school.stockcharts.com/doku.php?id=chart_analysis:heikin_ashi

    HA_close = (O+H+L+C)/4
    HA_open  = (HA_open_prev + HA_close_prev)/2   [first bar: (O+C)/2]
    HA_high  = max(H, HA_open, HA_close)
    HA_low   = min(L, HA_open, HA_close)
    """
    if not candles or len(candles[0]) != 6:
        return []
    ha = []
    for i, (ts, o, h, l, c, v) in enumerate(candles):
        ha_close = (o + h + l + c) / 4.0
        ha_open  = (o + c) / 2.0 if i == 0 else (ha[-1][1] + ha[-1][4]) / 2.0
        ha.append([
            ts,
            ha_open,
            max(h, ha_open, ha_close),
            min(l, ha_open, ha_close),
            ha_close,
            v,
        ])
    return ha


# ── Indicator Engine ──────────────────────────────────────────────────────────
class IndicatorEngine:
    """Indicator calculations: ATR (Wilder), RSI, Order Blocks, FVG, S/R.

    All results are cached per pair so _refresh_chart can read them without
    re-running O(N) algorithms every second.  Recalculated only in
    _ingest_candles (on actual new candle data).
    """

    def __init__(self):
        self.data = defaultdict(lambda: {
            'sr_zones':        [],
            'order_blocks':    [],
            'fair_value_gaps': [],
            'atr':             0.0,
            'rsi':             50.0,   # current RSI value (last bar)
            'rsi_series':      [],     # full series for chart display
            'ema_trend_up':    None,   # True/False/None if insufficient data
            'adx':             0.0,    # ADX — trend strength (0-100)
        })

    def calculate_support_resistance(self, pair, candles, bin_width=0.001, min_volume=1000):
        if len(candles) < 10:
            return
        closes  = np.array([c[4] for c in candles])
        volumes = np.array([c[5] for c in candles])
        price_range = closes.max() - closes.min()
        if price_range == 0:
            return
        bins = max(2, int(1 / bin_width))
        hist, edges = np.histogram(closes, bins=bins, weights=volumes)
        peaks = [
            ((edges[i] + edges[i+1]) / 2,
             (edges[i] + edges[i+1]) / 2 < closes[-1],
             hist[i])
            for i in range(1, len(hist)-1)
            if hist[i] > hist[i-1] and hist[i] > hist[i+1] and hist[i] > min_volume
        ]
        self.data[pair]['sr_zones'] = sorted(peaks, key=lambda x: x[2], reverse=True)[:10]

    def calculate_order_blocks(self, pair, candles):
        if len(candles) < 3:
            return
        closes     = np.array([c[4] for c in candles])
        highs      = np.array([c[2] for c in candles])
        lows       = np.array([c[3] for c in candles])
        timestamps = np.array([c[0] for c in candles])
        new_obs = []
        for i in range(2, len(candles)):
            is_bullish = highs[i-2] < highs[i-1] > highs[i]
            for j in range(i-1, 0, -1):
                if (is_bullish     and closes[j-1] > closes[j]) or \
                   (not is_bullish and closes[j-1] < closes[j]):
                    new_obs.append((timestamps[j], highs[j], lows[j], is_bullish))
                    break
        self.data[pair]['order_blocks'] = new_obs[-10:]

    def calculate_fair_value_gaps(self, pair, candles):
        if len(candles) < 3:
            return
        highs      = np.array([c[2] for c in candles])
        lows       = np.array([c[3] for c in candles])
        timestamps = np.array([c[0] for c in candles])
        atr_val    = self.data[pair].get('atr', 0)
        fvgs = []
        for i in range(1, len(candles) - 1):
            if lows[i-1] > highs[i+1]:      # bearish FVG
                fvg_hi, fvg_lo = lows[i-1], highs[i+1]
            elif highs[i-1] < lows[i+1]:    # bullish FVG
                fvg_hi, fvg_lo = lows[i+1], highs[i-1]
            else:
                continue
            gap = fvg_hi - fvg_lo
            # ATR-based spike filter: skip gaps wider than 2× ATR (anomalous)
            max_gap = (atr_val * 2.0) if atr_val > 0 else (fvg_lo * 0.01)
            if gap <= 0 or gap > max_gap:
                continue
            is_bull = highs[i-1] < lows[i+1]
            fvgs.append((timestamps[i], fvg_hi, fvg_lo, is_bull))
        self.data[pair]['fair_value_gaps'] = fvgs[-10:]

    def calculate_atr(self, pair, candles):
        """Wilder's ATR — the industry-standard volatility measure.

        Uses RMA (α=1/N) instead of SMA so the result matches TradingView
        and all professional platforms exactly.
        """
        if len(candles) < ATR_PERIOD + 1:
            self.data[pair]['atr'] = 0.0
            return
        series = atr_series(candles, ATR_PERIOD)
        valid  = series[~np.isnan(series)]
        self.data[pair]['atr'] = float(valid[-1]) if len(valid) > 0 else 0.0

    def calculate_rsi(self, pair, candles):
        """RSI(14) — momentum gate for signal quality.

        Stored as both a scalar (current bar) and a full series (chart display).
        """
        if len(candles) < 15:
            self.data[pair]['rsi'] = 50.0
            self.data[pair]['rsi_series'] = []
            return
        closes   = np.array([c[4] for c in candles])
        rsi_vals = rsi(closes, period=14)
        # Scalar: last non-NaN value
        valid = rsi_vals[~np.isnan(rsi_vals)]
        self.data[pair]['rsi']        = float(valid[-1]) if len(valid) > 0 else 50.0
        self.data[pair]['rsi_series'] = rsi_vals.tolist()

    def calculate_adx(self, pair, candles):
        """Cache the ADX trend-strength value."""
        self.data[pair]['adx'] = adx(candles, period=14)

    def calculate_ema_trend(self, pair, candles):
        """Trend direction via EMA(slow_period) slope.

        ema_trend_up = True  → trending up   (bullish bias, prefer buys)
        ema_trend_up = False → trending down  (bearish bias, prefer sells)
        ema_trend_up = None  → insufficient data
        """
        if len(candles) < 3:
            self.data[pair]['ema_trend_up'] = None
            return
        _, p_slow = sorted(MA_PERIODS)[:2]
        closes = np.array([c[4] for c in candles])
        vals   = ema(closes, p_slow)
        valid  = vals[~np.isnan(vals)]
        if len(valid) < 2:
            self.data[pair]['ema_trend_up'] = None
        else:
            self.data[pair]['ema_trend_up'] = bool(valid[-1] > valid[-2])

    def get_signals(self, pair, current_price):
        signals = []
        for price, is_support, _ in self.data[pair]['sr_zones']:
            if current_price and abs(current_price - price) / current_price < 0.005:
                signals.append({'type': 'SR', 'action': 'buy' if is_support else 'sell', 'price': price})
        for _, high, low, is_bullish in self.data[pair]['order_blocks']:
            if low <= current_price <= high:
                signals.append({'type': 'OB', 'action': 'buy' if is_bullish else 'sell', 'price': (high+low)/2})
        for _, high, low, is_bullish in self.data[pair]['fair_value_gaps']:
            if low <= current_price <= high:
                signals.append({'type': 'FVG', 'action': 'buy' if is_bullish else 'sell', 'price': (high+low)/2})
        return signals


# ── MA/EMA Crossover Strategy ────────────────────────────────────────────────
class MACrossover:
    """EMA(9)/EMA(21) crossover with 5 quality gates — full-port scalping mode.

    Signal quality gates (all must pass):
      1. ADX > 20    — market must be trending; ranging markets produce noise crosses
      2. Trend slope — EMA_slow must slope in signal direction for ≥ 3 consecutive bars
      3. RSI zone    — buys blocked above 65; sells blocked below 35
      4. RSI momentum— RSI must be RISING for buys, FALLING for sells
      5. Structure   — BUY: price above EMA_slow; SELL: price below EMA_slow

    Execution: full-port (all available capital per trade), buy→sell→buy alternation
    enforced naturally by capital gates — after a full buy, no USD remains so the
    next buy signal is suppressed until the position is sold.
    """

    def __init__(self, indicator_engine: IndicatorEngine):
        self.ie = indicator_engine
        self.last_trade_time: dict = defaultdict(float)
        self.last_buy_time:   dict = defaultdict(float)
        self.last_sell_time:  dict = defaultdict(float)

    def calculate_signals(self, pair, candles_signal, candles_conf):
        now = time.time()
        if now - self.last_trade_time[pair] < 10:
            return None

        p_fast, p_slow = sorted(MA_PERIODS)[:2]

        closes_s = np.array([c[4] for c in candles_signal], dtype=float)
        closes_c = np.array([c[4] for c in candles_conf],   dtype=float)
        if len(closes_s) < p_slow + 3 or len(closes_c) < p_slow:
            return None

        # ── EMA crossover on signal TF ────────────────────────────────────────
        ema_f_s = ema(closes_s, p_fast)
        ema_s_s = ema(closes_s, p_slow)
        valid   = ~(np.isnan(ema_f_s) | np.isnan(ema_s_s))
        if valid.sum() < 4:
            return None

        vf = ema_f_s[valid]
        vs = ema_s_s[valid]
        prev_diff = float(vf[-2] - vs[-2])
        curr_diff = float(vf[-1] - vs[-1])

        price_now = float(closes_s[-1])
        min_cross = price_now * 0.001   # 0.1% — eliminates flat noise crosses
        if abs(curr_diff) < min_cross:
            return None

        # ── Gate 1: ADX > 20 ──────────────────────────────────────────────────
        curr_adx = self.ie.data[pair].get('adx', 0.0)
        if curr_adx < 20:
            return None

        # ── Gate 2: EMA_slow slope sustained ≥ 3 bars ─────────────────────────
        slope_window = vs[-4:]
        slope_up   = all(slope_window[i] < slope_window[i+1] for i in range(len(slope_window)-1))
        slope_down = all(slope_window[i] > slope_window[i+1] for i in range(len(slope_window)-1))

        # ── Gate 3: RSI zone ──────────────────────────────────────────────────
        curr_rsi = self.ie.data[pair].get('rsi', 50.0)
        if np.isnan(curr_rsi):
            curr_rsi = 50.0

        # ── Gate 4: RSI momentum ──────────────────────────────────────────────
        rsi_series = self.ie.data[pair].get('rsi_series', [])
        if len(rsi_series) >= 3:
            rsi_rising  = rsi_series[-1] > rsi_series[-3]
            rsi_falling = rsi_series[-1] < rsi_series[-3]
        else:
            rsi_rising = rsi_falling = True

        # ── Gate 5: Price structure ────────────────────────────────────────────
        ema_slow_now     = float(vs[-1])
        price_above_slow = price_now > ema_slow_now
        price_below_slow = price_now < ema_slow_now

        # ── Confirmation TF alignment ──────────────────────────────────────────
        ema_f_c = ema(closes_c, p_fast)
        ema_s_c = ema(closes_c, p_slow)
        valid_c = ~(np.isnan(ema_f_c) | np.isnan(ema_s_c))
        if valid_c.sum() < 1:
            return None
        conf_diff = float(ema_f_c[valid_c][-1] - ema_s_c[valid_c][-1])
        min_conf  = price_now * 0.0005   # 0.05%

        # ── BUY ───────────────────────────────────────────────────────────────
        if (prev_diff < 0 and curr_diff > min_cross
                and conf_diff > min_conf
                and slope_up
                and curr_rsi < 65
                and rsi_rising
                and price_above_slow):
            if now - self.last_buy_time.get(pair, 0) < COOLDOWN_SECONDS:
                return None
            return {
                'action': 'buy',
                'price':  price_now,
                'source': f'EMA{p_fast}/EMA{p_slow}',
                'rsi':    curr_rsi,
                'adx':    curr_adx,
            }

        # ── SELL ──────────────────────────────────────────────────────────────
        if (prev_diff > 0 and curr_diff < -min_cross
                and conf_diff < -min_conf
                and slope_down
                and curr_rsi > 35
                and rsi_falling
                and price_below_slow):
            if now - self.last_sell_time.get(pair, 0) < COOLDOWN_SECONDS:
                return None
            return {
                'action': 'sell',
                'price':  price_now,
                'source': f'EMA{p_fast}/EMA{p_slow}',
                'rsi':    curr_rsi,
                'adx':    curr_adx,
            }

        return None

    def calculate_breakout(self, pair, candles):
        """ATR-normalized breakout detector for explosive momentum moves.

        Gates (all must pass):
          • ADX > 20  — only fires in trending markets, not chop
          • ATR momentum ≥ 2× ATR (raised from 1.5×) — higher bar, fewer false breaks
          • Volume > 75th percentile of prior 20 candles
          • RSI < 75 for buys (not already exhausted), RSI > 25 for sells
          • BUY:  close > 20-candle high
          • SELL: close < 20-candle low
        """
        now = time.time()
        if now - self.last_trade_time[pair] < 10:
            return None
        if len(candles) < 25:
            return None

        closes  = np.array([c[4] for c in candles], dtype=float)
        highs   = np.array([c[2] for c in candles], dtype=float)
        lows    = np.array([c[3] for c in candles], dtype=float)
        volumes = np.array([c[5] for c in candles], dtype=float)

        lb         = 20
        cur_close  = float(closes[-1])
        prev_close = float(closes[-2])

        prior_highs = highs[-lb-1:-1]
        prior_lows  = lows[-lb-1:-1]
        prior_vols  = volumes[-lb-1:-1]

        prev_high = float(prior_highs.max())
        prev_low  = float(prior_lows.min())

        # ADX gate — no breakout signals in ranging markets
        curr_adx = self.ie.data[pair].get('adx', 0.0)
        if curr_adx < 20:
            return None

        # RSI gate
        curr_rsi = self.ie.data[pair].get('rsi', 50.0)
        if np.isnan(curr_rsi):
            curr_rsi = 50.0

        # Volume: 75th percentile gate — robust on low-volume pairs
        vol_pct75 = float(np.percentile(prior_vols, 75)) if len(prior_vols) >= 4 else float(prior_vols.mean() * 1.5)
        vol_surge = volumes[-1] > vol_pct75

        # ATR-normalized momentum: raised to 2× ATR — fewer, higher-conviction fires
        atr_val      = self.ie.data[pair].get('atr', 0.0)
        atr_gate     = atr_val * 2.0 if atr_val > 0 else cur_close * 0.025
        price_move   = cur_close - prev_close   # signed

        if cur_close > prev_high and vol_surge and price_move > atr_gate and curr_rsi < 75:
            if now - self.last_buy_time.get(pair, 0) < COOLDOWN_SECONDS:
                return None
            return {
                'action': 'buy',
                'price':  cur_close,
                'source': 'Breakout↑',
                'atr':    atr_val,
            }

        if cur_close < prev_low and vol_surge and price_move < -atr_gate and curr_rsi > 25:
            if now - self.last_sell_time.get(pair, 0) < COOLDOWN_SECONDS:
                return None
            return {
                'action': 'sell',
                'price':  cur_close,
                'source': 'Breakdown↓',
                'atr':    atr_val,
            }

        return None


# ── Trade Persistence ─────────────────────────────────────────────────────────
def load_trade_history() -> dict:
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_trade_history(history: dict):
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Could not save trade history: {e}")


# ── Candle Cache ─────────────────────────────────────────────────────────────
# Per-TF TTL: short TFs go stale faster than daily candles.
_CACHE_TTL = {'1m': 30 * 60, '5m': 2 * 3600, '1h': 12 * 3600, '1d': 24 * 3600}

def load_candle_cache() -> dict:
    """Return cached candle data or empty dict.

    Cache structure:
      { 'saved_at': unix_ts, 'candles': { tf: { pair: [[ts,o,h,l,c,v], ...] } } }

    Each TF is validated independently against its own TTL so a stale 1m cache
    doesn't discard perfectly valid 1d candles.
    """
    if not os.path.exists(CANDLE_CACHE_FILE):
        return {}
    try:
        with open(CANDLE_CACHE_FILE) as f:
            data = json.load(f)
        saved_at = data.get('saved_at', 0)
        raw      = data.get('candles', {})
        out      = {}
        age      = time.time() - saved_at
        for tf, pairs in raw.items():
            ttl = _CACHE_TTL.get(tf, 86400)
            if age <= ttl:
                out[tf] = pairs
        return out
    except Exception:
        return {}


def save_candle_cache(candle_history: dict):
    """Persist candle_history to disk.  candle_history[tf][pair] is a deque of lists."""
    try:
        snapshot = {}
        for tf, pairs in candle_history.items():
            snapshot[tf] = {}
            for pair, dq in pairs.items():
                snapshot[tf][pair] = list(dq)
        payload = {'saved_at': time.time(), 'candles': snapshot}
        with open(CANDLE_CACHE_FILE, 'w') as f:
            json.dump(payload, f, separators=(',', ':'))
    except Exception as e:
        logger.warning(f"Could not save candle cache: {e}")


# ── Price Formatting ──────────────────────────────────────────────────────────
def format_price(price):
    if not isinstance(price, (int, float)) or price == 0:
        return "N/A"
    if price < 0.001:   return f"${price:,.8f}"
    if price < 0.01:    return f"${price:,.7f}"
    if price < 1:       return f"${price:,.4f}"
    if price < 10:      return f"${price:,.3f}"
    if price < 100000:  return f"${price:,.2f}"
    return f"${price:,.0f}"
