"""Where a written row goes: one family and one partition column per table.

Routing is a property of the TABLE, not of the dataset that wrote it. Six
datasets write `symbols`, two write `news`, three write `research_reports`;
a per-dataset family would send one table's rows to two topics and cost the
"one ACL line per family" promise the family scheme exists for. It also
keeps the earlier decision not to put a mandatory `family` on `Dataset`
(see `datasets/exposure.py`): exposure stays opt-in and fails closed, while
routing is exhaustive and is checked to be.

The partition column follows one rule, applied per table:

    `symbol` if the table has one, else `domain_key`, else `region`, else
    the table's own identifier.

The map is nevertheless written out in full rather than derived, so the
result of that rule is reviewable in a diff. `tests/unit/test_routing.py`
holds the map to the schema: every produced table is routed or named as
infrastructure, every route agrees with the family the API serves the table
under, and every partition column is a real column.

`INFRASTRUCTURE_TABLES` is the other half of the same decision. Its rows are
the pipeline talking to itself -- audit, gates, offsets, proxies, settings,
the outboxes -- and are never published. The three gate tables are called
out separately in `GATE_TABLES` because the write path has to recognise
them: their header writes carry `fetched_at` alone and would otherwise
acquire a predicate and a `RETURNING *` for a row nobody receives.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from yfin.core.families import DataFamily


@dataclass(frozen=True)
class Route:
    """The topic a table's rows land on, and the key they are ordered by."""

    family: DataFamily
    partition_column: str


_REFERENCE = DataFamily.REFERENCE
_BARS = DataFamily.BARS
_FUNDAMENTALS = DataFamily.FUNDAMENTALS
_HOLDERS = DataFamily.HOLDERS
_NEWS = DataFamily.NEWS
_DISCOVERY = DataFamily.DISCOVERY
_DOMAINS = DataFamily.DOMAINS


