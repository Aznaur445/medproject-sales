"""Database engines and sessions (async for web/bot, sync for Celery workers)."""

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def connect_args() -> dict[str, str]:
    schema = get_settings().db_schema
    return {"options": f"-c search_path={schema}"} if schema else {}


@lru_cache
def get_async_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args=connect_args()
    )


@lru_cache
def get_sync_engine() -> Engine:
    return create_engine(
        get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args=connect_args()
    )


@lru_cache
def _async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


@lru_cache
def _sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


def async_session() -> AsyncSession:
    return _async_sessionmaker()()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session


@contextmanager
def sync_session() -> Iterator[Session]:
    with _sync_sessionmaker()() as session:
        yield session
