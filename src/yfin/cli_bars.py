"""price_bars CLI komutlari: kapsam, bosluklar, bakim, olcekleme (PB S11).

`cli.py`den AYRI tutulur: o dosya 630 satirdi ve bu komutlarin hicbiri
mevcut komutlarla durum paylasmiyor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from yfin import normalize as nz
from yfin.models import BAR_INTERVALS, BarGap, IntradayScope
from yfin.rescale import apply_pending, seed_baseline, unseeded_historic_splits

scope_app = typer.Typer(help="Intraday kapsam yonetimi (intraday_scope)", no_args_is_help=True)
bars_app = typer.Typer(help="price_bars bakimi ve denetimi", no_args_is_help=True)


def _factory() -> sessionmaker[Session]:
    # Tembel import: cli.py bu modulu import ediyor, tersi dogrudan
    # yapilirsa dairesel import olusur.
    from yfin.cli import _session_factory

    return _session_factory()


# --- kapsam ---------------------------------------------------------------


@scope_app.command("add")
def scope_add(
    symbols: Annotated[list[str], typer.Argument(help="Sembol kodlari")],
    interval: Annotated[str, typer.Option("--interval", help="Bar interval'i")] = "1m",
    note: Annotated[str | None, typer.Option("--note")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="1m disi interval'e ILK kaydi onaylar")
    ] = False,
) -> None:
    """Sembolleri intraday_scope'a ekler.

    1m DISINDAKI bir interval'e ILK kaydi eklemek TEHLIKELIDIR: cozumleme
    "o interval icin en az bir satir var mi" diye baktigi icin tek bir
    satir diger TUM sembolleri kapsam disina atar (PB S5.4).
    """
    if interval not in BAR_INTERVALS:
        typer.echo(f"bilinmeyen interval: {interval}; gecerli: {', '.join(BAR_INTERVALS)}")
        raise typer.Exit(code=1)

    codes = [nz.normalize_symbol(s) for s in symbols]
    with _factory()() as session:
        already = session.execute(
            select(func.count())
            .select_from(IntradayScope)
            .where(IntradayScope.bar_interval == interval)
        ).scalar_one()
        if interval != "1m" and not already and not force:
            typer.echo(
                f"'{interval}' su an TUM evren icin kosuyor. Ilk kaydi eklemek diger "
                "butun sembolleri kapsam disina atar; onaylamak icin --force verin."
            )
            raise typer.Exit(code=1)

        now = datetime.now(UTC)
        for code in codes:
            session.merge(
                IntradayScope(
                    symbol=code, bar_interval=interval, enabled=True, added_at=now, note=note
                )
            )
        session.commit()
    typer.echo(f"{len(codes)} sembol {interval} kapsamina eklendi")


@scope_app.command("disable")
def scope_disable(
    symbols: Annotated[list[str], typer.Argument()],
    interval: Annotated[str, typer.Option("--interval")] = "1m",
) -> None:
    """Kapsamdan cikarir ama SATIRI SILMEZ (enabled=0).

    Silmek, o interval'in son satiriysa kapsami sessizce TUM evrene
    acardi; disable bu riski tasimaz.
    """
    codes = [nz.normalize_symbol(s) for s in symbols]
    with _factory()() as session:
        session.execute(
            update(IntradayScope)
            .where(IntradayScope.symbol.in_(codes), IntradayScope.bar_interval == interval)
            .values(enabled=False)
        )
        session.commit()
    typer.echo(f"{len(codes)} sembol {interval} kapsamindan cikarildi (enabled=0)")


@scope_app.command("list")
def scope_list(interval: Annotated[str | None, typer.Option("--interval")] = None) -> None:
    """Kapsam tablosunu ve her interval'in COZUMLENMIS anlamini gosterir."""
    with _factory()() as session:
        stmt = select(
            IntradayScope.bar_interval, IntradayScope.symbol, IntradayScope.enabled
        ).order_by(IntradayScope.bar_interval, IntradayScope.symbol)
        if interval:
            stmt = stmt.where(IntradayScope.bar_interval == interval)
        rows = list(session.execute(stmt))

    grouped: dict[str, list[tuple[str, bool]]] = {}
    for bar_interval, symbol, enabled in rows:
        grouped.setdefault(bar_interval, []).append((symbol, enabled))

    for name in BAR_INTERVALS:
        if interval and name != interval:
            continue
        entries = grouped.get(name, [])
        if not entries:
            meaning = "HICBIR sembol" if name == "1m" else "TUM evren"
            typer.echo(f"{name:5s} kayit yok -> {meaning}")
            continue
        active = sum(1 for _s, e in entries if e)
        typer.echo(f"{name:5s} {active} aktif / {len(entries)} kayit")
        for symbol, enabled in entries:
            typer.echo(f"      {'+' if enabled else '-'} {symbol}")


