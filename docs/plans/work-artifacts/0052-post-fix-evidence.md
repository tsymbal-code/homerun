# Plan 0052 — post-fix evidence

Captured 2026-05-11 after the `signal_bus.expire_source_signals_except`
grace-period fix was deployed to `polyhome-1` via
`./deploy/sync_remote.sh` (which restarts `backend` and
`worker-trading`). Baseline window for comparison is in
[`0052-pre-fix-evidence.md`](0052-pre-fix-evidence.md).

## Verification window

`crypto_5m_last_outcome` signals in `created_at BETWEEN
'2026-05-11 18:30 UTC' AND '2026-05-11 19:00 UTC'` (exclusive of
19:00:30 cycle which fell after measurement):

```
  status  | count
----------+-------
 executed |     6
 expired  |     7
 pending  |     3
 skipped  |     2
(4 rows)  total = 18
```

Ratio: `executed / (executed + expired) = 6 / 13 = 46 %`.

## Verdict on the race-condition class (this plan's actual scope)

**Fully resolved.** Per-signal lifecycle of every `expired` row
in the window:

| signal_id | created_at | updated_at | age_at_update_s |
|---|---|---|---|
| 620c7664… | 18:30:30.055 | 18:31:33.257 | **63.20** |
| ff4ac33e… | 18:30:30.055 | 18:31:33.257 | **63.20** |
| 8b735f09… | 18:30:30.055 | 18:31:33.257 | **63.20** |
| 16d09743… | 18:35:30.666 | 18:36:38.549 | **67.88** |
| 457e1bcd… | 18:40:30.061 | 18:41:31.138 | **61.08** |
| acab853f… | 18:40:30.061 | 18:41:31.138 | **61.08** |
| d9ce69f6… | 18:40:30.061 | 18:41:31.138 | **61.08** |

All 7 `expired` rows survive past the 60 s grace cutoff. None
expires inside the race window (`age < 60 s`). Compare to the
pre-fix 17:15 cycle, where the same 3-row pattern flipped to
`expired` at `age = 2.47 s`. The grace guard is doing exactly
what the plan promised.

## Why the executed ratio is still below 90 %

Trader `eff366f86217484b98950ea836099a02` ("Crypto 5m Last
Outcome", subscribed to `source=crypto / strategy_key=
crypto_5m_last_outcome`) did emit `trader_decisions` for only
8 of the 18 signals. **Two full 5 m cycles produced zero trader
decisions** (18:30 — 3 signals, 18:40 — 3 signals).

`worker-trading` log evidence for the missed 18:30 cycle:

```
{"ts":"2026-05-11T18:32:42.336Z","level":"WARNING",
 "msg":"Fast trader cycle exceeded hard budget",
 "data":{"trader_id":"eff366f8…","duration_s":3.334,
         "stage_timings_ms":{"runtime_list_signals":0.6,
                              "signal_cache_hit":0.1,
                              "signal_source":"cache",
                              "idle_touch_commit":3332.9}}}
```

Diagnosis:

- `signal_cache_hit: 0.1 ms` + `signal_source: "cache"` →
  trader read its in-memory snapshot.
- `runtime_list_signals: 0.6 ms` → snapshot returned **zero
  candidate signals** even though three `pending` signals existed
  in the `trade_signals` table at that moment.
- `idle_touch_commit: 3332.9 ms` → trader cycle entered the idle
  path and burned its hard-budget on a no-op DB commit.

In other words: `intent_runtime`'s in-memory signal cache was
stale — it had not yet absorbed the 3 freshly-INSERT-ed signals
from `signal_bus` by the time `fast_trader_runtime` read it. The
cache eventually warmed up (the 18:35, 18:45, 18:50 cycles all
saw their signals fine), but two cycles' worth of signals lived
their full 60 s grace window without ever appearing in the
trader's pull view.

This is a **second, independent defect** that was hidden until
plan 0052 removed the race-expiration noise. It belongs in its
own plan:

- **Plan 0053**: `fast-trader signal cache miss between
  signal_bus INSERT and intent_runtime read`.

## Bottom line

| Question | Answer |
|---|---|
| Did 0052 fix the projection-sweep race? | **Yes.** 0/13 expired signals had `age < 60 s` (vs 9/9 pre-fix). |
| Did 0052 hit the ≥ 90 % executed ratio? | **No, 46 %.** All shortfall is due to a different, downstream cache defect. |
| Is more work needed in `signal_bus`? | No. The remaining defect is in `workers.fast_trader_runtime` / `intent_runtime` snapshot freshness. Tracked under Plan 0053. |
