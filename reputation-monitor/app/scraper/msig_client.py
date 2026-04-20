"""Optional iMSiG / KRZ client (feature-flag ``enable_msig``).

The free iMSiG API requires an API key (small monthly fee). When the key is
configured, this module returns actual MSiG announcements for a given NIP/KRS.
When disabled, ``fetch_msig_events`` returns an empty list so the rest of the
pipeline just skips over this source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class MsigEvent:
    kind: str                                  # bankruptcy | restructuring | liquidation | other
    title: str
    body: Optional[str] = None
    event_date: Optional[datetime] = None
    severity: float = 0.8
    external_ref: Optional[str] = None
    url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["event_date"] = self.event_date.isoformat() if self.event_date else None
        return d


IMSIG_URL = "https://api.imsig.pl/v2/announcements"


def fetch_msig_events(
    *,
    nip: Optional[str] = None,
    krs: Optional[str] = None,
    limit: int = 20,
) -> list[MsigEvent]:
    settings = get_settings()
    if not getattr(settings, "enable_msig", False):
        return []
    api_key = getattr(settings, "imsig_api_key", None) or getattr(settings, "big_infomonitor_api_key", None)
    if not api_key:
        return []
    if not (nip or krs):
        return []
    params: dict[str, Any] = {"limit": limit}
    if nip:
        params["nip"] = nip
    if krs:
        params["krs"] = krs

    try:
        with httpx.Client(
            timeout=15.0,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        ) as client:
            resp = client.get(IMSIG_URL, params=params)
            if resp.status_code >= 400:
                logger.info("iMSiG %s: %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()
    except Exception as e:
        logger.info("iMSiG call failed: %s", e)
        return []

    out: list[MsigEvent] = []
    for item in (data.get("announcements") or data.get("items") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        out.append(
            MsigEvent(
                kind=str(item.get("kind") or "other"),
                title=str(item.get("title") or item.get("subject") or "")[:300] or "Ogłoszenie MSiG",
                body=str(item.get("body") or item.get("text") or "")[:2000] or None,
                event_date=_parse_iso(item.get("date") or item.get("publishedAt")),
                severity=float(item.get("severity") or 0.8),
                external_ref=str(item.get("id") or "")[:128] or None,
                url=item.get("url") or None,
                raw=item,
            )
        )
    return out


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")[:25])
    except Exception:
        return None
