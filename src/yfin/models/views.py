"""v_actions ve v_price_bars_regular VIEW'lari (PG S8).

actions tablo degildir; dividends + splits + capital_gains birlesimidir.
"""

from __future__ import annotations

# CAST(... AS VARCHAR(16)): literal uzunlugu view'in kolon tipini belirler;
# sabitlenmezse yeni bir action turu eklendiginde tip SESSIZCE degisir.
# PostgreSQL'de tirnakli literal `unknown` tipindedir ve UNION icinde
# `text`e cozulur, bu yuzden acik cast korunur.
#
# CHAR(16) KULLANILMAZ: PostgreSQL'de `bpchar`tir ve sonda BOSLUK DOLDURUR
# ('DIVIDEND        '). MySQL'de CHAR bu baglamda kirpiliyordu; VARCHAR
# dogru karsiliktir.
#
# action_value adi value'dan yeglenir (ORM/dialect tasinabilirligi).
#
# security_invoker = true (PG 15+, 18.6'da dogrulandi): view CAGIRANIN
# yetkisiyle okur. MySQL'deki `SQL SECURITY INVOKER` ile ayni niyet;
# varsayilan DEFINER davranisindan daha dogru bir varsayilandir.
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

# Yalniz normal seans barlari. Amaci kolaylik degil KAZA ONLEMEDIR:
# is_extended filtresini unutmak, seans disi dusuk hacimli barlari normal
# seansa karistirir ve bu, hesaplanan her gostergeyi sessizce bozar.
# Varsayilan okuma yolu view olmalidir (PB S5.10).
#
# price_bars artik bir HYPERTABLE'dir; duz view uzerinde chunk exclusion
# CALISIR (olculdu: Custom Scan (ChunkAppend) ve Index Cond chunk
# seviyesine iniyor). Continuous aggregate'e gerek yok (PG S7.5).
V_PRICE_BARS_REGULAR_CREATE = """
CREATE OR REPLACE VIEW v_price_bars_regular
  WITH (security_invoker = true) AS
  SELECT symbol, bar_interval, ts_utc, local_date,
         open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = false
"""

V_PRICE_BARS_REGULAR_DROP = "DROP VIEW IF EXISTS v_price_bars_regular"
