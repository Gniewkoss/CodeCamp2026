# Reputation Monitor

Aplikacja **FastAPI** do monitorowania reputacji medialnej i ryzyka inwestycyjnego / AML dla polskich podmiotów (i szerszego due diligence). Łączy zbieranie newsów, analizę LLM, rejestry (KRS, CEIDG, MF), sankcje, warstwę finansową (sprawozdania, wskaźniki, kontrakty publiczne) oraz **model ryzyka wielofilarowy** (composite score) z rekomendacją `Proceed` / `Monitor` / `Caution` / `Avoid`.

Interfejs: **SPA** (`app/web/`) serwowana statycznie z tego samego procesu co API (`/`). Baza domyślnie **SQLite**; możliwy Postgres przez `DATABASE_URL`.

---

## Stack

| Warstwa | Technologie |
|--------|-------------|
| API | FastAPI, Pydantic Settings, SQLAlchemy 2.x |
| LLM | `app/llm.py` — **Anthropic** (domyślnie) lub **OpenAI** (`LLM_PROVIDER=openai`); brak klucza aktywnego providera → heurystyki offline (m.in. leksykon w `claude_analyzer.py`) |
| HTTP / scraping | httpx, BeautifulSoup, feedparser |
| UI | HTML + Tailwind (CDN) + Alpine.js + Chart.js — **bez build step** |
| Kontenery | `Dockerfile`, `docker-compose.yml` |

---

## Szybki start

