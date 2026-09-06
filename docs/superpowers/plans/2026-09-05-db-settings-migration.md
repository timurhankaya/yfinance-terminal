# DB Tabanlı Yapılandırma — Göç ve İşletim Notu

**Spec:** `docs/superpowers/specs/2026-09-05-db-settings-design.md` (v2)
**Durum:** Plan A ve Plan B uygulandı (2026-09-06).

`Settings`in 38 alanı artık `settings` tablosundan yönetilir; 8 alan
`.env`de kalır. Çözüm sırası:

```
CLI bayrağı  >  settings tablosu  >  .env  >  model varsayılanı
```

---

## 1. Göç adımları (S9)

1. **İzin.** DB kullanıcısına `settings` tablosunda okuma (yazacaksa yazma)
   izni verin. Kısıtlı bir kullanıcıyla koşan kurulumda bu adım atlanırsa
   `alembic upgrade head` sonrası **her komut** yetki hatasıyla çöker —
   yükleyici sessizce env-only'ye DÜŞMEZ, çünkü sessiz düşüş yanlış
   yapılandırmayla koşmak demektir (S7).
2. `yfin db upgrade` — `settings` tablosu (boş) oluşur.
3. `yfin config seed --adopt-env` — mevcut `.env` **onurlandırılır**.
   Bu adım olmadan `.env`inde `YF_MAX_SHARDS=8` olan bir kurulum sessizce
   varsayılan 4'e dönerdi.
4. `yfin config list` — `source` kolonunun `env`den `db`ye döndüğü
   doğrulanır.
5. O 38 anahtardan `.env`de fiilen bulunanlar silinir. **`YF_PROBE_SUSTAINABILITY`
   de silinir** — o alan tümden kaldırıldı (aşağıya bakın).

**5. adımdan sonra `.env` katmanı fiilen boştur.** `Settings`e sonradan
eklenen bir alan için `yfin config seed` çalıştırılmazsa değer artık
`.env`e değil doğrudan model varsayılanına düşer. Bu yüzden
**`yfin config seed` her `alembic upgrade head` sonrası standart adımdır**;
`yfin db upgrade` eksik satır varsa bunu tek satırlık bir uyarıyla
hatırlatır.

### Downgrade uyarısı

5. adımdan sonra yapılandırma yalnızca DB'de yaşar. Öncesinde tam anlık
görüntü alın — ve **depoya değil**:

```bash
yfin config export --all > ~/yfinance-settings-backup.json
```

`config/settings.seed.json` kuruluma özgü değerlerle kirletilmemelidir: o
dosya **depo yapılandırmasıdır** (S5.1) ve `--all` çıktısı model
varsayılanlarını da içerdiği için oraya yazılırsa varsayılan
değişikliklerinin kuruluma yansıma bağı kopar.

---

## 2. `.env.example` — elle uygulanacak (BU DEPODA YAPILAMADI)

`.env*` dosyaları bu oturumda yazma izni dışındaydı. Aşağıdaki düzenleme
elle uygulanmalıdır: **38 DB-yönetimli anahtar + `YF_PROBE_SUSTAINABILITY`
çıkarılır**, kalan 8 anahtar
artı `YF_SETTINGS_SOURCE` bırakılır.

`.env`de kalması gereken TEK anahtarlar:

```dotenv
# --- baglanti (bootstrap paradoksu: baglantiyi acan deger baglantinin
#     ardinda duramaz) ---
DB_HOST=localhost
DB_PORT=5432
DB_USER=yfin
DB_PASSWORD=
DB_NAME=yfinance
DB_TEST_NAME=yfinance_test

# Fernet anahtari. SIFRELEDIGI proxy parolalariyla ayni yerde duramaz.
YF_PROXY_SECRET_KEY=

# configure_logging(), create_db_engine()'den ONCE cagrilir; ayrica
# yukleyicinin uyarilari yapilandirilmis logger'a dusmek zorundadir.
LOG_LEVEL=INFO

# KURTARMA ANAHTARI. `env` -> settings tablosu HIC okunmaz.
# Bir `Settings` alani DEGILDIR (katmani kapatan anahtar katmandan
# okunamaz). `env` disindaki bos olmayan her deger WARNING uretir.
# YF_SETTINGS_SOURCE=env
```

Diğer 38 anahtarın satırları silinir; değerleri `yfin config` ile yönetilir.
`YF_PROBE_SUSTAINABILITY` varsa o da silinir: **artık bir `Settings` alanı
değildir.**

---

## 3. Günlük kullanım

```bash
yfin config list                  # 38 ayar, etkin deger ve kaynak
yfin config list --group shard    # tek grup (11 grup var)
yfin config list --changed        # etkin degeri varsayilandan FARKLI olanlar
yfin config list --source db      # settings SATIRI olanlar
yfin config get yf_max_shards     # anahtar YF_MAX_SHARDS olarak da yazilabilir
yfin config set yf_max_shards 8   # dogrular, sonra yazar (gecersizse cikis 2)
yfin config unset yf_max_shards   # satiri siler; .env / varsayilana duser
yfin config seed --dry-run        # eksik satir varsa cikis 1 (CI adimi)
yfin config export                # yalniz DB satiri olanlar (seed'in tersi)
yfin config schema --json         # panelin form metadatasi
```

**Kurtarma.** Tabloya ham SQL ile sokulmuş bozuk bir değer *her* komutu
çökertir. Çıkış yolu:

```bash
YF_SETTINGS_SOURCE=env yfin config list      # katman kapali, komut calisir
YF_SETTINGS_SOURCE=env yfin config set yf_max_shards 4   # yine de DB'ye YAZAR
```

`list` / `get` / `export` DB erişilemezken de çökmez: uyarı basıp
`source=env|default` ile devam ederler.

---

## 4. Spec'ten bilinçli sapma: `yf_probe_sustainability` kaldırıldı

Spec (S4.3) bu alanı `datasets` grubunda tutuyor ve sayıları 47/39 olarak
veriyordu. Ama aynı spec (S3.4) dataset'i `register(..., opt_in=True)`
desenine taşıdı ve **o andan sonra bayrağı okuyan kimse kalmadı.**

Etkisi olmayan bir ayar, amacı bir yönetim panelini beslemek olan bu
katmanda en kötü türden gürültüdür: panelde çalışır görünen ama hiçbir şey
yapmayan bir anahtar. `yf_discovery_enabled` ve `YF_BAR_INTERVALS` için
verilen kararın aynısı uygulandı (YAGNI).

Sonuç: **46 alan / 8 env-only / 38 DB-yönetimli / 11 grup**
(`datasets` grubu 6 değil 5).

---

## 5. Bu tasarımın ÇÖZMEDİĞİ şeyler (S12)

- **Koşan bir sync'in ortasında ayar değiştirmek.** Değer süreç başında bir
  kez okunur; değişiklik BİR SONRAKİ koşuda etkili olur.
- **Değişiklik geçmişi / kim değiştirdi.** `updated_at` dışında iz yok.
- **Eşzamanlı yazımda çatışma tespiti.** Son yazan kazanır.
- **Çok kurulumlu merkezi yönetim.** Kurulum başına farklılaştırma yoktur.
- **Sırların yönetimi.** `yf_proxy_secret_key` ve `db_password` `.env`de kalır.
