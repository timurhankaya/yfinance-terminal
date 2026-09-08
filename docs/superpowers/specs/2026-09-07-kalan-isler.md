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
Şu an hepsi yeşil: unit 2131, repo 684 (2026-09-08).

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

## Dördüncü turda tamamlananlar (2026-09-08)

- **Madde 1 kapandı — sağlık kayıtları event loop'tan çıktı.**
  `HealthBox` (`stream/supervisor.py`), `LatestBox`'ın birebir kalıbı:
  event loop kaydeder, writer thread `_flush_health()` ile yazar. Kutu
  bağlantı başına yalnız en yenisini tutar, yani 50 kanarya mesajı bir
  `INSERT` eder. Kutuya **kopya** konuyor: `ConnectionHealth` mutable ve
  bağlantı onu değiştirmeye devam ediyor, canlı referans writer
  thread'in yarı değişmiş bir satır okumasına izin verirdi.
  `heartbeat_at` artık writer thread'in de yaşadığını kanıtlıyor.

- **Madde 2'nin somut zararı kapandı; yapısal kısmı açık.**
  `SnapshotWrite` iki eksenin ortak yazma politikası oldu
  (`SnapshotDataset` ve `SnapshotGlobalDataset` artık onu miras alıyor);
  `plain_upsert` iki özdeş gövdenin yerine geçti. `key_columns`'ın
  varsayılanı **kaldırıldı** — beş snapshot dataset'inin üçünde
  `("symbol",)` doğru, ikisinde yanlış, ve çoğunlukla doğru olan bir
  varsayılan tam olarak bir market dataset'inin sahip olmadığı bir
  kolonla anahtarlanma biçimi. `tests/unit/test_dataset_contracts.py`
  her kayıtlı dataset için bildirilen ama atanmayan her özniteliği
  kontrol ediyor: eskiden ilk yazmada (Yahoo çağrısı ödendikten sonra)
  patlayan hata artık CI'da patlıyor.
  **Açık kalan:** `upsert`'ün metot yerine bir `WritePolicy` protokolüne
  taşınması (kompozisyon). Tekrar ve sözleşme ayrışması gitti; kalıtım
  ekseni duruyor ve hâlâ ~6 base + ~25 dataset'lik kendi turunu hak
  ediyor.

- **Madde 3 ikiye ayrıldı; ikisi kapandı, biri açık.**
  `Ticker.earnings`/`quarterly_earnings` upstream'de deprecated ve
  düpedüz `None` döndürüyor ("Look for \"Net Income\" in
  Ticker.income_stmt", yfinance 1.7.0) — o net kâr zaten
  `financial_facts`'te `NetIncome` olarak var. `Ticker.get_shares()` ise
  `Fundamentals.shares`'i okuyor ve o property her sembolde
  `YFNotImplementedError` atıyor; canlı yol `get_shares_full()` ve o
  toplanıyor. İkisinin de gerekçesi artık dataset'lerin yanında yazılı
  ve `docs/measurements/yahoo-api.md`'de kayıtlı.

## Kalan işler (öncelik sırasıyla)

### 1. `options` / `option_chain` için dataset yok
yfinance'in `Ticker.option_chain()` / `options` yüzeyi hiç toplanmıyor
ve repo genelinde (docs, test, yorum dahil) tek kelime geçmiyor. Yukarıda
kapanan iki kardeşinin aksine bu **upstream'de canlı**: gerçek bir kapsam
eksiği.
**Yapılacak:** tablo ailesi + dataset. Yeni özellik, kendi tasarımını hak
ediyor — vade listesi ile zincirin kendisi iki ayrı istek, zincir
(sembol, vade) başına iki DataFrame, ve as-of/gate politikası seçilmeli.

### 2. Yazma politikası metot olduğu için kalıtımla çoğalıyor (yapısal kısım)
Somut zarar dördüncü turda kapandı (yukarıya bakın). Geriye "upsert bir
metot olduğu için politika kalıtım ekseninde çoğalıyor" duruyor:
`WritePolicy` protokolü (`PlainUpsert`, `SnapshotPolicy`,
`HashGatePolicy`, `AsOfPolicy`) + gated base sınıflarının silinmesi.
**Büyük iş** — ~6 base dosyası + ~25 dataset. Kendi planını hak ediyor.

### 3. Tam evren tek IP'ye sığmıyor
5.888 sembolün tam senkronu tek IP'de ~33 saat sürüyor, yani gecelik
cadence'e sığmıyor. Bu bir kod kusuru değil, bekleyen bir **proxy
kararı**; kod tarafı (`yfin proxy`) hazır.

## Zaten reddedilmiş iddialar (tekrar açmayın)

- Advisory-lock "sarmalayıcısı 3 yerde tekrar" değil: üç runner'daki
  `if acquire_lock: with advisory_lock(...): return run(..., acquire_lock=False)`
  yeniden girişi üçer satır ve katlamanın her yolu daha uzun bir thunk istiyor.
- `policy.clear_cache`, `Registry.unregister` ve `Registry.is_opt_in` "sadece
  testlerde kullanılıyor" diye silinemez: birincisi TTL cache'i süreç içinde
  sıfırlamanın tek yolu, ikincisi modül düzeyi singleton'a yapılan test
  kayıtlarını geri alıyor, üçüncüsü olmasa testler `registry._opt_in`'e uzanır.

- `uvicorn` ölü değil — Dockerfile'ın `CMD`'i konteyner komutu olarak
  çalıştırıyor (image artık `docker/entrypoint.sh`'i ENTRYPOINT olarak
  taşıyor; `CMD` ona argüman).
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

Üç madde kaldı ve üçü de kendi turunu hak ediyor: 1 yeni bir tablo
ailesi, 2 bir refactor planı, 3 kod değil bir işletme kararı.
