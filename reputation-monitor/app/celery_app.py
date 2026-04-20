from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "reputation_monitor",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=["app.scraper.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    "scrape-all-companies-every-6h": {
        "task": "app.scraper.tasks.scrape_all_companies",
        "schedule": crontab(minute=0, hour="*/6"),
    },
}
