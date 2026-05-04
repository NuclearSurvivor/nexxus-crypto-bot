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
            'rsi':             50.0,
            'rsi_series':      [],
            'adx':             0.0,
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



# ── MA/EMA Crossover Strategy ────────────────────────────────────────────────
class MACrossover:
    """MA crossover strategy — v1.0.4b logic.

    SMA(2)/SMA(5) crossover on the signal TF, confirmed by the same
    crossover direction on the confirmation TF.  No extra gates.

      BUY:  MA_fast crosses above MA_slow on signal TF AND conf TF agrees (fast > slow)
      SELL: MA_fast crosses below MA_slow on signal TF AND conf TF agrees (fast < slow)

    Per-direction cooldowns prevent double-firing; a 10s global gap
    prevents same-candle repeat triggers.
    """

    def __init__(self, indicator_engine: IndicatorEngine):
        self.ie = indicator_engine
        self.last_trade_time: dict = defaultdict(float)
        self.last_buy_time:   dict = defaultdict(float)
        self.last_sell_time:  dict = defaultdict(float)

    def calculate_signals(self, pair, candles_signal, candles_conf):
        now = time.time()
        _last_buy  = self.last_buy_time.get(pair, 0)
        _last_sell = self.last_sell_time.get(pair, 0)
        if now - self.last_trade_time[pair] < 10:
            return None

        p_fast, p_slow = sorted(MA_PERIODS)[:2]
        closes_s = np.array([c[4] for c in candles_signal])
        closes_c = np.array([c[4] for c in candles_conf])
        if len(closes_s) < p_slow or len(closes_c) < p_slow:
            return None

        ma_fast_s = np.convolve(closes_s, np.ones(p_fast) / p_fast, mode='valid')
        ma_slow_s = np.convolve(closes_s, np.ones(p_slow) / p_slow, mode='valid')
        ma_fast_c = np.convolve(closes_c, np.ones(p_fast) / p_fast, mode='valid')
        ma_slow_c = np.convolve(closes_c, np.ones(p_slow) / p_slow, mode='valid')

        if len(ma_fast_s) < 2 or len(ma_slow_s) < 2 or len(ma_fast_c) < 1:
            return None

        prev_diff = ma_fast_s[-2] - ma_slow_s[-2]
        curr_diff = ma_fast_s[-1] - ma_slow_s[-1]
        curr_conf = ma_fast_c[-1] - ma_slow_c[-1]

        if prev_diff < 0 and curr_diff > 0 and curr_conf > 0:
            if now - _last_buy < COOLDOWN_SECONDS:
                return None
            return {'action': 'buy',  'price': float(closes_s[-1]), 'source': f'MA{p_fast}/MA{p_slow}'}
        if prev_diff > 0 and curr_diff < 0 and curr_conf < 0:
            if now - _last_sell < COOLDOWN_SECONDS:
                return None
            return {'action': 'sell', 'price': float(closes_s[-1]), 'source': f'MA{p_fast}/MA{p_slow}'}
        return None

    def calculate_breakout(self, pair, candles):
        """Breakout detector — v1.0.4b logic.

        BUY:  close > 20-candle high  AND (volume ≥ 2× avg OR momentum ≥ 2%)
        SELL: close < 20-candle low   AND (volume ≥ 2× avg OR momentum ≤ -2%)
        """
        now = time.time()
        if now - self.last_trade_time[pair] < 10:
            return None
        if len(candles) < 25:
            return None

        closes  = np.array([c[4] for c in candles])
        highs   = np.array([c[2] for c in candles])
        lows    = np.array([c[3] for c in candles])
        volumes = np.array([c[5] for c in candles])

        lb          = 20
        cur_close   = closes[-1]
        prev_close  = closes[-2]
        prior_highs = highs[-lb-1:-1]
        prior_lows  = lows[-lb-1:-1]
        prior_vols  = volumes[-lb-1:-1]

        prev_high = prior_highs.max()
        prev_low  = prior_lows.min()
        avg_vol   = prior_vols.mean() if prior_vols.mean() > 0 else 1e-12
        vol_surge = volumes[-1] > avg_vol * 2.0
        momentum  = (cur_close - prev_close) / prev_close if prev_close > 0 else 0

        if cur_close > prev_high and (vol_surge or momentum > 0.02):
            if now - self.last_buy_time.get(pair, 0) < COOLDOWN_SECONDS:
                return None
            return {'action': 'buy',  'price': cur_close, 'source': 'Breakout↑'}
        if cur_close < prev_low and (vol_surge or momentum < -0.02):
            if now - self.last_sell_time.get(pair, 0) < COOLDOWN_SECONDS:
                return None
            return {'action': 'sell', 'price': cur_close, 'source': 'Breakdown↓'}
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
