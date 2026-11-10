"""Планировщик и один исполнитель задач с атомарным захватом через PostgreSQL."""

import logging
import os
import time
from datetime import timedelta

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select

from app.db import Item, Job, Search, Session, new_job, now
from parser import BlockedError, browser_options, extract, load_page, search_url


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
DELAY = max(1.0, float(os.getenv("PAGE_DELAY_SECONDS", "3")))


def schedule():
    with Session.begin() as db:
        due = db.scalars(select(Search).where(Search.enabled.is_(True), Search.interval_hours > 0, Search.next_run_at <= now()).with_for_update(skip_locked=True)).all()
        for search in due:
            busy = db.scalar(select(Job.id).where(Job.search_id == search.id, Job.status.in_(["queued", "running"])))
            if not busy:
                db.add(new_job(search))
            search.next_run_at = now() + timedelta(hours=search.interval_hours)


def claim() -> int | None:
    with Session.begin() as db:
        job = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.id).with_for_update(skip_locked=True).limit(1))
        if job:
            job.status = "running"
            job.started_at = now()
            return job.id
    return None


def process(job_id: int):
    with Session() as db:
        job = db.get(Job, job_id)
        search = db.get(Search, job.search_id)
        query = job.query_snapshot or search.query
        sites = (job.sites_snapshot or search.sites).split(",")
        pages = job.pages_snapshot or search.pages
    errors = []
    with Session() as db:
        count = db.scalar(select(func.count(Item.id)).where(Item.job_id == job_id)) or 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"], **browser_options())
            try:
                context = browser.new_context(locale="ru-RU", viewport={"width": 1366, "height": 900})
                page = context.new_page()
                for site in sites:
                    for number in range(1, pages + 1):
                        try:
                            listings = extract(site, query, load_page(page, search_url(site, query, number)))
                            if not listings:
                                errors.append(f"{site}/{number}: карточки не найдены (выдача пуста, блокировка или изменилась разметка)")
                            with Session.begin() as db:
                                existing = set(db.scalars(select(Item.url).where(Item.job_id == job_id)).all())
                                for listing in listings:
                                    if listing.url in existing:
                                        continue
                                    db.add(Item(job_id=job_id, site=site, title=listing.title[:500], price=int(listing.price) if listing.price else None, url=listing.url, image=listing.image))
                                    existing.add(listing.url)
                                    count += 1
                            log.info("job=%s site=%s page=%s items=%s", job_id, site, number, len(listings))
                        except BlockedError as exc:
                            log.warning("job=%s site=%s blocked: %s", job_id, site, exc)
                            errors.append(f"{site}/{number}: {exc}")
                            break
                        except Exception as exc:
                            log.exception("job=%s site=%s page=%s", job_id, site, number)
                            errors.append(f"{site}/{number}: {str(exc)[:300]}")
                        time.sleep(DELAY)
                context.close()
            finally:
                browser.close()
    except Exception as exc:
        log.exception("job=%s browser failure", job_id)
        errors.append(f"Браузер: {str(exc)[:300]}")
    with Session.begin() as db:
        job = db.get(Job, job_id)
        job.status = "partial" if errors and count else "failed" if errors else "done"
        job.error = "\n".join(errors)[:10000]
        job.found = count
        job.finished_at = now()


def main():
    # Таблицы создаёт веб-приложение при старте.
    # Задания, прерванные перезапуском контейнера, возвращаются в очередь.
    with Session.begin() as db:
        for job in db.scalars(select(Job).where(Job.status == "running").with_for_update(skip_locked=True)):
            job.status = "queued"
    while True:
        try:
            schedule()
            job_id = claim()
            if job_id:
                process(job_id)
            else:
                time.sleep(5)
        except Exception:
            log.exception("worker loop")
            time.sleep(10)


if __name__ == "__main__":
    main()
