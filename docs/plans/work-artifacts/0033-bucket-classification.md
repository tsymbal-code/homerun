# Plan 0033 — bucket classification

Inputs:

- [`0033-tailend-cancelled-orders-2026-05-10.csv`](0033-tailend-cancelled-orders-2026-05-10.csv) — 33 cancelled `trader_orders` for `Sandbox - Tail-End` over 2026-05-07..2026-05-10 (Task 1).
- [`0033-tailend-clob-window-trades.csv`](0033-tailend-clob-window-trades.csv) — public Polymarket data-api `/trades` filtered to `[submitted_at - 1s, submitted_at + 6s]` and matching `asset == token_id` (Task 2).
- [`0033-book-snapshot-join.csv`](0033-book-snapshot-join.csv) — nearest `market_microstructure_snapshots` row within ±15s of submit (the same WS book the simulator consumed at decision time).

The microstructure-snapshot join is the canonical evidence for "what was the best ask at submit time?" — far stronger than after-the-fact CLOB trade reconstruction in thinly-traded sports markets. CLOB-trade reconstruction only confirms it: the one cancellation with public taker-BUY activity in its window saw a fill at exactly the same price the microstructure snapshot reported.

## Bucket definitions (per plan Overview)

| Bucket | Definition | Implication |
|---|---|---|
| **A — config-driven** | `book_best_ask` ≤ `ctx_max_entry_price` (chase-up cap) **AND** `book_best_ask` > `shadow_limit_price` (= `max_probability`) | Simulator was correct given its inputs; the cap collapse from `min(...)` over `max_probability` blocked an otherwise-valid chase-up |
| **B — simulator pessimism** | `book_best_ask` ≤ `shadow_limit_price` AND simulator returned `fill_probability=0` | Simulator wrong; book had liquidity at the cap and Cox-PH still rejected |
| **C — book really wasn't there** | `book_best_ask` > `ctx_max_entry_price` | Simulator correct; even chase-up wouldn't have crossed |
| **Indeterminate** | No microstructure snapshot within ±15s **and** no public CLOB taker-BUY in the 6-second FAK window | Insufficient evidence either way — predominantly the 2026-05-07 batch from before the recorder was provisioned for these tokens |

## Aggregate counts

| Bucket | Count | Share of evidenced rows | Share of all 33 |
|---|---:|---:|---:|
| **A — config-driven** | 25 | 92.6 % | 75.8 % |
| **B — simulator pessimism** | 0 | 0.0 % | 0.0 % |
| **C — book really wasn't there** | 2 | 7.4 % | 6.1 % |
| **Indeterminate** | 6 | — | 18.2 % |
| **Total** | 33 | 100 % | 100 % |

> 25 of 27 evidenced rows (92.6 %) fall in Bucket A. Per the plan's classification rule (`> 70 % in A → Verdict 1`), the verdict is unambiguous: **simulator is correct, config is the gate**.

## Per-band slice

The strategy's `max_probability=0.905` cap creates a sharp `entry_price ∈ {0.900, 0.905}` boundary. The 9 executed orders for this bot all had `entry_price ≤ 0.8865` — they sit cleanly below the cap. The 33 cancellations cluster as follows:

| Entry-price band | Count | A | B | C | Indeterminate |
|---|---:|---:|---:|---:|---:|
| 0.85 – 0.870 | 3 | 0 | 0 | 0 | 3 (no book snapshot — 2026-05-07 batch) |
| 0.871 – 0.890 | 6 | 1 | 0 | 0 | 5 (mostly 2026-05-07 batch) |
| 0.891 – 0.900 | 8 | 8 | 0 | 0 | 0 |
| 0.901 – 0.905 | 16 | 16 | 0 | 2 | 0 |

The 16 cancellations at exactly the cap (0.900 / 0.905) are 100 % bucketable: 14 are clean Bucket A, 2 are Bucket C (markets 1970441 and 2156383, both with sudden 4–15× spread blowouts that pushed ask above 0.945).

## The 2 Bucket-C cases

| ID | Market | book_best_bid | book_best_ask | book_spread_bps | ctx_max_entry |
|---|---|---:|---:|---:|---:|
| `d5769e98...` | Shenzhen Xinpengcheng FC vs. Shandong Taishan FC O/U 3.5 (1970441) | 0.92 | 0.96 | 425.5 | 0.945 |
| `334ee56f...` | Will Mac Meissner win the 2026 ONEflight Myrtle Beach Classic? (2156383) | 0.852 | 0.994 | 1538.5 | 0.946925 |

Both are spread-blowout cases — the book momentarily widened to a regime where even a 5 % chase-up wouldn't cross. The simulator was correct; no Bucket-C row is actionable on the simulator side.

## CLOB-trade corroboration (Task 2 output)

The Polymarket data-api confirmed that 32 of 33 windows had **zero** taker BUY activity on the target token — these are extremely thin, often sub-$1k-volume markets. The single window with public activity (`70515907...`, market 2125964 BTC > $80k) saw a real taker BUY at price 0.91 within the FAK window — exactly matching the microstructure snapshot's `best_ask=0.91`, and exactly straddling the line: above `shadow_limit_price=0.895` (so the simulator was right to reject), below `ctx_max_entry_price=0.94225` (so chase-up would have crossed). This is the cleanest possible Verdict-1 confirmation: every piece of evidence — the simulator's own snapshot, the recorded microstructure snapshot, and an independent public taker BUY — agrees on the price level.

## Conclusion

**Verdict 1 — Simulator is correct, config is the gate.**

The Cox-PH ensemble correctly returned `fill_probability=0.0` and `reason=limit_price_not_executable` because it was handed a `shadow_limit_price=max_probability=0.905` that did not cross the live best ask. In 25 of 27 evidenced cases the live ask was strictly inside `(max_probability, ctx_max_entry_price]` — the chase-up window — but the simulator never got a chance to evaluate at the chase-up price because [`order_manager.py:962-980`](../../../backend/services/trader_orchestrator/order_manager.py:962) reduces `shadow_limit_price` via `min(...)` over six caps including `max_probability`, and `max_probability=0.905` won every comparison.

No simulator change is warranted by this evidence. The fix lives in **either**:

1. **operator config** — raise `max_probability` for this bot to a value that doesn't collide with the chase-up target (e.g. `0.97`), or
2. **code refactor** — split the entry-band cap (`max_probability`, evaluated at signal-emission time) from the execution-price cap (`max_execution_price`, evaluated at order-submit time) so a tight entry-band cap does not leak into the chase-up ceiling.

Both are out of scope for this plan, which is measurement-only.
