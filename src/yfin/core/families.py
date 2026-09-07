"""Data families: the closed set both the dataset side and the API share.

A family is the unit authorisation is expressed in. The API keeps no
hand-maintained map of dataset -> scope: a dataset that is readable
declares its family in its `ApiExposure`, and the scope follows from that
one declaration. Exposure is opt-in and absence fails closed, so a newly
registered dataset is unreachable rather than reachable under the wrong
scope -- see `datasets/exposure.py` for why that beats a mandatory
family on every dataset.

The set is deliberately closed. Adding a family is not free -- it needs a
migration for the enum type and a decision about what it means
commercially -- and that friction is the point: it stops "one more
scope" from happening by accident.

This lives in `core` rather than in `api` because both `yfin.datasets`
and `yfin.api` import it, and neither may import the other.
"""

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


#: Every scope is read-only; the API writes nothing.
SCOPE_SUFFIX = ":read"


def scope_for(family: DataFamily) -> str:
    """The scope a family requires. Single source of the mapping."""
    return f"{family.value}{SCOPE_SUFFIX}"


#: Usage is measured per family, plus two surfaces that belong to no
#: family: the token endpoint and the catalogue. Health is not measured.
EXTRA_USAGE_FAMILIES = ("oauth", "meta")
