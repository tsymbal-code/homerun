# Plan: Blacklist losing leader wallets on the Sandbox Traders Copy Trade bot

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0024` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate after the conservative live-risk-limit
> change from the 2026-05-10 runtime-tweaks entry has produced at
> least **200 additional terminal orders** on the Sandbox bot
> (`trader_id=61dcbeb2b9bc42bd9e9635a09ae5e0c3`). Current sample of
> 217 terminal orders is borderline for per-leader pruning — most
> losing leaders have only 5–8 trades, which is suggestive but not
> statistically firm. Re-pull per-leader P&L when sample doubles,
> re-confirm the same losers persist, then activate.

## Overview

A 217-terminal-order audit of the Sandbox bot on 2026-05-10
surfaced clear concentration of losses in a small set of leader
wallets. The five worst by total realised P&L:

| Leader | n | P&L | `in_top_pool`? |
|---|---:|---:|---|
| `0x3f3aa700...e8fd` | 5 | −$63 | ✓ |
| `0xbddf61af...c684` | 6 | −$52 | ✓ |
| `0x8e8ccd01...65dd` | 8 | −$45 | ✗ |
| `0xa5e3044f...4d7e` | 25 | −$35 | ✗ |
| `0x3471a897...f17e` | 6 | −$34 | ✓ |

Combined drag = **−$228**. Net Sandbox P&L was **+$12** for the
audit window; without these five it would have been roughly
**+$240**. Three of the five are still in the auto-managed top
pool with no exclusion flag.

The existing `discovered_wallets.source_flags` JSONB carries
operator-driven exclusion keys
(`pool_blacklisted`, `pool_manual_exclude`) honoured by
[`SmartWalletPoolService`](../../../backend/services/smart_wallet_pool.py:175)
on its 1-min recompute cycle. No new code is needed — this plan
is purely a curated SQL update + after-monitoring.

The plan is parked in `backlog/` until the sample doubles because
acting on a 5-trade sample risks variance: a leader with 5
straight losses might still have a positive long-run expectancy
that randomness obscured. Statistically firmer pruning needs
≥ 15–20 terminal orders per candidate.

### What "done" looks like

- The five losing leaders are flagged in
  `discovered_wallets.source_flags.pool_manual_exclude=true` (and
  `pool_blacklisted=true` for the worst three with the largest
  absolute losses).
- Within 1 min of the SQL update, the next `SmartWalletPoolService`
  recompute drops them from `in_top_pool` for the three currently
  in the pool.
- Sandbox bot's `traders_scope_context` rebuild (60-s cache TTL)
  picks up the smaller pool and stops generating signals from
  these wallets.
- Operator records before/after per-leader P&L in
  `runtime-tweaks.md` to track effect.

## Context / References

- Pool override mechanism:
  [`backend/services/smart_wallet_pool.py:87-88, 2024-2036`](../../../backend/services/smart_wallet_pool.py:87)
  (`pool_blacklisted`, `pool_manual_exclude` source_flags keys).
- Pool recompute cadence (every 1 min):
  [`backend/services/smart_wallet_pool.py:175`](../../../backend/services/smart_wallet_pool.py:175).
- `traders_scope` cache (60-s TTL):
  [`backend/workers/trader_orchestrator_worker.py:3610`](../../../backend/workers/trader_orchestrator_worker.py:3610).
- Common-bot doc § wallet scope:
  [`docs/strategies/_common-bot-parameters.md`](../../strategies/_common-bot-parameters.md).
- Original audit (in-conversation, 2026-05-10): per-leader
  breakdown SQL recipe lives in this plan's Task 1.

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT address, source_flags->>'\''pool_manual_exclude'\'' AS excluded, source_flags->>'\''pool_blacklisted'\'' AS blacklisted, in_top_pool FROM discovered_wallets WHERE address = ANY(ARRAY['\''0x3f3aa700...'\'', '\''0xbddf61af...'\'', '\''0x8e8ccd01...'\'', '\''0xa5e3044f...'\'', '\''0x3471a897...'\''])"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=2m worker-discovery 2>&1 | grep -iE "smart.?wallet.?pool|recompute"'`

## Out of scope

