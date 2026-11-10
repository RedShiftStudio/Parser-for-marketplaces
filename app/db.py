import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def now():
    return datetime.now(timezone.utc)


database_url = URL.create(
    "postgresql+psycopg",
    username="marketplace",
    password=os.environ["POSTGRES_PASSWORD"],
    host=os.getenv("DATABASE_HOST", "db"),
    database="marketplace",
)
engine = create_engine(database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Search(Base):
    __tablename__ = "searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(String(300))
    sites: Mapped[str] = mapped_column(String(100))
    pages: Mapped[int] = mapped_column(Integer, default=1)
    interval_hours: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    jobs: Mapped[list["Job"]] = relationship(back_populates="search")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id"), index=True)
    query_snapshot: Mapped[str | None] = mapped_column(String(300))
    sites_snapshot: Mapped[str | None] = mapped_column(String(100))
    pages_snapshot: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")
    found: Mapped[int] = mapped_column(Integer, default=0)
    search: Mapped[Search] = relationship(back_populates="jobs")
    items: Mapped[list["Item"]] = relationship(back_populates="job")


def new_job(search: Search) -> Job:
    return Job(search_id=search.id, query_snapshot=search.query, sites_snapshot=search.sites, pages_snapshot=search.pages)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("job_id", "url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    site: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    price: Mapped[int | None] = mapped_column(Numeric(12, 0))
    url: Mapped[str] = mapped_column(Text)
    image: Mapped[str] = mapped_column(Text, default="")
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    job: Mapped[Job] = relationship(back_populates="items")
