#!/usr/bin/env python3
"""
StockTwits retail-sentiment pull for the Alpaca paper-trading simulator.

Hourly keyless pull on the watchlist via the public StockTwits symbol stream:
    https://api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json

Counts Bullish vs Bearish sentiment tags on the last ~30 messages per ticker and
raises two context flags:
  - extreme_bullish_contrarian_caution: >80% of sentiment-tagged messages are
    bullish (on an extended runup this is a contrarian caution, not a buy).
  - sudden_bearish_flip: a holding's bearish share jumped >=30 percentage
    points since the prior hourly reading.

CONTEXT LAYER ONLY - this feed informs, it never triggers trades alone.
Pair with price action and the other signal feeds before any decision.

Rate budget: 5 tickers/hour, far under the ~200/hr unauthenticated limit.
Deps: stdlib + requests only.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.stocktwits.com/api/2/streams/symbol/{}.json"
TICKERS = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
HERE = os.path.dirname(os.path.abspath(__file__))
# Repo root on GitHub Actions (scripts/), goal dir locally (bin/).
GOAL_DIR = os.path.dirname(HERE)
FEED_DIR = os.path.join(GOAL_DIR, "hidden_files", "stocktwits")
LATEST = os.path.join(FEED_DIR, "stocktwits-latest.json")
HISTORY = os.path.join(FEED_DIR, "stocktwits-history.jsonl")

MIN_TAGGED = 5          # readings with fewer tagged messages are "low_sample"
EXTREME_BULL_PCT = 0.80  # >80% bullish -> contrarian caution flag
BEAR_FLIP_DELTA = 0.30   # bearish share up >=30pp vs prior reading -> flip flag


def fetch_stream(ticker):
    resp = requests.get(BASE.format(ticker),
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if resp.status_code == 429:
        raise RuntimeError("rate limited by StockTwits (HTTP 429)")
    resp.raise_for_status()
    data = resp.json()
    if data.get("response", {}).get("status") != 200:
        raise RuntimeError("unexpected response status: {}".format(
            data.get("response")))
    return data.get("messages", [])


def count_sentiment(messages):
    bull = bear = 0
    for m in messages:
        basic = (m.get("entities") or {}).get("sentiment")
        if not basic:
            continue
        tag = basic.get("basic")
        if tag == "Bullish":
            bull += 1
        elif tag == "Bearish":
            bear += 1
    return bull, bear


def prior_reading(ticker):
    """Most recent history row for this ticker, or None."""
    if not os.path.exists(HISTORY):
        return None
    last = None
    with open(HISTORY) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ticker") == ticker:
                last = row
    return last


def main():
    os.makedirs(FEED_DIR, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tickers_out = {}
    all_flags = []
    failures = []

    for ticker in TICKERS:
        entry = {"ticker": ticker, "as_of_utc": now,
                 "n_messages": 0, "n_bullish": 0, "n_bearish": 0,
                 "n_tagged": 0, "bull_pct": None, "bear_pct": None,
                 "low_sample": True, "flags": []}
        try:
            messages = fetch_stream(ticker)
        except Exception as exc:  # keep the other tickers alive
            failures.append({"ticker": ticker, "error": str(exc)})
            entry["error"] = str(exc)
            tickers_out[ticker] = entry
            continue

        bull, bear = count_sentiment(messages)
        tagged = bull + bear
        entry.update(n_messages=len(messages), n_bullish=bull,
                     n_bearish=bear, n_tagged=tagged)

        if tagged >= MIN_TAGGED:
            bull_pct = bull / tagged
            bear_pct = bear / tagged
            entry.update(bull_pct=round(bull_pct, 3),
                         bear_pct=round(bear_pct, 3), low_sample=False)

            if bull_pct >= EXTREME_BULL_PCT:
                flag = "extreme_bullish_contrarian_caution"
                entry["flags"].append(flag)
                all_flags.append(
                    f"{ticker}: EXTREME BULLISH ({bull_pct:.0%} of {tagged} "
                    "tagged messages) - contrarian caution, do not chase.")

            prior = prior_reading(ticker)
            if prior and prior.get("bear_pct") is not None:
                delta = bear_pct - prior["bear_pct"]
                if delta >= BEAR_FLIP_DELTA:
                    flag = "sudden_bearish_flip"
                    entry["flags"].append(flag)
                    all_flags.append(
                        f"{ticker}: SUDDEN BEARISH FLIP (bearish share "
                        f"{prior['bear_pct']:.0%} -> {bear_pct:.0%} since "
                        f"{prior.get('as_of_utc')}) - review any holding.")
        tickers_out[ticker] = entry
        # be gentle between pulls even though we are nowhere near the limit
        time.sleep(2)

    latest = {
        "as_of_utc": now,
        "feed": "stocktwits_symbol_stream",
        "tickers": tickers_out,
        "flags": all_flags,
        "failures": failures,
        "role": "Context layer - informs, never triggers trades alone.",
    }
    with open(LATEST, "w") as fh:
        json.dump(latest, fh, indent=2)

    with open(HISTORY, "a") as fh:
        for ticker in TICKERS:
            e = tickers_out[ticker]
            if "error" in e:
                continue
            fh.write(json.dumps({
                "as_of_utc": now, "ticker": ticker,
                "n_messages": e["n_messages"], "n_bullish": e["n_bullish"],
                "n_bearish": e["n_bearish"], "n_tagged": e["n_tagged"],
                "bull_pct": e["bull_pct"], "bear_pct": e["bear_pct"],
                "low_sample": e["low_sample"],
            }) + "\n")

    for line in [f"{t}: bull {e['n_bullish']} / bear {e['n_bearish']} "
                 f"(tagged {e['n_tagged']}) flags={e['flags']}"
                 for t, e in tickers_out.items()]:
        print(line)
    for f in all_flags:
        print("FLAG:", f)
    for f in failures:
        print(f"FAILED {f['ticker']}: {f['error']}", file=sys.stderr)

    if failures and len(failures) == len(TICKERS):
        print("All ticker pulls failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
