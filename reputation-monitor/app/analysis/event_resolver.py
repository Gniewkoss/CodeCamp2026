"""Future: resolve stale events using follow-up articles + Claude.

MVP: no-op hook so scheduled jobs can call into this later without breaking imports.
"""

from __future__ import annotations

import logging
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def resolve_stale_events_sync(db: Session, company_id: str, *, max_events: int = 20) -> int:
    """Placeholder — returns 0. Implement follow-up scanning when needed."""
    _ = (db, company_id, max_events)
    return 0