# --- bosluklar ve bakim ---------------------------------------------------


@bars_app.command("gaps")
def bars_gaps(
    symbol: Annotated[str | None, typer.Option("--symbol")] = None,
    interval: Annotated[str | None, typer.Option("--interval")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    open_only: Annotated[bool, typer.Option("--open-only")] = False,
) -> None:
    """Kacirilan pencereler (PB S5.5)."""
    with _factory()() as session:
        stmt = select(
            BarGap.symbol,
            BarGap.bar_interval,
            BarGap.gap_start_utc,
            BarGap.gap_end_utc,
            BarGap.reason,
            BarGap.resolved_at,
        ).order_by(BarGap.detected_at.desc())
        if symbol:
            stmt = stmt.where(BarGap.symbol == nz.normalize_symbol(symbol))
        if interval:
            stmt = stmt.where(BarGap.bar_interval == interval)
        if reason:
            stmt = stmt.where(BarGap.reason == reason)
        if open_only:
            stmt = stmt.where(BarGap.resolved_at.is_(None))
        rows = list(session.execute(stmt))

    if not rows:
        typer.echo("bosluk yok")
        return
    for sym, iv, start, end, why, resolved in rows:
        state = "cozuldu" if resolved else "ACIK"
        typer.echo(f"{sym:12s} {iv:4s} {start} -> {end}  {why:18s} {state}")


@bars_app.command("maintain")
def bars_maintain(dry_run: Annotated[bool, typer.Option("--dry-run")] = False) -> None:
    """Aylik bakim (PB S7.5). SILME YAPMAZ.

    IKI ADIM KALDIRILDI ve ikisi de motor degisiminin dogrudan
    sonucudur (PG S3.2, S7.2):

      * PARTITION ILERLETME. price_bars artik bir TimescaleDB
        hypertable'idir ve chunk'lari YAZMA ANINDA kendisi olusturur.
        "Aralik disi insert" kavrami yoktur, dolayisiyla bakimi atlamanin
        bir bedeli de yoktur. `maintenance.py` modulunun tamami silindi.

      * OKSUZ SATIR DENETIMI. FK'yi partition ugruna feda etmistik
        (MySQL ERROR 1506); hypertable referencing taraf olabildigi icin
        price_bars artik symbols'a FK TASIYOR ve butunluk DB seviyesinde
        garanti. Sorgu olu koda donusmustu.
    """
    with _factory()() as session:
        # 1) Tohum denetimi (PB S10/9a)
        unseeded = unseeded_historic_splits(session)
        if unseeded:
            typer.echo(
                f"UYARI: {unseeded} tarihsel split tohumlanmamis. `yfin rescale --seed` "
                "calistirilmadan `yfin sync --datasets bars` kosarsa ARSIV BOZULUR."
            )

        # 2) Bosluk ozeti
        for why, total, still_open in session.execute(
            text(
                # MySQL'de `SUM(x IS NULL)` boolean'i ortuk olarak int'e
                # ceviriyordu. PostgreSQL'de SUM(boolean) YOKTUR (42883);
                # FILTER hem dogru hem daha okunakli karsiliktir.
                "SELECT reason, COUNT(*), "
                "       COUNT(*) FILTER (WHERE resolved_at IS NULL) "
                "  FROM bar_gaps GROUP BY reason"
            )
        ).all():
            typer.echo(f"bosluk {why:18s} toplam {total}, acik {still_open}")

        if dry_run:
            session.rollback()
        else:
            session.commit()


@bars_app.command("rescale")
def bars_rescale(
    seed: Annotated[
        bool, typer.Option("--seed", help="Mevcut split'ler icin baseline kaydi")
    ] = False,
    symbol: Annotated[str | None, typer.Option("--symbol")] = None,
) -> None:
    """Geriye donuk olcekleme (PB S6.6).

    `--seed` KURULUMDA BIR KEZ, `bars_*` ilk kez kosmadan ONCE
    calistirilmalidir. Atlanirsa ilk kosu splits tablosundaki TUM tarihsel
    split'leri uygular ve Yahoo'dan zaten guncel olcekte gelmis barlari
    yeniden boler.
    """
    with _factory()() as session:
        if seed:
            count = seed_baseline(session)
            session.commit()
            typer.echo(f"baseline: {count} split tohumlandi")
            return
        if symbol is None:
            typer.echo("--seed ya da --symbol verin")
            raise typer.Exit(code=1)
        applied = apply_pending(session, nz.normalize_symbol(symbol))
        session.commit()
        typer.echo(f"{applied} split uygulandi")
