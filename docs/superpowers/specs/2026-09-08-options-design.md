# Options: vade listesi ve zincir

Status: approved, implemented 2026-09-08
Date: 2026-09-08
`2026-09-07-kalan-isler.md` madde 3'ün kapanışı.

## Neden

Denetim `options` / `option_chain`'i **unutulmuş kapsam** olarak
işaretledi ve gerekçesi şuydu: bu kod tabanı her dışlamayı yazılı
gerekçelendiriyor (`sustainability`, atlanan interval'ler, `valuation`'ın
ayrı alias olması), dolayısıyla gerekçe yokluğu bir işaret. Aynı
maddedeki iki kardeşi (`earnings`, `get_shares`) upstream'de ölü çıktı ve
gerekçeleri yazıldı; bu üçüncüsü **canlı** — yfinance 1.7.0'da
`Ticker.options` ve `Ticker.option_chain()` çalışıyor ve arşivde bir
karşılığı yok.

## Ölçülen upstream şekli

Kurulu yfinance 1.7.0'ın kaynağından okundu (`ticker.py`), tahmin
edilmedi:

| Gerçek | Yer |
| --- | --- |
| Uç nokta `/v7/finance/options/{ticker}`, vade için `?date=<epoch>` | `Ticker._download_options` |
| Argümansız **tek** istek hem `expirationDates` listesini hem de **ilk vadenin** zincirini döndürüyor | aynı |
| `Ticker.options` → `%Y-%m-%d` dizelerinden oluşan tuple | `Ticker.options` |
| `option_chain(date)` → `(calls, puts, underlying)` | `Ticker.option_chain` |
| calls/puts kolonları sabit ve `reindex` ile zorlanıyor: `contractSymbol, lastTradeDate, strike, lastPrice, bid, ask, change, percentChange, volume, openInterest, impliedVolatility, inTheMoney, contractSize, currency` | `Ticker._options2df` |
| `lastTradeDate` epoch saniyeden UTC'ye çevriliyor | aynı |
| `underlying` ham `quote` sözlüğü | `Ticker._download_options` |
| Yanıt boşsa üç alan da `None` | `Ticker.option_chain` |

## Kararlar

1. **Maliyet bu tasarımın ana kısıtı.** Bir sembolün 10-20 vadesi olur ve
   her vade **ayrı bir istek**. Tam evren zaten tek IP'de ~33 saat
   (`kalan-isler.md` madde 3); vade başına bir istek daha bunu ikiyle
   çarpmaz, on beşle çarpar. Bu yüzden dataset **`opt_in=True`**:
   `all` genişlemesine hiç girmiyor, yalnız `--datasets options` ile
   çalışıyor. Emsal `sustainability`, `search`, `lookup`, `screener`.
2. **Vade sayısı ayarlanabilir ve varsayılanı dörttür.**
   `yf_option_expiries`. Bir vade hiç term structure vermez, "hepsi"
   sembol başına 10-20 istek eder; dördü haftalıklarda bir ayı,
   aylıklarda bir çeyreği kapsar. İlk istek zaten vade listesini
   getirdiği için **ilk vade bedavadır**: N vade = N istek.
3. **İki tablo, çünkü iki farklı gerçek var.** `option_expirations` o gün
   hangi vadelerin **var olduğunu** kaydeder — bir vade listeden düştüğü
   an bu bilgi başka hiçbir yerde yok. `option_quotes` zinciri tutar.
   Vade listesi zincirin özeti değil: dördü çekilse de liste yirmi
   satırdır.
4. **`option_type` bir enum'dur ve PK'nin parçasıdır.** call ve put aynı
   tabloya yazılır; kolon setleri özdeş (`_options2df` ikisini de aynı
   `reindex` ile üretiyor). Emsal `institutional_holders`'ın
   `holder_type`'ı, ve gerekçesi de aynı: özdeş kolon seti → tek tablo +
   ayırıcı ENUM.
5. **`underlying` yazılmaz.** O, sembolün fetch anındaki quote'u; arşivde
   `price_history`, `ticker_info` ve `ticker_fast_info` zaten var ve
   üçüncü bir kopya "hangisi doğru" sorusunu doğurur. Zincirin kendisi
   `lastPrice`/`bid`/`ask` taşıyor, yani satır kendi başına okunabilir.
6. **As-of, `asof_state` gate'i ile.** Zincir her gün değişir, yani gate
   pratikte hiç atlamaz; yine de doğru şekil bu: `fetched_at` "en son ne
   zaman doğrulandı" demek olmayı sürdürür. `AsOfDataset` emsali
   `institutional_holders`.
7. **Boş sonuç silmez.** `call_optional` geçici bir 404 ile gerçek bir
   "vadesi yok" durumunu ayırt edemez; `replace_scope` boş bir sonuçla
   çalıştırılsa o günün satırları geçici bir kesintide yok olurdu. Bu
   kural repoda zaten yazılı (`institutional_holders`, KNOWN LIMITATION)
   ve aynen uygulanır.

## Şema

```
option_expirations
  symbol       PK, FK symbols
  as_of_date   PK
  expiry_date  PK
  fetched_at

option_quotes
  symbol           PK, FK symbols
  as_of_date       PK
  expiry_date      PK
  option_type      PK   ENUM(call, put)
  contract_symbol  PK   OCC sembolü ('AAPL260918C00250000')
  strike, last_price, bid, ask, change, percent_change   NUMERIC
  implied_volatility                                     NUMERIC
  volume, open_interest                                  BIGINT
  in_the_money                                           BOOLEAN
  contract_size, currency                                VARCHAR
  last_trade_ts_utc                                      TIMESTAMPTZ
  fetched_at
```

**Kapsam (`replace_scope`).** `option_expirations` için
`(symbol, as_of_date)`: o gün listeden düşen bir vade satırı kalmamalı.
`option_quotes` için `(symbol, as_of_date, expiry_date)`: yalnız
çekilen vadelerin kapsamı temizlenir, çekilmeyen vadenin dünkü satırı
durur.

## Dosyalar

**Yeni:** `src/yfin/models/options.py`,
`src/yfin/datasets/options.py`, migration,
`tests/unit/test_options.py`.

**Değişen:** `src/yfin/models/__init__.py`,
`src/yfin/datasets/__init__.py`, `src/yfin/core/config.py`
(`yf_option_expiries`), `openapi.json` (iki yeni dataset yüzeyi).

## Testler

**tests/unit (saf, ağsız):** vade listesi satırları ve kapsamı; zincirin
iki tarafı tek tabloya `option_type` ile yazılıyor; kolon eşlemesi (14
kolonun hepsi); `lastTradeDate` UTC'ye çevriliyor; boş sonuç hiçbir şey
yazmıyor (silmiyor); vade sayısı ayardan okunuyor ve aşılmıyor;
`opt_in` olduğu için `all` genişlemesinde yok.

## Kapsam dışı

Greeks (Yahoo vermiyor), `underlying` kopyası (Karar 5), zincir başına
implied volatility yüzeyi türetimi (okuma tarafının işi), terminalde bir
panel (kendi turunu hak ediyor).
