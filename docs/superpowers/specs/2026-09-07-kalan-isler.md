# Kalan işler — yeni oturum promptu

Bu dosyanın altındaki bölümü olduğu gibi yeni bir oturuma yapıştırın.

---

`/superpowers:brainstorming` Aşağıdaki kalan işleri birlikte planlayıp tamamlayalım.

## Bağlam

Bu repo (`yfinance`) bir yfinance veri ambarı boru hattı + okuma API'si. Önceki
oturumda dört bağımsız agent tüm kod tabanını denetledi (veri kapsamı, registry
mimarisi, YAGNI/ölü kod, SOLID). Bulunan kusurların bir kısmı düzeltildi ve
commit edildi; aşağıdakiler bilerek bırakıldı çünkü ya kendi onaylarını hak
eden büyük refactor'lar ya da benim erişemediğim dosyalar.

**Kural:** hiçbir düzeltme legacy bırakmayacak — deprecated alias, compat shim,
çift yol yok. Eski yol tamamen silinir.

Her değişiklikten sonra doğrulama:
```
uv run ruff check . && uv run mypy src/yfin
uv run pytest -q tests/unit && uv run pytest -q -m repo tests/repo
uv run python scripts/dump_openapi.py --check
```
Şu an hepsi yeşil: unit 1437, repo 450.

## Kalan işler (öncelik sırasıyla)

### 1. `.env.example` eksik — 12 `YFAPI_*` anahtarının hiçbiri yok
Zorunlu `YFAPI_JWT_SIGNING_KEY` dahil hiçbiri dosyada yok; onsuz API başlamıyor.
`tests/unit/test_env_example.py:33` yalnızca `Settings.model_fields`'a bakıyor,
`ApiSettings`'e bakmıyor — bu yüzden boşluğu hiçbir test yakalamıyor.
**Yapılacak:** 12 anahtarı `.env.example`'a ekle, testi `ApiSettings`'i de
kapsayacak şekilde genişlet. (Önceki oturumda bu dosya izin ayarları yüzünden
okunamadı; sizin oturumunuzda erişilebilir olmalı.)

### 2. `cli/app.py` — 1249 satır, 7 alt-Typer tek dosyada
`db`, `symbols`, `proxy`, `market`, `screen`, `discover`, `domain` hepsi burada;
oysa `bars`, `settings`, `stream`, `api` zaten ayrı dosyalarda. Projenin kendi
kalıbına aykırı. Ayrıca modül düzeyinde tüm pipeline/datasets/models import
ediliyor, yani `yfin --help` bile registry'nin tamamını yüklüyor —
`cli/stream.py:321` aynı importları fonksiyon gövdesinde yapıyor.
**Yapılacak:** `cli/proxy.py`, `cli/symbols.py`, `cli/market.py`, `cli/domain.py`,
`cli/db.py` olarak ayır; paylaşılan yardımcılar (`_selector`,
`_filtered_symbols`, `_engine`, `_session_factory`) `cli/common.py`'ye. Ağır
import'lar komut gövdelerine insin. Saf taşıma, davranış değişmemeli.

### 3. `core/openapi.py`'de üç tablo zorlanmıyor
`METERED`, `CACHED`, `CONDITIONAL` elle güncelleniyor ama unutulursa **hiçbir
test kırılmıyor** — belge sadece eksik kalıyor. Onları koruduğu sanılan üç test
totolojik: belgeyi, belgeyi üreten tablonun kendisine karşı doğruluyorlar
(`tests/unit/test_api_contract.py`'de `test_the_rate_headers_are_published...`,
`test_a_conditional_response_carries...`, `test_every_operation_publishes...`).
**Yapılacak:** `METERED`'i `guard()`'ın döndürdüğü bağımlılıktan türet;
`CACHED`/`CONDITIONAL`'ı route dekoratöründeki tek bir bildirime indir. Totolojik
testleri sil, yerine gerçek olanları koy (çalışma zamanı davranışını belgeye
karşı doğrulayan).

