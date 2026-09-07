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
Şu an hepsi yeşil: unit 1527, repo 492.

## Birinci turda tamamlananlar (2026-09-07)

Beş madde bitti ve commit edildi; aşağıdaki listeden çıkarıldılar.

- `b2d8e38` — market/domain audit satırları tur başına yazılıyor (eski madde 8)
- `ba0ec55` — as-of gate kaynağı, paylaşılan frame tüketicileri ve
  `--full-refresh`'in gate'lere ulaşması (eski maddeler 4, 5, 7; tek commit
  çünkü aynı dosyalara dokunuyorlar)
- `271730e` — yayımlanan başlık sözleşmesi route'ta bildiriliyor ve çalışma
  zamanına karşı test ediliyor (eski madde 3)

İki noktada plandan sapıldı, ikisinin de gerekçesi commit gövdesinde:

1. **`gate_source_table` tekil değil, `gate_source_tables` çoğul.** Tek bir
   "her zaman dolu" gate tablosu varsayımı repoda yanlış:
   `tests/fixtures/_discovery/search_Turkish-Airlines.json` `quotes: 0,
   researchReports: 3` döndürüyor, yani `search` için böyle bir tablo yok.
   Bildirilen şey artık sıralı bir aday listesi — asıl kazanç, sıranın
   `normalize`'ın write listesinden değil bildirimden gelmesi.

2. **`METERED` `guard()`'tan türetilmedi.** `listDatasets` ve `readDataset`
   metering'i gövdede yapıyor (ikincisi zorunlu olarak: ailesi hangi dataset'in
   istendiğine bağlı), dolayısıyla bağımlılıktan okunan bir kural bu ikisini
   "metered değil" sayardı. Üçü de tek tip `contract(...)` bildirimine indi.

Madde 1 (`.env.example`) bu turda alınmadı: dosya bu oturumda da izin
ayarlarıyla korunuyordu, ne okunabildi ne yazılabildi.

## İkinci turda tamamlananlar (2026-09-07)

- `b1aa7cb` — YAGNI: 13 sembol silindi, `expected_cell_count` testlere taşındı,
  `revoke`/`set-scopes`/`set-plan` CLI komutları eklendi (eski madde 6, ilk üç
  madde işareti). Kural: **çağıranı hiç olmayanı sil, testlerin gözlemlemek ya
  da sıfırlamak için gerçekten ihtiyaç duyduğunu tut, üretimin ihtiyaç duyup
  yolu olmayanı bağla.** Bu yüzden `clear_cache`, `Registry.unregister` ve
  `is_opt_in` duruyor.
- `32fb360` — `cli/app.py` 1249 → 347 satır; `common`, `db`, `symbols`,
  `market`, `domain`, `proxy` modülleri (eski madde 2). 44 komutun `--help`
  çıktısı birebir aynı.
