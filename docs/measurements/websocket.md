# Canlı WebSocket akışı — ölçülen davranış

Ölçüm tarihi: 2026-09-06 (**Pazar** — ABD ve Avrupa borsaları kapalı).
Ortam: PostgreSQL 18.6 + TimescaleDB 2.29.2 (`timescale/timescaledb:2.29.2-pg18`),
yfinance 1.7.0, macOS, yerel Docker.

Her kayıt, `docs/superpowers/specs/2026-09-06-websocket-streaming-design.md`
tasarımındaki bir sayının ya da iddianın dayanağıdır.

> **Kapsam uyarısı.** **Yazma tarafı** eksiksizdir ve haftanın gününden
> bağımsızdır. **Akış tarafı** Pazar günü alınmıştır: yalnızca 7/24 işlem
> gören kripto sembolleri mesaj üretmiştir, dolayısıyla ABD hisseleri için
> mesaj hızı ve alan doluluk oranları **ölçülmemiş** sayılır. Bunlar açık
> piyasada tekrarlanmalıdır (spec §13, Aşama 0).
>
> Abonelik limiti ve bağlantı davranışı ise haftanın gününden bağımsızdır
> ve ölçülmüştür — ilk turdaki hatalı sonuç ve düzeltmesi aşağıda açıkça
> kaydedilmiştir.

## Abonelik limiti: 100 sembol / bağlantı

### Önce, düzeltilen bir hata

İlk ölçüm turu "Yahoo'nun sembol limiti yok, 50.000 sembol kabul edildi"
sonucunu verdi. **Bu sonuç yanlıştı ve yöntem hatasından kaynaklandı.**

Test, listenin *başına* 7/24 akan kripto sembolleri koyuyor ve "mesaj
geliyor mu" diye bakıyordu. Sunucu listeyi ilk 100 girdiye kırptığı için
o kanaryalar her zaman kırpma noktasının içinde kalıyordu; akış devam
ediyor görünüyordu, oysa 100. sembolden sonrasına hiç abone
olunmamıştı.

Belirti kayıtlardaydı ama yanlış okundu: 10.000 ve 50.000 seviyelerinde
"farklı sembol" sayısı 8–12'de kalmıştı. Bu, Pazar gününe verildi; gerçek
sebep sessiz kırpmaydı.

### Kanarya yöntemi ve gerçek limit

Doğru yöntem kanaryayı listenin **sonuna** koymaktır: kırpma varsa
kanarya kırpma noktasının dışında kalır ve susar.

| Liste (kanarya **sonda**) | Toplam sembol | Veri gelen kanarya |
|---|---:|---:|
| 50 dolgu + 5 kanarya | 55 | 5/5 |
| 90 dolgu + 5 kanarya | 95 | 5/5 |
| 94 dolgu + 5 kanarya | 99 | 5/5 |
| **95 dolgu + 5 kanarya** | **100** | **5/5** |
| **96 dolgu + 5 kanarya** | **101** | **4/5** |
| 120 dolgu + 5 kanarya | 125 | 0/5 |
| 200 dolgu + 5 kanarya | 205 | 0/5 |
| *Kontrol:* 5 kanarya **başta** + 200 dolgu | 205 | 5/5 |

**Limit tam olarak 100 semboldür.** 101. sembolde tam bir kanarya düşüyor
— sunucu listeyi sırayı koruyarak ilk 100 girdiye kırpıyor. Kontrol satırı
"ilk gelen kazanır" semantiğini doğruluyor ve ilk turdaki hatanın da
açıklamasıdır.

Kırpma **hiçbir geri bildirim üretmez**: hata frame'i yok, bağlantı
kapanmıyor, onay yok. Bir istemci 10.000 sembole abone olduğunu sanarak
100 sembol alabilir ve bunu asla fark etmeyebilir.

### Limitin diğer özellikleri

Aşağıdakiler ilk turda bağımsız bir araştırma ajanı tarafından ölçüldü;
yukarıdaki kırpma noktası bu oturumda birebir doğrulandı.

| Özellik | Gözlem |
|---|---|
| Kapsam | Mesaj başına değil, **bağlantı başına kümülatif**. 100 sembol + ayrı frame'de 5 kanarya → kanaryalar susar. |
| Öncelik | **İlk gelen kazanır.** Sonraki abonelikler mevcutları tahliye etmez. |
| `unsubscribe` | Slotları **geri açar**; rotasyon mümkün. |
| Geçersiz semboller | **Slot tüketir.** 95 uydurma + 5 kanarya → 5/5; 200 uydurma + 5 kanarya → 0/5. Kirli sembol listesi doğrudan kapasite yakar. |
| Frame boyutu | Sınırlayıcı değil: 13.193 sembollük 103 KB'lık frame kabul edildi (ve ilk 100'ü işlendi). |

