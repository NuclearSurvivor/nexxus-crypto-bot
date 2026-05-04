# NEXXUS Crypto Bot

**Automated cryptocurrency trading terminal built on Coinbase Advanced Trade.**

> Use responsibly — test with small amounts until you've validated the strategy for your risk tolerance.

---

## Version History

### v1.0.4b *(current)*

#### Live Charts
- **Forming candle** — chart now shows the currently-forming candle in real-time. On every WebSocket price tick, the open/high/low/close of the current period is updated and drawn (Heikin-Ashi transformed) so the last candle body moves with the live price instead of being static until candle close.
- **1-second chart refresh** — while the Charts page is active the chart redraws every second, keeping the price line, price label, and forming candle continuously current.
- **Price label locked to live price** — the right-axis price label (colored tag) is now drawn AFTER `set_ylim`, so it always reflects the exact current price. When zoomed in, the label is clamped to the visible y-range so it never disappears off-screen. Color is green/red based on whether price is above/below the last candle close.
- **Order Book popup** — "Order Book" button in the chart header opens a live popup showing top 10 bids and asks with price, size, and cumulative columns. Data comes from the Coinbase `level2` WebSocket channel, updating up to 4× per second. Shows best bid/ask, spread, and a bid/ask volume imbalance indicator.
- **level2 WebSocket subscription** — the existing WebSocket connection now also subscribes to the `level2` channel, maintaining a live order book (`_order_book`) for all trading pairs. Bid/ask tables are capped at 50 levels each side.

---

### v1.0.3b

#### Bug Fixes
- **Bot balance display** — bot capital shown in topbar/key panel now cannot exceed total portfolio value. Proportional scaling clamp in `_fetch_balance` prevents `bot_balance + pair_alloc` from drifting above actual exchange USD. Display is further capped to portfolio with `min()` in `_update_metrics`.
- **Last signal on all pairs** — key panel "LAST SIGNAL" block now reads from current chart render (`_signal_data`) first, so BTC/ETH and all non-fill pairs show their most recent MA crossover arrow instead of "No signals yet".
- **Log-based signal recovery** — on startup with an empty `trades.json`, bot scans `bot.log` for the most recent `◆ FILLED` line and seeds `last_executed_signal` from it, so XCN (and any pair) shows the correct last fill after a restart.
- **Fill marker hover tooltip** — ▼/▲ marker drawn from fill history now correctly registers in the hover hit system. Fixed by: (a) appending to `_signal_data` so the hover scanner can find it, (b) keeping the timestamp UTC-aware throughout (naive datetimes shifted the hit-box by the local UTC offset), (c) using ATR-based `signal_offset` instead of `ax.get_ylim()` (which returns stale limits before matplotlib auto-scales) so the drawn marker position matches the hover proximity check.
- **Window resize artifacts** — black rectangle outlines appearing on maximize/restore fixed by replacing `CTkFrame(height=1/2, corner_radius=0)` separator strips inside rounded-corner cards with native `tk.Frame` strips (no canvas, no stale background). Added debounced `<Configure>` → `update_idletasks()` as safety net.
- **Per-direction MA cooldowns** — buy and sell signals now track independent cooldown timers so a recent buy no longer blocks an urgent sell (and vice versa). A 10-second global minimum gap prevents double-firing on the same candle.
- **config.json permissions** — file is `chmod 600` after every credential/settings save so the private key is not world-readable.

---

### v1.0.2b *(previous)*

#### Order Execution
- **Progressive limit order offsets** — attempt 1 places 1 tick outside the spread (guaranteed maker, 0% fee), attempt 2 at raw bid/ask, then market fallback. On zero-spread pairs like XCN-USD (bid=ask=$0.00514), the 1-tick cost (~0.19%) beats the taker fee (0.60%).
- **Fast execution for signals** — signal-triggered trades use 2 attempts × 25s timeout (~50s max), vs 3 × 90s for manual orders. Near-instant entry on candle close.
- **Zombie trade auto-removal** — trades that cannot be closed (e.g. "Insufficient balance") are auto-removed and flagged `ABANDONED` in logs. Previously caused infinite retry loops.
- **Quote/base precision** — `quote_increment` and `base_increment` fetched from product info at startup to ensure correct decimal formatting on all pairs.

