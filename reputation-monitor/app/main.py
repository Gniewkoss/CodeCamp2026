from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from datetime import datetime, timezone

from sqlalchemy import update

from app.api.finance_routes import router as finance_router
from app.api.risk_routes import router as risk_router
from app.api.routes import router
from app.database import SessionLocal, init_db
from app.models import ScanJob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Reputation Monitor",
    description="AI-powered media reputation & investment-risk monitoring for AML / due-diligence.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(risk_router)
app.include_router(finance_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # If the server was killed mid-scan, ScanJob rows are left in ``running``
    # forever and the UI shows "0 articles" from a stuck job. Flip them to
    # ``error`` on startup so the next scan can take over cleanly.
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        res = db.execute(
            update(ScanJob)
            .where(ScanJob.status == "running")
            .values(
                status="error",
                message="Skan przerwany przez restart serwera. Uruchom ponownie.",
                finished_at=now,
            )
        )
        if res.rowcount:
            logging.getLogger(__name__).info(
                "Marked %d stale running scan(s) as error on startup.", res.rowcount
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Startup scan-job sweep failed: %s", exc)
        db.rollback()
    finally:
        db.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ─── Static web UI ───────────────────────────────────────────────────────────

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(_WEB_DIR, "index.html"))