- **Building a new auto-blacklist mechanism.** This plan uses the
  existing operator-override flag. A rolling-P&L auto-blacklist
  belongs in a separate plan (call it 0028 — "Auto-blacklist
  wallets that lose for a given trader bot over a rolling
  window").
- **Adjusting `SmartWalletPoolService` scoring weights.** The
  insider/composite scoring is built for global "is this wallet
  smart" judgement; tuning it for per-bot P&L would entangle two
  concerns. Use exclusion flags for per-bot filtering.
- **Pool-size or threshold tuning.** That's a separate "tighten
  pool" plan if the manual exclusions don't move the needle
  enough.

## Activation prerequisite

Before unparking, run:

```sql
SELECT count(*)
FROM trader_orders o
WHERE o.trader_id = '61dcbeb2b9bc42bd9e9635a09ae5e0c3'
  AND o.status IN ('closed_win','closed_loss','resolved_win','resolved_loss')
  AND o.created_at > '2026-05-10 12:00:00'  -- post conservative-limits deploy
```

Wait until count ≥ 200, then re-run the per-leader breakdown SQL
in Task 1 to confirm the same five leaders are still bottom-of-list.
If a different set of leaders surfaces (P&L drift), update this
plan's leader list before activation.

### Task 1: Pull a fresh per-leader P&L breakdown

Re-run the audit on the bigger sample and confirm the same
worst-5 (or whatever the actual worst-5 turn out to be).

- [ ] Run:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "
      SELECT
        substr(s.payload_json::jsonb -> '\''strategy_context'\'' -> '\''copy_event'\'' ->> '\''wallet_address'\'', 1, 14) AS leader_short,
        s.payload_json::jsonb -> '\''strategy_context'\'' -> '\''copy_event'\'' ->> '\''wallet_address'\'' AS leader,
        count(*) AS n,
        sum(o.actual_profit) AS pnl,
        round((count(*) FILTER (WHERE o.actual_profit > 0))::numeric / nullif(count(*), 0) * 100, 1) AS win_rate_pct
      FROM trader_orders o
      JOIN trade_signals s ON s.id = o.signal_id
      WHERE o.trader_id = '\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\''
        AND o.status IN ('\''closed_win'\'', '\''closed_loss'\'', '\''resolved_win'\'', '\''resolved_loss'\'')
        AND o.created_at > '\''2026-05-10 12:00:00'\''
      GROUP BY 1, 2
      HAVING count(*) >= 10
      ORDER BY 4 ASC LIMIT 10
      "'
      ```
- [ ] Record the actual top-5 losers (n ≥ 10 sample) in this checkbox.
      If they overlap with the original audit's list, proceed to
      Task 2. If not, update Task 2's address list.
- [ ] Mark completed

### Task 2: Apply the exclusion flags

Use existing `pool_manual_exclude` for the marginal cases and
`pool_blacklisted` for the worst (`pool_blacklisted` is permanent
and survives operator re-includes; `pool_manual_exclude` can be
flipped back).

- [ ] For the **two worst by absolute loss with n ≥ 15**:
      ```sql
      UPDATE discovered_wallets
      SET source_flags = jsonb_set(
        coalesce(source_flags, '{}'::jsonb),
        '{pool_blacklisted}', 'true'::jsonb
      )
      WHERE address IN ('<addr1>', '<addr2>');
      ```
- [ ] For the **next three (n ≥ 10)**:
      ```sql
      UPDATE discovered_wallets
      SET source_flags = jsonb_set(
        coalesce(source_flags, '{}'::jsonb),
        '{pool_manual_exclude}', 'true'::jsonb
      )
      WHERE address IN ('<addr3>', '<addr4>', '<addr5>');
      ```
- [ ] Wait 60 s for `SmartWalletPoolService` recompute. Then run
      the verification command in `## Validation Commands` and
      confirm `excluded=true` (or `blacklisted=true`) and
      `in_top_pool=false` for all five.
- [ ] Wait another 60 s and confirm `traders_scope_context` cache
      rebuild dropped these wallets from the Sandbox bot's
      working set:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=3m worker-trading 2>&1 | grep "traders_scope_context"' | tail -10
      ```
- [ ] Mark completed

### Task 3: Document + monitor

- [ ] Append a dated entry to
      [`docs/operational/runtime-tweaks.md`](../../operational/runtime-tweaks.md):
      pre-action per-leader P&L summary, the addresses excluded,
      the SQL applied, expected effect (drop in losses on next
      cycle), and rollback recipe (set the flags to `false`).
- [ ] Schedule re-audit at +24 h, +7 d. Compare:
      - Sandbox bot win rate before vs after
      - Realised P&L delta
      - Whether new losing leaders emerge (rotation effect)
- [ ] If effect is positive, leave excluded; if negative or no
      change, revert via `UPDATE ... SET source_flags = ... =
      'false'` and consider a different angle (category-based
      filter, sample re-validation).
- [ ] `git mv docs/plans/0024-...md docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](../plan-control-index.md)
      to point at `completed/`.
- [ ] Mark completed
