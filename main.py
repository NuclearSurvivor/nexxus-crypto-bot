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
    TRADING_PAIRS, COINBASE_WS_URL, MAX_HISTORY, DISPLAY_CANDLES, FETCH_CANDLES,
    MA_PERIODS, TIMEFRAMES, ORDER_AMOUNT_USD, MINIMUM_RESERVE, WEBHOOK_PORT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAIL_STOP_PCT, ATR_PERIOD, COOLDOWN_SECONDS,
    SURGE_WINDOW, SURGE_PCT, SURGE_COOLDOWN,
    MAX_EXPOSURE_PER_PAIR, TF_TO_GRANULARITY, COINBASE_MAX_CANDLES, CONFIG_FILE
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d  %(levelname)-5s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'bot.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("nexxus")

# ── Theme ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Render budget — all interactive throttles use this single constant
_FRAME_DT = 1.0 / 144   # ~6.94 ms per frame @ 144 fps

# v2 color system — layered depth, trading terminal palette
C_BG       = "#070a0f"   # base background
C_PANEL    = "#0d1117"   # sidebar / panel layer
C_CARD     = "#131923"   # card background
C_CARD2    = "#1a2030"   # elevated / hover card
C_BORDER   = "#1e2538"   # subtle border
C_BORDER2  = "#28334d"   # stronger border / dividers
C_ACCENT   = "#00c8ff"   # cyan — primary accent
C_ACCENT2  = "#6e56cf"   # purple — secondary accent
C_ACCENT3  = "#f59e0b"   # amber — portfolio / value highlight
C_GREEN    = "#10b981"   # emerald green — profit / buy
C_RED      = "#ef4444"   # red — loss / sell
C_ORANGE   = "#f97316"   # orange — warning
C_TEXT     = "#f1f5f9"   # primary text
C_TEXT2    = "#94a3b8"   # secondary text
C_MUTED    = "#475569"   # muted / disabled
C_CHART_BG = "#07090e"   # chart background (deepest)
C_NAV_ACT  = "#141c2a"   # active nav item bg

