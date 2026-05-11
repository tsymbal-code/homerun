# Plan 0052 — Pre-fix evidence

Snapshot captured 2026-05-11 18:08 UTC against `polyhome-1` postgres.

## Baseline window: 2026-05-11 17:00:00 → 17:30:00 UTC

### `trade_signals` for `strategy_type='crypto_5m_last_outcome'`

```
  status  | count
----------+-------
 executed |     9
 expired  |     9
```

### `trader_events.firehose_emit` for the same window (per asset)

```
 asset | emits
-------+-------
 BTC   |     6
 SOL   |     6
 XRP   |     6
```

Every cycle emits, every emit becomes a `trade_signals` row, but 50%
of those rows die on the projection sweep before the trader can pick
them up.

`executed / (executed + expired) = 9 / 18 = 50%`. Pass criterion of
this plan: ≥ 90% over a 30-minute post-deploy window.

## Representative cycle: 17:15 UTC

All three side-by-side INSERTs at the same micro-second; all three
flipped to `expired` at the same micro-second 2.47 s later. No
intervening `crypto_5m_midcycle` write in that 5 s window — this is
not a sibling-strategy override, it's a `_project_status_batch`
sweep whose `keep_dedupe_keys` snapshot was captured before the
INSERT landed.

```
                id                |            dedupe_key            | status  |         created_at         |         updated_at         | age_at_update_s
----------------------------------+----------------------------------+---------+----------------------------+----------------------------+-----------------
 7d359460e9184554b541bc6619473158 | dd9064d241d5799d704e57fec0d6fc7a | expired | 2026-05-11 17:15:30.101874 | 2026-05-11 17:15:32.573268 |        2.471394
 bb94394b436c4dd4b74f75cf15d0431c | c8c43f4cc8e64a1ff23e4227393d898f | expired | 2026-05-11 17:15:30.101874 | 2026-05-11 17:15:32.573268 |        2.471394
 db016bff4ea542c4be9b4e8493e32251 | 49c5d3fe6c703cfc6ac8b40b7e8a6d20 | expired | 2026-05-11 17:15:30.101874 | 2026-05-11 17:15:32.573268 |        2.471394
```

The 2.47 s `updated_at - created_at` delta is the smoking gun: the
60 s grace window introduced by Task 2 of this plan would have
held all three rows past that sweep, leaving them eligible for the
trader's next cursor.

## Reproducible query for any future cycle

```sql
SELECT id, dedupe_key, status, created_at, updated_at,
       EXTRACT(EPOCH FROM (updated_at - created_at)) AS age_at_update_s
FROM trade_signals
WHERE strategy_type='crypto_5m_last_outcome'
  AND created_at BETWEEN '<cycle_start - 5s>' AND '<cycle_start + 35s>'
ORDER BY created_at;
```

Pre-deploy, expect 0–3 rows per cycle and `age_at_update_s ≈ 2–3`.
Post-deploy, expect 3 rows per cycle and `status='executed'` for
all three on `executed / (executed + expired) ≥ 90%`.
