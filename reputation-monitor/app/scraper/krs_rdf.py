"""KRS RDF (Repozytorium Dokumentów Finansowych) client.

Public portal https://ekrs.ms.gov.pl/rdf/rd/ lets anyone search and download
sprawozdania finansowe by KRS number. The portal is a JSF/PrimeFaces app, so
reverse-engineering the HTTP flow is fragile — session + ViewState tokens
rotate often, and the endpoint has aggressive anti-bot heuristics.

We therefore implement a BEST-EFFORT strategy:

1. Try the public HTML search (``search.xhtml``) and scrape any statement links
   visible in the response.
2. If the scrape fails (HTTP error / no matches), return an empty list. The
   higher-level extractor will then fall back to a Claude knowledge-based
   generator for well-known companies.

This module is intentionally lightweight — it provides a stable interface
(``list_statements`` / ``download_statement``) that the rest of the pipeline
can depend on even when the upstream portal changes.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

RDF_BASE = "https://ekrs.ms.gov.pl/rdf/rd"
SEARCH_URL = f"{RDF_BASE}/search.xhtml"
DOWNLOAD_URL = f"{RDF_BASE}/download.xhtml"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}


@dataclass
class StatementRef:
    """A single sprawozdanie entry listed in RDF for a company."""

    external_ref: str
    period_end: str  # ISO "YYYY-MM-DD" if parseable, else raw label
    period_type: str = "annual"
    document_type: Optional[str] = None  # e.g. "Sprawozdanie finansowe"
    source: str = "KRS_RDF"
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_ref": self.external_ref,
            "period_end": self.period_end,
            "period_type": self.period_type,
            "document_type": self.document_type,
            "source": self.source,
            "raw": self.raw,
        }


def _normalise_krs(krs: Optional[str]) -> Optional[str]:
    if not krs:
        return None
    digits = "".join(ch for ch in str(krs) if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(10)[-10:]


def list_statements(krs: str, *, timeout: float = 10.0, max_statements: int = 6) -> list[StatementRef]:
    """Return list of recent sprawozdania for a KRS number.

    Best-effort: returns an empty list on any error so the caller can fall back
    to alternative sources (Claude knowledge, manual upload).
    """
    normalised = _normalise_krs(krs)
    if not normalised:
        return []

    try:
        with httpx.Client(headers=_DEFAULT_HEADERS, timeout=timeout, follow_redirects=True) as client:
            # Warm up the session — ekrs issues JSESSIONID on first GET.
            resp = client.get(SEARCH_URL)
            resp.raise_for_status()
            # Submit the search form. Older layouts accept a plain GET with
            # ``unposted`` = "1" and KRS as query param; newer layouts need
            # full JSF postback. We try GET first (cheapest) and bail out if
            # the response doesn't look like results.
            params = {
                "search": "1",
                "unposted": "1",
                "nrKrs": normalised,
            }
            resp = client.get(SEARCH_URL, params=params)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.info("KRS RDF list failed for %s: %s", normalised, e)
        return []

    return _parse_statement_list(html, max_statements=max_statements)


def _parse_statement_list(html: str, *, max_statements: int) -> list[StatementRef]:
    """Extract visible statement entries from a search-results HTML page.

    The layout changes periodically — we look for common structural markers:
      * table rows mentioning "Sprawozdanie finansowe"
      * date spans in YYYY-MM-DD format
      * download links with a hash/id param
    and bail out as soon as we have enough entries.
    """
    if not html or "Sprawozdanie" not in html:
        return []

    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    ref_re = re.compile(r"fileId=([A-Za-z0-9\-]+)")

    found: list[StatementRef] = []
    seen: set[str] = set()

    # Naïve pass: look at every row containing a date and a fileId.
    for match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE):
        row = match.group(1)
        if "Sprawozdanie" not in row:
            continue
        date_m = date_re.search(row)
        ref_m = ref_re.search(row)
        if not (date_m and ref_m):
            continue
        ext = ref_m.group(1)
        if ext in seen:
            continue
        seen.add(ext)
        found.append(
            StatementRef(
                external_ref=ext,
                period_end=date_m.group(1),
                period_type="annual",
                document_type="Sprawozdanie finansowe",
                raw={"row_fragment": row[:400]},
            )
        )
        if len(found) >= max_statements:
            break

    return found


def download_statement(external_ref: str, *, timeout: float = 20.0) -> Optional[bytes]:
    """Download the raw document bytes for a statement. Returns None on error."""
    if not external_ref:
        return None
    try:
        with httpx.Client(headers=_DEFAULT_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(DOWNLOAD_URL, params={"fileId": external_ref})
            resp.raise_for_status()
            if not resp.content:
                return None
            return resp.content
    except Exception as e:
        logger.info("KRS RDF download failed for %s: %s", external_ref, e)
        return None


# ────────────────────────────────────────────────────────────────────────
# Rate-limited helper so a batched refresh doesn't hammer the portal.
# ────────────────────────────────────────────────────────────────────────


_LAST_CALL: dict[str, float] = {"t": 0.0}
_MIN_INTERVAL_S = 1.2


def polite_list_statements(krs: str, **kw: Any) -> list[StatementRef]:
    """Same as list_statements but respects a simple global rate limit."""
    now = time.monotonic()
    elapsed = now - _LAST_CALL["t"]
    if elapsed < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - elapsed)
    try:
        return list_statements(krs, **kw)
    finally:
        _LAST_CALL["t"] = time.monotonic()