- `24482d4` — sembol normalize, `sessionmaker` ve naive→UTC tekrarları tek yere
  indi (eski madde 6, dördüncü madde işaretinin 3'ü).

Yan bulgu: `test_api_examples.py` `token_endpoint`'i stub'lamıyordu, o yüzden
API'nin tek fail-closed limiter'ı **gerçek** Redis'e yazıyordu. 401 örneğini
yakalayan test her koşuda kalıcı bir sayaç bırakıyor, onuncu koşuda endpoint
401 yerine 429 döndürüyordu — on dakikada on kez koşturulamayan bir suite.

`.env.example` bu turda dolduruldu (12 `YFAPI_*` anahtarı) ve test
`ApiSettings`'i de kapsayacak şekilde genişletildi, **ama commit edilmedi**:
dosya hâlâ okunamıyor, dolayısıyla içinde benim yazmadığım commit edilmemiş
değişikliklerin de olduğu bir dosyayı görmeden commit'lemek doğru değil.
`git diff .env.example` ile gözden geçirip `tests/unit/test_env_example.py` ile
birlikte commit'leyin.

## Üçüncü turda tamamlananlar (2026-09-07)

- `4295764` — `yfin --help` artık ORM'i de yüklemiyor: **947 ms / 1529 modül →
  308 ms / 561 modül**. `bars`, `stream`, `settings`, `api` modüllerinin ağır
  import'ları komut gövdelerine indi. Suçlular ölçülerek bulundu, tahmin
  edilmedi (`core.config` ve `api.core.config` masum). `cli/api._scope_values`
  düz bir taşıma değildi: komut dekoratörleri *değerlendirilirken* çağrılıyor,
  o yüzden gövdeye indirmek yetmiyordu — `ApiScope`'un kendi türediği
  `core.families` çiftinden türetiliyor artık, ve bir test ikisinin ayrışmasını
  engelliyor. `tests/unit/test_cli_imports.py` bunu **alt süreçte** ölçüyor:
  suite koşarken paketin yarısı zaten import edilmiş oluyor, süreç içinde
  bakmak her hâlükârda yeşil verirdi.
- `ad23c11` — `core/text.comma_list` 11 elle yazılmış CSV parse'ının yerine
  geçti; `api/ratelimit/fixed_window.FixedWindow` iki birebir aynı sabit-pencere
  sayacının. `comma_list` hiçbir şey import etmeyen bir modülde duruyor ve bu
  taşıyıcı: `api/core/config.py` çağıranlardan biri ve az önce temizlenen
  `--help` yolunda.
- `a5661e5` — dört snapshot dataset'i artık yazma çiftini sınıfın kendi
  bildiriminden kuruyor. Literal ile öznitelik ayrışsaydı gate kimsenin
  yazmadığı bir tabloya bakardı ve her satır sonsuza dek yeni görünürdü.

## Kalan işler (öncelik sırasıyla)

### 1. Sağlık kayıtları event loop'u bloke ediyor
`stream/connection.py:259` her kanarya mesajında `_emit_health()` →
`supervisor.py:323` → `repository.py:220` **senkron `INSERT`**, hem de asyncio
event loop'unun içinde. Kanarya `BTC-USD` 7/24 tikliyor ve her bağlantıya
ekleniyor. `supervisor._offer`'ın docstring'i (`supervisor.py:294`) event loop'u
bloke etmenin ping/pong'u kaçırıp **her sembolü** durduracağını açıkça yazıyor —
kuyruk için uyulmuş, sağlık yolu için gözden kaçmış.
**Yapılacak:** `LatestBox` deseni: thread-safe `HealthBox`, writer thread'i
`_flush_counters` içinde toplu yazsın.

### 2. Yazma politikası metot olduğu için kalıtımla çoğalıyor
`upsert` `Dataset`'in metodu; 4 politika × 3 eksen = bugün 6 sınıf + 2 mixin +
1 serbest fonksiyon. Somut zarar bugün var: `SnapshotDataset.key_columns`
varsayılanı `("symbol",)` (`snapshot_base.py:73`) ama `SnapshotGlobalDataset`'te
varsayılansız (`market/base.py:135`) — kardeş sınıflar, aynı isim, farklı
sözleşme; market tarafında unutulan `key_columns` import'ta değil ilk yazmada,
yani Yahoo çağrısı ödendikten sonra patlıyor.
**Yapılacak:** `upsert`'ü `WritePolicy` protokolüne çevir (`PlainUpsert`,
`SnapshotPolicy`, `HashGatePolicy`, `AsOfPolicy`); gated base sınıflarını sil.
**Büyük iş** — ~6 base dosyası + ~25 dataset. Kendi planını hak ediyor.

### 3. `options` / `option_chain` için dataset yok
yfinance'in `Ticker.option_chain()` / `options` yüzeyi hiç toplanmıyor ve repo
genelinde (docs, test, yorum dahil) tek kelime geçmiyor. Bu kod tabanı her
dışlamayı yazılı gerekçelendiriyor (`sustainability`, atlanan interval'ler,
`valuation`'ın ayrı alias olması) — gerekçe yokluğu burada **unutulmuş kapsam**
işareti. Ayrıca `get_shares()` ve `earnings`/`quarterly_earnings` yok.
**Yapılacak:** `options` için tablo ailesi + dataset (yeni özellik, kendi
tasarımını hak ediyor); `shares` ve `earnings` için "türetilebilir/deprecated"
gerekçesini koda yaz.

## Zaten reddedilmiş iddialar (tekrar açmayın)

- Advisory-lock "sarmalayıcısı 3 yerde tekrar" değil: üç runner'daki
  `if acquire_lock: with advisory_lock(...): return run(..., acquire_lock=False)`
  yeniden girişi üçer satır ve katlamanın her yolu daha uzun bir thunk istiyor.
- `policy.clear_cache`, `Registry.unregister` ve `Registry.is_opt_in` "sadece
  testlerde kullanılıyor" diye silinemez: birincisi TTL cache'i süreç içinde
  sıfırlamanın tek yolu, ikincisi modül düzeyi singleton'a yapılan test
  kayıtlarını geri alıyor, üçüncüsü olmasa testler `registry._opt_in`'e uzanır.

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

Üç madde kaldı. Madde 1 dar ve tek başına alınabilir; 2 ve 3 kendi tasarım
turlarını hak ediyor.
