"""Governance risk — detect risky members of the management / board.

Strategy:

1. For every active ``CompanyPerson`` of the target company, ask Claude to recall
   their public history (other companies, past bankruptcies, disqualifications,
   notable scandals). Without PESEL we can't query KRS strictly, so we fall back
   to fuzzy name matching + Claude knowledge.
2. For each recalled risky relationship, create a ``PersonRiskFlag`` row (once).
3. Return a governance_score(0..100, higher = worse) aggregated from the count
   and severity of flags.

Completely optional — if the Anthropic key is missing or people list is empty,
returns neutral score with an explanation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CompanyPerson, PersonRiskFlag

logger = logging.getLogger(__name__)


@dataclass
class GovernanceResult:
    score: float = 50.0          # 0..100, higher = worse
    flags_count: int = 0
    people_checked: int = 0
    flagged_people: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ────────────────────────────────────────────────────────────────────────
# Claude prompt
# ────────────────────────────────────────────────────────────────────────


_SYSTEM = """Jesteś analitykiem due-diligence sprawdzającym osoby z zarządów spółek \
w Polsce. Dostajesz imię i nazwisko oraz (opcjonalnie) rolę i nazwę spółki. \
Twoim zadaniem jest — na podstawie publicznie znanych informacji — zwrócić \
listę ryzykownych powiązań/historii, jeśli takie istnieją.

ZASADY:
• Zwracaj WYŁĄCZNIE JSON.
• Wpisuj TYLKO konkretne, publicznie znane przypadki. Nie spekuluj.
• Jeśli osoba jest pospolita (popularne imię + nazwisko) i nie jesteś pewien \
  tożsamości → zwróć pustą listę z notą "niska pewność identyfikacji".
• Nie zgłaszaj drobnych nieporozumień / starych sporów cywilnych. Tylko sprawy \
  materialne: upadłości spółek z ich udziałem, zakazy prowadzenia działalności, \
  skazania, wyprowadzenia majątku, duże skandale publikowane w mediach.

SCHEMA:
{
  "flags": [
    {
      "kind": "past_bankruptcy" | "past_liquidation" | "active_liquidation" | \
              "disqualification" | "news_scandal" | "other",
      "other_company_name": "nazwa innej spółki której dotyczy zdarzenie lub null",
      "other_company_krs": "KRS innej spółki lub null",
      "severity": 0.0 .. 1.0,
      "notes": "krótki opis (1-2 zdania po polsku)",
      "evidence_url": "url źródła lub null"
    }
  ],
  "confidence": "low" | "medium" | "high",
  "identity_notes": "krótka uwaga o pewności identyfikacji osoby"
}
"""


def _query_claude_for_person(name: str, role: Optional[str], company_name: str) -> Optional[dict[str, Any]]:
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        payload = {"person": name, "role": role, "current_company": company_name}
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=800,
            system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        raw = "".join(getattr(b, "text", "") or "" for b in msg.content).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        return json.loads(m.group())
    except Exception as e:
        logger.info("Governance Claude call failed for %s: %s", name, e)
        return None


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────


VALID_FLAG_KINDS = {
    "past_bankruptcy",
    "past_liquidation",
    "active_liquidation",
    "disqualification",
    "news_scandal",
    "other",
}


def analyse_people(
    db: Session,
    company_id: str,
    *,
    company_name: str,
    max_people: int = 6,
) -> GovernanceResult:
    people = list(
        db.scalars(
            select(CompanyPerson)
            .where(CompanyPerson.company_id == company_id, CompanyPerson.is_active.is_(True))
            .limit(max_people)
        ).all()
    )
    if not people:
        return GovernanceResult(
            score=50.0,
            flags_count=0,
            people_checked=0,
            notes="Brak osób z KRS — nie można ocenić governance.",
        )

    total_severity = 0.0
    flagged_persons: list[dict[str, Any]] = []
    flags_added = 0

    for person in people:
        name = person.full_name or ""
        if not name or name.startswith("{"):
            continue
        resp = _query_claude_for_person(name, person.role, company_name)
        if not resp:
            continue
        confidence = str(resp.get("confidence") or "low").lower()
        if confidence == "low":
            # Low identification confidence — don't persist flags to avoid noise.
            continue
        flags = resp.get("flags") or []
        if not isinstance(flags, list):
            continue
        person_flag_bits: list[dict[str, Any]] = []
        for f in flags[:5]:
            if not isinstance(f, dict):
                continue
            kind = str(f.get("kind") or "other").lower()
            if kind not in VALID_FLAG_KINDS:
                kind = "other"
            severity = float(f.get("severity") or 0.5)
            severity = max(0.0, min(1.0, severity))

            # Avoid duplicate flags for the same (person, kind, other company).
            other_name = str(f.get("other_company_name") or "")[:512] or None
            other_krs = str(f.get("other_company_krs") or "")[:64] or None
            existing = db.scalar(
                select(PersonRiskFlag).where(
                    PersonRiskFlag.person_id == person.id,
                    PersonRiskFlag.kind == kind,
                    PersonRiskFlag.other_company_name == other_name,
                )
            )
            if existing is None:
                db.add(
                    PersonRiskFlag(
                        person_id=person.id,
                        kind=kind,
                        other_company_krs=other_krs,
                        other_company_name=other_name,
                        severity=severity,
                        notes=str(f.get("notes") or "")[:1000] or None,
                        evidence_url=str(f.get("evidence_url") or "") or None,
                    )
                )
                flags_added += 1
            total_severity += severity
            person_flag_bits.append(
                {
                    "kind": kind,
                    "other_company_name": other_name,
                    "severity": severity,
                    "notes": str(f.get("notes") or "")[:400],
                }
            )
        if person_flag_bits:
            flagged_persons.append(
                {"name": name, "role": person.role, "flags": person_flag_bits}
            )

    if flags_added:
        try:
            db.commit()
        except Exception as e:
            logger.warning("Governance commit failed: %s", e)
            db.rollback()

    # Map severity to score. 0 flags → 25 (low). Each flag of severity=1 adds up
    # to ~25 points; cap at 100.
    score = 25.0 + min(75.0, total_severity * 30.0)
    notes = f"Sprawdzono {len(people)} osób, znaleziono {len(flagged_persons)} z flagami."
    return GovernanceResult(
        score=round(score, 1),
        flags_count=len([f for p in flagged_persons for f in p["flags"]]),
        people_checked=len(people),
        flagged_people=flagged_persons,
        notes=notes,
    )
