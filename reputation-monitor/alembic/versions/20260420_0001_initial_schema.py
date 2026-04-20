"""initial schema

Revision ID: 20260420_0001
Revises:
Create Date: 2026-04-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260420_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("nip", sa.String(length=32), nullable=True),
        sa.Column("krs", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_companies_nip"), "companies", ["nip"], unique=False)

    op.create_table(
        "articles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url", name="uq_articles_url"),
    )
    op.create_index(op.f("ix_articles_company_id"), "articles", ["company_id"], unique=False)

    op.create_table(
        "article_analysis",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column("risk_keywords", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("risk_category", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.Float(), nullable=True),
        sa.Column("raw_llm_response", sa.Text(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id"),
    )

    op.create_table(
        "score_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("article_count", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_score_history_company_id"), "score_history", ["company_id"], unique=False)
    op.create_index(op.f("ix_score_history_timestamp"), "score_history", ["timestamp"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_score_history_timestamp"), table_name="score_history")
    op.drop_index(op.f("ix_score_history_company_id"), table_name="score_history")
    op.drop_table("score_history")
    op.drop_table("article_analysis")
    op.drop_index(op.f("ix_articles_company_id"), table_name="articles")
    op.drop_table("articles")
    op.drop_index(op.f("ix_companies_nip"), table_name="companies")
    op.drop_table("companies")
