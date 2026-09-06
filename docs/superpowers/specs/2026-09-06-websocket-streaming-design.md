# Canlı WebSocket akışı: ingest ve dağıtım omurgası — Tasarım

Tarih: 2026-09-06
Durum: onaylandı (iki bağımsız inceleme ve bir ölçüm turu sonrasında revize edildi)

Ölçümler: [`docs/measurements/websocket.md`](../../measurements/websocket.md)

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
bağlantı başına backpressure.

Bugünden hazırlanan tek dokunuş: Kafka konu ve anahtar düzeni (§9) o
servisin de doğal veri kaynağı olacak şekilde seçilir — fan-out servisi
`live_ticks` tablosunu poll etmek zorunda kalmaz.

Ayrıca kapsam dışı: tick'lerden türev gösterge hesabı, tick verisinin
salt-okuma API'sinden sunulması, `live_ticks` için retention/compression
politikası (§14).

## 2. Mimari kararlar

### K1 — Ayrı süreç; async yalnızca I/O sınırında

`yfin stream run` uzun ömürlü, ayrı bir süreçtir. İçinde tek bir asyncio
event loop **yalnızca** WS bağlantılarını ve protobuf çözümlemesini
yürütür. Çözülmüş tick'ler sınırlı bir `queue.Queue`'ya düşer; ayrı bir
**writer thread** batch'ler halinde yazar (§8).

Gerekçe: salt-okuma API spec'inin K1 kararı (tek senkron idiom, `mypy
--strict`) korunur. Async biçem `stream/` paketinin dışına sızmaz.

Reddedilen:

- **Uçtan uca async (asyncpg / async SQLAlchemy).** Kalıcı ikinci bir
  idiom ve ikinci bir yazma yolu. Ölçülen tavan (§3.2) veritabanı
  tarafındadır, Python tarafında değil — yani bu bedelin karşılığı yok.
- **API sürecine gömme.** İki farklı yük profili tek deploy'a karışır ve
  API'nin her yeniden başlatılışı veri toplamayı keser; canlı akışta
  kaybedilen veri geri gelmez.
- **Bağlantı başına thread (asyncio yok).** Kod tabanıyla tek deyim
  olurdu, ama yeniden bağlanma ve heartbeat mantığı her thread'de ayrı
  yürür.

### K2 — `yfinance`'ın WebSocket sınıfları kullanılmaz; yalnız `pricing_pb2` alınır

`stream/connection.py` doğrudan `websockets.asyncio.client.connect`
kullanır. `yfinance`'tan ithal edilen tek şey
`yfinance.pricing_pb2.PricingData` mesaj sınıfıdır.

Bu, projenin "Yahoo ile konuşmayı yfinance yapar" ilkesinden bilinçli bir
sapmadır ve dört doğrulanmış nedene dayanır (yfinance 1.7.0, `live.py`):

1. **`AsyncWebSocket`'in reconnect'i ölü koddur.** `listen()`'in `except`
   dalı 3 saniye bekleyip `await self._connect()` çağırır; ama
   `_connect()` yalnızca `self._ws is None` iken bağlanır ve hata yolunda
   `_ws` hiçbir zaman `None`'a çekilmez (yalnız *bağlanma*
   başarısızlığında çekilir). Sonuç: kapalı soket üzerinde 3 saniyede bir
   dönen sonsuz hata döngüsü. Yeniden bağlanma hiç gerçekleşmez.
2. **`WebSocket` (senkron) hatada sessizce durur.** `listen()`'in genel
   `except` dalı `break` eder; döngüden çıkar, hiçbir şey bildirmez.
3. **Global duruma bağımlılık.** `verbose=True` varsayılanı `print()`
   yapar; istisnaların yutulup yutulmayacağı
   `YfConfig.debug.hide_exceptions` genel bayrağına bağlıdır.
4. **Deprecated çağrı biçimi.** `sync_connect(self.url)` — ölçümde
   `DeprecationWarning: connect() must be used as a context manager`
   üretti.

"Çökmeyecek akış" şartı bu dört madde varken upstream'e bırakılamaz.
Protobuf şeması ise upstream'de kalır: `pricing.proto` değişirse
`pricing_pb2` ile birlikte gelir ve §12.1'deki statik alan testi bunu
yakalar.

**Bağımlılıklar açıkça eklenir:** `websockets` **ve `protobuf`**. İkisi de
bugün `yfinance` üzerinden transitif geliyor ama doğrudan kullanılıyor —
`curl_cffi` için kurulmuş olan kalıbın aynısı, aynı gerekçeyle.

### K3 — Bağlantı bölme gerekçesi arıza izolasyonudur, Yahoo limiti değil

**Ölçüldü: Yahoo'nun gözlenen bir sembol sayısı sınırı yoktur.** 50.000
sembollük, 802 KB'lık tek bir `subscribe` mesajı reddedilmedi, bağlantı
kapatılmadı, akış devam etti (§3.3). 10.000 sembol tek bir bağlantıdan
dinlenebilir.

Dolayısıyla bağlantıyı bölmenin gerekçesi kapasite değil, **arıza
izolasyonudur**: bir bağlantının ölümü yalnızca kendi sembol kümesini
durdurmalıdır. Exchange doğal bölme eksenidir, çünkü bir borsanın
sorunları o borsanın sembollerinde yoğunlaşır ve Kafka konu düzeni (§9.1)
aynı ekseni kullanır.

Bu ölçümün doğrudan sonucu olarak tasarımdan bir parça **çıkarıldı**:
bağlantı başına sembol tavanı varsayılan olarak devre dışıdır
(`yf_stream_max_symbols_per_connection = 0`). Ölçülmemiş bir limite karşı
savunma kodu yazmak YAGNI ihlalidir.

Reddedilen: **tek bağlantı, tüm semboller** — teknik olarak mümkün olduğu
ölçüldü, ama tek arıza noktasıdır; bir kopma bütün evreni durdurur.

### K4 — Akış kapsamı bir tablodur, ayar değil

Hangi sembollerin stream edileceği ve hangilerinin arşive gireceği
`stream_scope` tablosunda tutulur. Gerekçe `intraday_scope`'unkinin
aynısıdır (`models/bars.py:150-155`): 5.000+ sembollük bir evrende bir alt
küme `.env`'e sığmaz ve sürümlenebilir olması gerekir.

**Abonelik evreni `stream_scope.enabled = true` VE `symbols.is_active =
true`'dur.** İkinci koşul zorunludur: `known_symbols()`
(`storage/persistence.py:236-242`) yalnızca satırın varlığına bakar,
`is_active`'e bakmaz. Bu koşul olmadan `yfin symbols deactivate` edilmiş
veya `yf_delist_threshold`'u aşmış bir sembol
(`pipeline/runner.py:508-516`) akıştan hiç çıkmazdı — `yfin sync`'in
süzdüğü (`cli/app.py:514-516`) bir sembolün tick'leri yazılmaya devam
ederdi.

### K5 — Kafka opsiyoneldir ve maliyetini yalnızca kullanan öder

`yf_kafka_enabled = false` iken `stream_outbox`'a **hiçbir satır
yazılmaz**; yazma amplifikasyonu tam olarak sıfırdır. Açıkken outbox
satırı tick ile **aynı transaction'da** yazılır: at-least-once, sıralı ve
veritabanıyla atomik.

**Kabul edilen bedel, açıkça:** outbox ikinci bir yazma yoludur ve
ölçüldüğünde ucuz değildir — `COPY` yoluna geçildikten sonra **darboğaz
outbox'ın kendisine geçer** (§3.2). Bu yüzden outbox da `COPY` ile yazılır
ve temizliği `DELETE` değil `drop_chunks`'tır.

Reddedilen:

- **`live_ticks`'i log olarak tail eden relay (outbox tablosu yok).**
  Yazma amplifikasyonu sıfır olurdu, ama iki somut nedenle kapsamı
  yanlıştır. Birincisi: `archive = false` olan semboller (§6.4)
  `live_ticks`'e hiç yazılmaz, dolayısıyla Kafka'ya da ulaşmazlardı —
  oysa o sembollerin canlı fiyatı tam da tüketiciye akması gereken
  veridir. İkincisi: `live_ticks` ileride retention/compression'ın ilk
  adayıdır (§14); tail edilen kaynağın altından bir retention
  politikasının kayması, henüz yayımlanmamış satırların sessizce yok
  olması demektir.
- **Doğrudan producer (DB commit'inden sonra publish).** Kafka düşükken
  üretilen tick'ler veritabanına yazılır ve konuya hiç girmez — tüketici
  için kalıcı, telafisi olmayan boşluk.
- **Önce Kafka, sonra DB.** Kafka fiilen zorunlu hale gelir; "opsiyonel
  Kafka" şartı bozulur.

### K6 — Tick arşivi ile bar arşivi ayrıdır; tek köprü `bar_gaps`

`price_bars`'ın otoritesi Yahoo'nun bar endpoint'i olarak kalır. Tick'ler
`price_bars`'a **yalnızca** açık bir `bar_gaps` satırının penceresindeki
**boş dakikalar** için yazılır (§10).

Gerekçe: Yahoo 1m verisini 29 gün sonra düşürür. O pencerede kaçırılan bir
çekimin telafisi yoktur — elimizde tick varken o boşluğu boş bırakmak
bilgi imha etmektir. Buna karşılık her tick'ten bar türetip arşive yazmak
arşivin otoritesini karıştırır ve `bar_rescales` ledger'ını iki kaynaklı
satırları ayırt etmek zorunda bırakırdı.

**Mutabakat `retention_expired` satırlarını KAPSAR.** Bu, incelemede
düzeltilen bir hatadır: ilk taslak onları dışlıyordu, oysa K6'nın varlık
gerekçesini oluşturan pencereler tam olarak onlardır. `fetch_failed`
pencereleri zaten `GapReader` üzerinden Yahoo'dan yeniden çekilebilir
(`pipeline/runner.py:232`); `retention_expired` pencereleri ise **asla**
çekilemez ve tick tek kaynaktır. Dışlayan bir mutabakat, çözmek için
tasarlandığı problemi çözmüyor olurdu.

Yan etkisi kabul edilir: `models/bars.py`'deki "`retention_expired` rows
always stay NULL here" yorumu artık doğru değildir ve güncellenir.

Reddedilen: (a) hiç mutabakat yok — yukarıdaki telafi kaybı; (b) ayrı bir
`live_bars` tablosu — üçüncü bir bar tablosu ve "hangisi doğru" sorusu;
(c) `price_bars`'a `source` kolonu — her satıra ödenen bir maliyetle nadir
bir olguyu kaydetmek.

### K7 — Yazma yolu `COPY` tabanlıdır

`live_ticks` ve `stream_outbox` `COPY` ile yazılır: gövde `ON COMMIT DROP`
bir geçici tabloya alınır, oradan tek bir `INSERT ... SELECT ... ON
CONFLICT` ile hedefe taşınır.

Bu bir iyileştirme değil, 10.000 sembollük hedefin önkoşuludur. Ölçülen
(§3.2): düz `INSERT` ile tam yol **6.219** tick/sn, `COPY` ile **22.291**
tick/sn. 10.000 sembolün ortalama 1 mesaj/saniye ürettiği bir senaryo düz
`INSERT` ile karşılanamaz.

`COPY` `ON CONFLICT` desteklemediği için iki adım zorunludur; dedup ve FK
kontrolü ikinci adımda korunur. `_verify()`'ın key-existence sorgusu da
korunur — ölçüldü, maliyeti **%2**'dir, yani "her yazma okunarak
doğrulanır" güvencesini canlı akışta korumanın pratikte bedeli yoktur.

### K8 — `live_quotes` kuyruktan değil, son-değer kutusundan beslenir

Supervisor, sembol başına en son tick'i kilitli bir sözlükte
(`latest: dict[str, Tick]`) tutar. Writer thread her N batch'te bu
sözlüğün anlık kopyasını alıp `live_quotes`'u günceller.

İki sorunu birden çözer:

1. **Doğruluk.** İlk taslak "kuyruk dolduğunda `live_quotes` güncellemesi
   korunur" diyordu, ama kuyruk event loop ile writer arasındaki tek
   kanaldı — kuyruk doluysa tick writer'a hiç ulaşmaz ve o iddianın
   gerçekleşeceği bir yol yoktu. Son-değer kutusu kuyruktan bağımsızdır:
   kuyruk taşsa bile son durum güncel kalır.
2. **Maliyet.** `live_quotes` upsert'i ölçümde tek başına **%34**
   yiyordu; sabit bir kadansa taşımak **%23** kazandırır (§3.2).

`live_quotes` türetilmiş bir görünümdür, kaynağı `live_ticks`'tir. Onu
birkaç yüz milisaniye geciktirmek veri kaybettirmez.

## 3. Ölçülen gerçekler

Tümü [`docs/measurements/websocket.md`](../../measurements/websocket.md)
içinde, yöntemiyle birlikte.

### 3.1 Hacim

| Girdi | Değer |
|---|---|
| Akıştaki sembol (varsayım) | 500 |
| Ortalama mesaj hızı (**ölçülmedi**) | ~0,5 mesaj/sn/sembol |
| Seans süresi | 6,5 saat |
| Satır/gün | ~5,8 M |
| Satır/yıl | ~1,5 milyar |

Boyut, `docs/measurements/volume.md`'nin yöntemiyle ve **indeksler ayrı
bütçelenerek**:

| Bileşen | Yıllık |
|---|---|
| `live_ticks` yığın (36 kolon, 12'si `NUMERIC(28,12)`) | ~400–450 GB |
| `pk_live_ticks` (symbol + ts_utc + payload_hash) | ~80 GB |
| `ix_live_ticks_received_at` | ~45 GB |
| **Toplam** | **~500–600 GB** |

İlk taslaktaki "200 bayt/satır, ~300 GB" tahmini düşüktü ve indeksleri hiç
saymıyordu; `volume.md`'nin 9 kolonlu `price_bars` için ölçtüğü ~108
B/satır ve indeks başına ~30 B/satır ile tutarsızdı.

Karşılaştırma: tüm bar arşivi ilk yıl sonunda ~464 M satır ve ~64 GB'dır.
Canlı tick arşivi **satır sayısında ~3,2 kat, disk boyutunda ~8 kat** daha
büyüktür.

Bu, `stream_scope`'un (K4) neden opt-in olduğunun tek nedenidir.

### 3.2 Yazma yolu kapasitesi (ölçüldü)

Tek writer thread, `live_ticks` şemasının birebir kopyası, FK ve
hypertable dahil:

| Yapılandırma | tick/sn |
|---|---:|
| `INSERT` + her batch `live_quotes` + `INSERT` outbox | 6.219 |
| `COPY` ticks + `INSERT` outbox + seyrek quotes | 13.634 |
| **`COPY` ticks + `COPY` outbox + seyrek quotes** | **22.291** |

Bileşen maliyetleri: `_verify()` **%2**, `live_quotes` her batch upsert
**%34**, outbox **%11**. Batch'i 500'den 5.000'e çıkarmak yalnızca %6
kazandırır — dolayısıyla küçük batch (500) bedavadır ve gecikmeyi düşük
tutar.

Ortadaki satır tek başına öğreticidir: `live_ticks`'i `COPY`'ye taşımak
yetmez, o anda darboğaz outbox'a geçer (K5).

**Ölçümün sınırı, dürüstçe:** tek günlük veri üzerinde, yani tek chunk'ta
alınmıştır. `_verify()`'ın 3 kolonlu satır-kurucu `IN` sorgusu
(`storage/persistence.py:216-252`, `VERIFY_CHUNK = 500`) `ts_utc` üzerinde
aralık koşulu taşımadığı için TimescaleDB chunk exclusion uygulayamaz;
yüzlerce chunk biriktiğinde maliyetin %2'de kalıp kalmayacağı
**ölçülmemiştir**. §13'ün 0. aşaması bunu ölçer.

### 3.3 Abonelik limiti (ölçüldü)

5, 100, 500, 1.000, 2.000, 5.734, 10.000, 20.000 ve **50.000** sembol
seviyelerinin tamamı kabul edildi; hiçbirinde bağlantı kapanmadı. 802
KB'lık abonelik gövdesi sorunsuz iletildi. Geçersiz semboller için
**hiçbir hata dönmedi** — sunucu tanımadığını sessizce yok sayar.

Ölçülmeyen: açık piyasada aynı abonelik büyüklüğünün sürdürülebilirliği,
IP başına eşzamanlı bağlantı sınırı, uzun süreli bağlantıda throttle veya
ban davranışı, gerçek mesaj hızı ve alan doluluk oranları — ölçüm Pazar
günü alındı, yalnızca kripto akıyordu.

### 3.4 Sembol evreni (ölçüldü)

5.735 sembol, `exchange IS NULL` yok, 9 farklı exchange. Dağılım (2.717
sembolken): NAS 698, PCX 617, NYQ 474, NMS 335, NGM 236, BTS 178, YHD 156,
NCM 18, ASE 5.

**Dağılım son derece dengesizdir** — en büyük grup en küçüğün 140 katı.
Exchange başına bir bağlantı, 5 sembollük bir bağlantı ile 698 sembollük
bir bağlantıyı yan yana çalıştırır. Bu kabul edilir: amaç yük dengesi
değil (K3), arıza izolasyonudur.

## 4. Bağlantı topolojisi

### 4.1 Sembol → bağlantı eşlemesi

1. **Evren:** `stream_scope.enabled = true` **AND** `symbols.is_active =
   true` (K4).
2. **Gruplama:** `symbols.exchange`. `NULL`/boş → `unknown` grubu.
3. **Tavan:** grup sayısı `yf_stream_max_connections`'ı (varsayılan 16)
   aşarsa gruplar sembol sayısına göre azalan sırada dizilir ve en küçük
   gruplar tavana kadar birleştirilir (deterministik greedy bin-packing).
   Ölçülen evrende (9 exchange) bu adım hiç devreye girmez.
4. **Bölme:** `yf_stream_max_symbols_per_connection > 0` ise aşan grup
   `NMS#0`, `NMS#1` diye bölünür. **Varsayılan 0'dır, yani kapalıdır**
   (K3).

