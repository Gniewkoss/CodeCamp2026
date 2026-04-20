# 🛡️ Reputation Monitor

AI-powered **media reputation & investment-risk** monitoring for AML / due-diligence workflows — built for the Transparent Data hackathon challenge.

The system aggregates Polish and international media coverage about a company, uses **Claude (Anthropic)** to analyse every article for sentiment, AML red-flags and investment impact, then produces a **reputation score** and **investment-risk score** with a clear recommendation (`Proceed / Monitor / Caution / Avoid`) and a full trend over time.

---

## Highlights

- **AI-first pipeline** — every article is analysed by `claude-sonnet-4-5`, returning structured JSON with sentiment (-1..+1), risk level, risk categories, severity (0..10), investment impact, PL summary, key-facts and AML red-flags.
- **Company registry with fuzzy matching** — full names, aliases, NIP, KRS, ticker, sector. Search uses RapidFuzz token-sort ratio.
- **Dual-score model** — reputational risk (0..100) and investment risk (0..100) with a combined recommendation. Time-decayed, source-weighted, sentiment-amplified.
- **Multi-source scraping** — NewsAPI, GDELT and a curated RSS set (`pb.pl`, `bankier.pl`, `money.pl`, `wyborcza.biz`, `rp.pl`, `forsal`, `businessinsider.com.pl`).
- **Modern single-page UI** — Tailwind + Alpine.js + Chart.js, dark glass-morphism, served directly by FastAPI. No build step.
- **Zero-setup demo** — SQLite by default; one `uvicorn` command and you're running.
- **Background scanning** — FastAPI `BackgroundTasks` with a `ScanJob` table so the UI can poll progress.

---

## Quickstart (Windows, PowerShell)

```powershell
# 1. Create a virtualenv and install
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Copy .env.example to .env and fill in your keys (an .env with demo keys
#    is already included — double-check it before committing anywhere).
#    ANTHROPIC_API_KEY=...
#    NEWSAPI_KEY=...

# 3. Seed a few well-known Polish companies (optional)
python .\scripts\seed_companies.py

# 4. Run the API + UI
python -m uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/>.

### Linux / macOS

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/seed_companies.py
uvicorn app.main:app --reload
```

### Docker

```bash
docker compose up --build
```

---

## How it works

```
┌──────────┐   ┌───────────┐   ┌────────────────┐   ┌──────────────┐
│ NewsAPI  │──►│           │   │                │   │              │
│ GDELT    │──►│ Collector │──►│  Claude-based  │──►│ Scoring &    │──► UI
│ RSS (PL) │──►│           │   │  Analyzer      │   │ Recommender  │
└──────────┘   └───────────┘   └────────────────┘   └──────────────┘
```

1. **Collector** (`app/scraper/sources.py`) gathers raw articles for a company's name + aliases; the full article body is pulled via httpx + BeautifulSoup.
2. **Claude analyzer** (`app/analysis/claude_analyzer.py`) sends each article with a schema-strict prompt and returns structured JSON:

   ```json
   {
     "mentions_company": true,
     "sentiment_score": -0.7,
     "sentiment_label": "negative",
     "risk_level": "high",
     "risk_categories": ["corruption", "legal"],
     "risk_keywords": ["CBA", "zarzuty", "łapówka"],
     "severity": 8.2,
     "investment_impact": "negative",
     "investment_risk": 7.8,
     "summary": "…",
     "key_facts": ["…"],
     "red_flags": ["…"]
   }
   ```

3. **Scoring** (`app/scoring/calculator.py`) aggregates per-article contributions with:
   - exponential recency decay (`λ = 0.025/day`),
   - source authority weight (e.g. `pb.pl` = 1.0, `gdelt` = 0.55),
   - category lexicon weight (corruption/money-laundering = 10, operational = 3.5),
   - negative-sentiment amplifier up to 2×.

   Output: reputational score, investment score, category breakdown, top-5 articles and a `Proceed/Monitor/Caution/Avoid` recommendation.
4. **History** — every rescore is persisted to `score_history`, enabling the 90-day trend chart in the UI.

---

## API highlights

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/companies` | List companies with current scores |
| `POST` | `/api/companies` | Create |
| `GET`  | `/api/companies/{id}` | Detailed view + components |
| `DELETE` | `/api/companies/{id}` | Remove |
| `POST` | `/api/companies/{id}/scan` | Trigger a background AI scan |
| `GET`  | `/api/scans/{job_id}` | Poll scan progress |
| `GET`  | `/api/companies/{id}/articles` | Articles + per-article analysis |
| `GET`  | `/api/companies/{id}/score/history` | Historical scores |
| `GET`  | `/api/dashboard/overview` | KPIs, top-10 risks, category mix |
| `GET`  | `/api/dashboard/high-risk-articles` | Recent high-severity items |

OpenAPI explorer: <http://127.0.0.1:8000/docs>.

---

## Configuration

All settings live in `.env` (see `.env.example`):

```env
DATABASE_URL=sqlite:///./repmonitor.db         # or postgresql+psycopg2://user:pass@host:5432/repmonitor
ANTHROPIC_API_KEY=<your key>
ANTHROPIC_MODEL=claude-sonnet-4-5              # or claude-opus-4-1-20250805 for maximum quality
NEWSAPI_KEY=<your key>
GDELT_ENABLED=true
```

- Switching to Postgres only requires changing `DATABASE_URL` and installing `psycopg2-binary`.
- If the Anthropic key is missing, the system falls back to a deterministic lexicon-only analysis so demos never break.

---

## Mapping to the challenge brief

| Challenge requirement | Where it's addressed |
|---|---|
| Algorytm analizy sentymentu z kontekstem branży | `app/analysis/claude_analyzer.py` – schema-strict Claude prompt with Polish AML context |
| Mechanizm scoringu reputacyjnego + historia | `app/scoring/calculator.py`, `ScoreHistory` table, 90-day trend in UI |
| Rejestr firm + warianty nazw + NIP/KRS | `Company` model, `/api/search` fuzzy matching, seed script |
| Działające demo | Single `uvicorn` command → full dashboard at `/` |
| Aplikacja webowa, wizualizacja trendów | `/app/web` SPA with Chart.js |
| Crawler | `app/scraper/sources.py` + `app/scraper/extractor.py` |
| Powiązanie z sankcjami / red flags | Sanctions & money-laundering categories with highest lexicon weight + Claude-emitted `red_flags` list |
| Słowa kluczowe: łapówka, zarzuty, sankcje, pranie pieniędzy, oszustwo… | `app/analysis/risk_lexicon.py` |
| Polski język, fleksja, homonimy | Claude handles flekja; name resolution uses RapidFuzz + aliases |
| Online + dane historyczne | Scans run on demand (background) + snapshots kept forever |

---

## Project layout

```
reputation-monitor/
├─ app/
│  ├─ analysis/        # Claude-driven analyzer + risk lexicon
│  ├─ api/             # FastAPI routes
│  ├─ scoring/         # Reputation + investment-risk math
│  ├─ scraper/         # Source collection + body extractor + bg tasks
│  ├─ web/             # Single-page UI (Tailwind + Alpine + Chart.js)
│  ├─ config.py        # Settings
│  ├─ database.py      # SQLAlchemy engine + session
│  ├─ main.py          # FastAPI app entry
│  └─ models.py        # ORM models
├─ scripts/seed_companies.py
├─ requirements.txt
├─ Dockerfile
├─ docker-compose.yml
└─ README.md
```
