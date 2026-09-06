# Canlı WebSocket akışı: ingest ve dağıtım omurgası — Tasarım

Tarih: 2026-09-06
Durum: onaylandı (brainstorming oturumu sonucu)

## 1. Amaç ve kapsam

Yahoo'nun canlı fiyat soketinden (`wss://streamer.finance.yahoo.com/?version=2`)
gelen tick'leri kesintisiz tüketen, **33 protobuf alanının tamamını**
PostgreSQL 18 + TimescaleDB'ye eksiksiz yazan ve isteğe bağlı olarak
Kafka'ya yayınlayan bir alt sistem.

Akış yapısı iki eksende kurgulanır: **sembol** (her ilişkinin anahtarı,
`symbols.symbol`'e foreign key) ve **exchange** (bağlantı izolasyonunun
ve Kafka konu düzeninin ekseni).

**Kapsam içi:** upstream WS tüketimi, bağlantı topolojisi ve dayanıklılık,
protobuf çözümleme ve tip normalizasyonu, tick arşivi + son-durum tablosu,
transactional outbox ile opsiyonel Kafka yayını, `bar_gaps` kapatma yolu,
CLI, ayarlar, test ve ölçüm.

### Kapsam dışı (ayrı alt sistem, ayrı spec)

**Dışa dönük müşteri WebSocket'i** — API istemcilerinin sembole abone
olduğu fan-out servisi: kimlik doğrulama, abonelik protokolü, plan/kota,
bağlantı başına backpressure. Bu spec onu kapsamaz.

Bugünden hazırlanan tek dokunuş: Kafka konu ve anahtar düzeni (§9) o
servisin de doğal veri kaynağı olacak şekilde seçilir — fan-out servisi
`live_ticks` tablosunu poll etmek zorunda kalmaz.

