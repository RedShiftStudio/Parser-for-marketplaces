import csv
import io
import os
import secrets
from datetime import timedelta
from hashlib import scrypt
from hmac import compare_digest

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from app.db import Base, Item, Job, Search, Session, User, engine, new_job, now
from parser import BASE


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify(password: str, stored: str) -> bool:
    try:
        salt = bytes.fromhex(stored.split(":")[0])
        return compare_digest(password_hash(password, salt), stored)
    except (ValueError, IndexError):
        return False


app = FastAPI(title="Marketplace CRM", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ["SECRET_KEY"], same_site="lax", https_only=os.getenv("COOKIE_SECURE") == "1")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    # Совместимость с базой, созданной ранней версией проекта.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS query_snapshot VARCHAR(300)"))
        connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sites_snapshot VARCHAR(100)"))
        connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pages_snapshot INTEGER"))
    with Session.begin() as db:
        email = os.environ["ADMIN_EMAIL"].strip().lower()
        if not db.scalar(select(User).where(User.email == email)):
            db.add(User(email=email, password_hash=password_hash(os.environ["ADMIN_PASSWORD"])))


def current_user(request: Request, db):
    user_id = request.session.get("user_id")
    if not user_id or not (user := db.get(User, user_id)):
        raise HTTPException(401, "Требуется вход")
    return user


def csrf(request: Request, token: str):
    if not token or not compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "Неверный CSRF-токен")


def view(request: Request, name: str, **data):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **data})


def go(path: str):
    return RedirectResponse(path, status_code=303)


