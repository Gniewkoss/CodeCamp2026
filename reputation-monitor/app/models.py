from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    nip: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    krs: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    regon: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ticker: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, default="PL")

    # Registry metadata (from MF white-list / GUS BIR / CEIDG)
    legal_form: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status_vat: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    registration_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Primary PKD code (e.g. "62.01.Z") + human label
    pkd_primary: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pkd_primary_label: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Full PKD list — list of {"code": "...", "label": "..."} dicts
    pkd_all: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    # Which registries confirmed the entity: e.g. ["MF_WHITE_LIST", "GUS_BIR", "CEIDG"]
    registry_sources: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    registry_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    # AI-synthesised insights (SWOT + summary) — cached, regenerated after each scan
    ai_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strengths: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    weaknesses: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    opportunities: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    threats: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    investment_thesis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    insights_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    is_temporary: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    articles: Mapped[List["Article"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    score_snapshots: Mapped[List["ScoreHistory"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    registry_rows: Mapped[List["CompanyRegistryData"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    persons: Mapped[List["CompanyPerson"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    risk_events: Mapped[List["RiskEvent"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("company_id", "url", name="uq_articles_company_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="articles")
    analysis: Mapped[Optional["ArticleAnalysis"]] = relationship(
        back_populates="article", uselist=False, cascade="all, delete-orphan"
    )


class ArticleAnalysis(Base):
    """AI-produced analysis for a single article."""

    __tablename__ = "article_analysis"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    article_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    mentions_company: Mapped[bool] = mapped_column(default=True)

    # -1.0 (very negative) .. +1.0 (very positive)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sentiment_label: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    # Reputational
    risk_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # none|low|medium|high|critical
    risk_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # dominant category
    risk_categories: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    risk_keywords: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    severity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0..10

    # Investment perspective
    investment_impact: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # positive|neutral|negative
    investment_risk: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0..10

    # Fake-news / quality assessment
    credibility_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0..1 (1 = wiarygodne)
    is_likely_fake: Mapped[Optional[bool]] = mapped_column(nullable=True)
    credibility_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Narrative for the UI
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    key_facts: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    red_flags: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    positive_points: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)

    raw_llm_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped["Article"] = relationship(back_populates="analysis")


class ScoreHistory(Base):
    __tablename__ = "score_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Reputational risk score 0..100 (higher = worse reputation)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    # Investment risk 0..100 (higher = riskier to invest)
    investment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recommendation: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    score_components: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Ledger / event layer (optional; populated by unified scoring)
    ledger_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    active_event_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sanctions_match_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    company: Mapped["Company"] = relationship(back_populates="score_snapshots")


class CompanyRegistryData(Base):
    """Raw + timestamped snapshots from KRS, CEIDG, MF, etc."""

    __tablename__ = "company_registry_data"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # KRS, CEIDG_V2, MF, ...
    raw_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped["Company"] = relationship(back_populates="registry_rows")


class CompanyPerson(Base):
    """Management / supervisory board / beneficial where available."""

    __tablename__ = "company_persons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    start_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    end_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    company: Mapped["Company"] = relationship(back_populates="persons")


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)  # 0..1
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_person: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    article_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("articles.id", ondelete="SET NULL"), nullable=True
    )
    sanctions_list: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_excluded: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped["Company"] = relationship(back_populates="risk_events")


class ScanJob(Base):
    """Tracks background scans so the UI can poll progress."""

    __tablename__ = "scan_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|running|done|error
    # Fine-grained pipeline stage for nice UI progress. One of:
    # resolving | registry | scraping | analyzing | events | verdict | synth | done
    stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    stage_detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sources_found: Mapped[int] = mapped_column(Integer, default=0)
    articles_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
