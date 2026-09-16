"""Data families: the closed set both the dataset side and the API share.

A family is the unit authorisation is expressed in; a readable dataset declares
its family in its `ApiExposure` and the scope follows. Adding a family needs a
migration for the enum type. Lives in `core` because datasets and api both import it."""

from __future__ import annotations

import enum


class DataFamily(enum.StrEnum):
    REFERENCE = "reference"  # symbols, ticker info
    BARS = "bars"  # price series and corporate actions
    FUNDAMENTALS = "fundamentals"  # statements, valuation, calendar
    HOLDERS = "holders"  # institutional and insider holdings
    NEWS = "news"
    DISCOVERY = "discovery"  # search, lookup, screener results
    DOMAINS = "domains"  # sector / industry
    # Not BARS: that family routes a table through range change events and
    # `_purge_bars`, which assume a series keyed by an instant, not a contract.
    DERIVATIVES = "derivatives"  # option expirations and chains


#: Every scope is read-only; the API writes nothing.
SCOPE_SUFFIX = ":read"


def scope_for(family: DataFamily) -> str:
    """The scope a family requires. Single source of the mapping."""
    return f"{family.value}{SCOPE_SUFFIX}"


#: The token endpoint's usage family.
OAUTH_FAMILY = "oauth"

#: The dataset catalogue's, and that of any request refused before the
#: dataset it named could be resolved. Named rather than spelled inline:
#: `UsageFamily` is built dynamically, so a literal here would not type
#: check at the call site and a typo would only surface as a counter
#: nobody ever reads.
META_FAMILY = "meta"

#: Usage is measured per family, plus the two surfaces above. Health is
#: not measured.
EXTRA_USAGE_FAMILIES = (OAUTH_FAMILY, META_FAMILY)
