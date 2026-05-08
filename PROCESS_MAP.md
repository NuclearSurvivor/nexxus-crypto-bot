# NEXXUS Crypto Bot — Process Map

**Purpose:** This document is the definitive reference for any developer or AI assistant working on this codebase. Read this before touching any code. It defines the purpose, ownership, and data flow of every major system in the bot.

---

## 0. File Structure

| File | Purpose |
|------|---------|
| `main.py` | Entire GUI + backend orchestration (~5800 lines) |
| `engine.py` | Pure computation: indicators, signals, candle maths, persistence |
| `config.json` | User settings, bot state, API credentials (written by app) |
| `trade_history.json` | All open/closed trade records |
| `candle_cache.json` | Persisted candle history (survives restarts) |

---

## 1. App Launch Sequence

```
App.__init__  (main.py:5766)
  └─ creates root Tk window, sets theme
  └─ LoginScreen.__init__  (main.py:329)
       └─ loads saved credentials from config.json
       └─ if credentials exist → auto-connect after 200ms
       └─ on success → LoginScreen.on_login() → Dashboard.__init__

Dashboard.__init__  (main.py:423)
  ├─ load_user_settings()          — reads config.json into memory
  ├─ load_trade_history()          — restores open trades
  ├─ load_candle_cache()           — pre-populates candle_history deques
  ├─ self.chart_pair_var / chart_tf_var  — MUST be created here, before backend
  ├─ _build_layout()               — builds sidebar, topbar, content frame
  ├─ _start_backend()              — starts asyncio thread (see §3)
  ├─ _start_webhook()              — starts HTTP webhook server
  └─ root.after() calls:
       ├─ 100ms  → _ui_tick        (see §5)
       ├─ 1000ms → _tick_status
       ├─ 1000ms → _chart_live_tick
       └─ 2000ms → _trade_pl_tick
```

**Critical rule:** `chart_pair_var` and `chart_tf_var` must be initialised in `Dashboard.__init__` before `_start_backend()` is called, because the candles loop reads them immediately on startup.

---

## 2. Two-Thread Model

The app runs exactly two threads:

```
Thread 1: Tk Main Thread
  • Owns all widget reads/writes
  • Runs root.mainloop()
  • Processes all root.after() callbacks
  • Never blocks for more than a few ms

Thread 2: Asyncio Backend Thread  (main.py:4140)
  • Owns all network I/O (Coinbase REST + WebSocket)
  • asyncio event loop: self.loop
  • Never touches Tk widgets directly
  • Communicates to Thread 1 via:
      - root.after(0, callback)       for one-shot UI updates
      - dirty flags (self._pair_cards_dirty, etc.)  for coalesced UI
      - thread-safe deques (self._log_queue, etc.)  for log buffering
```

**Thread-safety rules:**
- Dirty flags (`bool`, `set.add/discard`) are GIL-safe — no lock needed
- `deque.append/popleft` is GIL-safe — no lock needed
- `dict` and `defaultdict` simple reads/writes are GIL-safe
- `self._forming_candle` is protected by `self._forming_candle_lock` (threading.Lock) — always acquire before read OR write

---

## 3. Backend Asyncio Tasks

All tasks run inside `_async_main()` (main.py:4144), gathered with `asyncio.gather()`:

```
_safe_loop(_candles_loop)      — candle fetch, 5min cycle
_safe_loop(_websocket_loop)    — WS price ticks, order book
_safe_loop(_balance_loop)      — REST balance poll, 60s
_safe_loop(_monitor_loop)      — SL/TP/trail check, 5s
_safe_loop(_price_poll_loop)   — REST price fallback, 8s
```

`_safe_loop` wraps every task with exponential-backoff retry (2→4→8…→120s). A crashed task restarts automatically.

---

## 4. Candle Pipeline (most important flow)

