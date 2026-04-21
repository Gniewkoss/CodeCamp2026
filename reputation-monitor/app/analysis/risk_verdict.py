"""Risk Verdict engine — unified, transparent, AI-assisted but constrained.

Pipeline:
  S1 per-article Claude analysis (already persisted in ArticleAnalysis)
  S2 deterministic aggregation  -> Signals
  S3 AI judge (Claude)          -> draft {risk_score, recommendation, confidence, rationale, ...}
  S4 rule enforcer (pure code)  -> final FinalVerdict (hard invariants R1..R5)

Design goals:
  * Reproducible: same inputs + same as_of → same Signals (tested).
  * Consistent: strongly-negative data CAN'T yield a positive recommendation
    because the rule enforcer overrides the AI if needed and records the override.
  * Transparent: rationale + key_concerns + key_positives + overrides returned with every verdict.
  * Robust to small N: small-sample shrinkage + explicit "insufficient_evidence" status.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Article, ArticleAnalysis, Company, RiskEvent

logger = logging.getLogger(__name__)


# ── Critical markers ──────────────────────────────────────────────────

CRITICAL_CATEGORIES = {"money_laundering", "corruption", "sanctions", "legal"}
CRITICAL_EVENT_TYPES = {
    "criminal_charges",
    "conviction",
    "arrest",
    "sanctions_match_company",
    "sanctions_match_person",
    "sanctioned_jurisdiction_link",
    "bankruptcy_filed",
    "license_revoked",
    "board_member_arrested",
    "corruption_allegation",
}


# ── Data classes ──────────────────────────────────────────────────────


@dataclass
class Signals:
    total_articles: int = 0
    mentions_company_count: int = 0
    evidence_count: int = 0                 # mentions_company ∧ credible ∧ not fake
    negative_count: int = 0
    positive_count: int = 0
    neutral_count: int = 0
    negative_ratio: float = 0.0             # 0..1 within evidence
    positive_ratio: float = 0.0
    avg_severity: float = 0.0               # 0..10, weighted by credibility*recency
    max_severity: float = 0.0
    avg_credibility: float = 0.0            # 0..1 within evidence
    avg_sentiment: float = 0.0              # -1..1 within evidence
    red_flag_count: int = 0
    critical_signals: list[str] = field(default_factory=list)
    active_event_count: int = 0
    critical_active_events: list[str] = field(default_factory=list)
    sanctions_active: bool = False
    low_credibility_count: int = 0
    fake_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FinalVerdict:
    status: str = "scored"                  # scored | insufficient_evidence | offline_fallback
    risk_score: float = 0.0                 # 0..100 (higher = riskier)
    recommendation: str = "Unknown"         # Avoid | Caution | Monitor | Proceed | Unknown
    recommendation_description: str = ""
    confidence: str = "medium"              # low | medium | high
    rationale: list[str] = field(default_factory=list)
    key_concerns: list[str] = field(default_factory=list)
    key_positives: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    ai_score: Optional[float] = None
    ai_raw: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "risk_score": self.risk_score,
            "recommendation": self.recommendation,
            "recommendation_description": self.recommendation_description,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "key_concerns": self.key_concerns,
            "key_positives": self.key_positives,
            "overrides": self.overrides,
            "ai_score": self.ai_score,
        }


# New recommendation bands — tighter than the old ones (75/55/30 vs 85/65/35).
# The band is enforced in enforce_rules() (R5).
RECOMMENDATIONS = [
    (75.0, "Avoid", "Odradzamy współpracę bez bardzo szczegółowego due diligence."),
    (55.0, "Caution", "Dopuszczalne wyłącznie z dodatkowym DD i monitoringiem sygnałów."),
    (30.0, "Monitor", "Możliwa współpraca przy bieżącym monitoringu medialnym i regulacyjnym."),
    (0.0, "Proceed", "Brak istotnych czerwonych flag w dostępnych danych."),
]
DESC_UNKNOWN = "Za mało wiarygodnych danych — potrzebny ponowny skan lub szersze aliasy."


def recommendation_for(score: float) -> tuple[str, str]:
    for thr, label, desc in RECOMMENDATIONS:
        if score >= thr:
            return label, desc
    return "Proceed", RECOMMENDATIONS[-1][2]


# ── S2: Deterministic aggregation ─────────────────────────────────────


def compute_signals(
    db: Session,
    company_id: str,
    *,
    as_of: Optional[datetime] = None,
    lookback_days: int = 90,
) -> Signals:
    as_of = _as_utc(as_of) or datetime.now(timezone.utc)
    cutoff = as_of - timedelta(days=lookback_days)
    effective = func.coalesce(Article.published_at, Article.scraped_at)

    rows = list(
        db.execute(
            select(Article, ArticleAnalysis)
            .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
            .where(Article.company_id == company_id)
            .where(effective >= cutoff)
        ).all()
    )

    s = Signals(total_articles=len(rows))
    weighted_sev_num = 0.0
    weighted_sev_den = 0.0
    cred_sum = 0.0
    sent_sum = 0.0
    crit_set: set[str] = set()
    red_flag_set: set[str] = set()

    for art, an in rows:
        if not an.mentions_company:
            continue
        s.mentions_company_count += 1
        cred = float(an.credibility_score) if an.credibility_score is not None else 0.7
        is_fake = bool(an.is_likely_fake)
        if is_fake or cred < 0.35:
            s.low_credibility_count += 1
        if is_fake:
            s.fake_count += 1
        if is_fake or cred < 0.4:
            continue  # not evidence

        s.evidence_count += 1

        sev = float(an.severity or 0.0)
        sent = float(an.sentiment_score or 0.0)
        pub = art.published_at or art.scraped_at or as_of
        pub = _as_utc(pub) or as_of
        age_days = max(0, (as_of - pub).days)
        recency = math.exp(-0.02 * age_days)          # softer decay than old scoring
        weight = cred * max(0.25, recency)

        weighted_sev_num += sev * weight
        weighted_sev_den += weight
        cred_sum += cred
        sent_sum += sent
        if sev > s.max_severity:
            s.max_severity = sev

        impact = (an.investment_impact or "").lower()
        label = (an.sentiment_label or "").lower()
        if impact == "negative" or sev >= 5 or sent <= -0.3 or label in ("negative", "very_negative"):
            s.negative_count += 1
        elif impact == "positive" or sent >= 0.3 or label in ("positive", "very_positive"):
            s.positive_count += 1
        else:
            s.neutral_count += 1

        for c in an.risk_categories or []:
            if c in CRITICAL_CATEGORIES:
                crit_set.add(c)
        for rf in an.red_flags or []:
            if rf:
                red_flag_set.add(str(rf)[:160])

    if s.evidence_count > 0:
        s.avg_severity = weighted_sev_num / weighted_sev_den if weighted_sev_den else 0.0
        s.avg_credibility = cred_sum / s.evidence_count
        s.avg_sentiment = sent_sum / s.evidence_count
        s.negative_ratio = s.negative_count / s.evidence_count
        s.positive_ratio = s.positive_count / s.evidence_count

    s.red_flag_count = len(red_flag_set)
    s.critical_signals = sorted(crit_set)

    # Active risk events (ledger layer)
    events = list(
        db.scalars(
            select(RiskEvent).where(
                RiskEvent.company_id == company_id,
                RiskEvent.is_excluded.is_(False),
            )
        ).all()
    )
    active = [e for e in events if (e.status or "").lower() == "active"]
    s.active_event_count = len(active)
    crit_ev: set[str] = set()
    sanct = False
    for e in active:
        et = e.event_type or ""
        if et in CRITICAL_EVENT_TYPES:
            crit_ev.add(et)
        if "sanction" in et:  # matches ``sanctions_*`` and ``sanctioned_*``
            sanct = True
    s.critical_active_events = sorted(crit_ev)
    s.sanctions_active = sanct
    return s


def heuristic_score(s: Signals) -> float:
    """Transparent, deterministic baseline 0..100 — independent of AI.

    Used when Claude is unavailable AND as a lower-bound floor for AI answers
    (prevents the model from under-reporting obvious negatives).
    """
    if s.evidence_count == 0 and s.active_event_count == 0:
        return 0.0
    parts = 0.0
    parts += 40.0 * s.negative_ratio                     # signal dominance
    parts += min(35.0, s.avg_severity * 4.0)             # severity dose
    parts += 25.0 if s.sanctions_active else 0.0         # sanctions floor
    parts += min(20.0, s.active_event_count * 4.0)       # active events
    parts += 10.0 if s.critical_signals else 0.0         # thematic
    parts += 8.0 if s.max_severity >= 8 else 0.0         # one very severe article
    parts -= 15.0 * s.positive_ratio                     # good news softens
    parts = max(0.0, min(100.0, parts))
    # Small-sample shrinkage: when evidence is thin, pull toward a neutral prior
    # that still reflects direction of negative sentiment.
    if s.evidence_count < 3 and not s.sanctions_active and not s.critical_active_events:
        prior = 45.0 if s.negative_ratio > 0.5 else 20.0
        alpha = max(0.35, s.evidence_count / 3.0)
        parts = alpha * parts + (1 - alpha) * prior
    return round(parts, 1)


def _confidence_from_signals(s: Signals) -> str:
    if s.evidence_count >= 6 and s.avg_credibility >= 0.7:
        return "high"
    if s.evidence_count >= 3 or s.active_event_count >= 1:
        return "medium"
    return "low"


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── S3: AI judge ──────────────────────────────────────────────────────


_SYSTEM_VERDICT = """You are the FINAL risk arbiter for a due-diligence / investment-risk tool.
You receive:
  * company info
  * pre-computed signals (mathematical aggregates over article analyses)
  * per-article mini-analyses (already classified)
  * active risk events (ledger)