Ayrıca kapsam dışı: tick'lerden türev gösterge hesabı, tick verisinin
API üzerinden okunması (mevcut salt-okuma API spec'inin genişletmesidir),
retention/compression politikası (§14'te açık madde).

## 2. Mimari kararlar

### K1 — Ayrı süreç; async yalnızca I/O sınırında

`yfin stream run` uzun ömürlü, ayrı bir süreçtir. İçinde tek bir asyncio
event loop **yalnızca** WS bağlantılarını ve protobuf çözümlemesini
yürütür. Çözülmüş tick'ler sınırlı bir `queue.Queue`'ya düşer; ayrı bir
**writer thread** mevcut senkron `PostgresRowWriter` ve `TableWrite`
sözleşmesiyle batch yazar.

Gerekçe: salt-okuma API spec'inin K1 kararı (tek senkron idiom, `mypy
--strict`) korunur. Async biçem `stream/` paketinin dışına sızmaz ve
depoda ikinci bir yazma/doğrulama yolu doğmaz — `_verify()`'ın
key-existence sorgusu tick satırları için de olduğu gibi çalışır, yani
"her yazma okunarak doğrulanır" güvencesi canlı akışta da geçerlidir.

Reddedilen:

- **Uçtan uca async (asyncpg / async SQLAlchemy).** En yüksek verim, ama
  kalıcı ikinci bir idiom ve ikinci bir yazma yolu. Darboğaz bugün
  bilinmiyor; §13'ün 0. aşamasındaki ölçüm onu belirlemeden bu bedel ödenmez.
- **API sürecine gömme.** FastAPI/uvicorn zaten bir event loop
  çalıştırıyor, ingest onun lifespan'ına bağlanabilirdi. İki farklı yük
  profili tek deploy'a karışır ve API'nin her yeniden başlatılışı veri
  toplamayı keser — canlı akışta kaybedilen veri geri gelmez.
- **Bağlantı başına thread (asyncio yok).** Kod tabanıyla tek deyim
  olurdu, ama N bağlantı = N thread ve yeniden bağlanma/heartbeat mantığı
  her thread'de ayrı ayrı yürür.

### K2 — `yfinance`'ın WebSocket sınıfları kullanılmaz; yalnız `pricing_pb2` alınır

`stream/connection.py` doğrudan `websockets.asyncio.client.connect`
kullanır. `yfinance`'tan ithal edilen tek şey
`yfinance.pricing_pb2.PricingData` mesaj sınıfıdır.

Bu, projenin "Yahoo ile konuşmayı yfinance yapar" ilkesinden bilinçli bir
sapmadır ve üç ölçülmüş nedene dayanır (yfinance 1.7.0, `live.py`):

1. **`AsyncWebSocket`'in reconnect'i ölü koddur.** `listen()`'in `except`
   dalı 3 saniye bekleyip `await self._connect()` çağırır; ama
   `_connect()` yalnızca `self._ws is None` iken bağlanır ve hata yolunda
   `_ws` hiçbir zaman `None`'a çekilmez (yalnız *bağlanma* başarısızlığında
   çekilir). Sonuç: kapalı soket üzerinde 3 saniyede bir dönen sonsuz hata
   döngüsü. Yeniden bağlanma hiç gerçekleşmez.
2. **`WebSocket` (senkron) hatada sessizce durur.** `listen()`'in genel
   `except` dalı `break` eder; döngüden çıkar, hiçbir şey bildirmez.
3. **Global duruma bağımlılık.** `verbose=True` varsayılanı `print()`
   yapar; istisnaların yutulup yutulmayacağı
   `YfConfig.debug.hide_exceptions` genel bayrağına bağlıdır. Bir üretim
   ingest süreci davranışını kütüphane genelinde bir hata ayıklama
   bayrağına bağlayamaz.

"Çökmeyecek akış" şartı bu üç madde varken upstream'e bırakılamaz.
Protobuf şeması ise upstream'de kalır: `pricing.proto` değişirse
`pricing_pb2` ile birlikte gelir ve §12.1'deki statik alan testi bunu
yakalar.

`websockets` bugün yfinance üzerinden transitif olarak geliyor;
`pyproject.toml`'a **açıkça** eklenir — `curl_cffi` için kurulmuş olan
kalıbın aynısı, aynı gerekçeyle: doğrudan kullanılan bir bağımlılığı
transitif bırakmak kırılgandır.

### K3 — Exchange'e göre gruplanmış, tavanlı bağlantı havuzu

Semboller `symbols.exchange` ile gruplanır, her grup bir bağlantı alır ve
toplam bağlantı sayısı bir tavanla sınırlanır (§4).

Reddedilen:

- **Tek bağlantı, tüm semboller.** Tek arıza noktası: bir exchange'in
  yol açtığı bir kopma bütün evreni durdurur. Ayrıca 15 saniyede bir
  gönderilen abonelik heartbeat'i binlerce sembollük tek bir mesaja döner.
- **Tavansız exchange-başına-bağlantı.** Bağlantı sayısı veriden gelirdi;
  evren büyüdükçe Yahoo tarafındaki bilinmeyen bir eşzamanlı bağlantı
  limitine kontrolsüz şekilde çarpılır.

### K4 — Akış kapsamı bir tablodur, ayar değil

Hangi sembollerin stream edileceği ve hangilerinin arşive gireceği
`stream_scope` tablosunda tutulur. Gerekçe `intraday_scope`'unkinin
aynısıdır: 5.000 sembollük bir evrende bir alt küme `.env`'e sığmaz ve
sürümlenebilir, değiştirilebilir olması gerekir. Bu veridir,
konfigürasyon değil.

### K5 — Kafka opsiyoneldir ve maliyetini yalnızca kullanan öder

`yf_kafka_enabled = false` iken `stream_outbox`'a **hiçbir satır
yazılmaz**; yazma amplifikasyonu tam olarak sıfırdır. Açıkken outbox
satırı tick ile **aynı transaction'da** yazılır: at-least-once, sıralı ve
veritabanıyla atomik.

**Kabul edilen bedel, açıkça:** outbox yılda ~1,5 milyar satırlık ikinci
bir yazma yoludur (§3.1'deki hacim tahmini). Bunu tolere edilebilir kılan
tek şey temizlik yönteminin `DELETE` olmamasıdır: `stream_outbox` da bir
hypertable'dır ve relay, tamamı yayımlanmış chunk'ları `drop_chunks` ile
düşürür. Bu hacimde bir yayınlama kuyruğunu `DELETE` + autovacuum ile
ayakta tutmak mümkün değildir.

Reddedilen:

- **`live_ticks`'i log olarak tail eden relay (outbox tablosu yok).**
  Yazma amplifikasyonu sıfır olurdu, ama iki somut nedenle kapsamı
  yanlıştır. Birincisi: `archive = false` olan semboller (§6.4)
  `live_ticks`'e hiç yazılmaz, dolayısıyla Kafka'ya da hiç ulaşmazlardı —
  oysa o sembollerin canlı fiyatı tam da tüketiciye akması gereken
  veridir. İkincisi: `live_ticks` ileride retention/compression'ın ilk
  adayıdır (§3.2); tail edilen kaynağın altından bir retention
  politikasının kayması, henüz yayımlanmamış satırların sessizce yok
  olması demektir. Outbox ise yalnızca yayımlanmamışı tutar ve
  retention'dan bağımsızdır.

  Not: her iki modelin de doğruluğu aynı invaryanta dayanır — **tek writer
  thread**, dolayısıyla `IDENTITY` sırası commit sırasıdır (§8.2). Bu,
  outbox'u seçmekle ortadan kalkan bir varsayım değildir; açıkça yazılan
  ve testle korunan bir invaryanttır.
- **Doğrudan producer (DB commit'inden sonra publish).** En ucuz yol, ama
  Kafka düşükken üretilen tick'ler veritabanına yazılır ve konuya hiç
  girmez — tüketici için kalıcı, telafisi olmayan boşluk.
- **Önce Kafka, sonra DB.** En düşük gecikme, ama Kafka fiilen zorunlu
  hale gelir ve "opsiyonel Kafka" şartı bozulur.

### K6 — Tick arşivi ile bar arşivi ayrıdır; tek köprü `bar_gaps`

`price_bars`'ın otoritesi Yahoo'nun bar endpoint'i olarak kalır. Tick'ler
`price_bars`'a **yalnızca** açık bir `bar_gaps` satırının penceresinde 1m
bar türetmek için yazılır ve satır `derived_from_ticks` gerekçesiyle
kapatılır.

Gerekçe: tick'ler yalnızca aksi halde sonsuza kadar boş kalacak
pencereleri doldurur. Yahoo 1m verisini 29 gün sonra düşürür; o pencerede
kaçırılan bir çekimin telafisi yoktur — elimizde tick varken o boşluğu
boş bırakmak bilgi imha etmektir. Buna karşılık her tick'ten bar türetip
arşive yazmak arşivin otoritesini karıştırır ve `bar_rescales` ledger'ını
iki kaynaklı satırları ayırt etmek zorunda bırakırdı.

Reddedilen: (a) hiç mutabakat yok — yukarıdaki telafi kaybı; (b) ayrı bir
`live_bars` tablosu — üçüncü bir bar tablosu ve "hangisi doğru" sorusu;
(c) `price_bars`'a `source` kolonu — arşivin otoritesi karışır.

## 3. Hacim ve maliyet

### 3.1 Büyüklük tahmini

Kaba tahmin, **ölçülmüş değil** (§13'ün 0. aşaması bunu ölçer):

| Girdi | Değer |
|---|---|
| Akıştaki sembol | 500 |
| Ortalama mesaj hızı | ~0,5 mesaj/sn/sembol |
| Seans süresi | 6,5 saat |
| **Satır/gün** | **~5,8 M** |
| **Satır/yıl** | **~1,5 milyar** |
| **Boyut/yıl** (~200 bayt/satır) | **~300 GB** |

Karşılaştırma için: `docs/measurements/volume.md`'deki tüm bar arşivi ilk
yıl sonunda ~464 M satır ve ~64 GB'dır. Yani canlı tick arşivi, tek
başına, mevcut bar arşivinin yaklaşık **beş katıdır**.

Bu, `stream_scope`'un (K4) neden opt-in bir tablo olduğunun tek nedenidir:
akış evreni operatörün açık kararıdır, varsayılan olarak boştur.

`yf_kafka_enabled` açıkken bu rakamlar `stream_outbox` için ikinci kez
ödenir; chunk'lar yayımlandıkça düşürüldüğü için kalıcı boyut değil,
anlık kuyruk derinliği kadar yer tutar.

### 3.2 Bu spec'in tetiklediği açık maliyet kararı

`price_bars` ve `price_history` için compression/retention politikası
bugün **bilerek** kapalıdır (rescale yolu geçmiş satırları yeniden
yazıyor, önce ölçüm gerekiyor). `live_ticks` bu kararı zorlamaz, çünkü
tick satırları hiçbir zaman yeniden yazılmaz — append-only'dir ve rescale
ona dokunmaz. Dolayısıyla `live_ticks` compression/retention için doğal
ilk adaydır, ama bu spec'in kapsamı dışındadır (§14).

## 4. Bağlantı topolojisi

### 4.1 Sembol → bağlantı eşlemesi

1. **Abonelik evreni:** `stream_scope.enabled = true` olan semboller.
2. **Gruplama:** `symbols.exchange` değerine göre. `NULL` veya boş
   exchange `__unknown__` grubuna düşer.
3. **Bölme:** bir grup `yf_stream_max_symbols_per_connection`'ı
   (varsayılan 250) aşarsa aynı exchange birden çok bağlantıya bölünür ve
   bağlantılar `NMS#0`, `NMS#1` diye adlandırılır.
4. **Birleştirme:** grup sayısı `yf_stream_max_connections`'ı (varsayılan
   8) aşarsa küçük gruplar deterministik olarak birleştirilir:
   `blake2b(exchange) mod tavan`.

Dördüncü adımda `hash()` **kullanılmaz**: PYTHONHASHSEED süreçler arasında
değişir, dolayısıyla `hash()` tabanlı bir eşleme yeniden başlatmalar
arasında farklı bağlantı düzeni üretirdi. `pipeline/shard.py` aynı
nedenle aynı seçimi yapıyor.

Bu dört adım, iki uç durumu tek kuralla kapsar: exchange sayısı tavanın
altındaysa yapı "exchange başına bir bağlantı"ya dejenere olur; üstündeyse
tavan korunur ve aynı exchange'in sembolleri her zaman bir arada kalır.

### 4.2 Çökmeme garantisi nereden geliyor

Bir bağlantının ölümü **yalnızca kendi sembol kümesini** etkiler.
Supervisor onu backoff ile yeniden kurarken diğer bağlantılar akmaya devam
eder. Supervisor hiçbir bağlantı istisnasını yukarı taşımaz: her bağlantı
görevi kendi `try` sınırındadır, ölümü `stream_connection_health`'e
yazılır ve yeniden başlatılır. Supervisor'ın kendisinin çıkması yalnızca
SIGTERM ile mümkündür.

Üç savunma katmanı, üçü farklı arıza tipine karşı:

| Katman | Ayar | Yakaladığı arıza |
|---|---|---|
| `websockets` ping/pong | `ping_interval=20`, `ping_timeout=20` | TCP yaşıyor, karşı uç ölü |
| Idle watchdog | `yf_stream_idle_timeout_seconds` (300) | Bağlantı sağlıklı görünüyor ama seans açıkken hiç mesaj gelmiyor |
| Abonelik heartbeat | 15 sn (upstream davranışı) | Yahoo tarafında sessizce düşmüş abonelik |

Idle watchdog'un tetiklenme koşulu dikkatle seçilmiştir: bağlantı `open`
durumunda, `last_message_at` **dolu**, ve o andan bu yana
`yf_stream_idle_timeout_seconds` geçmiş olmalıdır.

`last_message_at`'in dolu olması şartı, kapalı borsayı piyasa takvimine
bakmadan halleder. Hiç mesaj almamış bir bağlantı — açılış öncesi kurulan
bağlantı, ya da hafta sonu — watchdog tarafından hiçbir zaman yeniden
kurulmaz. Seans bittiğinde ise watchdog bir kez tetiklenir; yeniden kurulan
bağlantının `last_message_at`'i `NULL`'dır, dolayısıyla ikinci kez
tetiklenmez. Kapalı borsanın toplam maliyeti seans başına tek bir gereksiz
yeniden bağlanmadır.

Alternatif — `symbols.timezone` ve `calendar_*` verisinden seans penceresi
hesaplamak — reddedildi: watchdog'un doğruluğunu, kendisi de bu hattan
gelen ve eksik olabilen bir veriye bağlardı.

Yeniden bağlanma: full-jitter üstel backoff, 1 sn'den başlar,
`yf_stream_reconnect_max_seconds` (60) tavanına kadar çıkar, sonsuz
denenir. Vazgeçme yoktur — vazgeçmek sessiz veri kaybıdır.

### 4.3 Süreç tekliği

`yfin stream run` başlarken `yfin_stream` adlı PostgreSQL advisory lock'unu
alır (`storage/db.py`'deki mevcut mekanizma, `_lock_key` ile). İki kopya
aynı anda yazamaz. Ad `yfin_sync`'ten ayrıdır, dolayısıyla akış süreci
zamanlanmış sync ile çakışmaz ve ikisi aynı anda koşabilir.

### 4.4 Yeniden dengeleme

Supervisor `stream_scope`'u ve ayarları `yf_stream_rescan_seconds` (60)
aralığıyla yeniden okur. Değişiklik uygulaması cerrahidir:

- Sembol eklendi/çıkarıldı → yalnızca ilgili bağlantı `subscribe` /
  `unsubscribe` mesajı gönderir; bağlantı kesilmez.
- Yeni bir exchange geldi → yalnızca yeni bağlantı kurulur.
- Tavan veya bölme sınırı değişti → yalnızca yeniden eşlemesi değişen
  semboller taşınır.

Topoloji değişikliği hiçbir zaman "hepsini kapat, yeniden kur" değildir:
bu, ayarda yapılan küçük bir değişikliğin tüm akışı saniyelerce
kesmesi demek olurdu.

## 5. Paket düzeni

```
src/yfin/stream/
  __init__.py
  protocol.py     PricingData -> tick dict. 33 alanın kolon haritası,
                  float32 -> Decimal, ms/sn -> ts_utc, presence kuralı.
                  `yfinance`'a bağımlı TEK modül; SQLAlchemy import etmez.
  connection.py   Tek bağlantının yaşam döngüsü: connect, subscribe,
                  heartbeat, ping/pong, idle watchdog, backoff.
  supervisor.py   Bağlantı kümesi, sembol -> bağlantı eşlemesi,
                  yeniden dengeleme, sağlık kaydı.
  writer.py       Senkron writer thread: batch -> TableWrite -> RowWriter.
                  Bilinmeyen sembol süzgeci, reject kaydı, oturum sayaçları.
  relay.py        stream_outbox -> Kafka; offset ve drop_chunks.
  kafka.py        Opsiyonel producer sarmalayıcı (import guard).
  reconcile.py    Açık bar_gaps -> tick'ten 1m bar türetimi.
src/yfin/models/stream.py
src/yfin/cli/stream.py
```

Katman kuralı, mevcut dilin aynısı:

- `stream/` yalnızca `storage/contracts.py`'ye bağımlıdır, `storage/db.py`
  ve `datasets/`'e değil (`writer.py` kendisine verilen `RowWriter`'ı
  kullanır, kendi engine'ini kurmaz).
- `protocol.py` SQLAlchemy import etmez ve veritabanı bilmez.
- `connection.py` ve `supervisor.py` veritabanı bilmez; sağlık kayıtlarını
  bir callback üzerinden yazar.
- `cli/stream.py` sorgu yazmaz.

CLI giriş noktası `app.py`'ye `app.add_typer(stream_app, name="stream")`
ile bağlanır. `cli/bars.py`'nin `cli/app.py` ile arasındaki mevcut dairesel
import bu pakette tekrarlanmaz: `cli/stream.py` kendi session factory'sini
kurar.

## 6. Veri modeli

Sekiz tablo. `symbol` kolonu her yerde `symbols.symbol`'e foreign key'dir
(`underlying_symbol` hariç, §6.2) — ilişkiler sembol kodu üzerinden
kurulur.

### 6.1 `live_ticks` — hypertable

```
create_hypertable('live_ticks', by_range('ts_utc', INTERVAL '1 day'),
                  create_default_indexes => FALSE)
```

`create_default_indexes => FALSE`, `price_bars` ile aynı gerekçeyle
zorunludur: varsayılan indeks `Base.metadata`'da bulunmadığı için
`alembic revision --autogenerate` onu sonsuza kadar "düşürülmeli" diye
raporlar ve "boş diff" kapısı bir daha açılmaz.

Chunk aralığı `price_bars`'ın 7 gününden farklı olarak 1 gündür: günlük
~5,8 M satırda 7 günlük chunk ~40 M satıra ulaşır ve chunk başına indeks
çalışma kümesi belleğe sığmaz.

**Birincil anahtar: `(symbol, ts_utc, payload_hash)`**

Üçlü olmasının nedeni iki ayrı gerçektir:

- Yahoo aynı milisaniye içinde aynı sembol için birden çok mesaj
  gönderebilir; `(symbol, ts_utc)` çakışır ve ikinci tick sessizce
  kaybolurdu.
- Yahoo aynı mesajı tekrar gönderebilir (özellikle 15 saniyelik abonelik
  heartbeat'inden sonra gelen anlık görüntülerde). `payload_hash` —
  33 alanın kanonik gösteriminin SHA-256'sının ilk 16 hanesi, mevcut
  `ShortHashType()` — bunu `ON CONFLICT DO NOTHING` ile eler.

Böylece aynı milisaniyedeki *farklı* iki tick'in ikisi de saklanır, aynı
tick'in ikinci kopyası saklanmaz. Eksiksizlik ve tekrarsızlık tek anahtarla
sağlanır.

Hypertable'da her benzersiz kısıt partition kolonunu içermek zorundadır;
`ts_utc` PK'nin bileşeni olduğu için bu koşul sağlanır.

### 6.2 33 protobuf alanının kolon haritası

Hiçbir alan düşmez. `pricing.proto`'daki sıra korunur.

| # | proto alanı | proto tipi | kolon | SQL tipi |
|---|---|---|---|---|
| 1 | `id` | string | `symbol` | `SymbolType()`, FK, **PK** |
| 2 | `price` | float | `price` | `PriceType()` |
| 3 | `time` | sint64 | `ts_utc` | `TsType()`, **PK** |
| 4 | `currency` | string | `currency` | `VARCHAR(32) C` |
| 5 | `exchange` | string | `exchange` | `VARCHAR(32) C` |
| 6 | `quote_type` | int32 | `quote_type_code` | `SmallInteger` |
| 7 | `market_hours` | int32 | `market_hours_code` | `SmallInteger` |
| 8 | `change_percent` | float | `change_percent` | `PriceType()` |
| 9 | `day_volume` | sint64 | `day_volume` | `BigInteger` |
| 10 | `day_high` | float | `day_high` | `PriceType()` |
| 11 | `day_low` | float | `day_low` | `PriceType()` |
| 12 | `change` | float | `change` | `PriceType()` |
| 13 | `short_name` | string | `short_name` | `VARCHAR(128) C` |
| 14 | `expire_date` | sint64 | `expire_date_utc` | `TsType()` |
| 15 | `open_price` | float | `open_price` | `PriceType()` |
| 16 | `previous_close` | float | `previous_close` | `PriceType()` |
| 17 | `strike_price` | float | `strike_price` | `PriceType()` |
| 18 | `underlying_symbol` | string | `underlying_symbol` | `SymbolType()`, **FK yok** |
| 19 | `open_interest` | sint64 | `open_interest` | `BigInteger` |
| 20 | `options_type` | sint64 | `options_type_code` | `SmallInteger` |
| 21 | `mini_option` | sint64 | `mini_option_code` | `SmallInteger` |
| 22 | `last_size` | sint64 | `last_size` | `BigInteger` |
| 23 | `bid` | float | `bid` | `PriceType()` |
| 24 | `bid_size` | sint64 | `bid_size` | `BigInteger` |
| 25 | `ask` | float | `ask` | `PriceType()` |
| 26 | `ask_size` | sint64 | `ask_size` | `BigInteger` |
| 27 | `price_hint` | sint64 | `price_hint` | `SmallInteger` |
| 28 | `vol_24hr` | sint64 | `vol_24hr` | `BigInteger` |
| 29 | `vol_all_currencies` | sint64 | `vol_all_currencies` | `BigInteger` |
| 30 | `from_currency` | string | `from_currency` | `VARCHAR(32) C` |
| 31 | `last_market` | string | `last_market` | `VARCHAR(64) C` |
| 32 | `circulating_supply` | double | `circulating_supply` | `FactValueType()` |
| 33 | `market_cap` | double | `market_cap` | `BigNumType()` |

Ek kolonlar:

| kolon | tip | anlam |
|---|---|---|
| `payload_hash` | `ShortHashType()`, **PK** | 33 alanın kanonik gösteriminin SHA-256'sının ilk 16 hanesi |
| `received_at` | `TsType()`, NOT NULL | Çözümleme anı. `received_at - ts_utc` uçtan uca gecikmedir |
| `unknown_fields` | `Text` NULL | Haritada bulunmayan proto alanları, JSON metni |

Üç seçim gerekçe ister:

**`underlying_symbol` FK taşımaz.** Bir opsiyonun dayanak varlığı `symbols`
evreninde olmayabilir; FK koymak, sırf dayanağı bilinmeyen diye geçerli bir
opsiyon tick'ini reddetmek olurdu. `SymbolType()` yine kullanılır, çünkü
genişlik uyuşmazlığı ileride bir JOIN'i bozar.

**`quote_type` ve `market_hours` kod olarak saklanır.** İkisi de proto'da
`int32`'dir, yani Yahoo'nun enum'ları. Kolon adına `_code` soneki eklenir
ki `symbols.quote_type`'ın (metin) karşılığı sanılmasın. Kod → ad eşlemesi
Python'da bir sabittir; veritabanında ikinci bir doğruluk kaynağı
yaratılmaz — `BAR_INTERVALS`'ın enum yapılmama gerekçesinin aynısı.

**`raw_json` yoktur, ve bu bilinçlidir.** Projenin snapshot dataset'leri
ham gövdeyi saklar; burada saklamaz, çünkü 33 alanın tamamı zaten
kolonlanmıştır ve ham gövde hiçbir bilgi eklemez. Buna karşılık maliyeti
yılda ~1,5 milyar satır × ~400 bayttır. "Şemanın gerisinde kalma" riski —
yani `pricing.proto`'ya yeni bir alan eklenmesi — `unknown_fields` ile
kapatılır: normal durumda `NULL`, maliyeti sıfır, ve §12.1'deki statik alan
testi yeni alanı test zamanında zaten yakalar.

İndeksler: PK dışında `ix_live_ticks_received_at` (gecikme sorguları ve
operasyonel inceleme için). `symbol` üzerinde ayrı indeks gerekmez — PK'nin
ilk bileşenidir.

### 6.3 `live_quotes` — sembol başına tek satır

`live_ticks` ile aynı alan kümesi, artı `updated_at`. PK yalnızca `symbol`.
Hypertable değildir: satır sayısı akıştaki sembol sayısı kadar sabittir.

**Geriye sarma koruması zorunludur.** Yeniden bağlanma sonrası gelen anlık
görüntüler veya sıra dışı teslim, eski bir tick'in son durumu bozmasına yol
açabilir. Bu, `TableWrite` sözleşmesine küçük bir ekleme gerektirir:

```python
# storage/contracts.py, TableWrite
guard_column: str | None = None
```

Anlamı: `ON CONFLICT ... DO UPDATE ... WHERE excluded.<guard> > <table>.<guard>`.
`live_quotes` için `guard_column = "ts_utc"`.

Mevcut `monotonic_columns` bu işi göremez: o kolon bazlı `GREATEST(current,
new)` uygular. Burada gereken satır bazlı bir koşuldur — eski bir tick
geldiğinde *hiçbir* kolon güncellenmemelidir, yoksa satır iki farklı zamanın
alanlarını karıştıran, hiçbir zaman var olmamış bir duruma dönüşür.

`PostgresRowWriter._insert_stmt` bu bayrağı `on_conflict_do_update(...,
where=...)` ile karşılar. `_verify()` etkilenmez: doğrulama sorgusu anahtar
varlığını sayar, güncelleme yapılıp yapılmadığını değil.

### 6.4 `stream_scope`

| kolon | tip | anlam |
|---|---|---|
| `symbol` | `SymbolType()`, PK, FK | |
| `enabled` | `Boolean` NOT NULL, default `true` | Abonelik evrenine dahil mi |
| `archive` | `Boolean` NOT NULL, default `true` | `false` ise sembol yalnızca `live_quotes`'a yazılır, `live_ticks`'e yazılmaz |
| `added_at` | `TsType()` NOT NULL | |
| `note` | `VARCHAR(255) C` NULL | |

`archive = false`, hacim kontrolünün ince ayarıdır: bir sembolün canlı
fiyatı gerekiyor ama tick tarihi gerekmiyorsa maliyet sıfıra iner. Yeni
satırların varsayılanı `yf_stream_archive_default` (varsayılan `true`)
ayarından gelir.

### 6.5 `stream_outbox` — hypertable

```
create_hypertable('stream_outbox', by_range('created_at', INTERVAL '1 hour'),
                  create_default_indexes => FALSE)
```

| kolon | tip |
|---|---|
| `id` | `BigInteger`, `Identity()`, NOT NULL |
| `created_at` | `TsType()` NOT NULL, partition kolonu |
| `symbol` | `SymbolType()` NOT NULL (FK yok, §6.7 ile aynı gerekçe) |
| `exchange` | `VARCHAR(32) C` NULL |
| `payload` | `Text` NOT NULL — JSON metni |

PK `(created_at, id)`. İndeks: `ix_stream_outbox_id` (`id`), relay'in
`id > offset` taraması için.

Yalnızca `yf_kafka_enabled` iken yazılır. Bir saatlik chunk aralığı,
`drop_chunks`'ın makul bir granülarite ile çalışmasını sağlar: relay birkaç
saniye geride kalsa bile bir saatlik chunk'ın tamamı yayımlandığında
düşürülebilir.

`payload`, relay'in yeniden serileştirme yapmaması için Kafka'ya
gönderilecek gövdenin **birebir kendisidir**.

### 6.6 `stream_relay_offset`

Tek satırlık tablo: `id` (PK, sabit `1`), `last_published_id`
(`BigInteger` NOT NULL), `updated_at` (`TsType()` NOT NULL).

Tek satır olması `CHECK (id = 1)` ile zorlanır: ikinci bir offset satırı,
iki relay'in birbirinin ilerlemesini görmemesi demektir.

### 6.7 `stream_rejects`

Düşen her şeyin kalıcı kaydı. `bar_gaps` felsefesinin birebir aynısı: bir
tick'in düştüğü bilgisi o anda yazılmazsa sonsuza kadar kaybolur, ve
sonradan "burada tick var mıydı" sorusunun cevabı yoktur.

| kolon | tip |
|---|---|
| `id` | `BigInteger`, `Identity()`, PK |
| `received_at` | `TsType()` NOT NULL |
| `symbol` | `SymbolType()` NULL — **FK yok** |
| `reason` | `AsciiKeyType(24)` NOT NULL |
| `detail` | `Text` NULL |
| `raw_base64` | `Text` NULL |

FK yoktur ve bu zorunludur: `unknown_symbol` reddi, tanımı gereği
`symbols`'ta olmayan bir sembol içindir; FK olsaydı reddin kaydı da FK
ihlaliyle reddedilirdi. `sync_run_items.symbol`'ün FK taşımama gerekçesinin
aynısı.

`reason` değerleri:

| değer | anlam |
|---|---|
| `decode_failed` | Base64 veya protobuf çözümlemesi başarısız. `raw_base64` doldurulur |
| `no_timestamp` | `time <= 0`; PK bileşeni uydurulamaz |
| `unknown_symbol` | `symbols` evreninde yok |
| `queue_overflow` | Kuyruk dolu, tick düşürüldü (§8) |
| `non_finite_field` | NaN/Inf bir alan; `detail` alan adını taşır. Satır yazılır, alan NULL kalır |
| `expire_date_range` | `expire_date` makul aralığın dışında; alan NULL kalır |

Bozuk bir akışın tabloyu doldurmaması için `(symbol, reason)` başına saatte
`yf_stream_reject_sample_per_hour` (100) satır **örneklenir**. Sayaçlar
örneklenmez: reddedilenlerin tam sayısı `stream_sessions`'a yazılır.
Örnekleme, bilgiyi değil yalnızca tekrarını kırpar.

### 6.8 `stream_sessions`

`sync_runs`'ın akış karşılığı. Süreç başlarken bir satır açılır, çıkarken
kapatılır.

| kolon | tip |
|---|---|
| `id` | `BigInteger`, `Identity()`, PK |
| `started_at`, `finished_at` | `TsType()` |
| `status` | `Enum('running','ok','failed', name='stream_status')` |
| `connection_count`, `symbol_count` | `Integer` |
| `messages_received`, `rows_written`, `rows_rejected`, `rows_dropped` | `BigInteger` |

Sayaçlar writer thread'inde biriktirilir ve batch commit'iyle birlikte
güncellenir; her mesajda `UPDATE` atmak yazma yolunu ikiye katlardı.

### 6.9 `stream_connection_health`

`yfin stream status`'un kaynağı. Canlı durum tablosudur, tarih tutmaz.

| kolon | tip |
|---|---|
| `connection_key` | `AsciiKeyType(40)`, PK — `NMS#0` |
| `state` | `AsciiKeyType(16)` — `connecting`, `open`, `reconnecting`, `closed` |
| `symbol_count` | `Integer` |
| `connected_at`, `last_message_at` | `TsType()` NULL |
| `reconnect_count` | `Integer` NOT NULL default 0 |
| `last_error` | `Text` NULL |

## 7. Protokol çözümlemesi ve doğruluk

### 7.1 float32 → Decimal

`pricing.proto`'daki her fiyat alanı `float`, yani IEEE 754 binary32'dir.
Protobuf bunu Python `float`'a (binary64) genişletir: `232.35` olarak
yollanan bir fiyat `232.35000610351562` olarak okunur. `PriceType()` =
`NUMERIC(28,12)` bu artığı sadakatle saklar ve arşiv kalıcı olarak
kirlenir. "Hatasız veri" şartı tam burada kazanılır ya da kaybedilir.

```python
def f32_decimal(value: float) -> Decimal:
    """float32'ye geri döndüğünde aynı biti veren EN KISA ondalık gösterim."""
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == value:
            return Decimal(text)
    return Decimal(repr(value))  # ulaşılamaz: IEEE 754 binary32 garantisi 9 hane
```

Dokuz basamak tavanı keyfi değildir: binary32'nin round-trip garantisi 9
anlamlı hanedir, dolayısıyla döngü her zaman sonlanır. `repr(value)`
dönüşü savunma amaçlıdır ve ulaşılması beklenmez.

`price_hint` yuvarlama için **kullanılmaz**, yalnızca saklanır. O Yahoo'nun
*gösterim* önerisidir, veri değil; yuvarlamayı ona bağlamak, hint'in yanlış
geldiği bir sembolde arşive kalıcı bir hata yazmak olurdu.

`double` alanlar (`circulating_supply`, `market_cap`) bu işlemden geçmez:
onlar zaten binary64'tür ve genişletme artığı yoktur.

NaN/Inf: alan `NULL` yazılır, satırın geri kalanı yazılır,
`stream_rejects`'e `non_finite_field` + alan adı düşer. Satırın tamamını
atmak, tek bozuk alan yüzünden 32 sağlam alanı imha etmek olurdu.

### 7.2 Presence: 0 ile "gönderilmedi" ayırt edilemez

proto3'te `optional` işaretlenmemiş scalar alanların **field presence**'ı
yoktur. Üç yol da aynı duvara çarpar:

- `HasField('bid')` proto3 scalar için `ValueError` atar.
- `ListFields()` yalnızca varsayılan-olmayan alanları döndürür.
- `MessageToDict` varsayılan değerli alanları atlar.

Çünkü wire üzerinde **`bid = 0.0` ile `bid`'in hiç yollanmamış olması aynı
bayt dizisidir.** Bu protokolün özelliğidir; kod tarafında çözülemez.

**Karar:** varsayılan değerli alan `NULL` yazılır. Bu belgelenmiş bir
kayıptır, gizlenen bir hata değil:

> `live_ticks.bid IS NULL`, "bid yok" demek değildir; **"bid ya yok ya da
> sıfır"** demektir. Protokol bu ayrımı taşımaz. API ve Kafka
> tüketicilerinin bunu bilmesi gerekir.

İstisna yoktur, `price` dahil. `price`'ı `NULL` olan bir tick (örneğin
yalnız bid/ask güncellemesi) yine de yazılır — bilgi taşır.

`MessageToDict` kullanılmaz; `protocol.py` `PricingData` nesnesini doğrudan
okur. Gerekçe: `preserving_proto_field_name` bayrağının davranışı kütüphane
sürümüne bağlıdır, alan → kolon haritamız zaten açıktır, ve araya bir
dönüşüm katmanı daha koymanın hiçbir kazancı yoktur.

### 7.3 Zaman

`time` alanı `sint64`, **milisaniye** epoch'tur:
`ts_utc = datetime.fromtimestamp(time / 1000, UTC)`.

`time <= 0` ise satır yazılmaz ve `stream_rejects`'e `no_timestamp` düşer.
`ts_utc` PK'nin bileşenidir; `received_at` ile ikame etmek, veriyi
uydurmak olurdu.

`expire_date` alanı da `sint64`'tür ama **saniye** epoch'u taşır (opsiyon
vadesi). Bu asimetri §13'ün 0. aşamasında ölçülür; iki birimi karıştırmak her vadeyi
1970'e taşır. Ölçüm kesinleşene kadar savunma: çözülen değer 1970–2100
aralığının dışındaysa `expire_date_utc` `NULL` bırakılır ve
`expire_date_range` reddi kaydedilir.

`received_at` çözümleme anında yazılır. `received_at - ts_utc` uçtan uca
gecikmedir ve `yfin stream status` p50/p95'ini gösterir; bu, akışın
sağlıklı olup olmadığının tek gerçek göstergesidir (mesaj sayısı değil).

### 7.4 Bilinmeyen sembol

`live_ticks.symbol` FK taşır, dolayısıyla evrende olmayan tek bir sembol
tüm batch'i patlatır — 500 sağlam tick, bir tanesi yüzünden yazılmaz.

Writer, batch'i yazmadan önce mevcut `SymbolLookup.known_symbols()`
protokolüyle süzer; bilinmeyenler `stream_rejects`'e `unknown_symbol` ile
gider. Sonuç 60 saniye TTL'li bir sette önbelleklenir: batch başına bir
sorgu atmak yazma yolunu ikiye katlardı.

Bu, `ItemStatus.UNKNOWN_SYMBOL`'ün akıştaki karşılığıdır — aynı ayrım, aynı
gerekçe: sembolün bilinmemesi bir hata değil, kaydedilmesi gereken bir
olgudur.

## 8. Backpressure ve batch

Kuyruk: `queue.Queue(maxsize=yf_stream_queue_maxsize)`, varsayılan 50.000.
Event loop tarafı `put_nowait`, writer thread tarafı `get`.

### 8.1 Kuyruk dolduğunda

Üç seçenek vardır; üçüncüsü seçilir.

1. **Bloke ol.** `pipeline/runner.py`'ın yaptığı budur (`results.put()`
   dolu kuyrukta bloke eder, worker yeni sembol çekmez). Orada doğrudur:
   üretici bizim worker'ımızdır ve yavaşlaması kimseyi ilgilendirmez.
   Burada yanlıştır: event loop bloke olursa ping/pong cevaplanmaz, Yahoo
   bağlantıyı düşürür ve *tüm* semboller durur.
2. **Okumayı duraklat** (TCP backpressure). Aynı sonuç, sadece daha yavaş.
3. **Düşür ve kaydet.** `live_quotes` güncellemesi korunur (son durum her
   zaman güncel kalır), `live_ticks` satırı düşürülür, `stream_rejects`'e
   `queue_overflow` yazılır, `stream_sessions.rows_dropped` artar.

Üçüncü seçenek "eksiksiz veri" şartını ihlal ediyor gibi görünür; etmez.
Alternatif çökmek ve *bütün* sembolleri kaybetmektir. Düşen tick'in
düştüğü **kaydedilir** — `bar_gaps`'in "no data ile we missed it ayrı
kalsın" ilkesinin aynısı. Ayrıca varsayılan 50.000'lik kuyruk, §3.1'deki
tahmini yükte kabaca 8 saniyelik bir tampondur; buraya kadar dolduran bir
writer zaten başlı başına alarm konusudur ve `yfin stream status` bunu
gösterir.

### 8.2 Batch

Batch, `yf_stream_batch_size` (500) satıra **veya**
`yf_stream_batch_interval_ms` (250) süresine ulaşınca commit edilir. Süre
sınırı olmadan sakin bir piyasada son tick'ler dakikalarca kuyrukta
beklerdi; sayı sınırı olmadan yoğun bir açılışta tek transaction aşırı
büyürdü.

Tek transaction içinde sırayla: `live_ticks` yazımı, `live_quotes`
guard'lı upsert'ü, (etkinse) `stream_outbox` yazımı, `stream_rejects`
örneklenmiş satırları, `stream_sessions` sayaç güncellemesi. Hepsi tek
commit — outbox'ın atomikliği (K5) buradan gelir.

**Invaryant: writer tek thread'dir.** Süreçte yazan tek bir thread vardır
ve batch'ler ardışık commit edilir; dolayısıyla `stream_outbox.id`'nin
`IDENTITY` sırası commit sırasıyla aynıdır. Relay'in `id > offset`
taramasının doğruluğu (§9.3) buna dayanır: eşzamanlı iki writer olsaydı,
küçük `id`'li bir satır büyük `id`'li birinden sonra commit edilebilir ve
offset onun üzerinden geçtiği için o satır hiç yayımlanmazdı.

Bu invaryant bir yorum değil, kısıttır: yazma yolunu paralelleştirmek
isteyen bir değişiklik önce relay'in ilerleme modelini değiştirmek
zorundadır. §12.1 bunu bir testle sabitler.

### 8.3 Kapanış

SIGTERM: bağlantılar kapatılır, kuyruk boşaltılır, son batch commit
edilir, `stream_sessions` `ok` ile kapatılır, advisory lock bırakılır.
Kuyrukta veri varken çıkılmaz. Süreç zorla öldürülürse (SIGKILL) yalnızca
kuyruktaki, henüz commit edilmemiş tick'ler kaybolur ve bu kayıp
`stream_sessions`'ın açık kalmış satırından anlaşılır — bir sonraki
başlangıç onu `failed` ile kapatır.

## 9. Kafka

### 9.1 Konu ve anahtar düzeni

**Konu = exchange, partition anahtarı = sembol.** İstenen iki eksenin
ikisi de karşılanır; biri konuda, biri anahtarda.

- `yfin.ticks.NMS`, `yfin.ticks.IST`, `yfin.ticks.unknown`.
- Sembol başına konu **değil**: 5.000 konu × partition, broker
  metadata'sını ve controller'ı çökertir.
- Anahtar sembol olduğu için aynı sembolün tüm mesajları aynı partition'a
  düşer → **sembol bazında sıra garantisi**. Tüketici bir sembolün
  tick'lerini her zaman doğru sırada görür.

`yf_kafka_topic_pattern` varsayılanı `"yfin.ticks.{exchange}"`. Tek konu
isteyen `"yfin.ticks"` yazar; desen `{exchange}` içermiyorsa tüm mesajlar
oraya gider. Exchange değeri `NULL`/boşsa `unknown` kullanılır ve kod
Kafka'nın konu adı kuralına (`[a-zA-Z0-9._-]`, en fazla 249 karakter) göre
sanitize edilir.

Konular otomatik oluşturulmaz varsayılamaz: `yfin stream relay`
başlangıçta gerekli konuların varlığını kontrol eder ve yoksa yüksek sesle
hata verir. Broker'ın `auto.create.topics.enable` ayarına bel bağlamak,
yanlış partition sayısıyla oluşmuş bir konuda sıra garantisini sessizce
kaybetmek demektir.

### 9.2 Değer formatı

JSON, alan adları proto ile birebir. Protobuf değil: tüketici çeşitliliği
JSON'u haklı çıkarır ve `stream_outbox.payload` zaten JSON metni
sakladığı için relay yeniden serileştirme yapmaz.

Gövde `live_ticks` satırının alanlarını taşır, artı `payload_hash` ve
`received_at` — böylece tüketici `(symbol, ts_utc, payload_hash)` üçlüsüyle
idempotent olabilir (§9.4).

### 9.3 Relay döngüsü

`confluent-kafka` (librdkafka) kullanılır; relay senkron bir süreçtir,
`aiokafka` yersiz olurdu.

1. `SELECT ... FROM stream_outbox WHERE id > :last ORDER BY id LIMIT :batch`
   — doğruluğu §8.2'deki tek-writer invaryantına dayanır.
2. Her satır için `produce(topic, key=symbol, value=payload)`; ardından
   `flush()`. **Tüm teslim callback'leri başarılı olmadan ilerlenmez.**
3. `stream_relay_offset.last_published_id` güncellenir.
4. Periyodik olarak, tamamı yayımlanmış chunk'lar için
   `drop_chunks('stream_outbox', older_than => ...)`.

### 9.4 Garanti: at-least-once

2. ve 3. adım arasında çökülürse aynı mesajlar yeniden yayımlanır.
Tüketici `(symbol, ts_utc, payload_hash)` ile idempotent olabilir — bu
üçlü zaten `live_ticks`'in birincil anahtarıdır ve mesajda taşınır.

Exactly-once **denenmez**: Kafka transaction'ı ile PostgreSQL
transaction'ı arasında iki fazlı commit kurmak, kazanılan garantiye
değmeyecek bir karmaşıklıktır ve tüketici tarafı zaten idempotent olmak
zorundadır.

### 9.5 Opsiyonellik

`kafka = ["confluent-kafka>=2.5"]` bir extra'dır. `stream/kafka.py`
import'u guard'lar. `yf_kafka_enabled = true` iken extra kurulu değilse
süreç **başlangıçta yüksek sesle** hata verir; sessizce devre dışı kalmaz.
Sessiz devre dışı kalma, operatörün Kafka'ya veri aktığını sanarak
saatlerce beklemesi demektir.

`yf_kafka_enabled = false` iken: outbox'a hiç yazılmaz, relay hiç
başlatılmaz, `confluent-kafka` hiç import edilmez.

## 10. `bar_gaps` mutabakatı

`yfin stream reconcile` (ayrı komut, akış sürecinin içinde değil):

1. `bar_gaps`'ten `resolved_at IS NULL` ve `reason != 'retention_expired'`
   olan, `bar_interval = '1m'` satırlarını okur.
2. Her satır için `live_ticks`'te `gap_start_utc <= ts_utc < gap_end_utc`
   aralığında, o sembole ait tick var mı diye bakar.
3. Varsa dakikalık OHLCV türetir: `open` ilk tick'in `price`'ı, `high`/`low`
   uç değerler, `close` son tick'in `price`'ı, `volume` ise **türetilmez** —
   `day_volume` kümülatiftir ve dakikalık hacme çevrilmesi iki ardışık
   dakikanın *tam* gözlemini gerektirir; eksik tick'te bu yanlış olur.
   `volume` `NULL` bırakılır.
4. `price_bars`'a upsert eder ve `bar_gaps.resolved_at`'i doldurup
   `reason`'ı `derived_from_ticks` yapar.

`is_extended`, tick'in `market_hours_code` alanından türetilir
(`1 = REGULAR` dışındaki her şey `true`).

Türetilmiş barların ayırt edilebilirliği `bar_gaps.reason` üzerinden
sağlanır: hangi pencerelerin tick'ten geldiği kalıcı olarak kayıtlıdır.
`price_bars`'a `source` kolonu eklemek reddedildi (K6) — o, her satıra
ödenen bir maliyetle nadir bir olguyu kaydetmek olurdu.

`volume`'un `NULL` bırakılması bilinçli bir eksikliktir ve bar'ı
değersizleştirmez: OHLC dört alanı da doğrudur, hacim ise "bilinmiyor"
olarak dürüstçe işaretlenir.

## 11. CLI ve ayarlar

### 11.1 Komutlar

```
yfin stream run                                  # supervisor, ön planda
yfin stream status                               # sağlık, gecikme p50/p95, kuyruk
yfin stream scope add SYMBOL... [--no-archive]
yfin stream scope disable SYMBOL...
yfin stream scope list [--exchange NMS]
yfin stream relay                                # outbox -> Kafka, ayrı süreç
yfin stream reconcile [--dry-run]                # açık bar_gaps -> 1m bar
```

`run` ve `relay` ön planda çalışır ve SIGTERM'e uyar: systemd, Docker veya
Kubernetes altında koşmak için tasarlanmıştır. Kendi kendini
daemonize etmez.

`status` şunları gösterir: bağlantı başına durum ve son mesaj zamanı,
kuyruk doluluk oranı, `received_at - ts_utc` p50/p95, son bir saatteki
reddedilen ve düşürülen sayıları, (etkinse) relay'in outbox gerisindeki
gecikmesi.

### 11.2 Ayarlar

Hepsi `DB_MANAGED`'dır — yani `yfin config set` ile süreç çalışırken
değiştirilebilir ve `yf_stream_rescan_seconds` içinde uygulanır. `_cfg`
grubu `stream`.

| ayar | varsayılan | anlam |
|---|---|---|
| `yf_stream_enabled` | `false` | Ana anahtar |
| `yf_stream_max_connections` | `8` | Bağlantı tavanı |
| `yf_stream_max_symbols_per_connection` | `250` | Bölme sınırı |
| `yf_stream_queue_maxsize` | `50000` | Kuyruk derinliği |
| `yf_stream_batch_size` | `500` | Batch satır eşiği |
| `yf_stream_batch_interval_ms` | `250` | Batch süre eşiği |
| `yf_stream_idle_timeout_seconds` | `300` | Idle watchdog |
| `yf_stream_reconnect_max_seconds` | `60` | Backoff tavanı |
| `yf_stream_archive_default` | `true` | Yeni scope satırlarının `archive` varsayılanı |
| `yf_stream_reject_sample_per_hour` | `100` | `(symbol, reason)` başına reddedilen örnekleme |
| `yf_stream_rescan_seconds` | `60` | Scope/ayar yeniden okuma aralığı |
| `yf_kafka_enabled` | `false` | Outbox + relay ana anahtarı |
| `yf_kafka_bootstrap_servers` | `""` | |
| `yf_kafka_topic_pattern` | `"yfin.ticks.{exchange}"` | |
| `yf_kafka_relay_batch` | `1000` | Relay okuma batch'i |

Hiçbiri sır taşımaz, dolayısıyla hiçbiri `ENV_ONLY_FIELDS`'e girmez.
(Kafka SASL kimlik bilgileri gerekirse — bu spec'in kapsamında değil —
`yf_proxy_secret_key` kalıbıyla ortamda kalırlar.)

## 12. Test stratejisi

### 12.1 unit (veritabanı yok, dış ağ yok)

- **33 alanın tamamının haritalandığını statik doğrulayan test.**
  `PricingData.DESCRIPTOR.fields` ile `protocol.py`'daki kolon haritası
  karşılaştırılır. `yfinance` protoya bir alan eklerse test kırılır ve
  alan sessizce düşmez. `test_schema_invariants`'ın kurduğu kültürün
  akıştaki karşılığı.
- `f32_decimal` property testi: 10.000 rastgele binary32 değeri için
  round-trip eşitliği ve "daha kısa bir gösterim yok" iddiası.
- Presence → `NULL` kuralı; ms/sn zaman dönüşümleri; `expire_date` aralık
  savunması; NaN/Inf davranışı.
- Sembol → bağlantı eşlemesinin determinizmi: aynı girdi, farklı
  `PYTHONHASHSEED` ile aynı çıktı. Tavan ve bölme mantığının uç durumları.
- Backoff dizisi, idle watchdog tetiklenmesi (özellikle:
  `last_message_at IS NULL` iken tetiklenmediği), batch eşikleri (sayı ve
  süre), kuyruk taşma politikası.
- **Tek-writer invaryantı (§8.2):** supervisor'ın tam olarak bir writer
  thread başlattığı. Bu testin tek işi, relay'in ilerleme modelini sessizce
  geçersiz kılacak bir paralelleştirmenin fark edilmeden girmesini
  engellemektir.
- **Loopback WebSocket sunucusuyla** reconnect / heartbeat / idle
  senaryoları.

Son madde, projenin "unit = no database, no network" kuralının bilinçli
bir istisnasıdır. Gerekçe: yeniden bağlanma davranışı ancak gerçek bir
soketle test edilebilir. Mock'lanmış bir reconnect testi, upstream'de tam
da bu yüzden gözden kaçmış olan hatayı (K2, madde 1) bir kez daha
kaçırırdı. Sunucu `127.0.0.1`'de dinler; dış ağ erişimi yoktur.

### 12.2 repo (gerçek PostgreSQL + TimescaleDB)

- `live_ticks` ve `stream_outbox` hypertable oluşumu ve chunk aralıkları.
- `alembic revision --autogenerate` boş diff üretir.
- PK üçlüsünün davranışı: aynı payload iki kez → 1 satır; aynı
  milisaniyede farklı payload → 2 satır.
- `guard_column`'lı upsert: eski `ts_utc` taşıyan bir tick `live_quotes`'u
  geri sarmıyor, **hiçbir** kolonu güncellemiyor.
- Bilinmeyen sembol batch'i patlatmıyor, `stream_rejects`'e düşüyor.
- Outbox → relay offset ilerleyişi; `drop_chunks` sonrası offset
  tutarlılığı; relay yeniden başlatıldığında yinelenen yayın (kabul
  edilen at-least-once davranışı) doğrulanıyor.
- Reddedilen örnekleme sınırının sayaçları etkilemediği.
- `test_schema_invariants` genişletmesi: yeni tabloların collation ve FK
  tutarlılığı.

### 12.3 live (`@pytest.mark.live`)

Gerçek Yahoo WS'e 60 saniye bağlanır, en az N mesaj alır, hepsinin
yazıldığını ve `stream_rejects`'in boş kaldığını doğrular.

`yfin_stream` advisory lock'u `yfin_sync`'ten ayrı olduğu için bu test
mevcut LIVE testleriyle çakışmaz ve onlarla paralel koşabilir.

## 13. Uygulama aşamaları

**Aşama 0 — Ölçüm.** `docs/measurements/websocket.md`: 30 dakikalık gerçek
bağlantı ile mesaj/saniye (sembol ve exchange kırılımında), 33 alanın
doluluk oranı, float32 artık dağılımı, `expire_date` biriminin
doğrulanması, bağlantı başına sembol limiti, `received_at - ts_utc`
gecikme dağılımı.

§11.2'deki varsayılanlar bu ölçümden **sonra** sabitlenir; belgedeki
sayılar başlangıç tahminidir. Projenin kültürü kararı ölçüme bağlamaktır
ve bu alt sistemin hiçbir sayısı bugün ölçülmüş değildir.

1. `protocol.py` + tip dönüşümleri + unit testler.
2. `models/stream.py` + migration + repo testler.
3. `connection.py` + `supervisor.py` + loopback sunucu testleri.
4. `writer.py` + backpressure + `stream_scope` + `yfin stream run/status/scope`.
5. `TableWrite.guard_column` + `live_quotes`.
6. `stream_outbox` + `relay.py` + `kafka.py` + `kafka` extra + `yfin stream relay`.
7. `reconcile.py` + `bar_gaps.reason='derived_from_ticks'` + `yfin stream reconcile`.
8. README (`WebSocket streaming`: TODO → In progress), `docker-compose.yml`'ye
   opsiyonel Kafka servisi, CI.

Aşama 6'ya kadar Kafka'sız, tam çalışan bir sistem vardır. Her aşama kendi
başına yeşil bırakılabilir.

## 14. Açık maddeler (bu spec'in kapsamı dışında, bilinerek)

- **`live_ticks` için retention/compression politikası.** Tablo
  append-only olduğu için compression'ın önündeki mevcut engel (rescale'in
  geçmiş satırları yeniden yazması) burada yoktur. Ayrı bir ölçüm ve
  karar gerektirir.
- **Dışa dönük müşteri WebSocket'i.** §1'de kapsam dışı bırakıldı.
- **Tick verisinin salt-okuma API'sinden sunulması.** Mevcut API
  spec'inin genişletmesidir; `DataFamily`'ye yeni bir üye eklenip
  eklenmeyeceği (yoksa `BARS` altında mı kalacağı) o spec'in kararıdır.
- **Kafka SASL/TLS kimlik doğrulaması.** Bugün yalnızca düz
  `bootstrap.servers` destekleniyor.
