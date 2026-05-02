# NEXXUS Crypto Bot

**Automated cryptocurrency trading terminal built on Coinbase Advanced Trade.**

> Beta software — test with small amounts until you've validated the strategy for your risk tolerance.

---

## Version History

### v1.0.1-beta *(current)*
- **Signal Direction control** — Settings now has a `Both / Buy Only / Sell Only` toggle. Filter the bot to only act on one side of the market. Persists across restarts.
- **Bot Status — Mode row** — Shows current execution mode + capital availability (e.g. `Both (sell cap only)` when coins are allocated but no USD).
- **Next Window countdown** — Live 1-second countdown to the next candle close on the signal TF. Turns orange in the final 60 seconds.
- **Price Feed status** — Shows `LIVE 0.3s ago` or `STALE (XCN)` with per-pair freshness. Orders are blocked if feed is >15s stale.
- **Settings persistence** — Signal TF, direction, MA periods, swap targets, and all risk params now survive restarts via `config.json`.
- **P&L display fix** — `$0.00` P&L shows as neutral grey instead of red/green. Sign only shown when non-zero.
- **Coin Holdings allocation fix** — Allocating coins to bot no longer causes false negative P&L. `real_exposure` is no longer touched during allocation.
- **Sell proceeds routing fix** — Proceeds from selling user-handed coins go to `bot_balance` (liquid pool), not `bot_pair_alloc`. Correct proportional split when both coin sources are mixed.
- **Signal gate fix** — Bot now recognises `bot_coin_qty` as valid capital for sell signals. Separate buy/sell gate logic.
- **Surge cooldown** — Buy and sell surge cooldowns are now tracked independently. A bull surge no longer blocks a crash signal 60s later.
- **Surge reversal guard** — Requires majority of last 5 ticks to oppose the move (was a single contrary tick).
- **Real exposure zeroed on balance fetch** — Externally sold coins now clear from internal state on next balance poll.
- **Price staleness guard** — Orders rejected if live price is >15s old (WebSocket drop protection).
- **MA period fix** — Saved MA periods now correctly update the local module global on startup (was updating `engine.MA_PERIODS` only, leaving bot using stale default).
- **Signal markers** — All timeframe signals now render solid/opaque. No more dim "informational" markers.
- **Topbar allocation** — Bot allocation shown in top-right alongside Portfolio and P&L. Updates live with price.

### v1.0.0-beta
- Initial public beta release
- MA crossover strategy (`2, 5, 14` default on `1h`)
- Real-time surge/breakout detection via WebSocket
- Heikin Ashi charts with zoom, pan, hover — 144fps throttle
- Stop-loss / take-profit / trailing stop
- Per-pair USDC swap-on-sell
- Disk candle cache for instant startup
- Trade journal, emergency stop, live balance tracking

---

## Features

- **Live trading** via Coinbase Advanced Trade REST + WebSocket APIs
- **MA Crossover strategy** — configurable periods (default `2, 5, 14` on `1h`)
- **Breakout / surge detection** — real-time WebSocket tick monitoring (20-tick window, 2.5% threshold)
- **Signal Direction** — choose `Both`, `Buy Only`, or `Sell Only` per session
- **Heikin Ashi charts** — embedded matplotlib with zoom, pan, hover tooltip
- **Multi-timeframe** — 1m / 5m / 1h / 1d; signal TF user-selectable
- **Coin Holdings allocation** — hand existing coin balances directly to the bot
- **Per-pair swap-on-sell** — proceeds optionally auto-swapped to USDC or another coin
- **Stop-loss / take-profit / trailing stop** — ATR-aware dynamic exits
- **Disk candle cache** — charts render instantly on second launch
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
| Order Amount | `$100` | USD size per market order |
| Minimum Reserve | `$50` | Bot won't trade below this balance floor |
| Cooldown | `300s` | Minimum seconds between trades on the same pair |

---

## Trading Strategy

### MA Crossover
- **Buy**: MA_fast crosses above MA_slow on signal TF AND confirmed on lower TF
- **Sell**: MA_fast crosses below MA_slow on signal TF AND confirmed on lower TF
- Default `[2, 5, 14]` → fast = MA2, slow = MA5; MA14 shown on chart for trend context

### Breakout / Surge
- Fires when price moves **≥ 2.5%** within the last 20 WebSocket ticks
- Volume surge (≥ 2× 20-candle average) OR single-candle momentum ≥ 2% gate
- Independent buy/sell cooldowns (90s each); reversal guard checks last 5 ticks

---

## Allocating Funds

**USD Budget** — ring-fences a USD amount from your Coinbase balance for the bot to trade with.

**Coin Holdings** — hands existing coin holdings to the bot for sell-signal execution. Proceeds return to the liquid bot pool and can be redeployed on buy signals.

Bot Status card shows:
- **Mode** — `Both` / `Both (sell cap only)` / `Buy Only` / `Sell Only` / `No funds`
- **Next Window** — countdown to next candle close on signal TF
- **Price Feed** — live freshness; orders blocked if >15s stale

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