Return STRICT JSON (no prose, no markdown). Your output MUST satisfy:

R1. If sanctions_active=true OR any critical active event (criminal_charges, conviction, arrest,
    sanctions_match_company, sanctions_match_person, bankruptcy_filed, license_revoked,
    board_member_arrested, corruption_allegation) → risk_score ≥ 80 AND recommendation in {Avoid, Caution}.
R2. If negative_ratio ≥ 0.70 AND avg_severity ≥ 6 → risk_score ≥ 65 AND recommendation ≠ Proceed.
R3. If negative_ratio ≥ 0.50 AND avg_severity ≥ 4 → risk_score ≥ 40 AND recommendation ≠ Proceed.
R4. If evidence_count ≤ 1 AND active_event_count == 0 → confidence="low".
R5. recommendation must match score bands:
    - Avoid:   score ≥ 75
    - Caution: 55 ≤ score < 75
    - Monitor: 30 ≤ score < 55
    - Proceed: score < 30

Grounded output rules:
* Write rationale, key_concerns, key_positives IN POLISH, short bullets.
* rationale must reference real numbers or events (no generic platitudes).
* key_concerns come ONLY from credible evidence (credibility ≥ 0.4).
* Never invent events that are not in the input.
* Fake/low-credibility articles must not dominate concerns.

