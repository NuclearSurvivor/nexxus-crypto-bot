"""
NEXXUS Crypto Bot — main.py
Uses official coinbase-advanced-py SDK for all exchange communication.

Key references:
  Coinbase Advanced Trade API:  https://docs.cdp.coinbase.com/advanced-trade/docs/welcome
  CDP API Key setup:            https://docs.cdp.coinbase.com/coinbase-app/docs/authentication
  coinbase-advanced-py SDK:     https://github.com/coinbase/coinbase-advanced-py
  Advanced Trade WebSocket:     https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview
  WebSocket auth:               https://docs.cdp.coinbase.com/advanced-trade/docs/ws-auth
  REST candles endpoint:        https://docs.cdp.coinbase.com/advanced-trade/reference/product_getcandles
  REST orders endpoint:         https://docs.cdp.coinbase.com/advanced-trade/reference/create_order
"""

import asyncio
import json
import os
import sys
import threading
import time
import uuid
import logging
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta

import customtkinter as ctk
from tkinter import messagebox
import tkinter as tk
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import websockets
import pytz
import http.server
import socketserver

# Official Coinbase SDK
from coinbase.rest import RESTClient
from coinbase.jwt_generator import build_ws_jwt

from engine import (
    IndicatorEngine, MACrossover, heikin_ashi, normalize_candles, format_price,
    load_credentials, save_credentials, load_trade_history, save_trade_history,
    load_candle_cache, save_candle_cache,
    load_user_settings, save_user_settings,
    make_client, make_ws_jwt,
    ema as _ema_fn,          # EMA for chart MA lines (replaces np.convolve SMA)
    rsi as _rsi_fn,          # RSI series for chart display
    atr_series as _atr_fn,   # Wilder ATR series
    TRADING_PAIRS, WATCHLIST_PAIRS, COINBASE_WS_URL, MAX_HISTORY, DISPLAY_CANDLES, FETCH_CANDLES,
    MA_PERIODS, TIMEFRAMES, ORDER_AMOUNT_USD, MINIMUM_RESERVE, WEBHOOK_PORT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAIL_STOP_PCT, ATR_PERIOD, COOLDOWN_SECONDS,
    SURGE_WINDOW, SURGE_PCT, SURGE_COOLDOWN,
    MAX_EXPOSURE_PER_PAIR, TF_TO_GRANULARITY, COINBASE_MAX_CANDLES, CONFIG_FILE
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d  %(levelname)-5s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'bot.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("nexxus")
# Suppress websocket frame-level debug noise (ticker spam)
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('websockets.client').setLevel(logging.WARNING)

# ── Theme ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Theme system ──────────────────────────────────────────────────────────────
THEMES = {
    "Midnight": {   # default — blue/cyan glass
        "C_BG":       "#03060d",
        "C_PANEL":    "#060c1a",
        "C_GLASS":    "#091525",
        "C_CARD":     "#0c1e38",
        "C_CARD2":    "#132848",
        "C_BORDER":   "#1a3560",
        "C_BORDER2":  "#204080",
        "C_GLOW":     "#0a2860",
        "C_ACCENT":   "#00d4ff",
        "C_ACCENT2":  "#7c5cfc",
        "C_ACCENT3":  "#fbbf24",
        "C_GREEN":    "#00e896",
        "C_RED":      "#ff4560",
        "C_ORANGE":   "#ff9500",
        "C_TEXT":     "#e8f4ff",
        "C_TEXT2":    "#6ea8cc",
        "C_MUTED":    "#2a4a6a",
        "C_CHART_BG": "#020508",
        "C_NAV_ACT":  "#0a1e40",
        "C_HL":       "#1e4080",
    },
    "Emerald": {    # green trading terminal
        "C_BG":       "#030d06",
        "C_PANEL":    "#060f09",
        "C_GLASS":    "#091a0e",
        "C_CARD":     "#0c2212",
        "C_CARD2":    "#102a16",
        "C_BORDER":   "#1a5030",
        "C_BORDER2":  "#207040",
        "C_GLOW":     "#0a3018",
        "C_ACCENT":   "#00ff88",
        "C_ACCENT2":  "#00cc66",
        "C_ACCENT3":  "#fbbf24",
        "C_GREEN":    "#00ff88",
        "C_RED":      "#ff4560",
        "C_ORANGE":   "#ff9500",
        "C_TEXT":     "#e8fff2",
        "C_TEXT2":    "#6ecf99",
        "C_MUTED":    "#2a5a3a",
        "C_CHART_BG": "#020a04",
        "C_NAV_ACT":  "#0a2814",
        "C_HL":       "#1e6030",
    },
    "Crimson": {    # red/orange aggressive trading
        "C_BG":       "#0d0305",
        "C_PANEL":    "#130408",
        "C_GLASS":    "#1a050a",
        "C_CARD":     "#20060c",
        "C_CARD2":    "#280810",
        "C_BORDER":   "#501020",
        "C_BORDER2":  "#701828",
        "C_GLOW":     "#300810",
        "C_ACCENT":   "#ff4560",
        "C_ACCENT2":  "#ff6030",
        "C_ACCENT3":  "#fbbf24",
        "C_GREEN":    "#00e896",
        "C_RED":      "#ff4560",
        "C_ORANGE":   "#ff9500",
        "C_TEXT":     "#fff0f2",
        "C_TEXT2":    "#cc8090",
        "C_MUTED":    "#5a2030",
        "C_CHART_BG": "#080202",
        "C_NAV_ACT":  "#200610",
        "C_HL":       "#601020",
    },
    "Aurora": {     # purple/teal
        "C_BG":       "#060310",
        "C_PANEL":    "#0a0518",
        "C_GLASS":    "#100824",
        "C_CARD":     "#150b30",
        "C_CARD2":    "#1c103e",
        "C_BORDER":   "#3a1880",
        "C_BORDER2":  "#5020c0",
        "C_GLOW":     "#200860",
        "C_ACCENT":   "#a855f7",
        "C_ACCENT2":  "#06b6d4",
        "C_ACCENT3":  "#fbbf24",
        "C_GREEN":    "#00e896",
        "C_RED":      "#ff4560",
        "C_ORANGE":   "#ff9500",
        "C_TEXT":     "#f0e8ff",
        "C_TEXT2":    "#9a80cc",
        "C_MUTED":    "#3a2060",
        "C_CHART_BG": "#030108",
        "C_NAV_ACT":  "#10063a",
        "C_HL":       "#401890",
    },
}

_ACTIVE_THEME = "Midnight"

def _apply_theme(name: str):
    global _ACTIVE_THEME
    global C_BG, C_PANEL, C_GLASS, C_CARD, C_CARD2, C_BORDER, C_BORDER2
    global C_GLOW, C_ACCENT, C_ACCENT2, C_ACCENT3
    global C_GREEN, C_RED, C_ORANGE, C_TEXT, C_TEXT2, C_MUTED
    global C_CHART_BG, C_NAV_ACT, C_HL
    t = THEMES.get(name, THEMES["Midnight"])
    _ACTIVE_THEME = name
    C_BG       = t["C_BG"]
    C_PANEL    = t["C_PANEL"]
    C_GLASS    = t["C_GLASS"]
    C_CARD     = t["C_CARD"]
    C_CARD2    = t["C_CARD2"]
    C_BORDER   = t["C_BORDER"]
    C_BORDER2  = t["C_BORDER2"]
    C_GLOW     = t["C_GLOW"]
    C_ACCENT   = t["C_ACCENT"]
    C_ACCENT2  = t["C_ACCENT2"]
    C_ACCENT3  = t["C_ACCENT3"]
    C_GREEN    = t["C_GREEN"]
    C_RED      = t["C_RED"]
    C_ORANGE   = t["C_ORANGE"]
    C_TEXT     = t["C_TEXT"]
    C_TEXT2    = t["C_TEXT2"]
    C_MUTED    = t["C_MUTED"]
    C_CHART_BG = t["C_CHART_BG"]
    C_NAV_ACT  = t["C_NAV_ACT"]
    C_HL       = t["C_HL"]

# Load saved theme from config before anything else
try:
    with open(CONFIG_FILE) as _tf:
        _tcfg = json.load(_tf)
    _apply_theme(_tcfg.get("theme", "Midnight"))
except Exception:
    _apply_theme("Midnight")

# Render budget — throttles hover/drag redraws to the monitor's native refresh rate
def _detect_monitor_hz() -> float:
    """Query xrandr for the active display refresh rate; fall back to 60."""
    try:
        import subprocess, re
        out = subprocess.check_output(['xrandr'], text=True, timeout=2)
        for line in out.splitlines():
            m = re.search(r'([\d.]+)\*', line)
            if m:
                hz = float(m.group(1))
                if 24 <= hz <= 500:   # sanity range
                    return hz
    except Exception:
        pass
    return 60.0

_MONITOR_HZ = _detect_monitor_hz()
_FRAME_DT   = 1.0 / _MONITOR_HZ   # e.g. 6.94 ms @ 144 Hz, 16.67 ms @ 60 Hz

plt.rcParams.update({
    "figure.facecolor":  C_CHART_BG,
    "axes.facecolor":    C_CHART_BG,
    "axes.edgecolor":    C_BORDER,
    "xtick.color":       C_MUTED,
    "ytick.color":       C_MUTED,
    "xtick.labelsize":   7,
    "ytick.labelsize":   7,
    "grid.color":        C_GLASS,
    "grid.linestyle":    "-",
    "grid.alpha":        0.5,
})


# ── Webhook ───────────────────────────────────────────────────────────────────
class WebhookHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, app=None, **kwargs):
        self.app = app
        super().__init__(*args, **kwargs)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0 or length > 65536:
                self.send_response(400); self.end_headers(); return
            # Only accept requests from localhost
            client_ip = self.client_address[0]
            if client_ip not in ('127.0.0.1', '::1', 'localhost'):
                self.send_response(403); self.end_headers(); return
            data = json.loads(self.rfile.read(length))
            if data.get('status') == 'completed':
                amt = float(data.get('amount', 0))
                if amt > 0 and self.app and self.app.root_alive:
                    self.app.root.after(0, self.app.on_deposit, amt)
        except Exception as e:
            logger.warning(f"Webhook: {e}")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


# ── Login Screen ──────────────────────────────────────────────────────────────
class LoginScreen(ctk.CTkFrame):
    def __init__(self, parent, on_login):
        super().__init__(parent, fg_color=C_BG)
        self.on_login = on_login
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._build()

        # Auto-fill and skip login if creds already saved
        key, secret, passphrase = load_credentials()
        if key and secret:
            self.key_entry.insert(0, key)
            self.secret_box.insert("1.0", secret)
            if passphrase:
                self.pass_entry.insert(0, passphrase)
            # Auto-connect after UI renders
            parent.after(200, self._do_login)

    def _build(self):
        center = ctk.CTkFrame(self, fg_color=C_GLASS, corner_radius=24,
                              border_width=1, border_color=C_BORDER2)
        center.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.40, relheight=0.78)

        # Top highlight strip — tk.Frame avoids CTk canvas resize artifacts
        import tkinter as _tk
        _tk.Frame(center, height=1, bg=C_HL, bd=0, highlightthickness=0).pack(fill="x")

        # Glow ring around logo
        logo_ring = ctk.CTkFrame(center, fg_color="transparent",
                                 border_width=1, border_color=C_ACCENT, corner_radius=8)
        logo_ring.pack(pady=(28, 2))
        ctk.CTkLabel(logo_ring, text="⬡", font=("Segoe UI", 52),
                     text_color=C_ACCENT).pack(padx=8, pady=4)
        ctk.CTkLabel(center, text="NEXXUS", font=("Segoe UI", 28, "bold"),
                     text_color=C_TEXT).pack()
        ctk.CTkLabel(center, text="Crypto Trading Bot", font=("Segoe UI", 13),
                     text_color=C_MUTED).pack(pady=(0, 24))

        form = ctk.CTkFrame(center, fg_color="transparent")
        form.pack(fill="x", padx=36)

        def field(label, widget):
            ctk.CTkLabel(form, text=label, font=("Segoe UI", 12),
                         text_color=C_MUTED).pack(anchor="w", pady=(0, 4))
            widget.pack(fill="x", pady=(0, 12))

        self.key_entry = ctk.CTkEntry(
            form, placeholder_text="organizations/…/apiKeys/…",
            height=38, corner_radius=8, fg_color=C_CARD,
            border_color=C_BORDER, text_color=C_TEXT)
        field("API Key Name  ·  "
              "Get yours at: https://portal.cdp.coinbase.com",
              self.key_entry)

        self.secret_box = ctk.CTkTextbox(
            form, height=90, corner_radius=8, fg_color=C_CARD,
            border_color=C_BORDER, text_color=C_TEXT, font=("Courier New", 10))
        field("EC Private Key (PEM)  ·  ECDSA / ES256 required", self.secret_box)

        self.pass_entry = ctk.CTkEntry(
            form, placeholder_text="Leave blank if none",
            height=38, corner_radius=8, show="•",
            fg_color=C_CARD, border_color=C_BORDER, text_color=C_TEXT)
        field("Passphrase (optional)", self.pass_entry)

        self.status_lbl = ctk.CTkLabel(form, text="", text_color=C_RED,
                                        font=("Segoe UI", 11))
        self.status_lbl.pack()

        ctk.CTkButton(
            form, text="Connect to Coinbase", height=44, corner_radius=10,
            fg_color=C_ACCENT2, hover_color="#6a4de0",
            font=("Segoe UI", 14, "bold"),
            command=self._do_login
        ).pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(
            center,
            text="Coinbase Advanced Trade API  ·  docs.cdp.coinbase.com/advanced-trade/docs/welcome",
            font=("Segoe UI", 9), text_color=C_MUTED
        ).pack(pady=(16, 0))

    def _do_login(self):
        key    = self.key_entry.get().strip()
        secret = self.secret_box.get("1.0", "end").strip()
        phrase = self.pass_entry.get().strip()
        if not key or not secret:
            self.status_lbl.configure(text="API Key and Private Key are required.")
            return
        self.status_lbl.configure(text="Connecting…", text_color=C_ORANGE)
        save_credentials(key, secret, phrase)
        self.place_forget()
        self.on_login(key, secret, phrase)