### 4. `AsOfGate`'in yazılı olmayan önkoşulu
`datasets/asof_base.py:155` gate satırını `first_row(result)`'tan kazıyor —
"ilk yazılan tablonun ilk satırı `as_of_date`, `fetched_at` ve `symbol` taşımalı"
diye yazılı olmayan bir sözleşme. Üç alt sınıf bunu ayrı ayrı atlatmış
(`discovery/base.py:36` `UNGATED_TABLES`, `domain/base.py:149` ve
`discovery/base.py:55` `gate_identity` override).
**Risk:** yeni bir as-of dataset'i yazma listesinde başka bir tabloyu öne
koyarsa gate yanlış satırdan üretilir — ya `KeyError` (fetch parası ödendikten
sonra) ya da sessizce yanlış `as_of_date` ile yazılır ve gate kalıcı olarak
"değişmedi" der.
**Yapılacak:** `AsOfGate`'e `gate_source_table: str` ekle, `first_row` yerine o
tablonun write'ını oku. Bildirmeyen alt sınıf import zamanında patlasın.

### 5. `FRAME_CONSUMERS` ikinci merkezi liste
`datasets/history.py:45-50` paylaşılan history frame'ini tüketen dataset'leri
elle sayıyor ve `depends_on` DAG'ında yok (bilerek — `corporate_actions.py:38`
ve `bars.py:494` nedenini yazıyor). `tests/unit/test_sharding.py:254` seti sabit
assert ediyor: **ekleme yaparsanız** test kırılıyor, **eklemeyi unutursanız**
kırılmıyor — koruma yanlış yöne bakıyor. Unutulursa yeni tablo kalıcı olarak dar
pencereyle dolar.
**Yapılacak:** tüketiciliği dataset'in kendisinde bildir
(`shared_frame_watermark: tuple[str, str] | None`), `_shared_watermark` registry
üzerinden toplasın, `history.py`'deki dict silinsin.

### 6. Sağlık kayıtları event loop'u bloke ediyor
`stream/connection.py:259` her kanarya mesajında `_emit_health()` →
`supervisor.py:323` → `repository.py:220` **senkron `INSERT`**, hem de asyncio
event loop'unun içinde. Kanarya `BTC-USD` 7/24 tikliyor ve her bağlantıya
ekleniyor. `supervisor._offer`'ın docstring'i (`supervisor.py:294`) event loop'u
bloke etmenin ping/pong'u kaçırıp **her sembolü** durduracağını açıkça yazıyor —
kuyruk için uyulmuş, sağlık yolu için gözden kaçmış.
**Yapılacak:** `LatestBox` deseni: thread-safe `HealthBox`, writer thread'i
`_flush_counters` içinde toplu yazsın.

### 7. `--full-refresh` gate'leri atlamıyor
`datasets/base.py:174` yalnızca watermark'ı `None` yapıyor; `asof_base.py` ve
`hash_gated.py` `full_refresh`'e hiç bakmıyor. Veri satırları kaybolup gate satırı
kalırsa bir sonraki koşu hash'i eşit bulur, `skipped` der ve hiçbir şey yazmaz —
ve `--full-refresh` bunu onaramaz.
**Yapılacak:** bayrağı gate karşılaştırmasına kadar taşı.

### 8. Market/domain audit satırları run sonunda toplu yazılıyor
`pipeline/market_runner.py:151` ve `domain_runner.py:252` `items` listesini
biriktirip döngü bitince `write_items` çağırıyor; oysa veri her turda kendi
transaction'ında commit ediliyor. Süreç ortada ölürse veri yazılmış ama o koşunun
**hiçbir audit satırı yok**, `sync_runs` "running"da asılı kalıyor. Symbol runner
bu hatayı yapmıyor (`runner.py:279` sembol başına emit ediyor).
**Yapılacak:** her turdan sonra (veya N turda bir) yaz.

### 9. Yazma politikası metot olduğu için kalıtımla çoğalıyor
`upsert` `Dataset`'in metodu; 4 politika × 3 eksen = bugün 6 sınıf + 2 mixin +
1 serbest fonksiyon. Somut zarar bugün var: `SnapshotDataset.key_columns`
varsayılanı `("symbol",)` (`snapshot_base.py:73`) ama `SnapshotGlobalDataset`'te
varsayılansız (`market/base.py:135`) — kardeş sınıflar, aynı isim, farklı
sözleşme; market tarafında unutulan `key_columns` import'ta değil ilk yazmada,
yani Yahoo çağrısı ödendikten sonra patlıyor.
**Yapılacak:** `upsert`'ü `WritePolicy` protokolüne çevir (`PlainUpsert`,
`SnapshotPolicy`, `HashGatePolicy`, `AsOfPolicy`); gated base sınıflarını sil.
**Büyük iş** — ~6 base dosyası + ~25 dataset. Kendi planını hak ediyor.

