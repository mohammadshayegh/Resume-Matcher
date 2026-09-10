"""Strict option values for JobSpy-backed search.

A leaf module on purpose: ``app/schemas/job_search.py`` validates against these
constants, and importing them from ``app.services.job_search`` would drag in
``app/services/__init__.py`` — which imports the parser, which imports
``app.schemas`` — a circular import at startup.

The values mirror the enums in ``jobspy.model``. They are deliberately *copies*
rather than derived at import time, so nothing on the request path pays for
importing pandas; ``tests/unit/test_job_search_options.py`` fails if the
upstream enums ever drift from these.
"""

from __future__ import annotations

import re
from typing import Final

# --- Strict option values (mirrors jobspy.model enums) -----------------------

# jobspy.model.Site
SITES: Final[tuple[str, ...]] = (
    "linkedin",
    "indeed",
    "zip_recruiter",
    "glassdoor",
    "google",
    "bayt",
    "naukri",
    "bdjobs",
)

# jobspy.model.JobType — the *first* alias of each member is what the scrapers
# match on, so it is the only value safe to send.
JOB_TYPES: Final[tuple[str, ...]] = (
    "fulltime",
    "parttime",
    "contract",
    "temporary",
    "internship",
    "perdiem",
    "nights",
    "other",
    "summer",
    "volunteer",
)

# jobspy.model.DescriptionFormat
DESCRIPTION_FORMATS: Final[tuple[str, ...]] = ("markdown", "html")

# jobspy.model.Country — (value accepted by ``country_indeed``, display label,
# whether Glassdoor also covers it). US_CANADA/WORLDWIDE are internal markers
# upstream and are intentionally absent.
COUNTRIES: Final[tuple[tuple[str, str, bool], ...]] = (
    ("argentina", "Argentina", True),
    ("australia", "Australia", True),
    ("austria", "Austria", True),
    ("bahrain", "Bahrain", False),
    ("bangladesh", "Bangladesh", False),
    ("belgium", "Belgium", True),
    ("bulgaria", "Bulgaria", False),
    ("brazil", "Brazil", True),
    ("canada", "Canada", True),
    ("chile", "Chile", False),
    ("china", "China", False),
    ("colombia", "Colombia", False),
    ("costa rica", "Costa Rica", False),
    ("croatia", "Croatia", False),
    ("cyprus", "Cyprus", False),
    ("czech republic", "Czech Republic", False),
    ("denmark", "Denmark", False),
    ("ecuador", "Ecuador", False),
    ("egypt", "Egypt", False),
    ("estonia", "Estonia", False),
    ("finland", "Finland", False),
    ("france", "France", True),
    ("germany", "Germany", True),
    ("greece", "Greece", False),
    ("hong kong", "Hong Kong", True),
    ("hungary", "Hungary", False),
    ("india", "India", True),
    ("indonesia", "Indonesia", False),
    ("ireland", "Ireland", True),
    ("israel", "Israel", False),
    ("italy", "Italy", True),
    ("japan", "Japan", False),
    ("kuwait", "Kuwait", False),
    ("latvia", "Latvia", False),
    ("lithuania", "Lithuania", False),
    ("luxembourg", "Luxembourg", False),
    ("malaysia", "Malaysia", True),
    ("malta", "Malta", True),
    ("mexico", "Mexico", True),
    ("morocco", "Morocco", False),
    ("netherlands", "Netherlands", True),
    ("new zealand", "New Zealand", True),
    ("nigeria", "Nigeria", False),
    ("norway", "Norway", False),
    ("oman", "Oman", False),
    ("pakistan", "Pakistan", False),
    ("panama", "Panama", False),
    ("peru", "Peru", False),
    ("philippines", "Philippines", False),
    ("poland", "Poland", False),
    ("portugal", "Portugal", False),
    ("qatar", "Qatar", False),
    ("romania", "Romania", False),
    ("saudi arabia", "Saudi Arabia", False),
    ("singapore", "Singapore", True),
    ("slovakia", "Slovakia", False),
    ("slovenia", "Slovenia", False),
    ("south africa", "South Africa", False),
    ("south korea", "South Korea", False),
    ("spain", "Spain", True),
    ("sweden", "Sweden", False),
    ("switzerland", "Switzerland", True),
    ("taiwan", "Taiwan", False),
    ("thailand", "Thailand", False),
    ("türkiye", "Türkiye", False),
    ("ukraine", "Ukraine", False),
    ("united arab emirates", "United Arab Emirates", False),
    ("uk", "United Kingdom", True),
    ("usa", "United States", True),
    ("uruguay", "Uruguay", False),
    ("venezuela", "Venezuela", False),
    ("vietnam", "Vietnam", True),
)

COUNTRY_VALUES: Final[frozenset[str]] = frozenset(value for value, _, _ in COUNTRIES)

# Upper bound on ``results_wanted``. Each result is a request to the board, so
# this is the difference between "a search" and "an obvious scraping run".
MAX_RESULTS_WANTED: Final[int] = 100

# ``proxies`` entries are 'user:pass@host:port' or 'host:port'; anything with
# whitespace or a scheme would be silently mishandled by jobspy.
_PROXY_RE: Final[re.Pattern[str]] = re.compile(r"^[^\s/@]+(?::[^\s/@]*)?(?:@[^\s/@]+)?$")


def validate_proxy(proxy: str) -> bool:
    """Return whether ``proxy`` looks like a 'user:pass@host:port' entry."""
    return bool(_PROXY_RE.match(proxy))