### Bozuk mesaj bağlantıyı sessizce kapatır

| Gönderilen | Sonuç |
|---|---|
| `{"subscribe": "BTC-USD"}` (liste değil) | **bağlantı kapandı** |
| `hello world` (JSON değil) | **bağlantı kapandı** |
| `{"bogus_action": ["X"]}` | **bağlantı kapandı** |
| `{"subscribe": [null]}` | **bağlantı kapandı** |
| `{"subscribe": []}` | hayatta |
| `{}` | hayatta |
| `{"unsubscribe": ["hiç-abone-olunmamış"]}` | hayatta |

Kapanma status kodu bile taşımıyor (1005). Tek bir `None` sembol
sızıntısı bütün bağlantıyı düşürür.

## Eşzamanlı bağlantı ve istemci modeli

10.000 sembol ÷ 100 = **en az 100 eşzamanlı bağlantı**. Bu sayının
ulaşılabilir olup olmadığı doğrudan ölçüldü.

### Yahoo tarafı: 110 bağlantı sorunsuz

Yalnızca soket açan (okuma döngüsü olmayan) bir istemciyle, tek IP'den:

| Açma biçimi | Kurulan |
|---|---|
| 110 eşzamanlı, aralıksız | **110/110** |
| 110 kademeli (100 ms arayla) | **110/110** |

Yahoo tarafında ne bağlantı reddi, ne hız sınırı, ne de kademeli açma
gereği görüldü.

### İstemci tarafı: thread-per-connection 110'da çöküyor

Aynı iş — 110 bağlantı kur, abone ol, 20 saniye mesaj oku — iki modelde:

| Model | Kurulan | Veri alan | Mesaj |
|---|---:|---:|---:|
| A. Thread başına bağlantı (`websockets.sync`) | **19**/110 | 19 | 171 |
| B. Tek asyncio event loop | **110**/110 | 110 | 1.210 |

`ulimit -n` 1.048.576'dır; dosya tanıtıcı sınırı değildir. Senkron model
bağlantı başına bir okuma thread'i (artı zamanlayıcı) açar ve 110
bağlantıda ~330 thread'e çıkar; bu ölçekte bağlantıların çoğu hiç
kurulamıyor.

**Bu, tasarımın K1 kararının ölçülmüş gerekçesidir:** 10.000 sembol
hedefi tek bir asyncio event loop ile ulaşılabilir, bağlantı başına
thread ile ulaşılamaz.

## Akışın doğası: tick değil, saniyelik snapshot

BTC-USD için ardışık mesaj araları: `5.0, 4.0, 4.9, 5.0, 5.0, 5.0, 6.0,
3.0, 6.0, 5.0, 4.1, 5.9, 5.1, 5.0, 6.1` saniye — **tümü tam saniye
katları**, medyan 6,0 sn. `time` alanı milisaniye cinsindendir ama hep
`...000` ile biter.

