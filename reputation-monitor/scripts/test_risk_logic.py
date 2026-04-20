"""Offline unit-style checks for the risk-verdict engine.

Run with:   docker compose exec api python scripts/test_risk_logic.py
Exit code 0 = all invariants hold.

We test the rule enforcer in isolation (no DB, no Claude) because the
invariants R1..R5 MUST hold regardless of what Claude returns.
"""

from __future__ import annotations

import sys

from app.analysis.risk_verdict import (
    Signals,
    enforce_rules,
    heuristic_score,
    recommendation_for,
)


failures: list[str] = []


def _check(name: str, ok: bool, details: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}" + (f"  — {details}" if details else "")
    print(line)
    if not ok:
        failures.append(name)


def t1_all_negative_cannot_be_proceed() -> None:
    """R2: 4 credibly-negative articles with avg severity 7 → score ≥ 65, rec ≠ Proceed."""
    s = Signals(
        total_articles=4,
        mentions_company_count=4,
        evidence_count=4,
        negative_count=4,
        positive_count=0,
        neutral_count=0,
        negative_ratio=1.0,
        positive_ratio=0.0,
        avg_severity=7.0,
        max_severity=8.5,
        avg_credibility=0.8,
        avg_sentiment=-0.7,
        red_flag_count=6,
        critical_signals=["legal", "corruption"],
    )
    # Pretend Claude returned a ridiculous "Proceed / 10"
    v = enforce_rules(
        s,
        draft_score=10.0,
        draft_rec="Proceed",
        draft_conf="medium",
        rationale=["Claude twierdzi że ok"],
        concerns=[],
        positives=[],
    )
    _check("T1 all-negative CANNOT be Proceed", v.recommendation != "Proceed", f"rec={v.recommendation}, score={v.risk_score}")
    _check("T1 all-negative score >= 65", v.risk_score >= 65.0, f"score={v.risk_score}")
    _check("T1 AI 'Proceed' got overridden", len(v.overrides) > 0, f"overrides={v.overrides}")


def t2_sanctions_forces_min_80() -> None:
    """R1: active sanctions → score ≥ 80, rec in {Avoid, Caution}."""
    s = Signals(
        total_articles=0,
        evidence_count=0,
        active_event_count=1,
        critical_active_events=["sanctions_match_company"],
        sanctions_active=True,
    )
    v = enforce_rules(
        s,
        draft_score=5.0,
        draft_rec="Proceed",
        draft_conf="low",
        rationale=[],
        concerns=[],
        positives=[],
    )
    _check("T2 sanctions → score ≥ 80", v.risk_score >= 80.0, f"score={v.risk_score}")
    _check("T2 sanctions → rec in {Avoid, Caution}", v.recommendation in ("Avoid", "Caution"), f"rec={v.recommendation}")
    _check("T2 override recorded", any("R1" in o for o in v.overrides), f"overrides={v.overrides}")


def t3_mixed_moderate_negative() -> None:
    """R3: 60% negative @ avg 5 → score ≥ 40, rec ≠ Proceed."""
    s = Signals(
        total_articles=5,
        evidence_count=5,
        negative_count=3,
        positive_count=1,
        neutral_count=1,
        negative_ratio=0.6,
        positive_ratio=0.2,
        avg_severity=5.0,
        max_severity=7.0,
        avg_credibility=0.7,
        avg_sentiment=-0.3,
    )
    v = enforce_rules(
        s,
        draft_score=12.0,
        draft_rec="Proceed",
        draft_conf="medium",
        rationale=[],
        concerns=[],
        positives=[],
    )
    _check("T3 60% negative @ sev 5 → score ≥ 40", v.risk_score >= 40.0, f"score={v.risk_score}")
    _check("T3 60% negative → rec ≠ Proceed", v.recommendation != "Proceed", f"rec={v.recommendation}")


def t4_truly_positive_stays_positive() -> None:
    """No negatives + only positives → score can stay low (Proceed/Monitor)."""
    s = Signals(
        total_articles=3,
        evidence_count=3,
        negative_count=0,
        positive_count=3,
        neutral_count=0,
        negative_ratio=0.0,
        positive_ratio=1.0,
        avg_severity=1.0,
        max_severity=2.0,
        avg_credibility=0.8,
        avg_sentiment=0.5,
    )
    v = enforce_rules(
        s,
        draft_score=heuristic_score(s),
        draft_rec=recommendation_for(heuristic_score(s))[0],
        draft_conf="medium",
        rationale=[],
        concerns=[],
        positives=["Wzrost przychodów"],
    )
    _check("T4 all-positive → rec in {Proceed, Monitor}", v.recommendation in ("Proceed", "Monitor"), f"rec={v.recommendation}, score={v.risk_score}")
    _check("T4 all-positive → score < 55", v.risk_score < 55.0, f"score={v.risk_score}")


def t5_low_evidence_forces_low_confidence() -> None:
    """R4: 1 credible evidence, no events → confidence=low."""
    s = Signals(
        total_articles=1,
        evidence_count=1,
        mentions_company_count=1,
        negative_count=1,
        negative_ratio=1.0,
        avg_severity=6.0,
        max_severity=6.0,
        avg_credibility=0.8,
    )
    v = enforce_rules(
        s,
        draft_score=50.0,
        draft_rec="Monitor",
        draft_conf="high",
        rationale=[],
        concerns=[],
        positives=[],
    )
    _check("T5 low evidence → confidence=low", v.confidence == "low", f"conf={v.confidence}")


def t6_recommendation_band_consistency() -> None:
    """R5: recommendation must match the enforced score's band."""
    s = Signals(
        total_articles=3,
        evidence_count=3,
        negative_count=3,
        negative_ratio=1.0,
        avg_severity=7.5,
        max_severity=8.0,
        avg_credibility=0.75,
    )
    v = enforce_rules(
        s,
        draft_score=50.0,
        draft_rec="Monitor",  # inconsistent: score will be promoted to ≥65 by R2
        draft_conf="medium",
        rationale=[],
        concerns=[],
        positives=[],
    )
    expected, _ = recommendation_for(v.risk_score)
    _check("T6 rec consistent with band", v.recommendation == expected, f"got={v.recommendation}, expected={expected}, score={v.risk_score}")


if __name__ == "__main__":
    t1_all_negative_cannot_be_proceed()
    t2_sanctions_forces_min_80()
    t3_mixed_moderate_negative()
    t4_truly_positive_stays_positive()
    t5_low_evidence_forces_low_confidence()
    t6_recommendation_band_consistency()

    print()
    if failures:
        print(f"❌ {len(failures)} failing checks: {failures}")
        sys.exit(1)
    print("✅ All invariants hold.")