```
_candles_loop  (main.py:4400)
  └─ for each pair × timeframe:
       └─ _fetch_pair_tf(pair, tf)  (main.py:4457)
            ├─ calls Coinbase REST get_candles (up to FETCH_CANDLES[tf] candles)
            ├─ batches backwards in time (max 300/call)
            └─ on success → await _process_candles(pair, all_candles, tf)

_process_candles  (main.py:4535)   ← runs on asyncio thread, NOT main thread
  ├─ Step 1: await asyncio.to_thread(heikin_ashi, candles)
  │           converts OHLCV → Heikin Ashi — CPU-heavy, off event loop
  ├─ Step 2: update candle_history[tf][pair] deque  (GIL-safe dict/deque ops)
  │           compute percent_change, pct_24h
  ├─ Step 3: await asyncio.to_thread(_run_indicators, pair, ha)
  │           ATR, S/R, order blocks, FVGs, RSI, ADX  — CPU-heavy
  ├─ Step 4: await asyncio.to_thread(_compute_ma_cache, ha)
  │           SMA arrays for all MA_PERIODS — stored in self._ma_cache
  ├─ Step 5: signal check (on asyncio thread — fast)
  │           strategy.calculate_signals() + calculate_breakout()
  │           if signal fires → asyncio.ensure_future(_place_with_timeout())
  └─ Step 6: root.after(0, _mark_chart_dirty, pair, tf)
             tiny main-thread callback — sets _chart_dirty and _pair_cards_dirty

KEY: Nothing CPU-heavy runs on the main thread. Only dirty-flag writes reach Tk.
```

---

## 5. UI Tick Loop (`_ui_tick`, 100ms)

The single coalescing heartbeat for all UI updates:

```
_ui_tick  (main.py:2140)   runs every 100ms on main thread
  if _modal_open > 0:
      SKIP ALL — modal dialog is open, keep Tk queue empty
  else:
      if _topbar_dirty:     update topbar price/% labels (fast)
      if _pair_cards_dirty: _update_pair_cards() + portfolio card
      if _log_queue:        drain ≤30 lines into log_box text widget
      if _mon_queue:        drain ≤30 lines into monitor_box
      if _act_queue:        drain ≤30 lines into activity_box
      every 20 ticks (2s):  _update_metrics()  — all cards + bot status
```

**Modal gate:** When any popup is open, `_modal_open > 0`. This suspends ALL heavy Tk work so popup clicks register instantly. Data accumulates in deques and flushes when popup closes.

---

## 6. Chart Rendering Pipeline

```
Data source:  candle_history[tf][pair]  (deque, updated by _process_candles)
              _forming_candle[(pair,tf)] (live partial candle, updated by WS)
              _ma_cache[(pair,tf)]       (pre-computed SMA arrays)
              indicator_engine.data[pair] (ATR, RSI, S/R, OBs, FVGs)

Trigger path:
  _mark_chart_dirty(pair, tf) ← called from _process_candles via root.after(0,...)
    └─ sets self._chart_dirty = True  (if pair/tf matches current view)

  _chart_live_tick  (main.py:3010, every 1s)
    ├─ if _chart_dirty and not _chart_rendering:
    │     _refresh_chart()  → full redraw path
    └─ elif _chart_bg_valid:
          _chart_blit_price()  → fast blit path (just moves price line)

Full redraw path (_refresh_chart):
  1. Reads candle data, appends forming candle, computes HA
  2. Draws all artists on matplotlib axes (candles, MAs, indicators, signals)
  3. _bg_render() submitted to ThreadPoolExecutor
     └─ canvas.draw()  [Agg rasterisation, off main thread]
     └─ root.after(0, _on_render_complete)

  _on_render_complete  (main thread):
     └─ copy_from_bbox() → save background
     └─ draw price line artist
     └─ blit()  → fast screen update

Fast blit path (_chart_blit_price):
  └─ restore_region() from saved background
  └─ move price line to new price
  └─ draw_artist() + blit()  → <5ms, no full redraw
```

**Rule:** `chart_canvas.draw()` always runs in the ThreadPoolExecutor (never on main thread). Only `blit()`, `draw_artist()`, `copy_from_bbox()` run on main thread.

---

## 7. WebSocket Pipeline

```
_websocket_loop  (main.py:4640)
  └─ connects to wss://advanced-trade-ws.coinbase.com
  └─ subscribes: ticker, ticker_batch, level2  (3 channels, all TRADING_PAIRS)
  └─ JWT renewed every 90s (token valid 120s)
  └─ on each message:

    channel == "l2_data":
      → update self._order_book[pair] bids/asks lists (sorted, capped at 50 levels)
      → if order book popup open: throttled root.after(0, _update_ob_display)
         (throttled by _ob_update_pending flag — at most 1 queued at a time)

    channel == "ticker" or "ticker_batch":
      → self.live_prices[pair] = price
      → self._price_ts[pair] = now
      → self.price_history[pair].append(price)  (deque, SURGE_WINDOW deep)
      → update self._forming_candle[(pair,tf)] for all 4 timeframes (under lock)
      → set self._pair_cards_dirty = True
      → self._topbar_dirty.add(pair)
      → if channel == "ticker": await _check_surge(pair)
```

