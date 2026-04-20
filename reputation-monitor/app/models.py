import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    nip: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    krs: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    articles: Mapped[List["Article"]] = relationship(back_populates="company")
    score_snapshots: Mapped[List["ScoreHistory"]] = relationship(back_populates="company")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("url", name="uq_articles_url"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="articles")
    analysis: Mapped[Optional["ArticleAnalysis"]] = relationship(back_populates="article", uselist=False)


class ArticleAnalysis(Base):
    __tablename__ = "article_analysis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_keywords: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    risk_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    severity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_llm_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped["Article"] = relationship(back_populates="analysis")


class ScoreHistory(Base):
    __tablename__ = "score_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_components: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    company: Mapped["Company"] = relationship(back_populates="score_snapshots")