### 10. `options` / `option_chain` için dataset yok
yfinance'in `Ticker.option_chain()` / `options` yüzeyi hiç toplanmıyor ve repo
genelinde (docs, test, yorum dahil) tek kelime geçmiyor. Bu kod tabanı her
dışlamayı yazılı gerekçelendiriyor (`sustainability`, atlanan interval'ler,
`valuation`'ın ayrı alias olması) — gerekçe yokluğu burada **unutulmuş kapsam**
işareti. Ayrıca `get_shares()` ve `earnings`/`quarterly_earnings` yok.
**Yapılacak:** `options` için tablo ailesi + dataset (yeni özellik, kendi
tasarımını hak ediyor); `shares` ve `earnings` için "türetilebilir/deprecated"
gerekçesini koda yaz.

### 11. YAGNI temizliği (~350 satır)
- 9 sıfır-referans sembol: `INFO_SOURCE_KEYS`, `FAST_INFO_SOURCE_KEYS`,
  `HISTORY_METADATA_SOURCE_KEYS` (`models/fields.py:312-314`),
  `SKIP_OUT_OF_SCOPE` (`pipeline/runner.py:45`), `reject_for_subscription`
  (`stream/connection.py:349`), `MINUTE`/`known_timezone`
  (`stream/reconcile.py:48,280`), `now_utc` (`stream/supervisor.py:370`),
  `last_claimed` (`api/ratelimit/revocation.py:86`)
- Sadece testlerde kullanılanlar: `clients.set_scopes`/`set_plan`,
  `policy.clear_cache`, `normalize.convert_epoch_field`, `prune.asof_tables`,
  `domain_audit.expected_cell_count`, `Registry.unregister`/`is_opt_in`,
  `StreamRepository.archived_symbols`, `variants.NoVariantState`
- Bağlanmamış per-secret revocation: `publish_revocation(revoked_secret_ids=...)`
  ve `clients.revoke_secret` — okuma tarafı her istekte canlı ama yazma tarafını
  hiçbir CLI komutu tetiklemiyor (`cli/api.py`'de `revoke` komutu yok, oysa modül
  docstring'i ondan söz ediyor). **Ya CLI komutunu ekle ya yolu tamamen sil.**
- 11 kesin tekrar (~155 satır): sembol normalize (`core/normalize.py:72` vs
  `api/routers/v1/market.py:148` — gövdeler birebir aynı), naive→UTC 5 yerde,
  CSV parse 8 yerde, `sessionmaker(...)` 10 yerde (ve `stream/runner.py`'ın 3'ü
  sessizce `future=` olmadan), advisory-lock sarmalayıcısı 3 yerde,
  `snapshot_rows` mantığı 2 yerde elle tekrar, iki ayrı sabit-pencere limiter

## Zaten reddedilmiş iddialar (tekrar açmayın)

- `uvicorn` ölü değil — `Dockerfile:65` konteyner komutu olarak çalıştırıyor.
- `storage/contracts.py` protokolleri (`RowSink`/`HashReader`/`SymbolLookup`/
  `SnapshotWriter`) erken soyutlama değil; katman sınırı gerçek, iki agent
  savundu.
- `insider_transactions`'a `replace_scope` uygulanmadı: orada "tahmin →
  gerçekleşen" yaşam döngüsü yok ve Yahoo'nun penceresinden düşen kayıtları
  silme riski var.
- `Registry` sınıfı bölünmemeli — alias, dedup, topolojik sıralama, opt-in,
  bootstrap hepsi tek soruya cevap veriyor.
- `api/routers/v1/market.py` (579 satır) ve `storage/settings_store.py`
  (457 satır) bölünmemeli; somut fayda yok.

## Nasıl ilerleyelim

Önce hangilerini bu turda alacağımıza karar verelim. Benim önerim: 1 ve 3 hemen
(küçük, tamamen zorlanabilir), sonra 4-5-7-8 (sessiz hata sınıfı), sonra 2 ve 11
(mekanik ama geniş). 9 ve 10 kendi tasarım turlarını hak ediyor.
