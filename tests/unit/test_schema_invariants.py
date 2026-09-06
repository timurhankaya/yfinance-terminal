"""Sema genelinde gecerli olmasi gereken degismezler.

Bu testler tek tek tablolara degil, `Base.metadata`'nin TAMAMINA bakar.
Gerekce: bir politikanin (FK davranisi, damga hassasiyeti, sembol kolonu
collation'i) tek bir yardimcida tanimli olmasi onu KORUMAZ -- yeni bir tablo
yardimciyi kullanmadan da yazilabilir ve sessizce sapabilir. Burada
korunan sey yardimcinin kendisi degil, INVARYANT'tir.
"""

from __future__ import annotations

from sqlalchemy import Date, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP

from yfin.models import Base


def _symbol_fks() -> list[tuple[str, ForeignKeyConstraint]]:
    out = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_key_constraints:
            if fk.referred_table.name == "symbols":
                out.append((table.name, fk))
    return out


def test_every_symbol_fk_uses_the_same_policy() -> None:
    """ON UPDATE CASCADE + ON DELETE RESTRICT (T S5.5).

    RESTRICT, soft-delete politikasini DB SEVIYESINDE zorlayan seydir: tek
    bir DELETE 40 yillik gecmisi geri donusumsuz silmesin diye. Bir tablo
    CASCADE'e saparsa kimse fark etmez -- ta ki bir sembol silinip veri
    kaybolana kadar.
    """
    found = _symbol_fks()
    assert found, "symbols'a FK tasiyan tablo bulunamadi"
    for table_name, fk in found:
        assert fk.ondelete == "RESTRICT", f"{table_name}: ondelete={fk.ondelete}"
        assert fk.onupdate == "CASCADE", f"{table_name}: onupdate={fk.onupdate}"


def test_every_symbol_column_shares_the_symbols_collation() -> None:
    """FK kolonunun collation'i ebeveynle BIREBIR esit olmali.

    MySQL bunu MOTOR SEVIYESINDE zorluyordu: uyusmazlik ERROR 3780
    verir ve tablo hic olusmazdi. PostgreSQL boyle bir hata VERMEZ --
    yani sapma SESSIZDIR ve JOIN/karsilastirma semantigini ayristirir.
    Test tam da bu yuzden artik daha degerlidir (PG S9.4)."""
    parent = Base.metadata.tables["symbols"].c["symbol"].type
    for table_name, fk in _symbol_fks():
        for element in fk.elements:
            child = element.parent.type
            assert getattr(child, "collation", None) == getattr(parent, "collation", None), (
                f"{table_name}.{element.parent.name}"
            )
            assert getattr(child, "length", None) == getattr(parent, "length", None)


def test_no_timestamp_column_loses_sub_second_precision() -> None:
    """Tum damgalar TIMESTAMP(6) WITH TIME ZONE.

    Saniye hassasiyeti ayni saniyede PK cakismasi uretir
    (ticker_info_history PK'si (symbol, fetched_at)) ve PostgreSQL
    kesirleri YUVARLAR, kesmez (T S5.4, PG S2.3).

    `timezone` de kontrol edilir: naive bir damga kolonu, psycopg'nin
    baglanti TZ'sine gore yorumlamasi sayesinde DOGRU sonuc verir ama
    tip tutarsizligini kalicilastirir -- yani sapma SESSIZDIR.
    """
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.c
        if isinstance(col.type, TIMESTAMP)
        and (col.type.precision != 6 or col.type.timezone is not True)
    ]
    assert offenders == []


# As-of tablolarinda `as_of_date`ten ONCE gelmesine izin verilen PK onekleri.
# Sembol tarafinda kapsam `symbol`, domain tarafinda `domain_key`; bolgeli
# domain tablolarinda arada `region` vardir (SI S5.3-5.5) ve orasi da
# invariant'i BOZMAZ: bolge kumesi yapilandirmayla sinirli ve her sorguda
# bilinen bir degerdir, "D gunundeki deger" sorgusu yine oneki kullanir.
# `query_term` UCUNCU kapsam eksenidir (SQ S5.2, S5.6): bir arama teriminin
# sonucu BIRDEN COK sembol tasir, dolayisiyla kapsam sembol degildir. "D
# gunundeki sonuc" sorgusu yine oneki kullanir:
# `WHERE query_term = ? AND as_of_date = ?`.
ASOF_PK_PREFIXES = (
    ("symbol",),
    ("domain_key",),
    ("domain_key", "region"),
    ("query_term",),
    # `screen_key`: ekranin gunluk basligi ve kadrosu (SQ S5.9, S5.10).
    # `screen_quotes` bu listeye GIRMEZ ve girmemelidir -- onun oneki
    # `symbol`dur cunku kotasyon EKRANDAN BAGIMSIZDIR (SQ K5).
    ("screen_key",),
)


def test_as_of_date_is_always_the_second_key_component() -> None:
    """as_of_date ANAHTARDAYSA ikinci bilesen olmali: "D gunundeki deger"
    sorgusu `(kapsam[, region], as_of_date)` onekini kullanir (AH S5.9); sona kaysaydi
    o sorgu full scan olurdu.

    Kapi tablolari (`asof_state`, `domain_asof_state`) KAPSAM DISIDIR: orada
    as_of_date anahtar degil, kapinin TASIDIGI veridir.
    """
    checked = 0
    for table in Base.metadata.tables.values():
        pk = [c.name for c in table.primary_key.columns]
        if "as_of_date" not in pk:
            continue
        position = pk.index("as_of_date")
        assert tuple(pk[:position]) in ASOF_PK_PREFIXES, f"{table.name} -> {pk}"
        assert isinstance(table.c["as_of_date"].type, Date)
        checked += 1
    assert checked >= 14, f"as-of tablosu bekleniyordu, {checked} bulundu"


