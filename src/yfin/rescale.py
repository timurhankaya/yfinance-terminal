"""Split'te geriye donuk yeniden olcekleme (PB S6.6).

Tasarimin en kritik parcasi ve en kolay yanlis yazilani.

SORUN: Yahoo HAM FIYAT VERMIYOR. `auto_adjust=False` yalnizca TEMETTU
duzeltmesini `Adj Close`'a ayirir; SPLIT duzeltmesi OHLC'ye zaten
uygulanmis gelir. Olculdu (NVDA, 2024-06-10, 10:1):

    2024-06-05  Close=122.44  Volume=528.402.000   <- split ONCESI gun
                (o gun gercekte ~1224.40 ve ~52,84 M idi)

price_history bunu umursamaz: her kosuda TUM gecmisi yeniden yazar.
price_bars yazamaz - 30 gunu gecmis bir 1m bari yeniden CEKILEMEZ. Yani
arsivin olcegini korumak bize duser; aksi halde tablo karisik olcekli
olur ve split gununde sahte bir 10x sicrama gorunur.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from yfin.logging_setup import get_logger
from yfin.models import INTRADAY_INTERVALS, Base

log = get_logger(__name__)


def _rowcount(result: Any) -> int:
    """Etkilenen satir sayisi.

    `Session.execute` statik olarak `Result` doner ve `rowcount` yalniz
    `CursorResult`ta tanimlidir; cast yerine tek noktada okunur.
    """
    return int(getattr(result, "rowcount", 0) or 0)


class RescaleSkipped(Exception):
    """Bu split uygulanamaz; kayit da YAZILMAZ.

    Yazilsaydi split "uygulandi" sayilir ve dogru veri bir daha asla
    olceklenmezdi - sessiz ve kalici bir bozulma.
    """


def rescale_factors(ratio: Decimal) -> tuple[Decimal, Decimal]:
    """(fiyat carpani, hacim carpani).

    Yahoo split sonrasi gecmis fiyati BOLER, hacmi CARPAR; UPDATE ayni
    yonu uygular ve arsivi Yahoo'nun guncel olcegine hizalar. Ters split
    (ratio < 1) ayni formulle dogru calisir, ozel dal yoktur.
    """
    if ratio <= 0:
        raise RescaleSkipped(f"gecersiz split orani: {ratio}")
    return Decimal(1) / ratio, ratio


def split_boundary_utc(split_day: date, timezone_name: str | None) -> datetime:
    """Split gununun YEREL 00:00'inin UTC karsiligi (tz-naive).

    Ham UTC gece yarisi alinamaz: pozitif ofsetli borsalarda (BIST +03)
    yerel 00:00, UTC'de ONCEKI gunun 21:00'idir; aradaki barlar yanlis
    tarafta kalir ve ya olceklenmeden kalir ya iki kez olceklenir.

    tz bilinmiyorsa UTC VARSAYILMAZ: yanlis sinirla olceklemek, hic
    olceklememekten daha kotudur cunku sonucu geri alinamaz.
    """
    if not timezone_name:
        raise RescaleSkipped("sembolun IANA tz adi bilinmiyor")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # `timezone` kolonu "EDT"/"TRT" gibi kisaltmalar tasir ve buraya
        # duserse acikca reddedilir (PB S6.6/2).
        raise RescaleSkipped(f"gecersiz tz adi: {timezone_name}") from exc
    local_midnight = datetime.combine(split_day, datetime.min.time(), tzinfo=zone)
    # UTC-AWARE doner: `price_bars.ts_utc` artik timestamptz'dir
    # (PG S2.3) ve karsilastirma ayni farkindalik duzeyinde
    # yapilmalidir.
    return local_midnight.astimezone(UTC)


def _symbol_timezone(session: Session, symbol: str) -> str | None:
    table = Base.metadata.tables["history_metadata"]
    return session.execute(
        select(table.c["exchange_timezone_name"]).where(table.c["symbol"] == symbol)
    ).scalar_one_or_none()


def pending_splits(session: Session, symbol: str) -> list[tuple[date, Decimal]]:
    """Uygulanabilir split satirlari: kaydi olmayan VE arsivden YENI olanlar.

    `splits` TABLOSUNDAN okur, dataset bagimliligindan DEGIL: `history` o
    kosuda hic secilmemis olsa bile DB'deki mevcut split'lere gore dogru
    davranir (PB S6.1).

    IKI KAPI VARDIR ve ikisi de gereklidir:

      1. `bar_rescales`te kaydi yok  - idempotency (ayni split iki kez
         uygulanmaz).
      2. `split_date` > sembolun EN ERKEN bar tarihi - YAPISAL KORUMA.

    Ikincisi olmadan tasarim OPERASYONEL BIR ADIMA (`rescale --seed`)
    bagimli kalir: taze bir kurulumda seed atlanirsa ilk kosu, splits
    tablosundaki TUM tarihsel split'leri uygular ve Yahoo'dan ZATEN guncel
    olcekte gelmis barlari yeniden boler. Arsivden eski bir split'in
    uygulanacak bir isi yoktur - o barlar zaten split sonrasi olcekte
    geldi. Seed hala anlamlidir (denetim izi ve niyet beyani) ama artik
    dogrulugun TEK dayanagi degildir.
    """
    splits = Base.metadata.tables["splits"]
    applied = Base.metadata.tables["bar_rescales"]
    bars = Base.metadata.tables["price_bars"]
    earliest = (
        select(func.min(bars.c["local_date"])).where(bars.c["symbol"] == symbol).scalar_subquery()
    )
    stmt = (
        select(splits.c["split_date"], splits.c["ratio"])
        .outerjoin(
            applied,
            (applied.c["symbol"] == splits.c["symbol"])
            & (applied.c["split_date"] == splits.c["split_date"]),
        )
        .where(
            splits.c["symbol"] == symbol,
            applied.c["symbol"].is_(None),
            # earliest NULL ise (sembolun hic bari yok) kosul NULL doner ve
            # satir ELENIR - dogrusu budur: olceklenecek arsiv yoktur.
            splits.c["split_date"] > earliest,
        )
        .order_by(splits.c["split_date"])
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]


def seed_baseline(session: Session) -> int:
    """`yfin rescale --seed`: mevcut TUM split'ler icin baseline kaydi.

    BU ADIM ATLANIRSA ARSIV YOK OLUR. `splits` tablosu mevcut hat
    tarafindan zaten doludur (AAPL'in 1987, 2000, 2005, 2014, 2020
    split'leri dahil). Tetikleyici "karsiligi olmayan her split" oldugu
    icin, bos bir bar_rescales ile yapilan ILK KOSU tarihsel split'lerin
    tamamini uygular ve AAPL arsivini 2*2*2*7*4 = 224'e boler - oysa o
    barlar Yahoo'dan ZATEN guncel olcekte gelmistir.

    Idempotenttir: var olan kayitlara dokunmaz. `bars_*` ilk kez kosmadan
    ONCE calismak zorundadir (PB S10/9a).
    """
    now = datetime.now(UTC)
    result = session.execute(
        text(
            "INSERT INTO bar_rescales (symbol, split_date, ratio, applied_at, rows_affected) "
            "SELECT s.symbol, s.split_date, 1, :now, 0 FROM splits s "
            "LEFT JOIN bar_rescales r "
            "  ON r.symbol = s.symbol AND r.split_date = s.split_date "
            "WHERE r.symbol IS NULL"
        ),
        {"now": now},
    )
    seeded = _rowcount(result)
    log.info("rescale baseline seeded", rows=seeded)
    return seeded


def apply_pending(session: Session, symbol: str) -> int:
    """Bu sembolun bekleyen split'lerini uygular; uygulanan sayisini doner.

    CAGRI YERI: sembolun KENDI yazma transaction'i icinde, `bars_*`
    yazimindan ONCE (PB S6.6). Ters sirada, ayni kosuda yazilan yeni
    barlar (zaten yeni olcekte) bir kez daha bolunurdu.
    """
    pending = pending_splits(session, symbol)
    if not pending:
        return 0

    timezone_name = _symbol_timezone(session, symbol)
    applied = 0
    for split_day, ratio in pending:
        try:
            price_factor, volume_factor = rescale_factors(ratio)
            boundary = split_boundary_utc(split_day, timezone_name)
        except RescaleSkipped as exc:
            # Kayit YAZILMAZ: yazilsaydi split "uygulandi" sayilir ve
            # dogru veri bir daha asla olceklenmezdi.
            log.error("rescale atlandi", symbol=symbol, split_date=str(split_day), reason=str(exc))
            continue
        applied += _apply_one(
            session, symbol, split_day, ratio, price_factor, volume_factor, boundary
        )
    return applied


def _apply_one(
    session: Session,
    symbol: str,
    split_day: date,
    ratio: Decimal,
    price_factor: Decimal,
    volume_factor: Decimal,
    boundary: datetime,
) -> int:
    """Tek split: once SLOTU AL, sonra UPDATE et.

    Slot `INSERT ... ON CONFLICT DO NOTHING` ile alinir, kilitle DEGIL.
    Ilk tasarim `SELECT ... FOR UPDATE` oneriyordu; olcum bunun
    CALISMADIGINI gosterdi: var olmayan bir PK uzerindeki FOR UPDATE
    yalnizca bir GAP LOCK alir, gap lock'lar birbiriyle uyumludur, iki
    oturum da "satir yok, uygulayacagim" der ve cakisma INSERT aninda
    tekillik ihlali (23505) olarak patlar.

    rowcount 1 ise slot bizimdir; 0 ise baska bir oturum onceden
    almistir ve UPDATE calistirilmaz.
    """
    now = datetime.now(UTC)
    claim = session.execute(
        text(
            "INSERT INTO bar_rescales (symbol, split_date, ratio, applied_at, rows_affected) "
            "VALUES (:symbol, :split_date, :ratio, :now, 0) "
            "ON CONFLICT (symbol, split_date) DO NOTHING"
        ),
        {"symbol": symbol, "split_date": split_day, "ratio": ratio, "now": now},
    )
    if not _rowcount(claim):
        return 0  # baska bir oturum almis

    # 1wk/1mo KAPSAM DISI (PB S6.6/1): bu iki interval her kosuda
    # period="max" ile bastan cekilir, yani daima Yahoo'nun guncel
    # olcegindedir. Olceklenirlerse ve o kosuda fetch duserse satirlar
    # CIFT duzeltilmis kalir; bar_rescales split'i "uygulandi" saydigi
    # icin de bir daha duzelmez.
    #
    # FILTRE ARTIK YAPISAL OLARAK GEREKSIZ -- `price_bars` yalnizca
    # intraday tasiyor, 1wk/1mo `periodic_bars`ta (PG S7.1). Yine de
    # BIRAKILDI: kural kodda GORUNUR kalsin ve tablo bir gun yeniden
    # birlestirilirse sessizce bozulmasin. Maliyeti bir IN yan tumcesi.
    placeholders = ", ".join(f":iv{i}" for i in range(len(INTRADAY_INTERVALS)))
    params: dict[str, object] = {
        "symbol": symbol,
        "boundary": boundary,
        "price_factor": price_factor,
        "volume_factor": volume_factor,
    }
    params.update({f"iv{i}": iv for i, iv in enumerate(INTRADAY_INTERVALS)})
    updated = session.execute(
        text(
            "UPDATE price_bars SET "
            "  open = open * :price_factor, "
            "  high = high * :price_factor, "
            "  low = low * :price_factor, "
            "  close = close * :price_factor, "
            # FLOOR SART: 3:2 split'te volume*1.5 kesirli cikar. FLOOR
            # olmadan `numeric` deger `bigint` kolona atanirken YUVARLANIR;
            # FLOOR ile kesme davranisi ACIKTIR ve niyet kodda gorunur.
            #
            # Bu UPDATE artik bir HYPERTABLE'a gidiyor; `ts_utc < :boundary`
            # kosulu sayesinde yalnizca ilgili chunk'lara dokunur. Tam da
            # bu geriye donuk yazma yuzunden compression ACILMADI
            # (PG S7.3): sikistirilmis chunk'ta UPDATE chunk'i acmayi
            # gerektirir ve bir split tum tarihsel arsive dokunabilir.
            "  volume = FLOOR(volume * :volume_factor) "
            "WHERE symbol = :symbol AND ts_utc < :boundary "
            f"  AND bar_interval IN ({placeholders})"
        ),
        params,
    )
    rows = _rowcount(updated)
    session.execute(
        text(
            "UPDATE bar_rescales SET rows_affected = :rows "
            "WHERE symbol = :symbol AND split_date = :split_date"
        ),
        {"rows": rows, "symbol": symbol, "split_date": split_day},
    )
    log.info(
        "rescale uygulandi",
        symbol=symbol,
        split_date=str(split_day),
        ratio=str(ratio),
        rows=rows,
    )
    return 1


def unseeded_historic_splits(session: Session) -> int:
    """Tohumlanmamis TARIHSEL split sayisi (bakim isi uyarisi, PB S10/9a).

    price_bars'in en erken barindan ESKI olup bar_rescales'te karsiligi
    olmayan split, `--seed`in atlandigina isarettir.
    """
    splits = Base.metadata.tables["splits"]
    applied = Base.metadata.tables["bar_rescales"]
    bars = Base.metadata.tables["price_bars"]
    earliest = select(func.min(bars.c["local_date"])).scalar_subquery()
    stmt = (
        select(func.count())
        .select_from(
            splits.outerjoin(
                applied,
                (applied.c["symbol"] == splits.c["symbol"])
                & (applied.c["split_date"] == splits.c["split_date"]),
            )
        )
        .where(applied.c["symbol"].is_(None), splits.c["split_date"] < earliest)
    )
    return int(session.execute(stmt).scalar_one())
