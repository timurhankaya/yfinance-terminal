"""options dataset -> option_expirations + option_quotes.

**Opt-in, and the reason is arithmetic.** One request returns the expiry
LIST plus the first expiry's chain; every expiry after that is another
request. A symbol carries ten to twenty expiries, so collecting them all
would multiply a run by fifteen -- against a universe that already takes
~33 hours on one IP (`docs/superpowers/specs/2026-09-07-kalan-isler.md`).
So this never joins the `all` expansion and runs only when named:
`yfin sync --datasets options`. `yf_option_expiries` bounds it further
for whoever does name it.

Two tables, because there are two facts. The expiry list says which
contracts Yahoo was offering that day and is complete, since it comes
free with the first request; the chain is fetched for the first N
expiries only. Neither is a summary of the other.

Calls and puts share `option_quotes` and are told apart by an ENUM.
Their column sets are identical by construction -- yfinance builds both
frames with the same `reindex` call -- which is exactly the case this
codebase answers with one table plus a discriminator
(`institutional_holders` + `holder_type`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import key_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.models.options import CONTRACT_SYMBOL_LENGTH, OptionType
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

EXPIRATIONS_TABLE = "option_expirations"
QUOTES_TABLE = "option_quotes"

EXPIRATION_KEY = ("symbol", "as_of_date", "expiry_date")
EXPIRATION_SCOPE = ("symbol", "as_of_date")

QUOTE_KEY = ("symbol", "as_of_date", "expiry_date", "option_type", "contract_symbol")
#: Only the expiries actually fetched are cleared. A chain the run did not
#: ask for keeps yesterday's rows rather than being emptied by a run that
#: never looked at it.
QUOTE_SCOPE = ("symbol", "as_of_date", "expiry_date")

QUOTE_DATA_COLUMNS = (
    "strike",
    "last_price",
    "bid",
    "ask",
    "change",
    "percent_change",
    "implied_volatility",
    "volume",
    "open_interest",
    "in_the_money",
    "contract_size",
    "currency",
    "last_trade_ts_utc",
)

#: The fourteen columns `Ticker._options2df` forces onto the frame, mapped
#: to ours. Anything Yahoo adds later shows up as an unmapped key in the
#: log rather than being silently dropped.
COLUMN_MAP: dict[str, str] = {
    "contractSymbol": "contract_symbol",
    "lastTradeDate": "last_trade_ts_utc",
    "strike": "strike",
    "lastPrice": "last_price",
    "bid": "bid",
    "ask": "ask",
    "change": "change",
    "percentChange": "percent_change",
    "volume": "volume",
    "openInterest": "open_interest",
    "impliedVolatility": "implied_volatility",
    "inTheMoney": "in_the_money",
    "contractSize": "contract_size",
    "currency": "currency",
}


@dataclass
class OptionsPayload:
    """Every expiry Yahoo offered, and the chains actually fetched.

    `expiries` is the whole list; `chains` holds only the first
    `yf_option_expiries` of them, so the two are deliberately different
    lengths and the tables below say so.
    """

    expiries: list[str]
    chains: list[tuple[str, pd.DataFrame | None, pd.DataFrame | None]]
    fetched_at: datetime


class OptionsDataset(AsOfDataset[OptionsPayload]):
    name = "options"
    depends_on = ("symbols",)
    produces = asof_produces(EXPIRATIONS_TABLE, QUOTES_TABLE)
    #: The expiry list is written whenever the symbol has options at all,
    #: even when every chain came back empty, so it is the honest source
    #: for the gate.
    gate_source_tables = (EXPIRATIONS_TABLE,)
    api = (
        ApiExposure(
            name="option_expirations",
            family=DataFamily.DERIVATIVES,
            table=EXPIRATIONS_TABLE,
            sort_key=("as_of_date", "expiry_date"),
            descending=True,
            description="Which option expiries a symbol offered, by day.",
        ),
        ApiExposure(
            name="option_quotes",
            family=DataFamily.DERIVATIVES,
            table=QUOTES_TABLE,
            sort_key=("as_of_date", "expiry_date", "contract_symbol"),
            descending=True,
            filters=("expiry_date",),
            description="Option chains: strike, quote and open interest per contract.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> OptionsPayload:
        expiries = call_optional(lambda: list(ctx.ticker.options), what=f"{self.name}:{ctx.symbol}")
        if not expiries:
            return OptionsPayload(expiries=[], chains=[], fetched_at=ctx.fetched_at)
        wanted = expiries[: max(1, get_settings().yf_option_expiries)]
        chains: list[tuple[str, pd.DataFrame | None, pd.DataFrame | None]] = []
        for expiry in wanted:
            # Per expiry rather than in one call because that is what the
            # endpoint offers; `call_optional` per chain so one expiry
            # going missing does not cost the symbol its other three.
            # `partial`, not a lambda with a bound default: the repo's
            # own answer to this loop-variable question (`domain/taxonomy`),
            # and mypy can resolve it.
            chain = call_optional(
                partial(ctx.ticker.option_chain, expiry),
                what=f"{self.name}:{ctx.symbol}:{expiry}",
            )
            if chain is None:
                continue
            chains.append((expiry, chain.calls, chain.puts))
        return OptionsPayload(expiries=expiries, chains=chains, fetched_at=ctx.fetched_at)

    def normalize(self, raw: OptionsPayload, symbol: str) -> NormalizedResult:
        as_of = raw.fetched_at.date()
        if not raw.expiries:
            # Nothing is deleted on an empty result. `call_optional`
            # cannot tell a transient 404 from a symbol that genuinely has
            # no options, and a `replace_scope` here would destroy the
            # day's rows on a blip (the rule `institutional_holders`
            # already writes down).
            return NormalizedResult()

        writes = [
            TableWrite(
                table=EXPIRATIONS_TABLE,
                rows=[
                    {
                        "symbol": symbol,
                        "as_of_date": as_of,
                        "expiry_date": expiry,
                        "fetched_at": raw.fetched_at,
                    }
                    for expiry in sorted(_expiry_dates(raw.expiries, symbol))
                ],
                key_columns=EXPIRATION_KEY,
                update_columns=("fetched_at",),
                mode="replace_scope",
                scope_columns=EXPIRATION_SCOPE,
                # Stated rather than read off the rows: an expiry that
                # dropped off the list has to lose its row, and a scope
                # derived from the rows could never remove it.
                scope_values=({"symbol": symbol, "as_of_date": as_of},),
            )
        ]

        quotes: list[dict[str, Any]] = []
        scopes: list[dict[str, Any]] = []
        for expiry, calls, puts in raw.chains:
            expiry_date = nz.to_local_date(expiry)
            if expiry_date is None:
                continue
            scopes.append({"symbol": symbol, "as_of_date": as_of, "expiry_date": expiry_date})
            for option_type, frame in ((OptionType.CALL, calls), (OptionType.PUT, puts)):
                quotes.extend(
                    self._rows(frame, symbol, as_of, expiry_date, option_type, raw.fetched_at)
                )
        if scopes:
            writes.append(
                TableWrite(
                    table=QUOTES_TABLE,
                    rows=quotes,
                    key_columns=QUOTE_KEY,
                    update_columns=(*QUOTE_DATA_COLUMNS, "fetched_at"),
                    mode="replace_scope",
                    scope_columns=QUOTE_SCOPE,
                    scope_values=tuple(scopes),
                )
            )
        return NormalizedResult(writes=writes)

    def _rows(
        self,
        frame: pd.DataFrame | None,
        symbol: str,
        as_of: date,
        expiry_date: date,
        option_type: OptionType,
        fetched_at: datetime,
    ) -> list[dict[str, Any]]:
        if nz.is_empty_result(frame):
            return []
        assert isinstance(frame, pd.DataFrame)
        unmapped = sorted(str(c) for c in frame.columns if str(c) not in COLUMN_MAP)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        rows: dict[str, dict[str, Any]] = {}
        for _, record in frame.iterrows():
            contract = key_value(
                record.get("contractSymbol"),
                CONTRACT_SYMBOL_LENGTH,
                field="contract_symbol",
                dataset=self.name,
                symbol=symbol,
            )
            strike = nz.to_decimal(record.get("strike"))
            # A contract with no strike is not a contract, and `strike` is
            # NOT NULL: dropping the row here is what keeps one malformed
            # entry from rolling back the whole symbol.
            if contract is None or strike is None:
                continue
            rows[contract] = {
                "symbol": symbol,
                "as_of_date": as_of,
                "expiry_date": expiry_date,
                "option_type": option_type.value,
                "contract_symbol": contract,
                "strike": strike,
                "last_price": nz.to_decimal(record.get("lastPrice")),
                "bid": nz.to_decimal(record.get("bid")),
                "ask": nz.to_decimal(record.get("ask")),
                "change": nz.to_decimal(record.get("change")),
                "percent_change": nz.to_decimal(record.get("percentChange")),
                "implied_volatility": nz.to_decimal(record.get("impliedVolatility")),
                "volume": nz.to_int(record.get("volume")),
                "open_interest": nz.to_int(record.get("openInterest")),
                "in_the_money": nz.to_bool(record.get("inTheMoney")),
                "contract_size": nz.to_str(record.get("contractSize"), 16),
                "currency": nz.to_str(record.get("currency"), 8),
                # Already UTC-aware: `_options2df` converts the epoch
                # seconds Yahoo sends.
                "last_trade_ts_utc": nz.to_datetime_utc(record.get("lastTradeDate")),
                "fetched_at": fetched_at,
            }
        return list(rows.values())


def _expiry_dates(expiries: list[str], symbol: str) -> set[date]:
    parsed: set[date] = set()
    for raw in expiries:
        value = nz.to_local_date(raw)
        if value is None:
            log.warning("unparsable expiry", dataset="options", symbol=symbol, value=str(raw))
            continue
        parsed.add(value)
    return parsed


# Opt-in: registered always, expanded never. See the module docstring for
# the arithmetic.
register(OptionsDataset(), opt_in=True)
