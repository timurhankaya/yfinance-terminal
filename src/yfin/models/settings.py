"""DB tabanli yapilandirma tablosu (CFG S4.1).

Tablo `Settings` alanlarinin DB EZMELERINI tutar; `.env`in verdiginin
AYNI bicimde, yani METIN olarak. Deger tipini, aralik kisitini ve
varsayilani tabloda TEKRARLAMAK bilincli olarak reddedildi (CFG S2):
`Field(ge=1)` bir gun `ge=2` olsaydi tablodaki kopya sessizce bayatlardi.
Metadata'nin tek dogruluk kaynagi `config.Settings` modelidir.

Tablo MOTORDAN BAGIMSIZ yazilmistir (CFG S2): yalnizca `AsciiKeyType`,
`Text` ve `TsType` kullanir, motora ozgu hicbir tablo argumani tasimaz.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import AsciiKeyType, Base, TsType

SETTING_KEY_LENGTH = 64


class SettingRow(Base):
    """Tek bir yapilandirma ezmesi.

    SATIR SAYISI BIR DEGISMEZ DEGILDIR (CFG S4.1): `set` artirir, `unset`
    azaltir ve satirin OLMAMASI mesrudur -- "ezme yok" demenin tek yolu
    budur. NULL bu is icin KULLANILAMAZDI cunku bos dize mesru bir
    degerdir (`yf_news_tab=""`).
    """

    __tablename__ = "settings"

    # KOLON ADI `setting_key`, `key` DEGIL: `KEY` MySQL 8'de ayrilmis
    # sozcuktur ve kod tabani bu tuzagi iki kez belgelemis
    # (models/base.py `bar_interval`, models/funds.py `holding_rank`).
    # Collation "C" (duyarli) olmasinin amaci CAKISMAYI onlemek DEGIL:
    # ham SQL ile sokulmus `YF_MAX_SHARDS` satirinin kanonik
    # `yf_max_shards`tan AYRI ve GORUNUR kalmasi, boylece yukleyicinin
    # "bilinmeyen anahtar" uyarisina takilmasidir (CFG S2).
    setting_key: Mapped[str] = mapped_column(AsciiKeyType(SETTING_KEY_LENGTH), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now(6)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now(6), server_onupdate=func.now(6)
    )
