# paper-trading-feeds-hourly

Hourly public market-data feeds for a paper-trading research project. Companion to the private [paper-trading-feeds](https://github.com/dorkagent/paper-trading-feeds) repo (daily/weekly feeds).

## Feeds

| Script | What it collects | Cadence |
|---|---|---|
| `scripts/crypto-derivs.py` | Crypto derivatives positioning: funding rates, open interest, mark prices (Hyperliquid, Binance, OKX public APIs) | Hourly |
| `scripts/stocktwits-sentiment.py` | Retail sentiment per watchlist ticker (StockTwits public symbol streams) | Hourly |

## How it works

Each feed runs on a GitHub Actions schedule, writes `hidden_files/<feed>/<feed>-latest.json` (+ a `.jsonl` history line), and commits the result. No API keys, no secrets, no personal data — everything here is public market data.

Outputs are context only for the research system that consumes them — descriptive, not trade signals.
