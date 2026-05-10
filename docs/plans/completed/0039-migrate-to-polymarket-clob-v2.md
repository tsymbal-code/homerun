# Plan: Migrate Polymarket integration to CLOB V2 (addresses, OrderFilled topic, decoder, approve operators)

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0039` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Polymarket cut over from CLOB V1 to CLOB V2 on **2026-04-28**. Their
old exchange contracts (`0x4bFb41…D8B8982E` for CTF Exchange,
`0xc5D563…220f80a` for Neg Risk CTF Exchange) and the old `OrderFilled`
event signature (`0xd0a08e8c…f6` topic) are deprecated and currently
emit zero events on Polygon mainnet. New trading lives on
`0xE111180000d2663C0091e4f400237545B87B996B` (CTF Exchange V2) and
`0xe2222d279d744050d28e00520010520000310F59` (Neg Risk V2), with a
new `OrderFilled` ABI carrying `side`, `tokenId`, `builder`, `metadata`
(topic `0xd543adfd…d8ee`).

In our codebase the V1 constants are hard-coded in
[`backend/services/wallet_ws_monitor.py:40-48`](../../backend/services/wallet_ws_monitor.py)
(producer-side: every leader-trade detection passes through here) and
in [`backend/services/ctf_execution.py:72`](../../backend/services/ctf_execution.py)
(`CTF_EXCHANGE` operator address used as the target of
`setApprovalForAll` on `conditional_tokens`). The result is that
`worker-trading` opens an `eth_getLogs` subscription on dead V1
contracts and receives 0 logs forever; the copy-trade pipeline never
sees leader trades regardless of how many wallets are tracked.

Submit-side appears already V2-aware via `py-clob-client-v2`
(`config.get_contract_config(137)` exposes `exchange_v2` and
`neg_risk_exchange_v2`), but it is not yet verified in this codebase
that `ClobClient`/`OrderBuilder` actually selects V2 as
`verifyingContract` for the EIP-712 envelope. Task 1 of this plan
converts that assumption into evidence; if it turns out V1 is still
being signed, the same plan covers the fix.

### What "done" looks like

- `wallet_ws_monitor` reads logs from the V2 exchanges (3000+ logs per
  30 blocks observed live) and decodes them with a V2-only parser. V1
  fallback branches are removed, not retained, per the
  "clean cut, not back-compat" rule in [`AGENTS.md`](../../AGENTS.md).
- `wallet_monitor_events` table receives rows again; the
  `traders_copy_trade_signal_service` `_on_wallet_trade` callback
  fires; trader `Focused - 0x10c95474a8` (or any other future
  `traders_scope.individual_wallets` consumer) sees decisions for
  the leader's actual trades.
- `live_execution_service` and the fast-submit path are confirmed to
  sign EIP-712 with `verifyingContract = exchange_v2` /
  `neg_risk_exchange_v2`; if a config change is required for that,
  it ships in this plan.
- `ctf_execution.ensure_exchange_approval` approves the V2 exchange
  operator(s), so on-chain ERC-1155 split / merge / transfer paths
  work against V2.
- Test suite (`tests/test_wallet_ws_monitor.py`) is rewritten against
  V2 ABI fixtures; V1 cases are removed, not skipped. Round-trip
  parsing of a real captured V2 log (one `transactionHash` from a
  live block) is pinned as a regression test.
- [`architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  pipeline diagram + key-files table reflect V2 addresses and the new
  `OrderFilled` payload shape.
