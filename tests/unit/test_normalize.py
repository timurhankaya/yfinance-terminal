"""S8.3'teki her kural icin ayri test (S9.1)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from yfin import normalize as nz


class TestDecimalConversion:
    def test_numpy_float_does_not_raise(self) -> None:
        """Ciplak Decimal(repr(x)) numpy 2.x'te InvalidOperation firlatir:
        repr(np.float64(0.00187)) == 'np.float64(0.00187)'."""
        assert repr(np.float64(0.00187)).startswith("np.float64")
        assert nz.to_decimal(np.float64(0.00187)) == Decimal("0.00187")

    def test_no_binary_residue(self) -> None:
        """Decimal(float) ikili artik uretir; repr(float(x)) uretmez."""
        assert nz.to_decimal(np.float64(0.128348)) == Decimal("0.128348")
        assert str(nz.to_decimal(0.001870)) == "0.00187"

    def test_int_and_decimal_pass_through(self) -> None:
        assert nz.to_decimal(np.int64(5)) == Decimal(5)
        assert nz.to_decimal(Decimal("1.5")) == Decimal("1.5")

    def test_missing_returns_none(self) -> None:
        for value in (None, float("nan"), np.float64("nan"), pd.NA, pd.NaT, "-", ""):
            assert nz.to_decimal(value) is None


class TestSentinels:
    @pytest.mark.parametrize("value", ["-", "", "N/A", "  -  "])
    def test_sentinel_becomes_null(self, value: str) -> None:
        assert nz.to_str(value) is None

    def test_real_value_survives(self) -> None:
        assert nz.to_str("  US0378331005 ") == "US0378331005"


class TestEmptyResult:
    def test_none_is_empty_not_attribute_error(self) -> None:
        """get_shares_full None donebilir; None.empty AttributeError verir."""
        assert nz.is_empty_result(None) is True

    def test_empty_series_object_dtype(self) -> None:
        assert nz.is_empty_result(pd.Series([], dtype=object)) is True

    def test_non_empty(self) -> None:
        assert nz.is_empty_result(pd.Series([1.0])) is False


class TestTimezone:
    def test_positive_offset_date_does_not_shift_back(self) -> None:
        """THYAO 2000-05-10 00:00+03:00 UTC'de 2000-05-09 21:00'dir;
        session_date yerel tarihi korumalidir."""
        ts = pd.Timestamp("2000-05-10 00:00:00+03:00")
        assert nz.to_local_date(ts) == date(2000, 5, 10)
        assert nz.to_datetime_utc(ts) == datetime(2000, 5, 9, 21, 0, tzinfo=UTC)

    def test_negative_offset_same_day(self) -> None:
        ts = pd.Timestamp("2026-09-03 00:00:00-04:00")
        assert nz.to_local_date(ts) == date(2026, 9, 3)
        assert nz.to_datetime_utc(ts) == datetime(2026, 9, 3, 4, 0, tzinfo=UTC)

    def test_utc_result_is_naive(self) -> None:
        assert nz.to_datetime_utc(datetime(2020, 1, 1, tzinfo=UTC)).tzinfo is UTC


class TestEpochMap:
    def test_all_21_fields_mapped(self) -> None:
        assert len(nz.EPOCH_SEC_FIELDS) + len(nz.EPOCH_MS_FIELDS) == 21

    def test_milliseconds_field(self) -> None:
        assert nz.convert_epoch_field("firstTradeDateMilliseconds", 345479400000) == datetime(
            1980, 12, 12, 14, 30, tzinfo=UTC
        )

    def test_seconds_field(self) -> None:
        assert nz.convert_epoch_field("regularMarketTime", 345479400) == datetime(
            1980, 12, 12, 14, 30, tzinfo=UTC
        )

    @pytest.mark.parametrize("key", ["fullTimeEmployees", "allTimeHigh", "isEarningsDateEstimate"])
    def test_not_epoch_fields_rejected(self, key: str) -> None:
        with pytest.raises(KeyError):
            nz.convert_epoch_field(key, 1)

    def test_unmapped_epoch_like_is_reported(self) -> None:
        mapped = nz.EPOCH_SEC_FIELDS | nz.EPOCH_MS_FIELDS
        found = nz.warn_unmapped_epoch_like({"someNewDate": 1750000000}, mapped)
        assert found == ["someNewDate"]

    def test_known_non_epoch_not_reported(self) -> None:
        mapped = nz.EPOCH_SEC_FIELDS | nz.EPOCH_MS_FIELDS
        assert nz.warn_unmapped_epoch_like({"fullTimeEmployees": 1500000000}, mapped) == []


class TestCanonicalJson:
    def test_nan_never_reaches_mysql(self) -> None:
        """allow_nan=True ile NaN sizarsa content_hash her kosuda ayrisir ve tum
        sembolun transaction'i geri alinir."""
        out = nz.canonical_json({"x": float("nan"), "y": np.float64("inf")})
        assert "NaN" not in out and "Infinity" not in out
        assert json.loads(out) == {"x": None, "y": None}

    def test_keys_sorted_and_compact(self) -> None:
        assert nz.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_unicode_preserved(self) -> None:
        assert "Türk" in nz.canonical_json({"n": "Türk"})

    def test_hash_matches_body(self) -> None:
        payload = {"a": 1, "b": [1, 2]}
        canonical = nz.canonical_json(payload)
        import hashlib

        assert (
            nz.content_hash(canonical=canonical) == hashlib.sha256(canonical.encode()).hexdigest()
        )
        assert nz.content_hash(payload) == nz.content_hash(canonical=canonical)