İlk taslak burada `blake2b(exchange) mod tavan` kullanıyor ve bunu
`pipeline/shard.py`'a atfediyordu. **Her iki atıf da yanlıştı:**
`shard.py`'de hash tabanlı sembol eşlemesi yoktur — semboller dinamik bir
`mp.Queue` üzerinden iş-çalma ile dağıtılır (`shard.py:64`, `:126-135`,
`:396-404`). Depodaki tek `blake2b` `storage/db.py:25`'te, advisory lock
anahtarı içindir ve gerekçesi PYTHONHASHSEED değil, PostgreSQL
`hashtext()`'in sürümler arası değişebilmesidir (`db.py:19-24`).

`mod tavan` ayrıca §4.4'ün "cerrahi yeniden dengeleme" iddiasıyla
çelişirdi: tavan 8'den 9'a çıktığında modulus değişir ve neredeyse her
exchange yer değiştirirdi. Sıralı bin-packing'de tavan değişimi yalnızca
birleştirilmiş grupları etkiler; hiçbir grup birleştirilmemişse — olağan
durum — hiçbir bağlantı taşınmaz.

### 4.2 Çökmeme garantisi

Bir bağlantının ölümü yalnızca kendi sembol kümesini etkiler. Her bağlantı
görevi kendi `try` sınırındadır; ölümü `stream_connection_health`'e
yazılır ve backoff ile yeniden kurulur. Supervisor hiçbir bağlantı
istisnasını yukarı taşımaz.

Üç savunma katmanı, üçü farklı arıza tipine karşı:

