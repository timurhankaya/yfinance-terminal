"""Domain dataset'lerinin fetch ciktilari (SI S7).

`normalize` DB'ye ve aga dokunmaz; ihtiyac duydugu her sey buradan gelir.
`fetched_at` ve `as_of_date` payload'da tasinir cunku ikisi de RUN basina
uretilir ve normalize'in `datetime.now()` cagirmasi testleri
belirsizlestirirdi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class DomainPayload:
    """Tek bir (anahtar, bolge) ciftinin ham yaniti."""

    data: dict[str, Any]
    fetched_at: datetime
    as_of_date: date
    region: str
    # Bolgesiz dataset'lerin satirlarina `region` YAZILMAZ; bu alan yalniz
    # bolgeli tablolarin PK'sini besler.
    domain_type: str = "sector"
    # DB'deki ebeveyn sektor anahtari (yalniz endustri profilinde kullanilir)
    expected_parent: str | None = None


@dataclass(frozen=True)
class TaxonomyPayload:
    """11 sektorun ham yaniti; bootstrap TEK turda hepsini isler."""

    sectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_at: datetime = datetime.min