- [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  carries one journal entry recording the cutover, the rollback
  recipe, and the post-deploy event-rate measurement.

## Context / References

- **Producer of stuck V1 reads:**
  [`backend/services/wallet_ws_monitor.py:40-48`](../../backend/services/wallet_ws_monitor.py)
  (constants), `:255-322` (`_parse_order_filled_log`),
  `:325-…` (`_determine_trade_side_and_details`),
  `:1073-1119` (`_get_logs_for_block` `eth_getLogs` filter),
  `:1371-…` (maker/taker check vs `CTF_EXCHANGE_ADDRESS_SET`).
- **Approve-side V1 hardcode:**
  [`backend/services/ctf_execution.py:72`](../../backend/services/ctf_execution.py)
  (`CTF_EXCHANGE` constant), `:659-…` (`ensure_exchange_approval`).
- **Submit-side surfaces (need V2 verification):**
  [`backend/services/live_execution_service.py:2031-2160`](../../backend/services/live_execution_service.py)
  (`initialize`, `_apply_signature_type_to_client`),
  [`backend/services/trader_orchestrator/fast_submit.py`](../../backend/services/trader_orchestrator/fast_submit.py),
  [`backend/services/market_runtime.py`](../../backend/services/market_runtime.py),
  [`backend/services/trading_proxy.py`](../../backend/services/trading_proxy.py).
- **SDK that already knows V2:**
  `py-clob-client-v2>=1.0.0` (see
  [`backend/requirements-trading.txt`](../../backend/requirements-trading.txt)).
  `from py_clob_client_v2.config import get_contract_config;
  get_contract_config(137)` returns both V1 and V2 addresses.
- **Architecture note that gets refreshed:**
  [`architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  (the wallet-monitor box at the top of the pipeline diagram).
- **Polymarket V2 cutover docs (provenance for the constants):**
  - [docs.polymarket.com / v2-migration](https://docs.polymarket.com/v2-migration)
  - [docs.polymarket.com / resources/contracts](https://docs.polymarket.com/resources/contracts)
  - GitHub: [Polymarket/ctf-exchange-v2](https://github.com/Polymarket/ctf-exchange-v2)
  - V2 ABI source:
    [`ITrading.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/interfaces/ITrading.sol)

### V2 constants (single source of truth for this plan)

```
EXCHANGE_V2          = 0xE111180000d2663C0091e4f400237545B87B996B
NEG_RISK_EXCHANGE_V2 = 0xe2222d279d744050d28e00520010520000310F59
ORDER_FILLED_TOPIC   = 0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee

OrderFilled(
  bytes32 indexed orderHash,
  address indexed maker,
  address indexed taker,
  uint8   side,
  uint256 tokenId,
  uint256 makerAmountFilled,
  uint256 takerAmountFilled,
  uint256 fee,
  bytes32 builder,
  bytes32 metadata
)

Live log shape: topics=4, data_words=7 (verified 2026-05-10 via
                                          eth_getLogs, 30-block window:
                                          3084 logs on V2 CTF + 893 on
                                          V2 Neg Risk).
```

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_wallet_ws_monitor.py`
- `bash scripts/run_tests_remote.sh tests/test_ctf_execution.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT count(*) FROM wallet_monitor_events WHERE created_at > now() - interval '\''10 minutes'\''"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=10m worker-trading | rg "wallet_ws_monitor.*Trade detected" | head -10'`

## Out of scope

- **Historical backfill of V1 events.** V1 is deprecated; there is no
  active V1 trading to ingest. We do **not** retain V1 addresses or
  the V1 topic in the monitor as a fallback set, and we do **not**
  add a dual-decoder branch. (Per `AGENTS.md` "Clean cut, not
  backwards compatible.") If a one-shot V1-history backfill is ever
  needed, it is a separate plan that touches a separate module.
- **Multi-chain support.** V2 contracts on Polygon (`chain_id=137`)
  only. No Amoy / testnet wiring.
- **`metadata` / `builder` payload semantics.** V2's two new bytes32
  fields are decoded into hex strings and stored on
  `WalletTradeEvent.builder` / `.metadata` for future use, but no
  downstream consumer is wired to interpret them in this plan. (If
  Polymarket's referral programme requires populating `builder` on
  submit, that is a follow-up plan.)
- **Tier promotion of any knob.** No `risk_limits` / `strategy_params`
  values are touched. No CRITICAL or HIGH-tier knob is read or
  written. The CRITICAL knob walkthrough policy (see
  [`README.md`](README.md#critical-knob-touch-policy)) does not
  apply.

### Task 1: Confirm submit-side already signs against V2 (or write the fix)

Goal: avoid a one-sided migration where logs read V2 but order
submission still signs V1 (resulting in 100 % rejected orders the
moment live trading is re-enabled).

- [x] Inside `worker-trading`, instantiate `ClobClient` the same way
      `live_execution_service.initialize` does and inspect
      `client.builder.contract_config` (or whichever attribute carries
      `verifyingContract`). Record whether it resolves to the V2
      exchange (`0xE111…996B` / `0xe222…0F59`) or the V1 one. Document
      the inspection command + output as a comment paragraph at the
      top of `live_execution_service.py:initialize` so a future agent
      doesn't have to re-derive it.
      **Result:** `ClobClient.get_version()` returns `2` against live
      `https://clob.polymarket.com` (Polymarket CLOB cut over on
      2026-04-28). The SDK's `client.create_order` calls
      `__resolve_version()` → `2` → `builder.build_order(version=2)`
      → uses `contract_config.exchange_v2 = 0xE111…996B` or
      `neg_risk_exchange_v2 = 0xe222…0F59` as `verifyingContract`.
      No call site in our backend overrides this with `version=1`
      (verified by repo-wide grep — only matches are
      `strategy_version=1` / `control_version = 1` /
      `forced_version=1`, none of which touch `ClobClient`).
      Comment paragraph added to
      [`backend/services/live_execution_service.py`](../../backend/services/live_execution_service.py)
      `initialize()` docstring with the reproducer command.
- [x] If V2 is selected: no submit-side change. Tick this Task as
      "verified — no fix needed" and proceed to Task 2.
      **Confirmed: V2 selected automatically by the SDK; no
      submit-side change required.**
- [x] If V1 is still selected: identify whether it's
      `chain_id`/`signature_type`/`exchange_address` that needs an
      explicit override on the `ClobClient` constructor, apply the
      smallest single change, and re-verify with the same inspection.
      Add the change to
      [`backend/services/live_execution_service.py`](../../backend/services/live_execution_service.py)
      and to **all** other instantiation sites
      (`fast_submit.py`, `market_runtime.py`, `trading_proxy.py`,
      `routes_settings.py` test-connection path, `routes_orchestrator_live.py`).
      **Not applicable — V2 is selected (see above).**
- [x] `bash scripts/run_tests_remote.sh tests/test_routes_settings_polymarket_connection.py`
      remains green.
      **Skipped — no submit-side change was made; running an unrelated
      test would not exercise this Task's surface.**
- [x] Mark completed

### Task 2: Replace V1 constants with V2 in `wallet_ws_monitor.py`

- [x] Replace `CTF_EXCHANGE_ADDRESSES` (lines 40-43) with a single
      tuple of V2 addresses
      (`0xE111180000d2663C0091e4f400237545B87B996B`,
      `0xe2222d279d744050d28e00520010520000310F59`). Rename the
      symbol to `POLYMARKET_EXCHANGE_ADDRESSES_V2` so a `git grep` of
      the V1 constant in any future regression turns up zero hits.
- [x] Replace `CTF_EXCHANGE_ADDRESS_SET` accordingly (the lower-cased
      lookup set used in `_determine_trade_side_and_details` for the
      maker-side third-party leg filter at line 1373).
- [x] Replace `ORDER_FILLED_TOPIC` (line 48) with the V2 hash
      `0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee`
      and update the inline comment to reflect the V2 signature
      (`OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)`).
- [x] Update the file-level docstring (lines 1-9) to remove the
      V1-era `terauss/Polymarket-Copy-Trading-Bot` reference and to
      explicitly note "Polymarket CLOB V2 (post-2026-04-28 cutover)".
- [x] Mark completed

### Task 3: Rewrite `_parse_order_filled_log` for the V2 ABI

V2 layout: `topics=4`, `data_words=7`. Indexed on `orderHash`
(topic[1]), `maker` (topic[2]), `taker` (topic[3]); `data` carries
`(side, tokenId, makerAmountFilled, takerAmountFilled, fee,
builder, metadata)`.

- [x] Delete both existing branches in
      [`backend/services/wallet_ws_monitor.py:255-322`](../../backend/services/wallet_ws_monitor.py)
      (the "indexed-address" 4t+5w branch and the "legacy" 2t+7w
      branch). Replace with a single branch that requires
      `len(topics) >= 4 and len(data_hex) >= 64 * 7`; everything
      shorter returns `None`.
- [x] Decode in this order:
      `orderHash = topics[1]`,
      `maker = _decode_address_from_topic(topics[2])`,
      `taker = _decode_address_from_topic(topics[3])`,
      `side = _decode_uint256(data_words[0]) & 0xff` (the high bytes
      are zero-padding for ABI alignment),
      `token_id = str(_decode_uint256(data_words[1]))`,
      `maker_amount_filled = _decode_uint256(data_words[2])`,
      `taker_amount_filled = _decode_uint256(data_words[3])`,
      `fee = _decode_uint256(data_words[4])`,
      `builder = "0x" + data_words[5]`,
      `metadata = "0x" + data_words[6]`.
- [x] Update the return-dict keys to drop the V1 `maker_asset_id` /
      `taker_asset_id` fields entirely (they no longer exist in V2)
      and to add `side`, `token_id`, `builder`, `metadata` instead.
      Every consumer of the parser output is in the same file
      (`_determine_trade_side_and_details`, `_handle_log`); update
      both call sites in this same task — clean cut, no shim.
- [x] Update the docstring on `_parse_order_filled_log` to describe
      only the V2 layout. Remove the "Legacy layout" comment block.
- [x] Mark completed

### Task 4: Rewrite `_determine_trade_side_and_details` against V2 fields

V1 inferred BUY/SELL from the `(makerAssetId == 0 ? collateral : token)`
trick. V2 hands us `side: uint8` and `token_id: uint256` directly.
The new logic is strictly simpler and cannot misclassify legs.

- [x] Look up the canonical meaning of `side` from
      `Polymarket/ctf-exchange-v2` `Side` enum (`0 = BUY`,
      `1 = SELL`; capture the exact values in a comment that cites
      the source file + line). Map directly to `"BUY"` / `"SELL"`
      strings.
- [x] Compute `size_outcome_tokens = taker_amount_filled / 1e6`
      when `side == BUY` (taker leg pays USDC, receives outcome
      tokens — the maker amount is USDC) and the mirror for
      `side == SELL`. Document the unit conversion in a one-line
      comment citing Polymarket V2 USDC.e (6 decimals,
      `0x2791bca1f2de4661ed88a30c99a7a9449aa84174`).
- [x] Compute `price = maker_amount_filled / taker_amount_filled` for
      BUY (USDC paid per outcome token) and the inverse for SELL,
      mirroring V1 semantics so downstream consumers
      (`traders_copy_trade_signal_service`) see no contract change in
      the `WalletTradeEvent.price` / `.size` fields.
- [x] Drop the maker-side third-party-leg filter (`if maker_lower not
      in CTF_EXCHANGE_ADDRESS_SET: return` at line 1373) and replace
      with a V2-correct filter: skip the log only if both `maker`
      and `taker` are addresses we don't track. (V2 doesn't put
      exchange contract addresses into maker/taker positions; both
      sides are real wallets, so the "third-party leg" anti-pattern
      is structurally absent. The new filter is a stricter "neither
      side is a tracked wallet → skip".)
- [x] Mark completed

### Task 5: Add V2 exchange operators to `ctf_execution.ensure_exchange_approval`

`CTF_EXCHANGE` in
[`backend/services/ctf_execution.py:72`](../../backend/services/ctf_execution.py)
is the operator we authorise via `setApprovalForAll` on the
`conditional_tokens` contract. Approve has to cover **every**
operator that may move our ERC-1155 holdings; V2 is a different
contract from V1 and does not inherit the V1 approval.

- [x] Replace the single `CTF_EXCHANGE = "0x4bFb41…"` class attribute
      with a tuple of V2 operators (the V2 CTF Exchange and V2 Neg
      Risk Exchange). Source the values from
      `py_clob_client_v2.config.get_contract_config(137)` at module
      import time so we don't duplicate the addresses, and pin them
      with an `assert` at construction (defence against an SDK upgrade
      silently changing the canonical addresses).
- [x] Update `ensure_exchange_approval` (line 659) to iterate over
      the tuple and approve each operator. Either (a) emit one
      `setApprovalForAll` tx per operator, or (b) reuse the existing
      single-tx approve with the correct operator address per call.
      Pick (a) for clarity — two on-chain txs at first ensure-call
      is acceptable; subsequent calls find both already approved and
      no-op.
- [x] Update the call site
      [`backend/services/ctf_execution.py:725-727`](../../backend/services/ctf_execution.py)
      to gate on "all operators approved", not "the single approve
      succeeded".
- [x] Mark completed

### Task 6: Rewrite the V1 cases in `tests/test_wallet_ws_monitor.py`

- [x] Capture one real V2 `eth_getLogs` response from a recent
      Polygon block (use `https://polygon.drpc.org` HTTP RPC; the
      `_get_logs_for_block` filter shape lives in `wallet_ws_monitor.py:1082-1093`).
      Save the JSON as a fixture under
      `backend/tests/fixtures/polymarket_v2_order_filled_logs.json`
      (one or two logs is enough). Include the full transaction
      hash so a reviewer can re-derive the data from the chain.
- [x] Delete every V1-decoder test case (the ones that constructed
      synthetic 4t+5w / 2t+7w log dicts). They no longer exercise
      reachable code paths; per the "delete, don't deprecate" rule
      they go entirely.
- [x] Add `test_parse_order_filled_log_v2_real_block`: load the
      fixture, run `_parse_order_filled_log`, assert exact values
      for `order_hash`, `maker`, `taker`, `side`, `token_id`,
      `maker_amount_filled`, `taker_amount_filled`, `fee`,
      `builder`, `metadata`. Numeric values must match what
      polygonscan shows for the same `transactionHash`.
- [x] Add `test_parse_order_filled_log_rejects_v1_shaped_log`:
      construct a synthetic 4t+5w log (V1 shape) and assert
      `_parse_order_filled_log(...)` returns `None`. Pins that the
      V1 fallback was actually removed and not just renamed.
- [x] Add `test_determine_trade_side_v2_buy_and_sell_round_trip`:
      two parsed-dict fixtures (one BUY, one SELL), call
      `_determine_trade_side_and_details(parsed, wallet)`, assert
      `("BUY"|"SELL", token_id, size, price)` matches
      hand-calculated values.
- [x] Run validation:
      `bash scripts/run_tests_remote.sh tests/test_wallet_ws_monitor.py`.
- [x] Mark completed

### Task 7: Pre-deploy baseline + deploy + post-deploy live verification

- [x] **Pre-deploy baseline** (numbers live inline as evidence):
      - `wallet_monitor_events` count for the last 1 h: **0**.
      - Same query at 24 h: **0**. Confirms the producer was fully
        silent against V1 contracts before the cutover patch — not a
        slow-trickle, a hard zero.
      - `worker-trading` log tail (last 30 min) shows
        `set_wallets_for_source` heartbeats every 15 s but no
        `Trade detected` lines anywhere — the monitor is alive,
        opening WS subscriptions, but receiving zero
        `OrderFilled` events.
      - Verified live: `clob_v2_get_version()` against
        `https://clob.polymarket.com` already returns `2`, so
        submit-side is unaffected by the wallet-monitor change
        (Task 1 evidence, no rollback needed there).
- [x] `./deploy/sync_remote.sh` from local checkout. All containers
      came up `Up (healthy)`:
      `homerun-postgres`, `homerun-redis`, `homerun-backend`,
      `homerun-worker-trading`, `homerun-worker-discovery`,
      `homerun-worker-news`, `homerun-frontend`.
      Migrate one-shot exited 0; ~46 s end-to-end deploy.
- [x] **Post-deploy live RPC verification** (~3 min after redeploy):
      - `wallet_ws_monitor` is filtering `eth_getLogs` against
        `POLYMARKET_EXCHANGE_ADDRESSES_V2` + V2
        `ORDER_FILLED_TOPIC`. Live event flow detected: **236
        events / 10 min** processed (132 BUY + 104 SELL across the
        48 tracked source wallets).
      - Zero `wallet_ws_monitor` / `ctf_execution`
        ERROR-or-traceback log lines over a 10-minute window.
      - The pre-existing `Missing Polymarket API credentials`
        ERROR in `live_execution_service.initialize` is unrelated
        to this plan — it's the operator's pending in-UI
        credential entry and does not block the wallet monitor's
        on-chain feed (which runs against public Polygon RPC, not
        the CLOB API).
- [x] **Post-deploy DB verification**:
      - `select count(*) from wallet_monitor_events where
         detected_at > now() - interval '5 minutes'` → **234**
         (vs **0** baseline).
      - `select count(*) from wallet_monitor_events where
         detected_at > now() - interval '10 minutes'` → **236**.
      - Side / price / size sanity:

        ```text
         side | count |  min(price)  |  max(price)  | min(size) | max(size)
        ------+-------+--------------+--------------+-----------+----------
         BUY  |   132 |        0.002 |        0.999 |      0.06 |  43470.20
         SELL |   104 |        0.003 |        0.74  |      5.00 |   3563.94
        ```

        All prices are inside `(0, 1)` (sane for prediction-market
        outcome tokens); both maker- and taker-side branches are
        exercised (taker-side rows are visible because the V1
        third-party-leg filter was removed in Task 4 — V2 contracts
        never appear as maker/taker, so the filter would have
        silently dropped every taker-side hit if kept).
- [x] Mark completed

### Task 8: Update architecture notes + operational journal + close out

- [x] Update
      [`architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
      pipeline-diagram comment for the `wallet_ws_monitor` box
      mentions V2 contracts and the V2 `OrderFilled` ABI; the
      "Key files" table (if present in the file's lower half)
      mentions the new constants by name; bump
      `Last verified: YYYY-MM-DD` at the bottom.
- [x] Append a journal entry in
      [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
      with: deploy date, surface (`wallet_ws_monitor.py` +
      `ctf_execution.py` + any submit-side change from Task 1),
      pre/post `wallet_monitor_events` rate, regression-test
      reference, and rollback recipe (`git revert <SHA-range>` +
      `./deploy/sync_remote.sh` → V1 constants return → monitor
      goes silent again, but no other regressions).
- [x] `git mv docs/plans/0039-migrate-to-polymarket-clob-v2.md
      docs/plans/completed/`.
- [x] Update the row in [`plan-control-index.md`](plan-control-index.md)
      to point at the `completed/` path.
- [x] `git log --grep='Plan: 0039'` shows the full commit chain.
- [x] Mark completed
