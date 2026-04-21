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

from datetime import datetime, timezone

from app.config import get_settings
from app.models import CompanyPerson, PersonRiskFlag, RiskEvent

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
• SZCZEGÓLNIE SPRAWDŹ powiązania z Rosją / Białorusią / Iranem / Koreą Płn. / \
  Syrią: obywatelstwo, studia/praca w tych krajach, udziały w spółkach \
  z tych krajów, członkostwo w zarządach spółek kontrolowanych przez rosyjski \
  kapitał (Gazprom, Rosatom, Sbierbank itd.), kontakty z osobami na listach \
  sankcyjnych. Każde takie powiązanie oznacz kind="russia_link" \
  (lub "foreign_sanctions_link") z severity >= 0.9.
• Sprawdź też status PEP (polityk, urzędnik wysokiego szczebla) oraz \
  aktualne postępowania karne/CBA/CBŚP — kind="active_litigation" lub "pep".

SCHEMA:
{
  "flags": [
    {
      "kind": "past_bankruptcy" | "past_liquidation" | "active_liquidation" | \
              "disqualification" | "news_scandal" | "russia_link" | \
              "foreign_sanctions_link" | "active_litigation" | "pep" | "other",
      "other_company_name": "nazwa innej spółki której dotyczy zdarzenie lub null",
      "other_company_krs": "KRS innej spółki lub null",
      "severity": 0.0 .. 1.0,
      "notes": "krótki opis (1-2 zdania po polsku)",
      "evidence_url": "url źródła lub null"
    }
  ],
  "reputation": "positive" | "neutral" | "negative",
  "reputation_notes": "1-2 zdania o reputacji medialnej osoby",
  "confidence": "low" | "medium" | "high",
  "identity_notes": "krótka uwaga o pewności identyfikacji osoby"
}
"""


def _query_claude_for_person(name: str, role: Optional[str], company_name: str) -> Optional[dict[str, Any]]:
    from app.llm import llm_available, llm_complete

    if not llm_available():
        return None
    payload = {"person": name, "role": role, "current_company": company_name}
    raw = llm_complete(
        system=_SYSTEM,
        user=json.dumps(payload, ensure_ascii=False),
        max_tokens=800,
        purpose="governance_person",
    )
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
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
    "russia_link",
    "foreign_sanctions_link",
    "active_litigation",
    "pep",
    "other",
}

# Kinds treated as critical. They push score toward 100 and raise a RiskEvent.
CRITICAL_FLAG_KINDS = {"russia_link", "foreign_sanctions_link", "disqualification"}


_NAME_TOKEN_RE = re.compile(r"^[\w'\-]{2,}$", re.UNICODE)


def _person_name_looks_real(name: str) -> bool:
    """Reject masked KRS placeholders like 'K********' or '{... }'. We can't
    do anything useful with those — don't waste API tokens asking Claude."""
    if not name or name.startswith("{"):
        return False
    if "*" in name:
        return False
    tokens = [
        t for t in re.split(r"\s+", name.strip())
        if _NAME_TOKEN_RE.match(t) and any(ch.isalpha() for ch in t)
    ]
    return len(tokens) >= 2


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

    people_checked = 0
    for person in people:
        name = person.full_name or ""
        if not _person_name_looks_real(name):
            # Masked KRS placeholder (e.g. "K********") — no point querying
            # Claude, but count it so the UI can say "2 osób, imiona zamaskowane".
            continue
        resp = _query_claude_for_person(name, person.role, company_name)
        if not resp:
            continue
        people_checked += 1
        confidence = str(resp.get("confidence") or "low").lower()

        # Stash reputation summary on the person row so the UI can render it.
        reputation = str(resp.get("reputation") or "").lower()
        reputation_notes = str(resp.get("reputation_notes") or "")[:400]
        if reputation in {"positive", "neutral", "negative"} and reputation_notes:
            person.notes = f"[{reputation}] {reputation_notes}"

        if confidence == "low":
            # Low identification confidence — don't persist flags to avoid noise.
            continue
        flags = resp.get("flags") or []
        if not isinstance(flags, list):
            continue
        person_flag_bits: list[dict[str, Any]] = []
        for f in flags[:8]:
            if not isinstance(f, dict):
                continue
            kind = str(f.get("kind") or "other").lower()
            if kind not in VALID_FLAG_KINDS:
                kind = "other"
            severity = float(f.get("severity") or 0.5)
            severity = max(0.0, min(1.0, severity))
            if kind in CRITICAL_FLAG_KINDS:
                severity = max(severity, 0.9)

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
                # Critical person-level flags (Russia link etc.) get mirrored
                # as a RiskEvent on the company so the scoring pipeline and
                # ledger pick them up immediately — not only via the governance
                # pillar score.
                if kind in CRITICAL_FLAG_KINDS:
                    event_type = (
                        "sanctioned_jurisdiction_link"
                        if kind in {"russia_link", "foreign_sanctions_link"}
                        else "governance_redflag"
                    )
                    note = str(f.get("notes") or "").strip()
                    title_suffix = (
                        " — powiązanie z Rosją / sankcjami"
                        if kind == "russia_link"
                        else (
                            " — powiązanie z jurysdykcją sankcyjną"
                            if kind == "foreign_sanctions_link"
                            else ""
                        )
                    )
                    db.add(
                        RiskEvent(
                            company_id=company_id,
                            event_type=event_type,
                            title=f"Zarząd: {name}{title_suffix}",
                            description=(note or None),
                            severity=severity,
                            source_name="governance_risk / Claude",
                            source_url=str(f.get("evidence_url") or "") or None,
                            detected_at=datetime.now(timezone.utc),
                            related_person=name,
                        )
                    )
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
    # to ~25 points; cap at 100. Critical flags (russia_link etc.) floor at 90.
    score = 25.0 + min(75.0, total_severity * 30.0)
    has_critical = any(
        f.get("kind") in CRITICAL_FLAG_KINDS
        for p in flagged_persons
        for f in p["flags"]
    )
    if has_critical:
        score = max(score, 90.0)
    masked_count = sum(1 for p in people if not _person_name_looks_real(p.full_name or ""))
    notes_parts = [f"Sprawdzono {people_checked} z {len(people)} osób"]
    if masked_count:
        notes_parts.append(f"{masked_count} zamaskowanych przez KRS")
    notes_parts.append(f"znaleziono {len(flagged_persons)} z flagami")
    return GovernanceResult(
        score=round(score, 1),
        flags_count=len([f for p in flagged_persons for f in p["flags"]]),
        people_checked=people_checked,
        flagged_people=flagged_persons,
        notes=" · ".join(notes_parts) + ".",
    )
