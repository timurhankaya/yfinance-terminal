"""v_actions VIEW (S5.3).

actions tablo degildir; dividends + splits + capital_gains birlesimidir.
"""

from __future__ import annotations

# CAST(... AS CHAR(16)): literal uzunlugu view'in kolon tipini belirler;
# sabitlemezsek yeni bir action turu eklendiginde tip sessizce degisir.
# action_value adi value'dan yeglenir (ORM/dialect tasinabilirligi).
# SQL SECURITY INVOKER varsayilan DEFINER'dan daha dogru bir varsayilandir.
V_ACTIONS_CREATE = """
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_actions AS
  SELECT symbol, ex_date    AS action_date,
         CAST('DIVIDEND'     AS CHAR(16)) AS action_type,
         amount AS action_value FROM dividends
  UNION ALL
  SELECT symbol, split_date, CAST('SPLIT'        AS CHAR(16)), ratio  FROM splits
  UNION ALL
  SELECT symbol, gain_date,  CAST('CAPITAL_GAIN' AS CHAR(16)), amount FROM capital_gains
"""

V_ACTIONS_DROP = "DROP VIEW IF EXISTS v_actions"

# Yalniz normal seans barlari. Amaci kolaylik degil KAZA ONLEMEDIR:
# is_extended filtresini unutmak, seans disi dusuk hacimli barlari normal
# seansa karistirir ve bu, hesaplanan her gostergeyi sessizce bozar.
# Varsayilan okuma yolu view olmalidir (PB S5.10).
V_PRICE_BARS_REGULAR_CREATE = """
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_price_bars_regular AS
  SELECT symbol, bar_interval, ts_utc, local_date,
         open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = 0
"""

V_PRICE_BARS_REGULAR_DROP = "DROP VIEW IF EXISTS v_price_bars_regular"
