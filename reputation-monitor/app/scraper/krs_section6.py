"""KRS Dział 6 parser — upadłość, restrukturyzacja, likwidacja, zawieszenie.

Dział 6 of the KRS odpis contains every bankruptcy / restructuring / liquidation
event that has ever been entered against the company. We extract them into
``RegulatoryEvent`` rows so they can drive the Legal / regulatory pillar of the
composite score.

This is the single most impactful non-financial signal for "should I trust this
counterparty" — an active postępowanie upadłościowe alone should push the
composite score past 80.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Section6Event:
    kind: str                                  # bankruptcy | restructuring | liquidation | suspension | disqualification | other
    title: str
    body: Optional[str] = None
    event_date: Optional[datetime] = None
    severity: float = 0.7
    status: str = "active"
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["event_date"] = self.event_date.isoformat() if self.event_date else None
        return d


# ────────────────────────────────────────────────────────────────────────
# Classification keywords
# ────────────────────────────────────────────────────────────────────────


_KIND_PATTERNS: list[tuple[str, list[str], float]] = [
    ("bankruptcy", ["upadłość", "upadlosc", "ogłoszenie upadłości", "postępowanie upadłościowe"], 0.95),
    ("restructuring", ["restrukturyzacj", "układowe", "sanacja", "przyspieszone postępowanie"], 0.75),
    ("liquidation", ["likwidacj", "likwidator"], 0.80),
    ("suspension", ["zawieszenie działalności", "zawieszenie postępowania"], 0.50),
    ("disqualification", ["zakaz prowadzenia działalności", "pozbawienie prawa"], 0.85),
]


def _classify(text: str) -> tuple[str, float]:
    t = text.lower()
    for kind, kws, sev in _KIND_PATTERNS:
        for kw in kws:
            if kw in t:
                return kind, sev
    return "other", 0.4


# ────────────────────────────────────────────────────────────────────────
# Walker
# ────────────────────────────────────────────────────────────────────────


def _walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            new = f"{path}.{k}" if path else str(k)
            out.extend(_walk(v, new))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_walk(v, f"{path}[{i}]"))
    else:
        out.append((path, node))
    return out


def _parse_date(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    return None


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────


def parse_section6(krs_blob: Any) -> list[Section6Event]:
    """Extract all dzial-6 events from a KRS odpis JSON blob.

    The public REST API uses Polish field names. We search the full tree for any
    key path containing "dzial6" and look for sub-dicts with ``rodzaj`` /
    ``tresc`` / ``opis`` / ``dataWpisu`` / ``data``.
    """
    if not krs_blob:
        return []

    events: list[Section6Event] = []
    section6_chunks: list[Any] = []

    # Find any node rooted under a dzial6 path (case-insensitive).
    stack: list[tuple[str, Any]] = [("", krs_blob)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                new_path = f"{path}.{k}".lower()
                if "dzial6" in new_path or "dział6" in new_path:
                    section6_chunks.append(v)
                else:
                    stack.append((f"{path}.{k}", v))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                stack.append((f"{path}[{i}]", v))

    if not section6_chunks:
        return []

    for chunk in section6_chunks:
        pairs = _walk(chunk)
        # Group by containing sub-dict — heuristically by matching indices in path.
        # Simpler approach: sweep for text-like values and classify each.
        rendered_title_bits: list[str] = []
        date_found: Optional[datetime] = None
        for path, val in pairs:
            if isinstance(val, str) and len(val) >= 6:
                # Skip obviously structural strings.
                if val.strip().lower() in ("prp", "true", "false"):
                    continue
                if "data" in path.lower():
                    d = _parse_date(val)
                    if d:
                        date_found = d
                        continue
                rendered_title_bits.append(val.strip())

        combined = " | ".join(rendered_title_bits).strip()
        if not combined:
            continue
        kind, severity = _classify(combined)
        events.append(
            Section6Event(
                kind=kind,
                title=(combined[:200] + "…") if len(combined) > 200 else combined,
                body=combined[:2000],
                event_date=date_found,
                severity=severity,
                status="active",
                raw={"source": "krs_section_6"},
            )
        )

    return events