| Katman | Ayar | Yakaladığı arıza |
|---|---|---|
| `websockets` ping/pong | `ping_interval=20`, `ping_timeout=20` | TCP yaşıyor, karşı uç ölü |
| Idle watchdog | `yf_stream_idle_timeout_seconds` (300) | Bağlantı sağlıklı görünüyor ama mesaj gelmiyor |
| Abonelik heartbeat | 15 sn (upstream davranışı) | Yahoo tarafında sessizce düşmüş abonelik |

**Idle watchdog'un tetiklenme koşulu:** bağlantı `open`, `last_message_at`
**dolu**, ve o andan bu yana `yf_stream_idle_timeout_seconds` geçmiş.

`last_message_at`'in dolu olması şartı kapalı borsayı piyasa takvimine
bakmadan halleder. Hiç mesaj almamış bir bağlantı — hafta sonu, açılış
öncesi — hiçbir zaman yeniden kurulmaz. Seans bittiğinde watchdog bir kez
tetiklenir; yeniden kurulan bağlantının `last_message_at`'i `NULL`'dır,
dolayısıyla ikinci kez tetiklenmez. Kapalı borsanın maliyeti seans başına
tek bir gereksiz yeniden bağlanmadır.

Alternatif — `symbols.timezone` ve `calendar_*` verisinden seans penceresi
hesaplamak — reddedildi: watchdog'un doğruluğunu, kendisi de bu hattan
gelen ve eksik olabilen bir veriye bağlardı.

Backoff: full-jitter üstel, 1 sn → `yf_stream_reconnect_max_seconds` (60)
tavanı, sonsuz. Vazgeçme yoktur; vazgeçmek sessiz veri kaybıdır.

**Writer thread ölümü fail-fast'tir.** Writer bir `OperationalError` ile
ölürse süreç devam *edemez*: kuyruk dolar ve §8.3'e göre her tick
düşürülür — yani süreç ayakta görünürken %100 veri kaybeder. Bu yüzden
writer thread'in beklenmedik ölümü supervisor'ı ölümcül biçimde durdurur,
`stream_sessions` `failed` ile kapatılır ve süreç sıfırdan farklı bir
çıkış koduyla çıkar. Yeniden başlatma kararı süreç yöneticisinindir
(systemd / Kubernetes), bu spec'in değil.

### 4.3 Süreç tekliği

Üç komut, üç ayrı advisory lock (`storage/db.py:134-149` mekanizması;
ikinci bir kilit adı kullanımı zaten var — `cli/app.py:864-866`,
`yfin_market_sync`):

| Komut | Kilit | Gerekçe |
|---|---|---|
| `yfin stream run` | `yfin_stream` | İki writer aynı anda yazmasın |
| `yfin stream relay` | `yfin_stream_relay` | Aşağıda |
| `yfin stream reconcile` | **`yfin_sync`** | Aşağıda |

**Relay'in kendi kilidi zorunludur:** iki relay süreci aynı
`last_published_id`'yi okur, aynı mesajları yayımlar ve birbirinin
offset'ini geri sarabilir. §6.6'daki `CHECK (id = 1)` bu riski **çözmez**
— o yalnızca ikinci bir *satırı* engeller, ikinci bir *süreci* değil.

**`reconcile` `yfin_sync` alır** çünkü `price_bars` ve `bar_gaps`'e yazar,
yani tam da `run_sync`'in aynı kilit altında yazdığı tablolara
(`pipeline/shard.py:56`). Akış sürecinin sync ile aynı anda koşabilmesi
(ayrı kilitler) bu komut için geçerli değildir.

### 4.4 Yeniden dengeleme

Supervisor `stream_scope`'u ve ayarları `yf_stream_rescan_seconds` (60)
aralığıyla yeniden okur:

- Sembol eklendi/çıkarıldı ya da `is_active` değişti → ilgili bağlantı
  `subscribe` / `unsubscribe` gönderir; bağlantı kesilmez.
- Yeni bir exchange geldi → yalnızca yeni bağlantı kurulur.
- Tavan değişti ve birleştirme devredeyse → yalnızca birleştirilmiş
  gruplar taşınır; birleştirme yoksa hiçbir bağlantı etkilenmez (§4.1).

**Ayarların yeniden okunması açık bir işlemdir.** `get_settings()` süreç
ömrü boyunca tek bir singleton döndürür (`core/config.py:503-509`) ve
`applied_overrides()`'ın docstring'i bunu açıkça söyler: "The `settings`
table is never RE-READ" (`core/config.py:512-518`). Supervisor bu yüzden
her tarama turunda `settings_store.load_overrides()` +
`settings_from_overrides()` ile taze bir `Settings` nesnesi üretir ve
kendi yapılandırmasını ondan alır; süreç genelindeki singleton'a
dokunmaz. İlk taslağın "`yfin config set` ile çalışırken değiştirilebilir"
iddiası bu mekanizma olmadan yanlıştı.

## 5. Paket düzeni

```
src/yfin/stream/
  __init__.py
  protocol.py     PricingData -> tick. 33 alanın kolon haritası,
                  float32 -> Decimal, ms/sn -> ts_utc, presence kuralı,
                  sembol normalizasyonu. `yfinance`'a bağımlı TEK modül.
  connection.py   Tek bağlantının yaşam döngüsü.
  supervisor.py   Bağlantı kümesi, eşleme, son-değer kutusu (K8),
                  yeniden dengeleme.
  repository.py   Bu paketin TÜM okuma ve bakım SQL'i.
  writer.py       Writer thread: batch -> COPY -> staging -> hedef.
  relay.py        stream_outbox -> Kafka; offset ve chunk temizliği.
  kafka.py        Opsiyonel producer sarmalayıcı (import guard).
  reconcile.py    Açık bar_gaps -> tick'ten 1m bar.
src/yfin/models/stream.py
src/yfin/cli/stream.py
```

`repository.py` ilk taslakta yoktu ve bu bir boşluktu: katman kuralı
"`stream/` yalnızca `storage/contracts.py`'ye bağımlıdır" diyordu, ama o
sözleşme yalnızca `write` / `current_hash` / `known_symbols` sunar
(`storage/contracts.py:60-103`) — rastgele `SELECT` yoktur. Relay'in
outbox taraması, reconcile'ın `bar_gaps` sorgusu ve `stream status`'un
toplamları o sözleşmeyle ifade edilemezdi.

Katman kuralı, düzeltilmiş hali:

- **Yazma** `storage/contracts.py` üzerinden gider (`RowWriter`,
  `TableWrite`).
- **Okuma ve bakım SQL'i** yalnızca `stream/repository.py` içindedir.
- `protocol.py` SQLAlchemy import etmez, veritabanı bilmez.
- `connection.py` ve `supervisor.py` veritabanı bilmez; sağlık kayıtlarını
  callback üzerinden yazar.
- `cli/stream.py` sorgu yazmaz.

CLI `app.py`'ye `app.add_typer(stream_app, name="stream")` ile bağlanır;
`cli/stream.py` kendi session factory'sini kurar, böylece `cli/bars.py` ile
`cli/app.py` arasındaki mevcut dairesel import bu pakette tekrarlanmaz.

## 6. Veri modeli

Sekiz tablo. **FK taşıyan her `symbol` kolonu `symbol_fk_column()` ile
tanımlanır** (`models/base.py:185-194`, `ON UPDATE CASCADE ON DELETE
RESTRICT`); `test_schema_invariants.py:27-39` bu iki kuralı zorunlu kılar
ve helper'ı kullanmayan bir tanım testte patlar.

- **FK taşıyan:** `live_ticks`, `live_quotes`, `stream_scope`.
- **FK taşımayan:** `stream_outbox`, `stream_rejects`,
  `stream_relay_offset`, `stream_sessions`, `stream_connection_health`.

İki "FK yok" kararının gerekçeleri **farklıdır** ve ayrı ayrı yazılır
(§6.5 ve §6.7); ilk taslak ikisini aynı gerekçeye bağlıyordu ve bu yanlıştı.

### 6.1 `live_ticks` — hypertable

```
create_hypertable('live_ticks', by_range('ts_utc', INTERVAL '1 day'),
                  create_default_indexes => FALSE)
```

DDL `models/bars.py`'deki `timescale_ddl()` kalıbıyla tek kaynakta durur;
hem migration hem `tests/conftest.py:126` onu kullanır. Unutulursa testler
sessizce düz tabloya düşer ve hypertable davranışı hiç doğrulanmaz.

`create_default_indexes => FALSE` zorunludur: varsayılan indeks
`Base.metadata`'da yoktur ve autogenerate onu sonsuza kadar "düşürülmeli"
diye raporlar.

Chunk aralığı `price_bars`'ın 7 gününden farklı olarak 1 gündür: günlük
~5,8 M satırda 7 günlük chunk ~40 M satıra ulaşır.

**Birincil anahtar: `(symbol, ts_utc, payload_hash)`**

- Yahoo aynı milisaniyede aynı sembol için birden çok mesaj gönderebilir;
  `(symbol, ts_utc)` çakışır ve ikinci tick kaybolurdu.
- Yahoo aynı mesajı tekrar gönderebilir (15 saniyelik heartbeat sonrası
  anlık görüntüler). `payload_hash` bunu `ON CONFLICT DO NOTHING` ile eler.

`payload_hash` = **33 alanın ve `unknown_fields`'ın** kanonik gösteriminin
SHA-256'sının ilk 16 hanesi (`ShortHashType()`). `unknown_fields`'ın hash'e
dahil edilmesi zorunludur: dışarıda bırakılsaydı, 33 alanı aynı ama yeni
bir proto alanı farklı olan ikinci mesaj `DO NOTHING` ile düşer ve o yeni
alanın değeri hiç yazılmazdı — yani `unknown_fields`'ın tek varlık
gerekçesi ortadan kalkardı.

Batch içi tekrar ayrıca `dedupe_rows` + `distinct_key_count` tarafından
collapse edilir (`storage/contracts.py:106-124`,
`storage/persistence.py:49-71`), dolayısıyla `attempted` sayısı doğru kalır.