def test_child_tables_inherit_their_parent_timestamp() -> None:
    """Damgasiz her tablo ya bir EBEVEYNDEN turer ya kaynak-tarihlidir.

    Bu proje her satira `fetched_at` koymaz ve koymamalidir: `financial_facts`
    damgayi `financial_periods`'tan, `news_symbols` `news`'ten alir;
    `price_history`/`dividends`/`splits` ise kaynagin kendi tarihini tasiyan
    serilerdir (T S5.2). Test bu ayrimi BELGELER: damgasiz yeni bir tablo
    eklenirse hangi kategoriye girdigi acikca soylenmek zorundadir.
    """
    source_dated = {
        "price_history",
        "dividends",
        "splits",
        "capital_gains",
        "shares_full",
        "company_officers",
        "news",
        # price_bars: ts_utc KAYNAGIN kendi zamanidir, price_history'nin
        # session_date'i gibi. Ayrica bir fetched_at, kalici arsivde satir
        # basina 8 byte x ~464 milyon = ~4 GB'a mal olur ve hicbir soruya
        # cevap vermez: bar hangi kosuda yazildi bilgisi sync_run_items'ta
        # zaten var (PB S5.1).
        "price_bars",
        # periodic_bars: price_bars ile AYNI gerekce -- ts_utc kaynagin
        # kendi zamanidir ve bar hangi kosuda yazildi bilgisi
        # sync_run_items'ta zaten var.
        "periodic_bars",
    }
    # Operasyonel ve denetim tablolari: kendi zaman kolonlarini tasirlar
    # (added_at / detected_at / applied_at) ama bunlar "kaynaktan cekilme"
    # damgasi DEGILDIR, bu yuzden fetched_at aranmaz.
    exempt = {
        "symbols",
        "sync_runs",
        "sync_run_items",
        "proxies",
        "alembic_version",
        "intraday_scope",  # added_at: kapsam listesine ne zaman girdi
        "bar_gaps",  # detected_at: bosluk ne zaman tespit edildi
        "bar_rescales",  # applied_at: olcekleme ne zaman uygulandi
        # `screens` STATIK KIMLIKTIR (SQ S5.8), `symbols` ile ayni kategori:
        # bir ekranin tanimi Yahoo'dan "cekilmez", `screens.py`den seed
        # edilir. created_at/updated_at tasir; `fetched_at` burada yanlis
        # anlam olurdu. Gunluk cekim damgasi `screen_runs.fetched_at`tedir.
        "screens",
        # `settings` de STATIK KIMLIKTIR: operator tarafindan yazilir,
        # Yahoo'dan "cekilmez". created_at/updated_at tasir.
        "settings",
    }
    for table in Base.metadata.tables.values():
        if table.name in exempt or "fetched_at" in table.c:
            continue
        parents = {fk.referred_table.name for fk in table.foreign_key_constraints}
        assert table.name in source_dated or (parents - {"symbols"}), (
            f"{table.name}: ne fetched_at tasiyor, ne bir ebeveynden turuyor"
        )


# Kolon adi olarak cazip gelen AYRILMIS/TEHLIKELI sozcukler.
#
# Liste MySQL doneminde kuruldu ve PostgreSQL'e gecerken KORUNDU: buyuk
# kismi (pencere fonksiyonlari, `interval`, `order`, `group`, `key`,
# `rows`) PostgreSQL'de de ayrilmistir ya da tip/fonksiyon adidir.
# Birkaci yalnizca MySQL'de ayrilmis olabilir -- liste DARALTILMADI
# cunku amaci tasinabilirlik: bir kolon adi iki motorda da tirnaksiz
# calisiyorsa hicbir ham SQL onu bozamaz. `status` BU LISTEDE DEGILDIR.
#
# `rank` SQ sirasinda eklendi: pencere fonksiyonu olarak ayrilmistir ve
# tirnaksiz her ham SQL'i sozdizimi hatasiyla dusurur. SQLAlchemy kendi
# urettigi SQL'i tirnakladigi icin ORM yolu CALISIR -- hata yalnizca elle
# yazilan sorguda ve migration betiklerinde patlar, yani en gec fark
# edilen yerde. Bu testin varlik sebebi o gecikmeyi ortadan kaldirmaktir.
RESERVED_WORDS = frozenset(
    {
        "rank",
        "range",
        "row_number",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lead",
        "lag",
        "first_value",
        "last_value",
        "groups",
        "over",
        "window",
        "recursive",
        "system",
        "of",
        "except",
        "interval",
        "key",
        "order",
        "group",
        "rows",
        "condition",
    }
)

# ONCEDEN VAR OLAN istisna. `history_metadata.range` kaynagin kendi alan
# adidir (`range: "1mo"`) ve tablo uretimde. Yeniden adlandirmak bir
# migration + veri tasima demek olurdu ve bu testin amaci gecmisi yeniden
# yazmak degil, YENI tablolarin ayni tuzaga dusmesini engellemek.
# Listeye ekleme yapmadan once: adin gercekten kacinilmaz oldugundan emin
# olun.
RESERVED_GRANDFATHERED = frozenset({"history_metadata.range"})


def test_no_column_uses_a_reserved_word() -> None:
    """Kolon adlari ayrilmis sozcuklerden secilmez.

    `economic_calendar.last_reported` bu kuralin ilk uygulamasiydi;
    `screen_members.rank_index` ikincisi. Ikisi de "dogal" adin
    (`reported`, `rank`) ayrilmis oldugu yerlerde duruyor.
    """
    offenders = [
        name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.lower() in RESERVED_WORDS
        and (name := f"{table.name}.{column.name}") not in RESERVED_GRANDFATHERED
    ]
    assert offenders == [], offenders
