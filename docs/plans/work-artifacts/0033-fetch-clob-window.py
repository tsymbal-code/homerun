#!/usr/bin/env python3
"""
Plan 0033 / Task 2 — for each cancelled trader_order, fetch CLOB trades
within the [submitted_at - 1s, submitted_at + 6s] FAK window from the
public Polymarket data-api, filter to the matching asset_id (token_id),
and compute min_ask_in_window / volume_at_or_below(shadow_limit_price)
/ would_have_filled.

Reads the Task-1 CSV (--in).  Writes augmented CSV (--out).

Run on polyhome-1 (loopback works fine — public endpoints, no auth).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
PAGE_SIZE = 500


def http_get_json(url: str, attempts: int = 4, backoff: float = 1.0) -> Any:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": "homerun-plan-0033/1.0"})
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            last_exc = exc
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_exc}")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def lookup_condition_id(market_id: str, cache: dict[str, str | None]) -> str | None:
    """
    Singular `/markets/{id}` endpoint returns the market regardless of
    closed/active state.  The plural `/markets?id=...` form silently
    filters out closed markets, which would mask 19 of our 33 cases.
    """
    if market_id in cache:
        return cache[market_id]
    url = f"{GAMMA_BASE}/markets/{market_id}"
    try:
        payload = http_get_json(url)
    except RuntimeError as exc:
        print(f"  ! gamma lookup failed for {market_id}: {exc}", file=sys.stderr)
        cache[market_id] = None
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        cache[market_id] = None
        return None
    cond = payload.get("conditionId")
    cache[market_id] = cond
    return cond


def fetch_all_clob_trades(condition_id: str) -> list[dict[str, Any]]:
    """
    Public data-api `/trades` paginated.  `takerOnly=true&filterType=CASH&filterAmount=1`
    surfaces all taker-side cash trades (matches what we want — actual
    market orders that crossed an ask/bid).  Pagination via `offset`
    until a short page returns.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "market": condition_id,
            "takerOnly": "true",
            "filterType": "CASH",
            "filterAmount": "1",
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
        }
        url = f"{DATA_API_BASE}/trades?{urlencode(params)}"
        try:
            page = http_get_json(url)
        except RuntimeError as exc:
            print(f"  ! data-api page failed cond={condition_id[:14]} off={offset}: {exc}", file=sys.stderr)
            break
        if not page:
            break
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.10)
        if offset >= 5000:
            print(f"  ! data-api pagination cap hit for {condition_id[:14]} (offset={offset})", file=sys.stderr)
            break
    return out