`update_columns` boştur, dolayısıyla writer zaten `on_conflict_do_nothing`
üretir (`storage/persistence.py:184-188`) — ek kod gerekmez.

### 6.2 33 protobuf alanının kolon haritası

Hiçbir alan düşmez. `pricing.proto` sırası korunur.

| # | proto alanı | proto tipi | kolon | SQL tipi |
|---|---|---|---|---|
| 1 | `id` | string | `symbol` | `symbol_fk_column()`, **PK** |
| 2 | `price` | float | `price` | `PriceType()` |
| 3 | `time` | sint64 | `ts_utc` | `TsType()`, **PK** |
| 4 | `currency` | string | `currency` | `VARCHAR(32) C` |
| 5 | `exchange` | string | `exchange` | `VARCHAR(32) C` |
| 6 | `quote_type` | int32 | `quote_type_code` | `SmallInteger` **NOT NULL** |
| 7 | `market_hours` | int32 | `market_hours_code` | `SmallInteger` **NOT NULL** |
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
| 20 | `options_type` | sint64 | `options_type_code` | `Integer` |
| 21 | `mini_option` | sint64 | `mini_option_code` | `Integer` |
| 22 | `last_size` | sint64 | `last_size` | `BigInteger` |
| 23 | `bid` | float | `bid` | `PriceType()` |
| 24 | `bid_size` | sint64 | `bid_size` | `BigInteger` |
| 25 | `ask` | float | `ask` | `PriceType()` |
| 26 | `ask_size` | sint64 | `ask_size` | `BigInteger` |
| 27 | `price_hint` | sint64 | `price_hint` | `Integer` |
| 28 | `vol_24hr` | sint64 | `vol_24hr` | `BigInteger` |
| 29 | `vol_all_currencies` | sint64 | `vol_all_currencies` | `BigInteger` |
| 30 | `from_currency` | string | `from_currency` | `VARCHAR(32) C` |
| 31 | `last_market` | string | `last_market` | `VARCHAR(64) C` |
| 32 | `circulating_supply` | double | `circulating_supply` | `FactValueType()` |
| 33 | `market_cap` | double | `market_cap` | `BigNumType()` |

Ek kolonlar:

| kolon | tip | anlam |
|---|---|---|
| `payload_hash` | `ShortHashType()`, **PK** | §6.1 |
| `received_at` | `TsType()` NOT NULL | Çözümleme anı; gecikme ölçümü |
| `unknown_fields` | `RawJsonType()` NULL | Haritada bulunmayan proto alanları |

Dört seçim gerekçe ister:

**`options_type`, `mini_option`, `price_hint` `Integer`'dır,
`SmallInteger` değil.** Üçü de proto'da `sint64`'tür. `SmallInteger`'a
(±32.767) daraltmak, aralık dışı tek bir değerin `DataError` ile 500
sağlam tick'i geri almasına yol açardı — §7.5'in "bir bozuk kayıt yüzünden
batch patlamasın" ilkesiyle doğrudan çelişir. `Integer` bile taşarsa alan
`NULL` bırakılır ve `field_out_of_range` reddi yazılır.

**`quote_type_code` ve `market_hours_code` NOT NULL'dır** ve presence
kuralının istisnasıdır; gerekçe §7.2'de.

**`underlying_symbol` FK taşımaz.** Bir opsiyonun dayanak varlığı
`symbols` evreninde olmayabilir; FK koymak geçerli bir opsiyon tick'ini
reddetmek olurdu. `SymbolType()` yine kullanılır — genişlik uyuşmazlığı
ileride bir JOIN'i bozar. Bu invaryant testini kırmaz: collation testi
yalnızca FK kolonlarını tarar (`test_schema_invariants.py:18-24`).

**`raw_json` yoktur.** 33 alanın tamamı kolonlanmıştır; ham gövde bilgi
eklemez ama yılda ~1,5 milyar satır × ~400 bayt maliyeti olurdu. Şemanın
gerisinde kalma riski `unknown_fields` ile kapatılır (normalde `NULL`) ve
§12.1'deki statik alan testi yeni alanı zaten test zamanında yakalar.
`unknown_fields` ve `stream_outbox.payload` için çıplak `Text` değil
`RawJsonType()` kullanılır (`models/base.py:174-182`): jsonb anahtar
sırasını değiştirir ve `payload_hash`'in yeniden hesaplanabilirliğini
bozardı.

İndeksler: PK dışında `ix_live_ticks_received_at`. `symbol` PK'nin ilk
bileşenidir, ayrı indeks gerekmez.

### 6.3 `live_quotes` — sembol başına tek satır

`live_ticks` ile aynı alan kümesi, artı `updated_at`. PK yalnızca
`symbol`, FK taşır. Hypertable değildir. Kaynağı kuyruk değil, son-değer
kutusudur (K8).

**Geriye sarma koruması iki katmanlıdır.** Yalnızca veritabanı guard'ı
yetmez:

1. **Batch içi.** `PostgresRowWriter.write` INSERT'ten önce
   `dedupe_rows(rows, key_columns, monotonic_columns)` çağırır
   (`storage/persistence.py:138`) ve bu fonksiyon anahtar başına **son
   gelen satırı** tutar (`persistence.py:49-71`, "Keeps one row per key;
   the last one wins"). `live_quotes`'un anahtarı yalnızca `symbol`
   olduğundan, bir batch içinde sıra dışı gelen iki tick'ten *eskisi*
   kazanabilirdi. Bu yüzden `guard_column` `dedupe_rows`'a da geçirilir ve
   grup içinde **`max(guard)` taşıyan satır** seçilir.
2. **Veritabanı.** `ON CONFLICT ... DO UPDATE ... WHERE excluded.<guard> >
   <table>.<guard>`.

`TableWrite`'a eklenen alan:

```python
guard_column: str | None = None
```

Mevcut `monotonic_columns` bu işi göremez: o kolon bazlı
`GREATEST(current, new)` uygular (`storage/contracts.py:39-43`). Burada
gereken satır bazlı bir koşuldur — eski bir tick geldiğinde *hiçbir* kolon
güncellenmemelidir, yoksa satır iki farklı zamanın alanlarını karıştıran,
hiç var olmamış bir duruma dönüşür.

**`_verify()`'ın bu tabloda ne kanıtladığı sınırlıdır.** Doğrulama sorgusu
anahtarın varlığını sayar (`storage/persistence.py:198-226`); guard
tarafından reddedilmiş bir güncelleme de "verified" olarak döner. Yani
`live_quotes` için doğrulama satırın **varlığını** kanıtlar, **güncel
olduğunu** değil. `live_ticks` için ise tam kanıttır, çünkü orada her yazma
bir INSERT'tir. K1'in "her yazma okunarak doğrulanır" güvencesi bu
tabloda bu kadarıyla geçerlidir.

**Satır sayısı sabit değildir.** `yfin stream scope disable` satırı
`live_quotes`'tan da siler; aksi halde tablo yalnızca büyür ve
`yfin stream status` giderek bayatlayan satırlar gösterirdi.

### 6.4 `stream_scope`

| kolon | tip | anlam |
|---|---|---|
| `symbol` | `symbol_fk_column()`, PK | |
| `enabled` | `Boolean` NOT NULL, default `true` | Abonelik evrenine dahil mi |
| `archive` | `Boolean` NOT NULL, default `true` | `false` ise yalnızca `live_quotes`'a yazılır |
| `added_at` | `TsType()` NOT NULL | |
| `note` | `VARCHAR(255) C` NULL | |

`archive = false` hacim kontrolünün ince ayarıdır: canlı fiyat gerekiyor
ama tick tarihi gerekmiyorsa maliyet sıfıra iner. Yeni satırların
varsayılanı `yf_stream_archive_default`'tan gelir.

**Çözümleme semantiği `intraday_scope`'unkinden farklıdır.**
`intraday_scope`'ta kural "bu interval için en az bir satır var mı"dır ve
satır yoksa 1m için boş küme, diğer interval'ler için tüm evren anlamına
gelir (`pipeline/runner.py:181-207`); bu yüzden `bars scope disable` satırı
**silmez** (`cli/bars.py:79-99`). `stream_scope` ise sade bir küme
tanımıdır: satırın yokluğu her zaman "kapsam dışı" demektir, dolayısıyla o
tuzak burada yoktur.

**Purge etkileşimi.** `yfin symbols purge` (`cli/app.py:413-437`)
`symbol_scoped_tables()` üzerinde döner ve liste FK kenarlarından
türetilir (`models/__init__.py:142-150`). Üç sonucu vardır:

- `live_ticks`, `live_quotes`, `stream_scope` otomatik kapsanır — yani bir
  purge, milyarlarca satırlık bir hypertable'da sembol taraması yapar. Bu
  maliyet kabul edilir ama belgelenir.
- `stream_rejects` ve `stream_outbox` FK taşımadığı için listede görünmez
  ve öksüz satır bırakır. `models/__init__.py:134-140` bu kör noktayı
  zaten adlandırmış ve "FK taşıyamayan bir tablo yeniden eklenirse liste
  geri gelmeli" demiştir. Bu spec o tabloları eklediği için **manuel
  temizlik listesi geri gelir.**
- Akış süreci ayaktayken purge yapılırsa, silinen sembol için gelen bir
  sonraki tick FK ihlali üretir. `yfin symbols purge` bu yüzden akış
  süreci çalışırken (kilit sahibi varsa) uyarır; süzgeç önbelleği (§7.5)
  de en geç `yf_stream_rescan_seconds` içinde tazelenir.

### 6.5 `stream_outbox` — hypertable

```
create_hypertable('stream_outbox', by_range('created_at', INTERVAL '1 hour'),
                  create_default_indexes => FALSE)
```

| kolon | tip |
|---|---|
| `id` | `BigInteger`, `Identity()`, NOT NULL |
| `created_at` | `TsType()` NOT NULL, partition kolonu |
| `symbol` | `SymbolType()` NOT NULL, **FK yok** |
| `exchange` | `VARCHAR(32) C` NULL |
| `payload` | `RawJsonType()` NOT NULL |

PK `(created_at, id)`; indeks `ix_stream_outbox_id` (`id`).

**FK taşımama gerekçesi §6.7'ninkinden farklıdır.** Outbox'a giren her
tick zaten bilinen-sembol süzgecinden geçmiştir (§7.5), yani "bilinmeyen
sembol" sorunu yoktur. Gerekçe maliyettir: her INSERT `symbols` satırında
paylaşımlı kilit alır ve outbox yılda ~1,5 milyar satır yazar.
`live_ticks` bu bedeli veri bütünlüğü için öder; geçici bir yayın kuyruğu
ödemez.

Bir saatlik chunk aralığı `drop_chunks`'ın makul granülariteyle
çalışmasını sağlar. `payload`, Kafka'ya gidecek gövdenin birebir
kendisidir — relay yeniden serileştirme yapmaz.

### 6.6 `stream_relay_offset`

Tek satır: `id` (PK, `CHECK (id = 1)`), `last_published_id` (`BigInteger`
NOT NULL), `updated_at` (`TsType()` NOT NULL).

`CHECK (id = 1)` yalnızca ikinci bir *satırı* engeller. İki *relay
sürecine* karşı koruma advisory lock'tadır (§4.3).

### 6.7 `stream_rejects`

Düşen her şeyin kalıcı kaydı; `bar_gaps` felsefesinin aynısı — bir
tick'in düştüğü bilgisi o anda yazılmazsa sonsuza kadar kaybolur.

| kolon | tip |
|---|---|
| `id` | `BigInteger`, `Identity()`, PK |
| `received_at` | `TsType()` NOT NULL |
| `symbol` | `SymbolType()` NULL, **FK yok** |
| `reason` | `AsciiKeyType(24)` NOT NULL |
| `detail` | `Text` NULL |
| `raw_base64` | `Text` NULL |

**FK yoktur ve bu zorunludur:** `unknown_symbol` reddi tanımı gereği
`symbols`'ta olmayan bir sembol içindir; FK olsaydı reddin kaydı da FK
ihlaliyle reddedilirdi. `sync_run_items.symbol`'ün gerekçesinin aynısı
(`models/sync.py`).

| `reason` | anlam |
|---|---|
| `decode_failed` | Base64/protobuf çözümlemesi başarısız; `raw_base64` dolar |
| `no_timestamp` | `time <= 0`; PK bileşeni uydurulamaz |
| `unknown_symbol` | Normalizasyon sonrası `symbols` evreninde yok |
| `symbol_too_long` | `len(id) > SYMBOL_LENGTH` (§7.4) |
| `non_finite_field` | NaN/Inf alan; satır yazılır, alan `NULL` |
| `field_out_of_range` | Sayısal alan kolon aralığını aşıyor; alan `NULL` |
| `expire_date_range` | `expire_date` makul aralığın dışında; alan `NULL` |

Örnekleme: `(symbol, reason)` başına saatte
`yf_stream_reject_sample_per_hour` (100) satır.

**`queue_overflow` bu tabloya hiç yazılmaz** (§8.3). 500 sembol × 100
satır/saat = saatte 50.000 ek yazma demek olurdu ve bu, zaten tıkanmış
olan writer'ın transaction'ından geçerdi; yani sistemin çöktüğü anda yükü
artırırdı. Düşen tick sayısı yalnızca `stream_sessions.rows_dropped`
sayacında tutulur — sayaçlar örneklenmez.

### 6.8 `stream_sessions`

`sync_runs`'ın karşılığı. `StreamStatus(enum.StrEnum)` +
`values_callable=lambda e: [m.value for m in e]` ile tanımlanır —
projedeki tüm enum'ların kalıbı (`models/sync.py:75-86`).

| kolon | tip |
|---|---|
| `id` | `BigInteger`, `Identity()`, PK |
| `started_at`, `finished_at` | `TsType()` |
| `status` | `Enum(StreamStatus, name='stream_status')` |
| `connection_count`, `symbol_count` | `Integer` |
| `messages_received`, `rows_written`, `rows_rejected`, `rows_dropped` | `BigInteger` |

Sayaçlar writer thread'inde biriktirilir ve batch commit'iyle güncellenir;
her mesajda `UPDATE` atmak yazma yolunu ikiye katlardı.

### 6.9 `stream_connection_health`

| kolon | tip |
|---|---|
| `connection_key` | `AsciiKeyType(40)`, PK — `NMS` veya `NMS#0` |
| `session_id` | `BigInteger` NOT NULL, FK → `stream_sessions.id` |
| `state` | `AsciiKeyType(16)` — `connecting`, `open`, `reconnecting`, `closed` |
| `symbol_count` | `Integer` |
| `connected_at`, `last_message_at` | `TsType()` NULL |
| `heartbeat_at` | `TsType()` NOT NULL |
| `reconnect_count` | `Integer` NOT NULL default 0 |
| `last_error` | `Text` NULL |

`session_id` ve `heartbeat_at` ilk taslakta yoktu ve tablo bu yüzden çökme
sonrası **yalan söylerdi**: SIGKILL sonrası satırlar `state='open'` kalır
ve `yfin stream status` ölü bir sürecin bağlantılarını sağlıklı
gösterirdi. `heartbeat_at` her tarama turunda tazelenir; `status`,
`heartbeat_at` `2 × yf_stream_rescan_seconds`'tan eskiyse satırı **bayat**
işaretler ve sağlıklı saymaz.

## 7. Protokol çözümlemesi ve doğruluk

### 7.1 float32 → Decimal

Her fiyat alanı IEEE 754 binary32'dir. Protobuf bunu Python `float`'a
(binary64) genişletir: `232.35` olarak yollanan bir fiyat
`232.35000610351562` olarak okunur ve `NUMERIC(28,12)` bu artığı sadakatle
saklar. "Hatasız veri" şartı burada kazanılır ya da kaybedilir.

```python
def f32_decimal(value: float) -> Decimal:
    """float32'ye geri döndüğünde aynı biti veren EN KISA ondalık gösterim."""
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == value:
            return Decimal(text)
    return Decimal(repr(value))  # ulaşılamaz: binary32 garantisi 9 hane
```

`price_hint` yuvarlama için **kullanılmaz**, yalnızca saklanır: o Yahoo'nun
*gösterim* önerisidir, veri değil; yuvarlamayı ona bağlamak, hint'in
yanlış geldiği bir sembolde arşive kalıcı hata yazmak olurdu.

`double` alanlar (`circulating_supply`, `market_cap`) bu işlemden geçmez —
zaten binary64'tür, genişletme artığı yoktur.

NaN/Inf: alan `NULL`, satırın geri kalanı yazılır, `non_finite_field`
reddi. Tek bozuk alan yüzünden 32 sağlam alanı imha etmek yanlış olurdu.

### 7.2 Presence: 0 ile "gönderilmedi" ayırt edilemez

proto3'te `optional` işaretlenmemiş scalar alanların field presence'ı
yoktur: `HasField()` `ValueError` atar, `ListFields()` yalnızca
varsayılan-olmayanları verir, `MessageToDict` varsayılanları atlar. Wire
üzerinde `bid = 0.0` ile `bid`'in hiç yollanmamış olması aynı bayt
dizisidir. Bu protokolün özelliğidir; kod tarafında çözülemez.

**Genel kural:** varsayılan değerli alan `NULL` yazılır.

> `live_ticks.bid IS NULL`, "bid yok" demek değildir; **"bid ya yok ya da
> sıfır"** demektir. Protokol bu ayrımı taşımaz. API ve Kafka
> tüketicilerinin bunu bilmesi gerekir.

**İki istisna: `quote_type` ve `market_hours`.** Bu ikisi Yahoo enum
kodudur ve `0` **geçerli bir koddur** — `market_hours = 0` PRE_MARKET
demektir. Genel kural uygulansaydı tam da extended sayılması gereken
pre-market tick'lerinde `market_hours_code` `NULL` olurdu ve §10'un
`is_extended` türetimi imkânsızlaşırdı (`price_bars.is_extended`
NOT NULL'dır, `models/bars.py:146`). Bu yüzden iki kolon NOT NULL'dır ve
alan gelmediğinde `0` yazılır.

Ayrım şu ilkeye dayanır: **ölçüm alanlarında 0 anlamsızdır** (0 fiyat, 0
bid), **kod alanlarında 0 anlamlıdır**. Bu, protokolün taşımadığı bilgiyi
uydurmak değil; kodun anlam uzayında 0'ın meşru bir üye olduğunu kabul
etmektir.

`MessageToDict` kullanılmaz; `protocol.py` `PricingData` nesnesini
doğrudan okur. `preserving_proto_field_name` bayrağının davranışı kütüphane
sürümüne bağlıdır ve alan → kolon haritamız zaten açıktır.

### 7.3 Zaman

`time` `sint64`, **milisaniye** epoch → `ts_utc`. `time <= 0` ise satır
yazılmaz (`no_timestamp`): `ts_utc` PK bileşenidir, `received_at` ile
ikame etmek veriyi uydurmak olurdu.

`expire_date` de `sint64`'tür ama **saniye** epoch taşır (opsiyon vadesi).
Bu asimetri §13'ün 0. aşamasında ölçülür; iki birimi karıştırmak her
vadeyi 1970'e taşır. Ölçüm kesinleşene kadar: değer 1970–2100 aralığının
dışındaysa alan `NULL` ve `expire_date_range` reddi.

`received_at` çözümleme anında yazılır. `received_at - ts_utc` uçtan uca
gecikmedir ve akışın sağlığının tek gerçek göstergesidir — mesaj sayısı
değil.

### 7.4 Sembol normalizasyonu ve uzunluk

Gelen `id`, `symbols` ile karşılaştırılmadan **önce**
`nz.normalize_symbol()`'den geçirilir (`core/normalize.py:72-75`).

Bu adım ilk taslakta yoktu ve atlanamaz: `SymbolType()` COLLATE "C" ve
case-sensitive'dir (`models/base.py:30-35`), kod tabanının kuralı
"case-insensitivity yazma yolunda zorlanır"dır
(`datasets/symbols.py:68-73`, "this must be the single place normalization
happens") ve her giriş noktası bunu uygular (`cli/app.py:333`, `:402`,
`:420`, `:513`; `cli/bars.py:54`, `:89`). Normalizasyonsuz gelen `aapl`
`AAPL` ile eşleşmez ve geçerli bir tick `unknown_symbol` ile reddedilirdi.

Normalizasyon sonrası boş kalan `id` → `unknown_symbol`.

**Uzunluk savunması.** `len(id) > SYMBOL_LENGTH` (32,
`models/base.py:37-41`) ise satır **yazılmaz** ve `symbol_too_long` reddi
`raw_base64` ile kaydedilir. Sessiz kırpma (`nz.to_str`,
`core/normalize.py:96-102`) burada kabul edilemez: kırpılmış bir sembol
başka bir sembolün satırına yazılırdı ve `raw_json` tutulmadığı için
(§6.2) telafisi olmazdı. `underlying_symbol` taşarsa yalnızca o alan
`NULL` bırakılır — satır yine değerlidir.

### 7.5 Bilinmeyen sembol

Writer, batch'i yazmadan önce `SymbolLookup.known_symbols()`
(`storage/contracts.py:85-90`) ile süzer; bilinmeyenler `unknown_symbol`
ile reddedilir. `live_ticks.symbol` FK taşır, dolayısıyla süzgeçsiz tek
bir bilinmeyen sembol 500 sağlam tick'i geri alırdı. Bu,
`ItemStatus.UNKNOWN_SYMBOL`'ün akıştaki karşılığıdır.

Önbellek `yf_stream_rescan_seconds` TTL'lidir ve **kaçırma
toleranslıdır**: önbellekte bulunmayan bir sembol doğrudan reddedilmez,
tek seferlik gerçek bir sorgu yapılır. İlk taslak bunu yapmıyordu ve yeni
eklenmiş bir sembolün ilk TTL penceresindeki tüm tick'lerini kalıcı olarak
kaybettiriyordu — üstelik örnekleme sınırı yüzünden kaydın kendisi de
kırpılabilirdi.

`known_symbols()` `is_active` filtresi uygulamaz
(`storage/persistence.py:236-242`); `is_active` süzgeci abonelik evreninde
uygulanır (K4), yazma yolunda değil. Bu ayrım kasıtlıdır: bir sembol akış
sürerken pasifleştirilirse, bir sonraki tarama turuna kadar gelen
tick'leri atılmaz, yazılır.

## 8. Yazma yolu, backpressure ve batch

### 8.1 Writer thread ve iki invaryant

Tek writer thread.

**Invaryant 1 — tek writer.** Süreçte yazan tek bir thread vardır ve
batch'ler ardışık commit edilir; dolayısıyla `stream_outbox.id`'nin
`IDENTITY` sırası commit sırasıyla aynıdır. Relay'in `id > offset`
taramasının doğruluğu (§9.3) buna dayanır: eşzamanlı iki writer olsaydı,
küçük `id`'li bir satır büyük `id`'li birinden sonra commit edilebilir ve
offset onun üzerinden geçtiği için o satır hiç yayımlanmazdı.

**Invaryant 2 — `created_at` monotonluğu.** `stream_outbox.created_at`
writer thread'inde üretilir ve `id` ile monotondur; tick'in
`received_at`'inden **alınmaz**. Aksi halde gecikmiş bir tick düşük
`created_at` + yüksek `id` ile zaten yayımlanmış sayılan bir chunk'a düşer
ve `drop_chunks` onu yayımlanmadan düşürürdü (§9.3). Relay ilerlemesi `id`
üzerinden, temizlik `created_at` üzerinden çalıştığı için ikisinin uyumu
ancak bu invaryantla garanti edilir.

Her ikisi de yorum değil kısıttır: yazma yolunu paralelleştirmek isteyen
bir değişiklik önce relay'in ilerleme modelini değiştirmek zorundadır.
§12.1 birincisini testle sabitler.

### 8.2 Batch ve `COPY`

Batch `yf_stream_batch_size` (500) satıra **veya**
`yf_stream_batch_interval_ms` (250) süresine ulaşınca commit edilir. Süre
sınırı olmadan sakin bir piyasada son tick'ler dakikalarca beklerdi; sayı
sınırı olmadan yoğun bir açılışta tek transaction aşırı büyürdü. Ölçüm,
batch'i büyütmenin yalnızca %6 kazandırdığını gösterdi (§3.2) — küçük
batch bedava.

Tek transaction içinde, sırayla:

1. `CREATE TEMP TABLE ... ON COMMIT DROP` + `COPY` → `INSERT ... SELECT
   ... ON CONFLICT DO NOTHING` (`live_ticks`, K7)
2. `_verify()` key-existence sorgusu
3. (Kafka açıksa) aynı teknikle `stream_outbox` (K5)
4. Her `yf_stream_quotes_every_n_batches` (4) batch'te bir, son-değer
   kutusundan `live_quotes` guard'lı upsert (K8)
5. Örneklenmiş `stream_rejects` satırları
6. `stream_sessions` sayaç güncellemesi

Hepsi tek commit — outbox'ın atomikliği (K5) buradan gelir.

### 8.3 Kuyruk dolduğunda

Kuyruk `queue.Queue(maxsize=yf_stream_queue_maxsize)`. Event loop
`put_nowait`, writer `get`.

Üç seçenek, üçüncüsü seçilir:

1. **Bloke ol.** `pipeline/runner.py:800`'ün yaptığı budur ve orada
   doğrudur — üretici bizim worker'ımızdır ve yavaşlaması kimseyi
   ilgilendirmez. Burada yanlıştır: event loop bloke olursa ping/pong
   cevaplanmaz, Yahoo bağlantıyı düşürür ve *tüm* semboller durur.
2. **Okumayı duraklat** (TCP backpressure). Aynı sonuç, daha yavaş.
3. **Düşür ve say.** Tick düşürülür, atomik bir sayaç artar, writer bir
   sonraki batch'te sayacı `stream_sessions.rows_dropped`'a ekler.

Üçüncü seçenek "eksiksiz veri" şartını ihlal etmez: alternatif çökmek ve
*bütün* sembolleri kaybetmektir. Düşen tick'in düştüğü **sayılır**.

İki şey ilk taslaktan **çıkarıldı**, çünkü tek-kuyruk mimarisinde fiziksel
olarak imkânsızdı:

- "`live_quotes` güncellemesi korunur" iddiası. Kuyruk event loop ile
  writer arasındaki tek kanaldı; kuyruk doluysa tick writer'a hiç
  ulaşmazdı. Bu artık **başka bir mekanizmayla** doğrudur: `live_quotes`
  son-değer kutusundan beslenir (K8), kuyruktan değil.
- `queue_overflow` reddinin `stream_rejects`'e yazılması. O satır da aynı
  dolu kuyruktan geçmek zorundaydı; ayrıca sistemin çöktüğü anda yazma
  yükünü artırırdı (§6.7).

**Kuyruk boyutlandırması.** §3.1'in rakamları 500 sembol × 0,5 msg/sn ≈
250 mesaj/sn verir. 50.000'lik bir kuyruk bu yükte ~200 saniyelik tampon
demektir — ilk taslaktaki "8 saniye" 25 kat yanlıştı. 200 saniye
gereğinden uzundur: writer 200 saniye boyunca yetişemiyorsa sorun kuyruk
derinliği değildir. Varsayılan **10.000**'e (~40 saniye) çekilir ve
§13'ün 0. aşamasındaki gerçek mesaj hızıyla sabitlenir.

### 8.4 Kapanış

SIGTERM: bağlantılar kapatılır, kuyruk boşaltılır, son batch commit
edilir, `stream_sessions` `ok` ile kapatılır, kilit bırakılır. Kuyrukta
veri varken çıkılmaz.

SIGKILL: yalnızca kuyruktaki commit edilmemiş tick'ler kaybolur;
`stream_sessions` açık kalır ve bir sonraki başlangıç onu `failed` ile
kapatır.

## 9. Kafka

### 9.1 Konu ve anahtar düzeni

**Konu = exchange, partition anahtarı = sembol.** İstenen iki eksenin
ikisi de karşılanır; biri konuda, biri anahtarda.

- `yfin.ticks.NMS`, `yfin.ticks.IST`, `yfin.ticks.unknown`.
- Sembol başına konu **değil**: 5.000+ konu broker metadata'sını ve
  controller'ı çökertir.
- Anahtar sembol olduğu için aynı sembolün tüm mesajları aynı partition'a
  düşer → **sembol bazında sıra garantisi**.

**Konu adı `symbols.exchange`'ten türetilir, tick'in kendi `exchange`
alanından değil.** Üç aday vardı ve seçim gerekçelidir: `symbols.exchange`
yazma yolunda `.upper()` ile normalize edilir (`datasets/symbols.py:39-45`,
`:74-75`), tick'in proto alanı ise hamdır. Ham değer bir kez `nms`, bir kez
`NMS` gelirse iki ayrı konu doğar ve sembol bazlı sıra garantisi ikiye
bölünürdü. Ham alan yalnızca `live_ticks.exchange`'e yazılır;
`stream_outbox.exchange` normalize kaynaktan gelir.

Sanitize: `[a-zA-Z0-9._-]` dışındaki karakterler `-` olur. **`unknown`
etiketi hem topolojide hem konuda aynıdır** — ilk taslak topolojide
`__unknown__`, konuda `unknown` kullanıyordu, ve `yfin.ticks.__unknown__`
ayrıca Kafka'nın "aynı adda hem `.` hem `_`" metrik çakışması uyarısını
tetiklerdi.

`yf_kafka_topic_pattern` varsayılanı `"yfin.ticks.{exchange}"`. Tek konu
isteyen `"yfin.ticks"` yazar.

Konular otomatik oluşturulmuş varsayılmaz: `yfin stream relay` başlangıçta
gerekli konuların varlığını doğrular ve yoksa yüksek sesle hata verir.
`auto.create.topics.enable`'a bel bağlamak, yanlış partition sayısıyla
oluşmuş bir konuda sıra garantisini sessizce kaybetmek demektir.

### 9.2 Değer formatı

JSON, alan adları proto ile birebir. Protobuf değil: tüketici çeşitliliği
JSON'u haklı çıkarır ve `stream_outbox.payload` zaten bu gövdedir, relay
yeniden serileştirme yapmaz.

Gövde `payload_hash` ve `received_at`'i de taşır, böylece tüketici
`(symbol, ts_utc, payload_hash)` ile idempotent olabilir (§9.4).

### 9.3 Relay döngüsü

`confluent-kafka` (librdkafka); relay senkron bir süreçtir, `aiokafka`
yersiz olurdu.

**Producer yapılandırması sıra garantisinin parçasıdır ve zorunludur:**

```
enable.idempotence=true      # in-flight istekleri güvenli tutar, tekrarı eler
acks=all
```

Bunlar olmadan §9.1'deki sıra garantisi tutmaz: librdkafka'da varsayılan
`max.in.flight.requests.per.connection > 1`'dir ve bir batch'in yeniden
denenmesi aynı partition içinde sırayı bozar. İlk taslak hiçbir producer
ayarı vermiyordu.

Döngü:

1. `SELECT ... FROM stream_outbox WHERE id > :last ORDER BY id LIMIT
   :batch` — doğruluğu §8.1'in iki invaryantına dayanır.
2. `produce()` + `flush()`; **tüm teslim callback'leri başarılı olmadan
   ilerlenmez.**
3. `stream_relay_offset.last_published_id` güncellenir.
4. Periyodik `drop_chunks('stream_outbox', older_than => ...)`, yalnızca
   tamamı yayımlanmış chunk'lar için.

### 9.4 Garanti: at-least-once

2. ve 3. adım arasında çökülürse aynı mesajlar yeniden yayımlanır.
Tüketici `(symbol, ts_utc, payload_hash)` ile idempotent olabilir — bu
üçlü zaten `live_ticks`'in birincil anahtarıdır ve mesajda taşınır.

Exactly-once denenmez: Kafka transaction'ı ile PostgreSQL transaction'ı
arasında iki fazlı commit kurmak, kazanılan garantiye değmeyecek bir
karmaşıklıktır ve tüketici tarafı zaten idempotent olmak zorundadır.

### 9.5 Opsiyonellik

`kafka = ["confluent-kafka>=2.5"]` extra'dır. `stream/kafka.py` import'u
guard'lar. `yf_kafka_enabled = true` iken extra kurulu değilse süreç
**başlangıçta** hata verir; sessizce devre dışı kalmak, operatörün
Kafka'ya veri aktığını sanarak saatlerce beklemesi demektir.

Kapalıyken: outbox'a hiç yazılmaz, relay başlatılmaz, `confluent_kafka`
import edilmez.

## 10. `bar_gaps` mutabakatı

`yfin stream reconcile` — ayrı komut, `yfin_sync` kilidi altında (§4.3):

1. `bar_gaps`'ten `resolved_at IS NULL` ve `bar_interval = '1m'` satırları
   okunur. **`retention_expired` dahildir** (K6); `fetch_failed` de
   dahildir, ama onlar zaten Yahoo'dan çekilebildiği için tick yalnızca
   daha hızlı bir yoldur.
2. Pencere dakikalara bölünür. `fetch_failed` pencereleri gün
   granülaritesinde yazılır (`datasets/bars.py:392-393`,
   `datetime.combine(window_start, datetime.min.time())`), yani bir gap
   satırı günlerce sürebilir ve **içinde zaten yazılmış gerçek barlar
   bulunabilir**. İlk taslak tek bir bar türetiyormuş gibi yazılmıştı.
3. **Yalnızca `price_bars`'ta satırı bulunmayan dakikalar** için bar
   türetilir. Bu koşul zorunludur: koşulsuz bir upsert, `volume`'u kasten
   `NULL` bırakan türetilmiş satırlarla gerçek barların doğru hacmini
   silerdi.
4. Türetim yalnızca `price IS NOT NULL` tick'ler üzerinde çalışır; bir
   dakikanın tüm tick'lerinde `price` `NULL` ise o dakika atlanır
   (`price_bars.close` NOT NULL'dır, `models/bars.py:137`).
   `open`/`close` ilk ve son tick, `high`/`low` uç değerler.
5. `volume` **türetilmez**, `NULL` bırakılır: `day_volume` kümülatiftir ve
   dakikalık hacme çevrilmesi iki ardışık dakikanın tam gözlemini
   gerektirir; eksik tick'te bu yanlış olur. OHLC dördü de doğrudur, hacim
   dürüstçe "bilinmiyor" işaretlenir.
6. `is_extended`, `market_hours_code`'dan türetilir (`1 = REGULAR`
   dışındaki her şey `true`). §7.2'nin istisnası bunu mümkün kılar.
7. `local_date` `symbols.timezone` üzerinden `nz.to_local_date` ile
   hesaplanır. Bu adım zorunludur ve atlanamaz: kolon NOT NULL'dır
   (`models/bars.py:132`) ve `core/normalize.py:190` UTC'ye çevirip tarih
   almayı özellikle reddeder — "Converting to UTC first and taking the
   date shifts it back a day for positive-offset exchanges (BIST, Tokyo)".
   **`symbols.timezone` `NULL` olan sembol için gap kapatılmaz**; komut
   bunları sayıp raporlar.
8. `bar_gaps.resolved_at` doldurulur ve yeni `resolved_by` kolonuna
   `ticks` yazılır.

8. adımdaki kolon yenidir ve gereklidir: ilk taslak `reason`'ı
`derived_from_ticks` yapmayı öneriyordu, ama `bar_gaps` yazımının
`update_columns`'ı `("gap_end_utc", "detected_at", "reason")`'dır
(`datasets/bars.py:410-413`) — aynı anahtar bir sonraki sync'te yeniden
tespit edilirse `reason` `fetch_failed`'e geri döner ve "bu pencere
tick'ten geldi" bilgisi kaybolurdu. `resolved_by` `update_columns`
kapsamında değildir.

## 11. CLI ve ayarlar

### 11.1 Komutlar

```
yfin stream run                                  # supervisor, ön planda
yfin stream status                               # sağlık, gecikme, kuyruk
yfin stream scope add SYMBOL... [--no-archive]
yfin stream scope disable SYMBOL...
yfin stream scope list [--exchange NMS]
yfin stream relay                                # outbox -> Kafka
yfin stream reconcile [--dry-run]
```

`run` ve `relay` ön planda çalışır ve SIGTERM'e uyar; kendini daemonize
etmez — systemd, Docker veya Kubernetes altında koşmak için tasarlanmıştır.

`status` gösterir: bağlantı başına durum, son mesaj zamanı ve **bayatlık**
(§6.9), kuyruk doluluğu, `received_at - ts_utc` p50/p95, son bir saatteki
reddedilen ve düşürülen sayıları, (etkinse) relay'in outbox gerisindeki
gecikmesi.

İki CLI ayrıntısı mevcut kalıplardan gelir:

- **`scope add`, `exchange IS NULL` olan sembolleri sayıp uyarır.**
  `yfin symbols add` yalnızca `symbol` + `is_active` yazar
  (`cli/app.py:340-342`); `exchange` ilk sync'e kadar `NULL`'dır. Uyarı
  olmadan bu semboller sessizce `unknown` bağlantısına düşer ve operatör
  bunu ancak `stream status`'ta fark ederdi. `_filtered_symbols`'ün
  NULL-trap uyarısının aynısı (`cli/app.py:166-192`).
- **`scope list --exchange` `symbols` ile JOIN eder** (`stream_scope`'ta
  `exchange` kolonu yoktur) ve değeri **girdide** `.upper()` eder, kolonu
  `func.upper()`'a sarmaz — sarmak `ix_symbols_exchange`'i kullanılamaz
  hale getirirdi (`cli/app.py:126-158`, `models/symbols.py:16`).

### 11.2 Ayarlar

Yeni bir `stream` grubu `SETTING_GROUPS`'a eklenir (`core/config.py:43-55`);
`test_settings_split.py:59` bilinmeyen grubu reddeder ve
`test_settings_split.py:24-26`'daki `SNAPSHOT_TOTAL` /
`SNAPSHOT_DB_MANAGED` sabitleri güncellenir.

**Neden `YFAPI_` kalıbı izlenmiyor:** salt-okuma API spec'i kendi
`BaseSettings`'ini kurmuştu, çünkü orada bir imzalama anahtarı vardı ve
"operatör panelinden JWT audience değiştirilemez" kuralı gerekiyordu.
Akış ayarlarının hiçbiri sır değildir; hepsi tam olarak `yf_max_shards`
ile aynı türden operasyonel ayarlardır ve aynı tabloya girerler. Kafka
SASL kimlik bilgileri gerekirse (§14) o zaman `yf_proxy_secret_key`
kalıbıyla ortamda kalırlar.

| ayar | varsayılan |
|---|---|
| `yf_stream_enabled` | `false` |
| `yf_stream_max_connections` | `16` |
| `yf_stream_max_symbols_per_connection` | `0` (kapalı, K3) |
| `yf_stream_queue_maxsize` | `10000` |
| `yf_stream_batch_size` | `500` |
| `yf_stream_batch_interval_ms` | `250` |
| `yf_stream_quotes_every_n_batches` | `4` |
| `yf_stream_idle_timeout_seconds` | `300` |
| `yf_stream_reconnect_max_seconds` | `60` |
| `yf_stream_archive_default` | `true` |
| `yf_stream_reject_sample_per_hour` | `100` |
| `yf_stream_rescan_seconds` | `60` |
| `yf_kafka_enabled` | `false` |
| `yf_kafka_bootstrap_servers` | `""` |
| `yf_kafka_topic_pattern` | `"yfin.ticks.{exchange}"` |
| `yf_kafka_relay_batch` | `1000` |

Supervisor bunları §4.4'teki açık yeniden okuma yoluyla tazeler.

## 12. Test stratejisi

### 12.1 unit (veritabanı yok, dış ağ yok)

- **33 alanın tamamının haritalandığını statik doğrulayan test:**
  `PricingData.DESCRIPTOR.fields` ile kolon haritası karşılaştırılır. Yeni
  bir proto alanı testi kırar; alan sessizce düşmez.
  `test_schema_invariants`'ın kültürünün akıştaki karşılığı.
- `f32_decimal` property testi: 10.000 rastgele binary32 için round-trip
  ve "daha kısa gösterim yok" iddiası.
- Presence kuralı **ve iki istisnası** (§7.2): `market_hours = 0` gelen
  bir mesajda `market_hours_code` `0` yazılmalı, `NULL` değil.
- Sembol normalizasyonu, uzunluk savunması, ms/sn zaman dönüşümleri,
  `expire_date` aralığı, NaN/Inf, sayısal taşma.
- Sembol → bağlantı eşlemesinin determinizmi (PYTHONHASHSEED'den
  bağımsız), tavan/birleştirme ve bölme mantığı.
- `guard_column`'ın `dedupe_rows` içindeki etkisi: aynı batch'te sıra dışı
  gelen iki tick'ten **yenisi** seçilmeli (§6.3).
- Backoff, idle watchdog (özellikle `last_message_at IS NULL` iken
  tetiklenmediği), batch eşikleri, kuyruk taşma politikası.
- **Tek-writer invaryantı (§8.1):** supervisor'ın tam olarak bir writer
  thread başlattığı. Bu testin tek işi, relay'in ilerleme modelini sessizce
  geçersiz kılacak bir paralelleştirmenin fark edilmeden girmesini
  engellemektir.
- **Loopback WebSocket sunucusuyla** reconnect / heartbeat / idle
  senaryoları.

Son madde "unit = no database, no network" kuralının bilinçli
istisnasıdır: reconnect ancak gerçek bir soketle test edilebilir ve
mock'lanmış bir reconnect testi, upstream'de tam da bu yüzden gözden kaçmış
hatayı (K2, madde 1) bir kez daha kaçırırdı. Sunucu `127.0.0.1`'de dinler;
dış ağ erişimi yoktur.

**Altyapı:** bu testler `async def`'tir. `pyproject.toml`'un `dev`
extra'sına `pytest-asyncio` eklenir ve `[tool.pytest.ini_options]`'a
`asyncio_mode = "auto"` yazılır. Bugün ikisi de yoktur ve çıplak pytest
`async def` testlerini **sessizce atlar** — yani testler yeşil görünürken
hiç koşmazdı.

### 12.2 repo (gerçek PostgreSQL + TimescaleDB)

- Hypertable oluşumu ve chunk aralıkları; `alembic revision
  --autogenerate` boş diff üretir.
- PK üçlüsü: aynı payload iki kez → 1 satır; aynı milisaniyede farklı
  payload → 2 satır; yalnızca `unknown_fields` farklı → 2 satır (§6.1).
- `COPY` → staging → `INSERT ... SELECT` yolunun dedup ve FK davranışı.
- `guard_column`'lı upsert'ün geri sarmayı reddetmesi (hiçbir kolonu
  güncellemediği).
- Bilinmeyen sembolün batch'i patlatmaması; önbellek kaçırmasında tek
  seferlik sorgunun çalışması.
- Outbox → relay offset ilerleyişi, `drop_chunks` sonrası tutarlılık,
  relay yeniden başlatıldığında yinelenen yayın (kabul edilen
  at-least-once davranışı).
- Reconcile: yalnızca boş dakikaların yazıldığı, gerçek barların
  `volume`'unun korunduğu, `timezone` `NULL` sembolün atlandığı,
  `resolved_by`'ın bir sonraki gap yazımında ezilmediği.
- **`test_schema_invariants.py` genişletmesi.** Sekiz tablonun tamamı
  bugünkü `test_child_tables_inherit_their_parent_timestamp`'i kırar
  (`tests/unit/test_schema_invariants.py:169-182`): hiçbirinde
  `fetched_at` yoktur ve `symbols` dışında FK ebeveyni yoktur.
  `intraday_scope` bu yüzden zaten `exempt` listesindedir (`:156`).
  Sekizi de gerekçeleriyle `exempt`'e eklenir — bu tablolar bir kaynak
  tarihi taşımaz, kendi olay zamanlarını taşır.

### 12.3 live (`@pytest.mark.live`)

Gerçek Yahoo WS'e 60 saniye bağlanır, en az N mesaj alır ve hepsinin
yazıldığını doğrular.

Assertion **"`stream_rejects` boş kalmalı" değildir** — bu doğası gereği
kırılgandır: `non_finite_field`, `expire_date_range` ve `unknown_symbol`
tamamen upstream verisine bağlıdır ve 60 saniyelik gerçek bir bağlantıda
sıfır kalacaklarının garantisi yoktur. Kontrol edilebilir koşul:
**`decode_failed` kaydı olmamalı.** Çözümlemenin doğruluğu bizim
sorumluluğumuzdur; Yahoo'nun veri kalitesi değil.

`yfin_stream` kilidi `yfin_sync`'ten ayrı olduğu için bu test mevcut LIVE
testleriyle paralel koşabilir.

## 13. Uygulama aşamaları

**Aşama 0 — Ölçüm (açık piyasada).** Mevcut ölçümler
(`docs/measurements/websocket.md`) Pazar günü alındı: yazma tarafı
eksiksizdir, akış tarafı değildir. Açık piyasada ölçülecekler:

- Mesaj/saniye, sembol ve exchange kırılımında.
- 33 alanın doluluk oranı — §7.2'nin presence kararını ve iki istisnasını
  doğrular.
- float32 artık dağılımı.
- `expire_date` biriminin doğrulanması (§7.3).
- `received_at - ts_utc` gecikme dağılımı.
- **`_verify()`'ın çok chunk'lı hypertable üzerindeki planı ve süresi** —
  §3.2'nin açık bıraktığı tek soru.
- `yfin symbols exchanges` çıktısı; `yf_stream_max_connections`
  varsayılanı gerçek kardinaliteden sonra sabitlenir.
- Uzun süreli bağlantıda throttle / ban davranışı.

§11.2'deki varsayılanlar bu ölçümden sonra sabitlenir.

1. `protocol.py` + tipler + unit testler (`pytest-asyncio` dahil).
2. `models/stream.py` + `timescale_ddl()` genişletmesi + `conftest.py`
   bağlantısı + migration + `test_schema_invariants` `exempt`
   güncellemesi + repo testler.
3. `connection.py` + `supervisor.py` + `repository.py` + loopback testleri.
4. `writer.py` (`COPY` yolu) + backpressure + `stream_scope` +
   `run` / `status` / `scope` komutları.
5. `TableWrite.guard_column` (+ `dedupe_rows` etkisi) + `live_quotes` +
   son-değer kutusu.
6. `stream_outbox` + `relay.py` + `kafka.py` + `kafka` extra +
   `yfin stream relay`.
7. `reconcile.py` + `bar_gaps.resolved_by` + `models/bars.py`'deki
   "retention_expired stays NULL" yorumunun güncellenmesi +
   `yfin stream reconcile`.
8. `SETTING_GROUPS` + settings snapshot testleri; `pyproject.toml`
   (`websockets`, `protobuf`, `kafka` extra, `[[tool.mypy.overrides]]`
   içine `confluent_kafka`); README'nin `WebSocket streaming` satırı
   (TODO → In progress) **ve o satırdaki outbound-socket cümlesinin ayrı
   spec'e taşındığının belirtilmesi** (`README.md:142`);
   `docker-compose.yml` Kafka servisi; CI.

Aşama 6'ya kadar Kafka'sız tam çalışan bir sistem vardır; her aşama kendi
başına yeşil bırakılabilir.

**`mypy --strict` notu:** `yfinance` `py.typed` taşımaz ve
`ignore_missing_imports` altındadır (`pyproject.toml:94-96`), dolayısıyla
`PricingData` `Any` olarak görünür ve strict'in `warn_return_any` kuralı
`protocol.py`'de sürtünme yaratır. `protocol.py` bu yüzden protobuf
nesnesinden okuduğu her değeri açık bir tip dönüşümünden geçirir; `Any`
paketin sınırını geçmez.

## 14. Açık maddeler (kapsam dışı, bilinerek)

- **`live_ticks` için retention/compression politikası.** Tablo
  append-only olduğu için compression'ın mevcut engeli (rescale'in geçmiş
  satırları yeniden yazması) burada yoktur. §3.1'in ~500–600 GB/yıl
  tahmini bu kararı acil kılar, ama ayrı bir ölçüm gerektirir.
- **Dışa dönük müşteri WebSocket'i** (§1).
- **Tick verisinin salt-okuma API'sinden sunulması.** `DataFamily`'ye yeni
  bir üye eklenip eklenmeyeceği — yoksa `BARS` altında mı kalacağı — o
  spec'in kararıdır.
- **Kafka SASL/TLS.** Bugün yalnızca düz `bootstrap.servers`.
- **`stream_relay_offset` ve `stream_connection_health`'in
  birleştirilmesi.** İkisi de küçük tablolardır ve `session_id`
  eklendikten sonra ikincisi `stream_sessions`'ın bir uzantısı gibi
  davranır. Şimdilik ayrı kalırlar, çünkü yaşam döngüleri farklıdır: biri
  süreç ömrü kadar, diğeri kalıcı bir offset.