---

## 8. Order Placement Flow

```
Signal fires in _process_candles
  └─ asyncio.ensure_future(_place_with_timeout(pair, action))
       └─ asyncio.wait_for(_place_order(pair, action, fast_exec=True), 300s)

_place_order(pair, side)  (main.py:5082)
  BUY path:
    1. Stale price check (>30s → skip)
    2. Compute sl_pct = 1.5 × ATR/price,  tp_pct = 3.0 × ATR/price
    3. Capital check: prefer bot_pair_alloc[pair], fall back to bot_balance
    4. FEE GATE: tp_profit_usd must exceed 2 × FEE_PCT × amount_usd
    5. Exposure cap check (MAX_EXPOSURE_PER_PAIR)
    6. Compute limit price (bid + 1 tick for buy → post_only maker)
    7. Place limit_order_gtc_buy()
    8. _wait_for_fill() polls every 5s up to timeout
    9. On fill: update bot_bought_qty, bot_exposure, record trade_history

  SELL path:
    1. Compute sell_qty = bot_bought_qty + bot_coin_qty
    2. Place limit_order_gtc_sell()
    3. On fill: proceeds → bot_pair_alloc[pair] (NEVER bot_balance)
                clear bot_coin_qty, bot_bought_qty, bot_exposure

  fast_exec=True (signal-triggered):
    2 attempts × 25s timeout, then market fallback
  fast_exec=False (normal):
    5 attempts × 120s timeout, no market fallback
```

---

## 9. Capital Accounting

**All amounts are in USD unless noted.**

| Variable | Meaning | Owner |
|----------|---------|-------|
| `usd_balance` | Real liquid USD on Coinbase (from REST) | REST fetch |
| `bot_balance` | General bot pool (unallocated to specific pair) | User allocation |
| `bot_pair_alloc[pair]` | USD earmarked for specific pair | User allocation + sell proceeds |
| `bot_exposure[pair]` | USD currently deployed as open position | Order fills |
| `bot_bought_qty[pair]` | Coins bought by bot (base units) | Order fills |
| `bot_coin_qty[pair]` | Coins user allocated from holdings | User allocation |
| `real_exposure[pair]` | Real coin value on exchange (from REST) | REST fetch |
| `_real_coin_qty[pair]` | Real coin quantity on exchange (from REST) | REST fetch |

**Fund flow on buy:**
```
bot_pair_alloc[pair] -= amount_usd   (or bot_balance -= amount_usd)
bot_exposure[pair]   += amount_usd
bot_bought_qty[pair] += qty_bought
```

**Fund flow on sell/close:**
```
proceeds = filled_qty × filled_price
bot_pair_alloc[pair] += proceeds     ← ALWAYS pair-specific, never shared pool
bot_coin_qty[pair]    = 0
bot_bought_qty[pair]  = 0
bot_exposure[pair]    = 0
```

**Rule:** Sell proceeds always return to `bot_pair_alloc[pair]`. They NEVER go to `bot_balance`. This prevents XCN sale proceeds from buying BTC.

---

## 10. Dirty Flags & Queues

All cross-thread communication from asyncio → main thread:

| Name | Type | Set by | Consumed by |
|------|------|--------|-------------|
| `_pair_cards_dirty` | `bool` | WS ticker, _process_candles | `_ui_tick` |
| `_topbar_dirty` | `set` | WS ticker, _process_candles | `_ui_tick` |
| `_chart_dirty` | `bool` | `_mark_chart_dirty` (root.after) | `_chart_live_tick` |
| `_log_queue` | `deque` | `log_message()` | `_ui_tick` |
| `_mon_queue` | `deque` | `log_message("monitor")` | `_ui_tick` |
| `_act_queue` | `deque` | `log_message("trade"/"info")` | `_ui_tick` |
| `_ob_update_pending` | `bool` | WS level2 handler | `_update_ob_display` |
| `_modal_open` | `int` (counter) | `_popup._show_and_grab` | `_ui_tick`, `_chart_live_tick` |

---

## 11. Persistence