def classify_one(
    row: dict[str, str],
    condition_id: str | None,
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    submitted_at = parse_ts(row["created_at"])
    window_start = submitted_at - timedelta(seconds=1)
    window_end = submitted_at + timedelta(seconds=6)
    window_start_ts = int(window_start.timestamp())
    window_end_ts = int(window_end.timestamp()) + 1  # inclusive of trailing second
    target_token = row["token_id"]
    shadow_limit = float(row["shadow_limit_price"])
    ctx_max_entry = float(row["ctx_max_entry_price"]) if row.get("ctx_max_entry_price") else None
    leg_signal = float(row["leg_signal_price"])
    requested_shares = float(row["leg_shares"])

    in_window: list[dict[str, Any]] = []
    in_window_60s_either_side: list[dict[str, Any]] = []
    for t in trades:
        ts = int(t.get("timestamp") or 0)
        if abs(ts - submitted_at.timestamp()) <= 60:
            in_window_60s_either_side.append(t)
        if not (window_start_ts <= ts <= window_end_ts):
            continue
        asset_id = str(t.get("asset") or "")
        if asset_id != target_token:
            continue
        in_window.append(t)

    # Public data-api with takerOnly=true returns trades where `side`
    # is the taker's side. A taker BUY of our token == someone crossed
    # the ask; that's the only thing relevant to whether OUR taker BUY
    # would have filled.
    taker_buys: list[tuple[float, float]] = []
    other_taker: list[tuple[str, float, float]] = []
    for t in in_window:
        side = str(t.get("side") or "").upper()
        try:
            price = float(t.get("price"))
            size = float(t.get("size"))
        except (TypeError, ValueError):
            continue
        if side == "BUY":
            taker_buys.append((price, size))
        else:
            other_taker.append((side, price, size))

    asks_below_shadow = [(p, s) for (p, s) in taker_buys if p <= shadow_limit]
    asks_below_ctx_max = (
        [(p, s) for (p, s) in taker_buys if ctx_max_entry is not None and p <= ctx_max_entry]
        if ctx_max_entry is not None else []
    )

    min_taker_buy_price = min((p for p, _ in taker_buys), default=None)
    vol_at_or_below_shadow = sum(s for _, s in asks_below_shadow)
    vol_at_or_below_ctx_max = sum(s for _, s in asks_below_ctx_max)
    vol_total_taker_buy = sum(s for _, s in taker_buys)

    would_fill_at_shadow = (
        min_taker_buy_price is not None
        and min_taker_buy_price <= shadow_limit
        and vol_at_or_below_shadow >= requested_shares
    )
    would_fill_at_ctx_max = (
        ctx_max_entry is not None
        and min_taker_buy_price is not None
        and min_taker_buy_price <= ctx_max_entry
        and vol_at_or_below_ctx_max >= requested_shares
    )
    has_partial_at_shadow = (
        min_taker_buy_price is not None
        and min_taker_buy_price <= shadow_limit
        and vol_at_or_below_shadow > 0
    )
    has_partial_at_ctx_max = (
        ctx_max_entry is not None
        and min_taker_buy_price is not None
        and min_taker_buy_price <= ctx_max_entry
        and vol_at_or_below_ctx_max > 0
    )

    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "market_id": row["market_id"],
        "condition_id": condition_id or "",
        "token_id": target_token,
        "leg_signal_price": leg_signal,
        "ctx_max_entry_price": ctx_max_entry if ctx_max_entry is not None else "",
        "shadow_limit_price": shadow_limit,
        "cfg_max_probability": row.get("cfg_max_probability", ""),
        "requested_shares": round(requested_shares, 4),
        "trades_60s_either_side": len(in_window_60s_either_side),
        "trades_in_window": len(in_window),
        "taker_buys_in_window": len(taker_buys),
        "min_taker_buy_price": min_taker_buy_price if min_taker_buy_price is not None else "",
        "vol_total_taker_buy": round(vol_total_taker_buy, 4),
        "vol_at_or_below_shadow": round(vol_at_or_below_shadow, 4),
        "vol_at_or_below_ctx_max": round(vol_at_or_below_ctx_max, 4),
        "would_fill_at_shadow": would_fill_at_shadow,
        "has_partial_at_shadow": has_partial_at_shadow,
        "would_fill_at_ctx_max": would_fill_at_ctx_max,
        "has_partial_at_ctx_max": has_partial_at_ctx_max,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", dest="out", required=True)
    args = p.parse_args()

    rows = list(csv.DictReader(open(args.inp)))
    print(f"Loaded {len(rows)} cancelled-order rows", file=sys.stderr)

    cond_cache: dict[str, str | None] = {}
    unique_market_ids = sorted({r["market_id"] for r in rows})
    print(f"Resolving condition_id for {len(unique_market_ids)} unique markets", file=sys.stderr)
    for i, mid in enumerate(unique_market_ids):
        cond = lookup_condition_id(mid, cond_cache)
        print(f"  [{i+1:02d}/{len(unique_market_ids)}] {mid} -> {cond}", file=sys.stderr)
        time.sleep(0.10)

    trades_cache: dict[str, list[dict[str, Any]]] = {}
    unique_conds = sorted({c for c in cond_cache.values() if c})
    print(f"Fetching CLOB trades for {len(unique_conds)} unique conditions", file=sys.stderr)
    for i, cond in enumerate(unique_conds):
        trades = fetch_all_clob_trades(cond)
        trades_cache[cond] = trades
        ts_min = min((t.get("timestamp", 0) for t in trades), default=0)
        ts_max = max((t.get("timestamp", 0) for t in trades), default=0)
        print(f"  [{i+1:02d}/{len(unique_conds)}] {cond[:14]}... -> {len(trades)} trades ts=[{ts_min},{ts_max}]", file=sys.stderr)
        time.sleep(0.20)

    out_rows = []
    for r in rows:
        cond = cond_cache.get(r["market_id"])
        trades = trades_cache.get(cond, []) if cond else []
        out_rows.append(classify_one(r, cond, trades))

    fieldnames = list(out_rows[0].keys())
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)
    print(f"Wrote {len(out_rows)} rows to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