def safe_cell(value):
    """Не дать табличным редакторам выполнить текст сайта как формулу."""
    value = str(value if value is not None else "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def csv_stream(rows, headers):
    output = io.StringIO()
    writer = csv.writer(output)
    yield "\ufeff"
    writer.writerow(headers)
    yield output.getvalue()
    for row in rows:
        output.seek(0)
        output.truncate(0)
        writer.writerow([safe_cell(value) for value in row])
        yield output.getvalue()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return view(request, "login.html", error="")


@app.get("/health")
def health():
    with Session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/login")
def login(request: Request, email: str = Form(), password: str = Form(), token: str = Form()):
    csrf(request, token)
    with Session() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if not user or not verify(password, user.password_hash):
            return view(request, "login.html", error="Неверные учётные данные")
        request.session.clear()
        request.session["user_id"] = user.id
        return go("/")


@app.post("/logout")
def logout(request: Request, token: str = Form()):
    csrf(request, token)
    request.session.clear()
    return go("/login")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with Session() as db:
        user = current_user(request, db)
        searches = db.scalars(select(Search).where(Search.owner_id == user.id).order_by(Search.id.desc())).all()
        jobs = db.scalars(select(Job).join(Search).where(Search.owner_id == user.id).options(selectinload(Job.search)).order_by(Job.id.desc()).limit(30)).all()
        summaries = db.execute(select(Search.id, func.count(Item.id), func.min(Item.price), func.max(Item.price)).select_from(Search).join(Job, Job.search_id == Search.id, isouter=True).join(Item, Item.job_id == Job.id, isouter=True).where(Search.owner_id == user.id).group_by(Search.id)).all()
        summary = {search_id: {"count": count, "min": low, "max": high} for search_id, count, low, high in summaries}
        return view(request, "index.html", user=user, searches=searches, jobs=jobs, sites=BASE, summary=summary)


@app.post("/searches")
def create_search(request: Request, name: str = Form(), query: str = Form(), sites: list[str] = Form(), pages: int = Form(1), interval_hours: int = Form(0), token: str = Form()):
    csrf(request, token)
    if not name.strip() or len(name) > 120 or not query.strip() or len(query) > 300 or not set(sites) <= set(BASE) or not 1 <= pages <= 10 or not 0 <= interval_hours <= 720:
        raise HTTPException(422, "Проверьте параметры запроса")
    with Session.begin() as db:
        user = current_user(request, db)
        search = Search(owner_id=user.id, name=name.strip(), query=query.strip(), sites=",".join(dict.fromkeys(sites)), pages=pages, interval_hours=interval_hours)
        db.add(search)
        db.flush()
        db.add(new_job(search))
        if interval_hours:
            search.next_run_at = now() + timedelta(hours=interval_hours)
    return go("/")


def owned_search(db, request, search_id):
    user = current_user(request, db)
    search = db.get(Search, search_id)
    if not search or search.owner_id != user.id:
        raise HTTPException(404)
    return search


@app.get("/searches/{search_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, search_id: int):
    with Session() as db:
        search = owned_search(db, request, search_id)
        return view(request, "edit.html", search=search, user=current_user(request, db), sites=BASE)


@app.post("/searches/{search_id}/edit")
def edit_search(request: Request, search_id: int, name: str = Form(), query: str = Form(), sites: list[str] = Form(), pages: int = Form(), interval_hours: int = Form(), token: str = Form()):
    csrf(request, token)
    if not name.strip() or len(name) > 120 or not query.strip() or len(query) > 300 or not set(sites) <= set(BASE) or not 1 <= pages <= 10 or not 0 <= interval_hours <= 720:
        raise HTTPException(422, "Проверьте параметры запроса")
    with Session.begin() as db:
        search = owned_search(db, request, search_id)
        search.name = name.strip()
        search.query = query.strip()
        search.sites = ",".join(dict.fromkeys(sites))
        search.pages = pages
        search.interval_hours = interval_hours
        search.next_run_at = now() + timedelta(hours=interval_hours) if interval_hours else None
    return go("/")


@app.post("/searches/{search_id}/run")
def run_search(request: Request, search_id: int, token: str = Form()):
    csrf(request, token)
    with Session.begin() as db:
        search = owned_search(db, request, search_id)
        if not db.scalar(select(Job.id).where(Job.search_id == search_id, Job.status.in_(["queued", "running"]))):
            db.add(new_job(search))
    return go("/")


@app.post("/searches/{search_id}/toggle")
def toggle_search(request: Request, search_id: int, token: str = Form()):
    csrf(request, token)
    with Session.begin() as db:
        search = owned_search(db, request, search_id)
        search.enabled = not search.enabled
        if search.enabled and search.interval_hours:
            search.next_run_at = now() + timedelta(hours=search.interval_hours)
    return go("/")


def owned_job(db, request, job_id):
    user = current_user(request, db)
    job = db.scalar(select(Job).join(Search).where(Job.id == job_id, Search.owner_id == user.id).options(selectinload(Job.search)))
    if not job:
        raise HTTPException(404)
    return job


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int, site: str = "", page: int = 1):
    if page < 1 or (site and site not in BASE):
        raise HTTPException(422, "Недопустимый фильтр")
    with Session() as db:
        job = owned_job(db, request, job_id)
        query = select(Item).where(Item.job_id == job_id)
        if site:
            query = query.where(Item.site == site)
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        items = db.scalars(query.order_by(Item.id).offset((page - 1) * 50).limit(50)).all()
        return view(request, "job.html", job=job, items=items, site=site, page=page, total=total, pages=(total + 49) // 50)


@app.get("/jobs/{job_id}/export.csv")
def export(request: Request, job_id: int):
    with Session() as db:
        job = owned_job(db, request, job_id)
        query_text = job.query_snapshot or job.search.query

    def rows():
        with Session() as db:
            for item in db.scalars(select(Item).where(Item.job_id == job_id).order_by(Item.id)).yield_per(200):
                yield (item.site, query_text, item.title, item.price if item.price is not None else "", item.url, item.image, item.collected_at.isoformat())

    return StreamingResponse(csv_stream(rows(), ["site", "query", "title", "price_rub", "url", "image", "collected_at"]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="job-{job_id}.csv"'})


@app.get("/searches/{search_id}/export.csv")
def export_history(request: Request, search_id: int):
    with Session() as db:
        search = owned_search(db, request, search_id)
        current_query = search.query

    def rows():
        with Session() as db:
            statement = select(Job.id, Job.query_snapshot, Item.site, Item.title, Item.price, Item.url, Item.collected_at).join(Item, Item.job_id == Job.id).where(Job.search_id == search_id).order_by(Item.id)
            for job_id, snapshot, site, title, price, url, collected_at in db.execute(statement).yield_per(200):
                yield (job_id, snapshot or current_query, site, title, price if price is not None else "", url, collected_at.isoformat())

    return StreamingResponse(csv_stream(rows(), ["job_id", "query", "site", "title", "price_rub", "url", "collected_at"]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="search-{search_id}.csv"'})
