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
    "LINK-USD", "DOT-USD", "MATIC-USD", "LTC-USD",
]
COINBASE_WS_URL      = "wss://advanced-trade-ws.coinbase.com"
MAX_HISTORY          = 2000
DISPLAY_CANDLES      = {'1m': 200, '5m': 500, '1h': 300, '1d': 365}
# Double the display count is fetched/stored so MAs are fully warmed up
# across the entire visible window — no cold-start distortion on early candles.
FETCH_CANDLES        = {'1m': 400, '5m': 1000, '1h': 600, '1d': 730}
MA_PERIODS           = [2, 5, 14]
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


# ── Coinbase REST client factory ──────────────────────────────────────────────
def make_client(api_key: str, api_secret: str) -> RESTClient:
    """Return an authenticated Coinbase Advanced Trade REST client.

    Uses the official coinbase-advanced-py SDK which handles CDP JWT auth
    automatically: https://github.com/coinbase/coinbase-advanced-py
    """
    # The SDK handles JWT generation, token refresh, and rate limiting.
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
    """
    if not candles or len(candles[0]) != 6:
        return []
    ha = []
    for i, (ts, o, h, l, c, v) in enumerate(candles):
        ha_close = (o + h + l + c) / 4
        ha_open  = (o + c) / 2 if i == 0 else (ha[-1][1] + ha[-1][4]) / 2
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
    """SMC (Smart Money Concepts) indicator calculations.

    Concepts reference: https://www.investopedia.com/terms/s/smart-money.asp
    """

    def __init__(self):
        self.data = defaultdict(lambda: {
            'sr_zones': [], 'order_blocks': [], 'fair_value_gaps': [], 'atr': 0
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
        fvgs = []
        for i in range(1, len(candles)-1):
            if lows[i-1] > highs[i+1]:      # bearish FVG
                fvg_hi, fvg_lo = lows[i-1], highs[i+1]
            elif highs[i-1] < lows[i+1]:    # bullish FVG
                fvg_hi, fvg_lo = lows[i+1], highs[i-1]
            else:
                continue
            # Skip anomalous gaps from spike candles (> 1% of price)
            if fvg_lo > 0 and (fvg_hi - fvg_lo) / fvg_lo > 0.01:
                continue
            is_bull = highs[i-1] < lows[i+1]
            fvgs.append((timestamps[i], fvg_hi, fvg_lo, is_bull))
        self.data[pair]['fair_value_gaps'] = fvgs[-10:]

    def calculate_atr(self, pair, candles):
        """Average True Range — volatility measure for dynamic SL/TP sizing.

        Reference: https://www.investopedia.com/terms/a/atr.asp
        """
        if len(candles) < 2:
            self.data[pair]['atr'] = 0
            return
        trs = [
            max(candles[i][2] - candles[i][3],
                abs(candles[i][2] - candles[i-1][4]),
                abs(candles[i][3] - candles[i-1][4]))
            for i in range(1, len(candles))
        ]
        window = trs[-ATR_PERIOD:]
        if len(window) >= ATR_PERIOD:
            self.data[pair]['atr'] = sum(window) / ATR_PERIOD

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


# ── MA Crossover Strategy ────────────────────────────────────────────────────
class MACrossover:
    """MA9/MA20 crossover with SMC confirmation.

    Strategy logic:
      - MA9 crosses ABOVE MA20 on 5m + 1m confirms bullish → BUY
      - MA9 crosses BELOW MA20 on 5m + 1m confirms bearish → SELL
      - Must be confirmed by an active Order Block or Fair Value Gap
    """

    def __init__(self, indicator_engine: IndicatorEngine):
        self.ie = indicator_engine
        self.last_trade_time: dict = defaultdict(float)

    def calculate_signals(self, pair, candles_signal, candles_conf):
        """MA crossover on the signal TF confirmed by the confirmation TF.

        SMC (OB/FVG) confirmation removed — it rejects breakout moves by design
        because surges break out OF consolidation zones, not into them.

        Conditions:
          BUY:  MA_fast crosses above MA_slow on signal TF
                AND MA_fast > MA_slow on confirmation TF (trend aligned)
          SELL: MA_fast crosses below MA_slow on signal TF
                AND MA_fast < MA_slow on confirmation TF

        Uses sorted(MA_PERIODS)[:2] so bot and chart always use identical periods.
        """
        if time.time() - self.last_trade_time[pair] < COOLDOWN_SECONDS:
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
            return {'action': 'buy',  'price': closes_s[-1], 'source': f'MA{p_fast}/MA{p_slow}'}
        if prev_diff > 0 and curr_diff < 0 and curr_conf < 0:
            return {'action': 'sell', 'price': closes_s[-1], 'source': f'MA{p_fast}/MA{p_slow}'}
        return None

    def calculate_breakout(self, pair, candles):
        """
        Breakout / momentum detector — catches explosive moves that fire
        before MA crossover can react.  Designed for low-cap spikes like XCN.

        BUY  conditions (all required):
          ① Current close > highest close of the prior 20 candles (breakout)
          ② Volume surge (≥ 2× 20-period avg)  OR  single-candle move ≥ 2%

        SELL conditions:
          ① Current close < lowest close of the prior 20 candles (breakdown)
          ② Same volume/momentum gate

        No SMC confirmation — these moves are too fast to wait for OB/FVG.
        Uses the same cooldown as MA crossover to prevent double-firing.
        """
        if time.time() - self.last_trade_time[pair] < COOLDOWN_SECONDS:
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
            return {'action': 'buy',  'price': cur_close, 'source': 'Breakout↑'}
        if cur_close < prev_low  and (vol_surge or momentum < -0.02):
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
def load_candle_cache() -> dict:
    """Return cached candle data or empty dict.  Structure:
    { 'saved_at': unix_ts, 'candles': { tf: { pair: [[ts,o,h,l,c,v], ...] } } }
    Cache is rejected if older than 24 hours (stale beyond usefulness).
    """
    if not os.path.exists(CANDLE_CACHE_FILE):
        return {}
    try:
        with open(CANDLE_CACHE_FILE) as f:
            data = json.load(f)
        age = time.time() - data.get('saved_at', 0)
        if age > 86400:   # discard if older than 24 h
            return {}
        return data.get('candles', {})
    except Exception:
        return {}


def save_candle_cache(candle_history: dict):
    """Persist candle_history to disk.  candle_history[tf][pair] is a deque of lists."""
    try:
        snapshot = {}
        for tf, pairs in candle_history.items():
            snapshot[tf] = {}
            for pair, dq in pairs.items():
                snapshot[tf][pair] = list(dq)   # deque → plain list
        payload = {'saved_at': time.time(), 'candles': snapshot}
        with open(CANDLE_CACHE_FILE, 'w') as f:
            json.dump(payload, f, separators=(',', ':'))  # compact — no indent
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