ROUTES: Final[Mapping[str, Route]] = {
    # --- reference ------------------------------------------------------
    "symbols": Route(_REFERENCE, "symbol"),
    "ticker_info": Route(_REFERENCE, "symbol"),
    "ticker_info_history": Route(_REFERENCE, "symbol"),
    "ticker_fast_info": Route(_REFERENCE, "symbol"),
    "ticker_fast_info_history": Route(_REFERENCE, "symbol"),
    "history_metadata": Route(_REFERENCE, "symbol"),
    "company_officers": Route(_REFERENCE, "symbol"),
    "market_status": Route(_REFERENCE, "region"),
    "market_status_history": Route(_REFERENCE, "region"),
    "market_summary": Route(_REFERENCE, "symbol"),
    "market_summary_history": Route(_REFERENCE, "symbol"),
    # --- bars -----------------------------------------------------------
    # `price_bars` and `periodic_bars` carry `bar_interval`; the rest are
    # keyed by a date, which is what makes a range event need `ts_column`.
    "price_bars": Route(_BARS, "symbol"),
    "periodic_bars": Route(_BARS, "symbol"),
    "price_history": Route(_BARS, "symbol"),
    "dividends": Route(_BARS, "symbol"),
    "splits": Route(_BARS, "symbol"),
    "capital_gains": Route(_BARS, "symbol"),
    # --- fundamentals ---------------------------------------------------
    "financial_facts": Route(_FUNDAMENTALS, "symbol"),
    "financial_periods": Route(_FUNDAMENTALS, "symbol"),
    "earnings_dates": Route(_FUNDAMENTALS, "symbol"),
    "earnings_history": Route(_FUNDAMENTALS, "symbol"),
    "analyst_estimates": Route(_FUNDAMENTALS, "symbol"),
    "analyst_eps_trend": Route(_FUNDAMENTALS, "symbol"),
    "analyst_eps_revisions": Route(_FUNDAMENTALS, "symbol"),
    "analyst_growth_estimates": Route(_FUNDAMENTALS, "symbol"),
    "analyst_price_targets": Route(_FUNDAMENTALS, "symbol"),
    "analyst_recommendations": Route(_FUNDAMENTALS, "symbol"),
    "analyst_grade_changes": Route(_FUNDAMENTALS, "symbol"),
    "sec_filings": Route(_FUNDAMENTALS, "symbol"),
    "sec_filing_exhibits": Route(_FUNDAMENTALS, "symbol"),
    # Written by the same fetch as the bars, but served -- and therefore
    # published -- as fundamentals; see the module docstring's rule about
    # the API and the topic agreeing.
    "shares_full": Route(_FUNDAMENTALS, "symbol"),
    "ticker_calendar": Route(_FUNDAMENTALS, "symbol"),
    "ticker_calendar_history": Route(_FUNDAMENTALS, "symbol"),
    "calendar_earnings": Route(_FUNDAMENTALS, "symbol"),
    "calendar_ipo": Route(_FUNDAMENTALS, "symbol"),
    "calendar_splits": Route(_FUNDAMENTALS, "symbol"),
    "calendar_economic": Route(_FUNDAMENTALS, "region"),
    # --- holders --------------------------------------------------------
    "holder_breakdown": Route(_HOLDERS, "symbol"),
    "institutional_holders": Route(_HOLDERS, "symbol"),
    "insider_roster": Route(_HOLDERS, "symbol"),
    "insider_activity": Route(_HOLDERS, "symbol"),
    "insider_transactions": Route(_HOLDERS, "symbol"),
    "fund_profile": Route(_HOLDERS, "symbol"),
    "fund_metrics": Route(_HOLDERS, "symbol"),
    "fund_top_holdings": Route(_HOLDERS, "symbol"),
    "fund_weightings": Route(_HOLDERS, "symbol"),
    # --- news -----------------------------------------------------------
    "news": Route(_NEWS, "news_id"),
    "news_symbols": Route(_NEWS, "symbol"),
    # --- discovery ------------------------------------------------------
    "lookup_results": Route(_DISCOVERY, "symbol"),
    "lookup_totals": Route(_DISCOVERY, "query_term"),
    "search_quotes": Route(_DISCOVERY, "symbol"),
    "search_lists": Route(_DISCOVERY, "query_term"),
    "search_report_hits": Route(_DISCOVERY, "query_term"),
    "screens": Route(_DISCOVERY, "screen_key"),
    "screen_runs": Route(_DISCOVERY, "screen_key"),
    "screen_members": Route(_DISCOVERY, "symbol"),
    "screen_quotes": Route(_DISCOVERY, "symbol"),
    # --- domains --------------------------------------------------------
    # `domains` and the three `domain_top_*` tables carry a company (or
    # index) `symbol`, so the rule keys them by it; only the two tables
    # without one fall through to `domain_key`.
    "domains": Route(_DOMAINS, "symbol"),
    "domain_metrics": Route(_DOMAINS, "domain_key"),
    "domain_report_links": Route(_DOMAINS, "domain_key"),
    "domain_top_companies": Route(_DOMAINS, "symbol"),
    "domain_top_funds": Route(_DOMAINS, "symbol"),
    "domain_top_movers": Route(_DOMAINS, "symbol"),
    "research_reports": Route(_DOMAINS, "report_id"),
}


#: Content-hash gate state. Produced by the datasets that gate on it, and
#: infrastructure all the same: the row says "we checked", not "this moved".
GATE_TABLES: Final[frozenset[str]] = frozenset(
    {"asof_state", "domain_asof_state", "discovery_asof_state"}
)


#: Rows here are never published, and the writer emits today's statement for
#: them even when a collector is present.
INFRASTRUCTURE_TABLES: Final[frozenset[str]] = (
    frozenset(
        {
            # Audit and run bookkeeping.
            "sync_runs",
            "sync_run_items",
            # Operational state.
            "proxies",
            "settings",
            "bar_gaps",
            "bar_rescales",
            "intraday_scope",
            # The API's own tables.
            "api_clients",
            "api_client_scopes",
            "api_client_secrets",
            "api_plans",
            "api_usage_daily",
            # The live stream: its own outbox, its own relay, its own topics.
            "live_ticks",
            "live_quotes",
            "stream_scope",
            "stream_sessions",
            "stream_rejects",
            "stream_connection_health",
            "stream_outbox",
            "stream_relay_offset",
        }
    )
    | GATE_TABLES
)


__all__ = ["GATE_TABLES", "INFRASTRUCTURE_TABLES", "ROUTES", "Route"]