#### Interface
- **Monitor tab** — balance sync, WebSocket heartbeat, position monitor ticks, and candle data are routed to a separate Monitor tab within the Logs page. Main Logs tab only shows trading-relevant events (signals, fills, errors, warnings).
- **Save Logs button** — copies `bot.log` to a user-selected path from the Logs tab.
- **Signal hover seconds** — signal tooltip timestamps now include seconds (`2026-05-01 14:32:07 UTC`).
- **Settings mouse wheel scroll** — Settings tab now scrolls with the mouse wheel (all child widgets bound recursively).
- **Allocate popup redesign** — USD Budget mode pair buttons no longer show coin price (irrelevant for USD allocation). Info line shows USD / USDC / USDT / Total liquid / Bot wallet breakdown.
- **Allocate All Available button** — single click to fill the amount field with your full liquid USD (or full coin holdings in Coin Holdings mode).

#### Auto-Compound (new in settings)
- **Auto-Compound toggle** — when enabled, order size scales as `available_funds × pct%` (configurable), up to a user-defined cap. Profits automatically increase trade sizes until the cap is reached.
- **Position % and Cap** — e.g. 10% of $300 available = $30 order; cap at $500 means order never exceeds $500 even if the wallet grows to $5,000.

#### Performance
- **Monitor refresh rate detection** — `xrandr` is queried at startup to set `_FRAME_DT` to the actual display refresh rate (e.g. 144 Hz → 6.94ms throttle). Falls back to 60 Hz if unavailable.

---

### v1.0.1b
- **Signal Direction control** — `Both / Buy Only / Sell Only` toggle in Settings
- **Bot Status Mode row** — shows capital availability state live
- **Next Window countdown** — 1-second countdown to next candle close, turns orange in final 60s
- **Price Feed status** — `LIVE 0.3s ago` or `STALE (XCN)` with per-pair staleness
- **Settings persistence** — all params survive restarts via `config.json`
- **P&L display fix** — `$0.00` shows neutral; sign only when non-zero
- **Coin Holdings allocation fix** — `real_exposure` not modified during user allocation
- **Sell proceeds routing** — proportional split of proceeds between `bot_balance` and `bot_pair_alloc`
- **Signal gate fix** — `bot_coin_qty` recognized as valid sell capital
- **Surge cooldowns** — buy/sell tracked independently; reversal guard checks last 5 ticks
- **MA period fix** — saved periods now update both `engine.MA_PERIODS` and local global
- **Topbar allocation** — bot capital shown in topbar alongside portfolio and P&L

### v1.0.0b
- Initial public beta
- MA crossover strategy (`2, 5, 14` default on `1h`)
- Real-time surge/breakout detection via WebSocket
- Heikin Ashi charts with zoom, pan, hover — native Hz throttle
- Stop-loss / take-profit / trailing stop
- Per-pair swap-on-sell
- Disk candle cache for instant startup
- Trade journal, emergency stop, live balance tracking

---

## Features

- **Live trading** via Coinbase Advanced Trade REST + WebSocket APIs
- **MA Crossover strategy** — configurable periods (default `2, 5, 14` on `1h`)
- **Breakout / surge detection** — real-time WebSocket tick monitoring (20-tick window, 2.5% threshold)
- **Signal Direction** — `Both`, `Buy Only`, or `Sell Only`
- **Progressive limit orders** — guaranteed maker pricing on zero-spread pairs; 0% fee path
- **Heikin Ashi charts** — embedded matplotlib with zoom, pan, hover tooltip; frame-rate matched to display
- **Multi-timeframe** — 1m / 5m / 1h / 1d; signal TF user-selectable
- **Auto-Compound** — scale order size as % of available capital, up to a configurable cap
- **Coin Holdings allocation** — hand existing coin balances directly to the bot
- **Per-pair swap-on-sell** — proceeds optionally auto-swapped to USDC or another coin
- **Stop-loss / take-profit / trailing stop** — ATR-aware dynamic exits
- **Disk candle cache** — charts render instantly on second launch
- **Monitor / Logs split** — trading log and system monitor in separate tabs
- **Settings persistence** — all settings survive restarts
- **Trade journal** — all fills logged to `trades.json`
- **Emergency stop** — single-click halt

---

## Requirements

- Python 3.9+
- Coinbase Advanced Trade account with a **CDP API Key** (ECDSA / ES256)
  - Create at: https://portal.cdp.coinbase.com

---

## Installation