JSON shape:
{
  "risk_score": integer 0..100,
  "recommendation": "Avoid" | "Caution" | "Monitor" | "Proceed",
  "confidence": "low" | "medium" | "high",
  "rationale": [string, ...],     // 3..5 items
  "key_concerns": [string, ...],  // 0..5 items
  "key_positives": [string, ...]  // 0..5 items
}"""


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _clip(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def _articles_slim(db: Session, company_id: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(Article, ArticleAnalysis)
            .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
            .where(Article.company_id == company_id)
            .order_by(Article.scraped_at.desc())
            .limit(limit)
        ).all()
    )
    out: list[dict[str, Any]] = []
    for a, an in rows:
        out.append(
            {
                "title": (a.title or "")[:180],
                "source": a.source,
                "published_at": a.published_at.isoformat() if a.published_at else None,
                "mentions_company": bool(an.mentions_company),
                "sentiment_score": float(an.sentiment_score) if an.sentiment_score is not None else None,
                "sentiment_label": an.sentiment_label,
                "severity": float(an.severity) if an.severity is not None else None,
                "risk_level": an.risk_level,
                "risk_categories": an.risk_categories,
                "investment_impact": an.investment_impact,
                "credibility_score": float(an.credibility_score) if an.credibility_score is not None else None,
                "is_likely_fake": bool(an.is_likely_fake) if an.is_likely_fake is not None else None,
                "summary": (an.summary or "")[:400],
                "red_flags": an.red_flags or [],
                "positive_points": an.positive_points or [],
            }
        )
    return out


def _events_slim(db: Session, company_id: str, limit: int = 15) -> list[dict[str, Any]]:
    events = list(
        db.scalars(
            select(RiskEvent)
            .where(RiskEvent.company_id == company_id, RiskEvent.is_excluded.is_(False))
            .order_by(RiskEvent.detected_at.desc())
            .limit(limit * 3)  # over-fetch, we'll filter orphaned ones below
        ).all()
    )
    # Drop events whose source article has been re-analysed as NOT about this
    # company — those are legacy cross-contamination from the old pipeline.
    clean: list[RiskEvent] = []
    for e in events:
        if e.article_id:
            an = db.scalar(
                select(ArticleAnalysis).where(ArticleAnalysis.article_id == e.article_id)
            )
            if an is not None and an.mentions_company is False:
                continue
        clean.append(e)
        if len(clean) >= limit:
            break
    events = clean
    return [
        {
            "event_type": e.event_type,
            "title": e.title,
            "description": (e.description or "")[:400],
            "status": e.status,
            "severity": float(e.severity),
            "detected_at": e.detected_at.isoformat() if e.detected_at else None,
        }
        for e in events
    ]


def ai_verdict_claude(
    company: Company,
    signals: Signals,
    articles: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> Optional[dict]:
    from app.llm import llm_available, llm_complete

    settings = get_settings()
    if not llm_available():
        return None
    payload = {
        "company": {"name": company.name, "nip": company.nip, "sector": company.sector},
        "signals": signals.as_dict(),
        "articles": articles,
        "events": events,
    }
    raw = llm_complete(
        system=_SYSTEM_VERDICT,
        user=json.dumps(payload, ensure_ascii=False)[:18000],
        max_tokens=settings.llm_max_tokens,
        purpose="ai_verdict",
    )
    if not raw:
        return None
    parsed = _extract_json(raw)
    if parsed is not None:
        parsed["_raw"] = raw[:4000]
    return parsed


# ── S4: Rule enforcer ────────────────────────────────────────────────


def enforce_rules(
    signals: Signals,
    *,
    draft_score: float,
    draft_rec: str,
    draft_conf: str,
    rationale: list[str],
    concerns: list[str],
    positives: list[str],
) -> FinalVerdict:
    overrides: list[str] = []
    score = _clip(draft_score, 0.0, 100.0, heuristic_score(signals))

    # Heuristic lower-bound — prevent AI from under-reporting obvious negatives.
    heur = heuristic_score(signals)
    if heur - score > 10.0:
        overrides.append(f"Podniesiono AI score {score:.0f}→{heur:.0f} (heurystyka wyższa o >10)")
        score = heur

    # R1 — critical events / sanctions
    if signals.sanctions_active or signals.critical_active_events:
        if score < 80.0:
            trigger = "sankcje" if signals.sanctions_active else ",".join(signals.critical_active_events[:3])
            overrides.append(f"R1: aktywne zdarzenie krytyczne ({trigger}) → min 80")
            score = max(score, 80.0)

    # R2 — strong negative pattern
    if signals.negative_ratio >= 0.70 and signals.avg_severity >= 6.0:
        if score < 65.0:
            overrides.append(
                f"R2: {signals.negative_ratio:.0%} negatywnych przy avg_sev={signals.avg_severity:.1f} → min 65"
            )
            score = max(score, 65.0)

    # R3 — moderate negative pattern
    elif signals.negative_ratio >= 0.50 and signals.avg_severity >= 4.0:
        if score < 40.0:
            overrides.append(
                f"R3: {signals.negative_ratio:.0%} negatywnych przy avg_sev={signals.avg_severity:.1f} → min 40"
            )
            score = max(score, 40.0)

    # R5 — recommendation must match score band
    rec_label, rec_desc = recommendation_for(score)
    if draft_rec != rec_label:
        overrides.append(f"R5: rekomendacja dostosowana do score: {draft_rec}→{rec_label}")

    # R4 — confidence floor
    confidence = draft_conf if draft_conf in ("low", "medium", "high") else _confidence_from_signals(signals)
    if signals.evidence_count <= 1 and signals.active_event_count == 0 and confidence != "low":
        confidence = "low"
        overrides.append("R4: confidence=low (≤1 wiarygodny dowód, brak eventów)")

    return FinalVerdict(
        status="scored",
        risk_score=round(score, 1),
        recommendation=rec_label,
        recommendation_description=rec_desc,
        confidence=confidence,
        rationale=rationale[:5],
        key_concerns=concerns[:5],
        key_positives=positives[:5],
        overrides=overrides,
    )


# ── Top-level: build_verdict ─────────────────────────────────────────


def _insufficient_rationale(signals: Signals) -> list[str]:
    msgs: list[str] = []
    if signals.total_articles == 0:
        msgs.append("Skan nie znalazł artykułów dla tej spółki ani jej aliasów.")
    elif signals.mentions_company_count == 0:
        msgs.append(
            f"Znaleziono {signals.total_articles} artykułów, ale żaden nie został zakwalifikowany "
            "jako dotyczący spółki. Rozszerz listę aliasów i ponów skan."
        )
    elif signals.evidence_count == 0:
        msgs.append(
            f"Wszystkie {signals.mentions_company_count} dopasowanych artykułów miały niską "
            "wiarygodność lub oznaczone zostały jako potencjalny fake-news."
        )
    msgs.append('Rekomendacja: dodaj 2-3 aliasy lub pelna nazwa prawna i kliknij "Uruchom skan AI".')
    return msgs


def build_verdict(db: Session, company: Company, *, as_of: Optional[datetime] = None) -> FinalVerdict:
    as_of = _as_utc(as_of) or datetime.now(timezone.utc)
    signals = compute_signals(db, company.id, as_of=as_of)

    # Insufficient-evidence branch (status ≠ scored)
    if signals.evidence_count == 0 and signals.active_event_count == 0:
        return FinalVerdict(
            status="insufficient_evidence",
            risk_score=0.0,
            recommendation="Unknown",
            recommendation_description=DESC_UNKNOWN,
            confidence="low",
            rationale=_insufficient_rationale(signals),
            key_concerns=[],
            key_positives=[],
            overrides=["status=insufficient_evidence"],
        )

    articles = _articles_slim(db, company.id)
    events = _events_slim(db, company.id)
    ai = ai_verdict_claude(company, signals, articles, events)

    if ai is None:
        # Offline fallback — pure heuristic + rules.
        heur = heuristic_score(signals)
        rec_label, _ = recommendation_for(heur)
        rationale: list[str] = [
            "Werdykt heurystyczny — brak odpowiedzi modelu AI.",
            f"Ewidencja: {signals.evidence_count} artykułów (w tym {signals.negative_count} negatywnych, "
            f"{signals.positive_count} pozytywnych).",
            f"Średnia severity: {signals.avg_severity:.1f}/10, max {signals.max_severity:.1f}/10.",
        ]
        if signals.active_event_count:
            rationale.append(f"Aktywne zdarzenia ryzyka: {signals.active_event_count}.")
        concerns = (signals.critical_signals or []) + signals.critical_active_events
        verdict = enforce_rules(
            signals,
            draft_score=heur,
            draft_rec=rec_label,
            draft_conf=_confidence_from_signals(signals),
            rationale=rationale,
            concerns=concerns,
            positives=[],
        )
        verdict.status = "offline_fallback"
        return verdict

    # AI result — coerce + enforce
    ai_score = _clip(ai.get("risk_score"), 0.0, 100.0, heuristic_score(signals))
    rec = str(ai.get("recommendation") or "").strip().capitalize()
    if rec not in ("Avoid", "Caution", "Monitor", "Proceed"):
        rec = recommendation_for(ai_score)[0]
    conf = str(ai.get("confidence") or "").strip().lower()
    rationale = [str(x)[:400] for x in (ai.get("rationale") or []) if x]
    concerns = [str(x)[:220] for x in (ai.get("key_concerns") or []) if x]
    positives = [str(x)[:220] for x in (ai.get("key_positives") or []) if x]

    verdict = enforce_rules(
        signals,
        draft_score=ai_score,
        draft_rec=rec,
        draft_conf=conf,
        rationale=rationale,
        concerns=concerns,
        positives=positives,
    )
    verdict.ai_score = ai_score
    verdict.ai_raw = ai.get("_raw")
    return verdict
