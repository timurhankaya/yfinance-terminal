"""v_actions and v_price_bars_regular VIEWs.

actions is not a table; it is the union of dividends + splits + capital_gains.
"""

from __future__ import annotations

# CAST(... AS VARCHAR(16)) fixes the view's column type: a quoted literal
# resolves to `text` inside a UNION, and CHAR would pad with spaces.
# security_invoker = true: the view reads with the caller's privileges.
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

# Regular-session bars only; the default read path, since forgetting the
# is_extended filter mixes after-hours bars into every computed indicator.
# Chunk exclusion still works through a plain view over the hypertable.
V_PRICE_BARS_REGULAR_CREATE = """
CREATE OR REPLACE VIEW v_price_bars_regular
  WITH (security_invoker = true) AS
  SELECT symbol, bar_interval, ts_utc, local_date,
         open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = false
"""

V_PRICE_BARS_REGULAR_DROP = "DROP VIEW IF EXISTS v_price_bars_regular"
