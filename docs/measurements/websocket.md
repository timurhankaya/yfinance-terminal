# Canlı WebSocket akışı — ölçülen davranış

Ölçüm tarihi: 2026-09-06 (**Pazar** — ABD ve Avrupa borsaları kapalı).
Ortam: PostgreSQL 18.6 + TimescaleDB 2.29.2 (`timescale/timescaledb:2.29.2-pg18`),
yfinance 1.7.0, macOS, yerel Docker.

Her kayıt, `docs/superpowers/specs/2026-09-06-websocket-streaming-design.md`
tasarımındaki bir sayının ya da iddianın dayanağıdır.

> **Kapsam uyarısı.** Bu ölçümlerin **yazma tarafı** eksiksizdir ve haftanın
> gününden bağımsızdır. **Akış tarafı** ise Pazar günü alınmıştır: yalnızca
> 7/24 işlem gören kripto sembolleri mesaj üretmiştir, dolayısıyla mesaj
> hızı ve alan doluluk oranları ölçülmemiş sayılır. Bunlar açık piyasada
> tekrarlanmalıdır (spec §13, Aşama 0).

## Abonelik limiti

Yöntem: her seviyede yeni bir bağlantı açıldı, tek bir
`{"subscribe": [...]}` mesajı gönderildi, 12–20 saniye dinlendi. Ölçülen
şey tick sayısı değil, sunucunun aboneliği **kabul edip etmediğidir**.

| İstenen sembol | subscribe gövdesi | Bağlantı | Sonuç |
|---:|---:|---|---|
| 5 | 71 B | ayakta | kabul |
| 100 | 1.102 B | ayakta | kabul |
| 500 | 4.613 B | ayakta | kabul |
| 1.000 | 8.918 B | ayakta | kabul |
| 2.000 | 17.433 B | ayakta | kabul |
| 5.734 | 50.132 B | ayakta | kabul |
| 10.000 | 122.629 B | ayakta | kabul |
| 20.000 | 292.629 B | ayakta | kabul |
| **50.000** | **802.629 B** | **ayakta** | **kabul** |

**Gözlenen sembol sayısı sınırı yoktur.** 50.000 sembollük, 802 KB'lık bir
abonelik mesajı reddedilmedi, bağlantı kapatılmadı ve mesaj akışı devam
etti. 10.000 ve 20.000 seviyelerinde dolgu olarak kullanılan geçersiz
semboller (`ZZ000001.TEST`) için **hiçbir hata mesajı dönmedi** — sunucu
tanımadığı sembolü sessizce yok sayıyor.

Bunun anlamı: 10.000 sembol tek bir bağlantıdan dinlenebilir. Bağlantıyı
bölmenin gerekçesi Yahoo'nun bir limiti değil, **arıza izolasyonudur**
(spec K3).

Ölçülmeyen: açık piyasada aynı abonelik büyüklüğünün sürdürülebilirliği,
IP başına eşzamanlı bağlantı sınırı, uzun süreli bağlantıda throttle veya
ban davranışı.

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

- A (bugünkü tasarım) bunu **karşılamaz**.
- C bunu 2,2 kat marjla karşılar.

Bu, `COPY`'yi bir iyileştirme değil, 10.000 sembollük hedefin önkoşulu
yapar.

## Kütüphane davranışı (yfinance 1.7.0)

| İddia | Gözlem |
|---|---|
| `AsyncWebSocket`'in reconnect'i çalışmaz | `listen()`'in `except` dalı `_connect()` çağırır; `_connect()` yalnız `self._ws is None` iken bağlanır ve hata yolunda `_ws` hiç `None`'a çekilmez. Kapalı soket üzerinde 3 sn'de bir dönen sonsuz hata döngüsü. (`live.py`) |
| Senkron `WebSocket.listen()` hatada sessizce durur | Genel `except` dalı `break` eder. (`live.py`) |
| `websockets.sync.client.connect` bağlam yöneticisi dışında kullanımı **deprecated** | `DeprecationWarning: connect() must be used as a context manager` — yfinance tam da böyle çağırıyor (`sync_connect(self.url)`). |
| Geçersiz sembol sessizce yok sayılır | 45.000 dolgu sembolü için hiçbir hata mesajı dönmedi. |
| Mesaj zarfı | `{"message": "<base64 protobuf>"}`; gövde `PricingData`, 33 alan. |
| İlk mesaj gecikmesi | Abonelikten sonra 1,1–3,8 sn (kripto, Pazar). |

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
