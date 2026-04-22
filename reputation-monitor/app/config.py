from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./repmonitor.db"

    # LLM routing — see ``app/llm.py`` (unified OpenAI + Anthropic).
    # LLM_PROVIDER: anthropic | openai
    llm_provider: str = "anthropic"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o"
    # Default cap for app.analysis.* calls; ``llm_complete`` also falls back to anthropic_max_tokens.
    llm_max_tokens: int = 2200

    # Anthropic Claude — core of the analysis engine.
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_max_tokens: int = 1600

    # News sources
    newsapi_key: Optional[str] = None
    gdelt_enabled: bool = True

    # Polish business registries --------------------------------------------
    # GUS BIR (REGON) — SOAP. Production: wyszukiwarkaregon.stat.gov.pl
    # Docs: https://api.stat.gov.pl/Home/RegonApi — set GUS_BIR_API_KEY in .env
    # (never commit keys). Test endpoint: wyszukiwarkaregontest.stat.gov.pl
    gus_bir_api_url: str = "https://wyszukiwarkaregon.stat.gov.pl/wsBIR/UslugaBIRzewnPubl.svc"
    gus_bir_api_key: str = ""
    gus_bir_enabled: bool = False

    # CEIDG (sole traders). Requires a free JWT token from biznes.gov.pl —
    # set CEIDG_API_TOKEN to enable. Base URL defaults to production.
    ceidg_api_url: str = "https://dane.biznes.gov.pl/api/ceidg/v3"
    ceidg_api_token: Optional[str] = None
    # CEIDG v2 single-firma endpoint (same Bearer token).
    ceidg_v2_api_url: str = "https://dane.biznes.gov.pl/api/ceidg/v2"

    # Sanctions XML/HTML cache directory (Docker: /data/sanctions)
    sanctions_cache_dir: str = "./data/sanctions"

    # Safety limits so a single scan never runs away.
    # 25 was too tight — large companies (InPost, Orlen) have hundreds of
    # fresh articles per month. 100 keeps the Claude bill sane while giving
    # the verdict real coverage.
    max_articles_per_scan: int = 100
    max_article_chars: int = 14000
    # News recency window (days). NewsAPI is queried with ?from=today-N
    # so each scan brings in FRESH coverage, not the same backlog.
    news_lookback_days: int = 60
    # Skip re-analysing articles that were already analysed within this window
    # (avoids burning Claude credits on every re-scan).
    reanalysis_cooldown_hours: int = 24

    # ── Composite (multi-pillar) risk score weights ──────────────────────
    # Must sum to ~1.0. Priorytet: twarde dane finansowe i komercyjne nad
    # medialnym szumem. Efekt praktyczny: firma ze zdrowym bilansem (InPost,
    # Orlen) NIE może dostać wysokiego risk score tylko z powodu negatywnych
    # artykułów — ryzyko = 60% to co widać w rejestrach + kontraktach, 15%
    # media, 10% governance, 15% legal.
    score_weight_financial: float = 0.45
    score_weight_commercial: float = 0.15
    score_weight_legal: float = 0.15
    score_weight_governance: float = 0.10
    score_weight_media: float = 0.15

    # Minimalny udział filaru finansowego wymagany, aby rekomendacja była
    # "wiarygodna": gdy mamy tylko sygnały medialne i zero finansów, obniżamy
    # confidence do low (używane przez build_composite).
    composite_min_confidence_pillars: int = 2

    # ── Financial health sub-score calibration ───────────────────────────
    # Punkty składowe financial_health_score (0-100). Suma = 1.0.
    fh_weight_liquidity: float = 0.25
    fh_weight_leverage: float = 0.25
    fh_weight_profitability: float = 0.20
    fh_weight_bankruptcy: float = 0.20  # Altman/Mączyńska
    fh_weight_trend: float = 0.10

    # ── Trade credit limit calibration ───────────────────────────────────
    # Baseline formuła: min(equity * eq_ratio, revenue_monthly * rev_ratio).
    tcl_equity_ratio: float = 0.10          # 10% kapitału własnego
    tcl_revenue_monthly_ratio: float = 0.25  # 25% miesięcznych przychodów
    # Mnożniki zależne od kondycji (stosowane do baseline).
    tcl_factor_excellent: float = 1.30
    tcl_factor_good: float = 1.00
    tcl_factor_watch: float = 0.60
    tcl_factor_distress: float = 0.20
    tcl_factor_insured_bonus: float = 1.20
    tcl_factor_uninsured_penalty: float = 0.85
    tcl_factor_payment_late: float = 0.70
    tcl_factor_payment_severely_late: float = 0.35

    # Per-domain cooldowns (days). Skip refreshing these subsystems if we
    # already have a snapshot younger than this many days — keeps API and
    # Claude costs predictable.
    financials_refresh_days: int = 30
    balance_ai_refresh_days: int = 30
    contracts_refresh_days: int = 7
    insurance_refresh_days: int = 14
    payments_refresh_days: int = 14
    governance_refresh_days: int = 30
    regulatory_refresh_days: int = 7

    # Feature flags for optional / paid data sources.
    enable_ted: bool = True
    enable_bzp: bool = True
    enable_msig: bool = False
    imsig_api_key: Optional[str] = None
    big_infomonitor_api_key: Optional[str] = None

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