```bash
git clone https://github.com/NuclearSurvivor/nexxus-crypto-bot.git
cd nexxus-crypto-bot

pip install customtkinter pillow matplotlib websockets numpy pytz coinbase-advanced-py

python3 main.py
```

Or use the installer (also creates a desktop shortcut):

```bash
bash install.sh
```

---

## Configuration

All settings are adjustable from the **Settings** tab and persisted to `config.json` automatically.

| Setting | Default | Description |
|---|---|---|
| Signal Timeframe | `1h` | Candle TF on which MA crossovers fire orders |
| Signal Direction | `Both` | `Both` / `Buy Only` / `Sell Only` |
| MA Periods | `2, 5, 14` | Up to 3 comma-separated periods; two shortest used for crossover |
| Swap on Sell | `USDC` | Asset to receive after a sell (per pair; `USDC`/`USD` = hold) |
| Stop Loss | `2%` | Hard stop below entry |
| Take Profit | `5%` | Hard target above entry |
| Trailing Stop | `4%` | Activates once in profit; trails 4% below peak |
| Order Amount | `$225` | Base USD size per order (overridden when auto-compound is on) |
| Minimum Reserve | `$25` | Bot won't trade below this balance floor |
| Cooldown | `300s` | Minimum seconds between trades on the same pair |
| Auto-Compound | `Off` | Scale order size as % of available funds up to a cap |
| Position % | `10%` | % of available funds per trade (auto-compound mode) |
| Order Cap | `$500` | Maximum order size in auto-compound mode |

---

## Trading Strategy

### MA Crossover
- **Buy**: MA_fast crosses above MA_slow on signal TF AND confirmed on lower TF
- **Sell**: MA_fast crosses below MA_slow on signal TF AND confirmed on lower TF
- Default `[2, 5, 14]` → fast = MA2, slow = MA5; MA14 shown for trend context

### Breakout / Surge
- Fires when price moves **≥ 2.5%** within the last 20 WebSocket ticks
- Volume surge (≥ 2× 20-candle average) OR single-candle momentum ≥ 2% gate
- Independent buy/sell cooldowns (90s each); reversal guard checks last 5 ticks

### Order Execution
- **Attempt 1**: limit at ask+1tick (SELL) or bid-1tick (BUY) — guaranteed maker, 0% fee
- **Attempt 2**: limit at raw ask/bid — maker on normal-spread markets
- **Fallback**: market order — guaranteed fill, taker fee applies (~0.60%)

---

## Allocating Funds

**USD Budget** — ring-fences a USD amount from your Coinbase balance for the bot to trade with. Shows USDC, USDT, and raw USD breakdown so you can see exactly what's liquid.

**Coin Holdings** — hands existing coin holdings to the bot for sell-signal execution. Proceeds return to the liquid bot pool and can be redeployed on buy signals.

**Allocate All Available** — fills the amount field with your full liquid balance (USD + USDC + USDT) for one-click full allocation.

---

## Auto-Compound

When enabled, the bot scales each order to `available_funds × position_pct%`, capped at the max order size. As the bot profits, order sizes grow proportionally — up to the cap.

**Example:** wallet = $300, position = 10%, cap = $500 → $30 order.  
After $200 profit: wallet = $500 → $50 order. Cap won't be hit until wallet exceeds $5,000.

To disable scaling, turn off the toggle and orders revert to the fixed `Order Amount` setting.

---

## File Structure

```
├── main.py            # UI, charts, order placement, WebSocket client
├── engine.py          # Strategy, indicators, candle cache, persistence
├── config.json        # API credentials + user settings (auto-managed)
├── trades.json        # Trade history
├── candle_cache.json  # Startup candle cache (auto-managed, 24h TTL)
├── bot.log            # Operational log
├── crash.log          # Crash output from run.sh
├── icon.png           # App icon
├── install.sh         # Dependency installer + desktop shortcut
└── run.sh             # Launcher (uses NEXXUS venv)
```

---

## Security

- **Never commit** `config.json` — it contains your API private key.
- `.gitignore` excludes `config.json`, `trades.json`, `candle_cache.json`, `bot.log`.
- API keys should have **Trading** permission only — no withdrawal access.
- Webhook server (port 8000) only accepts `localhost` connections.

---

## Disclaimer

Cryptocurrency trading carries significant financial risk. This software is provided for educational and research purposes. The authors are not responsible for any losses. Always test with small amounts first.

---

## License

MIT
