# NEXXUS Crypto Bot

**Automated cryptocurrency trading terminal built on Coinbase Advanced Trade.**

> Beta release — use paper / small amounts until you've validated the strategy for your risk tolerance.

---

## Features

- **Live trading** via Coinbase Advanced Trade REST + WebSocket APIs
- **MA Crossover strategy** — configurable moving average periods (default `2, 5, 14` on `1h`)
- **Breakout / surge detection** — real-time WebSocket tick monitoring (20-tick window, 2.5% threshold)
- **Heikin Ashi charts** — embedded matplotlib charts with zoom, pan, and hover tooltip
- **Multi-timeframe signals** — 1m / 5m / 1h / 1d; signal TF is user-selectable
- **Per-pair swap-on-sell** — proceeds optionally auto-swapped to USDC (or held as USD)
- **Stop-loss / take-profit / trailing stop** — ATR-aware dynamic exit management
- **Disk candle cache** — charts render instantly on second launch from cached data
- **Settings persistence** — signal TF, MA periods, swap targets, and risk params survive restarts
- **Trade journal** — all fills logged to `trades.json`
- **Emergency stop** — single-click halt with graceful position management

---

## Requirements

- Python 3.9+
- Coinbase Advanced Trade account with a **CDP API Key** (ECDSA / ES256)
  - Create at: https://portal.cdp.coinbase.com

---

## Installation

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/nexxus-crypto-bot.git
cd nexxus-crypto-bot

# Install dependencies
pip install customtkinter pillow ccxt matplotlib websockets numpy pytz \
            coinbase-advanced-py

# Or use the installer script (also creates a desktop shortcut)
bash install.sh
```

---

## Running

```bash
python3 main.py
```

Or via the launcher (uses the NEXXUS venv if you ran `install.sh`):

```bash
bash run.sh
```

On first launch you will be prompted for your Coinbase API Key and EC Private Key.
Credentials are saved to `config.json` and auto-filled on subsequent launches.

---

## Configuration

All settings are adjustable from the **Settings** tab inside the app and persisted to `config.json` automatically.

| Setting | Default | Description |
|---|---|---|
| Signal Timeframe | `1h` | Candle TF on which MA crossovers fire orders |
| MA Periods | `2, 5, 14` | Up to 3 comma-separated periods; two shortest used for crossover |
| Swap on Sell | `USDC` | Asset to receive after a sell (per pair) |
| Stop Loss | `2%` | Hard stop below entry |
| Take Profit | `5%` | Hard target above entry |
| Trailing Stop | `4%` | Activates once in profit; trails 4% below peak |
| Order Amount | `$100` | USD size per market order |
| Minimum Reserve | `$50` | Bot won't trade below this balance floor |
| Cooldown | `300s` | Minimum seconds between trades on the same pair |

---

## Trading Strategy

### MA Crossover
- **Buy**: MA_fast crosses above MA_slow on signal TF **and** MA_fast > MA_slow on confirmation TF
- **Sell**: MA_fast crosses below MA_slow on signal TF **and** MA_fast < MA_slow on confirmation TF
- Default periods `[2, 5, 14]` → fast = 2, slow = 5; MA 14 is displayed on chart for trend context

### Breakout / Surge
- Fires when price moves **≥ 2.5%** within the last 20 WebSocket ticks
- Volume surge (≥ 2× 20-candle average) or single-candle momentum ≥ 2% gate
- 90-second cooldown; direction-reversal guard prevents chasing dead moves

---

## File Structure

```
├── main.py          # UI, chart rendering, order placement, WebSocket client
├── engine.py        # Strategy logic, indicators, candle cache, persistence helpers
├── config.json      # API credentials + persisted user settings (auto-managed)
├── trades.json      # Trade history log
├── candle_cache.json# Startup candle cache (auto-managed, max 24h old)
├── bot.log          # Operational log
├── crash.log        # Crash output from run.sh
├── icon.png         # App icon
├── install.sh       # Dependency installer + desktop shortcut creator
└── run.sh           # Launcher script (uses NEXXUS venv)
```

---

## Security Notes

- **Never commit** `config.json` to a public repository — it contains your API private key.
- The `.gitignore` excludes `config.json`, `trades.json`, `candle_cache.json`, and `bot.log`.
- API keys should have **Trading** permission only — do not grant withdrawal access.
- Webhook server (`port 8000`) only accepts connections from `localhost`.

---

## Disclaimer

This software is provided for educational and research purposes. Cryptocurrency trading carries significant financial risk. The authors are not responsible for any losses incurred from using this bot. Always test with small amounts first.

---

## License

MIT
