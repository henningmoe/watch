"""Postgres persistence layer for Cermaq Watch. Falls back gracefully if DATABASE_URL is unset."""

import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Base = None
engine = None
SessionLocal = None

if DATABASE_URL:
    try:
        from sqlalchemy import (
            create_engine, Column, Integer, String, Boolean,
            DateTime, JSON, Text, ForeignKey,
        )
        from sqlalchemy.orm import declarative_base, sessionmaker

        Base = declarative_base()
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        SessionLocal = sessionmaker(bind=engine)
        logger.info("Postgres-tilkobling etablert")
    except Exception as e:
        logger.error("Postgres-tilkobling feilet: %s", e)
        engine = None
        SessionLocal = None

# Define models only when SQLAlchemy is available
if Base is not None:
    from sqlalchemy import (
        create_engine, Column, Integer, String, Boolean,
        DateTime, JSON, Text, ForeignKey,
    )

    class Source(Base):
        __tablename__ = "sources"
        id = Column(Integer, primary_key=True, autoincrement=True)
        domain = Column(String(100), unique=True, index=True)
        name = Column(String(150))
        type = Column(String(30))
        region = Column(String(20))
        language = Column(String(10))
        is_paywalled = Column(Boolean, default=False)
        miniflux_feed_id = Column(Integer, nullable=True)
        added_at = Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
        )

    class Article(Base):
        __tablename__ = "articles"
        id = Column(Integer, primary_key=True)
        title = Column(Text)
        url = Column(Text, index=True)
        source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)
        source_domain = Column(String(100), index=True)
        source_name = Column(String(150))
        fetch_method = Column(String(30), index=True)
        fetched_at = Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
        )
        published_at = Column(DateTime(timezone=True), index=True)
        scope = Column(String(20), index=True)
        region = Column(String(20), index=True)
        tone = Column(String(20), index=True)
        category = Column(String(30), index=True)
        relevance = Column(Integer, index=True)
        summaries = Column(JSON)
        image_url = Column(Text)
        content = Column(Text)
        classified_at = Column(DateTime(timezone=True))
        updated_at = Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
        )

    class Digest(Base):
        __tablename__ = "digests"
        id = Column(Integer, primary_key=True, autoincrement=True)
        lang = Column(String(2), nullable=False, index=True)
        headline = Column(Text)
        body = Column(Text)
        article_ids = Column(JSON)
        web_search_urls = Column(JSON)
        article_count = Column(Integer)
        cermaq_count = Column(Integer)
        search_count = Column(Integer)
        generated_at = Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
        )

    class Alert(Base):
        __tablename__ = "alerts"
        id = Column(Integer, primary_key=True, autoincrement=True)
        name = Column(String(150), nullable=False)
        description = Column(Text)
        frequency = Column(String(30))
        email = Column(String(150), nullable=False)
        is_active = Column(Boolean, default=True)
        created_at = Column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
        )

else:
    # Stub classes so imports don't fail when DB is unavailable
    class Source:  # type: ignore[no-redef]
        pass

    class Article:  # type: ignore[no-redef]
        pass

    class Digest:  # type: ignore[no-redef]
        pass

    class Alert:  # type: ignore[no-redef]
        pass


def init_db() -> None:
    if engine is not None and Base is not None:
        Base.metadata.create_all(engine)
        logger.info("Postgres-tabeller opprettet/verifisert")


def is_db_available() -> bool:
    return engine is not None


def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return (url or "")[:100]


def get_or_create_source(session, domain: str, name: str = None, **kwargs):
    source = session.query(Source).filter_by(domain=domain).first()
    if not source:
        source = Source(domain=domain, name=name or domain, **kwargs)
        session.add(source)
        session.commit()
    return source
