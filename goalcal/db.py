from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_engine: Engine | None = None
_engine_url: str | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _engine_url, _SessionFactory
    target = url or settings.db_url
    if _engine is None or _engine_url != target:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(
            target,
            future=True,
            connect_args={"check_same_thread": False}
            if target.startswith("sqlite")
            else {},
        )
        _engine_url = target
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db(url: str | None = None) -> Engine:
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    return engine


def new_session() -> Session:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
