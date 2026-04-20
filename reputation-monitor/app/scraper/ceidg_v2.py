"""CEIDG API v2 — single company `firma` by NIP."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def fetch_ceidg_firma_v2(*, nip: str) -> Optional[dict[str, Any]]:
    settings = get_settings()
    if not settings.ceidg_api_token:
        return None
    base = settings.ceidg_v2_api_url.rstrip("/")
    url = f"{base}/firma"
    headers = {"Authorization": f"Bearer {settings.ceidg_api_token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, params={"nip": nip}, headers=headers)
            if r.status_code in (204, 404):
                return None
            r.raise_for_status()
            if not (r.content or b"").strip():
                return None
            return r.json()
    except Exception as e:
        logger.info("CEIDG v2 firma failed: %s", e)
        return None