# ── Dashboard ─────────────────────────────────────────────────────────────────
class Dashboard(ctk.CTkFrame):
    def __init__(self, parent, api_key, api_secret, passphrase):
        super().__init__(parent, fg_color=C_BG)
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.root = parent

        self.api_key    = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase

        # REST client (synchronous, called via asyncio.to_thread)
        self.client: RESTClient = make_client(api_key, api_secret)

        # Async event loop runs in a background thread
        self.loop = asyncio.new_event_loop()

        # State
        self.usd_balance     = 0.0
        self.usdc_balance    = 0.0        # USDC held (also counted in usd_balance)
        self.usdt_balance    = 0.0        # USDT held (also counted in usd_balance)
        self.bot_balance     = 0.0        # general (unassigned) bot pool
        self.bot_pair_alloc  = defaultdict(float)   # per-coin USD budget
        self.bot_coin_qty    = defaultdict(float)   # coins handed to bot from holdings
        self.initial_balance = 0.0
        self.bot_exposure    = defaultdict(float)
        self.real_exposure   = defaultdict(float)
        self.live_prices      = defaultdict(float)
        self._price_ts        = defaultdict(float)   # C4: timestamp of last price update
        self._base_precision  = {}                   # pair → int decimal places for base_size
        self._quote_precision = {}                   # pair → int decimal places for limit_price
        self.price_history   = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
        self.candle_history  = {tf: defaultdict(lambda: deque(maxlen=MAX_HISTORY))
                                 for tf in TIMEFRAMES}
        self.percent_change  = {tf: defaultdict(float) for tf in TIMEFRAMES}
        self.pct_24h         = {}   # true 24h change (last 2 daily candles); None until loaded
        self.trade_history   = load_trade_history()
        self.order_locks     = defaultdict(bool)
        self.order_lock_ts   = defaultdict(float)  # when the lock was acquired

        # ── Load persisted user settings ──────────────────────────────────────
        _s = load_user_settings()
        self.signal_tf        = _s.get('signal_tf', '1h')
        self.signal_direction = _s.get('signal_direction', 'Both')  # 'Both' | 'Buy Only' | 'Sell Only'
        # Swap targets: '' means no swap (hold as USD). Default '' not 'USDC' so
        # an explicit "USD" selection persists correctly across restarts.
        _saved_swaps = _s.get('swap_targets', {})
        self.swap_targets = {p: _saved_swaps.get(p, 'USDC') for p in TRADING_PAIRS}

        _saved_ma = _s.get('ma_periods')
        if _saved_ma and isinstance(_saved_ma, list) and len(_saved_ma) >= 2:
            import engine as _eng
            _eng.MA_PERIODS = _saved_ma
            globals()['MA_PERIODS'] = list(_saved_ma)   # C1 fix: update local global too
        self.custom_ma_periods = list(MA_PERIODS)  # mutable — user can override
        # Restore numeric trading params if saved
        if 'stop_loss_pct'    in _s: globals()['STOP_LOSS_PCT']    = float(_s['stop_loss_pct'])
        if 'take_profit_pct'  in _s: globals()['TAKE_PROFIT_PCT']  = float(_s['take_profit_pct'])
        if 'order_amount_usd' in _s: globals()['ORDER_AMOUNT_USD'] = float(_s['order_amount_usd'])
        if 'minimum_reserve'  in _s: globals()['MINIMUM_RESERVE']  = float(_s['minimum_reserve'])
        if 'cooldown_seconds' in _s:
            globals()['COOLDOWN_SECONDS'] = float(_s['cooldown_seconds'])
            import engine as _eng2; _eng2.COOLDOWN_SECONDS = float(_s['cooldown_seconds'])
        self.alloc_round_tokens     = int(_s.get('alloc_round_tokens', 250))
        self.auto_compound_enabled  = bool(_s.get('auto_compound_enabled', False))
        self.auto_compound_pct      = float(_s.get('auto_compound_pct',  10.0))  # % of avail per trade
        self.auto_compound_cap      = float(_s.get('auto_compound_cap', 500.0))  # max order USD

        # ── Restore persisted bot pool balances ───────────────────────────────
        # bot_balance and bot_pair_alloc are saved to config.json after every
        # trade and allocation so cash accumulated from sell cycles survives
        # restarts and isn't silently lost.
        self.bot_balance = float(_s.get('bot_balance', 0.0))
        for _p, _v in _s.get('bot_pair_alloc', {}).items():
            if _p in TRADING_PAIRS:
                self.bot_pair_alloc[_p] = float(_v)
        # Reconstruct bot_exposure from any open BUY trades persisted in trades.json
        # so the monitor loop has correct state if the bot is restarted mid-position.
        for _t in self.trade_history.values():
            if _t.get('event') == 'trade' and _t.get('side') == 'buy':
                _tp = _t.get('symbol', '')
                if _tp in TRADING_PAIRS:
                    self.bot_exposure[_tp] += float(_t.get('entry_price', 0)) * float(_t.get('quantity', 0))

        self.running         = True
        self.paused          = False
        self.root_alive      = True

        # Seed last_executed_signal from the most recent trade in history so the
        # key panel shows the last signal immediately on startup without needing
        # to wait for a new trade to fire in the current session.
        self.last_executed_signal = None
        _les_best_ts = -1
        for _les_t in self.trade_history.values():
            _les_ts = float(_les_t.get('timestamp', 0))
            if _les_ts > _les_best_ts:
                _les_best_ts = _les_ts
                _les_ms = _les_ts / 1000.0 if _les_ts > 1e12 else _les_ts
                self.last_executed_signal = {
                    'pair':   _les_t.get('symbol', _les_t.get('pair', '')),
                    'side':   _les_t.get('side', ''),
                    'price':  float(_les_t.get('entry_price', _les_t.get('price', 0))),
                    'qty':    float(_les_t.get('quantity', 0)),
                    'ts':     _les_ms,
                    'source': 'history',
                }

        # Fallback: if trades.json was empty (e.g. all positions closed and wiped),
        # scan bot.log for the most recent FILLED line to recover last signal display.
        # Line format: "YYYY-MM-DD HH:MM:SS.mmm  INFO   [TRADE] ◆ FILLED SELL XCN-USD  ... fill_price=$0.005140 ..."
        if self.last_executed_signal is None:
            import re as _re
            _log_path = os.path.join(os.path.dirname(__file__), 'bot.log')
            _fill_pat = _re.compile(
                r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'   # datetime
                r'.*◆ FILLED (BUY|SELL) ([A-Z]+-[A-Z]+)'     # side + pair
                r'.*fill_price=\$?([\d.]+)'                   # fill_price
            )
            _best_fill = None
            try:
                with open(_log_path, 'r', errors='replace') as _lf:
                    for _line in _lf:
                        _m = _fill_pat.search(_line)
                        if _m:
                            _best_fill = _m   # last match wins (latest entry)
            except Exception:
                pass
            if _best_fill:
                try:
                    from datetime import datetime as _dt2
                    _ts = _dt2.strptime(_best_fill.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                    self.last_executed_signal = {
                        'pair':   _best_fill.group(3),
                        'side':   _best_fill.group(2).lower(),
                        'price':  float(_best_fill.group(4)),
                        'qty':    0.0,
                        'ts':     _ts,
                        'source': 'log',
                    }
                except Exception:
                    pass

        self.indicator_engine = IndicatorEngine()
        self.strategy         = MACrossover(self.indicator_engine)
        self._ws_jwt_ts       = 0.0   # timestamp of last WS JWT
        self.surge_last_buy   = defaultdict(float)   # H3: per-pair per-direction cooldown
        self.surge_last_sell  = defaultdict(float)

        # Live forming candle — updated on every WS tick so the chart shows the
        # currently-forming bar in real-time (not just after the candle closes).
        # (pair, tf) → [ts_ms, open, high, low, close, vol]
        self._forming_candle: dict = {}

        # Order book — updated from level2 WS channel.
        # pair → {'bids': sorted list [(price, qty)], 'asks': sorted list [(price, qty)]}
        self._order_book: dict = defaultdict(lambda: {'bids': [], 'asks': []})
        self._ob_window = None   # CTkToplevel reference

        # Active page name — used to gate chart live-refresh to chart tab only
        self._active_page: str = 'dashboard'

        # ── Pre-populate candle history from disk cache ───────────────────────
        # Charts render immediately on startup; live API fills in newer candles.
        _cache = load_candle_cache()
        for tf, pairs in _cache.items():
            if tf not in self.candle_history:
                continue
            for pair, rows in pairs.items():
                if rows:
                    self.candle_history[tf][pair].extend(rows)
                    # Seed percent_change and pct_24h from cached data
                    if len(rows) >= 2:
                        op, lc = rows[0][4], rows[-1][4]
                        self.percent_change[tf][pair] = ((lc - op) / op * 100) if op else 0
                        if tf == '1d':
                            pc, lc2 = rows[-2][4], rows[-1][4]
                            self.pct_24h[pair] = ((lc2 - pc) / pc * 100) if pc else 0

        self._build_layout()
        self._start_backend()
        self._start_webhook()
        self.root.after(1000, self._tick_status)
        self.root.after(1000, self._chart_live_tick)   # 1-second live chart refresh

        # Force redraw after any window resize — prevents CTkFrame canvas
        # artifacts (black rectangles) that appear when inner corner_radius=0
        # frames are inside corner_radius>0 parents and the window is resized.
        self._resize_timer_id = None
        def _on_root_configure(event):
            if event.widget is not self.root:
                return
            if self._resize_timer_id is not None:
                self.root.after_cancel(self._resize_timer_id)
            self._resize_timer_id = self.root.after(
                80, lambda: self.root.update_idletasks())
        self.root.bind("<Configure>", _on_root_configure, add="+")

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_layout(self):
        self._nav_items: dict = {}   # name → {'row', 'accent', 'btn'}
        self._ticker_labels: dict = {}  # pair → {'price': lbl, 'pct': lbl}

        self.sidebar = ctk.CTkFrame(self, width=240, fg_color=C_PANEL, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        self.main_area = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        self.main_area.pack(side="left", fill="both", expand=True)
        self._build_topbar()
        self._build_content()

    def _build_sidebar(self):
        # ── Logo ─────────────────────────────────────────────────────────────
        logo_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo_frame.pack(fill="x", padx=20, pady=(24, 0))

        ctk.CTkLabel(logo_frame, text="⬡", font=("Segoe UI", 32),
                     text_color=C_ACCENT).pack(side="left", padx=(0, 10))

        txt_col = ctk.CTkFrame(logo_frame, fg_color="transparent")
        txt_col.pack(side="left")
        ctk.CTkLabel(txt_col, text="NEXXUS", font=("Segoe UI", 18, "bold"),
                     text_color=C_TEXT).pack(anchor="w")
        ctk.CTkLabel(txt_col, text="Trading Terminal", font=("Segoe UI", 10),
                     text_color=C_MUTED).pack(anchor="w")

        ctk.CTkFrame(self.sidebar, height=1, fg_color=C_BORDER2).pack(
            fill="x", padx=16, pady=(20, 12))

        # ── Navigation ───────────────────────────────────────────────────────
        nav = [
            ("dashboard", "◈", "Dashboard",  self._show_dashboard),
            ("charts",    "◉", "Live Charts", self._show_charts),
            ("trades",    "◫", "Trades",      self._show_trades),
            ("settings",  "◎", "Settings",    self._show_settings),
            ("logs",      "≡", "Logs",        self._show_logs),
        ]
        for name, icon, label, cmd in nav:
            self._nav_button(name, icon, label, cmd)

        # ── Status & Controls ────────────────────────────────────────────────
        ctk.CTkFrame(self.sidebar, height=1, fg_color=C_BORDER2).pack(
            fill="x", padx=16, pady=(16, 14))

        status_row = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        status_row.pack(fill="x", padx=20, pady=(0, 4))
        self.status_dot = ctk.CTkLabel(
            status_row, text="●", font=("Segoe UI", 10), text_color=C_ORANGE)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_lbl2 = ctk.CTkLabel(
            status_row, text="CONNECTING", font=("Segoe UI", 10, "bold"),
            text_color=C_ORANGE)
        self.status_lbl2.pack(side="left")

        ctk.CTkButton(
            self.sidebar, text="  ⬛  Emergency Stop", height=40, corner_radius=8,
            fg_color="#1a0a0a", hover_color="#2d1414",
            border_width=1, border_color="#3d1a1a",
            text_color=C_RED, font=("Segoe UI", 12, "bold"), anchor="w",
            command=self.emergency_stop
        ).pack(fill="x", padx=12, pady=(4, 3))

        self.resume_btn = ctk.CTkButton(
            self.sidebar, text="  ▶  Resume Trading", height=40, corner_radius=8,
            fg_color="#0a1a0a", hover_color="#152515",
            border_width=1, border_color="#1a3a1a",
            text_color=C_GREEN, font=("Segoe UI", 12, "bold"), anchor="w",
            state="disabled", command=self.resume_trading)
        self.resume_btn.pack(fill="x", padx=12, pady=3)

        # ── Version footer — glow separator ──────────────────────────────────
        ctk.CTkFrame(self.sidebar, height=1, fg_color=C_GLOW).pack(
            fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(self.sidebar, text="v2.0  ·  Coinbase Advanced Trade",
                     font=("Segoe UI", 9), text_color=C_MUTED).pack(pady=(0, 16))

    def _nav_button(self, name: str, icon: str, label: str, cmd):
        """Create a sidebar nav button with glass active-state accent indicator."""
        row = ctk.CTkFrame(self.sidebar, fg_color="transparent", corner_radius=9)
        row.pack(fill="x", padx=10, pady=2)
        row.pack_propagate(False)
        row.configure(height=44)

        # Shadow frame behind accent bar (glow effect)
        shadow = ctk.CTkFrame(row, width=8, fg_color="transparent", corner_radius=2)
        shadow.pack(side="left", fill="y", padx=(2, 0), pady=6)
        shadow.pack_propagate(False)

        # Accent bar (4px wide, glows with C_ACCENT2 when active)
        accent = ctk.CTkFrame(row, width=4, fg_color="transparent", corner_radius=2)
        accent.pack(side="left", fill="y", pady=6)
        accent.pack_propagate(False)

        btn = ctk.CTkButton(
            row, text=f" {icon}   {label}", height=44, corner_radius=8,
            fg_color="transparent", hover_color=C_CARD2,
            font=("Segoe UI", 13), text_color=C_TEXT2, anchor="w",
            command=cmd
        )
        btn.pack(side="left", fill="both", expand=True, padx=(2, 4))
        self._nav_items[name] = {'row': row, 'accent': accent, 'shadow': shadow, 'btn': btn}

    def _set_active_nav(self, name: str):
        for n, refs in self._nav_items.items():
            active = (n == name)
            refs['shadow'].configure(fg_color=C_GLOW if active else "transparent")
            refs['accent'].configure(fg_color=C_ACCENT2 if active else "transparent")
            if active:
                refs['row'].configure(
                    fg_color=C_NAV_ACT, border_width=1, border_color=C_BORDER2)
            else:
                refs['row'].configure(
                    fg_color="transparent", border_width=0)
            refs['btn'].configure(
                text_color=C_TEXT if active else C_TEXT2,
                font=("Segoe UI", 13, "bold") if active else ("Segoe UI", 13),
            )

    def _build_topbar(self):
        top = ctk.CTkFrame(self.main_area, height=62, fg_color=C_PANEL, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)

        # Top highlight strip — simulates reflected light on glass surface
        ctk.CTkFrame(top, height=1, fg_color=C_HL, corner_radius=0).pack(
            side="top", fill="x")

        # Bottom border line
        ctk.CTkFrame(top, height=1, fg_color=C_BORDER2).pack(side="bottom", fill="x")

        # Left: page title with accent left bar
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="y", padx=(0, 0))

        ctk.CTkFrame(left, width=3, fg_color=C_ACCENT2, corner_radius=0).pack(
            side="left", fill="y", pady=14)

        self.page_title = ctk.CTkLabel(
            left, text="Dashboard",
            font=("Segoe UI", 17, "bold"), text_color=C_TEXT)
        self.page_title.pack(side="left", padx=(14, 0))

        # Centre: live price tickers — glass pills
        tickers = ctk.CTkFrame(top, fg_color="transparent")
        tickers.pack(side="left", fill="y", padx=30)

        for pair in TRADING_PAIRS:
            tf = ctk.CTkFrame(tickers, fg_color=C_GLASS, corner_radius=8,
                              border_width=2, border_color=C_BORDER2)
            tf.pack(side="left", padx=6, pady=12)
            ctk.CTkLabel(tf, text=pair.replace("-", "/"),
                         font=("Segoe UI", 9, "bold"), text_color=C_MUTED).pack(
                side="left", padx=(10, 4))
            price_l = ctk.CTkLabel(tf, text="—", font=("Segoe UI", 11, "bold"),
                                   text_color=C_TEXT)
            price_l.pack(side="left", padx=(0, 4))
            pct_l = ctk.CTkLabel(tf, text="", font=("Segoe UI", 10),
                                 text_color=C_MUTED)
            pct_l.pack(side="left", padx=(0, 10))
            self._ticker_labels[pair] = {'price': price_l, 'pct': pct_l, 'frame': tf}

        # Right: portfolio + P&L — glass badge background
        right_bg = ctk.CTkFrame(top, fg_color=C_GLASS, corner_radius=10,
                                border_width=1, border_color=C_BORDER2)
        right_bg.pack(side="right", fill="y", padx=20, pady=10)
        right = ctk.CTkFrame(right_bg, fg_color="transparent")
        right.pack(fill="both", expand=True, padx=12, pady=4)

        self.bal_label = ctk.CTkLabel(right, text="Portfolio  —",
                                       font=("Segoe UI", 11), text_color=C_MUTED)
        self.bal_label.pack(anchor="e")
        self.pnl_label = ctk.CTkLabel(right, text="P&L  —",
                                       font=("Segoe UI", 12, "bold"), text_color=C_TEXT)
        self.pnl_label.pack(anchor="e")
        self.alloc_label = ctk.CTkLabel(right, text="",
                                         font=("Segoe UI", 10), text_color=C_ACCENT2)
        self.alloc_label.pack(anchor="e", pady=(1, 0))

    def _build_content(self):
        self.content = ctk.CTkFrame(self.main_area, fg_color=C_BG, corner_radius=0)
        self.content.pack(fill="both", expand=True)
        self.pages = {
            'dashboard': self._make_dashboard_page(),
            'charts':    self._make_charts_page(),
            'trades':    self._make_trades_page(),
            'settings':  self._make_settings_page(),
            'logs':      self._make_logs_page(),
        }
        self._show_dashboard()

    def _glass_card(self, parent, accent=None, corner=16, **kw):
        """A glass-effect panel: dark bg + tinted border + top highlight strip."""
        import tkinter as _tk
        if accent is None:
            accent = C_ACCENT2
        card = ctk.CTkFrame(parent, fg_color=C_GLASS, corner_radius=corner,
                            border_width=1, border_color=C_BORDER2, **kw)
        # Top highlight — tk.Frame avoids CTk canvas resize artifacts
        _tk.Frame(card, height=1, bg=C_HL, bd=0, highlightthickness=0).pack(
            fill="x")
        return card

    # ── Dashboard page ────────────────────────────────────────────────────────
    def _make_dashboard_page(self):
        page = ctk.CTkFrame(self.content, fg_color=C_BG, corner_radius=0)

        # ── Row 1: Metric cards ──────────────────────────────────────────────
        mrow = ctk.CTkFrame(page, fg_color="transparent")
        mrow.pack(fill="x", padx=20, pady=(20, 16))
        self.metric_cards = {}
        specs = [
            ("Portfolio Value", "usd_bal",  C_ACCENT3,  "💰"),
            ("Bot Balance",     "bot_bal",  C_ACCENT2,  "🤖"),
            ("Coin Holdings",   "exposure", C_GREEN,    "📦"),
            ("Overall P&L",     "pnl",      C_GREEN,    "📈"),
        ]
        for i, (title, key, color, icon) in enumerate(specs):
            c = self._metric_card(mrow, title, "—", color, icon)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            self.metric_cards[key] = c
        mrow.columnconfigure(list(range(4)), weight=1)

        # ── Row 2: Pair cards ────────────────────────────────────────────────
        prow = ctk.CTkFrame(page, fg_color="transparent")
        prow.pack(fill="x", padx=20, pady=(0, 0))
        self.pair_cards = {}
        for i, pair in enumerate(TRADING_PAIRS):
            pc = self._pair_card(prow, pair)
            pc.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 12, 0))
            self.pair_cards[pair] = pc
        prow.columnconfigure(list(range(len(TRADING_PAIRS))), weight=1)

        # ── Row 3: Bot status  +  Quick actions ─────────────────────────────
        r3 = ctk.CTkFrame(page, fg_color="transparent")
        r3.pack(fill="x", padx=20, pady=(14, 0))
        r3.columnconfigure(0, weight=1)
        r3.columnconfigure(1, weight=1)

        # Bot status card
        bsc = ctk.CTkFrame(r3, fg_color=C_CARD, corner_radius=14,
                           border_width=1, border_color=C_BORDER)
        bsc.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        bs_hdr = ctk.CTkFrame(bsc, fg_color="transparent")
        bs_hdr.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(bs_hdr, text="⚙  BOT STATUS", font=("Segoe UI", 9, "bold"),
                     text_color=C_MUTED).pack(side="left")
        ctk.CTkButton(
            bs_hdr, text="Copy", width=52, height=22, corner_radius=6,
            fg_color=C_CARD2, hover_color=C_BORDER2, text_color=C_TEXT2,
            font=("Segoe UI", 9),
            command=self._copy_bot_status
        ).pack(side="right")

        bs_grid = ctk.CTkFrame(bsc, fg_color="transparent")
        bs_grid.pack(fill="x", padx=16, pady=(0, 14))
        def _bs_row(label, valtext, col=C_TEXT):
            r = ctk.CTkFrame(bs_grid, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=label, width=130, anchor="w",
                         font=("Segoe UI", 11), text_color=C_TEXT2).pack(side="left")
            lbl = ctk.CTkLabel(r, text=valtext, font=("Segoe UI", 11, "bold"),
                               text_color=col)
            lbl.pack(side="left")
            return lbl
        self.bs_state_lbl  = _bs_row("State",        "Connecting…", C_ORANGE)
        self.bs_mode_lbl   = _bs_row("Mode",         "—",           C_MUTED)
        self.bs_signal_lbl = _bs_row("Signal TF",    self.signal_tf, C_ACCENT)
        self.bs_next_lbl   = _bs_row("Next Window",  "—",           C_MUTED)
        self.bs_feed_lbl   = _bs_row("Price Feed",   "—",           C_MUTED)
        self.bs_trades_lbl = _bs_row("Active Trades", "0",           C_TEXT)
        self.bs_alloc_lbl  = _bs_row("Allocated",    "$0.00",        C_ACCENT2)

        # Per-pair allocation breakdown (compact sub-rows)
        self._bs_pair_alloc_rows = {}
        for pair in TRADING_PAIRS:
            coin = pair.split('-')[0]
            r = ctk.CTkFrame(bs_grid, fg_color="transparent")
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(r, text=f"  └ {coin}", width=130, anchor="w",
                         font=("Segoe UI", 10), text_color=C_MUTED).pack(side="left")
            lbl = ctk.CTkLabel(r, text="$0.00", font=("Segoe UI", 10, "bold"),
                               text_color=C_MUTED)
            lbl.pack(side="left")
            self._bs_pair_alloc_rows[pair] = (r, lbl)

        # ── Last Executed Signal (hidden until a real fill happens) ───────────
        import tkinter as _tk; _tk.Frame(bsc, height=1, bg=C_BORDER, bd=0, highlightthickness=0).pack(
            fill="x", padx=16, pady=(6, 6))
        self._last_sig_frame = ctk.CTkFrame(bsc, fg_color="transparent")
        # Only packed when last_executed_signal is set
        ctk.CTkLabel(self._last_sig_frame, text="LAST EXECUTED SIGNAL",
                     font=("Segoe UI", 9, "bold"), text_color=C_MUTED).pack(
            anchor="w", padx=16, pady=(0, 4))
        sig_inner = ctk.CTkFrame(self._last_sig_frame, fg_color=C_CARD2,
                                  corner_radius=10, border_width=1, border_color=C_BORDER2)
        sig_inner.pack(fill="x", padx=16, pady=(0, 10))

        sig_top = ctk.CTkFrame(sig_inner, fg_color="transparent")
        sig_top.pack(fill="x", padx=14, pady=(10, 4))
        self._bs_sig_side_lbl = ctk.CTkLabel(sig_top, text="BUY",
                                              font=("Segoe UI", 15, "bold"),
                                              text_color=C_GREEN)
        self._bs_sig_side_lbl.pack(side="left", padx=(0, 8))
        self._bs_sig_pair_lbl = ctk.CTkLabel(sig_top, text="BTC-USD",
                                              font=("Segoe UI", 13, "bold"),
                                              text_color=C_TEXT)
        self._bs_sig_pair_lbl.pack(side="left")
        self._bs_sig_src_lbl = ctk.CTkLabel(sig_top, text="MA2/MA5",
                                             font=("Segoe UI", 10),
                                             text_color=C_MUTED)
        self._bs_sig_src_lbl.pack(side="right")

        sig_bot = ctk.CTkFrame(sig_inner, fg_color="transparent")
        sig_bot.pack(fill="x", padx=14, pady=(0, 10))
        self._bs_sig_price_lbl = ctk.CTkLabel(sig_bot, text="",
                                               font=("Segoe UI", 12, "bold"),
                                               text_color=C_ACCENT3)
        self._bs_sig_price_lbl.pack(side="left")
        self._bs_sig_spent_lbl = ctk.CTkLabel(sig_bot, text="",
                                               font=("Segoe UI", 11),
                                               text_color=C_TEXT2)
        self._bs_sig_spent_lbl.pack(side="left", padx=(10, 0))
        self._bs_sig_time_lbl  = ctk.CTkLabel(sig_bot, text="",
                                               font=("Segoe UI", 10),
                                               text_color=C_MUTED)
        self._bs_sig_time_lbl.pack(side="right")

        # Quick actions card
        acts = ctk.CTkFrame(r3, fg_color=C_CARD, corner_radius=14,
                            border_width=1, border_color=C_BORDER)
        acts.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(acts, text="⚡  QUICK ACTIONS", font=("Segoe UI", 9, "bold"),
                     text_color=C_MUTED).pack(anchor="w", padx=16, pady=(14, 10))

        qa_grid = ctk.CTkFrame(acts, fg_color="transparent")
        qa_grid.pack(fill="x", padx=16, pady=(0, 14))
        qa_grid.columnconfigure((0, 1), weight=1)

        ctk.CTkButton(qa_grid, text="＋  Allocate to Bot",
                      height=40, corner_radius=9,
                      fg_color=C_ACCENT2, hover_color="#5d47bb",
                      font=("Segoe UI", 12, "bold"),
                      command=self._open_allocate
                      ).grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=(0, 5))
        ctk.CTkButton(qa_grid, text="−  Unallocate",
                      height=40, corner_radius=9,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER2, font=("Segoe UI", 12),
                      command=self._open_unallocate
                      ).grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=(0, 5))
        ctk.CTkButton(qa_grid, text="📈  View Charts",
                      height=40, corner_radius=9,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER2, font=("Segoe UI", 12),
                      command=self._show_charts
                      ).grid(row=1, column=0, sticky="ew", padx=(0, 5), pady=(5, 0))
        ctk.CTkButton(qa_grid, text="🛑  Sell All → USD",
                      height=40, corner_radius=9,
                      fg_color="#1a0a0a", hover_color="#2d1414",
                      border_width=1, border_color="#3d1a1a",
                      text_color=C_RED, font=("Segoe UI", 12),
                      command=self._confirm_sell_all
                      ).grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=(5, 0))

        # ── Row 4: Activity feed ─────────────────────────────────────────────
        af = ctk.CTkFrame(page, fg_color=C_GLASS, corner_radius=14,
                          border_width=1, border_color=C_BORDER2)
        af.pack(fill="both", expand=True, padx=20, pady=(14, 20))

        af_hdr = ctk.CTkFrame(af, fg_color="transparent")
        af_hdr.pack(fill="x", padx=16, pady=(12, 0))
        # Pulsing dot + accent header
        ctk.CTkLabel(af_hdr, text="●", font=("Segoe UI", 9),
                     text_color=C_ACCENT).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(af_hdr, text="ACTIVITY FEED", font=("Segoe UI", 9, "bold"),
                     text_color=C_ACCENT).pack(side="left")
        import tkinter as _tk; _tk.Frame(af, height=1, bg=C_BORDER2, bd=0, highlightthickness=0).pack(
            fill="x", padx=0, pady=(8, 0))

        self.activity_box = ctk.CTkTextbox(
            af, fg_color="transparent", text_color=C_TEXT,
            font=("Courier New", 11), state="disabled")
        self.activity_box.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        return page

    def _metric_card(self, parent, title, value, color, icon=""):
        card = ctk.CTkFrame(parent, fg_color=C_GLASS, corner_radius=16,
                            border_width=1, border_color=C_BORDER2)
        # Top highlight strip — use tk.Frame (no CTk canvas) to avoid resize artifacts
        import tkinter as _tk
        _tk.Frame(card, height=1, bg=C_HL, bd=0, highlightthickness=0).pack(
            fill="x")
        # Colored accent bar along top (2px, full width)
        _tk.Frame(card, height=2, bg=color, bd=0, highlightthickness=0).pack(
            fill="x")
        # Title row
        th = ctk.CTkFrame(card, fg_color="transparent")
        th.pack(fill="x", padx=16, pady=(10, 2))
        if icon:
            ctk.CTkLabel(th, text=icon, font=("Segoe UI", 11),
                         text_color=color).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(th, text=title.upper(), font=("Segoe UI", 9, "bold"),
                     text_color=C_TEXT2).pack(side="left")
        # Value
        vl = ctk.CTkLabel(card, text=value, font=("Segoe UI", 22, "bold"),
                          text_color=C_TEXT)
        vl.pack(anchor="w", padx=16, pady=(2, 2))
        # Sub-line (P&L change, breakdown, etc.)
        sl = ctk.CTkLabel(card, text="", font=("Segoe UI", 10), text_color=C_MUTED)
        sl.pack(anchor="w", padx=16, pady=(0, 12))
        card._val = vl
        card._sub = sl
        return card

    def _pair_card(self, parent, pair):
        import tkinter as _tk
        coin = pair.split("-")[0]
        card = ctk.CTkFrame(parent, fg_color=C_GLASS, corner_radius=14,
                            border_width=1, border_color=C_BORDER2)
        # Top highlight strip — tk.Frame avoids CTk canvas resize artifacts
        _tk.Frame(card, height=1, bg=C_HL, bd=0, highlightthickness=0).pack(
            fill="x")
        # Header: pair name + change badge
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(hdr, text=f"{coin} / USD", font=("Segoe UI", 11, "bold"),
                     text_color=C_TEXT2).pack(side="left")
        badge_bg = ctk.CTkFrame(hdr, fg_color=C_GLOW, corner_radius=7,
                                border_width=1, border_color=C_BORDER2)
        badge_bg.pack(side="right")
        pct_lbl = ctk.CTkLabel(badge_bg, text="  —  ",
                               font=("Segoe UI", 10, "bold"), text_color=C_MUTED)
        pct_lbl.pack(padx=2, pady=2)
        # Price (large)
        price_lbl = ctk.CTkLabel(card, text="—", font=("Segoe UI", 21, "bold"),
                                 text_color=C_TEXT)
        price_lbl.pack(anchor="w", padx=14, pady=(0, 6))
        # H/L row
        hl = ctk.CTkFrame(card, fg_color="transparent")
        hl.pack(fill="x", padx=14, pady=(0, 14))
        high_lbl = ctk.CTkLabel(hl, text="H  —", font=("Segoe UI", 10), text_color=C_MUTED)
        high_lbl.pack(side="left", padx=(0, 12))
        low_lbl = ctk.CTkLabel(hl, text="L  —", font=("Segoe UI", 10), text_color=C_MUTED)
        low_lbl.pack(side="left")
        card._price     = price_lbl
        card._pct       = pct_lbl
        card._pct_badge = badge_bg
        card._high      = high_lbl
        card._low       = low_lbl
        return card

    # ── Charts page ───────────────────────────────────────────────────────────
    def _make_charts_page(self):
        page = ctk.CTkFrame(self.content, fg_color=C_CHART_BG, corner_radius=0)

        self.chart_pair_var = ctk.StringVar(value=TRADING_PAIRS[0])
        self.chart_tf_var   = ctk.StringVar(value="5m")

        # ── Single compact toolbar (everything in one 52px strip) ────────────
        tb = ctk.CTkFrame(page, fg_color=C_GLASS, corner_radius=0, height=52)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        ctk.CTkFrame(tb, height=1, fg_color=C_HL, corner_radius=0).pack(side="top", fill="x")
        ctk.CTkFrame(tb, height=1, fg_color=C_BORDER2).pack(side="bottom", fill="x")

        row = ctk.CTkFrame(tb, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=10)

        # Pair selector — trading pairs + watchlist in a dropdown + segmented buttons
        _all_chart_pairs = TRADING_PAIRS + WATCHLIST_PAIRS
        ctk.CTkSegmentedButton(
            row, values=TRADING_PAIRS, variable=self.chart_pair_var,
            command=self._on_chart_pair_change, height=32
        ).pack(side="left", padx=(0, 4), pady=10)
        # Watchlist dropdown — more coins without crowding the toolbar
        _wl_var = ctk.StringVar(value="More ▾")
        _wl_menu = ctk.CTkOptionMenu(
            row, variable=_wl_var,
            values=WATCHLIST_PAIRS,
            width=100, height=32,
            fg_color=C_CARD, button_color=C_CARD2,
            text_color=C_TEXT2, font=("Segoe UI", 11),
            command=lambda p: (self.chart_pair_var.set(p),
                               _wl_var.set("More ▾"),
                               self._on_chart_pair_change(p))
        )
        _wl_menu.pack(side="left", padx=(0, 0), pady=10)

        ctk.CTkFrame(row, width=1, fg_color=C_BORDER2).pack(
            side="left", fill="y", padx=10, pady=10)

        # TF selector
        ctk.CTkSegmentedButton(
            row, values=["1m", "5m", "1h", "1d"],
            variable=self.chart_tf_var, command=self._refresh_chart, height=32
        ).pack(side="left", pady=10)

        ctk.CTkFrame(row, width=1, fg_color=C_BORDER2).pack(
            side="left", fill="y", padx=10, pady=10)

        # Live price inline
        self.chart_hdr_pair_lbl = ctk.CTkLabel(
            row, text="BTC/USD", font=("Segoe UI", 10, "bold"), text_color=C_TEXT2)
        self.chart_hdr_pair_lbl.pack(side="left", padx=(0, 6))

        self.chart_hdr_price_lbl = ctk.CTkLabel(
            row, text="—", font=("Segoe UI", 13, "bold"), text_color=C_TEXT)
        self.chart_hdr_price_lbl.pack(side="left", padx=(0, 6))

        self.chart_hdr_change_lbl = ctk.CTkLabel(
            row, text="", font=("Segoe UI", 11), text_color=C_MUTED)
        self.chart_hdr_change_lbl.pack(side="left", padx=(0, 6))

        self.chart_hdr_stats_lbl = ctk.CTkLabel(
            row, text="", font=("Segoe UI", 10), text_color=C_MUTED)
        self.chart_hdr_stats_lbl.pack(side="left")

        # Right side: allocation readout + buttons + refresh
        ctk.CTkButton(
            row, text="⟳", width=32, height=32, corner_radius=7,
            fg_color=C_CARD2, hover_color=C_BORDER2, font=("Segoe UI", 13),
            command=self._refresh_chart
        ).pack(side="right", padx=(4, 0), pady=10)

        ctk.CTkButton(
            row, text="Order Book", width=90, height=32, corner_radius=7,
            fg_color=C_CARD2, hover_color=C_BORDER2, font=("Segoe UI", 10),
            command=self._open_orderbook
        ).pack(side="right", padx=(0, 3), pady=10)

        ctk.CTkButton(
            row, text="−", width=32, height=32, corner_radius=7,
            fg_color=C_CARD2, hover_color=C_BORDER2,
            border_width=1, border_color=C_BORDER2, font=("Segoe UI", 13),
            command=lambda: self._open_unallocate(self.chart_pair_var.get())
        ).pack(side="right", padx=(3, 0), pady=10)

        ctk.CTkButton(
            row, text="＋", width=32, height=32, corner_radius=7,
            fg_color=C_ACCENT2, hover_color="#5d47bb", font=("Segoe UI", 13, "bold"),
            command=lambda: self._open_allocate(self.chart_pair_var.get())
        ).pack(side="right", padx=(3, 0), pady=10)

        ctk.CTkFrame(row, width=1, fg_color=C_BORDER2).pack(
            side="right", fill="y", padx=8, pady=10)

        self.chart_bot_lbl = ctk.CTkLabel(
            row, text="Bot —", font=("Segoe UI", 10, "bold"), text_color=C_ACCENT2)
        self.chart_bot_lbl.pack(side="right", padx=(0, 4))

        self.chart_liquid_lbl = ctk.CTkLabel(
            row, text="Liquid —", font=("Segoe UI", 10), text_color=C_MUTED)
        self.chart_liquid_lbl.pack(side="right", padx=(0, 2))

        ctk.CTkFrame(row, width=1, fg_color=C_BORDER2).pack(
            side="right", fill="y", padx=8, pady=10)

        # ── Chart canvas fills all remaining space ────────────────────────────
        cf = ctk.CTkFrame(page, fg_color=C_CHART_BG, corner_radius=0)
        cf.pack(fill="both", expand=True)

        self.chart_fig = plt.figure(figsize=(13, 7))
        self.chart_fig.patch.set_facecolor(C_CHART_BG)
        # width_ratios: chart gets ~84%, key gets ~16%; wspace controls the gap
        gs = self.chart_fig.add_gridspec(1, 2, width_ratios=[5.5, 1], wspace=0.06)
        self.chart_ax  = self.chart_fig.add_subplot(gs[0])
        self.key_ax    = self.chart_fig.add_subplot(gs[1])
        self.chart_ax.set_facecolor(C_CHART_BG)
        self.key_ax.set_facecolor(C_PANEL)
        self.key_ax.axis('off')
        self.chart_ax.text(0.5, 0.5, "Loading chart data…",
                            ha='center', va='center',
                            transform=self.chart_ax.transAxes,
                            color=C_MUTED, fontsize=13)
        self.chart_canvas = FigureCanvasTkAgg(self.chart_fig, master=cf)
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.chart_canvas.mpl_connect('motion_notify_event',  self._on_chart_hover)
        self.chart_canvas.mpl_connect('scroll_event',         self._on_chart_scroll)
        self.chart_canvas.mpl_connect('button_press_event',   self._on_chart_press)
        self.chart_canvas.mpl_connect('button_release_event', self._on_chart_release)
        self.chart_canvas.mpl_connect('motion_notify_event',  self._on_chart_drag)
        self.chart_canvas.mpl_connect('button_press_event',   self._chart_dbl_click)
        self._signal_data   = []
        self._hover_ann     = None
        self._hover_last_t  = 0.0    # monotonic time of last hover redraw
        self._hover_active  = False  # True when a tooltip is currently shown
        self._pan_start     = None   # pixel coords on button-press
        self._pan_xlim      = None   # saved xlim at press start
        self._pan_ylim      = None   # saved ylim at press start
        self._drag_last_draw = 0.0  # monotonic time of last drag redraw
        self._zoom_locked   = False  # True once user has panned/zoomed
        return page

    # ── Trades page ───────────────────────────────────────────────────────────
    def _make_trades_page(self):
        page = ctk.CTkFrame(self.content, fg_color=C_BG, corner_radius=0)

        # Header
        th = ctk.CTkFrame(page, fg_color="transparent")
        th.pack(fill="x", padx=20, pady=(20, 10))
        ctk.CTkLabel(th, text="ACTIVE POSITIONS", font=("Segoe UI", 10, "bold"),
                     text_color=C_MUTED).pack(side="left")
        ctk.CTkButton(
            th, text="Copy All", width=70, height=26, corner_radius=7,
            fg_color=C_CARD2, hover_color=C_BORDER2, text_color=C_TEXT2,
            font=("Segoe UI", 9),
            command=self._copy_trades
        ).pack(side="right")

        table = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=14,
                             border_width=1, border_color=C_BORDER)
        table.pack(fill="x", padx=20)

        hdr = ctk.CTkFrame(table, fg_color=C_CARD2, corner_radius=10)
        hdr.pack(fill="x", padx=8, pady=(8, 2))
        for col in ["Pair", "Side", "Qty", "Entry", "Current", "P&L", "SL", "TP"]:
            ctk.CTkLabel(hdr, text=col, width=100,
                         font=("Segoe UI", 10, "bold"), text_color=C_MUTED).pack(
                side="left", padx=4, pady=10)

        self.trade_scroll = ctk.CTkScrollableFrame(
            table, fg_color="transparent", height=140)
        self.trade_scroll.pack(fill="x", padx=8, pady=(0, 8))
        self._trade_rows: dict = {}

        # History
        hh = ctk.CTkFrame(page, fg_color="transparent")
        hh.pack(fill="x", padx=20, pady=(18, 8))
        ctk.CTkLabel(hh, text="CLOSED TRADE HISTORY", font=("Segoe UI", 10, "bold"),
                     text_color=C_MUTED).pack(side="left")

        hf = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=14,
                          border_width=1, border_color=C_BORDER)
        hf.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.history_box = ctk.CTkTextbox(
            hf, fg_color="transparent", text_color=C_TEXT,
            font=("Courier New", 11), state="disabled")
        self.history_box.pack(fill="both", expand=True, padx=12, pady=12)
        return page

    # ── Settings page ─────────────────────────────────────────────────────────
    def _make_settings_page(self):
        page = ctk.CTkFrame(self.content, fg_color=C_BG, corner_radius=0)
        scroll = ctk.CTkScrollableFrame(page, fg_color=C_BG)
        scroll.pack(fill="both", expand=True, padx=20, pady=20)

        # Bind mouse wheel to scroll — persistent root-level bind that fires only
        # when the settings page is currently visible (winfo_ismapped).
        _canvas = scroll._parent_canvas
        def _settings_scroll(event):
            try:
                if not page.winfo_ismapped():
                    return
                delta = int(-1 * (event.delta / 120)) if event.delta else (
                    1 if event.num == 5 else -1)
                _canvas.yview_scroll(delta, "units")
            except Exception:
                pass
        self.root.bind("<MouseWheel>", _settings_scroll, add="+")
        self.root.bind("<Button-4>",   _settings_scroll, add="+")
        self.root.bind("<Button-5>",   _settings_scroll, add="+")

        def section(title, link=None):
            f = ctk.CTkFrame(scroll, fg_color=C_PANEL, corner_radius=12,
                             border_width=1, border_color=C_BORDER)
            f.pack(fill="x", pady=(0, 14))
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(14, 4))
            ctk.CTkLabel(row, text=title, font=("Segoe UI", 13, "bold"),
                         text_color=C_ACCENT).pack(side="left")
            if link:
                ctk.CTkLabel(row, text=f"  ↗ {link}",
                             font=("Segoe UI", 10), text_color=C_MUTED).pack(side="left")
            return f

        def entry_row(parent, label, var, unit=""):
            r = ctk.CTkFrame(parent, fg_color="transparent")
            r.pack(fill="x", padx=20, pady=(0, 10))
            ctk.CTkLabel(r, text=label, width=240, anchor="w",
                         font=("Segoe UI", 12), text_color=C_TEXT).pack(side="left")
            e = ctk.CTkEntry(r, textvariable=var, width=100, height=34,
                             fg_color=C_CARD, border_color=C_BORDER, text_color=C_TEXT)
            e.pack(side="left")
            if unit:
                ctk.CTkLabel(r, text=f"  {unit}", font=("Segoe UI", 11),
                             text_color=C_MUTED).pack(side="left")
            return e

        # ── Appearance / Theme ────────────────────────────────────────────────
        def _save_theme(t):
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE) as f: cfg = json.load(f)
                except Exception: pass
            cfg["theme"] = t
            with open(CONFIG_FILE, "w") as f: json.dump(cfg, f, indent=2)

        app_sec = section("Appearance  /  Theme")
        theme_row = ctk.CTkFrame(app_sec, fg_color="transparent")
        theme_row.pack(fill="x", padx=20, pady=(0, 14))
        ctk.CTkLabel(theme_row, text="Color Theme", width=240, anchor="w",
                     font=("Segoe UI", 12), text_color=C_TEXT).pack(side="left")
        _theme_var = ctk.StringVar(value=_ACTIVE_THEME)
        _theme_menu = ctk.CTkOptionMenu(
            theme_row, values=list(THEMES.keys()),
            variable=_theme_var, width=160, height=34,
            fg_color=C_CARD, button_color=C_CARD2,
            text_color=C_TEXT, font=("Segoe UI", 12),
            command=lambda t: _save_theme(t)
        )
        _theme_menu.pack(side="left")
        ctk.CTkLabel(theme_row, text="  (restarts to apply)",
                     font=("Segoe UI", 10), text_color=C_MUTED).pack(side="left")

        self.sl_var  = ctk.StringVar(value=str(round(STOP_LOSS_PCT * 100, 2)))
        self.tp_var  = ctk.StringVar(value=str(round(TAKE_PROFIT_PCT * 100, 2)))
        self.ord_var = ctk.StringVar(value=str(ORDER_AMOUNT_USD))
        self.res_var = ctk.StringVar(value=str(MINIMUM_RESERVE))
        self.cd_var  = ctk.StringVar(value=str(COOLDOWN_SECONDS))
        self.round_var = ctk.StringVar(value=str(self.alloc_round_tokens))

        risk = section("Risk Management",
                       "docs.cdp.coinbase.com/advanced-trade/reference/create_order")
        entry_row(risk, "Stop Loss",           self.sl_var,  "%")
        entry_row(risk, "Take Profit",         self.tp_var,  "%")
        entry_row(risk, "Order Size",          self.ord_var, "USD")
        entry_row(risk, "Minimum Bot Reserve", self.res_var, "USD")
        entry_row(risk, "Cooldown Between Trades", self.cd_var, "seconds")
        entry_row(risk, "Coin Alloc Rounding", self.round_var, "tokens")

        # ── Signal timeframe selector ─────────────────────────────────────────
        tf_row = ctk.CTkFrame(risk, fg_color="transparent")
        tf_row.pack(fill="x", padx=20, pady=(0, 12))
        ctk.CTkLabel(tf_row, text="Signal Timeframe", width=240, anchor="w",
                     font=("Segoe UI", 12), text_color=C_TEXT).pack(side="left")
        self.signal_tf_var = ctk.StringVar(value=self.signal_tf)
        ctk.CTkSegmentedButton(
            tf_row, values=["1m", "5m", "1h", "1d"],
            variable=self.signal_tf_var,
            width=220, height=34,
        ).pack(side="left")

        ctk.CTkLabel(
            risk,
            text="  Signals fire on completed candles of this timeframe.\n"
                 "  1m = fastest, most noise  ·  5m = balanced  ·  1h/1d = slower, higher conviction",
            font=("Segoe UI", 10), text_color=C_MUTED, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        # ── Signal direction ──────────────────────────────────────────────────
        dir_row = ctk.CTkFrame(risk, fg_color="transparent")
        dir_row.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(dir_row, text="Signal Direction", width=240, anchor="w",
                     font=("Segoe UI", 12), text_color=C_TEXT).pack(side="left")
        self.signal_dir_var = ctk.StringVar(value=self.signal_direction)
        ctk.CTkSegmentedButton(
            dir_row, values=["Both", "Buy Only", "Sell Only"],
            variable=self.signal_dir_var,
            width=260, height=34,
        ).pack(side="left")

        ctk.CTkLabel(
            risk,
            text="  Both = act on every signal  ·  Buy Only / Sell Only = ignore the other side",
            font=("Segoe UI", 10), text_color=C_MUTED, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        # ── Auto-Compound ─────────────────────────────────────────────────────
        ac_row = ctk.CTkFrame(risk, fg_color="transparent")
        ac_row.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkLabel(ac_row, text="Auto-Compound Profits", width=240, anchor="w",
                     font=("Segoe UI", 12), text_color=C_TEXT).pack(side="left")
        self.ac_enabled_var = ctk.BooleanVar(value=self.auto_compound_enabled)
        ctk.CTkSwitch(ac_row, text="", variable=self.ac_enabled_var,
                      width=48, height=24,
                      fg_color=C_CARD2, progress_color=C_GREEN).pack(side="left")

        self.ac_pct_var = ctk.StringVar(value=str(self.auto_compound_pct))
        self.ac_cap_var = ctk.StringVar(value=str(self.auto_compound_cap))
        entry_row(risk, "  Order Size (% of available funds)", self.ac_pct_var, "%")
        entry_row(risk, "  Max Order Cap",                     self.ac_cap_var, "USD")
        ctk.CTkLabel(
            risk,
            text="  When ON, order size = avail_funds × pct% (capped at max).\n"
                 "  Profits automatically scale trade sizes up to the cap.",
            font=("Segoe UI", 10), text_color=C_MUTED, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        ctk.CTkButton(risk, text="Save Settings", width=140, height=36, corner_radius=9,
                      fg_color=C_ACCENT2, hover_color="#5d47bb",
                      font=("Segoe UI", 12, "bold"),
                      command=self._save_settings).pack(anchor="w", padx=20, pady=(0, 14))

        # ── Moving Averages ───────────────────────────────────────────────────
        ma_sec = section("Moving Averages")
        ctk.CTkLabel(
            ma_sec,
            text="Enter up to 3 periods separated by commas.  "
                 "The two shortest are used for crossover signals.",
            font=("Segoe UI", 11), text_color=C_TEXT2, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        ma_row = ctk.CTkFrame(ma_sec, fg_color="transparent")
        ma_row.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkLabel(ma_row, text="Periods", width=120, anchor="w",
                     font=("Segoe UI", 12), text_color=C_TEXT).pack(side="left")
        self.ma_periods_var = ctk.StringVar(
            value=", ".join(str(p) for p in self.custom_ma_periods))
        ctk.CTkEntry(ma_row, textvariable=self.ma_periods_var,
                     width=180, height=34, fg_color=C_CARD,
                     border_color=C_BORDER, text_color=C_TEXT,
                     placeholder_text="e.g. 2, 5, 14").pack(side="left")

        ctk.CTkLabel(
            ma_sec,
            text="  Examples:  2, 5, 14 (1h)  ·  3, 5, 14 (1h)  ·  9, 20, 50  ·  7, 14, 28  ·  50, 200",
            font=("Segoe UI", 10), text_color=C_MUTED,
        ).pack(anchor="w", padx=20, pady=(0, 4))

        ctk.CTkButton(ma_sec, text="Apply MAs", width=120, height=34, corner_radius=9,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER2,
                      font=("Segoe UI", 12, "bold"), text_color=C_TEXT,
                      command=self._apply_ma_settings).pack(anchor="w", padx=20, pady=(0, 14))

        # ── Swap on Sell ──────────────────────────────────────────────────────
        swap_sec = section("Swap on Sell")
        ctk.CTkLabel(
            swap_sec,
            text="When a sell signal fires (or SL/TP is hit), automatically use the\n"
                 "proceeds to buy another asset instead of holding USD.",
            font=("Segoe UI", 11), text_color=C_TEXT2, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 12))

        self.swap_vars = {}
        # All possible targets (we exclude each pair's own coin per-row)
        _all_targets = ["USD", "USDC", "USDT"] + [p.split('-')[0] for p in TRADING_PAIRS]

        for pair in TRADING_PAIRS:
            coin = pair.split('-')[0]
            # Offer every option except the coin itself
            opts = [t for t in _all_targets if t != coin]
            current = self.swap_targets.get(pair, 'USDC')
            var = ctk.StringVar(value=current if current else 'USDC')
            self.swap_vars[pair] = var

            r = ctk.CTkFrame(swap_sec, fg_color="transparent")
            r.pack(fill="x", padx=20, pady=(0, 10))
            ctk.CTkLabel(r, text=f"{coin} / USD  →",
                         width=110, anchor="w",
                         font=("Segoe UI", 12, "bold"), text_color=C_TEXT).pack(side="left")
            ctk.CTkSegmentedButton(
                r, values=opts, variable=var, height=34
            ).pack(side="left")

        ctk.CTkLabel(
            swap_sec,
            text='  "USD" keeps proceeds as cash.  "USDC"/"USDT" stays in stablecoins.  '
                 'Any coin entry immediately places a buy order for that pair.',
            font=("Segoe UI", 10), text_color=C_MUTED, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 14))

        api = section("API Credentials",
                      "docs.cdp.coinbase.com/coinbase-app/docs/authentication")
        ctk.CTkLabel(
            api,
            text="Credentials auto-saved to config.json next to the app.\n"
                 "To rotate keys: visit portal.cdp.coinbase.com → API Keys → create new key\n"
                 "Required permissions:  trade  (to place/cancel orders)  +  view  (balances, prices)",
            font=("Segoe UI", 11), text_color=C_MUTED, justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 14))

        links = section("Useful Links")
        for label, url in [
            ("Coinbase Advanced Trade API Docs",      "https://docs.cdp.coinbase.com/advanced-trade/docs/welcome"),
            ("CDP Portal — Manage API Keys",          "https://portal.cdp.coinbase.com"),
            ("WebSocket Overview",                    "https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview"),
            ("coinbase-advanced-py SDK (GitHub)",     "https://github.com/coinbase/coinbase-advanced-py"),
            ("Order Placement Reference",             "https://docs.cdp.coinbase.com/advanced-trade/reference/create_order"),
            ("Get Candles Reference",                 "https://docs.cdp.coinbase.com/advanced-trade/reference/product_getcandles"),
        ]:
            r = ctk.CTkFrame(links, fg_color="transparent")
            r.pack(fill="x", padx=20, pady=2)
            ctk.CTkLabel(r, text=f"• {label}", width=300, anchor="w",
                         font=("Segoe UI", 11), text_color=C_TEXT).pack(side="left")
            ctk.CTkLabel(r, text=url, font=("Segoe UI", 10),
                         text_color=C_ACCENT).pack(side="left")
        ctk.CTkFrame(links, height=12, fg_color="transparent").pack()

        return page

    # ── Logs page ─────────────────────────────────────────────────────────────
    def _make_logs_page(self):
        page = ctk.CTkFrame(self.content, fg_color=C_BG, corner_radius=0)

        # Tab view: Logs (signals/fills/errors) | Monitor (balance/heartbeat)
        tabs = ctk.CTkTabview(page, fg_color=C_CARD, corner_radius=14,
                              border_width=1, border_color=C_BORDER,
                              segmented_button_fg_color=C_CARD2,
                              segmented_button_selected_color=C_ACCENT2,
                              segmented_button_selected_hover_color="#5d47bb",
                              segmented_button_unselected_color=C_CARD2,
                              segmented_button_unselected_hover_color=C_BORDER2,
                              text_color=C_TEXT)
        tabs.pack(fill="both", expand=True, padx=20, pady=20)
        tabs.add("Logs")
        tabs.add("Monitor")

        # ── Logs tab ──────────────────────────────────────────────────────────
        logs_tab = tabs.tab("Logs")
        lhdr = ctk.CTkFrame(logs_tab, fg_color="transparent")
        lhdr.pack(fill="x", pady=(4, 8))
        ctk.CTkLabel(lhdr, text="SIGNALS  ·  FILLS  ·  ERRORS", font=("Segoe UI", 9, "bold"),
                     text_color=C_MUTED).pack(side="left")
        ctk.CTkButton(lhdr, text="Clear", width=68, height=26, corner_radius=6,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 10), text_color=C_TEXT2,
                      command=self._clear_logs).pack(side="right", padx=(4, 0))
        ctk.CTkButton(lhdr, text="⎘ Copy", width=72, height=26, corner_radius=6,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 10), text_color=C_TEXT2,
                      command=self._copy_logs).pack(side="right", padx=(4, 0))
        ctk.CTkButton(lhdr, text="💾 Save", width=72, height=26, corner_radius=6,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 10), text_color=C_TEXT2,
                      command=self._save_logs).pack(side="right", padx=(4, 0))
        self.log_filter = ctk.StringVar(value="All")
        ctk.CTkSegmentedButton(lhdr, values=["All", "Trades", "Errors"],
                                variable=self.log_filter, height=26,
                                command=self._filter_logs).pack(side="right", padx=10)
        self.log_box = ctk.CTkTextbox(
            logs_tab, fg_color="transparent",
            text_color=C_TEXT, font=("Courier New", 12), state="disabled")
        self.log_box.pack(fill="both", expand=True)

        # ── Monitor tab ───────────────────────────────────────────────────────
        mon_tab = tabs.tab("Monitor")
        mhdr = ctk.CTkFrame(mon_tab, fg_color="transparent")
        mhdr.pack(fill="x", pady=(4, 8))
        ctk.CTkLabel(mhdr, text="BALANCE SYNC  ·  WS HEARTBEAT  ·  POSITION TICKS",
                     font=("Segoe UI", 9, "bold"), text_color=C_MUTED).pack(side="left")
        ctk.CTkButton(mhdr, text="Clear", width=68, height=26, corner_radius=6,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 10), text_color=C_TEXT2,
                      command=self._clear_monitor).pack(side="right", padx=(4, 0))
        ctk.CTkButton(mhdr, text="⎘ Copy", width=72, height=26, corner_radius=6,
                      fg_color=C_CARD2, hover_color=C_BORDER2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 10), text_color=C_TEXT2,
                      command=self._copy_monitor).pack(side="right", padx=(4, 0))
        self.monitor_box = ctk.CTkTextbox(
            mon_tab, fg_color="transparent",
            text_color=C_TEXT2, font=("Courier New", 11), state="disabled")
        self.monitor_box.pack(fill="both", expand=True)

        self._all_logs     = []   # (entry, color, level) — non-monitor messages
        self._all_monitor  = []   # (entry) — monitor messages
        return page

    # ── Page navigation ───────────────────────────────────────────────────────
    def _show_page(self, name, title):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.page_title.configure(text=title)
        self._set_active_nav(name)
        self._active_page = name
        # Immediately redraw chart when switching to it so it's never stale
        if name == 'charts':
            self._refresh_chart()

    def _show_dashboard(self): self._show_page("dashboard", "Dashboard")
    def _show_charts(self):    self._show_page("charts",    "Live Charts")
    def _show_trades(self):    self._show_page("trades",    "Trades")
    def _show_settings(self):  self._show_page("settings",  "Settings")
    def _show_logs(self):      self._show_page("logs",      "Logs")

    # ── Logging (thread-safe) ─────────────────────────────────────────────────
    def log_message(self, message: str, level="info"):
        # Suppress consecutive duplicate messages — identical content adds no info
        if message == getattr(self, '_last_log_msg', None):
            return
        self._last_log_msg = message
        now   = datetime.now(pytz.UTC)
        ts_ui = now.strftime("%H:%M:%S")
        entry = f"[{ts_ui}] {message}\n"
        # Write to bot.log with full date + ms and level tag
        log_fn = {"error": logger.error, "warn": logger.warning,
                  "trade": logger.info,  "info": logger.info,
                  "monitor": logger.debug}.get(level, logger.info)
        log_fn(f"[{level.upper():<7}] {message}")

        if level == "monitor":
            # Route to Monitor tab — keeps main Logs clean
            if hasattr(self, '_all_monitor'):
                self._all_monitor.append(entry)
            if self.root_alive:
                self.root.after(0, self._gui_append_monitor, entry)
        else:
            cmap  = {"error": C_RED, "warn": C_ORANGE, "trade": C_GREEN, "info": C_TEXT}
            color = cmap.get(level, C_TEXT)
            if hasattr(self, '_all_logs'):
                self._all_logs.append((entry, color, level))
            if self.root_alive:
                self.root.after(0, self._gui_append_log,      entry)
                self.root.after(0, self._gui_append_activity, entry)

    def _gui_append_log(self, entry):
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", entry)
            self.log_box.configure(state="disabled")
            self.log_box.see("end")
        except Exception:
            pass

    def _gui_append_monitor(self, entry):
        try:
            if not hasattr(self, 'monitor_box'):
                return
            self.monitor_box.configure(state="normal")
            self.monitor_box.insert("end", entry)
            self.monitor_box.configure(state="disabled")
            self.monitor_box.see("end")
        except Exception:
            pass

    def _gui_append_activity(self, entry):
        try:
            self.activity_box.configure(state="normal")
            self.activity_box.insert("end", entry)
            self.activity_box.configure(state="disabled")
            self.activity_box.see("end")
        except Exception:
            pass

    def _clear_logs(self):
        self._all_logs.clear()
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _clear_monitor(self):
        self._all_monitor.clear()
        self.monitor_box.configure(state="normal")
        self.monitor_box.delete("1.0", "end")
        self.monitor_box.configure(state="disabled")

    def _copy_monitor(self):
        try:
            text = self.monitor_box.get("1.0", "end").strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            pass

    def _copy_logs(self):
        try:
            text = self.log_box.get("1.0", "end").strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log_message("Logs copied to clipboard", "info")
        except Exception:
            pass

    def _save_logs(self):
        """Save bot.log to a user-chosen file via file dialog."""
        try:
            from tkinter import filedialog
            ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = filedialog.asksaveasfilename(
                parent=self.root,
                initialfile=f"nexxus_logs_{ts}.txt",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("Log files", "*.log"), ("All files", "*.*")],
                title="Save Logs"
            )
            if not path:
                return
            bot_log = os.path.join(os.path.dirname(__file__), 'bot.log')
            if os.path.exists(bot_log):
                import shutil
                shutil.copy2(bot_log, path)
                self.log_message(f"Logs saved → {path}", "info")
            else:
                # Fall back to saving the in-memory log box content
                text = self.log_box.get("1.0", "end")
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                self.log_message(f"Logs saved → {path}", "info")
        except Exception as e:
            self.log_message(f"Save logs error: {e}", "warn")

    def _filter_logs(self, _=None):
        filt = self.log_filter.get()
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        for entry, _, level in self._all_logs:
            show = (filt == "All" or
                    (filt == "Trades" and level == "trade") or
                    (filt == "Errors" and level in ("error", "warn")))
            if show:
                self.log_box.insert("end", entry)
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    # ── Metrics update ────────────────────────────────────────────────────────
    def _update_metrics(self):
        try:
            liquid_usd = self.usd_balance
            coin_value = sum(self.real_exposure[p] for p in TRADING_PAIRS)
            pair_alloc = sum(self.bot_pair_alloc[p] for p in TRADING_PAIRS)
            bot_total  = self.bot_balance + pair_alloc
            bot_exp    = sum(self.bot_exposure[p] for p in TRADING_PAIRS)
            # Portfolio = real liquid USD + real coin market value.
            # bot_total (earmarked USD) is already INSIDE liquid_usd — Coinbase still
            # holds that cash, we just label it for the bot.
            # bot_exp (deployed USD) is already INSIDE coin_value — those are real coins.
            # Adding them again would double-count and produce a false P&L swing on allocation.
            portfolio  = liquid_usd + coin_value
            pnl        = portfolio - self.initial_balance if self.initial_balance else 0
            pnl_pct    = (pnl / self.initial_balance * 100) if self.initial_balance else 0
            pcolor     = C_MUTED if abs(pnl) < 0.005 else (C_GREEN if pnl > 0 else C_RED)
            active     = sum(1 for t in self.trade_history.values()
                             if t.get('event') == 'trade')

            # Portfolio card
            self.metric_cards['usd_bal']._val.configure(text=f"${portfolio:,.2f}")
            self.metric_cards['usd_bal']._sub.configure(
                text=f"Liquid ${liquid_usd:,.2f}  ·  Coins ${coin_value:,.2f}")

            # Bot balance card — include coin holdings managed by the bot
            coin_holdings_val_mc = sum(
                self.bot_coin_qty.get(p, 0) * self.live_prices.get(p, 0)
                for p in TRADING_PAIRS
            )
            bot_display_total = min(bot_total + coin_holdings_val_mc, portfolio)
            alloc_lines = []
            for p in TRADING_PAIRS:
                usd_p  = self.bot_pair_alloc.get(p, 0)
                cqty_p = self.bot_coin_qty.get(p, 0)
                cval_p = cqty_p * self.live_prices.get(p, 0) if cqty_p > 0 else 0
                if usd_p > 0 and cval_p > 0:
                    alloc_lines.append(f"{p.split('-')[0]} ${usd_p+cval_p:,.2f}")
                elif usd_p > 0:
                    alloc_lines.append(f"{p.split('-')[0]} ${usd_p:,.2f}")
                elif cval_p > 0:
                    alloc_lines.append(f"{p.split('-')[0]} {cqty_p:.4f} ≈ ${cval_p:,.2f}")
            alloc_parts = "  ·  ".join(alloc_lines)
            self.metric_cards['bot_bal']._val.configure(text=f"${bot_display_total:,.2f}")
            self.metric_cards['bot_bal']._sub.configure(
                text=alloc_parts if alloc_parts else f"Unallocated  ·  {active} active trade{'s' if active != 1 else ''}")

            # Coin holdings card
            self.metric_cards['exposure']._val.configure(text=f"${coin_value:,.2f}")
            self.metric_cards['exposure']._sub.configure(
                text="  ".join(
                    f"{p.split('-')[0]} ${self.real_exposure[p]:,.2f}"
                    for p in TRADING_PAIRS if self.real_exposure[p] > 0
                ) or "No open positions")

            # P&L card
            sign = "+" if pnl > 0.005 else ("" if abs(pnl) < 0.005 else "-")
            self.metric_cards['pnl']._val.configure(
                text=f"{sign}${abs(pnl):,.2f}", text_color=pcolor)
            self.metric_cards['pnl']._sub.configure(
                text=f"{pnl_pct:+.2f}%  ·  initial ${self.initial_balance:,.2f}"
                if self.initial_balance else "Accumulating baseline…",
                text_color=pcolor)

            # Topbar
            self.bal_label.configure(text=f"Portfolio  ${portfolio:,.2f}")
            self.pnl_label.configure(
                text=f"P&L  {sign}${abs(pnl):,.2f}", text_color=pcolor)
            # Allocated line — only show when bot has funds under management
            _ch_val = sum(
                self.bot_coin_qty.get(p, 0) * self.live_prices.get(p, 0)
                for p in TRADING_PAIRS)
            _alloc_total = bot_total + _ch_val
            if _alloc_total >= 0.01:
                _parts = []
                for p in TRADING_PAIRS:
                    _uv = self.bot_pair_alloc.get(p, 0)
                    _cv = self.bot_coin_qty.get(p, 0) * self.live_prices.get(p, 0)
                    if _uv + _cv >= 0.01:
                        coin = p.split('-')[0]
                        _parts.append(f"{coin} ${_uv+_cv:,.2f}")
                _detail = "  ·  ".join(_parts) if _parts else f"${_alloc_total:,.2f}"
                self.alloc_label.configure(
                    text=f"Bot  {_detail}", text_color=C_ACCENT2)
            else:
                self.alloc_label.configure(text="")

            # Bot status card
            state_txt = ("PAUSED" if self.paused else
                         "RUNNING" if self.running else "STOPPED")
            state_col = (C_ORANGE if self.paused else
                         C_GREEN  if self.running else C_RED)
            self.bs_state_lbl.configure(text=state_txt, text_color=state_col)

            # Mode: user's direction setting + capital availability
            _dir = self.signal_direction  # 'Both' | 'Buy Only' | 'Sell Only'
            _has_usd  = bot_total >= MINIMUM_RESERVE
            _has_coin = any(self.bot_coin_qty.get(p, 0) > 0 or
                            self.bot_exposure.get(p, 0) > 0 for p in TRADING_PAIRS)
            if not (_has_usd or _has_coin):
                _mode_txt, _mode_col = "No funds", C_RED
            elif _dir == 'Buy Only':
                _mode_txt = "Buy Only"
                _mode_col = C_GREEN if _has_usd else C_RED
            elif _dir == 'Sell Only':
                _mode_txt = "Sell Only"
                _mode_col = C_ORANGE if _has_coin else C_RED
            else:
                # Both — show what's actually possible given capital
                _can_buy  = _has_usd
                _can_sell = _has_coin
                if _can_buy and _can_sell:
                    _mode_txt, _mode_col = "Both", C_GREEN
                elif _can_buy:
                    _mode_txt, _mode_col = "Both (buy cap only)", C_ACCENT
                else:
                    _mode_txt, _mode_col = "Both (sell cap only)", C_ORANGE
            self.bs_mode_lbl.configure(text=_mode_txt, text_color=_mode_col)

            self.bs_signal_lbl.configure(text=self.signal_tf)

            # Next signal window: time until next candle close on signal TF
            _tf_secs = {'1m': 60, '5m': 300, '1h': 3600, '1d': 86400}
            _period  = _tf_secs.get(self.signal_tf, 3600)
            _now_ts  = time.time()
            _elapsed = _now_ts % _period
            _remain  = _period - _elapsed
            if _remain < 60:
                _next_txt = f"{int(_remain)}s"
            elif _remain < 3600:
                _next_txt = f"{int(_remain//60)}m {int(_remain%60)}s"
            else:
                _next_txt = f"{int(_remain//3600)}h {int((_remain%3600)//60)}m"
            self.bs_next_lbl.configure(text=_next_txt,
                                        text_color=C_ACCENT if _remain > 60 else C_ORANGE)

            # Price feed freshness
            _stale_pairs = [
                p.split('-')[0] for p in TRADING_PAIRS
                if time.time() - self._price_ts.get(p, 0) > 15
            ]
            if _stale_pairs:
                self.bs_feed_lbl.configure(
                    text=f"STALE ({', '.join(_stale_pairs)})", text_color=C_RED)
            else:
                _oldest = max(
                    time.time() - self._price_ts.get(p, time.time())
                    for p in TRADING_PAIRS) if self._price_ts else 0
                self.bs_feed_lbl.configure(
                    text=f"LIVE  {_oldest:.1f}s ago", text_color=C_GREEN)

            self.bs_trades_lbl.configure(text=str(active))
            # Per-pair allocation sub-rows (includes coin holdings value)
            for pair, (row, lbl) in self._bs_pair_alloc_rows.items():
                usd_alloc  = self.bot_pair_alloc.get(pair, 0)
                exp        = self.bot_exposure.get(pair, 0)
                coin_qty   = self.bot_coin_qty.get(pair, 0)
                coin_val_p = coin_qty * self.live_prices.get(pair, 0) if coin_qty > 0 else 0
                total_p    = usd_alloc + exp + coin_val_p
                if total_p > 0:
                    parts = []
                    if usd_alloc > 0:
                        parts.append(f"${usd_alloc:,.2f} USD")
                    if coin_val_p > 0:
                        coin = pair.split('-')[0]
                        parts.append(f"{coin_qty:.4f} {coin} ≈ ${coin_val_p:,.2f}")
                    if exp > 0:
                        parts.append(f"${exp:,.2f} deployed")
                    lbl.configure(text="  ·  ".join(parts),
                                  text_color=C_ACCENT2)
                    row.pack(fill="x", pady=1)
                else:
                    row.pack_forget()

            # Total allocated = USD + coin holdings market value + deployed
            coin_holdings_val = sum(
                self.bot_coin_qty.get(p, 0) * self.live_prices.get(p, 0)
                for p in TRADING_PAIRS
            )
            alloc_display = bot_total + coin_holdings_val
            suffix = "  (general pool)" if (
                self.bot_balance > 0 and not any(
                    self.bot_pair_alloc.get(p, 0) > 0 for p in TRADING_PAIRS)
                and coin_holdings_val == 0) else ""
            self.bs_alloc_lbl.configure(text=f"${alloc_display:,.2f}{suffix}")

            # Last executed signal panel — only show after a real fill
            sig = self.last_executed_signal
            if sig:
                side      = sig['side']
                side_col  = C_GREEN if side == 'buy' else C_RED
                ts_str    = datetime.fromtimestamp(sig['ts']).strftime("%b %d  %H:%M:%S")
                self._bs_sig_side_lbl.configure(
                    text=side.upper(), text_color=side_col)
                self._bs_sig_pair_lbl.configure(text=sig['pair'])
                self._bs_sig_src_lbl.configure(text=sig.get('source', ''))
                self._bs_sig_price_lbl.configure(
                    text=format_price(sig['price']))
                self._bs_sig_spent_lbl.configure(
                    text=f"  ${sig['spent']:,.2f}  ·  {sig['qty']:.6f} {sig['pair'].split('-')[0]}")
                self._bs_sig_time_lbl.configure(text=ts_str)
                self._last_sig_frame.pack(fill="x", pady=(0, 4))
            else:
                self._last_sig_frame.pack_forget()

            # Charts header allocation
            cur_pair = self.chart_pair_var.get()
            pa = self.bot_pair_alloc.get(cur_pair, 0)
            self.chart_liquid_lbl.configure(text=f"Liquid  ${liquid_usd:,.2f}")
            self.chart_bot_lbl.configure(
                text=f"{cur_pair.split('-')[0]}  ${pa:,.2f}" if pa > 0 else
                     f"{cur_pair.split('-')[0]}  —",
                text_color=C_ACCENT2 if pa > 0 else C_MUTED)
        except Exception:
            pass

    def _copy_bot_status(self):
        """Copy current bot status snapshot to clipboard."""
        try:
            state_txt = ("PAUSED" if self.paused else
                         "RUNNING" if self.running else "STOPPED")
            bot_total = self.bot_balance + sum(self.bot_pair_alloc[p] for p in TRADING_PAIRS)
            bot_exp   = sum(self.bot_exposure[p] for p in TRADING_PAIRS)
            active    = sum(1 for t in self.trade_history.values() if t.get('event') == 'trade')
            lines = [
                f"── BOT STATUS ─────────────────── {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"State        : {state_txt}",
                f"Mode         : {self.signal_direction}",
                f"Signal TF    : {self.signal_tf}",
                f"Active Trades: {active}",
                f"Allocated    : ${bot_total + sum(self.bot_coin_qty.get(p,0)*self.live_prices.get(p,0) for p in TRADING_PAIRS):,.2f}",
                f"Deployed     : ${bot_exp:,.2f}",
                f"Liquid USD   : ${self.usd_balance:,.2f}",
            ]
            for pair in TRADING_PAIRS:
                usd_a = self.bot_pair_alloc.get(pair, 0)
                exp_a = self.bot_exposure.get(pair, 0)
                cqty  = self.bot_coin_qty.get(pair, 0)
                cval  = cqty * self.live_prices.get(pair, 0) if cqty > 0 else 0
                if usd_a + exp_a + cval > 0:
                    lines.append(
                        f"  {pair.split('-')[0]:<6}: ${usd_a:,.2f} USD  "
                        f"${exp_a:,.2f} deployed  "
                        f"{cqty:.4f} coins ≈ ${cval:,.2f}")
            sig = self.last_executed_signal
            if sig:
                lines.append(
                    f"Last Signal  : {sig['side'].upper()} {sig['pair']} "
                    f"@ {format_price(sig['price'])}  "
                    f"${sig['spent']:,.2f}  "
                    f"{datetime.fromtimestamp(sig['ts']).strftime('%H:%M:%S')}")
            text = "\n".join(lines)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception as e:
            self.log_message(f"Copy bot status error: {e}", "warn")

    def _copy_trades(self):
        """Copy active positions + closed trade history to clipboard."""
        try:
            lines = [
                f"── TRADES ──────────────────────── {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "ACTIVE POSITIONS",
                f"{'Pair':<12}{'Side':<6}{'Qty':>12}{'Entry':>12}{'Current':>12}{'P&L':>10}{'SL':>12}{'TP':>12}",
                "-" * 82,
            ]
            active_trades = [t for t in self.trade_history.values() if t.get('event') == 'trade']
            if active_trades:
                for t in active_trades:
                    pair = t.get('symbol', '')
                    cur  = self.live_prices.get(pair, t.get('entry_price', 0))
                    qty  = t.get('quantity', 0)
                    entr = t.get('entry_price', 0)
                    pl   = (cur - entr) * qty if t.get('side') == 'buy' else (entr - cur) * qty
                    lines.append(
                        f"{pair:<12}{t.get('side','').upper():<6}"
                        f"{qty:>12.4f}{format_price(entr):>12}{format_price(cur):>12}"
                        f"{pl:>+10.2f}{format_price(t.get('stop_loss',0)):>12}"
                        f"{format_price(t.get('take_profit',0)):>12}")
            else:
                lines.append("  (no active positions)")

            lines.append("")
            lines.append("CLOSED TRADE HISTORY")
            try:
                hist_text = self.history_box.get("1.0", "end").strip()
                lines.append(hist_text if hist_text else "  (no history)")
            except Exception:
                lines.append("  (unavailable)")

            text = "\n".join(lines)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception as e:
            self.log_message(f"Copy trades error: {e}", "warn")

    def _update_pair_cards(self):
        for pair in TRADING_PAIRS:
            pc    = self.pair_cards.get(pair)
            price = self.live_prices.get(pair, 0)
            # Use true 24h change (last vs prior daily close); fall back to 1h span
            pct24 = self.pct_24h.get(pair)
            if pct24 is not None:
                pct, label = pct24, "24h"
            else:
                pct   = self.percent_change.get('1h', {}).get(pair, 0.0)
                label = "1h"
            col = C_GREEN if pct >= 0 else C_RED
            if pc and price:
                pc._price.configure(text=format_price(price))
                pct_str = f"  {'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%  {label}  "
                pc._pct.configure(text=pct_str, text_color=col)
                pc._pct_badge.configure(
                    fg_color="#0d2a1a" if pct >= 0 else "#2a0d0d")
                # H/L from most recent daily candle
                day_data = list(self.candle_history['1d'][pair])
                if day_data:
                    d = day_data[-1]
                    pc._high.configure(
                        text=f"H  {format_price(d[2])}", text_color="#64b88a")
                    pc._low.configure(
                        text=f"L  {format_price(d[3])}", text_color="#b86464")
            # Update topbar ticker (labels only — never reconfigure the frame border,
            # as CTkFrame.configure() causes a full widget redraw that produces flicker)
            if pair in self._ticker_labels and price:
                pct_str = f"{'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%"
                self._ticker_labels[pair]['price'].configure(
                    text=format_price(price), text_color=col)
                self._ticker_labels[pair]['pct'].configure(
                    text=pct_str, text_color=col)

    def _on_chart_pair_change(self, _=None):
        """Update chart header, trigger on-demand fetch for watchlist pairs, refresh."""
        pair = self.chart_pair_var.get()
        self.chart_hdr_pair_lbl.configure(text=pair.replace("-", "/"))
        # If this pair has no cached data yet (watchlist pair never fetched), kick off
        # a background fetch for all timeframes so the chart populates immediately.
        if pair not in TRADING_PAIRS:
            has_data = any(len(self.candle_history[tf][pair]) > 0 for tf in TIMEFRAMES)
            if not has_data:
                self.log_message(f"On-demand fetch: {pair} (watchlist)", "info")
                asyncio.run_coroutine_threadsafe(
                    self._fetch_watchlist_pair(pair), self.loop)
        self._refresh_chart()

    # ── Chart drawing ─────────────────────────────────────────────────────────
    def _refresh_chart(self, _=None):
        pair = self.chart_pair_var.get()
        tf   = self.chart_tf_var.get()
        data = list(self.candle_history[tf][pair])

        if not data or len(data) < 2:
            self.chart_ax.clear()
            self.chart_ax.set_facecolor(C_CHART_BG)
            self.chart_ax.text(0.5, 0.5,
                               f"Waiting for {pair} {tf} candle data…\n"
                               f"({len(data)} candles loaded so far)",
                               ha='center', va='center',
                               transform=self.chart_ax.transAxes,
                               color=C_MUTED, fontsize=12)
            try:
                self.chart_canvas.draw_idle()
            except Exception:
                pass
            return

        limit        = DISPLAY_CANDLES.get(tf, 200)
        # compute_data: 2× history for accurate MA warm-up
        compute_data = list(data[-(2 * limit):])
        # data: only what the user sees on the chart
        data         = list(data[-limit:])

        # ── Live forming candle injection ────────────────────────────────────
        # Synthesise the currently-forming candle from WebSocket ticks so the
        # chart updates in real-time between REST candle refreshes (~5 min gap).
        _seconds_per = {'1m': 60, '5m': 300, '1h': 3600, '1d': 86400}
        _fc_secs = _seconds_per.get(tf, 3600)
        _fc_period_ms = (int(time.time()) // _fc_secs) * _fc_secs * 1000
        _fc_raw = self._forming_candle.get((pair, tf))
        if _fc_raw is not None:
            _fc_ts = _fc_raw[0]
            if data:
                _last_stored_ts = data[-1][0]
                if _fc_ts >= _last_stored_ts:
                    # Apply Heikin-Ashi to the raw forming candle using last stored HA candle
                    _prev_ha = data[-1] if _fc_ts > _last_stored_ts else (data[-2] if len(data) >= 2 else data[-1])
                    _ha_o = (_prev_ha[1] + _prev_ha[4]) / 2.0
                    _ha_c = (_fc_raw[1] + _fc_raw[2] + _fc_raw[3] + _fc_raw[4]) / 4.0
                    _ha_h = max(_fc_raw[2], _ha_o, _ha_c)
                    _ha_l = min(_fc_raw[3], _ha_o, _ha_c)
                    _forming_ha = [_fc_ts, _ha_o, _ha_h, _ha_l, _ha_c, _fc_raw[5]]
                    if _fc_ts > _last_stored_ts:
                        data = data + [_forming_ha]
                    else:
                        data = data[:-1] + [_forming_ha]
                    # Use the live price as close for indicator freshness (not HA close)
                    _live_p = self.live_prices.get(pair, 0)
                    if _live_p:
                        _forming_ha[4] = (_fc_raw[1] + _fc_raw[2] + _fc_raw[3] + _live_p) / 4.0

        ax           = self.chart_ax

        # ── Save pan/zoom state BEFORE ax.clear() destroys it ────────────────
        # ax.clear() resets xlim/ylim to matplotlib defaults (0,1).
        # Reading them AFTER clear (old behaviour) always restored the default,
        # not the user's actual zoom level.  Save here; restore in section 9.
        _pre_xl = ax.get_xlim() if self._zoom_locked else None
        _pre_yl = ax.get_ylim() if self._zoom_locked else None

        # Indicators are pre-computed by _ingest_candles on actual candle arrival
        # and cached in indicator_engine.data[pair].  Reading cached values here
        # is O(1) instead of the previous O(N) recompute on every 1s chart refresh.
        # On first startup (before first candle ingest) they may be 0/empty —
        # the chart renders gracefully with missing indicators.
        ind    = self.indicator_engine.data[pair]
        atr    = ind['atr']
        price  = self.live_prices.get(pair, 0)

        # Pre-compute warmed MAs from full 2× history; take last `limit` values
        # so every visible candle has a valid MA — no cold-start distortion.
        _all_c = np.array([c[4] for c in compute_data])
        def _warmed_ma(period):
            if len(_all_c) < period:
                return np.array([])
            ma = np.convolve(_all_c, np.ones(period) / period, mode='valid')
            return ma[-limit:] if len(ma) >= limit else ma

        # Update chart page header
        pct = (self.percent_change.get('1d', {}).get(pair)
               or self.percent_change.get('1h', {}).get(pair, 0.0))
        pct_col = C_GREEN if pct >= 0 else C_RED
        try:
            self.chart_hdr_pair_lbl.configure(text=pair.replace("-", "/"))
            self.chart_hdr_price_lbl.configure(
                text=format_price(price) if price else "—")
            pct_str = f"{'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%"
            self.chart_hdr_change_lbl.configure(text=pct_str, text_color=pct_col)
            data_hi = max(c[2] for c in data)
            data_lo = min(c[3] for c in data)
            stats_parts = [f"H {format_price(data_hi)}", f"L {format_price(data_lo)}"]
            if atr > 0:
                stats_parts.insert(0, f"ATR {format_price(atr)}")
            self.chart_hdr_stats_lbl.configure(text="   ·   ".join(stats_parts))
        except Exception:
            pass

        ax.clear()
        ax.set_facecolor(C_CHART_BG)
        ax.grid(True, color="#1e2330", linewidth=0.5, alpha=0.5)

        ts     = [datetime.fromtimestamp(c[0]/1000, pytz.UTC) for c in data]
        opens  = np.array([c[1] for c in data])
        highs  = np.array([c[2] for c in data])
        lows   = np.array([c[3] for c in data])
        closes = np.array([c[4] for c in data])

        chart_start_ms = data[0][0]

        # Use median inter-candle gap (not first-two only) so sparse pairs like
        # XCN-USD — where the first gap may be minutes wide — get the right width.
        if len(ts) > 2:
            _num_ts = mdates.date2num(ts)
            _gaps   = np.diff(_num_ts)
            _med    = float(np.median(_gaps))
            delta   = _med * 0.7
        elif len(ts) == 2:
            delta = (mdates.date2num(ts[1]) - mdates.date2num(ts[0])) * 0.7
        else:
            delta = 0.003
        # Tight signal offset: ATR-based so arrows sit right at the candle wick
        signal_offset = max(atr * 0.08, closes[-1] * 0.001) if atr > 0 else closes[-1] * 0.001

        # ── 1. Order Blocks (cap to 3 most recent visible) ───────────────────
        ob_bull_drawn = ob_bear_drawn = False
        obs_visible = [ob for ob in ind['order_blocks'] if ob[0] >= chart_start_ms][-3:]
        for ob_ts, ob_high, ob_low, is_bull in obs_visible:
            x_start = datetime.fromtimestamp(ob_ts / 1000, pytz.UTC)
            x_end   = ts[-1]
            color   = C_GREEN if is_bull else C_RED
            ax.fill_betweenx([ob_low, ob_high], x_start, x_end,
                              color=color, alpha=0.15)
            ax.hlines([ob_low, ob_high], x_start, x_end,
                      colors=color, linewidths=0.6, alpha=0.5, linestyles='solid')
            ax.annotate("OB↑" if is_bull else "OB↓",
                        xy=(x_end, (ob_high + ob_low) / 2),
                        fontsize=7, color=color, alpha=0.8,
                        ha='right', va='center',
                        bbox=dict(boxstyle='round,pad=0.1', fc=C_CHART_BG,
                                  ec=color, alpha=0.6, lw=0.5))
            if is_bull: ob_bull_drawn = True
            else:       ob_bear_drawn = True

        # ── 2. Fair Value Gaps (cap to 3 most recent; age-limited per TF) ──────
        fvg_bull_drawn = fvg_bear_drawn = False
        # Limit how far back FVGs are shown on short TFs (they become irrelevant quickly)
        _fvg_lookback_ms = {'1m': 45*60*1000, '5m': 3*3600*1000,
                            '1h': 24*3600*1000, '1d': 60*86400*1000}
        _fvg_min_ts = data[-1][0] - _fvg_lookback_ms.get(tf, 24*3600*1000)
        fvgs_visible = [fvg for fvg in ind['fair_value_gaps']
                        if fvg[0] >= chart_start_ms and fvg[0] >= _fvg_min_ts][-3:]
        for fvg_ts, fvg_high, fvg_low, is_bull in fvgs_visible:
            x_start = datetime.fromtimestamp(fvg_ts / 1000, pytz.UTC)
            x_end   = ts[-1]
            color   = "#00bcd4" if is_bull else "#e040fb"
            ax.fill_betweenx([fvg_low, fvg_high], x_start, x_end,
                              color=color, alpha=0.12)
            ax.hlines([fvg_low, fvg_high], x_start, x_end,
                      colors=color, linewidths=0.5, alpha=0.45, linestyles='dashed')
            ax.annotate("FVG↑" if is_bull else "FVG↓",
                        xy=(x_start, (fvg_high + fvg_low) / 2),
                        fontsize=7, color=color, alpha=0.8,
                        ha='left', va='center',
                        bbox=dict(boxstyle='round,pad=0.1', fc=C_CHART_BG,
                                  ec=color, alpha=0.6, lw=0.5))
            if is_bull: fvg_bull_drawn = True
            else:       fvg_bear_drawn = True

        # ── 3. Support / Resistance (top 3 strongest within visible range) ───
        sr_sup_drawn = sr_res_drawn = False
        price_lo = lows.min() * 0.98
        price_hi = highs.max() * 1.02
        # ind['sr_zones'] is already sorted by strength desc; filter then cap at 3
        visible_sr = [z for z in ind['sr_zones'] if price_lo <= z[0] <= price_hi][:3]
        for sr_price, is_support, strength in visible_sr:
            color = C_GREEN if is_support else C_RED
            ax.axhline(y=sr_price, color=color, linestyle='--',
                       linewidth=0.8, alpha=0.55)
            ax.annotate(f"{'S' if is_support else 'R'} {format_price(sr_price)}",
                        xy=(ts[-1], sr_price),
                        fontsize=7, color=color, alpha=0.85,
                        ha='right', va='bottom' if is_support else 'top',
                        bbox=dict(boxstyle='round,pad=0.1', fc=C_CHART_BG,
                                  ec=color, alpha=0.5, lw=0.5))
            if is_support: sr_sup_drawn = True
            else:          sr_res_drawn = True

        # ── 4. Heikin Ashi candles ───────────────────────────────────────────
        for i in range(len(data)):
            up  = closes[i] >= opens[i]
            col = C_GREEN if up else C_RED
            ax.vlines(ts[i], lows[i], highs[i], color=col, linewidth=0.8, alpha=0.7)
            ax.bar(ts[i], abs(closes[i] - opens[i]), delta,
                   bottom=min(opens[i], closes[i]), color=col, alpha=0.9)

        # ── 5. Moving Averages (warmed up from 2× history) ───────────────────
        # _warmed_ma() already computed at top of _refresh_chart from compute_data.
        # Returns exactly `limit` values — one per visible candle, fully warmed,
        # so we plot against all of ts with no skipped initial candles.
        ma_colors = [C_ACCENT, C_ACCENT2, C_ORANGE]
        ma_lines  = {}   # period → warmed MA array (for crossover detection below)
        for period, col in zip(MA_PERIODS, ma_colors):
            warmed = _warmed_ma(period)
            if len(warmed) == len(ts):
                ax.plot(ts, warmed, color=col, linewidth=1.3, alpha=0.9)
            elif len(warmed) > 0:
                ax.plot(ts[-len(warmed):], warmed, color=col, linewidth=1.3, alpha=0.9)
            ma_lines[period] = warmed

        # ── 6. Signals ────────────────────────────────────────────────────────
        # INVARIANT: every marker drawn here uses the SAME logic as
        # _ingest_candles + engine.calculate_signals / calculate_breakout.
        # Signals are ONLY shown when viewing the signal TF so that "a marker
        # on the chart" == "something that could trigger a trade".
        # On non-signal TF views, MA lines are still visible for context but
        # no signal markers are drawn — those crossovers do not drive orders.

        sorted_periods = sorted(MA_PERIODS)
        p_fast = sorted_periods[0]
        p_slow = sorted_periods[1] if len(sorted_periods) > 1 else sorted_periods[0]

        self._signal_data = []
        buy_signal_drawn = sell_signal_drawn = False
        breakout_drawn   = False
        is_signal_tf     = (tf == self.signal_tf)

        if not is_signal_tf:
            # Non-signal TF: no markers — inform user which TF has signals.
            ax.annotate(
                f"Signal markers shown on {self.signal_tf} chart",
                xy=(0.01, 0.01), xycoords='axes fraction',
                fontsize=7, color=C_MUTED, alpha=0.65, va='bottom', ha='left')
        else:
            # ── Precompute confirmation TF MA diff indexed by candle timestamp ─
            # Matches engine.calculate_signals: BUY only if MA_fast > MA_slow on
            # conf TF at the time of the crossover.  SELL requires the opposite.
            _conf_map = {'1m': '1m', '5m': '1m', '1h': '5m', '1d': '1h'}
            _conf_tf  = _conf_map.get(self.signal_tf, '1m')
            _conf_raw = list(self.candle_history[_conf_tf][pair])
            _conf_diff_by_ts: dict = {}   # candle_ts_ms → float (fast_ma − slow_ma)
            if len(_conf_raw) >= p_slow:
                _conf_c    = np.array([c[4] for c in _conf_raw])
                _cf_fast   = np.convolve(_conf_c, np.ones(p_fast) / p_fast, 'valid')
                _cf_slow   = np.convolve(_conf_c, np.ones(p_slow) / p_slow, 'valid')
                _conf_vts  = [c[0] for c in _conf_raw[(p_slow - 1):]]
                for _t, _fd, _sd in zip(_conf_vts, _cf_fast, _cf_slow):
                    _conf_diff_by_ts[_t] = float(_fd) - float(_sd)
            _conf_ts_sorted = sorted(_conf_diff_by_ts.keys())

            import bisect as _bisect
            def _conf_diff_at(ts_ms: int) -> 'float | None':
                """Conf TF MA diff at the most recent candle ≤ ts_ms. None if unavailable."""
                if not _conf_ts_sorted:
                    return None
                idx = _bisect.bisect_right(_conf_ts_sorted, ts_ms) - 1
                if idx < 0:
                    return None
                return _conf_diff_by_ts[_conf_ts_sorted[idx]]

            # ── 6a. MA crossover signals with conf TF validation ──────────────
            ma_fast = ma_lines.get(p_fast, np.array([]))
            ma_slow = ma_lines.get(p_slow, np.array([]))
            _n = min(len(ma_fast), len(ma_slow), len(ts))

            if _n >= 2:
                _mf        = ma_fast[-_n:]
                _ms        = ma_slow[-_n:]
                _ts        = ts[-_n:]
                _hi        = highs[-_n:]
                _lo        = lows[-_n:]
                _cl        = closes[-_n:]
                _data_win  = data[-_n:]   # raw candle rows with timestamps

                for i in range(1, _n):
                    prev = _mf[i - 1] - _ms[i - 1]
                    curr = _mf[i]     - _ms[i]
                    is_golden = (prev < 0 < curr)
                    is_death  = (prev > 0 > curr)
                    if not (is_golden or is_death):
                        continue

                    action = 'buy' if is_golden else 'sell'

                    # Confirmation TF check — same gate as engine.calculate_signals
                    candle_ts_ms = _data_win[i][0]
                    conf_diff = _conf_diff_at(candle_ts_ms)
                    if conf_diff is not None:
                        if action == 'buy'  and conf_diff <= 0:
                            continue   # conf TF disagrees — engine would reject
                        if action == 'sell' and conf_diff >= 0:
                            continue   # conf TF disagrees — engine would reject

                    if action == 'buy':
                        y_pos  = _lo[i] - signal_offset
                        color  = C_GREEN
                        marker = "▲"
                        va     = 'top'
                    else:
                        y_pos  = _hi[i] + signal_offset
                        color  = C_RED
                        marker = "▼"
                        va     = 'bottom'

                    ax.annotate(marker, (_ts[i], y_pos),
                                color=color, fontsize=10, alpha=1.0,
                                ha='center', va=va, fontweight='bold')

                    self._signal_data.append({
                        'ts':           _ts[i],
                        'price':        y_pos,
                        'candle_price': float(_cl[i]),
                        'action':       action,
                        'confirmed':    True,   # conf TF was checked above
                        'source':       f'MA{p_fast}/MA{p_slow} Cross',
                        'price_str':    format_price(_cl[i]),
                        'time_str':     _ts[i].strftime('%Y-%m-%d %H:%M:%S UTC'),
                        'ma9':          float(_mf[i]),
                        'ma20':         float(_ms[i]),
                        'p_fast':       p_fast,
                        'p_slow':       p_slow,
                        'atr':          atr,
                    })
                    if action == 'buy':  buy_signal_drawn  = True
                    else:                sell_signal_drawn = True

            # ── 6b. Breakout signals (signal TF only — matches calculate_breakout) ─
            if len(data) >= 25:
                _lb    = 20
                _cl_a  = closes
                _hi_a  = highs
                _lo_a  = lows
                _vol_a = np.array([c[5] for c in data])
                _ma9_last  = float(ma_lines.get(p_fast, [0])[-1]) if len(ma_lines.get(p_fast, [])) > 0 else 0
                _ma20_last = float(ma_lines.get(p_slow, [0])[-1]) if len(ma_lines.get(p_slow, [])) > 0 else 0
                for i in range(_lb + 4, len(data)):
                    _cur    = _cl_a[i]
                    _prev   = _cl_a[i - 1]
                    _p_hi   = _hi_a[i - _lb:i].max()
                    _p_lo   = _lo_a[i - _lb:i].min()
                    _avg_v  = _vol_a[i - _lb:i].mean() or 1e-12
                    _vsurge = _vol_a[i] > _avg_v * 2.0
                    _mom    = (_cur - _prev) / _prev if _prev > 0 else 0
                    if _cur > _p_hi and (_vsurge or _mom > 0.02):
                        y_pos = _lo_a[i] - signal_offset * 2.2
                        ax.annotate("⚡", (ts[i], y_pos),
                                    color=C_ACCENT3, fontsize=9,
                                    ha='center', va='top', fontweight='bold')
                        self._signal_data.append({
                            'ts':           ts[i],
                            'price':        y_pos,
                            'candle_price': float(_cur),
                            'action':       'buy',
                            'confirmed':    True,
                            'source':       'Breakout↑',
                            'price_str':    format_price(_cur),
                            'time_str':     ts[i].strftime('%Y-%m-%d %H:%M:%S UTC'),
                            'ma9':          _ma9_last,
                            'ma20':         _ma20_last,
                            'p_fast':       p_fast,
                            'p_slow':       p_slow,
                            'atr':          atr,
                        })
                        breakout_drawn = True
                    elif _cur < _p_lo and (_vsurge or _mom < -0.02):
                        y_pos = _hi_a[i] + signal_offset * 2.2
                        ax.annotate("⚡", (ts[i], y_pos),
                                    color=C_ACCENT3, fontsize=9,
                                    ha='center', va='bottom', fontweight='bold')
                        self._signal_data.append({
                            'ts':           ts[i],
                            'price':        y_pos,
                            'candle_price': float(_cur),
                            'action':       'sell',
                            'confirmed':    True,
                            'source':       'Breakdown↓',
                            'price_str':    format_price(_cur),
                            'time_str':     ts[i].strftime('%Y-%m-%d %H:%M:%S UTC'),
                            'ma9':          _ma9_last,
                            'ma20':         _ma20_last,
                            'p_fast':       p_fast,
                            'p_slow':       p_slow,
                            'atr':          atr,
                        })
                        breakout_drawn = True

        # ── 7. Live price line ────────────────────────────────────────────────
        # The price label annotation is drawn in section 9 AFTER set_ylim so
        # the blended-transform y position is always within the visible range.
        if price:
            ax.axhline(y=price, color="#ffffff", linestyle=':', linewidth=0.8, alpha=0.4)

        # ── 7b. Last executed trade marker ───────────────────────────────────
        # Drawn from last_executed_signal (seeded from trades.json or bot.log),
        # independent of the MA crossover replay so it always reflects the actual fill.
        _les = self.last_executed_signal
        if _les and _les.get('pair') == pair and _les.get('price') and _les.get('ts'):
            _les_price = float(_les['price'])
            _les_side  = _les.get('side', '')
            _les_color = C_RED if _les_side == 'sell' else C_GREEN
            _les_marker = '▼' if _les_side == 'sell' else '▲'
            # Dashed entry line across chart
            ax.axhline(y=_les_price, color=_les_color, linestyle='--',
                       linewidth=0.9, alpha=0.55)
            # Right-edge label with entry price
            import matplotlib.transforms as _mt2
            _bl2 = _mt2.blended_transform_factory(ax.transAxes, ax.transData)
            ax.annotate(f" {_les_marker} {format_price(_les_price)} ",
                        xy=(1.0, _les_price), xycoords=_bl2,
                        fontsize=7, color=_les_color, alpha=0.95,
                        ha='left', va='center', clip_on=False, annotation_clip=False,
                        bbox=dict(boxstyle='round,pad=0.2', fc=C_CARD2,
                                  ec=_les_color, alpha=0.88, lw=0.8))
            # Arrow marker at the time on chart + hover-tooltip entry.
            # Keep _les_dt as UTC-aware — mdates.date2num treats naive datetimes
            # as local time, which would shift the hover hit-box by the UTC offset.
            try:
                _les_dt  = datetime.fromtimestamp(_les['ts'], pytz.UTC)
                _les_x   = mdates.date2num(_les_dt)
                _xl      = ax.get_xlim()
                # Use the same ATR-based signal_offset as MA crossover arrows
                # (avoids stale ax.get_ylim() before matplotlib auto-scales).
                _offset  = signal_offset
                if _les_side == 'sell':
                    _marker_y = _les_price + _offset
                    _va       = 'bottom'
                else:
                    _marker_y = _les_price - _offset
                    _va       = 'top'
                if _xl[0] <= _les_x <= _xl[1]:
                    ax.annotate(_les_marker, (_les_dt, _marker_y),
                                color=_les_color, fontsize=11, alpha=1.0,
                                ha='center', va=_va, fontweight='bold',
                                clip_on=True)
                # Always register in _signal_data so hover works even when
                # the marker timestamp is outside the current view window.
                _les_ma_fast = float(ma_lines.get(p_fast, [0])[-1]) if ma_lines.get(p_fast, []) else 0
                _les_ma_slow = float(ma_lines.get(p_slow, [0])[-1]) if ma_lines.get(p_slow, []) else 0
                self._signal_data.append({
                    'ts':           _les_dt,           # UTC-aware — matches chart ts dtype
                    'price':        _marker_y,
                    'candle_price': _les_price,
                    'action':       _les_side,
                    'confirmed':    True,
                    'source':       f"Filled ({_les.get('source', 'order')})",
                    'price_str':    format_price(_les_price),
                    'time_str':     _les_dt.strftime('%Y-%m-%d %H:%M:%S UTC'),
                    'ma9':          _les_ma_fast,
                    'ma20':         _les_ma_slow,
                    'p_fast':       p_fast,
                    'p_slow':       p_slow,
                    'atr':          atr,
                })
            except Exception:
                pass

        # ── 8. ATR-based SL/TP bands (long bias: SL below, TP above) ────────
        if atr > 0 and price:
            sl_pct = 1.5 * atr / price
            tp_pct = 3.0 * atr / price
            ax.axhline(y=price * (1 - sl_pct), color=C_RED,
                       linestyle=':', linewidth=0.7, alpha=0.4)
            ax.axhline(y=price * (1 + tp_pct), color=C_GREEN,
                       linestyle=':', linewidth=0.7, alpha=0.4)

        # ── 9. Axes formatting ───────────────────────────────────────────────
        ax.xaxis.set_major_formatter(
            mdates.DateFormatter('%H:%M' if tf in ('1m', '5m') else '%m/%d'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(30)
            lbl.set_color(C_MUTED)
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")
        for lbl in ax.get_yticklabels():
            lbl.set_color(C_MUTED)

        # Robust Y-axis: use 1st–99th percentile of visible candle prices so
        # a single spike candle doesn't compress the rest of the chart.
        if not self._zoom_locked and len(lows) > 4:
            _p_lo = float(np.percentile(lows,  1))
            _p_hi = float(np.percentile(highs, 99))
            _pad  = (_p_hi - _p_lo) * 0.06
            ax.set_ylim(_p_lo - _pad, _p_hi + _pad)

        # Restore pan/zoom from the pre-clear snapshot (saved before ax.clear()).
        if self._zoom_locked and _pre_xl is not None:
            ax.set_xlim(_pre_xl)
            ax.set_ylim(_pre_yl)

        # ── 9a. Live price label — drawn after set_ylim so y position is correct ──
        # The label is clamped to within the visible y-range so it's always shown
        # even when the chart is zoomed to a region that doesn't include the price.
        if price:
            import matplotlib.transforms as _mt
            _yl_now    = ax.get_ylim()
            _label_y   = max(_yl_now[0], min(_yl_now[1], price))
            _blended   = _mt.blended_transform_factory(ax.transAxes, ax.transData)
            _price_col = C_GREEN if price >= closes[-1] else C_RED
            ax.annotate(f" {format_price(price)} ",
                        xy=(1.0, _label_y),
                        xycoords=_blended,
                        fontsize=7.5, color="#ffffff", alpha=0.95,
                        ha='left', va='center', clip_on=False,
                        annotation_clip=False,
                        bbox=dict(boxstyle='square,pad=0.3', fc=_price_col,
                                  ec=_price_col, alpha=0.95, lw=0))

        zoom_hint   = "  [zoomed]" if self._zoom_locked else ""
        ax.set_title(
            f"{pair}  ·  {tf.upper()}  ·  {len(data)} candles{zoom_hint}",
            color=C_TEXT, fontsize=10, pad=6)

        # ── 9b. Last-signal badge (top-right of chart) ───────────────────────
        # Only show confirmed executed trades for this pair — no historical fallback.
        _es = self.last_executed_signal   # {'pair','side','price','qty','ts','source'}
        _badge_shown = False
        if _es and _es.get('pair') == pair:
            _la = _es['side']
            _lp = format_price(_es['price'])
            _lt = datetime.fromtimestamp(_es['ts'], pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')
            _lc = C_GREEN if _la == 'buy' else C_RED
            _lm = "▲ EXECUTED BUY" if _la == 'buy' else "▼ EXECUTED SELL"
            _src = _es.get('source', '')
            ax.text(0.99, 0.985, f"{_lm}  {_lp}",
                    transform=ax.transAxes,
                    color=_lc, fontsize=7.5, ha='right', va='top', fontweight='bold',
                    bbox=dict(boxstyle='round,pad=0.3', fc=C_BG, ec=_lc, alpha=0.88, lw=0.8))
            _ts_line = _lt + (f"  [{_src}]" if _src else "")
            ax.text(0.99, 0.955, _ts_line,
                    transform=ax.transAxes,
                    color=C_MUTED, fontsize=6.5, ha='right', va='top',
                    bbox=dict(boxstyle='round,pad=0.2', fc=C_BG, ec='none', alpha=0.8))
            _badge_shown = True

        if not _badge_shown and not is_signal_tf:
            ax.text(0.99, 0.985,
                    f"Signals on {self.signal_tf} only",
                    transform=ax.transAxes,
                    color=C_MUTED, fontsize=7, ha='right', va='top',
                    bbox=dict(boxstyle='round,pad=0.25', fc=C_BG, ec=C_BORDER, alpha=0.8))

        # ── RSI + ADX overlay (top-left) ─────────────────────────────────────
        _rsi_val = ind.get('rsi', None)
        _adx_val = ind.get('adx', None)
        _overlay_parts = []
        _overlay_col   = C_MUTED
        if _rsi_val is not None and not np.isnan(_rsi_val):
            _rsi_col = (C_RED   if _rsi_val > 70 else
                        C_GREEN if _rsi_val < 30 else C_MUTED)
            _rsi_zone = (" OB" if _rsi_val > 70 else
                         " OS" if _rsi_val < 30 else "")
            _overlay_parts.append(f"RSI {_rsi_val:.1f}{_rsi_zone}")
            _overlay_col = _rsi_col
        if _adx_val is not None and not np.isnan(_adx_val):
            _adx_str = (f"ADX {_adx_val:.1f}"
                        + (" strong" if _adx_val >= 40 else
                           " trend"  if _adx_val >= 20 else
                           " chop"))
            _overlay_parts.append(_adx_str)
        if _overlay_parts:
            ax.text(0.01, 0.99, "   ".join(_overlay_parts),
                    transform=ax.transAxes,
                    color=_overlay_col, fontsize=7, ha='left', va='top',
                    bbox=dict(boxstyle='round,pad=0.25', fc=C_BG, ec=_overlay_col,
                              alpha=0.82, lw=0.7))

        # ── 10. Key panel ────────────────────────────────────────────────────
        ka = self.key_ax
        ka.clear()
        ka.set_facecolor(C_PANEL)
        ka.axis('off')
        ka.set_xlim(0, 1)
        ka.set_ylim(0, 1)

        ka.axvline(x=0.04, color=C_BORDER, linewidth=0.7, alpha=0.4)

        items = []   # (kind, color, text)
        items.append(('hdr',  C_ACCENT,    "KEY"))
        items.append(('sp',   None,        None))
        items.append(('sym',  C_GREEN,     "▮  Bull Candle (HA)"))
        items.append(('sym',  C_RED,       "▮  Bear Candle (HA)"))
        items.append(('sp',   None,        None))
        for p, c in zip(MA_PERIODS, ma_colors):
            if len(ma_lines.get(p, [])) > 0:
                items.append(('sym', c, f"──  MA {p}"))
        items.append(('sp',   None,        None))

        # Signal legend — markers only appear on signal TF view
        if buy_signal_drawn:
            items.append(('sym', C_GREEN,   "▲  BUY"))
        if sell_signal_drawn:
            items.append(('sym', C_RED,     "▼  SELL"))
        if breakout_drawn:
            items.append(('sym', C_ACCENT3, "⚡  Breakout"))
        if is_signal_tf and (buy_signal_drawn or sell_signal_drawn or breakout_drawn):
            items.append(('inf', C_MUTED,   "conf TF ✓"))
        items.append(('sp', None, None))

        if ob_bull_drawn:
            items.append(('sym', C_GREEN,  "▪  OB↑ Bull Block"))
        if ob_bear_drawn:
            items.append(('sym', C_RED,    "▪  OB↓ Bear Block"))
        if fvg_bull_drawn:
            items.append(('sym', "#00bcd4","▪  FVG↑ Bull FVG"))
        if fvg_bear_drawn:
            items.append(('sym', "#e040fb","▪  FVG↓ Bear FVG"))
        if ob_bull_drawn or ob_bear_drawn or fvg_bull_drawn or fvg_bear_drawn:
            items.append(('sp', None, None))
        if sr_sup_drawn:
            items.append(('sym', C_GREEN,  "╌╌  Support"))
        if sr_res_drawn:
            items.append(('sym', C_RED,    "╌╌  Resistance"))
        items.append(('sp',   None,        None))
        items.append(('sym',  "#cccccc",   "⋯   Live Price"))
        if atr > 0:
            items.append(('sym', C_RED,   f"⋯   SL  {format_price(price*(1-1.5*atr/price)) if price else '1.5×ATR'}"))
            items.append(('sym', C_GREEN, f"⋯   TP  {format_price(price*(1+3.0*atr/price)) if price else '3×ATR'}"))
        if self._signal_data:
            items.append(('sp',  None,    None))
            items.append(('inf', C_MUTED, "Hover ▲▼⚡ for"))
            items.append(('inf', C_MUTED, "signal details"))

        # ── Legend items loop ─────────────────────────────────────────────────
        # Reserve bottom 22% of key panel for the LAST SIGNAL block — items
        # only use the top 78% so they can never push the signal block off-screen.
        step = 0.76 / max(len(items), 1)
        y    = 0.97
        _x   = 0.16   # left indent — clears the border line
        for kind, color, text in items:
            if kind == 'sp':
                y -= step * 0.6
                continue
            if kind == 'hdr':
                ka.text(0.5, y, text, transform=ka.transAxes,
                        color=color, fontsize=8, fontweight='bold',
                        ha='center', va='top', clip_on=True)
            elif kind == 'inf':
                ka.text(_x, y, text, transform=ka.transAxes,
                        color=color, fontsize=6.5, va='top', ha='left',
                        fontfamily='monospace', clip_on=True)
            else:  # 'sym'
                ka.text(_x, y, text, transform=ka.transAxes,
                        color=color, fontsize=6.8, va='top', ha='left', clip_on=True)
            y -= step

        # ── Last signal block (fixed at bottom of key panel) ──────────────────
        # Priority order:
        #   1. Most recent signal arrow from _signal_data for this pair/TF
        #      (catches BUY/SELL arrows even when no trade was filled)
        #   2. last_executed_signal if it matches this pair (actual fill)
        #   3. trade_history scan for this pair
        _es_key = None

        # 1. Most recent arrow from the current chart render
        if self._signal_data:
            _sd_best = max(self._signal_data, key=lambda s: s['ts'])
            _es_key = {
                'side':  _sd_best['action'],
                'price': _sd_best['candle_price'],
                'ts':    _sd_best['ts'].timestamp() if hasattr(_sd_best['ts'], 'timestamp') else 0,
            }

        # 2. Actual fill for this pair (may be more recent than chart signals)
        _es = self.last_executed_signal
        if _es and _es.get('pair') == pair and _es.get('price'):
            _fill_ts = _es.get('ts', 0)
            _chart_ts = _es_key['ts'] if _es_key else 0
            if _fill_ts >= _chart_ts:
                _es_key = {
                    'side':  _es['side'],
                    'price': _es['price'],
                    'ts':    _fill_ts,
                }

        # 3. trade_history scan (open positions for this pair)
        if _es_key is None:
            _best_ts, _best_trade = -1, None
            for _t in self.trade_history.values():
                if _t.get('symbol') == pair or _t.get('pair') == pair:
                    _t_ts = float(_t.get('timestamp', 0))
                    if _t_ts > _best_ts:
                        _best_ts, _best_trade = _t_ts, _t
            if _best_trade:
                _t_ms = float(_best_trade.get('timestamp', 0))
                if _t_ms > 1e12:
                    _t_ms /= 1000.0
                _es_key = {
                    'side':  _best_trade.get('side', ''),
                    'price': float(_best_trade.get('entry_price',
                                   _best_trade.get('price', 0))),
                    'ts':    _t_ms,
                }

        # Separator line above the signal block
        ka.plot([0.04, 0.96], [0.21, 0.21], color=C_BORDER, linewidth=0.6, alpha=0.6,
                transform=ka.transAxes, clip_on=False)

        if _es_key and _es_key.get('side') and _es_key.get('price'):
            _la = _es_key['side']
            _lp = format_price(_es_key['price'])
            try:
                _lt = datetime.fromtimestamp(_es_key['ts'], pytz.UTC).strftime('%m-%d %H:%M')
            except Exception:
                _lt = ''
            _lc = C_GREEN if _la == 'buy' else C_RED
            _lm = "▲ BUY" if _la == 'buy' else "▼ SELL"
            ka.text(0.5, 0.195, "LAST SIGNAL", transform=ka.transAxes,
                    color=_lc, fontsize=7, fontweight='bold',
                    ha='center', va='top', clip_on=False)
            ka.text(0.5, 0.145, f"{_lm}  {_lp}", transform=ka.transAxes,
                    color=_lc, fontsize=6.8, fontweight='bold',
                    ha='center', va='top', clip_on=False)
            if _lt:
                ka.text(0.5, 0.10, _lt, transform=ka.transAxes,
                        color=C_MUTED, fontsize=6.2,
                        ha='center', va='top', clip_on=False)
        else:
            ka.text(0.5, 0.195, "No signals yet", transform=ka.transAxes,
                    color=C_MUTED, fontsize=6.5,
                    ha='center', va='top', clip_on=False)

        # Explicit margins so key panel never overlaps chart area
        self.chart_fig.subplots_adjust(left=0.01, right=0.99, top=0.94, bottom=0.10, wspace=0.06)
        try:
            self.chart_canvas.draw_idle()
        except Exception:
            pass

    # ── Live chart 1-second refresh ──────────────────────────────────────────
    def _chart_live_tick(self):
        """Redraw the chart every second while the Charts page is active.

        This keeps the forming candle and price label current without waiting
        for a REST candle close (which only triggers every 5 min on 1h TF).
        The forming candle is synthesized from WS ticks in _refresh_chart.
        """
        if self.root_alive and self._active_page == 'charts':
            try:
                self._refresh_chart()
            except Exception:
                pass
        if self.root_alive:
            self.root.after(1000, self._chart_live_tick)

    # ── Order book popup ──────────────────────────────────────────────────────
    def _open_orderbook(self):
        """Open a live order book popup for the currently selected chart pair.

        Data comes from the level2 WebSocket channel already subscribed in
        _websocket_loop. Display updates every 250ms via _update_ob_display.
        """
        pair = self.chart_pair_var.get()
        self._ob_pair = pair

        # Bring existing window forward if already open for the same pair
        if self._ob_window is not None:
            try:
                self._ob_window.lift()
                return
            except Exception:
                self._ob_window = None

        win = ctk.CTkToplevel(self)
        win.title(f"Order Book  —  {pair}")
        win.geometry("460x620")
        win.resizable(True, True)
        win.configure(fg_color=C_BG)
        self._ob_window = win

        def _on_close():
            self._ob_window = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # ── Header ─────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(win, fg_color=C_PANEL, corner_radius=0)
        hdr.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(hdr, text=f"{pair}  Order Book",
                     font=("Segoe UI", 13, "bold"), text_color=C_TEXT).pack(
            side="left", padx=14, pady=8)
        self._ob_spread_lbl = ctk.CTkLabel(
            hdr, text="Spread: —", font=("Segoe UI", 10), text_color=C_MUTED)
        self._ob_spread_lbl.pack(side="right", padx=14)

        # ── Asks — scrollable, worst ask at top ────────────────────────────
        ctk.CTkLabel(win, text="  Price (Ask)              Size         Total",
                     font=("Segoe UI Mono", 9), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=10, pady=(4, 0))
        ask_scroll = ctk.CTkScrollableFrame(win, fg_color="transparent", height=200)
        ask_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 0))
        self._ob_ask_rows = []
        for _ in range(20):
            lbl = ctk.CTkLabel(ask_scroll, text="", font=("Segoe UI Mono", 10),
                               text_color=C_RED, anchor="w")
            lbl.pack(fill="x", pady=1)
            self._ob_ask_rows.append(lbl)

        # ── Mid-price ───────────────────────────────────────────────────────
        mid_frame = ctk.CTkFrame(win, fg_color=C_CARD2, corner_radius=0, height=30)
        mid_frame.pack(fill="x", padx=0, pady=4)
        self._ob_mid_lbl = ctk.CTkLabel(
            mid_frame, text="—", font=("Segoe UI", 12, "bold"), text_color=C_TEXT)
        self._ob_mid_lbl.pack(side="left", padx=14, pady=4)
        self._ob_imb_lbl = ctk.CTkLabel(
            mid_frame, text="", font=("Segoe UI", 10), text_color=C_MUTED)
        self._ob_imb_lbl.pack(side="right", padx=14)

        # ── Bids — scrollable ──────────────────────────────────────────────
        ctk.CTkLabel(win, text="  Price (Bid)              Size         Total",
                     font=("Segoe UI Mono", 9), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=10, pady=(0, 0))
        bid_scroll = ctk.CTkScrollableFrame(win, fg_color="transparent", height=200)
        bid_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self._ob_bid_rows = []
        for _ in range(20):
            lbl = ctk.CTkLabel(bid_scroll, text="", font=("Segoe UI Mono", 10),
                               text_color=C_GREEN, anchor="w")
            lbl.pack(fill="x", pady=1)
            self._ob_bid_rows.append(lbl)

        # Start live refresh loop
        self._ob_refresh_loop(win)

    def _ob_refresh_loop(self, win):
        """Poll order book display every 250ms while the popup is open."""
        if self._ob_window is None:
            return
        try:
            win.winfo_exists()
        except Exception:
            self._ob_window = None
            return
        self._update_ob_display()
        try:
            win.after(250, lambda: self._ob_refresh_loop(win))
        except Exception:
            self._ob_window = None

    def _update_ob_display(self):
        """Refresh the order book labels with latest level2 data."""
        if self._ob_window is None:
            return
        pair = getattr(self, '_ob_pair', '')
        ob   = self._order_book.get(pair, {'bids': [], 'asks': []})
        bids = ob['bids'][:20]
        asks = ob['asks'][:20]

        # Asks shown top-to-bottom: worst ask first (highest price at top)
        asks_display = list(reversed(asks[:20]))

        def _fmt_row(price, qty, cum):
            return f"  {format_price(price):<18}  {qty:>10.4f}  {cum:>10.4f}"

        cum = 0.0
        for i, lbl in enumerate(self._ob_ask_rows):
            if i < len(asks_display):
                _p, _q = asks_display[i]
                cum += _q
                lbl.configure(text=_fmt_row(_p, _q, cum))
            else:
                lbl.configure(text="")

        cum = 0.0
        for i, lbl in enumerate(self._ob_bid_rows):
            if i < len(bids):
                _p, _q = bids[i]
                cum += _q
                lbl.configure(text=_fmt_row(_p, _q, cum))
            else:
                lbl.configure(text="")

        try:
            best_ask = asks[0][0] if asks else 0
            best_bid = bids[0][0] if bids else 0
            mid      = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
            spread   = best_ask - best_bid if best_bid and best_ask else 0
            self._ob_mid_lbl.configure(text=format_price(mid) if mid else "—")
            self._ob_spread_lbl.configure(
                text=f"Spread: {format_price(spread)}" if spread else "Spread: —")

            # Order imbalance ratio
            bid_vol = sum(q for _, q in bids)
            ask_vol = sum(q for _, q in asks)
            total   = bid_vol + ask_vol
            if total > 0:
                imb = bid_vol / total
                imb_col  = C_GREEN if imb > 0.55 else (C_RED if imb < 0.45 else C_MUTED)
                imb_side = "BID heavy" if imb > 0.55 else ("ASK heavy" if imb < 0.45 else "balanced")
                self._ob_imb_lbl.configure(
                    text=f"Imbalance  {imb:.0%} bid  ({imb_side})",
                    text_color=imb_col)
        except Exception:
            pass

    # ── Signal hover tooltip ──────────────────────────────────────────────────
    def _on_chart_hover(self, event):
        """Show tooltip when hovering near a buy/sell signal. Throttled to ~30fps."""
        now = time.monotonic()
        # Skip if a pan is in progress — avoids fighting draw_idle calls
        if self._pan_start is not None:
            return
        # Throttle hover redraws to the global frame budget (144 fps)
        if now - self._hover_last_t < _FRAME_DT:
            return

        ax = self.chart_ax
        outside = (event.inaxes != ax or not self._signal_data
                   or event.xdata is None or event.ydata is None)

        if outside:
            if self._hover_active:
                # Only redraw if we were showing a tooltip
                if self._hover_ann:
                    try:
                        self._hover_ann.remove()
                    except Exception:
                        pass
                    self._hover_ann = None
                self._hover_active = False
                self._hover_last_t = now
                try:
                    self.chart_canvas.draw_idle()
                except Exception:
                    pass
            return

        xlim   = ax.get_xlim()
        ylim   = ax.get_ylim()
        x_span = xlim[1] - xlim[0]
        y_span = ylim[1] - ylim[0]
        if x_span == 0 or y_span == 0:
            return

        # Find nearest signal within proximity threshold
        closest   = None
        best_dist = float('inf')
        for sig in self._signal_data:
            x_num = mdates.date2num(sig['ts'])
            dx    = abs(x_num - event.xdata) / x_span
            dy    = abs(sig['price'] - event.ydata) / y_span
            dist  = (dx ** 2 + dy ** 2) ** 0.5
            if dist < best_dist and dx < 0.04:
                best_dist = dist
                closest   = sig

        want_tooltip = (closest is not None and best_dist <= 0.06)

        # Remove any stale tooltip
        if self._hover_ann:
            try:
                self._hover_ann.remove()
            except Exception:
                pass
            self._hover_ann = None

        if not want_tooltip:
            if self._hover_active:
                self._hover_active = False
                self._hover_last_t = now
                try:
                    self.chart_canvas.draw_idle()
                except Exception:
                    pass
            return

        action = closest['action']
        color  = C_GREEN if action == 'buy' else C_RED
        header = "▲  BUY SIGNAL" if action == 'buy' else "▼  SELL SIGNAL"

        cp = closest['candle_price']
        sl_val = (cp * (1 - 1.5 * closest['atr'] / cp)
                  if closest['atr'] > 0 and cp > 0 else 0)
        tp_val = (cp * (1 + 3.0 * closest['atr'] / cp)
                  if closest['atr'] > 0 and cp > 0 else 0)

        src = closest.get('source', 'MA+SMC')
        lines = [
            f"  {header}  ",
            f"  {'─' * (len(header) + 2)}  ",
            f"  Source {src}  ",
            f"  Time   {closest['time_str']}  ",
            f"  Price  {closest['price_str']}  ",
            f"  MA {closest.get('p_fast', 'fast'):<4} {format_price(closest['ma9'])}  ",
            f"  MA {closest.get('p_slow', 'slow'):<4} {format_price(closest['ma20'])}  ",
        ]
        if closest['atr'] > 0:
            lines += [
                f"  ATR   {format_price(closest['atr'])}  ",
                f"  SL    {format_price(sl_val)}  ",
                f"  TP    {format_price(tp_val)}  ",
            ]
        label = "\n".join(lines)

        y_offset = y_span * 0.22
        ty  = (closest['price'] + y_offset if action == 'buy'
               else closest['price'] - y_offset)
        va  = 'bottom' if action == 'buy' else 'top'

        self._hover_ann = ax.annotate(
            label,
            xy=(closest['ts'], closest['price']),
            xytext=(closest['ts'], ty),
            color=C_TEXT, fontsize=7.5, fontfamily='monospace',
            ha='center', va=va,
            bbox=dict(boxstyle='round,pad=0.6', fc=C_CARD, ec=color,
                      alpha=0.97, lw=1.8),
            arrowprops=dict(arrowstyle='->', color=color, lw=1.4,
                            connectionstyle='arc3,rad=0.0'),
            zorder=25,
        )
        self._hover_active = True
        self._hover_last_t = now
        try:
            self.chart_canvas.draw_idle()
        except Exception:
            pass

    # ── Chart interaction: scroll-to-zoom, left-click-drag-pan ───────────────
    def _on_chart_scroll(self, event):
        """Scroll wheel zooms in/out centred on the cursor position."""
        ax = self.chart_ax
        if event.inaxes != ax:
            return
        self._zoom_locked = True
        factor = 0.85 if event.button == 'up' else 1.0 / 0.85

        xl = ax.get_xlim()
        yl = ax.get_ylim()
        cx, cy = event.xdata, event.ydata

        ax.set_xlim([cx - (cx - xl[0]) * factor,
                     cx + (xl[1] - cx) * factor])
        ax.set_ylim([cy - (cy - yl[0]) * factor,
                     cy + (yl[1] - cy) * factor])
        try:
            self.chart_canvas.draw_idle()
        except Exception:
            pass

    def _on_chart_press(self, event):
        """Record pixel position at press — pixel coords are stable across pans."""
        if event.inaxes != self.chart_ax:
            return
        if event.button in (1, 2, 3):
            self._pan_start = (event.x, event.y)   # pixels, not data coords
            self._pan_xlim  = self.chart_ax.get_xlim()
            self._pan_ylim  = self.chart_ax.get_ylim()

    def _on_chart_release(self, event):
        if event.button in (1, 2, 3):
            self._pan_start = None

    def _on_chart_drag(self, event):
        """Pan by total pixel displacement from press point → data units.
        Pixel coords are stable; xdata drifts as axes shift (old glitch source).
        Tkinter event.y origin is TOP (increases downward), matplotlib y is BOTTOM
        (increases upward) — no manual negation needed; subtraction handles it."""
        if self._pan_start is None:
            return
        ax   = self.chart_ax
        bbox = ax.get_window_extent()
        if bbox.width == 0 or bbox.height == 0:
            return
        xl = self._pan_xlim
        yl = self._pan_ylim
        dx_data = (event.x - self._pan_start[0]) * (xl[1] - xl[0]) / bbox.width
        dy_data = (event.y - self._pan_start[1]) * (yl[1] - yl[0]) / bbox.height
        ax.set_xlim([xl[0] - dx_data, xl[1] - dx_data])
        ax.set_ylim([yl[0] - dy_data, yl[1] - dy_data])
        self._zoom_locked = True
        now = time.monotonic()
        if now - self._drag_last_draw < _FRAME_DT:
            return
        self._drag_last_draw = now
        try:
            # draw() + flush_events() = synchronous render, no Tk queue backlog
            self.chart_canvas.draw()
            self.chart_canvas.flush_events()
        except Exception:
            pass

    def _chart_dbl_click(self, event):
        """Double-click resets pan/zoom back to auto-fit."""
        if event.dblclick and event.inaxes == self.chart_ax:
            self._zoom_locked = False
            self._pan_start   = None
            self._refresh_chart()

    # ── Trade rows ────────────────────────────────────────────────────────────
    def _refresh_trade_rows(self):
        active = {tid for tid, t in self.trade_history.items() if t.get('event') == 'trade'}
        for tid in list(self._trade_rows):
            if tid not in active:
                try: self._trade_rows[tid].destroy()
                except Exception: pass
                del self._trade_rows[tid]

        for tid, trade in self.trade_history.items():
            if trade.get('event') != 'trade':
                continue
            pair   = trade['symbol']
            side   = trade['side']
            qty    = trade['quantity']
            entry  = trade['entry_price']
            cur    = trade.get('current_price', entry)
            sl, tp = trade['stop_loss'], trade['take_profit']
            pl     = trade.get('pl', 0.0)
            pc     = C_GREEN if pl >= 0 else C_RED

            if tid not in self._trade_rows:
                rf = ctk.CTkFrame(self.trade_scroll, fg_color=C_CARD, corner_radius=8)
                rf.pack(fill="x", pady=2)
                lbls = {}
                for col in ["pair","side","qty","entry","cur","pl","sl","tp"]:
                    lb = ctk.CTkLabel(rf, text="", width=100,
                                      font=("Segoe UI", 11), text_color=C_TEXT)
                    lb.pack(side="left", padx=4, pady=8)
                    lbls[col] = lb
                rf._labels = lbls
                self._trade_rows[tid] = rf

            lb = self._trade_rows[tid]._labels
            lb['pair'].configure(text=pair)
            lb['side'].configure(text=side.upper(),
                                  text_color=C_GREEN if side == 'buy' else C_RED)
            lb['qty'].configure(text=f"{qty:.5f}")
            lb['entry'].configure(text=format_price(entry))
            lb['cur'].configure(text=format_price(cur))
            lb['pl'].configure(text=f"${pl:+.2f}", text_color=pc)
            lb['sl'].configure(text=format_price(sl))
            lb['tp'].configure(text=format_price(tp))

    # ── Allocation dialogs ────────────────────────────────────────────────────
    def _popup(self, title: str, w: int, h: int) -> ctk.CTkToplevel:
        """Create a properly focused modal dialog."""
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry(f"{w}x{h}")
        win.configure(fg_color=C_PANEL)
        win.resizable(False, False)
        # Centre over parent
        self.update_idletasks()
        px = self.winfo_rootx() + (self.winfo_width()  - w) // 2
        py = self.winfo_rooty() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{px}+{py}")
        win.lift()
        win.focus_force()
        win.grab_set()
        return win

    def _open_allocate(self, default_pair: str = None):
        win = self._popup("Allocate to Bot", 460, 560)

        ctk.CTkLabel(win, text="＋  Allocate to Bot",
                     font=("Segoe UI", 15, "bold"), text_color=C_TEXT).pack(pady=(18, 0))

        # ── Mode toggle ───────────────────────────────────────────────────────
        mode_var = ctk.StringVar(value="USD Budget")
        mode_bar = ctk.CTkSegmentedButton(
            win, values=["USD Budget", "Coin Holdings"],
            variable=mode_var, width=300, height=34)
        mode_bar.pack(pady=(8, 6))

        outer = ctk.CTkFrame(win, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=28)

        pair_var = ctk.StringVar(value=default_pair or TRADING_PAIRS[0])

        # ── Mode-specific area (always at the top of outer) ───────────────────
        mode_area = ctk.CTkFrame(outer, fg_color="transparent")
        mode_area.pack(fill="x")

        # ── USD Budget ────────────────────────────────────────────────────────
        usd_frame       = ctk.CTkFrame(mode_area, fg_color="transparent")
        _usd_bot_lbl_ref   = [None]   # mutable ref for bot wallet label
        _usd_source_cards  = []       # list of (key, btn_widget, balance) — rebuilt each time
        usd_source_var     = ctk.StringVar(value="Total")  # "Liquid USD" | "USDC" | "USDT" | "Total"

        def _source_balance() -> float:
            """Return the balance of the currently selected USD source."""
            raw_usd = max(0.0, self.usd_balance - self.usdc_balance - self.usdt_balance)
            src = usd_source_var.get()
            if src == "Liquid USD": return raw_usd
            if src == "USDC":       return self.usdc_balance
            if src == "USDT":       return self.usdt_balance
            return self.usd_balance   # "Total"

        def _highlight_source_cards():
            sel = usd_source_var.get()
            for key, btn, _ in _usd_source_cards:
                active = (key == sel)
                btn.configure(
                    fg_color=C_ACCENT2 if active else C_CARD,
                    border_color=C_ACCENT2 if active else C_BORDER,
                    border_width=2 if active else 1)

        def _select_source(key):
            usd_source_var.set(key)
            _highlight_source_cards()
            _update_qfill()
            if alloc_all_var.get():
                _fill_all_amount()

        def _build_usd_frame():
            _usd_source_cards.clear()
            for w2 in usd_frame.winfo_children():
                w2.destroy()
            raw_usd = max(0.0, self.usd_balance - self.usdc_balance - self.usdt_balance)

            bal_row = ctk.CTkFrame(usd_frame, fg_color="transparent")
            bal_row.pack(fill="x", pady=(0, 4))
            for lbl, val in [("Liquid USD", raw_usd),
                              ("USDC",       self.usdc_balance),
                              ("USDT",       self.usdt_balance)]:
                btn = ctk.CTkButton(
                    bal_row, text=f"{lbl}\n${val:,.2f}",
                    height=54, corner_radius=10,
                    fg_color=C_CARD, hover_color=C_BORDER,
                    border_width=1, border_color=C_BORDER,
                    font=("Segoe UI", 11, "bold"), text_color=C_ACCENT3,
                    command=lambda k=lbl: _select_source(k))
                btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
                _usd_source_cards.append((lbl, btn, val))

            ctk.CTkLabel(usd_frame,
                         text=f"Total liquid:  ${self.usd_balance:,.2f}",
                         font=("Segoe UI", 11), text_color=C_MUTED).pack(anchor="w", pady=(2, 6))

            prow = ctk.CTkFrame(usd_frame, fg_color="transparent")
            prow.pack(fill="x", pady=(0, 4))
            ctk.CTkLabel(prow, text="Allocate to pair:", font=("Segoe UI", 12),
                         text_color=C_TEXT).pack(side="left", padx=(0, 8))
            _pair_labels = [p.split("-")[0] for p in TRADING_PAIRS]
            _seg = ctk.CTkSegmentedButton(
                prow, values=_pair_labels, height=30,
                command=lambda coin: (pair_var.set(f"{coin}-USD"), _update_bot_lbl()))
            _sel_coin = pair_var.get().split("-")[0]
            if _sel_coin in _pair_labels:
                _seg.set(_sel_coin)
            else:
                _seg.set(_pair_labels[0])
                pair_var.set(TRADING_PAIRS[0])
            _seg.pack(side="left")

            bot_lbl = ctk.CTkLabel(usd_frame, text="", font=("Segoe UI", 10),
                                   text_color=C_MUTED)
            bot_lbl.pack(anchor="w", pady=(0, 4))
            _usd_bot_lbl_ref[0] = bot_lbl
            _update_bot_lbl()
            # Restore highlight after rebuild
            _highlight_source_cards()

        def _update_bot_lbl():
            p    = pair_var.get()
            coin = p.split("-")[0]
            if _usd_bot_lbl_ref[0]:
                _usd_bot_lbl_ref[0].configure(
                    text=f"Bot wallet ({coin}):  ${self.bot_pair_alloc.get(p,0):,.2f}")

        # ── Coin Holdings ─────────────────────────────────────────────────────
        coin_frame  = ctk.CTkFrame(mode_area, fg_color="transparent")
        pair_row_h  = ctk.CTkFrame(coin_frame, fg_color="transparent")
        pair_row_h.pack(fill="x", pady=(0, 4))
        info_lbl_h  = ctk.CTkLabel(coin_frame, text="", font=("Segoe UI", 11),
                                   text_color=C_MUTED)
        info_lbl_h.pack(anchor="w", pady=(0, 4))

        def _make_coin_buttons():
            for w2 in pair_row_h.winfo_children():
                w2.destroy()
            for p in TRADING_PAIRS:
                coin  = p.split("-")[0]
                price = self.live_prices.get(p, 0)
                holdings_qty = self.real_exposure.get(p, 0) / price if price else 0
                sub   = f"{holdings_qty:.4f} {coin}" if holdings_qty else "0"
                is_sel = (p == pair_var.get())
                btn = ctk.CTkButton(
                    pair_row_h, text=f"{coin}\n{sub}",
                    width=118, height=54, corner_radius=10,
                    fg_color=C_ACCENT2 if is_sel else C_CARD,
                    hover_color="#6a4de0" if is_sel else C_BORDER,
                    border_width=2 if is_sel else 1,
                    border_color=C_ACCENT2 if is_sel else C_BORDER,
                    font=("Segoe UI", 11, "bold"), text_color=C_TEXT,
                    command=lambda pp=p: _select_coin(pp))
                btn.pack(side="left", padx=(0, 8))
                btn._pair = p
            _refresh_coin_info()

        def _select_coin(p):
            pair_var.set(p)
            for b in pair_row_h.winfo_children():
                sel = b._pair == p
                b.configure(fg_color=C_ACCENT2 if sel else C_CARD,
                            hover_color="#6a4de0" if sel else C_BORDER,
                            border_width=2 if sel else 1,
                            border_color=C_ACCENT2 if sel else C_BORDER)
            _refresh_coin_info()
            _update_qfill()

        def _refresh_coin_info():
            p    = pair_var.get()
            coin = p.split("-")[0]
            price = self.live_prices.get(p, 0)
            qty   = self.real_exposure.get(p, 0) / price if price else 0
            info_lbl_h.configure(
                text=f"You hold: {qty:.6f} {coin}  ≈ ${self.real_exposure.get(p,0):,.2f}  ·  "
                     f"Bot manages: {self.bot_coin_qty.get(p,0):.6f} {coin}")

        # ── Static bottom area (amount, %, Allocate All toggle, confirm) ──────
        bottom = ctk.CTkFrame(outer, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))

        # Allocate All toggle row
        alloc_all_var = ctk.BooleanVar(value=False)
        alloc_all_row = ctk.CTkFrame(bottom, fg_color="transparent")
        alloc_all_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(alloc_all_row, text="Allocate All Available",
                     font=("Segoe UI", 12), text_color=C_ACCENT3).pack(side="left")
        alloc_all_sw = ctk.CTkSwitch(alloc_all_row, text="", variable=alloc_all_var,
                                     width=44, button_color=C_ACCENT3,
                                     progress_color=C_ACCENT2)
        alloc_all_sw.pack(side="right")

        # Amount label + entry
        input_lbl = ctk.CTkLabel(bottom, text="Amount in USD", font=("Segoe UI", 12),
                                 text_color=C_MUTED)
        input_lbl.pack(anchor="w", pady=(0, 4))
        amt = ctk.CTkEntry(bottom, placeholder_text="0.00", height=42,
                           fg_color=C_CARD, border_color=C_BORDER,
                           text_color=C_TEXT, font=("Segoe UI", 15))
        amt.pack(fill="x")
        amt.focus()

        # % quick-fill buttons
        qrow  = ctk.CTkFrame(bottom, fg_color="transparent")
        qrow.pack(fill="x", pady=(5, 0))
        qbtns = []
        for pct in (25, 50, 75, 100):
            b = ctk.CTkButton(qrow, text=f"{pct}%", width=72, height=28,
                              corner_radius=6, fg_color=C_CARD, hover_color=C_BORDER,
                              font=("Segoe UI", 11), text_color=C_MUTED)
            b.pack(side="left", padx=(0, 6))
            qbtns.append((pct, b))

        def _update_qfill():
            p    = pair_var.get()
            mode = mode_var.get()
            if mode == "USD Budget":
                total = _source_balance()
                for pct, b in qbtns:
                    v = f"{total * pct / 100:.2f}"
                    b.configure(state="normal",
                                command=lambda x=v: (amt.delete(0, "end"), amt.insert(0, x)))
            else:
                price = self.live_prices.get(p, 0)
                total = self.real_exposure.get(p, 0) / price if price else 0
                for pct, b in qbtns:
                    v = total * pct / 100
                    n = self.alloc_round_tokens
                    if n > 1:
                        import math as _math
                        v = _math.floor(v / n) * n
                    fmt = f"{v:.6f}"
                    b.configure(state="normal",
                                command=lambda x=fmt: (amt.delete(0, "end"), amt.insert(0, x)))

        def _fill_all_amount():
            mode = mode_var.get()
            if mode == "USD Budget":
                total = _source_balance()
                amt.delete(0, "end"); amt.insert(0, f"{total:.2f}")
            else:
                p = pair_var.get()
                price = self.live_prices.get(p, 0)
                total = self.real_exposure.get(p, 0) / price if price else 0
                n = self.alloc_round_tokens
                import math as _m2
                total = _m2.floor(total / n) * n if n > 1 else total
                amt.delete(0, "end"); amt.insert(0, f"{total:.6f}")

        def _on_alloc_all_toggle(*_):
            if alloc_all_var.get():
                # ON → fill amount, lock entry + % buttons + USD source cards
                # Pair selector (BTC/ETH/XCN) always stays interactive
                _fill_all_amount()
                amt.configure(state="disabled")
                for _, b in qbtns:
                    b.configure(state="disabled")
                # Lock only source cards in USD Budget mode
                if mode_var.get() == "USD Budget":
                    for _, btn, _ in _usd_source_cards:
                        btn.configure(state="disabled")
                # Coin Holdings: lock the coin source buttons
                else:
                    for b in pair_row_h.winfo_children():
                        b.configure(state="disabled")
            else:
                # OFF → unlock source cards, entry, % buttons
                # Pair selector (BTC/ETH/XCN) was never touched
                amt.configure(state="normal")
                for _, b in qbtns:
                    b.configure(state="normal")
                if mode_var.get() == "USD Budget":
                    for _, btn, _ in _usd_source_cards:
                        btn.configure(state="normal")
                else:
                    for b in pair_row_h.winfo_children():
                        b.configure(state="normal")

        alloc_all_var.trace_add("write", _on_alloc_all_toggle)

        def _on_mode_change(_=None):
            # Reset Allocate All toggle on mode switch
            alloc_all_var.set(False)
            # Swap mode frames — both live inside mode_area so pack ordering stays stable
            mode = mode_var.get()
            if mode == "USD Budget":
                coin_frame.pack_forget()
                _build_usd_frame()
                usd_frame.pack(fill="x", in_=mode_area)
                input_lbl.configure(text="Amount in USD")
                amt.configure(placeholder_text="0.00 USD", state="normal")
            else:
                usd_frame.pack_forget()
                _make_coin_buttons()
                coin_frame.pack(fill="x", in_=mode_area)
                p = pair_var.get(); coin = p.split("-")[0]
                input_lbl.configure(text=f"Amount in {coin}")
                amt.configure(placeholder_text=f"0.000000 {coin}", state="normal")
            amt.delete(0, "end")
            _update_qfill()

        mode_bar.configure(command=_on_mode_change)

        # Initial build
        _build_usd_frame()
        usd_frame.pack(fill="x", in_=mode_area)
        _update_qfill()

        err = ctk.CTkLabel(bottom, text="", text_color=C_RED, font=("Segoe UI", 11))
        err.pack(pady=(6, 2))

        def confirm():
            try:
                p     = pair_var.get()
                coin  = p.split("-")[0]
                mode  = mode_var.get()
                val   = float(amt.get())
                price = self.live_prices.get(p, 0)
                if val <= 0:
                    err.configure(text="Enter an amount greater than 0")
                    return
                if mode == "USD Budget":
                    avail = _source_balance()
                    src   = usd_source_var.get()
                    if val > avail + 1e-9:
                        err.configure(text=f"Max from {src}: ${avail:,.2f}")
                        return
                    # Deduct from overall usd_balance regardless of sub-bucket —
                    # usdc/usdt are already counted inside usd_balance at fetch time.
                    self.usd_balance       -= val
                    self.bot_pair_alloc[p] += val
                    src_tag = f" [{src}]" if src != "Total" else ""
                    self.log_message(
                        f"Allocated ${val:,.2f}{src_tag} → {coin} bot wallet", "trade")
                else:
                    # val = coin quantity; apply rounding before validation
                    n = self.alloc_round_tokens
                    if n > 1:
                        import math as _math
                        val = _math.floor(val / n) * n
                    if val <= 0:
                        err.configure(text=f"Amount rounds to 0 (step: {n} tokens)")
                        return
                    avail_qty = self.real_exposure.get(p, 0) / price if price else 0
                    if val > avail_qty + 1e-9:
                        err.configure(text=f"Max: {avail_qty:.6f} {coin}")
                        return
                    # Track how many coins the bot manages — do NOT touch real_exposure;
                    # those coins still exist in the Coinbase account and _fetch_balance
                    # is authoritative for their current market value.
                    self.bot_coin_qty[p] += val
                    self.log_message(
                        f"Allocated {val:.6f} {coin} holdings → bot", "trade")
                self._save_bot_state()
                self._update_metrics()
                win.destroy()
            except ValueError:
                err.configure(text="Enter a valid number")

        ctk.CTkButton(bottom, text="Confirm Allocation", height=44, corner_radius=10,
                      fg_color=C_ACCENT2, hover_color="#6a4de0",
                      font=("Segoe UI", 13, "bold"), command=confirm).pack(fill="x", pady=(0, 12))

    def _open_unallocate(self, default_pair: str = None):
        win = self._popup("Unallocate from Bot", 440, 400)

        ctk.CTkLabel(win, text="−  Unallocate Funds from Bot",
                     font=("Segoe UI", 15, "bold"), text_color=C_TEXT).pack(pady=(24, 2))

        total_alloc = sum(self.bot_pair_alloc.values()) + self.bot_balance
        ctk.CTkLabel(win, text=f"Total bot funds:  ${total_alloc:,.2f}",
                     font=("Segoe UI", 12), text_color=C_MUTED).pack(pady=(0, 4))

        form = ctk.CTkFrame(win, fg_color="transparent")
        form.pack(fill="x", padx=36, pady=(4, 0))

        # ── Coin selector ─────────────────────────────────────────────────────
        ctk.CTkLabel(form, text="Coin Wallet", font=("Segoe UI", 12),
                     text_color=C_MUTED).pack(anchor="w", pady=(0, 6))

        # Default to whichever pair has the most allocation
        if default_pair is None:
            default_pair = max(TRADING_PAIRS,
                               key=lambda p: self.bot_pair_alloc.get(p, 0))
        pair_var = ctk.StringVar(value=default_pair)

        pair_row = ctk.CTkFrame(form, fg_color="transparent")
        pair_row.pack(fill="x", pady=(0, 10))
        for p in TRADING_PAIRS:
            coin  = p.split("-")[0]
            alloc = self.bot_pair_alloc.get(p, 0)
            is_sel = (p == pair_var.get())
            btn = ctk.CTkButton(
                pair_row,
                text=f"{coin}\n${alloc:,.2f}",
                width=110, height=56, corner_radius=10,
                fg_color=C_ACCENT2 if is_sel else C_CARD,
                hover_color="#6a4de0" if is_sel else C_BORDER,
                border_width=2 if is_sel else 1,
                border_color=C_ACCENT2 if is_sel else C_BORDER,
                font=("Segoe UI", 11, "bold"), text_color=C_TEXT,
            )
            btn.pack(side="left", padx=(0, 8))
            btn._pair = p

        avail_lbl = ctk.CTkLabel(
            form,
            text=f"Available to unallocate: ${self.bot_pair_alloc.get(pair_var.get(), 0):,.2f}",
            font=("Segoe UI", 11), text_color=C_MUTED)
        avail_lbl.pack(anchor="w", pady=(0, 10))

        amt = ctk.CTkEntry(form, placeholder_text="0.00", height=42,
                           fg_color=C_CARD, border_color=C_BORDER,
                           text_color=C_TEXT, font=("Segoe UI", 15))

        qrow = ctk.CTkFrame(form, fg_color="transparent")
        qbtns_u = []
        for pct in (25, 50, 75, 100):
            b = ctk.CTkButton(qrow, text=f"{pct}%", width=70, height=28,
                              corner_radius=6, fg_color=C_CARD, hover_color=C_BORDER,
                              font=("Segoe UI", 11), text_color=C_MUTED)
            b.pack(side="left", padx=(0, 6))
            qbtns_u.append((pct, b))

        def _update_u_qfill():
            v_max = self.bot_pair_alloc.get(pair_var.get(), 0)
            for pct, b in qbtns_u:
                v = v_max * pct / 100
                b.configure(command=lambda x=v: (amt.delete(0, "end"),
                                                  amt.insert(0, f"{x:.2f}")))

        def _sel_pair_u(p, buttons):
            pair_var.set(p)
            avail_lbl.configure(
                text=f"Available to unallocate: ${self.bot_pair_alloc.get(p, 0):,.2f}")
            _update_u_qfill()
            for b in buttons:
                sel = b._pair == p
                b.configure(
                    fg_color=C_ACCENT2 if sel else C_CARD,
                    hover_color="#6a4de0" if sel else C_BORDER,
                    border_width=2 if sel else 1,
                    border_color=C_ACCENT2 if sel else C_BORDER,
                )

        u_btns = pair_row.winfo_children()
        for b in u_btns:
            b.configure(command=lambda p=b._pair, bs=u_btns: _sel_pair_u(p, bs))

        ctk.CTkLabel(form, text="Amount (USD)", font=("Segoe UI", 12),
                     text_color=C_MUTED).pack(anchor="w", pady=(0, 6))
        amt.pack(fill="x")
        amt.focus()
        qrow.pack(fill="x", pady=(6, 0))
        _update_u_qfill()

        err = ctk.CTkLabel(form, text="", text_color=C_RED, font=("Segoe UI", 11))
        err.pack(pady=(8, 4))

        def confirm():
            try:
                a = float(amt.get())
                p = pair_var.get()
                avail = self.bot_pair_alloc.get(p, 0)
                if a <= 0:
                    err.configure(text="Enter an amount greater than 0")
                    return
                if a > avail:
                    err.configure(text=f"Max available for {p.split('-')[0]}: ${avail:,.2f}")
                    return
                self.bot_pair_alloc[p] -= a
                self.usd_balance       += a
                self._save_bot_state()
                self._update_metrics()
                self.log_message(
                    f"Unallocated ${a:,.2f} from {p.split('-')[0]} → Liquid", "trade")
                win.destroy()
            except ValueError:
                err.configure(text="Enter a valid number")

        ctk.CTkButton(form, text="Confirm Unallocate", height=44, corner_radius=10,
                      fg_color=C_CARD, hover_color=C_BORDER,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 13, "bold"), text_color=C_TEXT,
                      command=confirm).pack(fill="x")

    def _confirm_sell_all(self):
        held = [p for p in TRADING_PAIRS
                if self.bot_exposure[p] > 0 or self.real_exposure[p] > 0]
        if not held:
            messagebox.showinfo("Nothing to Sell", "No positions held.", parent=self)
            return
        if messagebox.askyesno("Confirm Sell All",
                                f"Sell all holdings for {', '.join(held)}?",
                                parent=self):
            asyncio.run_coroutine_threadsafe(self._sell_all(held), self.loop)

    # ── Emergency / Resume ────────────────────────────────────────────────────
    def _set_status(self, text: str, color: str):
        """Update sidebar status dot + label together."""
        self.status_dot.configure(text_color=color)
        self.status_lbl2.configure(text=text, text_color=color)

    def emergency_stop(self):
        self.running = False
        self.paused  = True
        self._set_status("PAUSED", C_RED)
        self.resume_btn.configure(state="normal")
        self.log_message("Emergency Stop — trading halted", "warn")
        asyncio.run_coroutine_threadsafe(self._cancel_all_orders(), self.loop)

    def resume_trading(self):
        self.paused  = False
        self.running = True
        self._set_status("LIVE", C_GREEN)
        self.resume_btn.configure(state="disabled")
        self.log_message("Trading resumed", "trade")
        threading.Thread(target=self._run_backend_thread, daemon=True).start()

    def on_deposit(self, amount: float):
        self.usd_balance += amount
        self._update_metrics()
        self.log_message(f"Deposit received: ${amount:.2f}", "trade")

    def _save_settings(self):
        global STOP_LOSS_PCT, TAKE_PROFIT_PCT, ORDER_AMOUNT_USD, MINIMUM_RESERVE, COOLDOWN_SECONDS
        try:
            STOP_LOSS_PCT    = float(self.sl_var.get())  / 100
            TAKE_PROFIT_PCT  = float(self.tp_var.get())  / 100
            ORDER_AMOUNT_USD = float(self.ord_var.get())
            MINIMUM_RESERVE  = float(self.res_var.get())
            COOLDOWN_SECONDS = float(self.cd_var.get())
            self.alloc_round_tokens    = max(1, int(float(self.round_var.get())))
            self.auto_compound_enabled = bool(self.ac_enabled_var.get())
            self.auto_compound_pct     = max(0.1, min(100.0, float(self.ac_pct_var.get())))
            self.auto_compound_cap     = max(1.0, float(self.ac_cap_var.get()))
            import engine as _eng
            _eng.COOLDOWN_SECONDS = COOLDOWN_SECONDS
            self.signal_tf        = self.signal_tf_var.get()
            self.signal_direction = self.signal_dir_var.get()
            # Persist swap targets ('USD' maps to '' meaning no swap order placed)
            for pair in TRADING_PAIRS:
                chosen = self.swap_vars[pair].get()
                self.swap_targets[pair] = '' if chosen == 'USD' else chosen
            swap_summary = ', '.join(
                f"{p.split('-')[0]}→{self.swap_vars[p].get()}"
                for p in TRADING_PAIRS
                if self.swap_vars[p].get() != 'USD'
            ) or 'none'
            # ── Persist to config.json ────────────────────────────────────────
            save_user_settings({
                'signal_tf':               self.signal_tf,
                'signal_direction':        self.signal_direction,
                'ma_periods':              list(self.custom_ma_periods),
                'swap_targets':            dict(self.swap_targets),
                'stop_loss_pct':           STOP_LOSS_PCT,
                'take_profit_pct':         TAKE_PROFIT_PCT,
                'order_amount_usd':        ORDER_AMOUNT_USD,
                'minimum_reserve':         MINIMUM_RESERVE,
                'cooldown_seconds':        COOLDOWN_SECONDS,
                'alloc_round_tokens':      self.alloc_round_tokens,
                'auto_compound_enabled':   self.auto_compound_enabled,
                'auto_compound_pct':       self.auto_compound_pct,
                'auto_compound_cap':       self.auto_compound_cap,
            })
            self.log_message(
                f"Settings saved  ·  Signal TF: {self.signal_tf}  ·  Swaps: {swap_summary}",
                "trade")
        except ValueError:
            self.log_message("Invalid settings value", "error")

    def _apply_ma_settings(self):
        """Parse the MA periods entry, update MA_PERIODS globally, redraw chart."""
        global MA_PERIODS
        raw = self.ma_periods_var.get()
        try:
            periods = sorted(set(
                int(p.strip()) for p in raw.replace(';', ',').split(',')
                if p.strip().isdigit() and 1 < int(p.strip()) < 500
            ))[:3]   # cap at 3 MAs
            if not periods:
                raise ValueError("no valid periods")
        except Exception:
            self.log_message("MA Periods: enter comma-separated integers, e.g. 2, 5, 14", "error")
            return

        MA_PERIODS = periods
        import engine as _eng
        _eng.MA_PERIODS = periods
        self.custom_ma_periods = periods
        # Refresh display text (normalized)
        self.ma_periods_var.set(", ".join(str(p) for p in periods))
        # Persist MA change alongside current settings
        save_user_settings({
            'signal_tf':               self.signal_tf,
            'signal_direction':        self.signal_direction,
            'ma_periods':              list(periods),
            'swap_targets':            dict(self.swap_targets),
            'stop_loss_pct':           STOP_LOSS_PCT,
            'take_profit_pct':         TAKE_PROFIT_PCT,
            'order_amount_usd':        ORDER_AMOUNT_USD,
            'minimum_reserve':         MINIMUM_RESERVE,
            'cooldown_seconds':        COOLDOWN_SECONDS,
            'alloc_round_tokens':      self.alloc_round_tokens,
            'auto_compound_enabled':   self.auto_compound_enabled,
            'auto_compound_pct':       self.auto_compound_pct,
            'auto_compound_cap':       self.auto_compound_cap,
        })
        self.log_message(f"Moving averages updated: {periods}", "trade")
        self._refresh_chart()

    # ── 1-second status ticker (next window countdown + feed freshness) ──────
    def _tick_status(self):
        if not self.root_alive:
            return
        try:
            _tf_secs = {'1m': 60, '5m': 300, '1h': 3600, '1d': 86400}
            _period  = _tf_secs.get(self.signal_tf, 3600)
            _remain  = _period - (time.time() % _period)
            if _remain < 60:
                _next_txt = f"{int(_remain)}s"
            elif _remain < 3600:
                _next_txt = f"{int(_remain//60)}m {int(_remain%60)}s"
            else:
                _next_txt = f"{int(_remain//3600)}h {int((_remain%3600)//60)}m"
            self.bs_next_lbl.configure(
                text=_next_txt,
                text_color=C_ACCENT if _remain > 60 else C_ORANGE)

            _stale = [p.split('-')[0] for p in TRADING_PAIRS
                      if time.time() - self._price_ts.get(p, 0) > 15]
            if _stale:
                self.bs_feed_lbl.configure(
                    text=f"STALE ({', '.join(_stale)})", text_color=C_RED)
            else:
                _oldest = max((time.time() - self._price_ts.get(p, time.time()))
                              for p in TRADING_PAIRS) if self._price_ts else 0
                self.bs_feed_lbl.configure(
                    text=f"LIVE  {_oldest:.1f}s ago", text_color=C_GREEN)
        except Exception:
            pass
        self.root.after(1000, self._tick_status)

    # ── Backend startup ───────────────────────────────────────────────────────
    def _start_backend(self):
        threading.Thread(target=self._run_backend_thread, daemon=True).start()

    def _run_backend_thread(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.error(traceback.format_exc())
            if self.root_alive:
                self.root.after(0, self.log_message, f"Backend crashed: {e}", "error")

    async def _async_main(self):
        self.log_message("NEXXUS starting — connecting to Coinbase Advanced Trade…", "info")
        # Verify connection
        try:
            await asyncio.to_thread(self.client.get_product, "BTC-USD")
            self.log_message("Connected to Coinbase Advanced Trade ✓", "trade")
            if self.root_alive:
                self.root.after(0, lambda: self._set_status("LIVE", C_GREEN))
        except Exception as e:
            self.log_message(f"Connection failed: {e}", "error")
            if self.root_alive:
                self.root.after(0, lambda: self._set_status("ERROR", C_RED))
            return

        # Fetch REST prices first so balance calc has values for coin exposure
        await self._fetch_initial_prices()
        await self._fetch_balance()
        coin_val = sum(self.real_exposure[p] for p in TRADING_PAIRS)
        self.initial_balance = self.usd_balance + coin_val
        self.log_message(
            f"Liquid USD: ${self.usd_balance:.4f}  |  "
            f"Coin holdings: ${coin_val:.2f}  |  "
            f"Portfolio: ${self.initial_balance:.2f}", "info")

        tasks = [
            self._safe_loop(self._candles_loop,    "candles"),
            self._safe_loop(self._websocket_loop,  "websocket"),
            self._safe_loop(self._balance_loop,    "balance"),
            self._safe_loop(self._monitor_loop,    "monitor"),
            self._safe_loop(self._price_poll_loop, "price_poll"),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_loop(self, coro_func, name):
        """Retry wrapper — passes a callable so each retry creates a fresh coroutine."""
        retries = 0
        while self.running:
            try:
                await coro_func()
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                retries += 1
                wait = min(2 ** retries, 120)
                self.log_message(f"Task '{name}' error: {e} — retry in {wait}s", "warn")
                logger.error(f"Task {name}:\n{traceback.format_exc()}")
                if not self.running:
                    return
                await asyncio.sleep(wait)

    # ── Initial REST price fetch (parallel — all pairs at once) ─────────────────
    async def _fetch_initial_prices(self):
        async def _fetch_one(pair):
            try:
                resp = await asyncio.to_thread(self.client.get_product, pair)
                info = resp.to_dict()
                price = float(info.get('price', 0) or 0)
                if price:
                    self.live_prices[pair] = price
                    self._price_ts[pair]   = time.time()
                # Cache base_increment decimal precision so sell orders are rounded
                # to exactly what Coinbase requires (e.g. XCN may be 0.01 = 2dp)
                base_inc = info.get('base_increment', '0.00000001')
                try:
                    inc_f = float(base_inc)  # handles scientific notation (1e-8)
                    if inc_f >= 1:
                        dp = 0
                    else:
                        import math as _m
                        dp = max(0, -int(_m.floor(_m.log10(inc_f))))
                    self._base_precision[pair] = dp
                except Exception:
                    self._base_precision[pair] = 8  # safe default (BTC-level precision)
                # Cache quote_increment precision for limit order price formatting
                quote_inc = info.get('quote_increment', '0.01')
                try:
                    q_f = float(quote_inc)
                    if q_f >= 1:
                        qdp = 0
                    else:
                        import math as _mq
                        qdp = max(0, -int(_mq.floor(_mq.log10(q_f))))
                    self._quote_precision[pair] = qdp
                except Exception:
                    self._quote_precision[pair] = 2
                self.log_message(
                    f"{pair}: {format_price(price) if price else '—'}  "
                    f"(base_increment={base_inc} → {self._base_precision[pair]}dp  "
                    f"quote_increment={quote_inc} → {self._quote_precision[pair]}dp)", "info")
            except Exception as e:
                self.log_message(f"Price fetch {pair}: {e}", "warn")

        await asyncio.gather(*[_fetch_one(p) for p in TRADING_PAIRS])

    # ── Balance fetch ─────────────────────────────────────────────────────────
    async def _balance_loop(self):
        while self.running:
            await self._fetch_balance()
            await asyncio.sleep(60)

    def _save_bot_state(self):
        """Persist bot_balance and bot_pair_alloc to config.json.

        Called after every trade fill and every manual allocation so that
        cash accumulated from sell cycles (partial buybacks, profit) survives
        restarts. bot_exposure is NOT persisted here — it's reconstructed from
        trades.json entries on startup.
        """
        try:
            s = load_user_settings()
            s['bot_balance']    = round(self.bot_balance, 6)
            s['bot_pair_alloc'] = {p: round(v, 6)
                                   for p, v in self.bot_pair_alloc.items()
                                   if v > 0.001}
            save_user_settings(s)
        except Exception as e:
            logger.warning(f"Could not save bot state: {e}")

    async def _get_bid_ask(self, pair: str):
        """Fetch current best bid and ask. Returns raw (bid, ask) floats.

        Callers are responsible for applying any offset — see _place_order which
        uses attempt-based progressive offsets for maker-guaranteed pricing.
        Falls back to live_price ± 1 tick on API error.
        """
        qdp  = self._quote_precision.get(pair, 2)
        tick = 10 ** (-qdp)
        try:
            resp = await asyncio.to_thread(
                self.client.get_best_bid_ask, product_ids=[pair])
            raw   = resp.to_dict()
            books = raw.get('pricebooks', [])
            if books:
                book = books[0]
                bids = book.get('bids', [])
                asks = book.get('asks', [])
                bid  = float(bids[0]['price']) if bids else 0
                ask  = float(asks[0]['price']) if asks else 0
                if bid > 0 and ask > 0:
                    return bid, ask
        except Exception as e:
            self.log_message(f"bid/ask fetch {pair}: {e}", "warn")
        p = self.live_prices.get(pair, 0)
        return round(p - tick, qdp), round(p + tick, qdp)

    async def _wait_for_fill(self, order_id: str, timeout_s: int = 90) -> dict | None:
        """Poll get_order every 5s until FILLED or timeout.
        Returns order dict on fill, None on timeout/cancel/error."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                resp = await asyncio.to_thread(self.client.get_order, order_id=order_id)
                raw  = resp.to_dict()
                order = raw.get('order', raw)
                status = (order.get('status') or '').upper()
                if status == 'FILLED':
                    return order
                if status in ('CANCELLED', 'EXPIRED', 'FAILED'):
                    return None
            except Exception as e:
                self.log_message(f"Order poll {order_id}: {e}", "warn")
        return None

    async def _fetch_balance(self):
        try:
            resp  = await asyncio.to_thread(self.client.get_accounts)
            raw   = resp.to_dict()
            usd   = 0.0
            pair_coins = {p.split('-')[0]: p for p in TRADING_PAIRS}
            coin_log   = []

            # C3 fix: zero all pairs first so externally-sold coins clear properly
            for p in TRADING_PAIRS:
                self.real_exposure[p] = 0.0

            accounts = raw.get('accounts', [])
            if not accounts:
                self.log_message("Balance fetch returned empty accounts list — skipping update", "warn")
                return

            usdc = 0.0
            usdt = 0.0
            for a in accounts:
                cur = a.get('currency', '')
                val = float(a.get('available_balance', {}).get('value', 0) or 0)
                if cur == 'USD':
                    usd += val
                elif cur == 'USDC':
                    usdc += val
                    usd  += val   # treated as liquid USD for trading
                elif cur == 'USDT':
                    usdt += val
                    usd  += val
                elif cur in pair_coins:
                    pair  = pair_coins[cur]
                    price = self.live_prices.get(pair, 0)
                    if price > 0:
                        self.real_exposure[pair] = val * price
                        coin_log.append(f"{cur}={val:.4f} (${val*price:,.2f})")
                    else:
                        coin_log.append(f"{cur}={val:.4f} (price unknown)")

            self.usd_balance  = usd
            self.usdc_balance = usdc
            self.usdt_balance = usdt

            # Clamp bot accounting to actual liquid USD — the bot can't be
            # managing more USD than exists on the exchange.  Drift happens when
            # sell proceeds update bot_balance without reducing the pair budget,
            # or when coins are sold externally.  Scale proportionally so the
            # relative split between bot_balance and each pair_alloc is preserved.
            _bot_usd = self.bot_balance + sum(self.bot_pair_alloc.values())
            if _bot_usd > usd and _bot_usd > 0:
                _scale = usd / _bot_usd
                self.bot_balance = self.bot_balance * _scale
                for _p in list(self.bot_pair_alloc.keys()):
                    self.bot_pair_alloc[_p] *= _scale

            coin_str = "  |  ".join(coin_log) if coin_log else "none"
            self.log_message(
                f"Balance sync:  USD=${usd:,.2f}  |  Coins: {coin_str}  |  "
                f"bot_balance=${self.bot_balance:.2f}  "
                f"bot_alloc=${sum(self.bot_pair_alloc.values()):.2f}  "
                f"bot_exposure=${sum(self.bot_exposure.values()):.2f}  "
                f"bot_coin_qty={ {p.split('-')[0]: round(v,2) for p,v in self.bot_coin_qty.items() if v>0} }",
                "monitor")
            if self.root_alive:
                self.root.after(0, self._update_metrics)
                self.root.after(0, self._update_pair_cards)
        except Exception as e:
            self.log_message(f"Balance error: {e}", "warn")
            logger.error(traceback.format_exc())

    # ── Candles ───────────────────────────────────────────────────────────────
    async def _candles_loop(self):
        """
        Fetch candles sequentially with spacing to avoid Coinbase 429 rate limits.
        Trading pairs are fetched immediately at startup and refreshed every 5 min.
        Watchlist pairs are pre-cached once in a background task after the first
        trading-pair pass completes (so they never delay bot startup).
        """
        _watchlist_cached = False

        while self.running:
            # ── Trading pairs — always highest priority ───────────────────────
            for pair in TRADING_PAIRS:
                for tf in TIMEFRAMES:
                    if not self.running:
                        return
                    await self._fetch_pair_tf(pair, tf)
                    await asyncio.sleep(0.5)

            # ── Watchlist pre-cache — runs once after first trading-pair pass ─
            if not _watchlist_cached:
                _watchlist_cached = True
                asyncio.ensure_future(self._prefetch_watchlist())

            # Persist candle cache after each full trading-pair cycle so a crash
            # doesn't lose history built up since startup.
            await asyncio.to_thread(save_candle_cache, self.candle_history)
            await asyncio.sleep(300)           # trading pairs re-fetched every 5 min

    async def _prefetch_watchlist(self):
        """Background task: pre-cache all watchlist pairs once after startup."""
        for pair in WATCHLIST_PAIRS:
            if not self.running:
                return
            has_data = any(len(self.candle_history[tf][pair]) > 0 for tf in TIMEFRAMES)
            if not has_data:
                await self._fetch_watchlist_pair(pair)
            await asyncio.sleep(1.0)   # generous spacing — this is low priority

    async def _fetch_watchlist_pair(self, pair: str):
        """Fetch all 4 TFs for a watchlist/on-demand pair and cache to disk.
        Uses larger FETCH_CANDLES depth for best MA accuracy — same as trading pairs.
        No signals or indicator calculations run for watchlist pairs.
        """
        self.log_message(f"Caching watchlist: {pair} (all TFs)…", "monitor")
        for tf in TIMEFRAMES:
            if not self.running:
                return
            try:
                await self._fetch_pair_tf(pair, tf)
                await asyncio.sleep(0.6)   # conservative spacing for public endpoint
            except Exception as e:
                self.log_message(f"Watchlist fetch {pair}/{tf}: {e}", "warn")
        # Persist expanded cache to disk immediately
        save_candle_cache(self.candle_history)
        self.log_message(f"Watchlist cached: {pair}", "monitor")

    async def _fetch_pair_tf(self, pair: str, timeframe: str):
        """Batch-fetch up to FETCH_CANDLES[tf] candles (2× display) so MAs are
        fully warmed up across the entire visible window.  Each Coinbase call is
        capped at COINBASE_MAX_CANDLES (300); we walk backward in time until we
        have enough, pausing 0.35 s between batches to stay inside rate limits.
        """
        gran        = TF_TO_GRANULARITY[timeframe]
        seconds_per = {'1m': 60, '5m': 300, '1h': 3600, '1d': 86400}[timeframe]
        target      = FETCH_CANDLES.get(timeframe, COINBASE_MAX_CANDLES)

        all_candles: list = []
        end_ts = int(time.time())

        while len(all_candles) < target:
            batch_n  = min(COINBASE_MAX_CANDLES, target - len(all_candles))
            start_ts = end_ts - seconds_per * batch_n
            try:
                resp  = await asyncio.to_thread(
                    self.client.get_candles, pair,
                    str(start_ts), str(end_ts), gran
                )
                batch = normalize_candles(resp.to_dict().get('candles', []))
            except Exception as e:
                self.log_message(f"Candle fetch {pair}/{timeframe}: {e}", "warn")
                break

            if not batch:
                break
            # Prepend older candles so list stays chronological
            all_candles = batch + all_candles
            end_ts = start_ts - 1          # next batch goes further back
            if len(batch) < batch_n:
                break                      # exchange has no more history
            if len(all_candles) < target:
                await asyncio.sleep(0.35)  # rate-limit between batch calls

        if all_candles:
            span_from = datetime.fromtimestamp(all_candles[0][0]/1000 if isinstance(all_candles[0], list) else all_candles[0]['start'] if isinstance(all_candles[0], dict) else 0, pytz.UTC).strftime('%Y-%m-%d %H:%M')
            span_to   = datetime.fromtimestamp(all_candles[-1][0]/1000 if isinstance(all_candles[-1], list) else all_candles[-1]['start'] if isinstance(all_candles[-1], dict) else 0, pytz.UTC).strftime('%Y-%m-%d %H:%M')
            self.log_message(
                f"Candles fetched  {pair}/{timeframe}  "
                f"count={len(all_candles)}  span={span_from} → {span_to}", "monitor")
            if self.root_alive:
                self.root.after(0, self._ingest_candles, pair, all_candles, timeframe)

    def _ingest_candles(self, pair: str, candles: list, timeframe: str):
        ha       = heikin_ashi(candles)
        if not ha:
            self.log_message(f"Candle ingest  {pair}/{timeframe}  heikin_ashi returned empty — skipped", "warn")
            return
        existing = self.candle_history[timeframe][pair]
        last_ts  = existing[-1][0] if existing else 0
        new_ones = [c for c in ha if c[0] > last_ts]
        if new_ones:
            existing.extend(new_ones)
        self.log_message(
            f"Candle ingest  {pair}/{timeframe}  "
            f"raw={len(candles)}  ha={len(ha)}  new={len(new_ones)}  "
            f"total_stored={len(existing)}", "monitor")

        if len(ha) >= 2:
            op, np_ = ha[0][4], ha[-1][4]
            self.percent_change[timeframe][pair] = ((np_ - op) / op * 100) if op else 0
            # True 24h change: last close vs prior close on daily candles
            if timeframe == '1d':
                prev_c, last_c = ha[-2][4], ha[-1][4]
                self.pct_24h[pair] = ((last_c - prev_c) / prev_c * 100) if prev_c else 0

        # Watchlist-only pairs: skip indicator calculations and signal checks —
        # they are view/cache only; bot does not trade them.
        is_trading_pair = pair in TRADING_PAIRS
        # Recalculate ALL indicators here (on actual new candle data) so
        # _refresh_chart can read cached values without re-running O(N) maths
        # every second.  This cuts chart CPU by ~90% on 1s live refresh.
        if is_trading_pair and (timeframe == self.signal_tf or timeframe == '1h'):
            self.indicator_engine.calculate_atr(pair, ha)            # ATR first (FVG uses it)
            self.indicator_engine.calculate_support_resistance(pair, ha)
            self.indicator_engine.calculate_order_blocks(pair, ha)
            self.indicator_engine.calculate_fair_value_gaps(pair, ha)
            self.indicator_engine.calculate_rsi(pair, ha)            # RSI gate
            self.indicator_engine.calculate_ema_trend(pair, ha)      # trend filter
            self.indicator_engine.calculate_adx(pair, ha)            # ADX trend strength

        if is_trading_pair and timeframe == self.signal_tf and self.running and not self.paused:
            # Confirmation frame: one step shorter than the signal TF
            _conf_map = {'1m': '1m', '5m': '1m', '1h': '5m', '1d': '1h'}
            conf_tf   = _conf_map.get(self.signal_tf, '1m')
            # Capital gates — matched to what _place_order will actually accept.
            # BUY needs USD (bot_balance or bot_pair_alloc >= reserve).
            # SELL needs coins (bot_exposure or bot_coin_qty > 0).
            _can_buy  = (self.bot_pair_alloc.get(pair, 0) >= MINIMUM_RESERVE
                         or self.bot_balance >= MINIMUM_RESERVE)
            _can_sell = (self.bot_exposure[pair] > 0
                         or self.bot_coin_qty.get(pair, 0) > 0)
            cap = _can_buy or _can_sell
            # Log capital state on every signal-TF candle close for full traceability
            self.log_message(
                f"Candle close  {pair}/{timeframe}  "
                f"can_buy={_can_buy}  can_sell={_can_sell}  locked={self.order_locks[pair]}  "
                f"bot_balance=${self.bot_balance:.2f}  "
                f"pair_alloc=${self.bot_pair_alloc.get(pair,0):.2f}  "
                f"exposure=${self.bot_exposure[pair]:.2f}  "
                f"coin_qty={self.bot_coin_qty.get(pair,0):.2f}  "
                f"live_price={format_price(self.live_prices.get(pair,0))}  "
                f"direction={self.signal_direction}",
                "monitor")
            if cap and not self.order_locks[pair]:
                conf_candles = list(self.candle_history[conf_tf][pair])
                sig = (self.strategy.calculate_signals(pair, ha, conf_candles) or
                       self.strategy.calculate_breakout(pair, ha))
                raw_sig = sig['action'] if sig else 'none'
                # Filter by user's signal direction setting
                if sig and self.signal_direction == 'Buy Only'  and sig['action'] != 'buy':
                    sig = None
                if sig and self.signal_direction == 'Sell Only' and sig['action'] != 'sell':
                    sig = None
                # Drop signals for which we have no matching capital
                if sig and sig['action'] == 'buy'  and not _can_buy:
                    self.log_message(
                        f"Signal {pair} BUY suppressed — no USD capital  "
                        f"(bot_balance=${self.bot_balance:.2f}  pair_alloc=${self.bot_pair_alloc.get(pair,0):.2f}  reserve=${MINIMUM_RESERVE:.2f})",
                        "warn")
                    sig = None
                if sig and sig['action'] == 'sell' and not _can_sell:
                    self.log_message(
                        f"Signal {pair} SELL suppressed — no coin holdings  "
                        f"(exposure=${self.bot_exposure[pair]:.2f}  coin_qty={self.bot_coin_qty.get(pair,0):.2f})",
                        "warn")
                    sig = None
                if not sig and raw_sig != 'none':
                    self.log_message(
                        f"Signal {pair} {raw_sig.upper()} filtered  "
                        f"direction={self.signal_direction}  can_buy={_can_buy}  can_sell={_can_sell}",
                        "info")
                if sig:
                    self.order_locks[pair]    = True
                    self.order_lock_ts[pair]  = time.time()
                    self._last_signal_source  = sig['source']
                    self.log_message(
                        f"► SIGNAL [{sig['source']}] {sig['action'].upper()} {pair} "
                        f"@ {format_price(sig['price'])}  "
                        f"conf_candles={len(conf_candles)}  tf={timeframe}→{conf_tf}",
                        "trade")
                    asyncio.run_coroutine_threadsafe(
                        self._place_order(pair, sig['action'], fast_exec=True), self.loop)
            elif self.order_locks[pair]:
                self.log_message(
                    f"Signal check skipped — {pair} order lock active", "info")

        # Auto-refresh chart if this pair/tf is selected
        if self.root_alive:
            if (self.chart_pair_var.get() == pair and
                    self.chart_tf_var.get() == timeframe):
                self.root.after(0, self._refresh_chart)
            self.root.after(0, self._update_pair_cards)

    # ── WebSocket ticker ──────────────────────────────────────────────────────
    async def _websocket_loop(self):
        """
        Coinbase Advanced Trade WebSocket.
        Docs: https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview
        Auth: https://docs.cdp.coinbase.com/advanced-trade/docs/ws-auth

        Subscribes to both 'ticker' (per-trade) and 'ticker_batch' (~1s heartbeat)
        so low-volume pairs like XCN still receive periodic price updates.
        """
        _ws_backoff = 2   # seconds; doubles on each failed connection, cap 60
        while self.running:
            try:
                async with websockets.connect(
                    COINBASE_WS_URL,
                    ping_interval=20, ping_timeout=30,
                    max_size=None        # level2 BTC snapshots can exceed 2MB
                ) as ws:
                    _ws_backoff = 2   # reset on successful connection
                    # Fresh JWT for this connection
                    jwt = make_ws_jwt(self.api_key, self.api_secret)
                    self._ws_jwt_ts = time.time()

                    # Subscribe to price tickers + real-time order book
                    for channel in ("ticker", "ticker_batch", "level2"):
                        await ws.send(json.dumps({
                            "type":        "subscribe",
                            "product_ids": TRADING_PAIRS,
                            "channel":     channel,
                            "jwt":         jwt,
                        }))
                    self.log_message(
                        f"WebSocket connected  url={COINBASE_WS_URL}  "
                        f"pairs={TRADING_PAIRS}  "
                        f"channels=[ticker, ticker_batch, level2]", "trade")

                    _ws_ticks = 0
                    _ws_last_log = time.time()
                    while self.running:
                        try:
                            raw  = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(raw)

                            # Renew JWT every 90s (token valid for 120s)
                            if time.time() - self._ws_jwt_ts > 90:
                                jwt = make_ws_jwt(self.api_key, self.api_secret)
                                self._ws_jwt_ts = time.time()

                            chan = data.get("channel", "")

                            # ── level2 order book updates ─────────────────
                            # Coinbase sends subscription as "level2" but
                            # incoming messages arrive with channel "l2_data"
                            if chan in ("level2", "l2_data"):
                                for evt in data.get("events", []):
                                    _ev_pid = evt.get("product_id", "")
                                    if _ev_pid not in TRADING_PAIRS:
                                        continue
                                    _ob = self._order_book[_ev_pid]
                                    for upd in evt.get("updates", []):
                                        _side = upd.get("side", "")
                                        try:
                                            _pp = float(upd.get("price_level", 0))
                                            _qq = float(upd.get("new_quantity", 0))
                                        except (TypeError, ValueError):
                                            continue
                                        _book_side = _ob["bids"] if _side == "bid" else _ob["asks"]
                                        # Remove stale entry for this price, add new
                                        _book_side[:] = [
                                            x for x in _book_side if x[0] != _pp]
                                        if _qq > 0:
                                            _book_side.append((_pp, _qq))
                                    # Keep bids sorted descending, asks ascending
                                    _ob["bids"].sort(key=lambda x: -x[0])
                                    _ob["asks"].sort(key=lambda x:  x[0])
                                    # Cap to top 50 levels each side
                                    _ob["bids"] = _ob["bids"][:50]
                                    _ob["asks"] = _ob["asks"][:50]
                                    # Update order book popup if open for this pair
                                    if (self._ob_window is not None and
                                            getattr(self, '_ob_pair', '') == _ev_pid):
                                        if self.root_alive:
                                            self.root.after(0, self._update_ob_display)
                                continue

                            if chan not in ("ticker", "ticker_batch"):
                                continue

                            for evt in data.get("events", []):
                                for tick in evt.get("tickers", []):
                                    pid   = tick.get("product_id", "")
                                    price = tick.get("price", "")
                                    if pid in TRADING_PAIRS and price:
                                        try:
                                            p = float(price)
                                        except ValueError:
                                            self.log_message(
                                                f"WS bad price value '{price}' for {pid} — skipping tick", "warn")
                                            continue
                                        self.live_prices[pid] = p
                                        self._price_ts[pid]   = time.time()
                                        self.price_history[pid].append(p)
                                        _ws_ticks += 1
                                        # ── Forming candle synthesis ─────────
                                        _now_ts = int(time.time())
                                        _secs_map = {'1m': 60, '5m': 300,
                                                     '1h': 3600, '1d': 86400}
                                        for _tf, _secs in _secs_map.items():
                                            _ps_ms = (_now_ts // _secs) * _secs * 1000
                                            _fkey  = (pid, _tf)
                                            _fc    = self._forming_candle.get(_fkey)
                                            if _fc is None or _fc[0] != _ps_ms:
                                                # New candle period — open at this price
                                                self._forming_candle[_fkey] = [
                                                    _ps_ms, p, p, p, p, 0.0]
                                            else:
                                                _fc[2] = max(_fc[2], p)  # high
                                                _fc[3] = min(_fc[3], p)  # low
                                                _fc[4] = p               # close
                                        if self.root_alive:
                                            self.root.after(0, self._update_pair_cards)
                                        if chan == "ticker":
                                            await self._check_surge(pid)

                            # Log WS tick rate every 60s
                            _now = time.time()
                            if _now - _ws_last_log >= 60:
                                prices_str = "  ".join(
                                    f"{p.split('-')[0]}={format_price(self.live_prices.get(p,0))}"
                                    f"({time.time()-self._price_ts.get(p,0):.0f}s ago)"
                                    for p in TRADING_PAIRS)
                                self.log_message(
                                    f"WS heartbeat  ticks_last_60s={_ws_ticks}  {prices_str}",
                                    "monitor")
                                _ws_ticks = 0
                                _ws_last_log = _now

                        except asyncio.TimeoutError:
                            self.log_message("WS recv timeout — sending ping", "info")
                            await ws.ping()
                        except Exception as e:
                            self.log_message(f"WS recv error: {e}", "warn")
                            logger.error(traceback.format_exc())
                            break

            except Exception as e:
                self.log_message(
                    f"WS disconnected: {e} — reconnect in {_ws_backoff}s", "warn")
                logger.error(traceback.format_exc())
                await asyncio.sleep(_ws_backoff)
                _ws_backoff = min(_ws_backoff * 2, 60)

    # ── REST price poll fallback (catches pairs WS misses) ────────────────────
    async def _price_poll_loop(self):
        """
        Polls get_best_bid_ask every 8 seconds as a safety net for pairs
        that haven't received a WebSocket tick recently (e.g. low-volume XCN).
        Uses the bid/ask midpoint. Only updates pairs stale for >5s to avoid
        overwriting a fresher WS price.
        """
        await asyncio.sleep(15)   # let WS establish first
        while self.running:
            try:
                now  = time.time()
                stale = [p for p in TRADING_PAIRS
                         if now - self._price_ts.get(p, 0) > 5]
                if stale:
                    resp = await asyncio.to_thread(
                        self.client.get_best_bid_ask, product_ids=stale)
                    for entry in resp.to_dict().get("pricebooks", []):
                        pid = entry.get("product_id", "")
                        bid = float(entry.get("bids", [{}])[0].get("price", 0) or 0)
                        ask = float(entry.get("asks", [{}])[0].get("price", 0) or 0)
                        if pid in TRADING_PAIRS and bid > 0 and ask > 0:
                            mid = (bid + ask) / 2
                            self.live_prices[pid] = mid
                            self._price_ts[pid]   = time.time()
                            if self.root_alive:
                                self.root.after(0, self._update_pair_cards)
            except Exception as e:
                logger.warning(f"Price poll error: {e}")
            await asyncio.sleep(8)

    # ── Surge / flash-move detector ───────────────────────────────────────────
    async def _check_surge(self, pair: str):
        """Detect rapid price moves in the live tick stream and act immediately.

        Compares the latest price against the price SURGE_WINDOW ticks ago.
        If the move exceeds SURGE_PCT (2.5%) and the pair has capital allocated,
        fires a market order bypassing the MA candle-close requirement.

        This catches XCN pumps, BTC flash crashes, and any momentum spike that
        would be missed waiting for the next candle to close.

        Surge has its own cooldown (SURGE_COOLDOWN = 90s) so it doesn't
        re-enter immediately after a filled surge trade.
        """
        if not self.running or self.paused:
            return
        history = self.price_history[pair]
        if len(history) < SURGE_WINDOW:
            return
        ticks   = list(history)
        oldest  = ticks[-SURGE_WINDOW]
        newest  = ticks[-1]
        if oldest <= 0:
            return
        move = (newest - oldest) / oldest   # signed % move
        if abs(move) < SURGE_PCT:
            return
        now = time.time()
        action = 'buy' if move > 0 else 'sell'
        pair_cap     = (self.bot_pair_alloc.get(pair, 0) >= MINIMUM_RESERVE
                        or self.bot_balance >= MINIMUM_RESERVE)
        has_exposure = self.bot_exposure[pair] > 0
        has_coins    = self.bot_coin_qty.get(pair, 0) > 0
        last_ts      = self.surge_last_buy[pair] if action == 'buy' else self.surge_last_sell[pair]
        cd_remaining = max(0, SURGE_COOLDOWN - (now - last_ts))

        if not (pair_cap or has_exposure or has_coins):
            return
        if self.order_locks[pair]:
            return
        if cd_remaining > 0:
            return

        # Reversal guard: require majority of last 5 ticks to match direction
        if len(ticks) >= 5:
            recent = ticks[-5:]
            dirs   = [recent[i] - recent[i-1] for i in range(1, len(recent))]
            bull   = sum(1 for d in dirs if d > 0)
            bear   = sum(1 for d in dirs if d < 0)
            if action == 'buy'  and bear > bull:
                return
            if action == 'sell' and bull > bear:
                return

        if self.signal_direction == 'Buy Only'  and action != 'buy':
            return
        if self.signal_direction == 'Sell Only' and action != 'sell':
            return

        if action == 'buy':
            self.surge_last_buy[pair]  = now
        else:
            self.surge_last_sell[pair] = now
        self.order_locks[pair] = True

        self._last_signal_source = f"Surge⚡ {move*100:+.1f}%"
        self.log_message(
            f"⚡ SURGE FIRING  {action.upper()} {pair}  "
            f"move={move*100:+.2f}%  oldest={format_price(oldest)}→newest={format_price(newest)}  "
            f"window={SURGE_WINDOW} ticks  bot_balance=${self.bot_balance:.2f}  "
            f"coin_qty={self.bot_coin_qty.get(pair,0):.2f}", "trade")
        asyncio.create_task(self._place_order(pair, action, fast_exec=True))

    # ── Trade monitor ─────────────────────────────────────────────────────────
    async def _monitor_loop(self):
        _monitor_tick  = 0
        _last_log: dict = {}   # tid → {'price': float, 'sl': float, 'pl': float}
        while self.running:
            try:
                _monitor_tick += 1

                # ── Order lock timeout (anti-deadlock) ────────────────────────
                # If an order lock has been held for > 300s, auto-release it.
                # This prevents a deadlock where _place_order raised before its
                # finally block could clear the lock.
                _now = time.time()
                for _lp in list(self.order_locks.keys()):
                    if self.order_locks[_lp]:
                        _lock_age = _now - self.order_lock_ts.get(_lp, _now)
                        if _lock_age > 300:
                            self.order_locks[_lp] = False
                            self.log_message(
                                f"Order lock auto-released for {_lp} "
                                f"(held {_lock_age:.0f}s — deadlock guard)", "warn")

                active = [(tid, t) for tid, t in self.trade_history.items()
                          if t.get('event') == 'trade']

                for tid, trade in active:
                    pair  = trade['symbol']
                    # Skip if price feed is stale — SL/TP on a stale price is dangerous
                    price_age = time.time() - self._price_ts.get(pair, 0)
                    if price_age > 30 and self._price_ts.get(pair, 0) > 0:
                        self.log_message(
                            f"Monitor: skipping {pair} SL/TP check — price feed stale ({price_age:.0f}s)", "warn")
                        continue
                    cur   = self.live_prices.get(pair) or trade['current_price']
                    if cur <= 0:
                        continue   # no usable price at all
                    entry = trade['entry_price']
                    trade['current_price'] = cur
                    pl = ((cur - entry) * trade['quantity']
                          if trade['side'] == 'buy'
                          else (entry - cur) * trade['quantity'])
                    trade['pl'] = pl

                    orig_sl = trade['stop_loss']
                    orig_tp = trade['take_profit']

                    # ── Trailing stop ───────────────────────────────────────
                    hit_trail = False
                    trail_active = False
                    if trade['side'] == 'buy':
                        peak = max(trade.get('peak_price', entry), cur)
                        trade['peak_price'] = peak
                        trail_sl = peak * (1 - TRAIL_STOP_PCT)
                        if peak > entry * 1.01:
                            trade['stop_loss'] = max(orig_sl, trail_sl)
                            hit_trail    = cur <= trail_sl
                            trail_active = True
                    else:
                        trough = min(trade.get('peak_price', entry), cur)
                        trade['peak_price'] = trough
                        trail_sl = trough * (1 + TRAIL_STOP_PCT)
                        if trough < entry * 0.99:
                            trade['stop_loss'] = min(orig_sl, trail_sl)
                            hit_trail    = cur >= trail_sl
                            trail_active = True

                    cur_sl = trade['stop_loss']
                    hit_sl = (trade['side'] == 'buy'  and cur <= cur_sl) or \
                             (trade['side'] == 'sell' and cur >= cur_sl)
                    hit_tp = (trade['side'] == 'buy'  and cur >= orig_tp) or \
                             (trade['side'] == 'sell' and cur <= orig_tp)

                    # ── Selective logging — only on meaningful change ────────
                    prev      = _last_log.get(tid, {})
                    prev_price = prev.get('price', 0)
                    prev_sl    = prev.get('sl', 0)
                    prev_pl    = prev.get('pl', None)
                    # Log if: price moved >0.2%, SL ratcheted, P&L crossed $0.10
                    # boundary, within 3% of SL/TP, or every 60 ticks (~5 min)
                    price_moved  = prev_price == 0 or (abs(cur - prev_price) / prev_price > 0.002)
                    sl_moved     = cur_sl != prev_sl
                    pl_boundary  = prev_pl is None or int(pl / 0.10) != int(prev_pl / 0.10)
                    sl_dist      = abs(cur - cur_sl) / cur if cur else 1
                    tp_dist      = abs(cur - orig_tp) / cur if cur else 1
                    near_trigger = sl_dist < 0.03 or tp_dist < 0.03
                    periodic     = _monitor_tick % 60 == 0

                    if price_moved or sl_moved or pl_boundary or near_trigger or periodic:
                        trail_str = (f"  trail_sl={format_price(trail_sl)}"
                                     f"  peak={format_price(trade.get('peak_price', entry))}"
                                     if trail_active else "")
                        near_str  = (f"  ⚠ {(sl_dist*100):.1f}% from SL" if sl_dist < 0.03
                                     else f"  ⚠ {(tp_dist*100):.1f}% from TP" if tp_dist < 0.03
                                     else "")
                        msg = (
                            f"Position  {trade['side'].upper()} {pair}  "
                            f"cur={format_price(cur)}  P&L=${pl:+.4f}  "
                            f"SL={format_price(cur_sl)}  TP={format_price(orig_tp)}"
                            f"{trail_str}{near_str}"
                        )
                        if msg != prev.get('msg'):
                            self.log_message(msg, "monitor")
                        _last_log[tid] = {'price': cur, 'sl': cur_sl, 'pl': pl, 'msg': msg}

                    if hit_trail:
                        peak_str = format_price(trade.get('peak_price', cur))
                        self.log_message(
                            f"► TRAIL STOP firing  {pair}  cur={format_price(cur)}  "
                            f"trail_sl={format_price(trail_sl)}  peak={peak_str}", "trade")
                        await self._close_trade(
                            tid, cur, f"Trailing Stop  (peak {peak_str})")
                    elif hit_sl or hit_tp:
                        reason = "Stop Loss" if hit_sl else "Take Profit"
                        self.log_message(
                            f"► {reason.upper()} firing  {pair}  "
                            f"cur={format_price(cur)}  "
                            f"SL={format_price(cur_sl)}  TP={format_price(orig_tp)}  "
                            f"entry={format_price(entry)}  P&L=${pl:+.4f}", "trade")
                        await self._close_trade(tid, cur, reason)

                # Clean up state for closed trades
                for gone in set(_last_log) - {tid for tid, _ in active}:
                    _last_log.pop(gone, None)

                if self.root_alive:
                    self.root.after(0, self._refresh_trade_rows)
                    self.root.after(0, self._update_metrics)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log_message(f"Monitor error: {e}", "error")
                logger.error(traceback.format_exc())
                await asyncio.sleep(5)

    # ── Order placement ───────────────────────────────────────────────────────
    async def _place_order(self, pair: str, side: str, amount: float = None,
                           fast_exec: bool = False):
        """
        Place a limit order (post_only maker, 0% fee) via Coinbase Advanced Trade REST API.
        fast_exec=True: signal-triggered trades — 2 attempts × 25s then market fallback
                        so total max delay is ~1 minute instead of 4.5 minutes.
        Reference: https://docs.cdp.coinbase.com/advanced-trade/reference/create_order
        """
        if amount is None:
            amount = ORDER_AMOUNT_USD
        try:
            if not self.running or self.paused:
                return

            # Reject if live price is stale — 30s threshold to avoid false-blocking
            # on startup before WS connects (initial REST price is set at startup time)
            price_age = time.time() - self._price_ts.get(pair, 0)
            if price_age > 30:
                self.log_message(
                    f"Skipping {side} {pair} — price feed stale ({price_age:.0f}s old)",
                    "warn")
                return

            atr    = self.indicator_engine.data[pair]['atr']
            vp     = self.live_prices.get(pair, 0)
            sl_pct = (1.5 * atr / vp) if (atr > 0 and vp) else STOP_LOSS_PCT
            tp_pct = (3.0 * atr / vp) if (atr > 0 and vp) else TAKE_PROFIT_PCT

            if side == 'buy':
                # Prefer per-coin allocation; fall back to general bot_balance
                pair_funds    = self.bot_pair_alloc.get(pair, 0)
                general_funds = self.bot_balance
                if pair_funds >= MINIMUM_RESERVE:
                    avail_funds    = pair_funds
                    use_pair_alloc = True
                elif general_funds >= MINIMUM_RESERVE:
                    avail_funds    = general_funds
                    use_pair_alloc = False
                else:
                    self.log_message(
                        f"No funds allocated for {pair} — allocate via Dashboard or Charts",
                        "warn")
                    return
                # Hard cap: the bot cannot spend more than the actual liquid USD
                # balance on exchange — prevents stale accounting from over-ordering.
                avail_funds = min(avail_funds, self.usd_balance)
                if avail_funds < MINIMUM_RESERVE:
                    self.log_message(
                        f"Buy {pair} skipped — liquid USD ${self.usd_balance:.2f} "
                        f"below minimum reserve ${MINIMUM_RESERVE:.2f}", "warn")
                    return
                # Full-port scalping: always deploy the entire available allocation.
                # Overrides fixed ORDER_AMOUNT_USD and auto-compound — the goal is
                # to be 100% deployed at all times, cycling between USD and coin
                # on every alternating buy/sell signal.
                amount_usd = avail_funds
                total_cap  = avail_funds + sum(self.bot_exposure.values())
                if self.bot_exposure[pair] + amount_usd > MAX_EXPOSURE_PER_PAIR * total_cap:
                    self.log_message(f"Exposure cap reached for {pair}", "warn")
                    return
            else:
                # Full-port sell: liquidate the entire coin position in one order.
                bot_exp_usd  = self.bot_exposure.get(pair, 0)
                coin_qty_val = self.bot_coin_qty.get(pair, 0) * vp if vp else 0
                total_sell   = bot_exp_usd + coin_qty_val
                amount_usd   = total_sell   # sell everything — no partial exits
                if amount_usd <= 0:
                    self.log_message(f"No holdings to sell for {pair}", "warn")
                    return

            price = vp
            if not price:
                self.log_message(f"No live price for {pair}", "error")
                return

            import math as _math
            bdp  = self._base_precision.get(pair, 8)
            qdp  = self._quote_precision.get(pair, 2)

            # ── Progressive limit order (maker → 0% fee) ─────────────────────
            # Attempt 1: price offset 1 tick from bid/ask — guaranteed maker on ANY
            #            market, even zero-spread (XCN-USD). Cost ≤ 0.2% vs 0.6% taker.
            # Attempt 2: price at raw bid/ask — maker on normal-spread markets.
            # Fallback:  market order — guaranteed fill, taker fee applies.
            #
            # fast_exec (signal-triggered): 2 attempts × 25s → market (~1 min max)
            # normal:                       3 attempts × 90s → market (~5 min max)
            MAX_LIMIT_ATTEMPTS = 2 if fast_exec else 3
            _fill_timeout      = 25 if fast_exec else 90
            tick         = 10 ** (-qdp)
            filled_price = None
            qty_filled   = None
            order_id     = None
            used_market  = False

            for attempt in range(MAX_LIMIT_ATTEMPTS):
                bid, ask = await self._get_bid_ask(pair)
                # Progressive offset: attempt 0 → 1 tick from crossing (safe maker)
                #                     attempt 1+ → at the spread (maker if spread > 0)
                offset = tick if attempt == 0 else 0

                if side == 'buy':
                    # Buy below bid by offset → guaranteed maker; more aggressive each retry
                    limit_px = round(bid - offset, qdp)
                    if limit_px <= 0:
                        self.log_message(f"BUY limit_px is 0 for {pair} — skipping attempt", "warn")
                        continue
                    raw_qty  = amount_usd / limit_px
                    floored  = _math.floor(raw_qty * 10**bdp) / 10**bdp
                    if floored <= 0:
                        self.log_message(
                            f"BUY qty rounds to 0 for {pair} (amount=${amount_usd:.2f} px={limit_px}) — skipping", "warn")
                        break
                    qty_str  = f"{floored:.{bdp}f}"
                    lp_str   = f"{limit_px:.{qdp}f}"
                    offset_note = f"  (bid-{offset:.{qdp}f}, maker-safe)" if offset else "  (at bid)"
                    self.log_message(
                        f"BUY LIMIT {pair}  qty={qty_str}  price={lp_str}{offset_note}"
                        f"  attempt {attempt+1}/{MAX_LIMIT_ATTEMPTS}", "info")
                    order_id   = str(uuid.uuid4())
                    order_resp = await asyncio.to_thread(
                        self.client.limit_order_gtc_buy,
                        client_order_id=order_id,
                        product_id=pair,
                        base_size=qty_str,
                        limit_price=lp_str,
                        post_only=True
                    )
                else:
                    # Sell above ask by offset → guaranteed maker; more aggressive each retry
                    limit_px = round(ask + offset, qdp)
                    raw_qty  = amount_usd / price
                    raw_qty  = _math.floor(raw_qty * 10**bdp) / 10**bdp
                    min_qty  = 10 ** (-bdp)
                    if raw_qty < min_qty:
                        self.log_message(
                            f"Sell qty below min increment for {pair} — skipping", "warn")
                        return
                    qty_str    = f"{raw_qty:.{bdp}f}"
                    lp_str     = f"{limit_px:.{qdp}f}"
                    offset_note = f"  (ask+{offset:.{qdp}f}, maker-safe)" if offset else "  (at ask)"
                    self.log_message(
                        f"SELL LIMIT {pair}  qty={qty_str}  price={lp_str}{offset_note}"
                        f"  attempt {attempt+1}/{MAX_LIMIT_ATTEMPTS}", "info")
                    order_id   = str(uuid.uuid4())
                    order_resp = await asyncio.to_thread(
                        self.client.limit_order_gtc_sell,
                        client_order_id=order_id,
                        product_id=pair,
                        base_size=qty_str,
                        limit_price=lp_str,
                        post_only=True
                    )

                raw     = order_resp.to_dict()
                success = raw.get('success', False)
                if not success:
                    err_obj = raw.get('error_response', {}) or {}
                    reason  = err_obj.get('message') or err_obj.get('error') or str(raw)
                    preview = err_obj.get('preview_failure_reason', '')
                    detail  = f"  [{preview}]" if preview else ''
                    self.log_message(
                        f"Limit order REJECTED {pair} {side.upper()} attempt {attempt+1}: "
                        f"{reason}{detail}", "warn")
                    # post_only rejection (would cross spread) — retry immediately
                    await asyncio.sleep(1)
                    continue

                # Wait for fill
                placed_id = (raw.get('success_response', {}) or {}).get('order_id') or order_id
                filled_order = await self._wait_for_fill(placed_id, timeout_s=_fill_timeout)
                if filled_order:
                    avg = float(filled_order.get('average_filled_price', 0) or 0)
                    fees = float(filled_order.get('total_fees', 0) or 0)
                    if avg > 0:
                        filled_price = avg
                    qty_f = float(filled_order.get('filled_size', 0) or 0)
                    if qty_f > 0:
                        qty_filled = qty_f
                    if fees > 0:
                        self.log_message(f"Order fee: ${fees:.6f} {pair}", "warn")
                    else:
                        self.log_message(f"Order fee: $0.00 (maker limit) {pair}", "info")
                    break
                else:
                    # Not filled in 90s — cancel and retry
                    self.log_message(
                        f"Limit order not filled in 90s ({pair} {side}) — "
                        f"cancelling, attempt {attempt+1}", "warn")
                    try:
                        await asyncio.to_thread(
                            self.client.cancel_orders, order_ids=[placed_id])
                    except Exception:
                        pass
                    await asyncio.sleep(1)

            if filled_price is None or qty_filled is None:
                # All limit attempts failed — fall back to market order
                self.log_message(
                    f"All limit attempts failed for {pair} {side} — "
                    f"falling back to market order (taker fee applies)", "warn")
                used_market = True
                order_id    = str(uuid.uuid4())
                if side == 'buy':
                    quote_str  = str(round(amount_usd, 2))
                    order_resp = await asyncio.to_thread(
                        self.client.market_order_buy,
                        client_order_id=order_id,
                        product_id=pair,
                        quote_size=quote_str
                    )
                else:
                    raw_qty2 = amount_usd / price
                    raw_qty2 = _math.floor(raw_qty2 * 10**bdp) / 10**bdp
                    order_resp = await asyncio.to_thread(
                        self.client.market_order_sell,
                        client_order_id=order_id,
                        product_id=pair,
                        base_size=f"{raw_qty2:.{bdp}f}"
                    )
                raw     = order_resp.to_dict()
                success = raw.get('success', False)
                if not success:
                    err_obj = raw.get('error_response', {}) or {}
                    reason  = err_obj.get('message') or err_obj.get('error') or str(raw)
                    preview = err_obj.get('preview_failure_reason', '')
                    detail  = f"  [{preview}]" if preview else ''
                    self.log_message(
                        f"Market order REJECTED {pair} {side.upper()}: {reason}{detail}", "error")
                    return
                filled_price = price   # best we can do for market fills
                sr   = (raw.get('success_response', {}) or {})
                avg2 = float(sr.get('average_filled_price', 0) or 0)
                if avg2 > 0 and (price == 0 or abs(avg2 - price) / price < 0.20):
                    filled_price = avg2
                # Try to extract qty_filled from the market order response
                qty_f2 = float(sr.get('filled_size', 0) or 0)
                if qty_f2 > 0:
                    qty_filled = qty_f2

            if qty_filled is None:
                qty_filled = amount_usd / filled_price if filled_price else 0
            if qty_filled <= 0:
                self.log_message(f"Order filled but qty is 0 for {pair} {side} — skipping state update", "error")
                return

            sl = filled_price * (1 - sl_pct) if side == 'buy' else filled_price * (1 + sl_pct)
            tp = filled_price * (1 + tp_pct) if side == 'buy' else filled_price * (1 - tp_pct)

            spent = qty_filled * filled_price
            if side == 'buy':
                if use_pair_alloc:
                    self.bot_pair_alloc[pair] = max(0, self.bot_pair_alloc[pair] - spent)
                else:
                    self.bot_balance = max(0, self.bot_balance - spent)
                self.bot_exposure[pair] += spent
                # NOTE: bot_coin_qty is USER-allocation only. Bot-bought coins are
                # tracked solely via bot_exposure (USD cost basis). Do NOT increment
                # bot_coin_qty here — that would double-count coins in sell capacity
                # and corrupt the proceeds routing user_frac/bot_frac split.
            else:
                # H1 fix: route proceeds to the correct bucket based on coin source.
                # bot_coin_qty coins came from user (not from bot-traded USD), so
                # proceeds return to bot_balance (liquid bot pool), not bot_pair_alloc.
                # bot_exposure coins came from bot buying with bot_pair_alloc USD, so
                # proceeds return to bot_pair_alloc to replenish that trading budget.
                coin_qty   = self.bot_coin_qty.get(pair, 0)
                bot_exp    = self.bot_exposure.get(pair, 0)
                total_qty  = coin_qty + (bot_exp / filled_price if filled_price else 0)
                if total_qty > 0:
                    # Proportional split of proceeds back to each bucket
                    user_frac = coin_qty / total_qty if total_qty > 0 else 0
                    bot_frac  = 1.0 - user_frac
                    self.bot_balance          += spent * user_frac
                    self.bot_pair_alloc[pair] += spent * bot_frac
                else:
                    self.bot_balance += spent
                # Drain tracked quantities — clamp to prevent negatives if price moved
                drained_qty = min(qty_filled, coin_qty)
                bot_frac_drain = bot_frac if total_qty > 0 else 0
                self.bot_coin_qty[pair] = max(0, coin_qty - drained_qty)
                self.bot_exposure[pair] = max(0, bot_exp - min(spent * bot_frac_drain, bot_exp))

                # ── Swap-on-sell ──────────────────────────────────────────────
                await self._execute_swap(pair, spent)

            tid = str(uuid.uuid4())
            self.trade_history[tid] = {
                'event': 'trade', 'id': tid, 'symbol': pair, 'side': side,
                'quantity': qty_filled, 'entry_price': filled_price,
                'current_price': filled_price, 'pl': 0.0,
                'stop_loss': sl, 'take_profit': tp,
                'timestamp': time.time() * 1000
            }
            save_trade_history(self.trade_history)
            _now = time.time()
            self.strategy.last_trade_time[pair] = _now
            if side == 'buy':
                self.strategy.last_buy_time[pair]  = _now
            else:
                self.strategy.last_sell_time[pair] = _now
            # Record last executed signal for the dashboard display
            self.last_executed_signal = {
                'pair':    pair,
                'side':    side,
                'price':   filled_price,
                'qty':     qty_filled,
                'spent':   spent,
                'ts':      time.time(),
                'source':  getattr(self, '_last_signal_source', side.upper()),
            }
            coin = pair.split('-')[0]
            self.log_message(
                f"◆ FILLED {side.upper()} {pair}  "
                f"qty={qty_filled:.6f} {coin}  "
                f"fill_price={format_price(filled_price)}  "
                f"live_price={format_price(price)}  "
                f"spent=${spent:.4f}  "
                f"SL={format_price(sl)}  TP={format_price(tp)}  "
                f"sl_pct={sl_pct*100:.2f}%  tp_pct={tp_pct*100:.2f}%  "
                f"─── POST-FILL STATE ───  "
                f"bot_balance=${self.bot_balance:.4f}  "
                f"pair_alloc=${self.bot_pair_alloc[pair]:.4f}  "
                f"bot_exposure=${self.bot_exposure[pair]:.4f}  "
                f"coin_qty={self.bot_coin_qty[pair]:.4f}  "
                f"order_id={order_id}", "trade")
            self._save_bot_state()
            if self.root_alive:
                self.root.after(0, self._refresh_trade_rows)
                self.root.after(0, self._update_metrics)
            await self._fetch_balance()

        except Exception as e:
            self.log_message(f"Order error {pair} {side}: {e}", "error")
            logger.error(traceback.format_exc())
        finally:
            self.order_locks[pair] = False

    async def _execute_swap(self, sold_pair: str, proceeds_usd: float):
        """After selling `sold_pair`, immediately buy the configured swap target.

        swap_targets[pair] values:
          ''          → keep as USD (no action)
          'USD'       → keep as USD (no action)
          'USDC'/'USDT' → keep as stablecoin (USD equivalent, no action)
          'BTC'/'ETH'/'XCN' → buy that coin using the proceeds
        """
        target_coin = self.swap_targets.get(sold_pair, '')
        if not target_coin or target_coin in ('USD', 'USDC', 'USDT'):
            return   # nothing to do — proceeds stay as USD/stablecoin

        target_pair = f"{target_coin}-USD"
        if target_pair not in TRADING_PAIRS or target_pair == sold_pair:
            self.log_message(
                f"Swap target {target_coin} not in TRADING_PAIRS, keeping USD", "warn")
            return

        if proceeds_usd < 1.0:
            return   # too small to swap

        # Move proceeds from sold pair's allocation → target pair's allocation
        self.bot_pair_alloc[sold_pair]  = max(0, self.bot_pair_alloc[sold_pair] - proceeds_usd)
        self.bot_pair_alloc[target_pair] += proceeds_usd

        self.log_message(
            f"↔  SWAP  {sold_pair.split('-')[0]} → {target_coin}  "
            f"${proceeds_usd:.2f}", "trade")

        # Small delay so the sell settlement clears first
        await asyncio.sleep(0.5)
        await self._place_order(target_pair, 'buy', proceeds_usd)

    async def _close_trade(self, tid: str, price: float, reason: str):
        trade = self.trade_history.get(tid)
        if not trade:
            return
        # Guard: don't attempt to close the same position twice concurrently
        if trade.get('_closing'):
            return
        trade['_closing'] = True
        pair       = trade['symbol']
        close_side = 'sell' if trade['side'] == 'buy' else 'buy'
        qty        = trade['quantity']
        cur_price  = self.live_prices.get(pair, price)
        try:
            import math as _m2
            bdp = self._base_precision.get(pair, 8)
            qdp = self._quote_precision.get(pair, 2)

            if close_side == 'sell':
                qty_adj = _m2.floor(qty * 10**bdp) / 10**bdp
                if qty_adj < 10**(-bdp):
                    self.log_message(
                        f"Close qty {qty_adj} below min increment — skipping {reason}", "warn")
                    trade.pop('_closing', None)
                    return

            # Progressive limit order — same offset strategy as _place_order
            # Attempt 1: 1 tick from crossing → guaranteed maker on any market
            # Attempt 2: at raw bid/ask → maker if spread exists
            # Fallback:  market
            MAX_CLOSE_ATTEMPTS = 3
            tick_c             = 10 ** (-qdp)
            close_filled_price = None
            close_filled_qty   = None
            for attempt in range(MAX_CLOSE_ATTEMPTS):
                bid, ask   = await self._get_bid_ask(pair)
                offset     = tick_c if attempt == 0 else 0
                order_id   = str(uuid.uuid4())
                if close_side == 'sell':
                    limit_px   = round(ask + offset, qdp)
                    lp_str     = f"{limit_px:.{qdp}f}"
                    bs_str     = f"{qty_adj:.{bdp}f}"
                    note       = f"  (ask+{offset:.{qdp}f})" if offset else "  (at ask)"
                    self.log_message(
                        f"CLOSE SELL LIMIT {pair}  qty={bs_str}  price={lp_str}{note}"
                        f"  {reason}  attempt {attempt+1}", "info")
                    order_resp = await asyncio.to_thread(
                        self.client.limit_order_gtc_sell,
                        client_order_id=order_id,
                        product_id=pair,
                        base_size=bs_str,
                        limit_price=lp_str,
                        post_only=True
                    )
                else:
                    limit_px    = round(bid - offset, qdp)
                    lp_str      = f"{limit_px:.{qdp}f}"
                    raw_qty_buy = qty * cur_price / limit_px if limit_px else 0
                    bs_str      = f"{_m2.floor(raw_qty_buy * 10**bdp) / 10**bdp:.{bdp}f}"
                    note        = f"  (bid-{offset:.{qdp}f})" if offset else "  (at bid)"
                    self.log_message(
                        f"CLOSE BUY LIMIT {pair}  qty={bs_str}  price={lp_str}{note}"
                        f"  {reason}  attempt {attempt+1}", "info")
                    order_resp = await asyncio.to_thread(
                        self.client.limit_order_gtc_buy,
                        client_order_id=order_id,
                        product_id=pair,
                        base_size=bs_str,
                        limit_price=lp_str,
                        post_only=True
                    )

                raw     = order_resp.to_dict()
                success = raw.get('success', False)
                if not success:
                    err_obj = raw.get('error_response', {}) or {}
                    err_msg = err_obj.get('message') or err_obj.get('error') or str(raw)
                    self.log_message(
                        f"Close limit REJECTED {pair} attempt {attempt+1}: {err_msg}", "warn")
                    await asyncio.sleep(1)
                    continue

                placed_id = (raw.get('success_response', {}) or {}).get('order_id') or order_id
                filled_order = await self._wait_for_fill(placed_id, timeout_s=90)
                if filled_order:
                    avg = float(filled_order.get('average_filled_price', 0) or 0)
                    fees = float(filled_order.get('total_fees', 0) or 0)
                    if avg > 0:
                        close_filled_price = avg
                    qty_f = float(filled_order.get('filled_size', 0) or 0)
                    if qty_f > 0:
                        close_filled_qty = qty_f
                    if fees > 0:
                        self.log_message(f"Close fee: ${fees:.6f} {pair}", "warn")
                    else:
                        self.log_message(f"Close fee: $0.00 (maker limit) {pair}", "info")
                    break
                else:
                    self.log_message(
                        f"Close limit not filled in 90s ({pair} {close_side}) — "
                        f"cancelling, attempt {attempt+1}", "warn")
                    try:
                        await asyncio.to_thread(
                            self.client.cancel_orders, order_ids=[placed_id])
                    except Exception:
                        pass
                    await asyncio.sleep(1)

            if close_filled_price is None:
                # Fall back to market order
                self.log_message(
                    f"Close limit attempts exhausted — falling back to market ({pair})", "warn")
                order_id = str(uuid.uuid4())
                if close_side == 'sell':
                    resp = await asyncio.to_thread(
                        self.client.market_order_sell,
                        client_order_id=order_id,
                        product_id=pair,
                        base_size=f"{qty_adj:.{bdp}f}"
                    )
                else:
                    resp = await asyncio.to_thread(
                        self.client.market_order_buy,
                        client_order_id=order_id,
                        product_id=pair,
                        quote_size=str(round(qty * cur_price, 2))
                    )
                raw     = resp.to_dict()
                success = raw.get('success', False)
                if not success:
                    err_msg = raw.get('error_response', {}).get('message', str(raw))
                    self.log_message(
                        f"ABANDONED {pair} trade — all close attempts failed ({reason}): {err_msg}  "
                        f"Removing from active positions. Verify on Coinbase.", "error")
                    logger.error(f"_close_trade all attempts failed: {raw}")
                    # Remove the zombie trade from history so the monitor stops retrying.
                    # _closing stays True to block any concurrent calls that haven't returned yet.
                    self.trade_history.pop(tid, None)
                    save_trade_history(self.trade_history)
                    if self.root_alive:
                        self.root.after(0, self._refresh_trade_rows)
                    return
                close_filled_price = cur_price
                sr2 = (raw.get('success_response', {}) or {})
                avg2 = float(sr2.get('average_filled_price', 0) or 0)
                if avg2 > 0 and (cur_price == 0 or abs(avg2 - cur_price) / cur_price < 0.20):
                    close_filled_price = avg2

            if close_filled_qty is None:
                close_filled_qty = qty

            # Use actual fill price for P&L and proceeds calculation
            cur_price = close_filled_price
            pl = ((cur_price - trade['entry_price']) * close_filled_qty
                  if trade['side'] == 'buy'
                  else (trade['entry_price'] - cur_price) * close_filled_qty)
            proceeds = close_filled_qty * cur_price
            if close_side == 'sell':
                # Drain coin sources proportionally (same logic as regular sell)
                coin_qty = self.bot_coin_qty.get(pair, 0)
                bot_exp  = self.bot_exposure.get(pair, 0)
                total_q  = coin_qty + (bot_exp / cur_price if cur_price else 0)
                if total_q > 0:
                    user_frac = coin_qty / total_q
                    bot_frac  = 1.0 - user_frac
                    self.bot_balance          += proceeds * user_frac
                    self.bot_pair_alloc[pair] += proceeds * bot_frac
                else:
                    self.bot_pair_alloc[pair] += proceeds
                drained = min(qty, coin_qty)
                self.bot_coin_qty[pair] = max(0, coin_qty - drained)
                self.bot_exposure[pair] = max(0, bot_exp - proceeds * (1.0 - (coin_qty / total_q if total_q else 0)))
                # Apply swap-on-sell for SL/TP closes too
                await self._execute_swap(pair, proceeds)
            else:
                self.bot_pair_alloc[pair] = max(0, self.bot_pair_alloc[pair] - proceeds)
                self.bot_exposure[pair]   += proceeds
            self.log_message(
                f"CLOSED {trade['side'].upper()} {pair}  "
                f"qty={qty:.6f} @ {format_price(cur_price)}  "
                f"{reason}  P&L=${pl:+.2f}", "trade")
            # Reset cooldown on the close direction so a new entry can fire sooner
            _ct = time.time()
            self.strategy.last_trade_time[pair] = _ct
            if close_side == 'sell':
                self.strategy.last_sell_time[pair] = _ct
            else:
                self.strategy.last_buy_time[pair] = _ct
            del self.trade_history[tid]
            save_trade_history(self.trade_history)
            self._save_bot_state()
            if self.root_alive:
                self.root.after(0, self._refresh_trade_rows)
                self.root.after(0, self._update_metrics)
            await self._fetch_balance()
        except Exception as e:
            self.log_message(f"Close trade error {pair}: {e}", "error")
            logger.error(traceback.format_exc())
            # CRITICAL: reset _closing so the monitor can retry on the next tick.
            # Without this, a Python exception leaves the flag True forever, silently
            # preventing any future close attempt on this position.
            trade = self.trade_history.get(tid)
            if trade is not None:
                trade.pop('_closing', None)
            # Resync balance even on failure — real holdings may have changed
            try:
                await self._fetch_balance()
            except Exception:
                pass

    async def _cancel_all_orders(self):
        # Advanced Trade doesn't have persistent open orders for market orders,
        # but we cancel any open limit orders that may exist
        try:
            resp  = await asyncio.to_thread(self.client.list_orders,
                                             order_status="OPEN")
            raw   = resp.to_dict()
            ids   = [o['order_id'] for o in raw.get('orders', [])]
            if ids:
                await asyncio.to_thread(self.client.cancel_orders, order_ids=ids)
                self.log_message(f"Cancelled {len(ids)} open order(s)", "warn")
        except Exception as e:
            self.log_message(f"Cancel orders error: {e}", "error")

    async def _sell_all(self, coins: list):
        for pair in TRADING_PAIRS:
            coin = pair.split('-')[0]
            if coin not in coins:
                continue
            qty = self.bot_exposure[pair] / max(self.live_prices.get(pair, 1), 1e-12)
            if qty < 1e-8:
                continue
            try:
                import math as _m_sell
                dp  = self._base_precision.get(pair, 8)
                qty = _m_sell.floor(qty * (10 ** dp)) / (10 ** dp)
                if qty < 10 ** (-dp):
                    continue
                resp = await asyncio.to_thread(
                    self.client.market_order_sell,
                    client_order_id=str(uuid.uuid4()),
                    product_id=pair,
                    base_size=f"{qty:.{dp}f}"
                )
                proceeds = qty * self.live_prices.get(pair, 0)
                self.usd_balance        += proceeds
                self.bot_exposure[pair]  = 0.0
                self.log_message(f"Sold {coin} → ${proceeds:.2f}", "trade")
                if self.root_alive:
                    self.root.after(0, self._update_metrics)
            except Exception as e:
                self.log_message(f"Sell error {pair}: {e}", "error")

    # ── Webhook ───────────────────────────────────────────────────────────────
    def _start_webhook(self):
        app_ref = self
        class Handler(WebhookHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, app=app_ref, **kw)
        try:
            srv = socketserver.TCPServer(('', WEBHOOK_PORT), Handler)
            srv.allow_reuse_address = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except OSError:
            self.log_message(f"Webhook port {WEBHOOK_PORT} busy — skipping", "warn")

    # ── Shutdown — INSTANT ────────────────────────────────────────────────────
    def on_close(self):
        self.running    = False
        self.root_alive = False
        save_trade_history(self.trade_history)
        save_candle_cache(self.candle_history)
        try:
            tasks = asyncio.all_tasks(self.loop)
            for t in tasks:
                t.cancel()
        except Exception:
            pass
        os._exit(0)   # immediate — no hung threads


# ── App root ──────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        # Load saved theme before building UI
        try:
            with open(CONFIG_FILE) as _f:
                _cfg = json.load(_f)
            _apply_theme(_cfg.get("theme", "Midnight"))
        except Exception:
            _apply_theme("Midnight")

        super().__init__()
        self.title("NEXXUS — Crypto Bot")
        self.geometry("1300x840")
        self.minsize(1100, 700)
        self.configure(fg_color=C_BG)

        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            try:
                from PIL import Image, ImageTk
                img   = Image.open(icon_path).resize((32, 32), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.iconphoto(True, photo)
                self._icon_ref = photo
            except Exception:
                pass

        self.dashboard = None
        LoginScreen(self, self._on_login)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_login(self, key, secret, passphrase):
        self.dashboard = Dashboard(self, key, secret, passphrase)

    def _on_close(self):
        save_trade_history(self.dashboard.trade_history if self.dashboard else {})
        os._exit(0)   # instant kill — no hanging threads or loops


if __name__ == "__main__":
    crash_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")
    try:
        app = App()
        app.mainloop()
    except Exception:
        err = traceback.format_exc()
        with open(crash_log, "w") as f:
            f.write(err)
        try:
            import tkinter as _tk
            _r = _tk.Tk(); _r.withdraw()
            messagebox.showerror("NEXXUS — Startup Error",
                                 f"Crash log saved to:\n{crash_log}\n\n{err[:600]}")
            _r.destroy()
        except Exception:
            pass
        sys.exit(1)
