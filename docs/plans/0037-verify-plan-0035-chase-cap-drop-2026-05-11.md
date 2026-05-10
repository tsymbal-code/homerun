# Plan: Verify Plan 0035 chase-cap drop on 2026-05-11

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0037` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

> **Earliest run date is encoded in the filename:** do not start
> this plan before **2026-05-11 14:05 UTC**. The pre-deploy
> baseline was captured at 2026-05-10 14:05 UTC; a meaningful
> 24 h post-deploy comparison needs at least one full UTC day of
> post-fix `Sandbox - Tail-End` cycles.

## Overview

[Plan 0035](completed/0035-split-entry-band-from-execution-price-cap.md)
deployed the entry-band-vs-execution-price split on
2026-05-10 ~14:05 UTC. Its Task 5 captured the pre-deploy
baseline:

| Day | executed | cancelled | dropped_by_chase_cap |
|---|---:|---:|---:|
| 2026-05-07 | 0 | 7 | 7 |
| 2026-05-08 | 0 | 3 | 3 |
| 2026-05-09 | 2 | 13 | 13 |
| 2026-05-10 (partial, pre-deploy) | 7 | 10 | 10 |

100 % of the cancellations on this bot were
`limit_price_not_executable` — the cap-collapse pattern Plan 0035
fixed. Pre-deploy active-window rate ≈ 8.25 / day.

**Done means:** the SQL in Task 1 has been run on or after
2026-05-11 14:05 UTC; the verdict is recorded; either Plan 0035
is confirmed effective (this plan closes), or Plan 0035 is
re-opened with a Task 7 diagnosis containing the live SQL output
and the most likely root cause (live-path conflation that
slipped, or genuine deep-book scenario).

## Context / References

- [Plan 0035 (completed) — Split entry-band cap from execution-price cap](completed/0035-split-entry-band-from-execution-price-cap.md)
- [Plan 0033 (completed) — Verify Cox-PH shadow-fill pessimism](completed/0033-verify-cox-ph-shadow-fill-pessimism.md)
- [`docs/operational/runtime-tweaks.md` — Plan 0033 verdict + Plan 0035 simulation entry](../operational/runtime-tweaks.md)
- [Architecture: Cox-PH fill simulator, live execution, reconciliation, redeemer](architecture/execution-and-fills.md)

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'`

## Out of scope

This plan is a measurement-and-record-only follow-up to Plan
0035. It does **not**:

- Touch any of the 15 CRITICAL-tier safety knobs.
- Touch the chase-up reducer or any other code path. The
  semantic split shipped with Plan 0035 is the only structural
  change; this plan records its field effect.
- Re-tune any operator config. The
  `Sandbox - Tail-End` `max_probability=0.97` override stays
  in place per Plan 0035's Out-of-scope clause.

The only artefacts this plan writes are: a SQL output snapshot
(in this plan file), a verdict line in
[`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
(addendum to the Plan 0035 simulation entry), and (conditional)
a re-opened Plan 0035 with a fresh diagnosis task.

### Task 1: Run the post-deploy SQL on or after 2026-05-11 14:05 UTC

- [ ] Confirm we are at or past the gate time:
  `date -u +'%Y-%m-%d %H:%M UTC'` ≥ `2026-05-11 14:05 UTC`. If
  not, abort and resume later — the 24 h post-deploy window
  is the whole point.
- [ ] Run, recording the full output verbatim under
  "Recorded output" below:
  ```bash
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun" <<'SQL'
  SELECT
      date_trunc('day', created_at) AS day,
      COUNT(*) FILTER (WHERE status = 'executed') AS executed,
      COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
      COUNT(*) FILTER (WHERE status = 'cancelled'
                          AND payload_json#>>'{leg,reason}' = 'limit_price_not_executable') AS dropped_by_chase_cap
  FROM trader_orders
  WHERE trader_id = '388da687054c4b4a858ea152fff04900'
    AND created_at >= now() - interval '7 days'
  GROUP BY 1 ORDER BY 1 DESC;
  SQL
  ```
- [ ] Append the recorded output to this file in a fenced
  block under "Recorded output" before proceeding.
- [ ] Mark completed

#### Recorded output

```
(filled in when Task 1 runs)
```

### Task 2: Verdict

- [ ] Apply the decision rule:
  - **Drop confirmed** if 2026-05-11 (the first full
    post-deploy UTC day on this bot) shows
    `dropped_by_chase_cap` ≪ 8.25/day — operationally,
    "≪ 8.25" means roughly **≤ 3** (a > 60 % drop;
    accounting for the natural day-to-day jitter visible in
    the baseline table). A clean post-fix day where the bot
    crosses the book and has zero cap-collapse drops sits
    near 0.
  - **Drop NOT observed** if 2026-05-11
    `dropped_by_chase_cap` ≥ 6 (within ~30 % of the active-
    window baseline). That's the trigger to re-open Plan
    0035.
  - **Inconclusive** if 2026-05-11 had `executed +
    cancelled < 5` for this bot (low activity day; can't
    distinguish fix from absence of opportunities). In this
    case, postpone Task 3 by one more day; re-run Task 1
    on 2026-05-12 ≥ 14:05 UTC and append a second
    "Recorded output" block, then re-evaluate.
- [ ] Write the verdict (**confirmed / not observed /
  inconclusive**) inline below, with the numeric values that
  triggered it.
- [ ] Mark completed

#### Verdict

(filled in when Task 2 runs)

### Task 3: Record the verdict and close (or re-open Plan 0035)

- [ ] If verdict is **confirmed**, append a one-paragraph
  addendum to the Plan 0035 simulation entry in
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  with format:
  `### 2026-05-11 — Plan 0035 chase-cap drop confirmed
  (X dropped_by_chase_cap on 2026-05-11 vs ~8.25/day
  baseline)`. Include the executed/cancelled/dropped triple
  for the post-deploy day.
- [ ] If verdict is **not observed**, do NOT close this plan.
  Instead:
  1. Re-open Plan 0035 by `git mv`-ing it back from
     `docs/plans/completed/` to `docs/plans/`.
  2. Update the row in `plan-control-index.md` accordingly.
  3. Add `### Task 8: Diagnose post-deploy chase-cap drop
     failure` to Plan 0035 with three sub-bullets the
     diagnosis must cover: (a) re-audit the live-execution
     path for the same conflation pattern at any other
     submit site that wasn't covered by Task 1; (b) sample
     5 of the still-cancelled orders' `payload_json#>>'{leg}'`
     and `book_best_ask` at submit time to confirm whether
     the chase-up ceiling is being lifted as expected;
     (c) check whether the `tail_end_carry` strategy itself
     started emitting deeper `target_price` values that
     genuinely exceed available depth (in which case the
     fix landed but a new bottleneck is dominating).
  4. Close THIS plan as "completed (verdict: not observed,
     handed back to Plan 0035 Task 8)".
- [ ] If verdict is **inconclusive** (low-activity day),
  this plan stays open until Task 1 is re-run with enough
  activity. Task 1 records the second day's output; Task 2
  re-applies the decision rule; Task 3 then runs.
- [ ] Mark completed

### Task 4: Close-out

- [ ] Run all Validation Commands; all pass.
- [ ] `git log --grep='Plan: 0037'` shows the full commit set.
- [ ] `git mv docs/plans/0037-verify-plan-0035-chase-cap-drop-2026-05-11.md docs/plans/completed/`.
- [ ] Update the row in `plan-control-index.md` to point at
  `completed/`.
- [ ] Mark completed
