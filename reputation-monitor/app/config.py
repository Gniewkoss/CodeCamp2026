from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./repmonitor.db"

    # LLM provider: "anthropic" (default) | "openai".
    # When set to anthropic, ``app.llm.active_provider`` does not fall back to
    # OpenAI — set ANTHROPIC_API_KEY. For OpenAI-only, set LLM_PROVIDER=openai.
    llm_provider: str = "anthropic"  # "openai" | "anthropic"

    # OpenAI — optional; use LLM_PROVIDER=openai + OPENAI_API_KEY.
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o"

    # Anthropic Claude (default path for this project).
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-5"

    # Max response tokens for LLM calls. 2200 leaves enough headroom for
    # the biggest prompt (article analysis with Polish summary + red_flags
    # + positives) without getting cut off mid-JSON.
    llm_max_tokens: int = 2200
    # Back-compat alias — pre-existing code reads settings.anthropic_max_tokens.
    anthropic_max_tokens: int = 2200

    # News sources
    newsapi_key: Optional[str] = None
    gdelt_enabled: bool = True

    # Polish business registries --------------------------------------------
    # GUS BIR (REGON) — SOAP. Off by default until you have a key from GUS.
    # When ready: GUS_BIR_ENABLED=true, set GUS_BIR_API_URL + GUS_BIR_API_KEY.
    gus_bir_api_url: str = "https://wyszukiwarkaregontest.stat.gov.pl/wsBIR/UslugaBIRzewnPubl.svc"
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
    # 100 articles × 6-10s sequential Claude calls = ~8-15 minutes scan.
    # We now run Claude calls in a thread pool (see ``analysis_workers``), so
    # the article cap can stay generous — but we still trim so single scans
    # don't burn 100+ credits without the user noticing.
    max_articles_per_scan: int = 60
    # Previously 14000 — that forced Claude to chew through very long bodies
    # and frequently hit the response-token cap mid-JSON, which caused
    # "Claude returned non-JSON; falling back" warnings. 8000 gives the model
    # plenty of context (TL;DR + body intro + first few sections) and roughly
    # halves per-article latency.
    # 0 = no truncation, pass the full article body to the LLM. gpt-4o has a
    # 128k context so even very long investigative pieces fit comfortably.
    # Set a positive number here if you ever need to cap cost per call.
    max_article_chars: int = 0
    # News recency window (days). NewsAPI is queried with ?from=today-N
    # so each scan brings in FRESH coverage, not the same backlog.
    news_lookback_days: int = 60
    # Skip re-analysing articles that were already analysed within this window
    # (avoids burning Claude credits on every re-scan).
    reanalysis_cooldown_hours: int = 24
    # How many Claude analyses to run in parallel during a scan. The Anthropic
    # API comfortably handles ~10 concurrent requests per key; we keep a
    # safety margin so we don't get rate-limited.
    analysis_workers: int = 8
    # Hard deadline for the analysis stage (seconds). If we can't finish all
    # articles within this budget we stop issuing new Claude calls and move
    # on to the registry/governance/regulatory stages — the user explicitly
    # asked us not to freeze the scan for 10+ minutes. Anything already
    # persisted stays in the DB.
    analysis_deadline_seconds: int = 240

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
