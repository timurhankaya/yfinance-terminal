"""v_actions and v_price_bars_regular VIEWs.

actions is not a table; it is the union of dividends + splits + capital_gains.
"""

from __future__ import annotations

# CAST(... AS VARCHAR(16)): the literal's length fixes the view's column
# type. Without it, adding a new action type would silently change the
# type. A quoted PostgreSQL literal is `unknown` and resolves to `text`
# inside a UNION, so the explicit cast is kept.
#
# Not CHAR(16): PostgreSQL's `bpchar` pads with trailing spaces
# ('DIVIDEND        '). VARCHAR is the correct equivalent.
#
# Named action_value rather than value for ORM/dialect portability.
#
# security_invoker = true (PG 15+, verified on 18.6): the view reads with
# the CALLER's privileges, matching MySQL's `SQL SECURITY INVOKER` intent
# -- a safer default than DEFINER.
V_ACTIONS_CREATE = """
CREATE OR REPLACE VIEW v_actions
  WITH (security_invoker = true) AS
  SELECT symbol, ex_date    AS action_date,
         CAST('DIVIDEND'     AS VARCHAR(16)) AS action_type,
         amount AS action_value FROM dividends
  UNION ALL
  SELECT symbol, split_date, CAST('SPLIT'        AS VARCHAR(16)), ratio  FROM splits
  UNION ALL
  SELECT symbol, gain_date,  CAST('CAPITAL_GAIN' AS VARCHAR(16)), amount FROM capital_gains
"""

V_ACTIONS_DROP = "DROP VIEW IF EXISTS v_actions"

# Regular-session bars only. This exists to prevent accidents, not for
# convenience: forgetting the is_extended filter mixes low-volume
# after-hours bars into the regular session and silently corrupts every
# computed indicator. This view should be the default read path.
#
# price_bars is a hypertable; chunk exclusion works on a plain view over
# it (measured: Custom Scan (ChunkAppend) with Index Cond pushed down to
# the chunk level). No continuous aggregate is needed.
V_PRICE_BARS_REGULAR_CREATE = """
CREATE OR REPLACE VIEW v_price_bars_regular
  WITH (security_invoker = true) AS
  SELECT symbol, bar_interval, ts_utc, local_date,
         open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = false
"""

V_PRICE_BARS_REGULAR_DROP = "DROP VIEW IF EXISTS v_price_bars_regular"