plt.rcParams.update({
    "figure.facecolor":  C_CHART_BG,
    "axes.facecolor":    C_CHART_BG,
    "axes.edgecolor":    C_BORDER,
    "xtick.color":       C_MUTED,
    "ytick.color":       C_MUTED,
    "xtick.labelsize":   7,
    "ytick.labelsize":   7,
    "grid.color":        "#111827",
    "grid.linestyle":    "-",
    "grid.alpha":        0.6,
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
        center = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=20,
                              border_width=1, border_color=C_BORDER)
        center.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.40, relheight=0.78)

        ctk.CTkLabel(center, text="⬡", font=("Segoe UI", 52),
                     text_color=C_ACCENT).pack(pady=(32, 2))
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
        self.bot_balance     = 0.0        # general (unassigned) bot pool
        self.bot_pair_alloc  = defaultdict(float)   # per-coin USD budget
        self.bot_coin_qty    = defaultdict(float)   # coins handed to bot from holdings
        self.initial_balance = 0.0
        self.bot_exposure    = defaultdict(float)
        self.real_exposure   = defaultdict(float)
        self.live_prices      = defaultdict(float)
        self._price_ts        = defaultdict(float)   # C4: timestamp of last price update
        self._base_precision  = {}                   # pair → int decimal places for base_size
        self.price_history   = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
        self.candle_history  = {tf: defaultdict(lambda: deque(maxlen=MAX_HISTORY))
                                 for tf in TIMEFRAMES}
        self.percent_change  = {tf: defaultdict(float) for tf in TIMEFRAMES}
        self.pct_24h         = {}   # true 24h change (last 2 daily candles); None until loaded
        self.trade_history   = load_trade_history()
        self.order_locks     = defaultdict(bool)

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
        self.alloc_round_tokens = int(_s.get('alloc_round_tokens', 250))

        self.running         = True
        self.paused          = False
        self.root_alive      = True

        self.last_executed_signal = None   # set when an order is confirmed filled

        self.indicator_engine = IndicatorEngine()
        self.strategy         = MACrossover(self.indicator_engine)
        self._ws_jwt_ts       = 0.0   # timestamp of last WS JWT
        self.surge_last_buy   = defaultdict(float)   # H3: per-pair per-direction cooldown
        self.surge_last_sell  = defaultdict(float)

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

        ctk.CTkFrame(self.sidebar, height=1, fg_color=C_BORDER).pack(
            fill="x", padx=16, pady=(20, 12))

        # ── Navigation ───────────────────────────────────────────────────────
        nav = [
            ("dashboard", "⬛", "Dashboard",  self._show_dashboard),
            ("charts",    "📈", "Live Charts", self._show_charts),
            ("trades",    "💼", "Trades",      self._show_trades),
            ("settings",  "⚙",  "Settings",    self._show_settings),
            ("logs",      "📋", "Logs",        self._show_logs),
        ]
        for name, icon, label, cmd in nav:
            self._nav_button(name, icon, label, cmd)

        # ── Status & Controls ────────────────────────────────────────────────
        ctk.CTkFrame(self.sidebar, height=1, fg_color=C_BORDER).pack(
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
            self.sidebar, text="  🛑  Emergency Stop", height=40, corner_radius=8,
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

        # ── Version footer ───────────────────────────────────────────────────
        ctk.CTkFrame(self.sidebar, height=1, fg_color=C_BORDER).pack(
            fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(self.sidebar, text="v2.0  ·  Coinbase Advanced Trade",
                     font=("Segoe UI", 9), text_color=C_MUTED).pack(pady=(0, 16))

    def _nav_button(self, name: str, icon: str, label: str, cmd):
        """Create a sidebar nav button with active-state accent indicator."""
        row = ctk.CTkFrame(self.sidebar, fg_color="transparent", corner_radius=9)
        row.pack(fill="x", padx=10, pady=2)
        row.pack_propagate(False)
        row.configure(height=44)

        accent = ctk.CTkFrame(row, width=3, fg_color="transparent", corner_radius=2)
        accent.pack(side="left", fill="y", padx=(4, 0), pady=6)
        accent.pack_propagate(False)

        btn = ctk.CTkButton(
            row, text=f" {icon}   {label}", height=44, corner_radius=8,
            fg_color="transparent", hover_color=C_CARD2,
            font=("Segoe UI", 13), text_color=C_TEXT2, anchor="w",
            command=cmd
        )
        btn.pack(side="left", fill="both", expand=True, padx=(2, 4))
        self._nav_items[name] = {'row': row, 'accent': accent, 'btn': btn}

    def _set_active_nav(self, name: str):
        for n, refs in self._nav_items.items():
            active = (n == name)
            refs['accent'].configure(fg_color=C_ACCENT2 if active else "transparent")
            refs['row'].configure(fg_color=C_NAV_ACT if active else "transparent")
            refs['btn'].configure(
                text_color=C_TEXT if active else C_TEXT2,
                font=("Segoe UI", 13, "bold") if active else ("Segoe UI", 13),
            )

    def _build_topbar(self):
        top = ctk.CTkFrame(self.main_area, height=62, fg_color=C_PANEL, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)

        # Bottom border line
        ctk.CTkFrame(top, height=1, fg_color=C_BORDER).pack(side="bottom", fill="x")

        # Left: page title with accent left bar
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="y", padx=(0, 0))

        ctk.CTkFrame(left, width=3, fg_color=C_ACCENT2, corner_radius=0).pack(
            side="left", fill="y", pady=14)

        self.page_title = ctk.CTkLabel(
            left, text="Dashboard",
            font=("Segoe UI", 17, "bold"), text_color=C_TEXT)
        self.page_title.pack(side="left", padx=(14, 0))

        # Centre: live price tickers
        tickers = ctk.CTkFrame(top, fg_color="transparent")
        tickers.pack(side="left", fill="y", padx=30)

        for pair in TRADING_PAIRS:
            tf = ctk.CTkFrame(tickers, fg_color=C_CARD, corner_radius=8,
                              border_width=1, border_color=C_BORDER)
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

        # Right: portfolio + P&L
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right", fill="y", padx=20)

        self.bal_label = ctk.CTkLabel(right, text="Portfolio  —",
                                       font=("Segoe UI", 11), text_color=C_MUTED)
        self.bal_label.pack(anchor="e", pady=(10, 0))
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

    # ── Dashboard page ────────────────────────────────────────────────────────
    def _make_dashboard_page(self):
        page = ctk.CTkFrame(self.content, fg_color=C_BG, corner_radius=0)

        # ── Row 1: Metric cards ──────────────────────────────────────────────
        mrow = ctk.CTkFrame(page, fg_color="transparent")
        mrow.pack(fill="x", padx=20, pady=(20, 0))
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
        prow.pack(fill="x", padx=20, pady=(14, 0))
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
        ctk.CTkFrame(bsc, height=1, fg_color=C_BORDER).pack(fill="x", padx=16, pady=(6, 6))
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
        af = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=14,
                          border_width=1, border_color=C_BORDER)
        af.pack(fill="both", expand=True, padx=20, pady=(14, 20))

        af_hdr = ctk.CTkFrame(af, fg_color="transparent")
        af_hdr.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(af_hdr, text="📡  ACTIVITY FEED", font=("Segoe UI", 9, "bold"),
                     text_color=C_MUTED).pack(side="left")
        ctk.CTkFrame(af, height=1, fg_color=C_BORDER).pack(fill="x", padx=0, pady=(8, 0))

        self.activity_box = ctk.CTkTextbox(
            af, fg_color="transparent", text_color=C_TEXT,
            font=("Courier New", 11), state="disabled")
        self.activity_box.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        return page

    def _metric_card(self, parent, title, value, color, icon=""):
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=14,
                            border_width=1, border_color=C_BORDER)
        # Colored accent bar along top
        ctk.CTkFrame(card, height=3, fg_color=color, corner_radius=2).pack(
            fill="x", pady=(0, 0))
        # Title row
        th = ctk.CTkFrame(card, fg_color="transparent")
        th.pack(fill="x", padx=16, pady=(10, 2))
        if icon:
            ctk.CTkLabel(th, text=icon, font=("Segoe UI", 11),
                         text_color=color).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(th, text=title.upper(), font=("Segoe UI", 9, "bold"),
                     text_color=C_MUTED).pack(side="left")
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
        coin = pair.split("-")[0]
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=14,
                            border_width=1, border_color=C_BORDER)
        # Header: pair name + change badge
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(14, 4))
        ctk.CTkLabel(hdr, text=f"{coin} / USD", font=("Segoe UI", 11, "bold"),
                     text_color=C_TEXT2).pack(side="left")
        badge_bg = ctk.CTkFrame(hdr, fg_color=C_CARD2, corner_radius=7)
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
        tb = ctk.CTkFrame(page, fg_color=C_PANEL, corner_radius=0, height=52)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        ctk.CTkFrame(tb, height=1, fg_color=C_BORDER).pack(side="bottom", fill="x")

        row = ctk.CTkFrame(tb, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=10)

        # Pair selector
        ctk.CTkSegmentedButton(
            row, values=TRADING_PAIRS, variable=self.chart_pair_var,
            command=self._on_chart_pair_change, height=32
        ).pack(side="left", padx=(0, 0), pady=10)

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
        gs = self.chart_fig.add_gridspec(1, 2, width_ratios=[5, 1], wspace=0.02)
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
        hdr = ctk.CTkFrame(page, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(20, 10))
        ctk.CTkLabel(hdr, text="SYSTEM LOGS", font=("Segoe UI", 10, "bold"),
                     text_color=C_MUTED).pack(side="left")
        ctk.CTkButton(hdr, text="Clear", width=76, height=30, corner_radius=7,
                      fg_color=C_CARD, hover_color=C_CARD2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 11), text_color=C_TEXT2,
                      command=self._clear_logs).pack(side="right", padx=(6, 0))
        ctk.CTkButton(hdr, text="⎘  Copy", width=84, height=30, corner_radius=7,
                      fg_color=C_CARD, hover_color=C_CARD2,
                      border_width=1, border_color=C_BORDER,
                      font=("Segoe UI", 11), text_color=C_TEXT2,
                      command=self._copy_logs).pack(side="right", padx=(6, 0))
        self.log_filter = ctk.StringVar(value="All")
        ctk.CTkSegmentedButton(hdr, values=["All", "Trades", "Errors"],
                                variable=self.log_filter, height=30,
                                command=self._filter_logs).pack(side="right", padx=12)
        self.log_box = ctk.CTkTextbox(
            page, fg_color=C_CARD, corner_radius=14,
            border_width=1, border_color=C_BORDER,
            text_color=C_TEXT, font=("Courier New", 12), state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self._all_logs = []
        return page

    # ── Page navigation ───────────────────────────────────────────────────────
    def _show_page(self, name, title):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.page_title.configure(text=title)
        self._set_active_nav(name)

    def _show_dashboard(self): self._show_page("dashboard", "Dashboard")
    def _show_charts(self):    self._show_page("charts",    "Live Charts")
    def _show_trades(self):    self._show_page("trades",    "Trades")
    def _show_settings(self):  self._show_page("settings",  "Settings")
    def _show_logs(self):      self._show_page("logs",      "Logs")

    # ── Logging (thread-safe) ─────────────────────────────────────────────────
    def log_message(self, message: str, level="info"):
        now   = datetime.now(pytz.UTC)
        ts_ui = now.strftime("%H:%M:%S")
        entry = f"[{ts_ui}] {message}\n"
        # Write to bot.log with full date + ms and level tag
        log_fn = {"error": logger.error, "warn": logger.warning,
                  "trade": logger.info,  "info":  logger.info}.get(level, logger.info)
        log_fn(f"[{level.upper():<5}] {message}")
        cmap  = {"error": C_RED, "warn": C_ORANGE, "trade": C_GREEN, "info": C_TEXT}
        color = cmap.get(level, C_TEXT)
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

    def _gui_append_activity(self, entry):
        try:
            self.activity_box.configure(state="normal")
            self.activity_box.insert("end", entry)
            self.activity_box.configure(state="disabled")
            self.activity_box.see("end")
        except Exception:
            pass

    def _clear_logs(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _copy_logs(self):
        try:
            text = self.log_box.get("1.0", "end").strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log_message("Logs copied to clipboard", "info")
        except Exception:
            pass

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
            bot_display_total = bot_total + coin_holdings_val_mc
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
            # Update topbar ticker
            if pair in self._ticker_labels and price:
                pct_str = f"{'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%"
                self._ticker_labels[pair]['price'].configure(
                    text=format_price(price), text_color=col)
                self._ticker_labels[pair]['pct'].configure(
                    text=pct_str, text_color=col)
                self._ticker_labels[pair]['frame'].configure(
                    border_color="#1a3324" if pct >= 0 else "#331a1a")

    def _on_chart_pair_change(self, _=None):
        """Update chart header pair label then refresh chart."""
        pair = self.chart_pair_var.get()
        self.chart_hdr_pair_lbl.configure(text=pair.replace("-", "/"))
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
        compute_data = data[-(2 * limit):]
        # data: only what the user sees on the chart
        data         = data[-limit:]
        ax           = self.chart_ax

        # Indicators computed on full 2× history for accuracy
        self.indicator_engine.calculate_support_resistance(pair, compute_data)
        self.indicator_engine.calculate_order_blocks(pair, compute_data)
        self.indicator_engine.calculate_fair_value_gaps(pair, compute_data)
        self.indicator_engine.calculate_atr(pair, compute_data)

        ind    = self.indicator_engine.data[pair]
        atr    = ind['atr']
        price  = self.live_prices.get(pair, 0)

        # Pre-compute warmed MAs from full history; take last `limit` values
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

        # ── 6. Signals — MA crossover replay across all timeframes ──────────
        # All crossovers shown solid regardless of timeframe — bot acts on
        # signal TF, other TFs show the same markers for context.

        sorted_periods = sorted(MA_PERIODS)
        p_fast = sorted_periods[0]
        p_slow = sorted_periods[1] if len(sorted_periods) > 1 else sorted_periods[0]

        self._signal_data = []
        buy_signal_drawn = sell_signal_drawn = False
        breakout_drawn   = False
        is_signal_tf     = (tf == self.signal_tf)

        ma_fast = ma_lines.get(p_fast, np.array([]))
        ma_slow = ma_lines.get(p_slow, np.array([]))
        _n = min(len(ma_fast), len(ma_slow), len(ts))

        if _n >= 2:
            _mf  = ma_fast[-_n:]
            _ms  = ma_slow[-_n:]
            _ts  = ts[-_n:]
            _hi  = highs[-_n:]
            _lo  = lows[-_n:]
            _cl  = closes[-_n:]

            for i in range(1, _n):
                prev = _mf[i - 1] - _ms[i - 1]
                curr = _mf[i]     - _ms[i]
                is_golden = (prev < 0 < curr)
                is_death  = (prev > 0 > curr)
                if not (is_golden or is_death):
                    continue

                action = 'buy' if is_golden else 'sell'

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
                    'confirmed':    is_signal_tf,
                    'source':       f'MA{p_fast}/MA{p_slow} Cross',
                    'price_str':    format_price(_cl[i]),
                    'time_str':     _ts[i].strftime('%Y-%m-%d %H:%M UTC'),
                    'ma9':          float(_mf[i]),
                    'ma20':         float(_ms[i]),
                    'p_fast':       p_fast,
                    'p_slow':       p_slow,
                    'atr':          atr,
                })
                if action == 'buy':  buy_signal_drawn  = True
                else:                sell_signal_drawn = True

        # ── 6b. Breakout signals (same logic as calculate_breakout) ──────────
        if len(data) >= 25:
            _lb    = 20
            _cl_a  = closes
            _hi_a  = highs
            _lo_a  = lows
            _vol_a = np.array([c[5] for c in data])
            for i in range(_lb + 4, len(data)):
                _cur   = _cl_a[i]
                _prev  = _cl_a[i - 1]
                _p_hi  = _hi_a[i - _lb:i].max()
                _p_lo  = _lo_a[i - _lb:i].min()
                _avg_v = _vol_a[i - _lb:i].mean() or 1e-12
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
                        'time_str':     ts[i].strftime('%Y-%m-%d %H:%M UTC'),
                        'ma9':          float(ma_lines.get(p_fast, [0])[-1]) if ma_lines.get(p_fast, []) is not None and len(ma_lines.get(p_fast, [])) > 0 else 0,
                        'ma20':         float(ma_lines.get(p_slow, [0])[-1]) if ma_lines.get(p_slow, []) is not None and len(ma_lines.get(p_slow, [])) > 0 else 0,
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
                        'time_str':     ts[i].strftime('%Y-%m-%d %H:%M UTC'),
                        'ma9':          float(ma_lines.get(p_fast, [0])[-1]) if ma_lines.get(p_fast, []) is not None and len(ma_lines.get(p_fast, [])) > 0 else 0,
                        'ma20':         float(ma_lines.get(p_slow, [0])[-1]) if ma_lines.get(p_slow, []) is not None and len(ma_lines.get(p_slow, [])) > 0 else 0,
                        'p_fast':       p_fast,
                        'p_slow':       p_slow,
                        'atr':          atr,
                    })
                    breakout_drawn = True

        # ── 7. Live price line ───────────────────────────────────────────────
        if price:
            ax.axhline(y=price, color="#ffffff", linestyle=':', linewidth=0.8, alpha=0.4)
            ax.annotate(format_price(price),
                        xy=(ts[-1], price), fontsize=7.5,
                        color="#ffffff", alpha=0.85, ha='right', va='center',
                        bbox=dict(boxstyle='round,pad=0.2', fc="#1e2330",
                                  ec="#ffffff", alpha=0.7, lw=0.6))

        # ── 8. ATR-based SL/TP bands (long bias: SL below, TP above) ────────
        if atr > 0 and price:
            sl_pct = 1.5 * atr / price
            tp_pct = 3.0 * atr / price
            ax.axhline(y=price * (1 - sl_pct), color=C_RED,
                       linestyle=':', linewidth=0.7, alpha=0.4)
            ax.axhline(y=price * (1 + tp_pct), color=C_GREEN,
                       linestyle=':', linewidth=0.7, alpha=0.4)

        # ── 9. Axes formatting ───────────────────────────────────────────────
        # Restore user's pan/zoom limits if they've interacted with the chart
        if self._zoom_locked:
            _saved_xl = ax.get_xlim()
            _saved_yl = ax.get_ylim()

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

        if self._zoom_locked:
            ax.set_xlim(_saved_xl)
            ax.set_ylim(_saved_yl)

        zoom_hint   = "  [zoomed]" if self._zoom_locked else ""
        tf_note     = f"  [bot: {self.signal_tf}]" if not is_signal_tf else ""
        ax.set_title(
            f"{pair}  ·  {tf.upper()}  ·  {len(data)} candles{zoom_hint}{tf_note}",
            color=C_TEXT, fontsize=10, pad=6)

        # ── 10. Key panel ────────────────────────────────────────────────────
        ka = self.key_ax
        ka.clear()
        ka.set_facecolor(C_PANEL)
        ka.axis('off')
        ka.set_xlim(0, 1)
        ka.set_ylim(0, 1)

        ka.axvline(x=0.03, color=C_BORDER, linewidth=0.8, alpha=0.5)

        items = []   # (kind, color, text)
        items.append(('hdr',  C_ACCENT,    "CHART KEY"))
        items.append(('sp',   None,        None))
        items.append(('sym',  C_GREEN,     "▮  Bull Candle (HA)"))
        items.append(('sym',  C_RED,       "▮  Bear Candle (HA)"))
        items.append(('sp',   None,        None))
        for p, c in zip(MA_PERIODS, ma_colors):
            if len(ma_lines.get(p, [])) > 0:
                items.append(('sym', c, f"──  MA {p}"))
        items.append(('sp',   None,        None))

        # Signal legend — all TFs show solid markers
        if buy_signal_drawn:
            items.append(('sym', C_GREEN,   "▲  BUY signal"))
        if sell_signal_drawn:
            items.append(('sym', C_RED,     "▼  SELL signal"))
        if breakout_drawn:
            items.append(('sym', C_ACCENT3, "⚡  Breakout signal"))
        if not is_signal_tf and (buy_signal_drawn or sell_signal_drawn or breakout_drawn):
            items.append(('inf', C_MUTED,   f"Bot executes on {self.signal_tf}"))
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

        step = 0.95 / max(len(items), 1)
        y    = 0.97
        for kind, color, text in items:
            if kind == 'sp':
                y -= step * 0.5
                continue
            if kind == 'hdr':
                ka.text(0.5, y, text, transform=ka.transAxes,
                        color=color, fontsize=8.5, fontweight='bold',
                        ha='center', va='top', clip_on=True)
            elif kind == 'inf':
                ka.text(0.12, y, text, transform=ka.transAxes,
                        color=color, fontsize=6.8, va='top', ha='left',
                        fontfamily='monospace', clip_on=True)
            else:  # 'sym'
                ka.text(0.12, y, text, transform=ka.transAxes,
                        color=color, fontsize=7, va='top', ha='left', clip_on=True)
            y -= step

        self.chart_fig.tight_layout(pad=0.4)
        try:
            self.chart_canvas.draw_idle()
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
            self.chart_canvas.draw_idle()
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
        win = self._popup("Allocate to Bot", 460, 480)

        ctk.CTkLabel(win, text="＋  Allocate to Bot",
                     font=("Segoe UI", 15, "bold"), text_color=C_TEXT).pack(pady=(20, 0))

        # ── Mode toggle: USD budget  vs  Coin holdings ────────────────────────
        mode_var = ctk.StringVar(value="USD")
        mode_bar = ctk.CTkSegmentedButton(
            win, values=["USD Budget", "Coin Holdings"],
            variable=mode_var, width=300, height=34)
        mode_bar.pack(pady=(8, 0))

        form = ctk.CTkFrame(win, fg_color="transparent")
        form.pack(fill="x", padx=32, pady=(6, 0))

        # ── Coin selector ─────────────────────────────────────────────────────
        ctk.CTkLabel(form, text="Coin", font=("Segoe UI", 12),
                     text_color=C_MUTED).pack(anchor="w", pady=(4, 5))

        pair_var = ctk.StringVar(value=default_pair or TRADING_PAIRS[0])
        pair_row = ctk.CTkFrame(form, fg_color="transparent")
        pair_row.pack(fill="x", pady=(0, 6))

        def _make_pair_buttons():
            for w2 in pair_row.winfo_children():
                w2.destroy()
            mode  = mode_var.get()
            for p in TRADING_PAIRS:
                coin  = p.split("-")[0]
                price = self.live_prices.get(p, 0)
                if mode == "USD Budget":
                    sub = f"{format_price(price)}" if price else "—"
                else:
                    holdings_usd = self.real_exposure.get(p, 0)
                    holdings_qty = holdings_usd / price if price else 0
                    sub = f"{holdings_qty:.4f} {coin}" if holdings_qty else "0"
                is_sel = (p == pair_var.get())
                btn = ctk.CTkButton(
                    pair_row, text=f"{coin}\n{sub}",
                    width=118, height=54, corner_radius=10,
                    fg_color=C_ACCENT2 if is_sel else C_CARD,
                    hover_color="#6a4de0" if is_sel else C_BORDER,
                    border_width=2 if is_sel else 1,
                    border_color=C_ACCENT2 if is_sel else C_BORDER,
                    font=("Segoe UI", 11, "bold"), text_color=C_TEXT)
                btn.pack(side="left", padx=(0, 8))
                btn._pair = p
            _rewire_pair_buttons()
            _refresh_labels()

        def _rewire_pair_buttons():
            for b in pair_row.winfo_children():
                b.configure(command=lambda p=b._pair: _select_pair(p))

        def _select_pair(p):
            pair_var.set(p)
            for b in pair_row.winfo_children():
                sel = b._pair == p
                b.configure(
                    fg_color=C_ACCENT2 if sel else C_CARD,
                    hover_color="#6a4de0" if sel else C_BORDER,
                    border_width=2 if sel else 1,
                    border_color=C_ACCENT2 if sel else C_BORDER)
            _refresh_labels()
            _update_qfill()

        # ── Dynamic labels ────────────────────────────────────────────────────
        info_lbl  = ctk.CTkLabel(form, text="", font=("Segoe UI", 11),
                                  text_color=C_MUTED)
        info_lbl.pack(anchor="w", pady=(0, 6))
        input_lbl = ctk.CTkLabel(form, text="Amount (USD)", font=("Segoe UI", 12),
                                  text_color=C_MUTED)
        input_lbl.pack(anchor="w", pady=(0, 5))
        amt = ctk.CTkEntry(form, placeholder_text="0.00", height=42,
                           fg_color=C_CARD, border_color=C_BORDER,
                           text_color=C_TEXT, font=("Segoe UI", 15))
        amt.pack(fill="x")
        amt.focus()

        qrow = ctk.CTkFrame(form, fg_color="transparent")
        qrow.pack(fill="x", pady=(5, 0))
        qbtns = []
        for pct in (25, 50, 75, 100):
            b = ctk.CTkButton(qrow, text=f"{pct}%", width=72, height=28,
                              corner_radius=6, fg_color=C_CARD, hover_color=C_BORDER,
                              font=("Segoe UI", 11), text_color=C_MUTED)
            b.pack(side="left", padx=(0, 6))
            qbtns.append((pct, b))

        def _refresh_labels():
            p    = pair_var.get()
            mode = mode_var.get()
            coin = p.split("-")[0]
            if mode == "USD Budget":
                info_lbl.configure(
                    text=f"Liquid USD: ${self.usd_balance:,.4f}  ·  "
                         f"Bot wallet ({coin}): ${self.bot_pair_alloc.get(p,0):,.2f}")
                input_lbl.configure(text="Amount in USD")
                amt.configure(placeholder_text="0.00 USD")
            else:
                price = self.live_prices.get(p, 0)
                qty   = self.real_exposure.get(p, 0) / price if price else 0
                info_lbl.configure(
                    text=f"You hold:  {qty:.6f} {coin}  "
                         f"≈ ${self.real_exposure.get(p,0):,.2f}  ·  "
                         f"Bot manages: {self.bot_coin_qty.get(p,0):.6f} {coin}")
                input_lbl.configure(text=f"Amount in {coin}  (or % of holdings)")
                amt.configure(placeholder_text=f"0.000000 {coin}")

        def _update_qfill():
            p    = pair_var.get()
            mode = mode_var.get()
            if mode == "USD Budget":
                total = self.usd_balance
            else:
                price = self.live_prices.get(p, 0)
                total = self.real_exposure.get(p, 0) / price if price else 0
            for pct, b in qbtns:
                v = total * pct / 100
                if mode == "Coin Holdings":
                    # round down to nearest alloc_round_tokens boundary
                    n = self.alloc_round_tokens
                    if n > 1:
                        import math as _math
                        v = _math.floor(v / n) * n
                fmt = f"{v:.2f}" if mode == "USD Budget" else f"{v:.6f}"
                b.configure(command=lambda x=fmt: (amt.delete(0, "end"),
                                                    amt.insert(0, x)))

        def _on_mode_change(_=None):
            _make_pair_buttons()

        mode_bar.configure(command=_on_mode_change)
        _make_pair_buttons()

        err = ctk.CTkLabel(form, text="", text_color=C_RED, font=("Segoe UI", 11))
        err.pack(pady=(6, 4))

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
                    if val > self.usd_balance:
                        err.configure(text=f"Max: ${self.usd_balance:,.2f}")
                        return
                    self.usd_balance       -= val
                    self.bot_pair_alloc[p] += val
                    self.log_message(
                        f"Allocated ${val:,.2f} USD → {coin} bot wallet", "trade")
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
                self._update_metrics()
                win.destroy()
            except ValueError:
                err.configure(text="Enter a valid number")

        ctk.CTkButton(form, text="Confirm Allocation", height=44, corner_radius=10,
                      fg_color=C_ACCENT2, hover_color="#6a4de0",
                      font=("Segoe UI", 13, "bold"), command=confirm).pack(fill="x")

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
            self.alloc_round_tokens = max(1, int(float(self.round_var.get())))
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
                'signal_tf':           self.signal_tf,
                'signal_direction':    self.signal_direction,
                'ma_periods':          list(self.custom_ma_periods),
                'swap_targets':        dict(self.swap_targets),
                'stop_loss_pct':       STOP_LOSS_PCT,
                'take_profit_pct':     TAKE_PROFIT_PCT,
                'order_amount_usd':    ORDER_AMOUNT_USD,
                'minimum_reserve':     MINIMUM_RESERVE,
                'cooldown_seconds':    COOLDOWN_SECONDS,
                'alloc_round_tokens':  self.alloc_round_tokens,
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
            'signal_tf':           self.signal_tf,
            'signal_direction':    self.signal_direction,
            'ma_periods':          list(periods),
            'swap_targets':        dict(self.swap_targets),
            'stop_loss_pct':       STOP_LOSS_PCT,
            'take_profit_pct':     TAKE_PROFIT_PCT,
            'order_amount_usd':    ORDER_AMOUNT_USD,
            'minimum_reserve':     MINIMUM_RESERVE,
            'cooldown_seconds':    COOLDOWN_SECONDS,
            'alloc_round_tokens':  self.alloc_round_tokens,
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
                self.log_message(
                    f"{pair}: {format_price(price) if price else '—'}  "
                    f"(base_increment={base_inc} → {self._base_precision[pair]}dp)", "info")
            except Exception as e:
                self.log_message(f"Price fetch {pair}: {e}", "warn")

        await asyncio.gather(*[_fetch_one(p) for p in TRADING_PAIRS])

    # ── Balance fetch ─────────────────────────────────────────────────────────
    async def _balance_loop(self):
        while self.running:
            await self._fetch_balance()
            await asyncio.sleep(60)

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

            for a in accounts:
                cur = a.get('currency', '')
                val = float(a.get('available_balance', {}).get('value', 0) or 0)
                if cur in ('USD', 'USDC', 'USDT'):
                    usd += val
                elif cur in pair_coins:
                    pair  = pair_coins[cur]
                    price = self.live_prices.get(pair, 0)
                    if price > 0:
                        self.real_exposure[pair] = val * price
                        coin_log.append(f"{cur}={val:.4f} (${val*price:,.2f})")
                    else:
                        coin_log.append(f"{cur}={val:.4f} (price unknown)")

            self.usd_balance = usd
            coin_str = "  |  ".join(coin_log) if coin_log else "none"
            self.log_message(
                f"Balance sync:  USD=${usd:,.2f}  |  Coins: {coin_str}  |  "
                f"bot_balance=${self.bot_balance:.2f}  "
                f"bot_alloc=${sum(self.bot_pair_alloc.values()):.2f}  "
                f"bot_exposure=${sum(self.bot_exposure.values()):.2f}  "
                f"bot_coin_qty={ {p.split('-')[0]: round(v,2) for p,v in self.bot_coin_qty.items() if v>0} }",
                "info")
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
        Coinbase Advanced Trade: ~10 req/s private, ~3 req/s public per IP.
        We stay conservative: one candle-batch call every 0.5 s.
        """
        while self.running:
            for pair in TRADING_PAIRS:
                for tf in TIMEFRAMES:
                    if not self.running:
                        return
                    await self._fetch_pair_tf(pair, tf)
                    await asyncio.sleep(0.5)   # 0.5 s between each call
            await asyncio.sleep(300)           # full refresh every 5 min

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
                f"count={len(all_candles)}  span={span_from} → {span_to}", "info")
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
            f"total_stored={len(existing)}", "info")

        if len(ha) >= 2:
            op, np_ = ha[0][4], ha[-1][4]
            self.percent_change[timeframe][pair] = ((np_ - op) / op * 100) if op else 0
            # True 24h change: last close vs prior close on daily candles
            if timeframe == '1d':
                prev_c, last_c = ha[-2][4], ha[-1][4]
                self.pct_24h[pair] = ((last_c - prev_c) / prev_c * 100) if prev_c else 0

        # Always recalculate indicators on the signal TF so SMC zones stay current.
        # Also keep 1h as a fallback so the chart has data even when signal_tf differs.
        if timeframe == self.signal_tf or timeframe == '1h':
            self.indicator_engine.calculate_support_resistance(pair, ha)
            self.indicator_engine.calculate_order_blocks(pair, ha)
            self.indicator_engine.calculate_fair_value_gaps(pair, ha)
            self.indicator_engine.calculate_atr(pair, ha)

        if timeframe == self.signal_tf and self.running and not self.paused:
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
                "info")
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
                    self.order_locks[pair] = True
                    self._last_signal_source = sig['source']
                    self.log_message(
                        f"► SIGNAL [{sig['source']}] {sig['action'].upper()} {pair} "
                        f"@ {format_price(sig['price'])}  "
                        f"conf_candles={len(conf_candles)}  tf={timeframe}→{conf_tf}",
                        "trade")
                    asyncio.run_coroutine_threadsafe(
                        self._place_order(pair, sig['action']), self.loop)
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
        while self.running:
            try:
                async with websockets.connect(
                    COINBASE_WS_URL,
                    ping_interval=20, ping_timeout=30,
                    max_size=2**21
                ) as ws:
                    # Fresh JWT for this connection
                    jwt = make_ws_jwt(self.api_key, self.api_secret)
                    self._ws_jwt_ts = time.time()

                    # Subscribe to both channels in one shot
                    for channel in ("ticker", "ticker_batch"):
                        await ws.send(json.dumps({
                            "type":        "subscribe",
                            "product_ids": TRADING_PAIRS,
                            "channel":     channel,
                            "jwt":         jwt,
                        }))
                    self.log_message(
                        f"WebSocket connected  url={COINBASE_WS_URL}  "
                        f"pairs={TRADING_PAIRS}  channels=[ticker, ticker_batch]", "trade")

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
                                    "info")
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
                self.log_message(f"WS disconnected: {e} — reconnect in 5s", "warn")
                logger.error(traceback.format_exc())
                await asyncio.sleep(5)

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

        # Log every surge candidate so we know why it was blocked or passed
        self.log_message(
            f"Surge candidate  {pair}  move={move*100:+.2f}%  ({SURGE_PCT*100:.1f}% threshold)  "
            f"action={action}  oldest={format_price(oldest)}  newest={format_price(newest)}  "
            f"pair_cap={pair_cap}  has_exposure={has_exposure}  has_coins={has_coins}  "
            f"locked={self.order_locks[pair]}  cd_remaining={cd_remaining:.0f}s  "
            f"direction={self.signal_direction}", "info")

        if not (pair_cap or has_exposure or has_coins):
            self.log_message(f"Surge {pair} blocked — no capital/coins", "info")
            return
        if self.order_locks[pair]:
            self.log_message(f"Surge {pair} blocked — order lock active", "info")
            return
        if cd_remaining > 0:
            self.log_message(
                f"Surge {pair} blocked — {action} cooldown {cd_remaining:.0f}s remaining", "info")
            return

        # Reversal guard: require majority of last 5 ticks to match direction
        if len(ticks) >= 5:
            recent = ticks[-5:]
            dirs   = [recent[i] - recent[i-1] for i in range(1, len(recent))]
            bull   = sum(1 for d in dirs if d > 0)
            bear   = sum(1 for d in dirs if d < 0)
            if action == 'buy' and bear > bull:
                self.log_message(
                    f"Surge {pair} BUY blocked — reversal guard  bull={bull} bear={bear}  "
                    f"ticks={[round(t,8) for t in recent]}", "info")
                return
            if action == 'sell' and bull > bear:
                self.log_message(
                    f"Surge {pair} SELL blocked — reversal guard  bull={bull} bear={bear}  "
                    f"ticks={[round(t,8) for t in recent]}", "info")
                return

        if self.signal_direction == 'Buy Only'  and action != 'buy':
            self.log_message(f"Surge {pair} {action.upper()} blocked — direction=Buy Only", "info")
            return
        if self.signal_direction == 'Sell Only' and action != 'sell':
            self.log_message(f"Surge {pair} {action.upper()} blocked — direction=Sell Only", "info")
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
        asyncio.create_task(self._place_order(pair, action))

    # ── Trade monitor ─────────────────────────────────────────────────────────
    async def _monitor_loop(self):
        _monitor_tick = 0
        while self.running:
            try:
                _monitor_tick += 1
                active = [(tid, t) for tid, t in self.trade_history.items()
                          if t.get('event') == 'trade']
                # Log position summary every 12 ticks (~60s) or when positions exist
                if active and _monitor_tick % 12 == 0:
                    self.log_message(
                        f"Monitor heartbeat — {len(active)} open position(s)  "
                        f"bot_balance=${self.bot_balance:.2f}  "
                        f"bot_exposure=${ sum(self.bot_exposure.values()):.2f }  "
                        f"coin_qty={ {p.split('-')[0]:round(v,2) for p,v in self.bot_coin_qty.items() if v>0} }",
                        "info")
                for tid, trade in active:
                    pair  = trade['symbol']
                    cur   = self.live_prices.get(pair) or trade['current_price']
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
                            hit_trail   = cur <= trail_sl
                            trail_active = True
                    else:
                        trough = min(trade.get('peak_price', entry), cur)
                        trade['peak_price'] = trough
                        trail_sl = trough * (1 + TRAIL_STOP_PCT)
                        if trough < entry * 0.99:
                            trade['stop_loss'] = min(orig_sl, trail_sl)
                            hit_trail   = cur >= trail_sl
                            trail_active = True

                    cur_sl = trade['stop_loss']
                    hit_sl = (trade['side'] == 'buy'  and cur <= cur_sl) or \
                             (trade['side'] == 'sell' and cur >= cur_sl)
                    hit_tp = (trade['side'] == 'buy'  and cur >= orig_tp) or \
                             (trade['side'] == 'sell' and cur <= orig_tp)

                    # Log every position on every tick for full traceability
                    trail_str = (f"  trail_sl={format_price(trail_sl)}"
                                 f"  peak={format_price(trade.get('peak_price',entry))}"
                                 if trail_active else "  trail=inactive")
                    self.log_message(
                        f"Monitor  {trade['side'].upper()} {pair}  "
                        f"qty={trade['quantity']:.4f}  "
                        f"entry={format_price(entry)}  cur={format_price(cur)}  "
                        f"P&L=${pl:+.4f}  "
                        f"SL={format_price(cur_sl)}  TP={format_price(orig_tp)}"
                        f"{trail_str}  "
                        f"hit_sl={hit_sl}  hit_tp={hit_tp}  hit_trail={hit_trail}",
                        "info")

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
    async def _place_order(self, pair: str, side: str, amount: float = None):
        """
        Place a live market order via Coinbase Advanced Trade REST API.
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
                amount_usd = min(amount, avail_funds)
                total_cap  = avail_funds + sum(self.bot_exposure.values())
                if self.bot_exposure[pair] + amount_usd > MAX_EXPOSURE_PER_PAIR * total_cap:
                    self.log_message(f"Exposure cap reached for {pair}", "warn")
                    return
            else:
                # Sell: use bot-traded exposure OR directly allocated coin qty
                bot_exp_usd  = self.bot_exposure.get(pair, 0)
                coin_qty_val = self.bot_coin_qty.get(pair, 0) * vp if vp else 0
                total_sell   = bot_exp_usd + coin_qty_val
                amount_usd   = min(amount, total_sell)
                if amount_usd <= 0:
                    self.log_message(f"No holdings to sell for {pair}", "warn")
                    return

            price = vp
            if not price:
                self.log_message(f"No live price for {pair}", "error")
                return

            order_id = str(uuid.uuid4())

            # Coinbase Advanced Trade market order
            # For buys use quote_size (USD); for sells use base_size (coin qty)
            if side == 'buy':
                quote_str = str(round(amount_usd, 2))
                self.log_message(
                    f"Placing BUY {pair}  quote_size={quote_str}  "
                    f"price≈{format_price(price)}", "info")
                order_resp = await asyncio.to_thread(
                    self.client.market_order_buy,
                    client_order_id=order_id,
                    product_id=pair,
                    quote_size=quote_str
                )
            else:
                dp  = self._base_precision.get(pair, 8)
                qty = amount_usd / price
                # Floor (not round) to never over-sell; respect Coinbase base_increment
                import math as _math
                qty = _math.floor(qty * (10 ** dp)) / (10 ** dp)
                min_qty = 10 ** (-dp)
                if qty < min_qty:
                    self.log_message(
                        f"Sell qty {qty} below min increment {min_qty} for {pair} — skipping",
                        "warn")
                    return
                base_str = f"{qty:.{dp}f}"
                self.log_message(
                    f"Placing SELL {pair}  base_size={base_str} ({dp}dp)  "
                    f"≈${amount_usd:.2f}  price≈{format_price(price)}", "info")
                order_resp = await asyncio.to_thread(
                    self.client.market_order_sell,
                    client_order_id=order_id,
                    product_id=pair,
                    base_size=base_str
                )

            raw = order_resp.to_dict()
            success = raw.get('success', False)
            if not success:
                err_obj = raw.get('error_response', {}) or {}
                reason  = err_obj.get('message') or err_obj.get('error') or str(raw)
                preview = err_obj.get('preview_failure_reason', '')
                detail  = f"  [{preview}]" if preview else ''
                self.log_message(
                    f"Order REJECTED {pair} {side.upper()}: {reason}{detail}", "error")
                return

            # Parse fill price: prefer success_response.average_filled_price,
            # fall back to live price.  Old base_size=1 default was catastrophically wrong.
            filled_price = price   # safe default
            if side == 'buy':
                try:
                    sr   = raw.get('success_response', {}) or {}
                    avg  = float(sr.get('average_filled_price', 0) or 0)
                    if avg > 0 and (price == 0 or abs(avg - price) / price < 0.20):
                        filled_price = avg
                    else:
                        conf = (raw.get('order_configuration', {})
                                   .get('market_market_ioc', {}))
                        q = float(conf.get('quote_size', 0) or 0)
                        b = float(conf.get('base_size',  0) or 0)
                        if q > 0 and b > 0:
                            parsed = q / b
                            if price == 0 or abs(parsed - price) / price < 0.20:
                                filled_price = parsed
                except Exception:
                    pass
            qty_filled   = amount_usd / filled_price

            sl = filled_price * (1 - sl_pct) if side == 'buy' else filled_price * (1 + sl_pct)
            tp = filled_price * (1 + tp_pct) if side == 'buy' else filled_price * (1 - tp_pct)

            spent = qty_filled * filled_price
            if side == 'buy':
                if use_pair_alloc:
                    self.bot_pair_alloc[pair] = max(0, self.bot_pair_alloc[pair] - spent)
                else:
                    self.bot_balance = max(0, self.bot_balance - spent)
                self.bot_exposure[pair] += spent
                self.bot_coin_qty[pair] += qty_filled
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
            self.strategy.last_trade_time[pair] = time.time()
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
            order_id = str(uuid.uuid4())
            if close_side == 'sell':
                dp  = self._base_precision.get(pair, 8)
                import math as _m2
                qty_adj  = _m2.floor(qty * (10 ** dp)) / (10 ** dp)
                min_qty  = 10 ** (-dp)
                if qty_adj < min_qty:
                    self.log_message(
                        f"Close qty {qty_adj} below min increment — skipping {reason}", "warn")
                    trade.pop('_closing', None)
                    return
                resp = await asyncio.to_thread(
                    self.client.market_order_sell,
                    client_order_id=order_id,
                    product_id=pair,
                    base_size=f"{qty_adj:.{dp}f}"
                )
            else:
                resp = await asyncio.to_thread(
                    self.client.market_order_buy,
                    client_order_id=order_id,
                    product_id=pair,
                    quote_size=str(round(qty * cur_price, 2))
                )

            # Verify the close order succeeded before touching balances
            raw     = resp.to_dict()
            success = raw.get('success', False)
            if not success:
                err_msg = raw.get('error_response', {}).get('message', str(raw))
                self.log_message(
                    f"Close order REJECTED {pair} ({reason}): {err_msg}", "error")
                logger.error(f"_close_trade rejected: {raw}")
                trade.pop('_closing', None)
                return

            pl = ((cur_price - trade['entry_price']) * qty
                  if trade['side'] == 'buy'
                  else (trade['entry_price'] - cur_price) * qty)
            proceeds = qty * cur_price
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
            # Reset cooldown so a new entry doesn't fire instantly after SL/TP
            self.strategy.last_trade_time[pair] = time.time()
            del self.trade_history[tid]
            save_trade_history(self.trade_history)
            if self.root_alive:
                self.root.after(0, self._refresh_trade_rows)
                self.root.after(0, self._update_metrics)
            await self._fetch_balance()
        except Exception as e:
            self.log_message(f"Close trade error {pair}: {e}", "error")
            logger.error(traceback.format_exc())
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
                resp = await asyncio.to_thread(
                    self.client.market_order_sell,
                    client_order_id=str(uuid.uuid4()),
                    product_id=pair,
                    base_size=str(round(qty, 8))
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