class TestHistoryMetadataEncoder:
    def test_dataframe_and_timestamps_serialize(self) -> None:
        """tradingPeriods bir DataFrame; duz json.dumps TypeError verir."""
        frame = pd.DataFrame(
            {
                "start": [pd.Timestamp("2026-09-03 09:30:00-04:00")],
                "end": [pd.Timestamp("2026-09-03 16:00:00-04:00")],
            }
        )
        payload = {
            "tradingPeriods": frame,
            "firstTradeDate": pd.Timestamp("1980-12-12 09:30:00-05:00"),
            "YF repair?": False,
        }
        with pytest.raises(TypeError):
            json.dumps(payload)
        out = nz.canonical_json(payload)
        assert json.loads(out)["YF repair?"] is False
        assert isinstance(json.loads(out)["tradingPeriods"], list)

    def test_mapping_that_is_not_dict(self) -> None:
        from collections.abc import Mapping

        class NotADict(Mapping):  # type: ignore[type-arg]
            _d = {"a": 1}

            def __getitem__(self, k):  # type: ignore[no-untyped-def]
                return self._d[k]

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self._d)

            def __len__(self) -> int:
                return len(self._d)

        value = NotADict()
        assert not isinstance(value, dict)
        assert nz.canonical_json({"m": value}) == '{"m":{"a":1}}'


class TestNames:
    def test_symbol_canonical_form(self) -> None:
        assert nz.normalize_symbol("  thyao.is ") == "THYAO.IS"

    def test_double_space_in_officer_name(self) -> None:
        """Kaynakta cift bosluk var; normalize edilmezse duplike satir olusur."""
        assert nz.normalize_person_name("Mr. Kevan  Parekh") == "Mr. Kevan Parekh"


class TestNumericBoundaries:
    """Kolon kapasitesi ile Python'un Decimal context'i arasindaki sinir.

    Bu iki kural olmadan hata SESSIZ ya da TUM HUCREYI dusuren cinstendi;
    ikisi de denetimde `ok` gorunurdu.
    """

    def test_numpy_int_epoch_is_not_silently_null(self) -> None:
        """`np.int64` int'in alt sinifi DEGILDIR (np.float64 float'in ALT
        SINIFIDIR). Duz `isinstance(v, int | float)` ile epoch degeri
        `to_datetime_utc`'ye duser ve None doner: SESSIZ NULL. Bir sembolun
        insider tarihlerinin TAMAMI doluysa pandas kolonu int64 yapar ve
        uc tarih kolonu birden NULL olurdu.
        """
        from yfin.models.kinds import KINDS

        convert = KINDS["dt"].convert
        expected = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
        assert convert(np.int64(1700000000)) == expected
        assert convert(np.float64(1700000000.0)) == expected
        assert convert(1700000000) == expected

    def test_bool_is_not_an_epoch(self) -> None:
        """`bool` Integral'dir ama tarih degildir."""
        from yfin.models.kinds import KINDS

        assert KINDS["dt"].convert(True) is None

    def test_fact_value_limit_matches_the_column(self) -> None:
        """DECIMAL(38,10) = 28 tam hane. Python'un VARSAYILAN context'i 28
        ANLAMLI hane tasidigi icin duz `quantize` 1e18'de InvalidOperation
        firlatirdi -- ve istisna `normalize`dan cikip o (sembol x dataset)
        hucresinin TUM satirlarini dusururdu.
        """
        from yfin.datasets.common import to_fact_value

        assert to_fact_value(Decimal("1e18")) is not None
        assert to_fact_value(Decimal("1e27")) is not None  # kolonun sinirinda
        assert to_fact_value(Decimal("1e28")) is None  # gercekten tasar -> satir duser

    def test_big_value_limit_matches_the_column(self) -> None:
        """DECIMAL(38,0) = 38 tam hane."""
        from yfin.models.kinds import KINDS

        convert = KINDS["big"].convert
        assert convert(Decimal("1e28")) is not None
        assert convert(Decimal("1e37")) is not None
        assert convert(Decimal("1e39")) is None