BTC saniyede yüzlerce kez işlem görür; bu düzenlilik ancak sunucu tarafı
örnekleme ile açıklanır. Bağımsız bir kayıt da aynı yöne işaret ediyor:
bir NSE hissesinde `day_volume` ardışık mesajlar arasında 1.000–1.200
adet artıyor, yani mesaj başına birden çok işlem
([yfinance#319](https://github.com/ranaroussi/yfinance/pull/319#issuecomment-842056388)).

**Sonuç: gelen veri tick-by-tick değil, ~1 saniyelik ızgaraya oturtulmuş
birleştirilmiş (consolidated) snapshot'tır.** Bu, tasarımın `volume`
alanını türetmeme kararını doğrudan doğrular: `day_volume` kümülatiftir ve
mesajlar arasında binlerce adet atlar, dolayısıyla dakikalık hacme
güvenilir biçimde çevrilemez.

Yük altında tekil sembol hızı düşmüyor: 2 sembollük bağlantıda BTC 23
mesaj aldı, 99 sembollük bağlantıda yine 23. Darboğaz bant genişliği
değil, 100'lük kota.

**15 saniyelik yeniden-abonelik heartbeat'i gerekli değil.** 240 saniye
boyunca hiçbir mesaj gönderilmeden bağlantı açık kaldı ve akış sürdü.
yfinance her 15 saniyede tüm seti yeniden gönderir; bu hem gereksizdir hem
de 100'lük kırpmayı kalıcılaştırır. (4 dakikadan uzun idle test edilmedi.)

## Enum kodları

`pricing.proto` bu alanları çıplak `int32` olarak tanımlar; anlamları
istemcinin haritalaması gerekir. Değerler
[yliveticker/yaticker.proto](https://github.com/yahoofinancelive/yliveticker/blob/main/yliveticker/yaticker.proto)
kaynağından, kısmen ölçümle doğrulandı:

```
MarketHoursType: PRE_MARKET=0  REGULAR_MARKET=1  POST_MARKET=2
                 EXTENDED_HOURS_MARKET=3
QuoteType:       NONE=0 ALTSYMBOL=5 HEARTBEAT=7 EQUITY=8 INDEX=9
                 MUTUALFUND=11 MONEYMARKET=12 OPTION=13 CURRENCY=14
                 WARRANT=15 BOND=17 FUTURE=18 ETF=20 COMMODITY=23
                 ECNQUOTE=28 CRYPTOCURRENCY=41 INDICATOR=42 INDUSTRY=1000
OptionType:      CALL=0  PUT=1
```

Ölçümle doğrulanan: BTC-USD/ETH-USD → `quote_type=41` (CRYPTOCURRENCY),
`market_hours=1` (REGULAR_MARKET; kripto 7/24).

**`PRE_MARKET = 0` olması tasarım açısından belirleyicidir:** proto3'te 0
"gönderilmedi"den ayırt edilemez, dolayısıyla "varsayılan → NULL" kuralı
bu alana uygulanamaz (spec §7.2).

## Zarf biçimi

`version=2` dış zarfı JSON'dur:

```json
{"type":"pricing","message":"CgdCVEMtVVNEFX03nEcYwLDW6o5oIgNVU0Qq..."}
```

Gözlenen tek `type` değeri `"pricing"`. yfinance yalnızca `message`
alanını okur ve `type`'ı hiç kontrol etmez; başka bir `type` gelirse boş
string protobuf'a verilir ve sessizce boş bir kayıt üretilir.

## Yazma yolu kapasitesi

Yöntem: `live_ticks` şemasının birebir kopyası (36 kolon, `symbols`'a FK,
`by_range(ts_utc, INTERVAL '1 day')` hypertable), geçici bir şemada, tek
bir writer bağlantısıyla, 500 sembol dönüşümlü. Her aşama önceki aşamanın
üstüne eklenerek ölçüldü.

### Adım adım maliyet (batch = 500 satır)

| Aşama | satır/sn | ek maliyet |
|---|---:|---|
| A. Yalnız `INSERT ... ON CONFLICT DO NOTHING` | 10.683 | — |
| B. + key-existence `verify` sorgusu | 10.513 | **%2** |
| C. + `live_quotes` guard'lı upsert | 6.985 | %34 |
| D. + `stream_outbox` yazımı (Kafka açık) | 6.219 | %11 |

İki sonuç tasarımı doğrudan etkiliyor:

- **`_verify()` pratikte bedavadır (%2).** "Her yazma okunarak doğrulanır"
  güvencesini canlı akışta da korumanın maliyeti ölçülebilir değil.
  Bunu bir ödünleşim sanmak yanlış olurdu.
- **`live_quotes` upsert'i tek başına %34 yiyor.** En pahalı tek parça
  budur; batch'teki her ayrı sembol için bir çakışma çözümü demektir.

### Batch boyutunun etkisi

| Batch | satır/sn |
|---:|---:|
| 500 | 12.071 |
| 2.000 | 12.686 |
| 5.000 | 12.864 |

Batch'i on kat büyütmek yalnızca **%6** kazandırıyor. Küçük batch
(500) tercih edilebilir: gecikmeyi düşük tutar ve kayıp penceresini
daraltır.

### `COPY` yolu

| Yöntem (batch = 2.000) | satır/sn |
|---|---:|
| `INSERT ... ON CONFLICT` | 12.686 |
| **`COPY` → geçici tablo → `INSERT ... SELECT ... ON CONFLICT`** | **39.301** |

**3,1 kat.** `COPY` `ON CONFLICT` desteklemediği için iki adım gerekiyor:
gövde `COPY` ile `ON COMMIT DROP` bir geçici tabloya alınır, oradan tek
bir `INSERT ... SELECT` ile hedefe taşınır. Dedup ve FK kontrolü ikinci
adımda korunur.

### `live_quotes` kadansı

| Yöntem | satır/sn |
|---|---:|
| Her batch'te upsert (500'lük) | 8.976 |
| Her 4. batch'te upsert (biriktirilmiş son değerler) | 11.024 |

**%23 kazanç.** `live_quotes` türetilmiş bir görünümdür; kaynağı
`live_ticks`'tir. Onu her batch'te değil sabit bir kadansta güncellemek
son durumu birkaç yüz milisaniye geciktirir, veri kaybettirmez.

### Sonuç: tavan

Tek writer thread ile ölçülen tam yol (tick + verify + outbox + quotes):

| Yapılandırma | tick/sn |
|---|---:|
| A. `INSERT` + her batch `live_quotes` + `INSERT` outbox | **6.219** |
| B. `COPY` ticks + `INSERT` outbox + seyrek quotes | 13.634 |
| C. **`COPY` ticks + `COPY` outbox + seyrek quotes** | **22.291** |

B ile C arasındaki fark tek başına öğreticidir: `live_ticks`'i `COPY`'ye
taşımak yetmez. O yapıldığı anda **darboğaz `stream_outbox`'ın kendisine
geçer** — outbox da `COPY` ile yazılınca verim bir kat daha artıyor.
Kafka açıkken outbox, tick tablosuyla aynı yazma tekniğini hak eder.

10.000 sembolün ortalama 1 mesaj/saniye ürettiği bir senaryo 10.000
tick/sn demektir:

- A (ilk tasarım) bunu **karşılamaz**.
- C bunu 2,2 kat marjla karşılar.

Bu, `COPY`'yi bir iyileştirme değil, 10.000 sembollük hedefin önkoşulu
yapar. Aynı hedefin diğer iki önkoşulu yukarıda ölçüldü: **≥100 bağlantı**
(100 sembol/bağlantı kotası) ve **tek asyncio event loop** (thread başına
bağlantı 110'da çöküyor).

## Kütüphane davranışı (yfinance 1.7.0)

| İddia | Gözlem |
|---|---|
| `AsyncWebSocket`'in reconnect'i çalışmaz | `listen()`'in `except` dalı `_connect()` çağırır; `_connect()` yalnız `self._ws is None` iken bağlanır ve hata yolunda `_ws` hiç `None`'a çekilmez. Kapalı soket üzerinde 3 sn'de bir dönen sonsuz hata döngüsü. (`live.py`) |
| Senkron `WebSocket.listen()` hatada sessizce durur | Genel `except` dalı `break` eder. (`live.py`) |
| `websockets.sync.client.connect` bağlam yöneticisi dışında kullanımı **deprecated** | `DeprecationWarning: connect() must be used as a context manager` — yfinance tam da böyle çağırıyor (`sync_connect(self.url)`). |
| Geçersiz sembol sessizce yok sayılır | 45.000 dolgu sembolü için hiçbir hata mesajı dönmedi. |
| Mesaj zarfı | `{"message": "<base64 protobuf>"}`; gövde `PricingData`, 33 alan. |
| İlk mesaj gecikmesi | Abonelikten sonra 1,1–3,8 sn (kripto, Pazar). |
| **`_subscriptions` bir `set`'tir** | Gönderim `list(self._subscriptions)` ile yapılır. 100 sembolü aşınca **hangi 100 sembolün hayatta kalacağını Python'un set sıralaması belirler** — `PYTHONHASHSEED` ile süreçler arası değişir. Kullanıcıya hiçbir uyarı verilmez. |
| `subscribe()` tüm birikmiş seti yeniden gönderir | Yalnızca yenileri değil; `_periodic_subscribe` bunu 15 sn'de bir tekrarlar ve kırpmayı kalıcılaştırır. |

## Sembol evreni (bu kurulumda)

`symbols` tablosundan, 2026-09-06:

| Ölçüt | Değer |
|---|---|
| Toplam sembol | 5.735 |
| `exchange IS NULL` | 0 |
| Farklı `exchange` | 9 |

Dağılım (ilk ölçümde, 2.717 sembolken): NAS 698, PCX 617, NYQ 474,
NMS 335, NGM 236, BTS 178, YHD 156, NCM 18, ASE 5.

**Dağılım son derece dengesizdir** — en büyük grup en küçüğün 140 katıdır.
Exchange başına bir bağlantı kuran bir tasarım, 5 sembollük bir bağlantı
ile 698 sembollük bir bağlantıyı yan yana çalıştırır. Exchange sayısının
(9) bağlantı tavanına yakın olması da tesadüftür ve evren büyüdükçe
değişir.
