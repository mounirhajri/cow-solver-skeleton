from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.persistence.db import get_engine, get_session_factory


def test_get_engine_returns_async_engine():
    engine = get_engine()
    assert isinstance(engine, AsyncEngine)


def test_get_engine_is_cached():
    e1 = get_engine()
    e2 = get_engine()
    assert e1 is e2


def test_session_factory_is_session_maker():
    factory = get_session_factory()
    assert isinstance(factory, async_sessionmaker)