W katalogu projektu `reputation-monitor/`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Uzupełnij w .env co najmniej ANTHROPIC_API_KEY (domyślny LLM_PROVIDER=anthropic) lub OPENAI_API_KEY przy LLM_PROVIDER=openai; opcjonalnie NEWSAPI_KEY, CEIDG_API_TOKEN, GUS_BIR_* — bez commitowania .env
python scripts/seed_companies.py   # opcjonalnie
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Otwórz [http://127.0.0.1:8000/](http://127.0.0.1:8000/) — UI; dokumentacja interaktywna API: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

**Windows (PowerShell):** analogicznie `python -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, `python -m uvicorn app.main:app --reload`.

**Docker:** `docker compose up --build`.

**Uwaga:** Uruchamiaj serwer z katalogu `reputation-monitor/`, żeby pakiet `app` był widoczny dla Uvicorna.

---

## Architektura wysokiego poziomu

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ NewsAPI,    │     │ Collector +      │     │ Analiza artykułów│
│ Google News │────►│ ekstrakcja treści│────►│ (LLM + leksykon) │
│ RSS, GDELT  │     │ (sources/extract)│     │                  │
└─────────────┘     └────────┬─────────┘     └────────┬──────────┘
                             │                        │
┌─────────────┐              │     ┌──────────────────▼──────────┐
│ KRS / CEIDG │──────────────┴────►│ Pipeline skanu (tasks.py)    │
│ MF / GUS    │                    │ + scoring + composite        │
│ sankcje     │                    └──────────────────┬──────────┘
└─────────────┘                                       │
                                                      ▼
                                            SQLite / Postgres + UI
```

---

## Pipeline skanu (`POST /api/companies/{id}/scan`)

Tło: `BackgroundTasks` → `run_scan_in_background` → `scrape_company_sync` (`app/scraper/tasks.py`). Postęp zapisuje `ScanJob` (`pending` → `running` → `done` / `error`); UI odpytuje `GET /api/scans/{job_id}`.

Kolejność etapów (pole `stage` w `ScanJob`):

1. **scraping** — `collect_all_sources`: NewsAPI (jeśli klucz), **Google News RSS** (bez klucza), kanały RSS z `sources.py`, GDELT (wg `GDELT_ENABLED`).
2. **registry** — `sync_all_registries`: KRS (odpis aktualny JSON), CEIDG v2, uzupełnianie firmy, osoby, snapshoty w `company_registry_data`.
3. **analyzing** — materializacja `Article`, ekstrakcja treści (`extractor.py`), analiza LLM równolegle (`ThreadPoolExecutor`, `ANALYSIS_WORKERS`), cooldown ponownej analizy (`REANALYSIS_COOLDOWN_HOURS`), twardy limit czasu (`ANALYSIS_DEADLINE_SECONDS`).
4. Dla każdej przeanalizowanej publikacji z `mentions_company=True` — **wykrywanie zdarzeń** (`event_detector.py` → `RiskEvent`).
5. **events** — `apply_sanctions_check` (cache plików w `SANCTIONS_CACHE_DIR`).
6. **financials** — sprawozdania (m.in. KRS RDF), wskaźniki (`financial_pipeline.refresh_financial_*`).
7. **balance_ai** — syntetyczna ocena bilansu z kilku lat (`balance_ai_analyzer`).
8. **contracts** — intensywność kontraktów publicznych (TED, BZP, prasa — flagi `ENABLE_TED`, `ENABLE_BZP`, itd.).
9. **insurance** — heurystyki ubezpieczeniowe (`insurance_detector`).
10. **payments** — opinia płatnicza (`payment_reputation`; opcjonalnie BIG przy kluczu).
11. **governance** — ryzyko osób powiązanych z KRS (`governance_risk`).
12. **regulatory** — m.in. KRS dział 6, MSiG (`ENABLE_MSIG`, `IMSIG_API_KEY`).
13. **limit** — rekomendowany limit kupiecki (`trade_credit_limit`).
14. **verdict** — `recalculate_and_persist` — **composite score** + zapis `ScoreHistory`.
15. **synth** — agregacja SWOT / teza inwestycyjna na `Company` (`synthesize_company_insights`), jeśli były nowe analizy.

Przy starcie aplikacji (`main.py`) skany w stanie `running` są oznaczane jako `error`, żeby uniknąć „zawieszonego” UI po restarcie serwera.

---

## Model ryzyka i scoring

- **Artykuł** — struktura z LLM: sentyment, kategorie ryzyka, severity, wpływ inwestycyjny, red flags, wiarygodność źródła (`ArticleAnalysis`). Moduł wywołań: `app/analysis/claude_analyzer.py` (nazwa historyczna; faktyczny provider przez `app/llm.py`).
- **Composite score** (`app/analysis/composite_score.py`) — pięć filarów 0–100 (wyżej = ryzykowniej), ważonych przez `SCORE_WEIGHT_*` w `config.py`:
  - financial, commercial, legal, governance, media  
  Twardych override’ów (np. sankcje, restrykcyjne sygnały prawne) szukaj w logice `build_composite` / kalkulatora.
- **Agregacja mediów** — `app/scoring/calculator.py`: decay czasowy, wagi źródeł, leksykon kategorii (`risk_lexicon.py`), historia w `score_history`.
- **Ledger zdarzeń** — `RiskEvent` + endpointy w `risk_routes.py`; wpływ na werdykt zależy od `calculator` / event lifecycle.

---

## Rejestry i zewnętrzne API

| Źródło | Moduł / uwagi |
|--------|----------------|
| **KRS (Open API)** | `app/scraper/krs_client.py` — host `https://api-krs.ms.gov.pl`, dokumentacja portalu: [PRS — Otwarte API KRS](https://prs.ms.gov.pl/krs/openApi). Funkcje: odpis aktualny / pełny, biuletyny; użycie w sync: głównie odpis aktualny + ekstrakcja organów. |
| **CEIDG** | `ceidg_v2.py` — Bearer `CEIDG_API_TOKEN` z biznes.gov.pl. |
| **MF (biała lista VAT)** | `registry.py` — `lookup_nip` itd., bez klucza. |
| **GUS BIR (REGON)** | SOAP w `registry.py`; włączenie: `GUS_BIR_ENABLED=true` + `GUS_BIR_API_KEY` + URL test/prod. |
| **Sankcje** | `services/sanctions_sync.py`, `analysis/sanctions_checker.py` — cache XML/CSV w `SANCTIONS_CACHE_DIR`. |
| **TED / BZP / MSiG** | `contracts.py`, `msig_client.py`, `prs_scraper.py` — sterowane `ENABLE_*` i kluczami w `.env`. |

---

## REST API — przegląd

Pełna specyfikacja: **Swagger** `/docs`. Poniżej skrót ścieżek.

### Core (`app/api/routes.py`)

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| GET | `/api/companies` | Lista firm z aktualnymi ocenami |
| POST | `/api/companies` | Utworzenie firmy |
| GET | `/api/companies/{id}` | Szczegóły + komponenty score |
| DELETE | `/api/companies/{id}` | Usunięcie |
| GET | `/api/search` | Wyszukiwanie nazw (RapidFuzz) |
| POST | `/api/companies/quick-lookup` | Szybkie dodanie / rozstrzygnięcie po NIP/KRS/REGON/nazwie |
| GET | `/api/registry/lookup` | Podgląd rejestru po identyfikatorze (bez tworzenia firmy) |
| GET | `/api/companies/{id}/score/history` | Historia punktów score |
| POST | `/api/companies/{id}/recalculate` | Przeliczenie werdyktu z istniejących danych |
| GET | `/api/companies/{id}/articles` | Artykuły + analizy |
| POST | `/api/companies/{id}/scan` | Start skanu w tle |
| GET | `/api/scans/{job_id}` | Status skanu |
| GET | `/api/companies/{id}/scans` | Historia skanów dla firmy |
| POST | `/api/companies/{id}/synthesize` | Ponowna synteza SWOT z analiz |
| GET | `/api/dashboard/overview` | KPI dashboardu |
| GET | `/api/dashboard/high-risk-articles` | Lista wysokiego ryzyka |
| GET | `/api/risk-categories` | Słownik kategorii |

### Risk / rejestry / ledger (`app/api/risk_routes.py`)

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| GET/POST/PUT/DELETE | `/api/companies/{id}/events` … | CRUD zdarzeń ryzyka |
| POST | `.../events/{event_id}/resolve` | Rozstrzygnięcie zdarzenia |
| POST | `/api/companies/{id}/registry/refresh` | Odświeżenie sync rejestrów |
| POST | `/api/companies/{id}/registry/krs` | Wymuszenie ścieżki KRS |
| POST | `/api/companies/{id}/registry/ceidg` | CEIDG |
| POST | `/api/companies/{id}/sanctions/recheck` | Ponowny screening sankcyjny |
| POST | `/api/admin/cleanup-orphan-events` | Porządki administracyjne |
| GET | `/api/ledger/companies` | Widok ledgera po firmach |
| GET | `/api/companies/{id}/ledger` | Ledger dla jednej firmy |
| GET | `/api/companies/{id}/registry/data` | Surowe snapshoty `CompanyRegistryData` |

### Finanse i filary (`app/api/finance_routes.py`)

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| GET/POST | `/api/companies/{id}/financials` (+ `/refresh`) | Sprawozdania / figury |
| GET/POST | `/api/companies/{id}/trade-credit-limit` (+ `/refresh`) | Limit kupiecki |
| GET/POST | `/api/companies/{id}/contracts` (+ `/refresh`) | Kontrakty |
| GET/POST | `/api/companies/{id}/payment-reputation` (+ `/refresh`) | Płatności |
| GET/POST | `/api/companies/{id}/insurance` (+ `/refresh`) | Sygnały ubezpieczeniowe |
| GET/POST | `/api/companies/{id}/governance` (+ `/refresh`) | Governance |
| GET/POST | `/api/companies/{id}/regulatory` (+ `/refresh`) | Zdarzenia regulacyjne |
| GET | `/api/companies/{id}/profile-bundle` | Zbiorczy pakiet profilu ekonomicznego |

### Inne

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| GET | `/health` | Healthcheck |

---

## Model danych (skrót)

Główne tabele w `app/models.py`:

- **Company** — nazwa, aliasy, NIP, KRS, REGON, branża, metadane rejestrów, cache SWOT (`ai_summary`, `strengths` / `weaknesses` / …).
- **Article**, **ArticleAnalysis** — treść + wynik LLM.
- **ScoreHistory** — snapshoty: score złożony, `investment_score`, `recommendation`, `score_components`, kolumny filarów (`financial_score`, …), liczniki sankcji / ledger.
- **CompanyRegistryData** — JSON z KRS/CEIDG/MF itd.
- **CompanyPerson** — osoby z KRS+LLM lub innych źródeł.
- **RiskEvent** — zdarzenia z artykułów lub sankcji.
- **ScanJob** — postęp skanu.
- **FinancialStatement**, **FinancialFigures**, **FinancialRatios**, **FinancialAIAnalysis** — warstwa finansowa.
- Dodatkowe encje na kontrakty, płatności, ubezpieczenia, governance, regulatory — zgodnie z importami w `finance_routes` / `financial_pipeline`.

---

## Konfiguracja (`app/config.py` + `.env`)

Wszystkie zmienne są opisane w **`.env.example`** (skopiuj do `.env`). Nazwy w `.env` odpowiadają polom `Settings` (wielkie litery, podkreślenia). Najważniejsze grupy:

- **Baza:** `DATABASE_URL`
- **LLM:** `LLM_PROVIDER` (`anthropic` | `openai`), `ANTHROPIC_*`, `OPENAI_*`, `LLM_MAX_TOKENS`
- **News:** `NEWSAPI_KEY`, `GDELT_ENABLED`, limity `MAX_ARTICLES_PER_SCAN`, `NEWS_LOOKBACK_DAYS`, `REANALYSIS_COOLDOWN_HOURS`, `ANALYSIS_WORKERS`, `ANALYSIS_DEADLINE_SECONDS`, `MAX_ARTICLE_CHARS`
- **Rejestry PL:** `CEIDG_API_TOKEN`, `GUS_BIR_*`, URL-e CEIDG
- **Sankcje:** `SANCTIONS_CACHE_DIR`
- **Wagi composite / financial health / trade credit:** `SCORE_WEIGHT_*`, `FH_WEIGHT_*`, `TCL_*`
- **Cooldown odświeżeń filarów:** `FINANCIALS_REFRESH_DAYS`, `CONTRACTS_REFRESH_DAYS`, itd.
- **Opcjonalne źródła:** `ENABLE_TED`, `ENABLE_BZP`, `ENABLE_MSIG`, `IMSIG_API_KEY`, `BIG_INFOMONITOR_API_KEY`

**Bezpieczeństwo:** nie commituj pliku `.env`; trzymaj klucze API poza repozytorium.

---

## Zbieranie newsów (`app/scraper/sources.py`)

- **NewsAPI** — `/v2/everything`, okno `NEWS_LOOKBACK_DAYS`.
- **Google News RSS** — wyszukiwanie po nazwie i aliasach (bez klucza).
- **RSS** — lista `RSS_FEEDS` (m.in. pb.pl, bankier.pl, money.pl, …).
- **GDELT** — `api.gdeltproject.org` (opcjonalnie wyłącz).

Treść artykułów jest dociągana przez `extractor.py` (httpx + BS4), z heurystyką języka.

---

## Front-end (`app/web/`)

Statyczne pliki montowane w `main.py`. Wywołuje endpointy `/api/...`; wykres historii score (Chart.js), dashboard, szczegóły firmy, skany.

---

## Skrypty i testy

| Plik | Rola |
|------|------|
| `scripts/seed_companies.py` | Przykładowe firmy w bazie |
| `scripts/test_risk_logic.py` | Testy logiki ryzyka (uruchom ręcznie) |

---

## Układ katalogów

```
reputation-monitor/
├── app/
│   ├── main.py              # FastAPI, CORS, routery, static web, startup
│   ├── config.py          # Settings / .env
│   ├── database.py        # Engine, sesja, init_db
│   ├── models.py          # ORM
│   ├── llm.py             # Anthropic / OpenAI + retry
│   ├── api/
│   │   ├── routes.py      # firmy, skany, dashboard
│   │   ├── risk_routes.py
│   │   └── finance_routes.py
│   ├── analysis/          # LLM prompts, composite, finanse, governance, …
│   ├── scoring/           # calculator, event_lifecycle
│   ├── scraper/           # sources, tasks, KRS, CEIDG, kontrakty, …
│   ├── services/          # registry_sync, sanctions_sync
│   └── web/               # index.html, assety JS/CSS
├── scripts/
├── data/                  # cache sankcji (ścieżka konfigurowalna)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Hackathon / brief (mapowanie funkcji)

| Wymaganie (idea) | Realizacja w kodzie |
|------------------|---------------------|
| Analiza sentymentu + kontekst AML | `claude_analyzer.py` + `risk_lexicon.py` |
| Scoring + historia | `calculator.py`, `ScoreHistory`, wykres w UI |
| Rejestr firm, NIP/KRS/aliasy | `Company`, `routes` (search, quick-lookup), `registry.py` |
| Crawler / źródła | `sources.py`, `extractor.py`, `tasks.py` |
| Sankcje / red flags | `sanctions_sync`, `sanctions_checker`, kategorie w analizie LLM |

---

## Licencja / dane

Dane z KRS wykorzystywane zgodnie z otwartym API Ministerstwa Sprawiedliwości ([PRS — Otwarte API KRS](https://prs.ms.gov.pl/krs/openApi)). Pozostałe źródła wymagają własnych kluczy i przestrzegania regulaminów dostawców.
