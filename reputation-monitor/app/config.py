from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./repmonitor.db"

    # Anthropic Claude — core of the analysis engine.
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_max_tokens: int = 1600

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

    # Safety limits so a single scan never runs away
    max_articles_per_scan: int = 25
    max_article_chars: int = 14000

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
