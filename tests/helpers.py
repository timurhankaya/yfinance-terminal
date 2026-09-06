"""Fixture yukleme ve rehydration yardimcilari."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


# --- surece ozel test semasi ----------------------------------------------


def schema_name(base: str) -> str:
    """Test semasi SURECE ozeldir: `<base>_<pid>`.

    Sabit tek sema kullanildiginda iki pytest kosusu (ornegin iki editor
    oturumu) birbirinin tablolarini `drop_all` ile dusuruyordu; belirti
    es zamanli DDL hatalari ve tekrarlanmayan satir-sayisi
    uyusmazliklariydi. Sema adini surece baglamak bu sinifi imkansiz
    kilar.

    PostgreSQL'de SEMA kullanilir, VERITABANI DEGIL: `CREATE DATABASE`
    transaction disinda calismak zorundadir, sablon veritabanini kopyalar
    ve her yeni veritabaninda `CREATE EXTENSION timescaledb` gerektirir;
    `CREATE SCHEMA` siradan bir DDL'dir ve `DROP SCHEMA ... CASCADE`
    chunk'lari da temizler (PG S9.1).
    """
    return f"{base}_{os.getpid()}"


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # baska kullanicinin sureci: yasiyor say
        return True
    return True


def drop_stale_schemas(engine: Engine, base: str) -> list[str]:
    """Kesilen kosulardan kalan `<base>_<pid>` semalarini dusurur.

    Ctrl-C ve cokmeler teardown'i atlar; temizlik olmasaydi semalar
    sinirsiz birikirdi. YALNIZCA PID'i artik yasamayan semalar silinir,
    boylece ES ZAMANLI bir kosunun semasina dokunulmaz. Taban ad
    (`<base>`, sayisal son eki yok) da korunur.

    `engine` TEST VERITABANINA bagli olmalidir, bootstrap (`postgres`)
    baglantisina DEGIL: PostgreSQL'de `information_schema.schemata`
    VERITABANINA OZELDIR ve bootstrap baglantisi test veritabanindaki
    semalari GOREMEZ. Yanlis engine ile temizlik SESSIZCE hicbir sey
    yapar ve semalar sonsuza kadar birikir (PG S9.1).
    """
    prefix = f"{base}_"
    dropped: list[str] = []
    with engine.connect() as conn:
        names = list(
            conn.execute(text("SELECT schema_name FROM information_schema.schemata")).scalars()
        )
        for name in names:
            suffix = name[len(prefix) :] if name.startswith(prefix) else ""
            if not suffix.isdigit() or pid_is_alive(int(suffix)):
                continue
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            conn.commit()
            dropped.append(name)
    return dropped


def load_fixture(symbol: str, dataset: str) -> Any:
    path = FIXTURE_ROOT / symbol / f"{dataset}.json"
    if not path.exists():
        pytest.skip(f"fixture yok: {path} (scripts/capture_fixtures.py calistirin)")
    return json.loads(path.read_text(encoding="utf-8"))


def load_domain_fixture(kind: str, key: str, region: str = "US") -> Any:
    """Ham domain zarfini ({"data": {...}}) dondurur (SI S9.1).

    Zarf ACILMAZ: `fetch_domain` `payload["data"]` okur ve testler ayni
    yoldan gecmelidir.
    """
    suffix = "" if region == "US" else f".{region}"
    path = FIXTURE_ROOT / "_domain" / kind / f"{key}{suffix}.json"
    if not path.exists():
        pytest.skip(
            f"fixture yok: {path} "
            "(python scripts/capture_fixtures.py --domain calistirin)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def domain_data(kind: str, key: str, region: str = "US") -> Any:
    return load_domain_fixture(kind, key, region)["data"]


def domain_fixture_keys(kind: str) -> list[tuple[str, str]]:
    """Mevcut domain fixture'larinin (anahtar, bolge) listesi."""
    root = FIXTURE_ROOT / "_domain" / kind
    if not root.exists():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.json")):
        stem = path.stem
        key, _, region = stem.partition(".")
        out.append((key, region or "US"))
    return out


# --- fixture rehydration --------------------------------------------------


def as_series(records: Any) -> Any:
    """capture_fixtures.py'nin [{index, value}] bicimini Series'e cevirir."""
    import pandas as pd

    if records is None:
        return None
    if not records:
        return pd.Series([], dtype=object)
    # DST gecisleri yuzunden ofsetler karisiktir (-05:00 / -04:00); tek bir
    # DatetimeIndex'e zorlamak tz bilgisini kaybettirir. Gercek API tz-aware
    # bir index dondurur ve normalize her elemani tek tek isler.
    index = pd.Index([pd.Timestamp(r["index"]) for r in records], dtype=object)
    return pd.Series([r["value"] for r in records], index=index)


def as_frame(records: Any) -> Any:
    """history fixture'ini DataFrame'e cevirir (index = Date)."""
    import pandas as pd

    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    index_col = "index" if "index" in frame.columns else "Date"
    index = pd.Index([pd.Timestamp(v) for v in frame[index_col]], dtype=object)
    frame = frame.drop(columns=[index_col])
    frame.index = index
    return frame


