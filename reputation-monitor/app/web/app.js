/* Reputation Monitor — front-end state & rendering.
   Plain Alpine.js (no build step). */

const CATEGORY_LABELS = {
  corruption: "Korupcja",
  legal: "Sprawy prawne",
  management: "Zarząd",
  sanctions: "Sankcje",
  financial: "Problemy fin.",
  money_laundering: "Pranie pieniędzy",
  regulatory: "Regulacyjne",
  operational: "Operacyjne",
  esg: "ESG",
};

const RECOMMENDATION_HEADLINES = {
  Proceed: "Możesz działać",
  Monitor: "Zalecany monitoring",
  Caution: "Wymaga ostrożności",
  Avoid: "Odradzamy współpracę",
};

function app() {
  return {
    // ── State ─────────────────────────────────────────────────────────
    view: "overview",
    tabs: [
      { id: "overview", label: "Panel", icon: "📊" },
      { id: "companies", label: "Spółki", icon: "🏢" },
      { id: "ledger", label: "Ledger ryzyka", icon: "📜" },
    ],
    ledgerRows: [],
    ledgerDetail: null,
    ledgerLoading: false,
    companies: [],
    overview: null,
    highRisk: [],
    selected: null,
    articles: [],
    articleFilter: "relevant",
    profileBundle: null,
    profileTab: "overview",
    profileTabs: [
      { id: "overview",    label: "Przegląd",      icon: "📋" },
      { id: "finance",     label: "Finanse",       icon: "💰" },
      { id: "contracts",   label: "Kontraktacja",  icon: "📑" },
      { id: "press",       label: "Prasa",         icon: "📰" },
      { id: "registry",    label: "Rejestry",      icon: "🏛️" },
      { id: "governance",  label: "Governance",    icon: "👥" },
      { id: "ledger",      label: "Ledger",        icon: "📜" },
    ],
    articleFilters: [
      { id: "relevant", label: "Dotyczą firmy" },
      { id: "all", label: "Wszystkie" },
      { id: "unrelated", label: "Niezwiązane" },
      { id: "high", label: "Wysokie ryzyko" },
      { id: "flagged", label: "Z red flags" },
      { id: "positive", label: "Pozytywne" },
      { id: "trusted", label: "Wiarygodne" },
      { id: "fake", label: "Podejrzane" },
    ],
    searchQuery: "",
    searchResults: null,
    scanning: false,
    synthing: false,
    showAdd: false,
    submitting: false,
    lookup: { query: "", running: false, status: "", error: false },
    scanProgress: {
      visible: false,
      company: "",
      resolvedNote: "",
      elapsed: 0,
      steps: [
        { id: "resolve",    icon: "🧠", label: "Rozpoznanie firmy przez AI",            hint: "Claude szuka oficjalnej nazwy, NIP, KRS i aliasów medialnych",        status: "pending" },
        { id: "registry",   icon: "🏛️", label: "Rejestry MF / KRS / CEIDG",              hint: "Pobieranie danych prawnych: adres, zarząd, PKD, status VAT",         status: "pending" },
        { id: "scraping",   icon: "📡", label: "Zbieranie artykułów z sieci",           hint: "Google News, NewsAPI, RSS (pb.pl, bankier, forsal), GDELT",          status: "pending" },
        { id: "analyzing",  icon: "🤖", label: "Analiza AI każdego artykułu",           hint: "Sentyment, red flags, wiarygodność, dopasowanie do spółki",          status: "pending", progress: 0, total: 0 },
        { id: "events",     icon: "⚖️", label: "Sankcje UE / OFAC / MSW",               hint: "Lista konsolidowana, ograniczenia eksportowe",                       status: "pending" },
        { id: "financials", icon: "📊", label: "Sprawozdania finansowe (KRS RDF)",      hint: "3 lata bilansu + rachunek wyników, wskaźniki i Altman/Mączyńska",    status: "pending" },
        { id: "balance_ai", icon: "🧮", label: "Analiza bilansu 3Y przez Claude",       hint: "Kondycja, red flags, prognoza wypłacalności 12m",                    status: "pending" },
        { id: "contracts",  icon: "📑", label: "Kontraktacja (TED / BZP / prasa)",      hint: "Aktywne kontrakty, koncentracja klientów, trend YoY",                status: "pending" },
        { id: "insurance",  icon: "🛡️", label: "Sygnał ubezpieczenia należności",       hint: "Czy firma jest ubezpieczona (Euler, Coface, Atradius...)",           status: "pending" },
        { id: "payments",   icon: "⏱️", label: "Opinia rynkowa — płatności w terminie", hint: "DPO, zaległości w prasie, opcjonalnie BIG InfoMonitor",              status: "pending" },
        { id: "governance", icon: "👥", label: "Historia osób z KRS",                   hint: "Claude sprawdza przeszłe bankructwa, dyskwalifikacje zarządu",       status: "pending" },
        { id: "regulatory", icon: "🏛️", label: "KRS Dział 6 + MSiG / KRZ",              hint: "Bankructwo, restrukturyzacja, likwidacja, postępowania sądowe",      status: "pending" },
        { id: "limit",      icon: "💳", label: "Limit kupiecki",                        hint: "Rekomendacja limitu kredytu handlowego z korektami ryzyka",          status: "pending" },
        { id: "verdict",    icon: "🎯", label: "Werdykt AI (composite)",                hint: "5 filarów: Financial > Commercial > Legal > Governance > Media",     status: "pending" },
        { id: "synth",      icon: "📊", label: "SWOT i teza inwestycyjna",              hint: "Drugi pass Claude'a — mocne/słabe strony, szanse, zagrożenia",       status: "pending" },
      ],
      _timer: null,
    },
    newCompany: {
      name: "", nip: "", krs: "", ticker: "", sector: "", aliasesRaw: "", scanNow: true,
    },
    toasts: [],
    _charts: {},
    lastRefreshLabel: "—",

    // ── Lifecycle ─────────────────────────────────────────────────────
    async init() {
      await Promise.all([this.loadCompanies(), this.loadOverview(), this.loadHighRisk()]);
      this.updateLastRefresh();
      this.$nextTick(() => this.renderOverviewCharts());
      // Simple polling so scans in the background reflect on the overview
      setInterval(() => {
        if (this.view === "overview") {
          this.refreshOverview();
        }
      }, 30000);
    },

    async goto(tab) {
      this.view = tab;
      if (tab === "overview") {
        this.$nextTick(() => this.renderOverviewCharts());
      }
      if (tab === "ledger") {
        await this.loadLedger();
      }
    },

    // ── Data loaders ──────────────────────────────────────────────────
    async api(path, opts = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...opts,
      });
      if (!res.ok) {
        // FastAPI returns JSON `{"detail": "…"}` for HTTPException — surface
        // that text verbatim. We clone first because .json()/.text() each
        // consume the body stream.
        let msg = res.statusText || `HTTP ${res.status}`;
        const clone = res.clone();
        try {
          const body = await res.json();
          if (body && typeof body.detail === "string") msg = body.detail;
          else if (body && body.detail) msg = JSON.stringify(body.detail);
          else if (typeof body === "string") msg = body;
        } catch {
          try { msg = await clone.text(); } catch { /* keep statusText */ }
        }
        throw new Error(msg);
      }
      if (res.status === 204) return null;
      return res.json();
    },

    async loadCompanies() {
      try {
        this.companies = await this.api("/api/companies");
      } catch (e) { this.toast("Błąd ładowania spółek: " + e.message, "error"); }
    },

    async loadOverview() {
      try {
        this.overview = await this.api("/api/dashboard/overview");
      } catch (e) { this.toast("Błąd ładowania panelu: " + e.message, "error"); }
    },

    async loadHighRisk() {
      try {
        this.highRisk = await this.api("/api/dashboard/high-risk-articles?hours=72&min_severity=5");
      } catch (e) { this.highRisk = []; }
    },

    async loadLedger() {
      try {
        this.ledgerRows = await this.api("/api/ledger/companies");
      } catch (e) {
        this.ledgerRows = [];
        this.toast("Błąd ledger: " + e.message, "error");
      }
    },

    async openLedgerCompany(id) {
      this.ledgerLoading = true;
      this.ledgerDetail = null;
      try {
        this.ledgerDetail = await this.api(`/api/companies/${id}/ledger`);
        this.$nextTick(() => this.renderLedgerChart?.());
      } catch (e) {
        this.toast("Błąd szczegółów: " + e.message, "error");
      } finally {
        this.ledgerLoading = false;
      }
    },

    ledgerRiskBadge(score) {
      const s = score ?? 0;
      if (s > 70) return "🔴";
      if (s >= 40) return "🟡";
      return "🟢";
    },

    async ledgerRecheckSanctions(companyId) {
      try {
        await this.api(`/api/companies/${companyId}/sanctions/recheck`, { method: "POST" });
        this.toast("Ponowna weryfikacja sankcji zakończona", "success", "✅");
        await this.loadLedger();
        if (this.ledgerDetail?.company?.id === companyId) await this.openLedgerCompany(companyId);
      } catch (e) { this.toast(e.message, "error"); }
    },

    async ledgerRefreshRegistry(companyId) {
      try {
        await this.api(`/api/companies/${companyId}/registry/refresh`, { method: "POST" });
        this.toast("Odświeżono dane rejestrowe", "success");
        if (this.ledgerDetail?.company?.id === companyId) await this.openLedgerCompany(companyId);
      } catch (e) { this.toast(e.message, "error"); }
    },

    async ledgerResolveEvent(companyId, eventId) {
      const note = window.prompt("Notka zamknięcia (resolution):");
      if (!note) return;
      try {
        await this.api(`/api/companies/${companyId}/events/${eventId}/resolve`, {
          method: "POST",
          body: JSON.stringify({ resolution_note: note }),
        });
        this.toast("Zdarzenie oznaczone jako rozwiązane", "success");
        await this.openLedgerCompany(companyId);
        await this.loadLedger();
      } catch (e) { this.toast(e.message, "error"); }
    },

    renderLedgerChart() {
      const canvas = document.getElementById("ledger-score-chart");
      if (!canvas || !this.ledgerDetail?.score_timeline?.length) return;
      const labels = this.ledgerDetail.score_timeline.map((p) => (p.t || "").slice(0, 10));
      const scores = this.ledgerDetail.score_timeline.map((p) => p.score ?? 0);
      if (this._ledgerChart) { try { this._ledgerChart.destroy(); } catch (_) {} }
      this._ledgerChart = new Chart(canvas, {
        type: "line",
        data: {
          labels,
          datasets: [{
            label: "Wynik (0–100)",
            data: scores,
            borderColor: "#5a83ff",
            backgroundColor: "rgba(90,131,255,0.15)",
            fill: true,
            tension: 0.25,
          }],
        },
        options: {
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#94a3b8", maxRotation: 45 } },
            y: { min: 0, max: 100, ticks: { color: "#94a3b8" } },
          },
        },
      });
    },

    async refreshOverview() {
      await Promise.all([this.loadCompanies(), this.loadOverview(), this.loadHighRisk()]);
      this.updateLastRefresh();
      this.renderOverviewCharts();
    },

    updateLastRefresh() {
      this.lastRefreshLabel = new Date().toLocaleTimeString("pl-PL");
    },

    // ── Company detail ────────────────────────────────────────────────
    async openCompany(id) {
      this.view = "company";
      this.profileTab = "overview";
      this.profileBundle = null;
      try {
        [this.selected, this.articles] = await Promise.all([
          this.api(`/api/companies/${id}`),
          this.api(`/api/companies/${id}/articles?limit=80`),
        ]);
        this.$nextTick(() => this.renderCompanyChart(id));
        this.loadProfileBundle(id);
      } catch (e) {
        this.toast("Błąd ładowania spółki: " + e.message, "error");
      }
    },

    async loadProfileBundle(id) {
      try {
        this.profileBundle = await this.api(`/api/companies/${id}/profile-bundle`);
      } catch (e) {
        // Non-fatal: the main profile still loads.
        this.profileBundle = null;
      }
    },

    async refreshFinancials() {
      if (!this.selected) return;
      this.toast("Odświeżam dane finansowe…", "info", "💰");
      try {
        await this.api(`/api/companies/${this.selected.id}/financials/refresh`, { method: "POST" });
        await this.loadProfileBundle(this.selected.id);
        this.toast("Dane finansowe zaktualizowane", "success", "✅");
      } catch (e) {
        this.toast("Błąd odświeżania: " + e.message, "error");
      }
    },

    async refreshContractsEndpoint() {
      if (!this.selected) return;
      this.toast("Szukam nowych kontraktów…", "info", "📑");
      try {
        await this.api(`/api/companies/${this.selected.id}/contracts/refresh`, { method: "POST" });
        await this.loadProfileBundle(this.selected.id);
        this.toast("Kontrakty zaktualizowane", "success", "✅");
      } catch (e) {
        this.toast("Błąd: " + e.message, "error");
      }
    },

    async refreshGovernanceEndpoint() {
      if (!this.selected) return;
      this.toast("Sprawdzam historię osób…", "info", "👥");
      try {
        await this.api(`/api/companies/${this.selected.id}/governance/refresh`, { method: "POST" });
        await this.loadProfileBundle(this.selected.id);
        this.toast("Governance zaktualizowane", "success", "✅");
      } catch (e) {
        this.toast("Błąd: " + e.message, "error");
      }
    },

    fmtMoney(v, currency) {
      if (v == null || isNaN(v)) return "—";
      const c = currency || "PLN";
      const n = Number(v);
      if (Math.abs(n) >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + " mld " + c;
      if (Math.abs(n) >= 1_000_000) return (n / 1_000_000).toFixed(2) + " mln " + c;
      if (Math.abs(n) >= 1_000) return (n / 1_000).toFixed(1) + " tys. " + c;
      return n.toFixed(0) + " " + c;
    },

    fmtRatio(v, digits) {
      if (v == null || isNaN(v)) return "—";
      return Number(v).toFixed(digits ?? 2);
    },

    fmtPercent(v) {
      if (v == null || isNaN(v)) return "—";
      return (Number(v) * 100).toFixed(1) + "%";
    },

    conditionClass(cond) {
      const c = (cond || "unknown").toLowerCase();
      if (c === "excellent" || c === "good") return "text-good";
      if (c === "watch") return "text-warn";
      if (c === "distress") return "text-danger";
      return "text-slate-400";
    },

    dbtLabel(flag) {
      return ({
        on_time: "Płaci w terminie",
        late: "Opóźnia płatności",
        severely_late: "Poważne zaległości",
        unknown: "Brak danych",
      })[flag] || "Brak danych";
    },

    dbtClass(flag) {
      if (flag === "on_time") return "text-good";
      if (flag === "late") return "text-warn";
      if (flag === "severely_late") return "text-danger";
      return "text-slate-400";
    },

    insuranceLabel(state) {
      return ({
        known_insured: "Ubezpieczona (potwierdzone)",
        likely_insured: "Prawdopodobnie ubezpieczona",
        unknown: "Brak danych",
        likely_uninsured: "Brak sygnałów ubezpieczenia",
      })[state] || "Brak danych";
    },

    insuranceClass(state) {
      if (state === "known_insured" || state === "likely_insured") return "text-good";
      if (state === "likely_uninsured") return "text-warn";
      return "text-slate-400";
    },

    // ── Quick lookup (hero) ───────────────────────────────────────────
    async quickLookup() {
      const q = this.lookup.query.trim();
      if (!q) return;
      this.lookup.running = true;
      this.lookup.error = false;
      this.lookup.status = "";
      this.openScanProgress(q, "Szukam w rejestrach i proszę Claude'a o rozpoznanie…");
      this.markStep("resolve", "active", { detail: `Analizuję wpis: „${q}"` });
      try {
        const res = await this.api("/api/companies/quick-lookup", {
          method: "POST",
          body: JSON.stringify({ query: q, scan: true }),
        });
        const resolvedName = res.resolved_name || (res.registry_record && res.registry_record.name) || q;
        this.scanProgress.company = resolvedName;
        let resolvedNote = "";
        if (res.resolved_from === "registry" && res.registry_record) {
          const srcs = (res.registry_record.sources || []).join(", ");
          resolvedNote = `Znaleziono w rejestrze (${srcs || "MF/KRS/CEIDG"})`;
          this.toast(`${res.registry_record.name} — dane z rejestru`);
        } else if (res.resolved_from === "ai") {
          resolvedNote = `AI rozpoznało: „${resolvedName}" (z wpisu „${q}")`;
          this.toast(`AI rozpoznało „${q}" jako ${resolvedName}`);
        } else {
          resolvedNote = `Kontynuuję jako: ${resolvedName}`;
        }
        this.scanProgress.resolvedNote = resolvedNote;
        this.markStep("resolve", "done", { detail: resolvedNote });
        this.markStep("registry", res.from_registry ? "done" : "active", {
          detail: res.from_registry
            ? `Dane z rejestru: ${(res.registry_record && res.registry_record.sources || []).join(" · ") || "MF/KRS/CEIDG"}`
            : "Tylko dane od AI — brak wpisu w rejestrach",
        });
        await this.openCompany(res.company_id);
        await this.loadCompanies();
        if (res.scan_job_id) {
          this.scanning = true;
          this.pollScan(res.scan_job_id).finally(() => { this.scanning = false; });
        } else {
          this.closeScanProgress(true, "Pominięto skan artykułów.");
        }
        this.lookup.query = "";
      } catch (e) {
        this.lookup.error = true;
        const msg = (e && e.message) ? e.message : "Nie udało się wyszukać firmy.";
        this.lookup.status = msg;
        this.markStep("resolve", "error", { detail: msg });
        this.closeScanProgress(false, msg);
        this.toast(msg);
      } finally {
        this.lookup.running = false;
      }
    },

    async regenerateInsights() {
      if (!this.selected || this.synthing) return;
      this.synthing = true;
      this.toast("Generuję syntezę AI…", "info", "🧠");
      try {
        const updated = await this.api(`/api/companies/${this.selected.id}/synthesize`, { method: "POST" });
        this.selected = updated;
        this.toast("Synteza zaktualizowana", "success", "✅");
      } catch (e) {
        this.toast("Błąd syntezy: " + e.message, "error");
      } finally {
        this.synthing = false;
      }
    },

    hasInsights(c) {
      if (!c) return false;
      return !!(c.ai_summary || (c.strengths && c.strengths.length) || (c.weaknesses && c.weaknesses.length) || c.investment_thesis);
    },

    hasRegistryData(c) {
      if (!c) return false;
      return !!(c.regon || c.krs || c.legal_form || c.pkd_primary || (c.pkd_all && c.pkd_all.length) || c.address || c.status_vat || (c.registry_sources && c.registry_sources.length));
    },

    registrySourceLabel(src) {
      return {
        "MF_WHITE_LIST": "Biała lista MF (VAT)",
        "GUS_BIR": "GUS BIR (REGON)",
        "CEIDG": "CEIDG (JDG)",
      }[src] || src;
    },
    registrySourceShort(src) {
      return {
        "MF_WHITE_LIST": "MF VAT",
        "GUS_BIR": "GUS REGON",
        "CEIDG": "CEIDG",
      }[src] || src;
    },

    async deleteCompany() {
      if (!this.selected) return;
      if (!confirm(`Usunąć "${this.selected.name}" wraz z wszystkimi artykułami?`)) return;
      try {
        await this.api(`/api/companies/${this.selected.id}`, { method: "DELETE" });
        this.toast("Spółka usunięta", "success");
        this.selected = null;
        await this.loadCompanies();
        await this.loadOverview();
        this.goto("companies");
      } catch (e) {
        this.toast("Błąd: " + e.message, "error");
      }
    },

    async startScan() {
      if (!this.selected || this.scanning) return;
      this.scanning = true;
      this.openScanProgress(this.selected.name);
      this.markStep("resolve", "done");
      this.markStep("registry", "done");
      try {
        const { job_id } = await this.api(`/api/companies/${this.selected.id}/scan`, { method: "POST" });
        await this.pollScan(job_id);
      } catch (e) {
        this.closeScanProgress(false, "Błąd: " + e.message);
        this.toast("Błąd: " + e.message, "error");
      } finally {
        this.scanning = false;
      }
    },

    openScanProgress(companyName, resolvedNote = "") {
      this.scanProgress.visible = true;
      this.scanProgress.company = companyName || "—";
      this.scanProgress.resolvedNote = resolvedNote || "";
      this.scanProgress.elapsed = 0;
      this.scanProgress.steps.forEach((s) => {
        s.status = "pending";
        if (s.id === "analyzing") { s.progress = 0; s.total = 0; s.detail = ""; }
        else { s.detail = ""; }
      });
      if (this.scanProgress._timer) clearInterval(this.scanProgress._timer);
      this.scanProgress._timer = setInterval(() => { this.scanProgress.elapsed += 1; }, 1000);
    },

    closeScanProgress(ok = true, finalDetail = "") {
      if (this.scanProgress._timer) { clearInterval(this.scanProgress._timer); this.scanProgress._timer = null; }
      if (ok) {
        this.scanProgress.steps.forEach((s) => { if (s.status !== "error") s.status = "done"; });
      }
      if (finalDetail) {
        const last = this.scanProgress.steps[this.scanProgress.steps.length - 1];
        if (last) last.detail = finalDetail;
      }
      setTimeout(() => { this.scanProgress.visible = false; }, ok ? 900 : 2500);
    },

    markStep(id, status, extras = {}) {
      const steps = this.scanProgress.steps;
      const idx = steps.findIndex((s) => s.id === id);
      if (idx === -1) return;
      // Any step before the active one becomes done; the active one gets `status`.
      for (let i = 0; i < idx; i++) if (steps[i].status !== "error") steps[i].status = "done";
      steps[idx].status = status;
      if (extras.detail !== undefined) steps[idx].detail = extras.detail;
      if (extras.progress !== undefined) steps[idx].progress = extras.progress;
      if (extras.total !== undefined) steps[idx].total = extras.total;
    },

    _stageToStepId(stage) {
      return ({
        scraping: "scraping",
        registry: "registry",
        analyzing: "analyzing",
        events: "events",
        financials: "financials",
        balance_ai: "balance_ai",
        contracts: "contracts",
        insurance: "insurance",
        payments: "payments",
        governance: "governance",
        regulatory: "regulatory",
        limit: "limit",
        verdict: "verdict",
        synth: "synth",
        done: null,
      })[stage] || null;
    },

    async pollScan(jobId) {
      for (let i = 0; i < 300; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        try {
          const job = await this.api(`/api/scans/${jobId}`);
          const stepId = this._stageToStepId(job.stage);
          if (stepId) {
            const extras = { detail: job.stage_detail || "" };
            if (job.stage === "analyzing") {
              extras.progress = job.articles_analyzed || 0;
              extras.total = job.sources_found || 0;
            }
            this.markStep(stepId, "active", extras);
          }
          if (job.status === "done") {
            this.closeScanProgress(true, job.message || "");
            this.toast(`Skan zakończony — ${job.articles_analyzed}/${job.sources_found} artykułów`, "success", "✅");
            if (this.selected) await this.openCompany(this.selected.id);
            this.refreshOverview();
            return;
          }
          if (job.status === "error") {
            this.closeScanProgress(false, job.message || "nieznany błąd");
            this.toast("Skan nieudany: " + (job.message || "nieznany błąd"), "error");
            return;
          }
        } catch (e) {
          // transient — keep polling
        }
      }
      this.closeScanProgress(false, "Przekroczono czas oczekiwania");
      this.toast("Przekroczono czas oczekiwania na skan", "error");
    },

    // ── Add company ───────────────────────────────────────────────────
    async submitCompany() {
      if (!this.newCompany.name.trim()) return;
      this.submitting = true;
      const payload = {
        name: this.newCompany.name.trim(),
        aliases: this.newCompany.aliasesRaw.split(",").map((s) => s.trim()).filter(Boolean),
        nip: this.newCompany.nip.trim() || null,
        krs: this.newCompany.krs.trim() || null,
        ticker: this.newCompany.ticker.trim() || null,
        sector: this.newCompany.sector.trim() || null,
      };
      try {
        const created = await this.api("/api/companies", { method: "POST", body: JSON.stringify(payload) });
        this.toast(`Dodano: ${created.name}`, "success", "🎉");
        this.showAdd = false;
        const scanNow = this.newCompany.scanNow;
        this.newCompany = { name: "", nip: "", krs: "", ticker: "", sector: "", aliasesRaw: "", scanNow: true };
        await this.loadCompanies();
        await this.openCompany(created.id);
        if (scanNow) this.startScan();
      } catch (e) {
        this.toast("Błąd: " + e.message, "error");
      } finally {
        this.submitting = false;
      }
    },

    // ── Search ────────────────────────────────────────────────────────
    async runSearch() {
      if (!this.searchQuery.trim()) { this.searchResults = null; return; }
      try {
        this.searchResults = await this.api(`/api/search?q=${encodeURIComponent(this.searchQuery.trim())}`);
      } catch (e) { this.searchResults = []; }
    },

    get filteredCompanies() {
      if (this.searchResults !== null) return this.searchResults;
      return this.companies;
    },

    // Treat `mentions_company === null/undefined` as "probably relevant" (older
     // rows without analysis) so we never silently hide everything.
    _mentionsCompany(a) { return a.mentions_company !== false; },

    get filteredArticles() {
      const arts = this.articles || [];
      const relevant = arts.filter(this._mentionsCompany);
      switch (this.articleFilter) {
        case "relevant": return relevant;
        case "unrelated": return arts.filter((a) => a.mentions_company === false);
        case "high": return relevant.filter((a) => (a.severity ?? 0) >= 5 || ["high", "critical"].includes(a.risk_level));
        case "flagged": return relevant.filter((a) => (a.red_flags || []).length > 0);
        case "positive": return relevant.filter((a) => (a.sentiment_score ?? 0) > 0.2 || a.investment_impact === "positive" || (a.positive_points || []).length > 0);
        case "trusted": return relevant.filter((a) => (a.credibility_score ?? 0) >= 0.7 && !a.is_likely_fake);
        case "fake": return relevant.filter((a) => a.is_likely_fake || (a.credibility_score ?? 1) < 0.4);
        case "all":
        default: return arts;
      }
    },

    get unrelatedCount() {
      return (this.articles || []).filter((a) => a.mentions_company === false).length;
    },

    // ── KPIs ──────────────────────────────────────────────────────────
    get kpis() {
      const o = this.overview || {};
      return [
        { label: "Monitorowane spółki", value: o.companies_total ?? 0, sub: `${o.high_risk_companies ?? 0} wysokiego ryzyka`, icon: "🏢" },
        { label: "Artykuły w bazie", value: o.articles_total ?? 0, sub: `${o.articles_analyzed ?? 0} analizowanych AI`, icon: "📰" },
        { label: "Ostatnie 24h", value: o.articles_last_24h ?? 0, sub: "nowe artykuły", icon: "⏱️" },
        { label: "Średni wynik", value: (o.average_score ?? 0).toFixed(1), sub: "top-10 wg ryzyka", icon: "📈" },
      ];
    },

    // ── Charts ────────────────────────────────────────────────────────
    renderOverviewCharts() {
      this.renderTopRisksChart();
      this.renderCategoriesChart();
    },

    renderTopRisksChart() {
      const el = document.getElementById("topRisksChart");
      if (!el || !this.overview) return;
      const rows = (this.overview.top_risks || []).slice().reverse();
      const labels = rows.map((r) => r.name);
      const rep = rows.map((r) => r.score);
      const inv = rows.map((r) => r.investment_score);
      if (this._charts.top) this._charts.top.destroy();
      this._charts.top = new Chart(el, {
        type: "bar",
        data: {
          labels,
          datasets: [
            { label: "Reputacja", data: rep, backgroundColor: "rgba(122,162,255,0.75)", borderRadius: 6 },
            { label: "Inwestycyjne", data: inv, backgroundColor: "rgba(194,60,112,0.75)", borderRadius: 6 },
          ],
        },
        options: {
          indexAxis: "y",
          maintainAspectRatio: false,
          scales: {
            x: { max: 100, grid: { color: "rgba(255,255,255,0.06)" }, ticks: { color: "#94a3b8" } },
            y: { grid: { display: false }, ticks: { color: "#cbd5e1" } },
          },
          plugins: {
            legend: { labels: { color: "#cbd5e1", usePointStyle: true } },
            tooltip: { backgroundColor: "#111627", borderColor: "#242b4a", borderWidth: 1 },
          },
        },
      });
    },

    renderCategoriesChart() {
      const el = document.getElementById("categoriesChart");
      if (!el || !this.overview) return;
      const dist = this.overview.risk_category_distribution || {};
      const labels = Object.keys(dist).map((k) => CATEGORY_LABELS[k] || k);
      const values = Object.values(dist);
      if (this._charts.cats) this._charts.cats.destroy();
      if (!values.length) {
        const ctx = el.getContext("2d");
        ctx.clearRect(0, 0, el.width, el.height);
        ctx.fillStyle = "#64748b";
        ctx.font = "14px Inter";
        ctx.textAlign = "center";
        ctx.fillText("Brak danych — uruchom skan", el.width / 2, el.height / 2);
        return;
      }
      this._charts.cats = new Chart(el, {
        type: "doughnut",
        data: {
          labels,
          datasets: [
            {
              data: values,
              backgroundColor: [
                "#5a83ff", "#c23c70", "#f6c768", "#2fd8a6", "#ff6b6b",
                "#7aa2ff", "#b9ccff", "#5af0c4", "#ffd98a",
              ],
              borderColor: "#070a13",
              borderWidth: 3,
              hoverOffset: 6,
            },
          ],
        },
        options: {
          maintainAspectRatio: false,
          cutout: "60%",
          plugins: { legend: { position: "bottom", labels: { color: "#cbd5e1", boxWidth: 12 } } },
        },
      });
    },

    async renderCompanyChart(id) {
      const el = document.getElementById("companyTrendChart");
      if (!el) return;
      let history;
      try {
        history = await this.api(`/api/companies/${id}/score/history?days=90`);
      } catch { history = []; }
      if (this._charts.trend) this._charts.trend.destroy();
      if (!history.length) {
        const ctx = el.getContext("2d");
        ctx.clearRect(0, 0, el.width, el.height);
        ctx.fillStyle = "#64748b";
        ctx.font = "14px Inter";
        ctx.textAlign = "center";
        ctx.fillText("Brak historii — uruchom skan, aby rozpocząć trendy.", el.width / 2, el.height / 2);
        return;
      }
      const labels = history.map((r) => new Date(r.timestamp).toLocaleDateString("pl-PL"));
      this._charts.trend = new Chart(el, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: "Reputacja",
              data: history.map((r) => r.score),
              borderColor: "#7aa2ff",
              backgroundColor: "rgba(122,162,255,0.2)",
              tension: 0.35, fill: true, pointRadius: 3,
            },
            {
              label: "Ryzyko inwestycyjne",
              data: history.map((r) => r.investment_score ?? 0),
              borderColor: "#ff6b6b",
              backgroundColor: "rgba(255,107,107,0.15)",
              tension: 0.35, fill: true, pointRadius: 3,
            },
          ],
        },
        options: {
          maintainAspectRatio: false,
          interaction: { intersect: false, mode: "index" },
          scales: {
            x: { grid: { color: "rgba(255,255,255,0.04)" }, ticks: { color: "#94a3b8" } },
            y: { max: 100, min: 0, grid: { color: "rgba(255,255,255,0.06)" }, ticks: { color: "#94a3b8" } },
          },
          plugins: {
            legend: { labels: { color: "#cbd5e1", usePointStyle: true } },
            tooltip: { backgroundColor: "#111627", borderColor: "#242b4a", borderWidth: 1 },
          },
        },
      });
    },

    // ── Formatters ────────────────────────────────────────────────────
    fmt(n) { return (n ?? 0).toFixed(1); },
    fmtDate(d) { if (!d) return "—"; try { return new Date(d).toLocaleString("pl-PL", { dateStyle: "medium", timeStyle: "short" }); } catch { return d; } },
    prettyCategory(id) { return CATEGORY_LABELS[id] || id; },
    recommendationHeadline(rec) { return RECOMMENDATION_HEADLINES[rec] || "Brak rekomendacji"; },

    scoreColor(score) {
      if (score == null) return "#64748b";
      if (score >= 75) return "#c23c70";
      if (score >= 55) return "#ff6b6b";
      if (score >= 35) return "#f6c768";
      return "#2fd8a6";
    },

    recBadge(rec) {
      const base = "rec-badge ";
      return base + ({ Proceed: "rec-proceed", Monitor: "rec-monitor", Caution: "rec-caution", Avoid: "rec-avoid" }[rec] || "rec-unknown");
    },
    recBadgeLarge(rec) {
      const base = "rec-badge-lg ";
      return base + ({ Proceed: "rec-proceed", Monitor: "rec-monitor", Caution: "rec-caution", Avoid: "rec-avoid" }[rec] || "rec-unknown");
    },

    severityPill(sev) {
      if (sev == null) return "sev-pill sev-low";
      if (sev >= 8) return "sev-pill sev-crit";
      if (sev >= 5.5) return "sev-pill sev-high";
      if (sev >= 3) return "sev-pill sev-med";
      return "sev-pill sev-low";
    },

    riskLevelBadge(lvl) {
      return "level-badge " + ({ critical: "level-critical", high: "level-high", medium: "level-medium", low: "level-low" }[lvl] || "level-none");
    },

    sentimentPill(score) {
      const s = score ?? 0;
      if (s <= -0.6) return "sentiment-pill sentiment-very-negative";
      if (s <= -0.2) return "sentiment-pill sentiment-negative";
      if (s < 0.2) return "sentiment-pill sentiment-neutral";
      if (s < 0.6) return "sentiment-pill sentiment-positive";
      return "sentiment-pill sentiment-very-positive";
    },
    sentimentText(score, label) {
      if (label) return label.replace(/_/g, " ");
      if (score == null) return "brak";
      if (score <= -0.2) return `neg ${score.toFixed(2)}`;
      if (score < 0.2) return `neutral ${score.toFixed(2)}`;
      return `poz ${score.toFixed(2)}`;
    },

    credibilityPill(score) {
      const s = score ?? 0.7;
      if (s >= 0.75) return "cred-pill cred-high";
      if (s >= 0.55) return "cred-pill cred-med";
      if (s >= 0.35) return "cred-pill cred-low";
      return "cred-pill cred-fake";
    },
    credibilityText(score) {
      if (score == null) return "wiarygodność ?";
      const pct = Math.round(score * 100);
      if (score >= 0.75) return `✓ wiarygodne ${pct}%`;
      if (score >= 0.55) return `~ średnie ${pct}%`;
      if (score >= 0.35) return `⚠ niska ${pct}%`;
      return `⚠ fake ${pct}%`;
    },

    // ── Analysis status / verdict helpers ─────────────────────────────
    analysisStatus(c) {
      if (!c) return "unknown";
      return c.analysis_status || c.verdict_status || (c.current_score == null ? "never_scanned" : "scored");
    },
    analysisStatusLabel(c) {
      return {
        scored: "Przeanalizowana",
        insufficient_evidence: "Niewystarczające dane",
        offline_fallback: "Ocena heurystyczna",
        never_scanned: "Nieprzeanalizowana",
        scanning: "Skanowanie…",
      }[this.analysisStatus(c)] || "Nieznany";
    },
    analysisStatusClass(c) {
      return {
        scored: "status-pill status-scored",
        insufficient_evidence: "status-pill status-insufficient",
        offline_fallback: "status-pill status-offline",
        never_scanned: "status-pill status-new",
        scanning: "status-pill status-scanning",
      }[this.analysisStatus(c)] || "status-pill status-new";
    },
    confidenceLabel(c) {
      return { low: "niska pewność", medium: "średnia pewność", high: "wysoka pewność" }[c?.confidence] || "—";
    },
    confidenceClass(c) {
      return "conf-pill " + ({ low: "conf-low", medium: "conf-med", high: "conf-high" }[c?.confidence] || "conf-low");
    },
    hasVerdict(c) {
      if (!c) return false;
      return !!(c.rationale && c.rationale.length) || !!(c.key_concerns && c.key_concerns.length) || !!(c.key_positives && c.key_positives.length) || !!c.recommendation;
    },
    isInsufficient(c) {
      return c && (c.verdict_status === "insufficient_evidence" || c.analysis_status === "insufficient_evidence");
    },

    // ── Toasts ────────────────────────────────────────────────────────
    toast(msg, kind = "info", icon = "ℹ️") {
      const id = Date.now() + Math.random();
      const item = { id, msg, kind, icon };
      this.toasts.push(item);
      setTimeout(() => { this.toasts = this.toasts.filter((t) => t.id !== id); }, 4500);
    },
  };
}