| What | File | When written |
|------|------|-------------|
| API credentials | `config.json` | On successful login |
| User settings (SL/TP/mode/signal_tf) | `config.json` | On settings save |
| Bot state (balances, allocations, qtys) | `config.json` | `_save_bot_state()` — on every allocation change or trade |
| Trade history | `trade_history.json` | On every open/close |
| Candle cache | `candle_cache.json` | After each full candle cycle (every 5min) |

`_settings_cache` is the in-memory dict — always write through to disk on changes, never let in-memory drift from disk.

---

## 12. Modal Dialog System

All popups go through `_popup(title, w, h)` (main.py:3473):

```
_popup():
  1. Create CTkToplevel, withdraw() it (hidden)
  2. Centre over parent window
  3. win.after(10, _show_and_grab):
       → self._modal_open += 1
       → deiconify()        ← show window first
       → grab_set()         ← THEN grab (Linux requires window visible first)
  4. win.bind("<Destroy>", _on_destroy):
       → self._modal_open -= 1  (clamped to 0)
  5. Return win to caller who builds all widgets synchronously

On confirm/close:
  1. win.destroy()                     ← triggers _on_destroy → _modal_open = 0
  2. root.after(0, _update_metrics)    ← runs AFTER destroy, into clean window
```

**Why destroy-first matters:** Calling `_update_metrics()` before `win.destroy()` means the labels are updated while the popup still has screen focus. The OS repaint on popup-close can overdraw the changes. Scheduling via `after(0, ...)` ensures the update paints into the fully-restored main window.

---

## 13. Fee Gate Logic

Before placing any BUY order:

```python
_round_trip_fee = 2.0 × FEE_PCT × amount_usd   # buy fee + sell fee
_tp_profit_usd  = tp_pct × amount_usd           # ATR-based take-profit

if _tp_profit_usd <= _round_trip_fee:
    skip trade  # ATR too small — fees eat the profit
```

With `FEE_PCT = 0.006` (0.6% maker), the minimum required TP is 1.2% of position size. Since `tp_pct = 3.0 × ATR / price`, this means `ATR / price > 0.4%` — i.e., only trade when volatility is sufficient to clear fees.

---

## 14. Surge Detection

Real-time flash-move detector running on every WS ticker message:

```
_check_surge(pair)  (main.py:4848)
  1. history = self.price_history[pair]  (deque of last N prices)
  2. oldest = history[-SURGE_WINDOW]    (direct deque indexing — no list copy)
  3. newest = history[-1]
  4. move   = (newest - oldest) / oldest
  5. if abs(move) > SURGE_PCT (2.5%):
       fire buy or sell depending on direction
       subject to SURGE_COOLDOWN (90s per pair per direction)
       subject to capital gates (same as signal trades)
```

Surge bypasses the candle-close requirement — it fires intra-candle on large moves.

---

## 15. Known Issues & Roadmap

See audit report for full details. Top priorities in order:

### Critical (fix before next release)
1. `_bg_render` has no error recovery — if canvas.draw() fails, `_chart_rendering` locks True forever
2. JWT renewal (every 90s) does not re-subscribe to WS channels — after 120s token expires, data stops silently
3. `bot_pair_alloc` scaling on balance re-fetch loses precision — allocations shrink on each fetch
4. Confirmation TF candles not validated before signal check — empty conf_tf list causes silent miss
5. `_wait_for_fill` doesn't distinguish transient vs permanent API errors

### High
6. Indicator calculations only run for signal_tf + 1h — 1m/1d charts show no RSI/ADX
7. `_forming_candle` read path in `_refresh_chart` should acquire the lock
8. Candle cache staleness not checked — old cache + new data shows time gaps in chart
9. WS recv timeout does not force reconnect on stale subscriptions (>60s without ticks)
10. `_trade_pl_tick` and `_monitor_loop` can both update trade P&L simultaneously — should pick one owner

### Medium
11. Log boxes have no hard max-lines cap guarantee across the queued flush
12. Settings not written to disk immediately on every change — crash between change and save loses settings
13. Resize debounce at 80ms — increase to 200ms
14. S/R `min_volume=1000` hardcoded — doesn't scale for low-volume pairs like XCN
15. Order book: bid insertion uses manual binary search, ask uses bisect — should be consistent

### Low / Polish
16. Chart axes font size should scale with DPI
17. Topbar price labels should use fixed-width font to prevent layout reflow
18. Loading progress bar (2px) too thin — increase to 4px
19. Spinner at 80ms intervals misaligns with 60Hz monitor
20. Candle ingest log verbosity is too high — 12+ log lines/min at steady state
