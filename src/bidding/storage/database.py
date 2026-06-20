from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bidding.models.db import Base

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_DEFAULT_URL = f"sqlite+aiosqlite:///{_DATA_DIR / 'bidding.db'}"

_engine = None
_session_factory = None


def get_engine(url: str | None = None):
    global _engine
    if _engine is None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url or _DEFAULT_URL, echo=False)
    return _engine


def get_session_factory(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        engine = get_engine(url)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factory


async def init_db(url: str | None = None):
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
