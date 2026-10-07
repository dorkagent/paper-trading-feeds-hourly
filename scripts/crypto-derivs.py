#!/usr/bin/env python3
"""Crypto-derivatives positioning feed (keyless).

Combines:
  1. Hyperliquid public API (live): metaAndAssetCtxs (funding/hr + OI + mark px),
     fundingHistory per coin (trailing ~48h funding trend).
  2. Binance (Futures) via CoinGecko keyless /derivatives: funding_rate + OI + 24h vol.
     (Binance's own fapi hosts are HTTP-451 geo-blocked from this network; Bybit is
     country-blocked too. CoinGecko is the reachable $0 substitute for Binance data.)
  3. OKX public API funding-rate as a third corroborating venue.

Outputs:
  hidden_files/crypto-derivs/crypto-derivs-latest.json   (full snapshot)
  hidden_files/crypto-derivs/crypto-derivs-history.jsonl (one compact line per run)

Context layer only: "informs, never triggers trades alone."

Stdlib + requests only.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

COINS = ("BTC", "ETH", "SOL")
HL_COIN = {c: c for c in COINS}
CG_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
OKX_INST = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}

HERE = os.path.dirname(os.path.abspath(__file__))
# Repo root on GitHub Actions (scripts/), goal dir locally (bin/) — both
# resolve to the same hidden_files/<feed> layout.
OUT_DIR = os.path.join(os.path.dirname(HERE), "hidden_files", "crypto-derivs")
LATEST = os.path.join(OUT_DIR, "crypto-derivs-latest.json")
HISTORY = os.path.join(OUT_DIR, "crypto-derivs-history.jsonl")

UA = {"User-Agent": "crypto-derivs-feed/1.0 (paper-trading research)"}

# Thresholds are in *percent per 8h* funding-period terms (Binance/OKX convention).
EXTREME_PP = 0.10   # |funding| > 0.10%/8h -> crowded positioning
ELEVATED_PP = 0.05  # |funding| > 0.05%/8h -> watch
DIVERGE_PP = 0.05   # max-min venue spread > 0.05pp -> venue-specific squeeze risk


def get(url, timeout=25, retries=3):
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (451, 403):
                break  # geo/block, retry won't help
        except Exception as e:  # noqa: BLE001 - network flake
            last = repr(e)
        time.sleep(2)
    raise RuntimeError(f"GET {url} failed: {last}")


def post(url, payload, timeout=25, retries=4):
    last = None
    for _ in range(retries):
        try:
            r = requests.post(url, json=payload, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(3)
    raise RuntimeError(f"POST {url} {payload} failed: {last}")


def hyperliquid_meta():
    """Returns {coin: {funding_per_hr, open_interest_coins, oracle_px}}."""
    data = post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"}).json()
    universe, ctxs = data[0]["universe"], data[1]
    out = {}
    for u, c in zip(universe, ctxs):
        name = u["name"]
        if name in COINS:
            out[name] = {
                "funding_per_hr": float(c.get("funding") or 0),
                "open_interest_coins": float(c.get("openInterest") or 0),
                "oracle_px_usd": float(c.get("oraclePx") or 0),
            }
    return out


def hyperliquid_funding_trend(coin, hours=48):
    """Trailing hourly funding samples -> avg, latest, trend slope sign."""
    now_ms = int(time.time() * 1000)
    rows = post(
        "https://api.hyperliquid.xyz/info",
        {"type": "fundingHistory", "coin": coin,
         "startTime": now_ms - hours * 3600_000, "endTime": now_ms},
    ).json()
    rates = [float(r["fundingRate"]) for r in rows if "fundingRate" in r]
    if not rates:
        return {}
    half = max(1, len(rates) // 2)
    return {
        "samples": len(rates),
        "avg_per_hr": sum(rates) / len(rates),
        "latest_per_hr": rates[-1],
        "first_half_avg_per_hr": sum(rates[:half]) / half,
        "second_half_avg_per_hr": sum(rates[half:]) / max(1, len(rates) - half),
    }


def coingecko_binance():
    """Binance (Futures) funding_rate (% per 8h), OI (USD), 24h volume (USD)."""
    rows = get("https://api.coingecko.com/api/v3/derivatives?include_tickers=all",
               timeout=40).json()
    out = {}
    for r in rows:
        if r.get("market") != "Binance (Futures)":
            continue
        sym = r.get("symbol")
        for coin, want in CG_SYMBOL.items():
            if sym == want:
                out[coin] = {
                    "funding_8h_pct": r.get("funding_rate"),
                    "open_interest_usd": r.get("open_interest"),
                    "volume_24h_usd": r.get("volume_24h"),
                    "last_traded_at": r.get("last_traded_at"),
                }
    return out


def okx_funding(coin):
    """OKX perpetual funding rate (decimal per 8h period)."""
    d = get(f"https://www.okx.com/api/v5/public/funding-rate?instId={OKX_INST[coin]}").json()
    rows = d.get("data") or []
    if not rows:
        return {}
    r = rows[0]
    return {
        "funding_8h_pct": float(r["fundingRate"]) * 100,
        "funding_time": r.get("fundingTime"),
    }


def per8h_pct_hl(funding_per_hr):
    return funding_per_hr * 8 * 100


def build_flags(coin, venues):
    flags = []
    fundings = {v: d.get("funding_8h_pct") for v, d in venues.items()
                if isinstance(d.get("funding_8h_pct"), (int, float))}
    if not fundings:
        return ["NO_FUNDING_DATA"]
    mx, mn = max(fundings.values()), min(fundings.values())
    if mx > EXTREME_PP:
        flags.append("CROWDED_LONGS: extreme positive funding (>0.10%/8h) "
                     "- contrarian caution / consider trimming longs")
    elif mx > ELEVATED_PP:
        flags.append("ELEVATED_POSITIVE_FUNDING: longs paying up (>0.05%/8h) - watch for crowding")
    if mn < -EXTREME_PP:
        flags.append("CROWDED_SHORTS: extreme negative funding (<-0.10%/8h) "
                     "- contrarian caution on shorts")
    elif mn < -ELEVATED_PP:
        flags.append("ELEVATED_NEGATIVE_FUNDING: shorts paying up (<-0.05%/8h) - watch for crowding")
    spread = mx - mn
    if spread > DIVERGE_PP:
        out_v = max(fundings, key=fundings.get)
        flags.append(f"VENUE_DIVERGENCE: funding spread {spread:.3f}pp across venues "
                     f"(highest: {out_v} {mx:.4f}%/8h) - venue-specific squeeze risk")
    return flags or ["NEUTRAL"]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    errors = []
    snapshot = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Context layer - informs, never triggers trades alone.",
        "sources": {
            "hyperliquid": "live (metaAndAssetCtxs + fundingHistory)",
            "binance": "via CoinGecko keyless /derivatives (fapi.binance.com is HTTP-451 "
                       "geo-blocked from this network; /futures/data/* ratios dropped)",
            "okx": "live public funding-rate (corroborating venue)",
        },
        "errors": errors,
        "coins": {},
    }

    try:
        hl = hyperliquid_meta()
    except Exception as e:  # noqa: BLE001
        errors.append(f"hyperliquid meta: {e}")
        hl = {}
    try:
        cg = coingecko_binance()
    except Exception as e:  # noqa: BLE001
        errors.append(f"coingecko/binance: {e}")
        cg = {}

    for coin in COINS:
        venues = {}
        h = hl.get(coin, {})
        if h:
            venues["hyperliquid"] = {
                "funding_8h_pct": per8h_pct_hl(h["funding_per_hr"]),
                "open_interest_coins": h["open_interest_coins"],
                "mark_px_usd": h["oracle_px_usd"],
            }
            try:
                tr = hyperliquid_funding_trend(coin)
                if tr:
                    venues["hyperliquid"]["funding_trend_48h"] = {
                        "avg_8h_pct": tr["avg_per_hr"] * 8 * 100,
                        "rising": tr["second_half_avg_per_hr"] > tr["first_half_avg_per_hr"],
                    }
                    avg = tr["avg_per_hr"] * 8 * 100
                    lat = tr["latest_per_hr"] * 8 * 100
                    if avg > 0.005 and lat > 3 * avg and lat > 0.02:
                        venues["hyperliquid"]["funding_spike"] = True
            except Exception as e:  # noqa: BLE001
                errors.append(f"hyperliquid trend {coin}: {e}")
        b = cg.get(coin)
        if b and b.get("funding_8h_pct") is not None:
            venues["binance_via_coingecko"] = b
        try:
            o = okx_funding(coin)
            if o:
                venues["okx"] = o
        except Exception as e:  # noqa: BLE001
            errors.append(f"okx {coin}: {e}")

        snapshot["coins"][coin] = {
            "venues": venues,
            "flags": build_flags(coin, venues),
        }

    with open(LATEST, "w") as f:
        json.dump(snapshot, f, indent=2)

    hist_line = {
        "t": snapshot["fetched_at_utc"],
        "coins": {
            c: {
                "f": {v: round(d.get("funding_8h_pct"), 5)
                      for v, d in cd["venues"].items()
                      if isinstance(d.get("funding_8h_pct"), (int, float))},
                "flags": cd["flags"],
            }
            for c, cd in snapshot["coins"].items()
        },
        "errors": errors,
    }
    with open(HISTORY, "a") as f:
        f.write(json.dumps(hist_line) + "\n")

    extreme = any(
        any(x.startswith(("CROWDED", "VENUE_DIVERGENCE")) for x in cd["flags"])
        for cd in snapshot["coins"].values()
    )
    for c, cd in snapshot["coins"].items():
        fl = "; ".join(cd["flags"])
        print(f"{c}: {fl}")
    if errors:
        print("ERRORS: " + " | ".join(errors), file=sys.stderr)
    if not any(snapshot["coins"][c]["venues"] for c in COINS):
        print("ALL LEGS FAILED", file=sys.stderr)
        return 2
    return 0 if not extreme else 0


if __name__ == "__main__":
    sys.exit(main())
