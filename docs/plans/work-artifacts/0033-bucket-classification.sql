-- Plan 0033 / Task 3: for each cancelled trader_order, find the
-- nearest microstructure snapshot (within ±15s) and pull best_bid /
-- best_ask.  This is the canonical evidence for "what was the book
-- doing at submit time?" — far stronger than after-the-fact CLOB
-- trade reconstruction in thinly-traded sports markets.
WITH cancelled AS (
  SELECT
    id,
    created_at,
    market_id,
    market_question,
    payload_json#>>'{leg,leg,token_id}'                                AS token_id,
    (payload_json#>>'{leg,leg,price}')::numeric                        AS leg_signal_price,
    (payload_json#>>'{strategy_context,max_entry_price}')::numeric     AS ctx_max_entry_price,
    (payload_json#>>'{leg,shadow_simulation,ensemble,realistic,estimate,limit_price}')::numeric AS shadow_limit_price,
    (payload_json#>>'{strategy_params,max_probability}')::numeric      AS cfg_max_probability,
    (payload_json#>>'{leg,shares}')::numeric                            AS leg_shares,
    (payload_json#>>'{leg,shadow_simulation,survival_features,spread_bps}')::numeric AS sim_spread_bps
  FROM trader_orders
  WHERE trader_id = '388da687054c4b4a858ea152fff04900'
    AND status = 'cancelled'
),
nearest_book AS (
  SELECT
    c.id,
    c.created_at,
    c.market_id,
    c.token_id,
    c.leg_signal_price,
    c.ctx_max_entry_price,
    c.shadow_limit_price,
    c.cfg_max_probability,
    c.leg_shares,
    c.sim_spread_bps,
    s.observed_at      AS book_observed_at,
    s.best_bid         AS book_best_bid,
    s.best_ask         AS book_best_ask,
    s.spread_bps       AS book_spread_bps,
    EXTRACT(EPOCH FROM (s.observed_at - c.created_at)) AS book_offset_s
  FROM cancelled c
  LEFT JOIN LATERAL (
    SELECT observed_at, best_bid, best_ask, spread_bps
    FROM market_microstructure_snapshots m
    WHERE m.token_id = c.token_id
      AND m.snapshot_type = 'book'
      AND m.observed_at BETWEEN c.created_at - INTERVAL '15 seconds'
                            AND c.created_at + INTERVAL '15 seconds'
    ORDER BY abs(extract(epoch from (m.observed_at - c.created_at)))
    LIMIT 1
  ) s ON TRUE
)
SELECT
  id,
  created_at,
  market_id,
  token_id,
  leg_signal_price,
  ctx_max_entry_price,
  shadow_limit_price,
  cfg_max_probability,
  leg_shares,
  book_observed_at,
  book_best_bid,
  book_best_ask,
  book_spread_bps,
  book_offset_s,
  CASE
    WHEN book_best_ask IS NULL                          THEN 'no_book_snapshot'
    WHEN book_best_ask <= shadow_limit_price            THEN 'simulator_was_wrong'
    WHEN book_best_ask <= ctx_max_entry_price           THEN 'config_gated_chase_would_help'
    ELSE                                                     'book_above_chase_cap'
  END AS verdict
FROM nearest_book
ORDER BY created_at DESC;