def as_statement_frame(records: Any) -> Any:
    """Finansal tablo fixture'ini DataFrame'e cevirir.

    Gercek API'de index kalem etiketi (str), kolonlar donem sonu
    (tz-naive Timestamp) doner; fixture da bu sekli korur.
    """
    import pandas as pd

    if not records:
        return pd.DataFrame()
    labels = [r["index"] for r in records]
    columns = [c for c in records[0] if c != "index"]
    data = {pd.Timestamp(col): [r.get(col) for r in records] for col in columns}
    return pd.DataFrame(data, index=pd.Index(labels, dtype=str))


def as_valuation_frame(records: Any) -> Any:
    """valuation fixture'i: kolon etiketleri HAM kalir.

    `as_statement_frame`ten farki budur: kaynak burada donem sonu Timestamp
    degil, 'Current' ve 'M/D/YYYY' DIZELERI dondurur. Kolonlar testte
    Timestamp'e cevrilseydi dataset'in tam da bu cevrimi yapan adimi
    (`period_columns`) hic kosmazdi.
    """
    import pandas as pd

    if not records:
        return pd.DataFrame()
    labels = [r["index"] for r in records]
    columns = [c for c in records[0] if c != "index"]
    data = {col: [r.get(col) for r in records] for col in columns}
    return pd.DataFrame(data, index=pd.Index(labels, dtype=str))


def as_calendar_frame(records: Any) -> Any:
    """Takvim fixture'i: index sembol/olay adi, kolonlar ham adlariyla."""
    import pandas as pd

    if not records:
        return pd.DataFrame()
    index = pd.Index([r["index"] for r in records], dtype=object)
    columns = [c for c in records[0] if c != "index"]
    frame = pd.DataFrame({c: [r.get(c) for r in records] for c in columns}, index=index)
    return frame


def as_earnings_frame(payload: Any) -> Any:
    """earnings_dates fixture'i: {"tz": ..., "records": [...]}.

    Gercek API tz-AWARE bir DatetimeIndex dondurur ve tz ADI onemlidir
    (THYAO.IS'te bile America/New_York); ISO string yalnizca ofseti tasir.
    """
    import pandas as pd

    records = payload.get("records") if isinstance(payload, dict) else payload
    if not records:
        return None
    # ISO stringler karisik ofset tasir (EDT/EST); utc=True ile once UTC'ye
    # normalize edilir, sonra kaynak tz'ye cevrilir
    index = pd.DatetimeIndex(pd.to_datetime([r["index"] for r in records], utc=True))
    tz = payload.get("tz") if isinstance(payload, dict) else None
    if tz:
        index = index.tz_convert(tz)
    columns = [c for c in records[0] if c != "index"]
    return pd.DataFrame({c: [r.get(c) for r in records] for c in columns}, index=index)


# --- AH: analiz / sahiplik / fon fixture'lari ------------------------------

_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}.*)?$")


def _revive(value: Any) -> Any:
    """ISO damgasini Timestamp'e geri cevirir.

    Fixture JSON'dur ve `Timestamp` orada dizeye duser; gercek API ise
    `Timestamp` dondurur. Cevrilmeseydi test, uretimde HIC olusmayan bir
    girdi sekliyle kosardi -- yani yesil kalirken hicbir sey kanitlamazdi.
    """
    if isinstance(value, str) and _ISO_TIMESTAMP.match(value):
        import pandas as pd

        try:
            return pd.Timestamp(value)
        except ValueError:
            return value
    return value


def as_dataset_frame(records: Any, *, datetime_index: bool = False) -> Any:
    """`_frame_records` ciktisini dataset'in gordugu cerceveye cevirir.

    `as_frame`ten farki: index'i KOSULSUZ Timestamp'e cevirmez. Bu ailede
    index kimi dataset'te tarih (upgrades_downgrades), kimi dataset'te
    donem etiketi ('0q'), kimi dataset'te sira numarasidir -- `pd.Timestamp('0')`
    ucuncusunde patlardi.
    """
    import pandas as pd

    if not records:
        return pd.DataFrame()
    columns = [c for c in records[0] if c != "index"]
    raw_index = [r["index"] for r in records]
    if datetime_index:
        index = pd.Index([pd.Timestamp(v) for v in raw_index], dtype=object)
    else:
        index = pd.Index(raw_index, dtype=object)
    data = {c: [_revive(r.get(c)) for r in records] for c in columns}
    return pd.DataFrame(data, index=index)


def as_funds_data(payload: Any) -> Any:
    """funds_data fixture'ini `_collect` ciktisi seklinde geri kurar."""
    if payload is None:
        return None
    frames = {"fund_operations", "top_holdings", "equity_holdings", "bond_holdings"}
    return {
        key: (as_dataset_frame(value) if key in frames else value)
        for key, value in payload.items()
    }
