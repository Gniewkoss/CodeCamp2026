from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000").rstrip("/")


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=120.0) as client:
        r = client.get(f"{API_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


def api_post(path: str, json: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=300.0) as client:
        r = client.post(f"{API_URL}{path}", json=json)
        r.raise_for_status()
        return r.json()


def risk_badge(score: float | None) -> str:
    if score is None:
        return "⚪ Unknown"
    if score >= 60:
        return "🔴 High"
    if score >= 30:
        return "🟡 Medium"
    return "🟢 Low"


def score_color(score: float | None) -> str:
    if score is None:
        return "#888"
    if score >= 60:
        return "#c0392b"
    if score >= 30:
        return "#f39c12"
    return "#27ae60"


st.set_page_config(page_title="Reputation Monitor", layout="wide")
st.title("Company Reputation Monitoring")

PAGES = ("Company search", "Company detail", "Add company", "Risk dashboard")
if "nav_page" not in st.session_state:
    st.session_state["nav_page"] = PAGES[0]
_nav = st.session_state["nav_page"]
_idx = PAGES.index(_nav) if _nav in PAGES else 0
page = st.sidebar.radio("Pages", PAGES, index=_idx)
st.session_state["nav_page"] = page

if page == "Company search":
    st.subheader("Company search")
    q = st.text_input("Name or NIP", placeholder="e.g. Orlen or 7740001454")
    if st.button("Search") and q.strip():
        try:
            companies = api_get("/search", {"q": q.strip()})
        except Exception as e:
            st.error(f"API error: {e}")
            companies = []
        if not companies:
            st.info("No matches.")
        else:
            enriched = []
            for c in companies:
                try:
                    d = api_get(f"/companies/{c['id']}")
                    enriched.append(d)
                except Exception:
                    enriched.append({**c, "current_score": None})
            for row in enriched:
                cols = st.columns([3, 1, 1])
                with cols[0]:
                    st.write(f"**{row['name']}** — NIP: {row.get('nip') or '—'}")
                with cols[1]:
                    st.markdown(risk_badge(row.get("current_score")))
                with cols[2]:
                    if st.button("Open", key=f"open_{row['id']}"):
                        st.session_state["company_id"] = row["id"]
                        st.session_state["nav_page"] = "Company detail"
                        st.rerun()

if page == "Company detail":
    cid = st.session_state.get("company_id")
    if not cid:
        cid = st.text_input("Company UUID", placeholder="paste id from search")
    if cid:
        try:
            detail = api_get(f"/companies/{cid}")
        except Exception as e:
            st.error(f"API error: {e}")
            detail = None
        if detail:
            st.subheader(detail["name"])
            st.caption(f"Aliases: {', '.join(detail.get('aliases') or []) or '—'}")
            st.write(f"NIP: {detail.get('nip') or '—'}  |  KRS: {detail.get('krs') or '—'}")
            if st.button("Run scan now (fetch news + analyze + rescore)", type="primary"):
                try:
                    with st.spinner("Scanning…"):
                        api_post(f"/companies/{cid}/scan")
                    st.success("Scan finished — refresh numbers below.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            score = detail.get("current_score")
            st.markdown(
                f"<div style='font-size:48px;font-weight:700;color:{score_color(score)}'>"
                f"{score if score is not None else '—'}</div>",
                unsafe_allow_html=True,
            )
            st.caption(risk_badge(score))
            try:
                hist = api_get(f"/companies/{cid}/score/history", {"days": 90})
            except Exception:
                hist = []
            if hist:
                df = pd.DataFrame(hist)
                fig = px.line(df, x="timestamp", y="score", markers=True, title="Risk score (90 days)")
                st.plotly_chart(fig, use_container_width=True)
            try:
                arts = api_get(f"/companies/{cid}/articles", {"limit": 80})
            except Exception:
                arts = []
            cats: dict[str, int] = {}
            for a in arts:
                rc = a.get("risk_category") or "unknown"
                cats[rc] = cats.get(rc, 0) + 1
            if cats:
                pie = go.Figure(data=[go.Pie(labels=list(cats.keys()), values=list(cats.values()), hole=0.35)])
                pie.update_layout(title="Articles by dominant risk category")
                st.plotly_chart(pie, use_container_width=True)
            if arts:
                st.subheader("Recent articles")
                rows = []
                for a in arts:
                    sent = a.get("sentiment_score")
                    bar = "—"
                    if sent is not None:
                        pct = int(max(0, min(100, (sent + 1) * 50)))
                        bar = f"{sent:+.2f} " + ("█" * (pct // 10)).ljust(10, "░")
                    rows.append(
                        {
                            "title": a.get("title") or a.get("url"),
                            "source": a.get("source"),
                            "date": str(a.get("published_at") or "")[:19],
                            "keywords": ", ".join(a.get("risk_keywords") or []),
                            "sentiment": bar,
                        }
                    )
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

if page == "Add company":
    st.subheader("Add company")
    with st.form("add"):
        name = st.text_input("Company name")
        aliases = st.text_input("Aliases (comma-separated)", placeholder="Alias 1, Alias 2")
        nip = st.text_input("NIP")
        krs = st.text_input("KRS")
        scan_now = st.checkbox("Trigger immediate scan after save")
        submitted = st.form_submit_button("Save")
    if submitted:
        if not name.strip():
            st.error("Name required")
        else:
            payload = {
                "name": name.strip(),
                "aliases": [a.strip() for a in aliases.split(",") if a.strip()],
                "nip": nip.strip() or None,
                "krs": krs.strip() or None,
            }
            try:
                created = api_post("/companies", json=payload)
                st.success(f"Created {created['id']}")
                if scan_now:
                    with st.spinner("Scanning… this may take a few minutes."):
                        api_post(f"/companies/{created['id']}/scan")
                    st.success("Scan complete")
            except Exception as e:
                st.error(str(e))

if page == "Risk dashboard":
    st.info(
        "Scores come from **analyzed news** (RSS, GDELT, NewsAPI). "
        "If `articles_count` is 0, open **Company detail** and click **Run scan now**, "
        "or use `POST /companies/{id}/scan`. A score of **0** can mean either no articles yet "
        "or no risk signals in the lexicon for the last 90 days."
    )
    st.subheader("Top 10 riskiest companies")
    try:
        top = api_get("/dashboard/top-risks")
    except Exception as e:
        st.error(str(e))
        top = {"companies": []}
    df_top = pd.DataFrame(top.get("companies", []))
    if not df_top.empty:
        st.dataframe(df_top, use_container_width=True, hide_index=True)
    st.subheader("Recent high-risk articles (48h, severity ≥ 5)")
    cat = st.selectbox("Filter by risk category", ["(all)", "corruption", "legal", "management", "sanctions", "financial", "regulatory"])
    params: dict[str, Any] = {"hours": 48, "min_severity": 5.0}
    if cat != "(all)":
        params["category"] = cat
    try:
        arts = api_get("/dashboard/high-risk-articles", params)
    except Exception as e:
        st.error(str(e))
        arts = []
    if arts:
        st.dataframe(pd.DataFrame(arts), use_container_width=True, hide_index=True)
    else:
        st.info(
            "No rows match: only articles **analyzed in the last 48 hours** with **severity ≥ 5** "
            "are listed. After a scan, most neutral headlines stay below 5 — try the company "
            "article table for keywords and sentiment, or temporarily lower `min_severity` via the API."
        )
