from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from tracecore.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None) -> Engine:
    database_url = url or get_settings().database_url
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncGenerator[Session, None]:
    session = SessionLocal()
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        session.info["principal"] = principal
    try:
        yield session
    finally:
        session.close()
